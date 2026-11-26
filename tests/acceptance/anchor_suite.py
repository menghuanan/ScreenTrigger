"""Build and exercise the current TASK-031 code. Each invocation writes a fresh evidence directory."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from run_acceptance import baseline


def now():
    return datetime.now(timezone.utc).isoformat()


def run(output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    host_build = ROOT / "dist/automation-work/anchors-independent"
    cargo = ["--offline", "--locked", "--manifest-path", "native/host/Cargo.toml", "--target-dir", str(host_build)]
    project = "desktop/ScreenTrigger.ContractTests/ScreenTrigger.ContractTests.csproj"
    assembly = "dist/desktop/bin/ScreenTrigger.ContractTests/debug/ScreenTrigger.ContractTests.dll"
    library = str(host_build / "debug/screen_trigger_lib.dll")
    checks = [
        ("rust", ["cargo", "test", *cargo], "Shared vectors, monitor gap zero sends, five DPI scales and existing host tests"),
        ("host", ["cargo", "build", *cargo], "Build the development host DLL"),
        ("python", [sys.executable, "-m", "unittest", "-v", "backend.tests.test_anchored_point",
                    "backend.tests.test_input_proxy", "backend.tests.test_execution_runtime",
                    "backend.tests.test_capture_qa_boundaries"], "Projection, old lease and capture regressions"),
        ("csharp-build", ["dotnet", "build", project, "--no-restore"], "Build the managed consumer"),
        ("csharp", ["dotnet", assembly, "types", str(output / "types")], "Managed shared vectors and all type cases"),
        ("window", [sys.executable, "tests/acceptance/anchor_window.py", library, str(output / "window")],
         "Three scopes, moved window, absolute click, rejection, stop/release and foreground loss through saved Lua"),
        ("docs", [sys.executable, "tools/check_docs.py"], "Repository documentation valid"),
    ]
    record = dict(started_at=now(), os=platform.platform(), cpu=platform.processor(),
                  runtime=sys.version, baseline=baseline(ROOT)["sha256"], commands=[])
    try:
        for name, command, expectation in checks:
            log = output / (name + ".log")
            started = now()
            with log.open("xb") as sink:
                code = subprocess.run(command, cwd=ROOT, stdout=sink, stderr=subprocess.STDOUT,
                                      env={**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT":"1", "PYTHONUTF8":"1"}).returncode
            record["commands"].append(dict(argv=command, expected=expectation, started_at=started, finished_at=now(),
                                            exit_code=code, path=log.name, sha256=hashlib.sha256(log.read_bytes()).hexdigest()))
            print(f"{name}: {code}", flush=True)
            if code:
                return 1
    finally:
        record.update(finished_at=now(), final_baseline=baseline(ROOT)["sha256"])
        (output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf8")
    if record["baseline"] != record["final_baseline"]:
        print("Source changed during this attempt.")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    sys.exit(run(parser.parse_args().output))
