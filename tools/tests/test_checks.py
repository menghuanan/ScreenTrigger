"""Check the gates with synthetic evidence in disposable repositories only."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_docs import ROOT, validate
from check_model import build
from run_acceptance import baseline, evaluate, file_ref, main, save, spec_hash


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="screentrigger-audit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("README.md", "AGENTS.md"):
            shutil.copy2(ROOT / name, self.root / name)
        for name in ("docs", "tools"):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__", "check-state.json"))
        self.model = build(self.root)
        self.state = {
            "schema_version": 1,
            "tasks": {
                task_id: {"implementation": "NOT_STARTED", "owner": "Synthetic test owner"}
                for task_id in self.model["tasks"]
            },
            "issues": [],
            "invalidations": [],
            "scope_changes": [],
            "release_artifact": None,
        }
        self.state_save()
        self.clock = datetime.now(timezone.utc) - timedelta(hours=1)
        self.serial = 0
        raw = self.root / "reports/raw/synthetic.log"
        raw.parent.mkdir(parents=True)
        raw.write_text("SYNTHETIC TOOL TEST ONLY. No product was run.\n", encoding="utf-8")
        self.raw = file_ref(self.root, "reports/raw/synthetic.log")
        self.base = baseline(self.root)["sha256"]

    def state_save(self):
        save(self.root / "docs/agent/check-state.json", self.state)

    def record(self, key, status="PASS", artifact=None):
        self.serial += 1
        t = self.clock + timedelta(seconds=self.serial * 3)
        record = dict(schema_version=1, run_id=f"synthetic-{self.serial:04}", check_id=key,
                      baseline=self.base, spec_sha256=spec_hash(self.model["checks"][key]),
                      expected=self.model["checks"][key]["expected"], status=status, mode="manual",
                      started_at=t.isoformat(), finished_at=(t + timedelta(seconds=1)).isoformat(),
                      environment={k: "Synthetic unit-test fixture, not a product environment"
                                   for k in ("os", "hardware", "runtime_versions", "models", "datasets", "coverage")},
                      steps=["Exercise evidence validation with synthetic data"], actual="Synthetic fixture assertion",
                      evidence=[self.raw], artifact=artifact,
                      review={"by": "synthetic test reviewer", "at": (t + timedelta(seconds=2)).isoformat(),
                              "decision": "ACCEPT", "note": "Only validates the audit tool"})
        if status == "N/A":
            record["reason"] = "Synthetic no-UI case"
        save(self.root / "reports/evidence" / (record["run_id"] + ".json"), record)
        return record

    def complete_task(self, tid):
        self.state["tasks"][tid]["implementation"] = "IMPLEMENTED"
        self.state_save()
        for key in self.model["tasks"][tid]["checks"]:
            self.record(key)

    def green_release(self):
        artifact = self.root / "reports/fixtures/package.exe"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"SYNTHETIC - NOT AN EXECUTABLE")
        ref = file_ref(self.root, "reports/fixtures/package.exe")
        self.state["release_artifact"] = ref
        for task in self.state["tasks"].values():
            task["implementation"] = "IMPLEMENTED"
        self.state_save()
        for key in self.model["checks"]:
            self.record(key, artifact=ref)
        return ref

    def issue(self, failed, status="OPEN", retest=None):
        key = failed["check_id"]
        issue = dict(id="ISSUE-SYNTHETIC", check_ids=[key], task_ids=["TASK-001"], owner="Test maintainer",
                     severity="major", expected="Synthetic expected", actual="Synthetic failure",
                     reproduction="Synthetic reproduction", created_at=failed["finished_at"],
                     due_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                     status=status, failed_run_ids=[failed["run_id"]], regression_checks=[key])
        if status == "CLOSED":
            issue.update(fix={"description": "Synthetic correction", "by": "test", "at": failed["finished_at"]},
                         retest_run_ids=[retest["run_id"]] if retest else [], closed_by="test reviewer",
                         closed_at=datetime.now(timezone.utc).isoformat())
        self.state["issues"] = [issue]
        self.state_save()
        return issue

    def test_current_documents_catalogue_and_archive(self):
        result = validate(self.root)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(self.model["tasks"]), 29)
        self.assertEqual(len(self.model["tests"]), 16)
        self.assertGreater(len(self.model["checks"]), 400)
        self.assertEqual(self.model["tasks"]["TASK-002"]["dependencies"], [])
        self.assertIn("TASK-001", self.model["tasks"]["TASK-012"]["dependencies"])
        self.assertTrue(all(t["tests"] for t in self.model["tasks"].values()))

    def test_desktop_decision_keeps_its_gate_when_rows_move(self):
        path = self.root / "docs/decisions/技术决策.md"
        lines = path.read_text(encoding="utf-8").splitlines()
        color = next(i for i, line in enumerate(lines) if line.startswith("| DEC-001 "))
        desktop = next(i for i, line in enumerate(lines) if line.startswith("| DEC-003 "))
        desktop_row = lines[desktop].split("|")
        desktop_row[1] = " DEC-003 桌面界面选型 "
        lines[color], lines[desktop] = "|".join(desktop_row), lines[color]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        model = build(self.root)
        self.assertEqual(model["decisions"], self.model["decisions"])
        self.assertEqual(model["decisions"]["DEC-003"]["tasks"], ["TASK-003", "TASK-004"])
        self.assertIn("桌面界面选型", model["checks"]["DEC-003"]["expected"])
        self.assertIn("WinUI 3", model["checks"]["DEC-003"]["expected"])

    def test_empty_product_passes_only_baseline(self):
        self.assertEqual(evaluate(self.root)["gate"], "PASS")
        for scope in ("P0", "P1", "P2", "P3", "release"):
            report = evaluate(self.root, scope)
            self.assertEqual(report["gate"], "NOT_READY")
            self.assertEqual(report["verified_tasks"], 0)
            self.assertGreater(report["counts"].get("NOT_RUN", 0), 0)

    def test_previous_document_snapshot_cannot_accept_a_current_run_or_replace_raw_output(self):
        key = "TASK-003.CONTRACT"
        record = self.record(key)
        doc_name = "docs/decisions/技术决策.md"
        document = self.root / doc_name
        reference = file_ref(self.root, doc_name)
        record["evidence"].append(reference)
        receipt = self.root / "reports/evidence" / (record["run_id"] + ".json")
        save(receipt, record)

        snapshots = self.root / "reports/raw/doc-sha256"
        snapshot = snapshots / (reference["sha256"] + ".md")
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(document.read_bytes())
        with document.open("ab") as stream:
            stream.write(b"\n")
        report = evaluate(self.root)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["checks"][key]["status"], "STALE")

        record["baseline"] = report["baseline"]
        save(receipt, record)
        self.assertIn("evidence missing or hash mismatch", " ".join(evaluate(self.root)["errors"]))
        record["baseline"] = self.base
        save(receipt, record)
        original = snapshot.read_bytes()
        snapshot.write_bytes(b"altered snapshot")
        self.assertIn("evidence missing or hash mismatch", " ".join(evaluate(self.root)["errors"]))
        snapshot.write_bytes(original)

        raw = self.root / self.raw["path"]
        raw_copy = snapshots / (self.raw["sha256"] + ".md")
        raw_copy.write_bytes(raw.read_bytes())
        raw.unlink()
        self.assertIn("evidence missing or hash mismatch", " ".join(evaluate(self.root)["errors"]))

    def test_reported_implementation_cannot_unlock(self):
        self.state["tasks"]["TASK-001"]["implementation"] = "IMPLEMENTED"
        self.state_save()
        report = evaluate(self.root, task_id="TASK-001")
        self.assertEqual(report["tasks"]["TASK-001"]["acceptance"], "PENDING")
        self.assertEqual(report["gate"], "NOT_READY")

    def test_valid_task_evidence_passes_but_not_global_tests(self):
        self.complete_task("TASK-001")
        report = evaluate(self.root, task_id="TASK-001")
        self.assertEqual(report["gate"], "PASS", report["errors"])
        self.assertEqual(report["checks"]["TEST-002.RESULT.01"]["status"], "NOT_RUN")

    def test_upstream_missing_blocks_completed_downstream(self):
        self.complete_task("TASK-003")
        self.record("DEC-003")
        report = evaluate(self.root, task_id="TASK-003")
        self.assertEqual(report["tasks"]["TASK-003"]["acceptance"], "BLOCKED")
        self.assertEqual(set(report["selected_tasks"]), {"TASK-001", "TASK-002", "TASK-003"})

    def test_source_change_invalidates_old_pass(self):
        self.complete_task("TASK-001")
        (self.root / "source.py").write_text("changed = True\n")
        report = evaluate(self.root, task_id="TASK-001")
        self.assertEqual(report["gate"], "NOT_READY")
        self.assertTrue(all(report["checks"][c]["status"] == "STALE" for c in self.model["tasks"]["TASK-001"]["checks"]))

    def test_explicit_environment_invalidation(self):
        self.complete_task("TASK-001")
        self.state["invalidations"] = [{"at": datetime.now(timezone.utc).isoformat(), "by": "tester",
                                       "reason": "test machine changed", "check_ids": ["TASK-001.BUILD"]}]
        self.state_save()
        report = evaluate(self.root, task_id="TASK-001")
        self.assertEqual(report["checks"]["TASK-001.BUILD"]["status"], "STALE")

    def test_missing_and_tampered_evidence_rejected(self):
        self.record("TASK-001.BUILD")
        (self.root / self.raw["path"]).write_text("tampered")
        self.assertIn("hash mismatch", " ".join(evaluate(self.root)["errors"]))
        (self.root / self.raw["path"]).unlink()
        self.assertNotEqual(evaluate(self.root)["gate"], "PASS")

    def test_retry_cannot_erase_failed_attempt(self):
        self.record("TASK-001.BUILD", "FAIL")
        self.complete_task("TASK-001")
        report = evaluate(self.root, task_id="TASK-001")
        self.assertEqual(report["checks"]["TASK-001.BUILD"]["status"], "BLOCKED")
        self.assertTrue(any(i["id"].startswith("UNTRIAGED") for i in report["issues"]))

    def test_closure_requires_current_post_fix_retest(self):
        failure = self.record("TASK-001.BUILD", "FAIL")
        self.issue(failure, "CLOSED")
        report = evaluate(self.root, task_id="TASK-001")
        self.assertEqual(report["issues"][0]["status"], "INVALID")
        retest = self.record("TASK-001.BUILD")
        self.issue(failure, "CLOSED", retest)
        report = evaluate(self.root, task_id="TASK-001")
        self.assertEqual(report["issues"][0]["status"], "CLOSED", report["errors"])
        self.assertEqual(report["checks"]["TASK-001.BUILD"]["status"], "PASS")
        (self.root / "source.py").write_text("new = True\n")
        self.assertEqual(evaluate(self.root)["issues"][0]["status"], "INVALID")

    def test_open_issue_blocks_all_declared_affected_tasks(self):
        self.complete_task("TASK-001")
        failure = self.record("TASK-001.BUILD", "FAIL")
        self.issue(failure)
        self.assertEqual(evaluate(self.root)["tasks"]["TASK-001"]["acceptance"], "BLOCKED")

    def test_na_cannot_bypass_safety(self):
        self.record("TEST-008.RESULT.01", "N/A")
        self.assertIn("N/A only allowed", " ".join(evaluate(self.root)["errors"]))

    def test_reviewed_na_is_counted_separately(self):
        self.complete_task("TASK-001")
        self.record("TASK-001.UI", "N/A")
        report = evaluate(self.root, task_id="TASK-001")
        self.assertEqual(report["gate"], "PASS")
        self.assertEqual(report["counts"]["N/A"], 1)

    def test_unreviewed_success_does_not_pass(self):
        record = self.record("TASK-001.BUILD")
        record["review"] = {}
        save(self.root / "reports/evidence" / (record["run_id"] + ".json"), record)
        self.assertIn("reviewer", " ".join(evaluate(self.root)["errors"]))

    def test_nonzero_exit_cannot_be_relabelled_pass(self):
        record = self.record("TASK-001.BUILD")
        record.update(mode="command", command={"argv": ["synthetic"], "exit_code": 2})
        save(self.root / "reports/evidence" / (record["run_id"] + ".json"), record)
        self.assertIn("nonzero", " ".join(evaluate(self.root)["errors"]))

    def test_complete_synthetic_release_and_wrong_artifact(self):
        self.green_release()
        report = evaluate(self.root, "release")
        self.assertEqual(report["gate"], "PASS", report["errors"])
        self.assertEqual(report["verified_tasks"], 29)
        self.assertTrue(all(r["status"] == "PASS" for r in report["requirements"].values()))
        other = self.root / "reports/fixtures/other.exe"
        other.write_bytes(b"different synthetic package")
        self.state["release_artifact"] = file_ref(self.root, "reports/fixtures/other.exe")
        self.state_save()
        report = evaluate(self.root, "release")
        self.assertEqual(report["gate"], "NOT_READY")
        self.assertEqual(report["checks"]["TEST-014.RESULT.01"]["status"], "STALE")

    def test_command_capture_failure_preserved_and_zero_needs_review(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["--root", str(self.root), "capture", "--check", "TASK-001.BUILD", "--", sys.executable, "-c", "raise SystemExit(4)"])
            self.assertEqual(code, 1)
            code = main(["--root", str(self.root), "capture", "--check", "TASK-001.BUILD", "--", sys.executable, "-c", "print('synthetic')"])
            self.assertEqual(code, 0)
        records = [json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "reports/evidence").glob("*.json")]
        self.assertEqual({r["status"] for r in records}, {"FAIL", "BLOCKED"})
        self.assertEqual(evaluate(self.root)["checks"]["TASK-001.BUILD"]["status"], "BLOCKED")

    def test_timeout_is_a_failed_attempt(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["--root", str(self.root), "capture", "--check", "TASK-001.BUILD", "--timeout", "1", "--", sys.executable, "-c", "import time; time.sleep(5)"])
        self.assertEqual(code, 1)
        record = json.loads(next((self.root / "reports/evidence").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["command"]["exit_code"], 124)

    def test_submit_rejects_stale_and_duplicate_records(self):
        record = self.record("TASK-001.BUILD")
        draft = self.root / "reports/draft.json"
        save(draft, record)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "submit", str(draft)]), 2)
            record["run_id"] = "new-attempt"
            record["baseline"] = "stale"
            save(draft, record)
            self.assertEqual(main(["--root", str(self.root), "submit", str(draft)]), 2)

    def test_unknown_state_status_cannot_claim_verified(self):
        self.state["tasks"]["TASK-001"]["implementation"] = "VERIFIED"
        self.state_save()
        self.assertNotEqual(evaluate(self.root)["gate"], "PASS")

    def test_document_defects_and_archive_tampering_detected(self):
        with (self.root / "README.md").open("a", encoding="utf-8") as stream:
            stream.write('\nT01 TEST-099\n[bad anchor](docs/agent/任务拆解-P0.md#absent)\n```json\n{bad}\n```\n')
        archive = self.root / "docs/archive/v1/README.md"
        archive.write_bytes(b"tampered")
        errors = " ".join(validate(self.root)["errors"])
        for message in ("legacy ID", "undefined typed", "missing anchor", "invalid JSON", "Archive SHA mismatch"):
            self.assertIn(message, errors)

    def test_stage_risk_and_decision_gates_are_selected(self):
        report = evaluate(self.root, "P0")
        self.assertIn("DEC-003", report["selected_checks"])
        self.assertIn("R01", report["selected_checks"])
        self.assertNotIn("TEST-014.RESULT.01", report["selected_checks"])

    def test_global_failure_reopens_implementation_and_downstream(self):
        self.green_release()
        self.record("TEST-008.RESULT.01", "FAIL")
        report = evaluate(self.root, "release")
        self.assertEqual(report["tasks"]["TASK-019"]["acceptance"], "BLOCKED")
        self.assertEqual(report["tasks"]["TASK-029"]["acceptance"], "BLOCKED")

    def test_late_review_of_old_pass_cannot_hide_new_blocker(self):
        old = self.record("TASK-001.BUILD")
        self.record("TASK-001.BUILD", "BLOCKED")
        old["review"]["at"] = datetime.now(timezone.utc).isoformat()
        save(self.root / "reports/evidence" / (old["run_id"] + ".json"), old)
        self.assertEqual(evaluate(self.root)["checks"]["TASK-001.BUILD"]["status"], "BLOCKED")

    def test_duplicate_json_keys_are_rejected(self):
        path = self.root / "reports/evidence/bad.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"FAIL","status":"PASS"}', encoding="utf-8")
        self.assertIn("duplicate JSON key", " ".join(evaluate(self.root)["errors"]))

    def test_outside_evidence_path_rejected(self):
        record = self.record("TASK-001.BUILD")
        record["evidence"][0] = {"path": "../escape.log", "sha256": "irrelevant"}
        save(self.root / "reports/evidence" / (record["run_id"] + ".json"), record)
        self.assertNotEqual(evaluate(self.root)["gate"], "PASS")


if __name__ == "__main__":
    unittest.main()
