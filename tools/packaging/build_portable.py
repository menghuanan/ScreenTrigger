"""Publish and seal an automation-v2 ZIP from one frozen copy of the working source."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"tools"))
from run_acceptance import baseline, digest, now, save


def build(out, crt, pipelines):
    out=out.resolve()
    if out.parent != ROOT/"dist/automation-v2" or not out.name.startswith("ScreenTrigger-automation-v2-") or out.suffix!=".zip":
        raise ValueError("Choose a new dist/automation-v2/ScreenTrigger-automation-v2-<candidate>.zip")
    if out.exists():
        raise FileExistsError(out)
    manifest=baseline(ROOT)
    snapshot=ROOT/"dist/auto"/uuid4().hex[:10]
    workspace=snapshot/"src"
    workspace.mkdir(parents=True)
    record={"snapshot":str(workspace),"started":now(),"source":manifest["sha256"],"files":manifest["files"],"commands":[]}
    def run(arguments):
        record["commands"].append(arguments)
        subprocess.run(arguments,cwd=workspace,check=True,env={**os.environ,"DOTNET_CLI_TELEMETRY_OPTOUT":"1"})
    try:
        for rel,sha in manifest["files"].items():
            dest=workspace/rel
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/rel,dest)
            if digest(dest)!=sha:
                raise ValueError(f"Source changed during capture: {rel}")
        if baseline(workspace)["sha256"]!=manifest["sha256"]:
            raise ValueError("Incomplete source snapshot")
        ui=workspace/"dist/ui"
        run(["dotnet","publish","desktop/ScreenTrigger/ScreenTrigger.csproj","-c","Release","-p:PublishProfile=Offline",
             "-p:RestoreLockedMode=true","--disable-build-servers","-o",str(ui)])
        toolchain=ROOT/".venv/probe-toolchain"
        candidate=workspace/"dist/automation-v2"/out.name
        args=[sys.executable,"tools/packaging/build_offline.py","--portable","--out",str(candidate),
              "--ui",str(ui),"--crt",str(crt.resolve()),"--cpython",str(toolchain/"python.3.13.14.nupkg")]
        for name in ("wheels","models","nuget","cargo"):
            args.extend(("--"+name,str(toolchain/name)))
        for pipeline in pipelines:
            args.extend(("--pipeline",pipeline))
        run(args)
        out.parent.mkdir(parents=True,exist_ok=True)
        # Same volume. Windows rename refuses a concurrent candidate with the same name.
        candidate.rename(out)
        record.update(package=str(out),sha256=digest(out),bytes=out.stat().st_size)
        print(json.dumps({k:record[k] for k in ("source","package","sha256","bytes")}),flush=True)
    finally:
        record["finished"]=now()
        save(snapshot/"build.json",record,exclusive=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crt",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--pipeline",action="append",default=[])
    args=parser.parse_args()
    build(args.out,args.crt,args.pipeline)
