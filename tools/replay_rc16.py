"""Replay the signed RC16 release audit and preserve its compatibility fixtures."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
AUDIT = "reports/audits/20261126T022504-714a9f96/result.json"
AUDIT_SHA = "42f780257bb0b406dfa0b0f1b32c39d8fc4f7a82c2c0389bd58c82e8023e2502"
BASELINE = "0a5ccb3a1105fe7597c1e3666c6a6d955ac60a5916b0911af99b7ce1a3698091"
PACKAGE_SHA = "2654243c6cb4d0f9c89b1fd609d1e7e6d94829259dda932abd4ca99c71ad0a10"
COMMIT = "6107a0b12a685a6b644c72d841a12ea94e27a021"
PRE_UPGRADE = "reports/raw/rc16-acceptance/pre-upgrade16.json"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def relative_file(folder, name):
    path = (folder / name).resolve()
    if Path(name).is_absolute() or not path.is_relative_to(folder):
        raise ValueError(f"Expected a path within {folder}: {name}")
    return path


def copy(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as inp, dest.open("xb") as out:
        shutil.copyfileobj(inp, out, 1024 * 1024)


def restore_sources(folder, files):
    for name, expected in files.items():
        dest = relative_file(folder, name)
        source = relative_file(ROOT, name)
        saved = ROOT / "reports/raw/history-source-sha256" / expected
        for candidate in (source, saved):
            if candidate.is_file() and digest(candidate) == expected:
                copy(candidate, dest)
                break
        else:
            # Git uses -text in this repository. Only the exact signed bytes qualify.
            blob = subprocess.run(["git", "show", f"{COMMIT}:{name}"], cwd=ROOT,
                                  capture_output=True, check=True).stdout
            if hashlib.sha256(blob).hexdigest() != expected:
                raise ValueError(f"Original RC16 source unavailable: {name} ({expected})")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)


def restore_audit_inputs(folder):
    state_file = "docs/agent/check-state.json"
    copy(ROOT / state_file, folder / state_file)
    state = read(folder / state_file)
    paths = {state["release_artifact"]["path"]}
    for change in state["scope_changes"]:
        paths.add(change["confirmation"]["path"])
        if "retired_inventory" in change:
            paths.add(change["retired_inventory"]["path"])
    for receipt in (ROOT / "reports/evidence").glob("*.json"):
        paths.add(receipt.relative_to(ROOT).as_posix())
        record = read(receipt)
        paths.update(item["path"] for item in record["evidence"])
        if record.get("artifact"):
            paths.add(record["artifact"]["path"])
    for archive in ("history-baselines", "history-source-sha256", "doc-sha256"):
        paths.update(path.relative_to(ROOT).as_posix()
                     for path in (ROOT / "reports/raw" / archive).iterdir() if path.is_file())
    for name in sorted(paths):
        # Source snapshots above take precedence, including when the live tree
        # has since introduced a file which did not exist in the signed release.
        if not name.startswith(("dist/", "reports/")):
            continue
        src = relative_file(ROOT, name)
        if src.is_file():
            copy(src, relative_file(folder, name))


def copy_installed_fixture(output):
    snapshot = ROOT / PRE_UPGRADE
    source_root = ROOT / "dist/p3/profile12/ScreenTrigger"
    inputs = []
    for entry in read(snapshot):
        # The signed run recorded absolute paths on its machine.
        origin = Path(entry["Path"])
        old_root = Path("D:/ScreenTrigger/dist/p3/profile12/ScreenTrigger")
        if not origin.is_relative_to(old_root):
            continue
        name = origin.relative_to(old_root)
        dest = relative_file(output, Path("installed-data") / name)
        copy(relative_file(source_root, name), dest)
        if digest(dest) != entry["Hash"]:
            raise ValueError(f"Installed fixture changed after RC16 inventory: {name}")
        inputs.append({"path": dest.relative_to(output).as_posix(), "sha256": entry["Hash"]})
    scripts = [item for item in inputs if item["path"].endswith(".lua")]
    if not scripts or not any(item["path"] == "installed-data/config.json" for item in inputs):
        raise ValueError("Missing legacy Lua or configuration in install data")
    return {"inventory": {"path": PRE_UPGRADE, "sha256": digest(snapshot)}, "files": inputs,
            "lua": scripts, "config": "installed-data/config.json"}


def main():
    started = datetime.now(timezone.utc)
    output = ROOT / "reports/automation-v2/raw" / ("rc16-" + uuid4().hex[:10])
    folder = output / "source"
    folder.mkdir(parents=True, exist_ok=False)
    print(output.relative_to(ROOT), flush=True)
    run = {"started_at": started.isoformat(), "environment": platform.platform(),
           "python": sys.version, "expected": "RC16 signed release reproduces exactly except generated_at",
           "original_result": {"path": AUDIT, "sha256": AUDIT_SHA}, "source_baseline": BASELINE,
           "candidate_sha256": PACKAGE_SHA, "exit_code": 1}
    try:
        if digest(ROOT / AUDIT) != AUDIT_SHA:
            raise ValueError("Signed RC16 result changed")
        original = read(ROOT / AUDIT)
        if original["baseline"] != BASELINE or original["release_artifact"]["sha256"] != PACKAGE_SHA:
            raise ValueError("Unexpected original source or candidate")
        restore_sources(folder, original["source_files"])
        restore_audit_inputs(folder)
        fixture = copy_installed_fixture(output)
        window = "tools/probes/windows/window_probe.py"
        fixture["window_probe"] = {
            "path": "source/" + window, "sha256": digest(folder / window),
            "argv": [sys.executable, "source/" + window, "--json-out", "window-observation.json"],
            "cwd": str(output), "purpose": "Dedicated Win32 window identity, move, resize, minimize and close",
        }
        write(output / "fixtures.json", fixture)
        cmd = [sys.executable, "-B", "-X", "utf8", "tools/run_acceptance.py", "audit", "--scope", "release"]
        run["command"] = cmd
        with (output / "output.log").open("xb") as log:
            result = subprocess.run(cmd, cwd=folder, stdout=log, stderr=subprocess.STDOUT)
        run["audit_exit_code"] = result.returncode
        result_dir = read(folder / "reports/latest-audit.json")["path"]
        result_file = relative_file(folder, Path(result_dir) / "result.json")
        actual = read(result_file)
        run["result"] = {"path": result_file.relative_to(ROOT).as_posix(), "sha256": digest(result_file)}
        run["counts"] = actual["counts"]
        run["verified_tasks"] = actual["verified_tasks"]
        run["differing_fields"] = sorted(key for key in actual.keys() | original.keys()
                                         if key != "generated_at" and actual.get(key) != original.get(key))
        if result.returncode or run["differing_fields"]:
            raise ValueError(f"RC16 release audit diverged: {run['differing_fields']}")
        run["exit_code"] = 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        run["error"] = str(error)
        print(error, file=sys.stderr)
    finally:
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        write(output / "replay.json", run)
    return run["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
