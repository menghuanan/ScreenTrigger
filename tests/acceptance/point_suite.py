"""Verify the TASK-032 WinUI pick and its existing rectangular-selection regression."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"tools"))
from run_acceptance import baseline


def now():
    return datetime.now(timezone.utc).isoformat()


def run(out):
    out=out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    # A fresh obj/bin pair keeps XAML metadata and its resource index from different builds apart.
    build=ROOT/"dist/automation-work"/("point-" + out.name)
    gui=build/"bin/ScreenTrigger/debug_win-x64/ScreenTrigger.DevPanel.exe"
    csharp=build/"bin/ScreenTrigger.ContractTests/debug/ScreenTrigger.ContractTests.dll"
    host_build=ROOT/"dist/automation-work/point-host"
    flags=[f"-p:ArtifactsPath={build}",f"-p:HostOutput={host_build}","-p:RestoreIgnoreFailedSources=true","-p:NuGetAudit=false"]
    checks=[
        ("host",["cargo","test","--offline","--locked","--manifest-path","native/host/Cargo.toml","--target-dir",str(host_build)],
         "Point terminal events, stale and wrong IDs, original rectangle/routing, monitor-gap and zero sends"),
        ("ui-build",["dotnet","build","desktop/ScreenTrigger/ScreenTrigger.csproj",*flags],"Build WinUI with a single intermediate/output tree"),
        ("contract-build",["dotnet","build","desktop/ScreenTrigger.ContractTests/ScreenTrigger.ContractTests.csproj",*flags],"Build managed contract suite"),
        ("dip",["dotnet",str(csharp),"points",str(out/"points")],"One physical-pixel point at five DPI settings and negative origin"),
        ("window",["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File","tools/probes/windows/verify-workspace.ps1",
         "-Executable",str(gui),"-OutputDirectory",str(out/"ui"),"-PointCases"],"Actual WinUI and private HWND, plus original rectangle capture"),
        ("docs",[sys.executable,"tools/check_docs.py"],"Shared contract documents and repository checks"),
    ]
    env={**os.environ,"PYTHONUTF8":"1","DOTNET_CLI_TELEMETRY_OPTOUT":"1","SIDECAR_PYTHON":str(ROOT/".venv/Scripts/python.exe")}
    report=dict(started_at=now(),baseline=baseline(ROOT)["sha256"],os=platform.platform(),processor=platform.processor(),
                python=sys.version,environment={"SIDECAR_PYTHON":env["SIDECAR_PYTHON"]},commands=[])
    try:
        for name,args,expected in checks:
            started=now(); log=out/(name+".log")
            with log.open("xb") as f:
                status=subprocess.run(args,env=env,cwd=ROOT,stdout=f,stderr=subprocess.STDOUT).returncode
            report["commands"].append(dict(argv=args,expected=expected,exit_code=status,started_at=started,finished_at=now(),
                                        path=log.name,sha256=hashlib.sha256(log.read_bytes()).hexdigest()))
            print(name,status,flush=True)
            if status: return 1
    finally:
        report.update(finished_at=now(),final_baseline=baseline(ROOT)["sha256"])
        (out/"run.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")
    return int(report["baseline"]!=report["final_baseline"])


if __name__=="__main__":
    sys.exit(run(Path(sys.argv[1])))

