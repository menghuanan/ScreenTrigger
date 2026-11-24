"""Evidence collection, per-task checking and cumulative completion audits.

Exit 0: requested gate passed; 1: checks not ready; 2: invalid input/tool error.
Only `capture -- ...` runs an explicitly supplied command. Audits verify evidence.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

from check_docs import ROOT, digest, validate as validate_docs
from check_model import build

STATE = "docs/agent/check-state.json"
SKIP = {"reports", ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "target", "dist", ".workbuddy"}
RESULTS = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "N/A"}


def record_time(record):
    review = record.get("review", {})
    # Reviewing an old run later must not override a more recent blocked/failed run.
    return (stamp(record["finished_at"]),
            stamp(review["at"] if record["status"] in ("PASS", "N/A") else record["finished_at"]))


def now():
    return datetime.now(timezone.utc).isoformat()


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must have timezone")
    return result


def load(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def save(path, data, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def local(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path outside workspace: {name}")
    return path


def baseline(root):
    files = {}
    resolved_root = root.resolve()
    for directory, directories, filenames in root.walk():
        directories[:] = [name for name in directories if name not in SKIP]
        for name in filenames:
            path = directory / name
            rel = path.relative_to(root).as_posix()
            if name in SKIP or rel == STATE or path.suffix in (".pyc", ".pyo") or not path.is_file():
                continue
            if not path.resolve().is_relative_to(resolved_root):
                raise ValueError(f"source symlink outside workspace: {rel}")
            files[rel] = digest(path)
    files = dict(sorted(files.items()))
    return {"sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "files": files}


def spec_hash(check):
    return hashlib.sha256(json.dumps(check, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_ref(root, name):
    path = local(root, name)
    if not path.is_file():
        raise ValueError(f"missing evidence/artifact: {name}")
    return {"path": path.relative_to(root).as_posix(), "sha256": digest(path)}


def verify_file(root, ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        return False
    try:
        return file_ref(root, ref["path"])["sha256"] == ref.get("sha256")
    except (OSError, ValueError):
        return False


def verify_run_evidence(root, ref, source_files):
    if verify_file(root, ref):
        return True
    if not source_files or not isinstance(ref, dict):
        return False
    name, sha = ref.get("path"), ref.get("sha256")
    if (not isinstance(name, str) or name not in source_files
            or not name.startswith("docs/") or name.startswith("docs/archive/") or not name.endswith(".md")):
        return False
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        return False
    # Only a previous run's mutable specification can use an exact-byte snapshot.
    snapshot = f"reports/raw/doc-sha256/{sha}.md"
    return verify_file(root, {"path": snapshot, "sha256": sha})


def record_errors(root, record, model, historical_sources=None):
    errors = []
    if not isinstance(record, dict):
        return ["evidence record must be an object"]
    if record.get("schema_version") != 1:
        errors.append("unsupported evidence schema")
    check = model["checks"].get(record.get("check_id"))
    if not check:
        return ["unknown check_id"]
    if not isinstance(record.get("run_id"), str) or not record["run_id"] or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in record["run_id"]):
        errors.append("invalid run_id")
    if record.get("status") not in RESULTS:
        errors.append("unknown result status")
    for field in ("baseline", "spec_sha256", "expected", "actual", "environment", "started_at", "finished_at"):
        if not record.get(field):
            errors.append(f"missing {field}")
    if record.get("spec_sha256") == spec_hash(check) and record.get("expected") != check["expected"]:
        errors.append("expected result does not match current check definition")
    if record.get("status") in ("PASS", "N/A"):
        environment = record.get("environment", {})
        if not isinstance(environment, dict) or any(not environment.get(k) for k in
                ("os", "hardware", "runtime_versions", "models", "datasets", "coverage")):
            errors.append("environment requires os/hardware/runtime_versions/models/datasets/coverage; explain inapplicable fields")
    try:
        if stamp(record["started_at"]) > stamp(record["finished_at"]) or stamp(record["finished_at"]) > datetime.now(timezone.utc):
            errors.append("invalid run timestamps")
    except (KeyError, ValueError, TypeError, AttributeError):
        errors.append("invalid run timestamps")
    mode = record.get("mode")
    if mode not in ("command", "manual"):
        errors.append("mode must be command or manual")
    if mode == "command":
        command = record.get("command", {})
        if (not isinstance(command.get("argv"), list) or not command["argv"]
                or any(not isinstance(arg, str) for arg in command["argv"])
                or type(command.get("exit_code")) is not int):
            errors.append("command argv/exit_code required")
        if record.get("status") == "PASS" and command.get("exit_code") != 0:
            errors.append("nonzero command cannot PASS")
    elif not record.get("steps"):
        errors.append("manual reproduction steps required")
    evidence = record.get("evidence", [])
    if record.get("status") in ("PASS", "FAIL", "N/A") and not evidence:
        errors.append("raw evidence required")
    if not isinstance(evidence, list) or not all(verify_run_evidence(root, ref, historical_sources) for ref in evidence):
        errors.append("evidence missing or hash mismatch")
    if record.get("artifact") is not None and not verify_file(root, record["artifact"]):
        errors.append("artifact missing or hash mismatch")
    if record.get("status") in ("PASS", "N/A"):
        review = record.get("review", {})
        if not review.get("by") or review.get("decision") != "ACCEPT" or not review.get("note"):
            errors.append("named evidence reviewer and acceptance note required")
        try:
            if stamp(review["at"]) < stamp(record["finished_at"]) or stamp(review["at"]) > datetime.now(timezone.utc):
                errors.append("review timestamp outside valid interval")
        except (KeyError, ValueError, TypeError, AttributeError):
            errors.append("invalid review timestamp")
    if record.get("status") == "N/A" and (not check["allow_na"] or not record.get("reason")):
        errors.append("N/A only allowed for designated checks with reviewed reason")
    return errors


def validate_state(state, model):
    errors = []
    if state.get("schema_version") != 1:
        errors.append("unsupported state schema")
    if set(state.get("tasks", {})) != set(model["tasks"]):
        errors.append("state must contain exactly all task IDs")
    for tid, entry in state.get("tasks", {}).items():
        if entry.get("implementation") not in ("NOT_STARTED", "IN_PROGRESS", "IMPLEMENTED") or not entry.get("owner"):
            errors.append(f"{tid}: invalid implementation state/owner")
    for field in ("issues", "scope_changes", "invalidations"):
        if not isinstance(state.get(field), list):
            errors.append(f"{field} must be a list")
    return errors


def evaluate(root, scope="baseline", task_id=None):
    model, state, current = build(root), load(root / STATE), baseline(root)
    errors = validate_state(state, model)
    docs = validate_docs(root)
    errors += docs["errors"]
    records = {}
    per_check = {key: [] for key in model["checks"]}
    for path in sorted((root / "reports/evidence").glob("*.json")):
        try:
            record = load(path)
            previous = isinstance(record, dict) and record.get("baseline") != current["sha256"]
            invalid = record_errors(root, record, model, current["files"] if previous else None)
            rid = record.get("run_id")
            if rid in records:
                invalid.append("duplicate run ID")
            if invalid:
                errors.append(f"{path.name}: {'; '.join(invalid)}")
                continue
            records[rid] = record
            per_check[record["check_id"]].append(record)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(f"{path.name}: malformed evidence: {exc}")

    invalidated = {}
    for event in state.get("invalidations", []):
        try:
            if not event.get("reason") or not event.get("by") or not event["check_ids"]:
                raise ValueError("invalidation requires reason/by/check_ids")
            at = stamp(event["at"])
            if at > datetime.now(timezone.utc):
                raise ValueError("future invalidation")
            for key in event["check_ids"]:
                if key not in model["checks"]:
                    raise ValueError(f"unknown invalidation target {key}")
                invalidated[key] = max(at, invalidated.get(key, at))
        except (KeyError, ValueError, TypeError) as exc:
            errors.append(f"Invalid invalidation: {exc}")
    result = {}
    for key, history in per_check.items():
        history.sort(key=lambda r: (record_time(r), r["run_id"]))
        last = history[-1] if history else None
        status = last["status"] if last else "NOT_RUN"
        reason = "No submitted evidence" if not last else last["actual"]
        if last and (last["baseline"] != current["sha256"] or last["spec_sha256"] != spec_hash(model["checks"][key]) or
                     (key in invalidated and stamp(last["started_at"]) <= invalidated[key])):
            status, reason = "STALE", "Source/specification changed or evidence explicitly invalidated; rerun required"
        result[key] = {"status": status, "reason": reason, "run_id": last["run_id"] if last else None}

    # Failure history is append-only. A later success alone cannot erase a failure.
    covered_failures, issue_rows, blocked_checks, blocked_tasks = set(), [], set(), set()
    issue_ids = set()
    for issue in state.get("issues", []):
        invalid = []
        iid = issue.get("id")
        if not iid or iid in issue_ids:
            invalid.append("missing/duplicate issue ID")
        issue_ids.add(iid)
        for field in ("owner", "severity", "expected", "actual", "reproduction", "created_at", "due_at"):
            if not issue.get(field):
                invalid.append(f"missing {field}")
        if issue.get("severity") not in ("critical", "major", "minor"):
            invalid.append("unknown severity")
        if issue.get("status") not in ("OPEN", "FIXING", "READY_FOR_RETEST", "CLOSED"):
            invalid.append("unknown issue status")
        check_ids = issue.get("check_ids", [])
        task_ids = issue.get("task_ids", [])
        if not check_ids or any(c not in result for c in check_ids) or any(t not in model["tasks"] for t in task_ids):
            invalid.append("invalid affected check/task IDs")
        regression = issue.get("regression_checks", [])
        if not regression or any(c not in result for c in regression):
            invalid.append("explicit affected regression checks required")
        for rid in issue.get("failed_run_ids", []):
            if rid not in records or records[rid]["status"] != "FAIL" or records[rid]["check_id"] not in check_ids:
                invalid.append(f"invalid linked failure {rid}")
        try:
            created, due = stamp(issue["created_at"]), stamp(issue["due_at"])
            if due < created or created > datetime.now(timezone.utc):
                invalid.append("invalid issue dates")
        except (KeyError, ValueError, TypeError):
            invalid.append("invalid issue dates")
        if issue.get("status") == "CLOSED":
            fix = issue.get("fix", {})
            if not fix.get("description") or not fix.get("by") or not fix.get("at"):
                invalid.append("fix record required")
            if not issue.get("closed_by") or not issue.get("closed_at"):
                invalid.append("closure reviewer/time required")
            try:
                fixed = stamp(fix["at"])
                closed = stamp(issue["closed_at"])
                if fixed < created or closed < fixed or closed > datetime.now(timezone.utc):
                    invalid.append("invalid fix/closure chronology")
                retests = [records[r] for r in issue.get("retest_run_ids", []) if r in records]
                for key in set(check_ids + regression):
                    if not any(r["check_id"] == key and r["status"] == "PASS" and r["baseline"] == current["sha256"]
                               and r["spec_sha256"] == spec_hash(model["checks"][key])
                               and fixed <= stamp(r["started_at"]) <= stamp(r["finished_at"]) <= closed
                               and result[key]["status"] == "PASS" for r in retests):
                        invalid.append(f"missing current post-fix retest: {key}")
            except (KeyError, ValueError, TypeError, UnboundLocalError):
                invalid.append("invalid closure evidence")
        effective = issue.get("status") if not invalid else "INVALID"
        if effective != "CLOSED":
            blocked_checks.update(check_ids)
            blocked_checks.update(regression)
            blocked_tasks.update(task_ids)
        if not invalid:
            covered_failures.update(issue.get("failed_run_ids", []))
        else:
            errors.append(f"Issue {iid}: {'; '.join(invalid)}")
        issue_rows.append({"id": iid, "status": effective, "owner": issue.get("owner"), "due_at": issue.get("due_at"), "errors": invalid})
    for rid, record in records.items():
        if record["status"] == "FAIL" and rid not in covered_failures:
            blocked_checks.add(record["check_id"])
            issue_rows.append({"id": f"UNTRIAGED:{rid}", "status": "OPEN", "owner": model["checks"][record["check_id"]]["owner"],
                               "errors": ["Failed attempt must be linked to an issue; retry cannot erase it"]})
    for key in blocked_checks:
        if key in result:
            result[key]["status"] = "BLOCKED"
            result[key]["reason"] = "Open/unverified defect or untriaged failure"

    def affected_tasks(key):
        definition = model["checks"][key]
        if definition.get("task"):
            return [definition["task"]]
        if key in model["decisions"]:
            return model["decisions"][key]["tasks"]
        if key in model["risks"]:
            return model["risks"][key]["tasks"]
        groups = [definition["test"]] if definition.get("test") else model["successes"].get(key, {}).get("tests", [])
        if groups:
            return [t for t, item in model["tasks"].items() if set(item["tests"]) & set(groups)]
        return ["TASK-029"]

    # Known global regressions reopen their contributing tasks, then the dependency graph.
    # Merely unexecuted global tests do not prevent earlier module development.
    for key, row in result.items():
        if row["status"] in ("FAIL", "BLOCKED", "STALE"):
            blocked_tasks.update(affected_tasks(key))

    # Scope changes are recorded, not counted as test passes or automatic waivers.
    for change in state.get("scope_changes", []):
        for field in ("id", "reason", "approved_by", "approved_at", "version", "expires_at", "confirmation", "affected_checks"):
            if not change.get(field):
                errors.append(f"Scope change lacks {field}")
        if not verify_file(root, change.get("confirmation")):
            errors.append("Scope change confirmation missing/hash mismatch")
        try:
            if stamp(change["expires_at"]) <= datetime.now(timezone.utc) or stamp(change["approved_at"]) > datetime.now(timezone.utc):
                errors.append("Scope change expired/unapproved")
        except (KeyError, ValueError, TypeError):
            errors.append("Scope change timestamps invalid")
        for key in change.get("affected_checks", []):
            if key not in model["checks"]:
                errors.append(f"Scope change references missing check {key}")
            if key.startswith(("TEST-005", "TEST-008")) or key == "S03":
                errors.append("Input safety invariants cannot be waived")

    accepted = lambda key: result[key]["status"] == "PASS" or (result[key]["status"] == "N/A" and model["checks"][key]["allow_na"])
    task_rows = {}

    def task_status(tid, visiting=None):
        if tid in task_rows:
            return task_rows[tid]["acceptance"]
        visiting = set() if visiting is None else visiting
        if tid in visiting or tid not in model["tasks"]:
            return "BLOCKED"
        visiting.add(tid)
        item = model["tasks"][tid]
        prerequisites = {dep: task_status(dep, visiting) for dep in item["dependencies"]}
        visiting.remove(tid)
        gates = [k for k, d in model["decisions"].items() if tid in d["tasks"]]
        implementation = state.get("tasks", {}).get(tid, {}).get("implementation", "NOT_STARTED")
        if errors or tid in blocked_tasks or any(v != "VERIFIED" for v in prerequisites.values()) or any(not accepted(k) for k in gates):
            status = "BLOCKED"
        elif all(accepted(k) for k in item["checks"]) and implementation == "IMPLEMENTED":
            status = "VERIFIED"
        elif implementation == "NOT_STARTED":
            status = "NOT_RUN"
        else:
            status = "PENDING"
        task_rows[tid] = dict(implementation=implementation, acceptance=status,
                              owner=state.get("tasks", {}).get(tid, {}).get("owner"), dependencies=prerequisites,
                              decision_gates=gates, checks=item["checks"])
        return status
    for tid in model["tasks"]:
        task_status(tid)
    if task_id:
        if task_id not in model["tasks"]:
            raise ValueError(f"unknown task {task_id}")
        selected_tasks = set()

        def include(tid):
            if tid in selected_tasks:
                return
            selected_tasks.add(tid)
            for dep in model["tasks"][tid]["dependencies"]:
                include(dep)
        include(task_id)
    elif scope == "baseline":
        selected_tasks = set()
    else:
        phase = "P3" if scope == "release" else scope
        selected_tasks = {t for t, item in model["tasks"].items() if item["stage"] <= phase}
    selected_checks = {c for t in selected_tasks for c in model["tasks"][t]["checks"]}
    selected_checks |= {k for k, d in model["decisions"].items() if set(d["tasks"]) & selected_tasks}
    if not task_id:
        selected_checks |= {k for k, r in model["risks"].items() if set(r["tasks"]) & selected_tasks}
    full_release = not task_id and scope in ("P3", "release")
    if full_release:
        selected_checks = set(model["checks"])
        artifact = state.get("release_artifact")
        if not verify_file(root, artifact):
            errors.append("No verified release artifact; install/release acceptance cannot pass")
        else:
            for key in selected_checks:
                if model["checks"][key]["kind"] in ("acceptance", "measurement", "success", "release") and accepted(key):
                    record = records[result[key]["run_id"]]
                    if record.get("artifact") != artifact:
                        result[key] = {"status": "STALE", "reason": "Evidence does not reference current release artifact", "run_id": record["run_id"]}
    requirement_rows = {}
    for uid, item in model["requirements"].items():
        test_checks = [c for t in item["tests"] for c in model["tests"][t]["checks"]]
        requirement_rows[uid] = {"tasks": item["tasks"], "tests": item["tests"],
                                 "status": "PASS" if all(task_rows[t]["acceptance"] == "VERIFIED" for t in item["tasks"])
                                 and all(accepted(c) for c in test_checks) else "NOT_VERIFIED"}
    ready = not errors and all(task_rows[t]["acceptance"] == "VERIFIED" for t in selected_tasks) and all(accepted(k) for k in selected_checks)
    if full_release and any(i["status"] != "CLOSED" for i in issue_rows):
        ready = False
    return dict(schema_version=1, generated_at=now(), scope=task_id or scope,
                gate="PASS" if ready else "NOT_READY", baseline=current["sha256"],
                source_files=current["files"], documentation=docs, errors=errors,
                selected_tasks=sorted(selected_tasks), selected_checks=sorted(selected_checks),
                counts=dict(Counter(result[k]["status"] for k in selected_checks)),
                implementation_counts=dict(Counter(task_rows[t]["implementation"] for t in selected_tasks)),
                verified_tasks=sum(task_rows[t]["acceptance"] == "VERIFIED" for t in selected_tasks),
                task_denominator=len(selected_tasks), tasks=task_rows, checks=result,
                requirements=requirement_rows, issues=issue_rows,
                release_artifact=state.get("release_artifact"),
                limits=["Baseline PASS validates documentation/registry integrity only; it does not accept product functionality.",
                        "Task-level partial tests cannot satisfy full TEST groups.",
                        "Manual evidence is validated structurally; its substance requires the named reviewer."])


def write_audit(root, result):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    folder = root / "reports/audits" / run_id
    save(folder / "result.json", result, exclusive=True)
    save(folder / "catalog.json", build(root), exclusive=True)
    lines = [f"# 检查结果：{result['scope']}", "", f"结论：**{result['gate']}**", "",
             f"基线：`{result['baseline']}`", "",
             f"任务验收：{result['verified_tasks']}/{result['task_denominator']}；逐项状态：`{json.dumps(result['counts'])}`。",
             "", "baseline 的 PASS 仅表示文档/账本结构有效，不表示业务功能通过。", "",
             "## 阻塞与输入错误", ""]
    lines.extend(f"- {e}" for e in result["errors"])
    if not result["errors"]:
        lines.append("无输入错误；业务未执行项仍按 NOT_RUN/PENDING/BLOCKED 保留。")
    lines += ["", "## 任务状态", "", "|任务|实施|验收|", "|---|---|---|"]
    for tid in result["selected_tasks"]:
        row = result["tasks"][tid]
        lines.append(f"|{tid}|{row['implementation']}|{row['acceptance']}|")
    lines += ["", "## 逐项结果", "", "|检查项|结果|运行记录|", "|---|---|---|"]
    for key in result["selected_checks"]:
        row = result["checks"][key]
        lines.append(f"|{key}|{row['status']}|{row['run_id'] or '无'}|")
    lines += ["", "完整预期、任务/需求映射见同目录 catalog.json；各结果原因、缺陷、文件哈希及范围见 result.json。", ""]
    (folder / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    save(root / "reports/latest-audit.json", {"run_id": run_id, "scope": result["scope"], "path": folder.relative_to(root).as_posix(), "gate": result["gate"]})
    return folder


def template(root, key, artifact=None):
    model = build(root)
    if key not in model["checks"]:
        raise ValueError(f"unknown check {key}")
    current = baseline(root)
    return dict(schema_version=1, run_id=uuid.uuid4().hex, check_id=key,
                baseline=current["sha256"], spec_sha256=spec_hash(model["checks"][key]),
                expected=model["checks"][key]["expected"], status="NOT_RUN", mode="manual",
                started_at=now(), finished_at=now(), environment={"os": platform.platform(), "runtime_versions": {"python": sys.version},
                    "hardware": "", "models": "", "datasets": "", "coverage": ""},
                steps=[], actual="尚未执行", evidence=[], artifact=artifact,
                review={"by": "", "at": "", "decision": "PENDING", "note": ""})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="action", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--scope", choices=["baseline", "P0", "P1", "P2", "P3", "release"], default="baseline")
    audit.add_argument("--task")
    sub.add_parser("catalog")
    draft = sub.add_parser("template")
    draft.add_argument("--check", required=True)
    draft.add_argument("--artifact")
    submit = sub.add_parser("submit")
    submit.add_argument("file", type=Path)
    capture = sub.add_parser("capture")
    capture.add_argument("--check", required=True)
    capture.add_argument("--timeout", type=int, default=600)
    capture.add_argument("--artifact")
    capture.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.action == "audit":
            report = evaluate(root, args.scope, args.task)
            folder = write_audit(root, report)
            print(json.dumps({"gate": report["gate"], "scope": report["scope"], "counts": report["counts"], "errors": report["errors"], "report": str(folder / "summary.md")}, ensure_ascii=False, indent=2))
            return 0 if report["gate"] == "PASS" else 1
        if args.action == "catalog":
            path = root / "reports/check-catalog.json"
            save(path, build(root))
            print(path)
            return 0
        if args.action == "submit":
            record = load(args.file)
            model = build(root)
            errors = record_errors(root, record, model)
            if record.get("baseline") != baseline(root)["sha256"] or record.get("spec_sha256") != spec_hash(model["checks"].get(record.get("check_id"), {})):
                errors.append("Cannot submit stale evidence; rerun required")
            if errors:
                raise ValueError("; ".join(errors))
            path = root / "reports/evidence" / (record["run_id"] + ".json")
            save(path, record, exclusive=True)
            print(path)
            return 0
        artifact = file_ref(root, args.artifact) if args.artifact else None
        record = template(root, args.check, artifact)
        if args.action == "capture":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command or args.timeout <= 0:
                raise ValueError("capture requires a command and positive timeout")
            raw = root / "reports/raw" / (record["run_id"] + ".log")
            raw.parent.mkdir(parents=True, exist_ok=True)
            with raw.open("xb") as stream:
                try:
                    completed = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                                               timeout=args.timeout, shell=False)
                    code = completed.returncode
                except subprocess.TimeoutExpired:
                    stream.write(b"\nAUDIT: command timed out; attempt failed.\n")
                    code = 124
                except OSError as exc:
                    stream.write(str(exc).encode("utf-8"))
                    code = 127
            record.update(mode="command", command={"argv": command, "exit_code": code},
                          finished_at=now(), evidence=[file_ref(root, raw.relative_to(root).as_posix())],
                          status="FAIL" if code else "BLOCKED", actual="命令失败" if code else "命令退出 0，待核对断言及人工签核")
            if record["baseline"] != baseline(root)["sha256"]:
                record.update(status="FAIL", actual="执行期间源码/规格发生变化，结果不可验收")
            if record["status"] == "FAIL":
                save(root / "reports/evidence" / (record["run_id"] + ".json"), record, exclusive=True)
            else:
                save(root / "reports/evidence" / (record["run_id"] + ".json"), record, exclusive=True)
                # The reviewer submits a new attempt linked to the captured run, preserving history.
                record["captured_run_id"] = record["run_id"]
                record["run_id"] = uuid.uuid4().hex
        path = root / "reports/drafts" / (record["run_id"] + ".json")
        save(path, record, exclusive=True)
        print(path)
        return 1 if record["status"] == "FAIL" else 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"Audit error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
