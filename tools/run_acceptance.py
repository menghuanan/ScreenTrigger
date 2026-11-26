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
from acceptance_version import (
    AUTOMATION, FIRST, VERSIONS, candidate_ref, catalog, ledger_path, raw_ref, report_path,
)

STATE = "docs/agent/check-state.json"
SKIP = {"reports", ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "target", "dist", ".workbuddy"}
RESULTS = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "N/A"}
ARCHIVED_DESIGNS = {
    "00-项目概览.md", "01-模块拆分.md", "05-功能流程图.md", "07-实现细节清单.md",
    "开发指南.md", "winui3-migration.md", "agent/阶段任务.md", "agent/deferred-testing.md",
}


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


def historical_manifest(root, baseline_id):
    """Bind a saved source manifest to the baseline hash in the original receipt."""
    if not isinstance(baseline_id, str) or len(baseline_id) != 64 or any(c not in "0123456789abcdef" for c in baseline_id):
        return {}
    path = local(root, f"reports/raw/history-baselines/{baseline_id}.json")
    if not path.is_file():
        return {}
    files = load(path)
    if not isinstance(files, dict) or hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != baseline_id:
        raise ValueError(f"Invalid historical source manifest: {baseline_id}")
    return files


def verify_run_evidence(root, ref, source_files, archived_files=None):
    if verify_file(root, ref):
        return True
    if not source_files or not isinstance(ref, dict):
        return False
    name, sha = ref.get("path"), ref.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        return False
    # Both the historical manifest and receipt must independently identify the same file.
    if (archived_files and isinstance(name, str) and archived_files.get(name) == sha
            and name.startswith(("src/", "src-tauri/", "backend/", "desktop/", "native/", "models/",
                                 "manifest/", "tools/", "schema/", "tests/", "docs/"))
            and not name.startswith("docs/archive/") and name != STATE):
        if verify_file(root, {"path": f"reports/raw/history-source-sha256/{sha}", "sha256": sha}):
            return True
    if (not isinstance(name, str) or not name.startswith("docs/")
            or name.startswith("docs/archive/") or not name.endswith(".md")):
        return False
    if name not in source_files:
        original = name.removeprefix("docs/")
        if original not in ARCHIVED_DESIGNS or f"docs/archive/initial-development/{original}" not in source_files:
            return False
    # Only a previous run's mutable specification can use an exact-byte snapshot.
    snapshot = f"reports/raw/doc-sha256/{sha}.md"
    return verify_file(root, {"path": snapshot, "sha256": sha})


def record_errors(root, record, model, historical_sources=None, archived_files=None, verify_artifact=verify_file):
    errors = []
    if not isinstance(record, dict):
        return ["evidence record must be an object"]
    version = model.get("version", FIRST)
    if record.get("version", FIRST) != version:
        errors.append("receipt belongs to a different version")
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
    if version == AUTOMATION and isinstance(evidence, list):
        if not all(raw_ref(root, version, ref) for ref in evidence):
            errors.append("evidence must be in this version's raw directory")
        if record.get("artifact") is not None and not candidate_ref(root, version, record["artifact"]):
            errors.append("artifact outside this version's candidate namespace or RC16 digest")
    if record.get("status") in ("PASS", "FAIL", "N/A") and not evidence:
        errors.append("raw evidence required")
    if not isinstance(evidence, list) or not all(verify_run_evidence(root, ref, historical_sources, archived_files) for ref in evidence):
        errors.append("evidence missing or hash mismatch")
    if record.get("artifact") is not None and not verify_artifact(root, record["artifact"]):
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
        if model.get("version") == AUTOMATION:
            if "acceptance" in entry:
                errors.append(f"{tid}: acceptance is computed by the checker")
            if entry.get("implementation") != "NOT_STARTED" and tid in model["tasks"]:
                item = model["tasks"][tid]
                if (entry.get("owner") == "待领取" or entry.get("dependencies") != item["dependencies"]
                        or entry.get("tests") != item["tests"]):
                    errors.append(f"{tid}: opening record requires owner, catalogue dependencies and tests")
                for field in ("allowed_modules", "expected"):
                    values = entry.get(field)
                    if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
                        errors.append(f"{tid}: opening record lacks {field}")
                try:
                    if stamp(entry["opened_at"]) > datetime.now(timezone.utc):
                        raise ValueError("future opening")
                except (KeyError, ValueError, TypeError, AttributeError):
                    errors.append(f"{tid}: invalid opened_at")
    for field in ("issues", "scope_changes", "invalidations"):
        if not isinstance(state.get(field), list):
            errors.append(f"{field} must be a list")
    return errors


def receipt_retirements(root, state, model):
    entries = {}
    for change in state["scope_changes"]:
        ref = change.get("retired_inventory")
        if ref is None:
            continue
        if not verify_file(root, ref) or not verify_file(root, change.get("confirmation")):
            raise ValueError("Historical retirement inventory or authorization has changed")
        for item in load(local(root, ref["path"])):
            rid = item["run_id"]
            checks = [item["check_id"], *item["regression_checks"]]
            if (rid in entries or not item["regression_checks"] or not item["missing_sources"]
                    or any(k not in model["checks"] or k not in change["affected_checks"] for k in checks)):
                raise ValueError(f"Invalid historical retirement entry: {rid}")
            entries[rid] = dict(item, approved_at=change["approved_at"])
    return entries


def matches_retired_receipt(root, path, record, item, sources, archived):
    if (record["status"] not in ("PASS", "BLOCKED") or record["check_id"] != item["check_id"]
            or path.name != record["run_id"] + ".json" or digest(path) != item["receipt_sha256"]):
        return False
    allowed = {(ref["path"], ref["sha256"]) for ref in item["missing_sources"]}
    refs = record.get("evidence", [])
    # Raw output, archives, user authorizations and package files must still verify.
    if not allowed <= {(r["path"], r["sha256"]) for r in refs} or any(
            not name.startswith(("backend/", "docs/", "schema/", "src/", "src-tauri/")) or
            name.startswith("docs/archive/") or name == STATE for name, _ in allowed):
        return False
    return all((r["path"], r["sha256"]) in allowed or verify_run_evidence(root, r, sources, archived)
               for r in refs)


def evaluate(root, scope="baseline", task_id=None, version=FIRST, test_id=None):
    if version == AUTOMATION and scope not in ("baseline", "release"):
        raise ValueError(f"{scope} is a first-version phase")
    if version == AUTOMATION and task_id and scope != "baseline":
        raise ValueError("select an automation task or a release scope")
    if test_id and (version == FIRST or task_id or scope != "baseline"):
        raise ValueError("select one automation test group with --test")
    model, state, current = catalog(root, version), load(ledger_path(root, version)), baseline(root)
    errors = validate_state(state, model)
    if state.get("version", FIRST) != version:
        errors.append("ledger belongs to a different version")
    artifact = state.get("release_artifact")
    if version == AUTOMATION and artifact is not None and not candidate_ref(root, version, artifact):
        errors.append("ledger candidate must belong to the automation version and have its own digest")
    docs = validate_docs(root, version)
    errors += docs["errors"]
    records = {}
    archived_manifests = {}
    artifact_results = {}

    def verify_shared_artifact(folder, ref):
        if not isinstance(ref, dict) or not all(isinstance(ref.get(k), str) for k in ("path", "sha256")):
            return False
        key = (ref["path"], ref["sha256"])
        if key not in artifact_results:
            artifact_results[key] = verify_file(folder, ref)
        return artifact_results[key]

    if version == AUTOMATION and artifact is not None and not verify_shared_artifact(root, artifact):
        errors.append("ledger candidate missing or hash mismatch")
    retired = receipt_retirements(root, state, model) if version == FIRST else {}
    seen_retired = set()
    per_check = {key: [] for key in model["checks"]}
    for path in sorted(report_path(root, version, "evidence").glob("*.json")):
        try:
            report_path(root, version, Path("evidence") / path.name)
            record = load(path)
            previous = isinstance(record, dict) and record.get("baseline") != current["sha256"]
            old_baseline = record.get("baseline") if previous else None
            if version == FIRST and old_baseline and old_baseline not in archived_manifests:
                archived_manifests[old_baseline] = historical_manifest(root, old_baseline)
            invalid = record_errors(root, record, model, current["files"] if previous else None,
                                    archived_manifests.get(old_baseline), verify_shared_artifact)
            rid = record.get("run_id")
            if version == AUTOMATION and path.stem != rid:
                invalid.append("receipt filename must match run_id")
            if rid in retired and previous and invalid in ([], ["evidence missing or hash mismatch"]):
                if matches_retired_receipt(root, path, record, retired[rid], current["files"], archived_manifests.get(old_baseline)):
                    seen_retired.add(rid)
                    continue
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
        if (version == AUTOMATION and status in ("PASS", "N/A")
                and (model["checks"][key].get("requires_artifact") or last.get("artifact") is not None)
                and (artifact is None or last.get("artifact") != artifact)):
            status, reason = "STALE", "Evidence must reference the current version's candidate"
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
    retirement_rows = []
    for rid, item in retired.items():
        if rid not in seen_retired:
            errors.append(f"Historical retirement receipt missing, changed or ineligible: {rid}")
        pending = []
        for key in [item["check_id"], *item["regression_checks"]]:
            row = result[key]
            new = records.get(row["run_id"], {})
            if (row["status"] != "PASS" or not state.get("release_artifact")
                    or new.get("artifact") != state["release_artifact"]
                    or stamp(new["started_at"]) < stamp(item["approved_at"])
                    or (key == item["check_id"] and rid not in new.get("retested_original_runs", []))):
                pending.append(key)
        if pending or rid not in seen_retired:
            blocked_checks.add(item["check_id"])
        retirement_rows.append(dict(run_id=rid, status="VOID", check_id=item["check_id"],
                                    original_preserved=rid in seen_retired, pending_retests=pending))
    for key in blocked_checks:
        if key in result:
            result[key]["status"] = "BLOCKED"
            result[key]["reason"] = "Open/unverified defect or untriaged failure"

    def affected_tasks(key):
        definition = model["checks"][key]
        if definition.get("affected_tasks"):
            return definition["affected_tasks"]
        if definition.get("task"):
            return [definition["task"]]
        if key in model["decisions"]:
            return model["decisions"][key]["tasks"]
        if key in model["risks"]:
            return model["risks"][key]["tasks"]
        groups = [definition["test"]] if definition.get("test") else model["successes"].get(key, {}).get("tests", [])
        if groups:
            return [t for t, item in model["tasks"].items() if set(item["tests"]) & set(groups)]
        return ["TASK-044" if version == AUTOMATION else "TASK-029"]

    # Known global regressions reopen their contributing tasks, then the dependency graph.
    # Merely unexecuted global tests do not prevent earlier module development.
    for key, row in result.items():
        if row["status"] in ("FAIL", "BLOCKED", "STALE"):
            blocked_tasks.update(affected_tasks(key))

    # Scope changes are recorded, not counted as test passes or automatic waivers.
    for change in state.get("scope_changes", []):
        if version == AUTOMATION and (change.get("version") != version or change.get("retired_inventory")
                                      or not raw_ref(root, version, change.get("confirmation"))):
            errors.append("Scope decision must be authorized for the selected version")
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
    final_checks = [k for k, d in model["checks"].items() if d["kind"] in ("acceptance", "release")]

    def final_candidate_verified():
        return artifact is not None and all(
            accepted(k) and records[result[k]["run_id"]].get("artifact") == artifact for k in final_checks
        )

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
        elif (all(accepted(k) for k in item["checks"]) and implementation == "IMPLEMENTED"
              and (tid != "TASK-044" or final_candidate_verified())):
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
    elif test_id or scope == "baseline":
        selected_tasks = set()
    else:
        phase = "P3" if scope == "release" else scope
        selected_tasks = (set(model["tasks"]) if version == AUTOMATION else
                          {t for t, item in model["tasks"].items() if item["stage"] <= phase})
    selected_checks = {c for t in selected_tasks for c in model["tasks"][t]["checks"]}
    selected_checks |= {k for k, d in model["decisions"].items() if set(d["tasks"]) & selected_tasks}
    if not task_id:
        selected_checks |= {k for k, r in model["risks"].items() if set(r["tasks"]) & selected_tasks}
    if test_id:
        if test_id not in model["tests"]:
            raise ValueError(f"unknown test {test_id}")
        selected_checks = set(model["tests"][test_id]["checks"])
    if task_id == "TASK-044":
        selected_checks.update(final_checks)
    full_release = not task_id and not test_id and scope in ("P3", "release")
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
    # A release ZIP may be referenced by hundreds of receipts. Recheck the bytes
    # before issuing the audit so that changes during a shared verification fail it.
    for (name, sha), valid in artifact_results.items():
        if valid and not verify_file(root, {"path": name, "sha256": sha}):
            errors.append(f"Artifact changed during audit: {name}")
    ready = not errors and all(task_rows[t]["acceptance"] == "VERIFIED" for t in selected_tasks) and all(accepted(k) for k in selected_checks)
    if full_release and any(i["status"] != "CLOSED" for i in issue_rows):
        ready = False
    return dict(schema_version=1, **({"version": version} if version != FIRST else {}),
                generated_at=now(), scope=task_id or test_id or scope,
                gate="PASS" if ready else "NOT_READY", baseline=current["sha256"],
                source_files=current["files"], documentation=docs, errors=errors,
                selected_tasks=sorted(selected_tasks), selected_checks=sorted(selected_checks),
                counts=dict(Counter(result[k]["status"] for k in selected_checks)),
                implementation_counts=dict(Counter(task_rows[t]["implementation"] for t in selected_tasks)),
                verified_tasks=sum(task_rows[t]["acceptance"] == "VERIFIED" for t in selected_tasks),
                task_denominator=len(selected_tasks), tasks=task_rows, checks=result,
                requirements=requirement_rows, issues=issue_rows, retired_receipts=retirement_rows,
                release_artifact=state.get("release_artifact"),
                limits=["Baseline PASS validates documentation/registry integrity only; it does not accept product functionality.",
                        "Task-level partial tests cannot satisfy full TEST groups.",
                        "Manual evidence is validated structurally; its substance requires the named reviewer."])


def write_audit(root, result):
    version = result.get("version", FIRST)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    folder = report_path(root, version, Path("audits") / run_id)
    save(folder / "result.json", result, exclusive=True)
    save(folder / "catalog.json", catalog(root, version), exclusive=True)
    lines = [f"# 检查结果：{result['scope']}", "", f"结论：**{result['gate']}**", "",
             f"基线：`{result['baseline']}`", "",
             f"任务验收：{result['verified_tasks']}/{result['task_denominator']}；逐项状态：`{json.dumps(result['counts'])}`。",
             "", "baseline 的 PASS 仅表示文档/账本结构有效，不表示业务功能通过。", "",
             "## 阻塞与输入错误", ""]
    lines.extend(f"- {e}" for e in result["errors"])
    if version != FIRST:
        lines[2:2] = [f"版本：`{version}`", ""]
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
    save(report_path(root, version, "latest-audit.json"), {"run_id": run_id, "scope": result["scope"], "path": folder.relative_to(root).as_posix(), "gate": result["gate"]})
    return folder


def template(root, key, artifact=None, version=FIRST):
    model = catalog(root, version)
    if key not in model["checks"]:
        raise ValueError(f"unknown check {key}")
    current = baseline(root)
    if version == AUTOMATION and artifact is not None and not candidate_ref(root, version, artifact):
        raise ValueError("artifact does not belong to this version")
    return dict(schema_version=1, **({"version": version} if version != FIRST else {}),
                run_id=uuid.uuid4().hex, check_id=key,
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
    selector = audit.add_mutually_exclusive_group()
    audit.add_argument("--scope", choices=["baseline", "P0", "P1", "P2", "P3", "release"], default="baseline")
    selector.add_argument("--task")
    selector.add_argument("--test")
    catalog_parser = sub.add_parser("catalog")
    init = sub.add_parser("init", help="create an empty automation-version ledger")
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
    for command_parser in (audit, catalog_parser, draft, submit, capture, init):
        command_parser.add_argument("--version", choices=VERSIONS, default=FIRST)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    version = args.version
    try:
        if args.action == "init":
            if version != AUTOMATION:
                raise ValueError("init requires an explicit automation version")
            model = catalog(root, version)
            path = ledger_path(root, version)
            save(path, dict(schema_version=1, version=version, tasks={
                tid: {"implementation": "NOT_STARTED", "owner": "待领取"} for tid in model["tasks"]
            }, issues=[], scope_changes=[], invalidations=[], release_artifact=None), exclusive=True)
            print(path)
            return 0
        if args.action == "audit":
            report = evaluate(root, args.scope, args.task, version, args.test)
            folder = write_audit(root, report)
            print(json.dumps({"gate": report["gate"], "scope": report["scope"], "counts": report["counts"], "errors": report["errors"], "report": str(folder / "summary.md")}, ensure_ascii=False, indent=2))
            return 0 if report["gate"] == "PASS" else 1
        if args.action == "catalog":
            path = report_path(root, version, "check-catalog.json")
            save(path, catalog(root, version))
            print(path)
            return 0
        if args.action == "submit":
            record = load(args.file)
            model = catalog(root, version)
            errors = record_errors(root, record, model)
            if version == AUTOMATION and record.get("status") in ("PASS", "N/A"):
                check = model["checks"].get(record.get("check_id"), {})
                if check.get("requires_artifact") or record.get("artifact") is not None:
                    candidate = load(ledger_path(root, version)).get("release_artifact")
                    if candidate is None or record.get("artifact") != candidate:
                        errors.append("receipt must reference the current version's candidate")
            if record.get("baseline") != baseline(root)["sha256"] or record.get("spec_sha256") != spec_hash(model["checks"].get(record.get("check_id"), {})):
                errors.append("Cannot submit stale evidence; rerun required")
            if errors:
                raise ValueError("; ".join(errors))
            path = report_path(root, version, Path("evidence") / (record["run_id"] + ".json"))
            save(path, record, exclusive=True)
            print(path)
            return 0
        artifact = file_ref(root, args.artifact) if args.artifact else None
        record = template(root, args.check, artifact, version)
        if args.action == "capture":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command or args.timeout <= 0:
                raise ValueError("capture requires a command and positive timeout")
            raw = report_path(root, version, Path("raw") / (record["run_id"] + ".log"))
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
            save(report_path(root, version, Path("evidence") / (record["run_id"] + ".json")), record, exclusive=True)
            if record["status"] != "FAIL":
                # The reviewer submits a new attempt linked to the captured run, preserving history.
                record["captured_run_id"] = record["run_id"]
                record["run_id"] = uuid.uuid4().hex
        path = report_path(root, version, Path("drafts") / (record["run_id"] + ".json"))
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
