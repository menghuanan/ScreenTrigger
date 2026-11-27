"""Exercise both public acceptance CLIs in disposable repositories, without product evidence."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from run_acceptance import spec_hash


VERSION = "automation-v2"
RC16 = ROOT / "dist/p3/ScreenTrigger-x64-rc16.zip"
RC16_SHA256 = "2654243c6cb4d0f9c89b1fd609d1e7e6d94829259dda932abd4ca99c71ad0a10"


def read(path):
    return json.loads(path.read_bytes())


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


class AutomationVersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="screentrigger-version-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        ignore = shutil.ignore_patterns("__pycache__", "target", "obj", "bin", "check-state.json")
        for name in ("docs", "tools", "tests", "backend", "desktop", "native", "schema", "manifest"):
            shutil.copytree(ROOT / name, self.root / name, ignore=ignore)
        for name in ("AGENTS.md", "README.md"):
            shutil.copy2(ROOT / name, self.root / name)
        self.reports = self.root / "reports"
        self.version_reports = self.reports / VERSION
        self.cli("catalog", version=None)
        legacy = read(self.reports / "check-catalog.json")
        self.legacy_tasks = legacy["tasks"]
        self.legacy_tests = legacy["tests"]
        self.legacy_state = self.root / "docs/agent/check-state.json"
        write(self.legacy_state, {
            "schema_version": 1,
            "tasks": {tid: {"owner": "test fixture", "implementation": "NOT_STARTED"}
                      for tid in self.legacy_tasks},
            "issues": [], "scope_changes": [], "invalidations": [], "release_artifact": None,
        })
        self.cli("init")
        self.ledger = self.version_reports / "check-state.json"
        self.cli("catalog")
        self.catalog = read(self.version_reports / "check-catalog.json")

    def cli(self, action, *args, code=0, version=VERSION):
        command = [sys.executable, "-X", "utf8", str(self.root / "tools/run_acceptance.py"),
                   "--root", str(self.root), action]
        if version:
            command.extend(["--version", version])
        command.extend(str(arg) for arg in args)
        result = subprocess.run(command, cwd=self.root, capture_output=True, encoding="utf-8",
                                timeout=60)
        self.assertEqual(result.returncode, code,
                         f"{command}\nstdout: {result.stdout}\nstderr: {result.stderr}")
        return result.stdout.strip()

    def audit(self, *args, code, version=VERSION):
        summary = Path(json.loads(self.cli("audit", *args, code=code, version=version))["report"])
        owner = self.reports if version is None else self.version_reports
        self.assertEqual(summary.parent.parent, owner / "audits")
        self.assertTrue(summary.is_file())
        return read(summary.with_name("result.json"))

    def mark_implemented(self, ledger, tid):
        task = self.catalog["tasks"][tid]
        ledger["tasks"][tid].update(implementation="IMPLEMENTED", owner="isolated tool test",
                                    opened_at=datetime.now(timezone.utc).isoformat(),
                                    dependencies=task["dependencies"], tests=task["tests"],
                                    allowed_modules=["tools/tests/"], expected=[task["deliverable"]])

    def test_catalog_ledger_and_report_isolation(self):
        self.assertEqual(len(self.legacy_tasks), 29)
        self.assertEqual(len(self.legacy_tests), 16)
        self.assertEqual(set(self.catalog["tasks"]), {f"TASK-{n:03}" for n in range(30, 46)})
        self.assertEqual(set(self.catalog["tests"]), {f"TEST-{n:03}" for n in range(17, 26)})
        self.assertEqual(self.catalog["tasks"]["TASK-045"]["dependencies"], ["TASK-044"])
        self.assertEqual(self.catalog["tasks"]["TASK-039"]["dependencies"], ["TASK-038", "TASK-042"])
        self.assertEqual(set(self.catalog["tasks"]["TASK-044"]["dependencies"]),
                         {f"TASK-{n:03}" for n in (32, 34, 35, 37, 40, 41, 42, 43)})
        self.assertTrue(set(self.catalog["tasks"]).isdisjoint(self.legacy_tasks))
        self.assertEqual(read(self.ledger)["version"], VERSION)
        previous_state = self.ledger.read_bytes()
        self.cli("init", code=2)
        self.assertEqual(self.ledger.read_bytes(), previous_state)

        legacy = self.audit("--scope", "baseline", code=0, version=None)
        legacy_pointer = (self.reports / "latest-audit.json").read_bytes()
        legacy_ledger = self.legacy_state.read_bytes()
        baseline = self.audit("--scope", "baseline", code=0)
        self.assertEqual(set(legacy["tasks"]), set(self.legacy_tasks))
        self.assertEqual(set(baseline["tasks"]), set(self.catalog["tasks"]))
        ledger = read(self.ledger)
        self.mark_implemented(ledger, "TASK-030")
        write(self.ledger, ledger)
        task = self.audit("--task", "TASK-030", code=1)
        self.assertEqual(task["errors"], [])
        self.assertEqual(task["gate"], "NOT_READY")
        self.assertNotEqual(task["tasks"]["TASK-030"]["acceptance"], "VERIFIED")
        self.assertEqual(task["baseline"], baseline["baseline"])
        group = self.audit("--test", "TEST-017", code=1)
        self.assertEqual(set(group["selected_checks"]), set(self.catalog["tests"]["TEST-017"]["checks"]))
        release = self.audit("--scope", "release", code=1)
        self.assertEqual(release["task_denominator"], 16)
        self.assertEqual(release["verified_tasks"], 0)
        self.assertEqual((self.reports / "latest-audit.json").read_bytes(), legacy_pointer)
        self.assertEqual(self.legacy_state.read_bytes(), legacy_ledger)

    def reviewed_draft(self, check, raw, version=VERSION):
        draft = Path(self.cli("template", "--check", check, version=version))
        record = read(draft)
        relative = raw.relative_to(self.root).as_posix()
        record.update(status="PASS", steps=["Check synthetic receipt isolation."],
                      actual="Observed the synthetic fixture.", evidence=[{
                          "path": relative, "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                      }], environment={k: "synthetic tool-test fixture"
                                       for k in ("os", "hardware", "runtime_versions", "datasets",
                                                 "coverage", "models")}, review={
                          "at": datetime.now(timezone.utc).isoformat(), "decision": "ACCEPT",
                          "by": "tool test", "note": "Only the isolated checker is under test.",
                      })
        write(draft, record)
        return draft, record

    def test_old_pass_cannot_be_used_as_new_evidence_or_release_media(self):
        raw = self.reports / "raw/legacy-test.log"
        raw.parent.mkdir(parents=True)
        raw.write_text("synthetic legacy tool test", encoding="utf-8")
        old_draft, old = self.reviewed_draft("TASK-001.BUILD", raw, version=None)
        old_receipt = Path(self.cli("submit", old_draft, version=None))
        self.assertEqual(old_receipt.parent, self.reports / "evidence")
        self.cli("submit", old_draft, code=2)
        new_check = self.catalog["tasks"]["TASK-030"]["checks"][0]
        new_raw = self.version_reports / "raw/version-test.log"
        new_raw.parent.mkdir(parents=True, exist_ok=True)
        new_raw.write_text("synthetic current tool test", encoding="utf-8")
        draft, new = self.reviewed_draft(new_check, new_raw)

        # Even if retyped against today's check, a legacy raw file or versionless
        # receipt must not be accepted into the new ledger.
        for field in ("version", "evidence", "artifact"):
            altered = deepcopy(new)
            if field == "version":
                del altered[field]
            elif field == "evidence":
                altered[field] = old["evidence"]
            else:
                package = self.root / "dist/ScreenTrigger-RC16.zip"
                package.parent.mkdir()
                package.write_bytes(b"synthetic old media, not a real application")
                altered[field] = {"path": package.relative_to(self.root).as_posix(),
                                  "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}
            with self.subTest(field=field):
                write(draft, altered)
                self.cli("submit", draft, code=2)
                self.assertFalse((self.version_reports / "evidence" / (new["run_id"] + ".json")).exists())

        ledger = read(self.ledger)
        ledger["release_artifact"] = altered["artifact"]
        write(self.ledger, ledger)
        report = self.audit("--scope", "baseline", code=1)
        self.assertTrue(report["errors"])
        self.assertNotEqual(report["tasks"]["TASK-030"]["acceptance"], "VERIFIED")
        self.assertEqual(read(old_receipt), old)

        # The exact same draft with its own verified raw file must be accepted.
        ledger["release_artifact"] = None
        write(self.ledger, ledger)
        write(draft, new)
        accepted = Path(self.cli("submit", draft))
        self.assertEqual(accepted.parent, self.version_reports / "evidence")
        result = self.audit("--task", "TASK-030", code=1)
        self.assertEqual(result["checks"][new_check]["status"], "PASS")
        self.assertNotEqual(result["tasks"]["TASK-030"]["acceptance"], "VERIFIED")

    @unittest.skipUnless(RC16.is_file(), "Requires the original local RC16 distribution.")
    def test_renamed_original_package_rejected_by_contents(self):
        # The old file is read-only input. Copy it to a legal *new* name to test
        # content ownership independently of the normal filename rejection.
        path = self.root / "dist" / VERSION / "ScreenTrigger-automation-v2-run01.zip"
        path.parent.mkdir(parents=True)
        shutil.copyfile(RC16, path)
        with path.open("rb") as stream:
            file_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        self.assertEqual(file_hash, RC16_SHA256)
        ledger = read(self.ledger)
        ref = {"path": path.relative_to(self.root).as_posix(), "sha256": file_hash}
        ledger["release_artifact"] = ref
        write(self.ledger, ledger)
        rejected = self.audit("--scope", "baseline", code=1)
        self.assertTrue(rejected["errors"])
        # Same path, discrete, valid archive for the checker-only positive control.
        with ZipFile(path, "w") as archive:
            archive.writestr("fixture.txt", "Synthetic isolation test, not a product package.")
        ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        write(self.ledger, ledger)
        allowed = self.audit("--scope", "baseline", code=0)
        self.assertEqual(allowed["release_artifact"], ref)
        self.assertEqual(allowed["verified_tasks"], 0)

    def test_legacy_closed_issue_and_scope_decision_do_not_transfer(self):
        raw = self.reports / "raw/legacy-test.log"
        raw.parent.mkdir(parents=True)
        raw.write_text("synthetic scope and regression fixture", encoding="utf-8")
        key = "TASK-001.CONTRACT"
        draft, receipt = self.reviewed_draft(key, raw, version=None)
        self.cli("submit", draft, version=None)
        # Build a closed issue valid in the legacy fixture, then try importing it.
        legacy = read(self.legacy_state)
        issue = {
            "id": "CLOSED-FIXTURE", "severity": "minor", "status": "CLOSED",
            "owner": "checker fixture", "created_at": receipt["started_at"], "due_at": receipt["finished_at"],
            "expected": "synthetic fixture condition", "actual": "synthetic historical issue",
            "reproduction": "inspect fixture log", "check_ids": [key], "task_ids": ["TASK-001"],
            "failed_run_ids": [], "regression_checks": [key], "retest_run_ids": [receipt["run_id"]],
            "fix": {"description": "fixture repair", "by": "test", "at": receipt["started_at"]},
            "closed_at": receipt["review"]["at"], "closed_by": "tool test",
        }
        change = {
            "id": "SCOPE-FIXTURE", "reason": "synthetic old version scope change", "version": "v1",
            "approved_by": "tool test", "approved_at": receipt["started_at"],
            "expires_at": "9999-12-31T00:00:00+00:00", "confirmation": receipt["evidence"][0],
            "affected_checks": [key],
        }
        legacy["scope_changes"] = [change]
        legacy["issues"] = [issue]
        write(self.legacy_state, legacy)
        original = self.audit("--scope", "baseline", code=0, version=None)
        self.assertEqual(original["issues"][0]["status"], "CLOSED")
        pristine = read(self.ledger)
        for field in ("scope_changes", "issues"):
            with self.subTest(field=field):
                new_ledger = dict(pristine, **{field: legacy[field]})
                write(self.ledger, new_ledger)
                refused = self.audit("--task", "TASK-030", code=1)
                self.assertTrue(refused["errors"])
                self.assertNotEqual(refused["tasks"]["TASK-030"]["acceptance"], "VERIFIED")
                if field == "issues":
                    self.assertEqual(refused["issues"][0]["status"], "INVALID")

    def test_task_044_needs_same_package_groups_and_signoff(self):
        package = self.root / "dist" / VERSION / "ScreenTrigger-automation-v2-fixture.zip"
        package.parent.mkdir(parents=True)
        with ZipFile(package, "w") as archive:
            archive.writestr("fixture.txt", "checker test only")
        media = {"path": package.relative_to(self.root).as_posix(),
                 "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}
        ledger = read(self.ledger)
        ledger["release_artifact"] = media
        for tid in self.catalog["tasks"]:
            self.mark_implemented(ledger, tid)
        write(self.ledger, ledger)
        raw = self.version_reports / "raw/checker-fixture.log"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("Synthetic test of the final gate; no product ran.", encoding="utf-8")
        _, receipt = self.reviewed_draft("TASK-030.CATALOG", raw)

        def install_fixture(kind):
            for key, check in self.catalog["checks"].items():
                if check["kind"] != kind:
                    continue
                run_id = "synthetic-" + key.replace(".", "-")
                row = dict(receipt, run_id=run_id, expected=check["expected"], spec_sha256=spec_hash(check),
                           check_id=key, artifact=media)
                write(self.version_reports / "evidence" / f"{run_id}.json", row)

        install_fixture("task")
        modules_only = self.audit("--task", "TASK-044", code=1)
        self.assertEqual(modules_only["errors"], [])
        self.assertEqual(modules_only["verified_tasks"], 14)
        self.assertNotEqual(modules_only["tasks"]["TASK-044"]["acceptance"], "VERIFIED")
        install_fixture("acceptance")
        no_signoff = self.audit("--task", "TASK-044", code=1)
        self.assertEqual(no_signoff["checks"]["RELEASE.REVIEW"]["status"], "NOT_RUN")
        self.assertNotEqual(no_signoff["tasks"]["TASK-044"]["acceptance"], "VERIFIED")
        install_fixture("release")
        complete = self.audit("--scope", "release", code=0)
        self.assertEqual(complete["verified_tasks"], 16)

        replacement = package.with_name("ScreenTrigger-automation-v2-replacement.zip")
        with ZipFile(replacement, "w") as archive:
            archive.writestr("fixture.txt", "second checker-only package")
        ledger["release_artifact"] = {"path": replacement.relative_to(self.root).as_posix(),
                                      "sha256": hashlib.sha256(replacement.read_bytes()).hexdigest()}
        write(self.ledger, ledger)
        stale = self.audit("--task", "TASK-044", code=1)
        self.assertEqual(stale["checks"]["RELEASE.REVIEW"]["status"], "STALE")
        self.assertNotEqual(stale["tasks"]["TASK-044"]["acceptance"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()
