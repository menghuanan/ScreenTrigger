"""Exercise the release ABI in a disposable copy with its original runtime, DLL and relative data root."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO/"tools/packaging"))
from portable_layout import MARKER, host_path, metadata_dir
sys.path.insert(0,str(REPO/"tools/probes/windows"))
from verify_p1_runtime import Host


def run(folder,op,label):
    app=Path(sys.executable).resolve().parent
    if not (metadata_dir(app)/MARKER).is_file():
        raise ValueError("Launch with the test driver adjacent to the actual portable EXE")
    folder.mkdir(parents=True,exist_ok=False)
    host=Host(host_path(app),folder)
    report={"exe":sys.executable,"app":str(app),"operation":op,"label":label}
    try:
        host.session=host.call("start")["session_id"]
        report["hello"]=host.request("hello")
        models=host.request("list_ocr_models")
        report["models"]=models
        display=next(d["id"] for d in host.request("get_display_list")["displays"] if d["primary"])
        def execute(script):
            eid=host.request("execute_script",script_id=script,display_id=display)["execution_id"]
            return host.until(eid,"completed")
        if op=="save":
            meta=host.request("save_script",name=label,parameters=[],source="return 39")["script"]
            report["script"]=meta
            report["state"]=execute(meta["id"])
            assert report["state"]["result"]==39
        elif op=="load":
            matches=[s for s in host.request("list_scripts",offset=0,limit=100)["items"] if s["name"]==label]
            assert len(matches)==1,matches
            report["state"]=execute(matches[0]["id"])
            assert report["state"]["result"]==39
        elif op=="factory":
            assert len(report["hello"]["model_manifests"])==2
            caps={c["id"]:c["state"] for c in report["hello"]["capabilities"]}
            assert caps.get("feature_match")=="available" and "object_detect" not in caps
        elif op=="hold":
            (folder/"ready").write_text(host.session,"ascii")
            deadline=time.monotonic()+60
            while not (folder/"release").exists() and time.monotonic()<deadline: time.sleep(.05)
        assert not (folder/"data"/"config.json").exists(),"Release accepted the debug SIDECAR_DATA_DIR override"
        report["passed"]=True
    finally:
        host.close()
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("output",type=Path)
    p.add_argument("op",choices=["save","load","factory","hold"])
    p.add_argument("label",nargs="?",default="portable-039")
    args=p.parse_args()
    run(args.output.resolve(),args.op,args.label)
