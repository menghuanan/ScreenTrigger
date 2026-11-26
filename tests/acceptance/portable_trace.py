"""Observe a normal portable workload with an independent ETW consumer and nonempty controls."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import psutil
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"tests/acceptance"))
from portable_media import attach, sha
from portable_trace_files import review
from portable_trace_controls import exercise as exercise_controls
from portable_trace_scenes import measure, calibrate


def observe(archive,app,output,observer=None,orb_dataset=None,recover=False,calibration=False):
    if ctypes.windll.shell32.IsUserAnAdmin(): raise ValueError("Run the application workload under the standard user account")
    observer=(observer or ROOT/"dist/auto-observer/bin/PortableObserver/release/PortableObserver.exe").resolve(strict=True)
    if observer.name.lower()!='portableobserver.exe' or not observer.is_relative_to((ROOT/'dist').resolve()):
        raise ValueError('Expected a PortableObserver.exe published under this workspace dist directory')
    result_roots=ROOT/'reports/automation-v2/raw'
    if not output.is_relative_to(result_roots.resolve()) or output==result_roots.resolve():raise ValueError('Output must belong to the automation version raw directory')
    if not app.resolve().is_relative_to((ROOT/'dist/automation-work').resolve()):raise ValueError('Use a disposable extracted kit under dist/automation-work')
    output.mkdir(parents=True,exist_ok=False)
    helper=r"""$ErrorActionPreference='Stop'
$job=Start-Process -Verb RunAs -WindowStyle Hidden -PassThru -FilePath $env:ST_PORTABLE_OBSERVER -ArgumentList @($env:ST_PORTABLE_TRACED_PID,('"'+$env:ST_PORTABLE_EVIDENCE+'"'))
$job.WaitForExit()
exit $job.ExitCode"""
    env={**os.environ,"ST_PORTABLE_OBSERVER":str(observer),"ST_PORTABLE_TRACED_PID":str(os.getpid()),
         "ST_PORTABLE_EVIDENCE":str(output)}
    result={"zip":sha(archive),"root":str(app),"standard_user":True,"windows":[],"samples":[]}
    with (output/"observer-broker.log").open("xb") as broker_log:
        broker=subprocess.Popen(["powershell.exe","-NoProfile","-NonInteractive","-Command",helper],
                                env=env,stdout=broker_log,stderr=subprocess.STDOUT)
    log=output/"events.jsonl"
    try:
        begin=time.monotonic()
        while not log.exists() or not log.stat().st_size:
            if broker.poll() is not None or time.monotonic()-begin>60: raise RuntimeError("ETW observer unavailable")
            time.sleep(.1)
        with socket.socket() as server:
            server.bind(("127.0.0.1",0))
            server.listen()
            with socket.create_connection(server.getsockname(),timeout=3) as client,server.accept()[0] as peer:
                client.sendall(b"portable-control")
                assert peer.recv(16)==b"portable-control"
        with (output/"write-control.bin").open("xb",buffering=0) as control:
            control.write(bytes(65536))
            os.fsync(control.fileno())
        exercise_controls(output,result)
        if calibration:
            calibrate(app,output,result)
        else:
            run_release_workload(archive,app,output,result,orb_dataset,recover)
    finally:
        (output/"trace.done").write_text("done","ascii")
        try:
            broker.wait(35)
        except subprocess.TimeoutExpired:
            # Only our own unelevated waiter is terminated. A late observer sees
            # the completion file and cannot launch or modify the workload.
            broker.terminate()
            broker.wait(10)
        result["observer_exit"]=broker.returncode
        (output/"processes.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),"utf8")
    events=[json.loads(line) for line in log.read_text("utf8").splitlines()]
    summary=review(output,app,result,events)
    (output/"result.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),"utf8")
    if not summary["file_network_passed"]:raise RuntimeError("See result.json for file/network evidence failures")


def run_release_workload(archive,app,output,result,orb_dataset,recover):
    import shutil
    ui_root=app/"qa-logs/initial-window"
    ui_root.parent.mkdir(exist_ok=True)
    with (output/"ui.log").open("xb") as sink:
        ui=subprocess.Popen(["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(ROOT/"tests/acceptance/portable_window.ps1"),
            "-Executable",str(app/"ScreenTrigger.exe"),"-OutputDirectory",str(ui_root)],stdout=sink,stderr=subprocess.STDOUT)
        modules=set()
        while ui.poll() is None:
            tree=[]
            try: children=psutil.Process(ui.pid).children(recursive=True)
            except psutil.NoSuchProcess: children=[]
            for child in children:
                try:
                    tree.append({"pid":child.pid,"exe":child.exe(),"working_set":child.memory_info().rss})
                    if child.name().lower()=="screentrigger.exe" or child.name().lower().startswith("python"):
                        modules.update(m.path for m in child.memory_maps(grouped=True) if m.path and m.path.lower().endswith((".dll",".pyd")))
                except (psutil.NoSuchProcess,psutil.AccessDenied): continue
            result["samples"].append({"time":time.monotonic(),"tree":tree})
            time.sleep(.15)
        if ui.returncode:raise RuntimeError("Portable window failed")
    result["modules"]=sorted(modules)
    result["windows"].append(json.loads((ui_root/"window.json").read_text("utf-8-sig"))["pid"])
    # All probe output produced by this driver, as well as the actual
    # sidecar's data, is kept inside this freshly extracted disposable kit.
    shutil.copytree(ui_root,output/"first-window")
    attach(app)
    if orb_dataset is not None:measure(app,output,result,"orb",orb_dataset)
    (output/"desktop-done").write_text("done","ascii")
    profile_data=app/"data"/"profile-telemetry"
    exe=app/"portable-abi-test.exe"
    with (output/"profiles.log").open("xb") as sink:
        profile=subprocess.Popen([str(exe),"-I","-B","-X","utf8",str(ROOT/"tests/acceptance/portable_profiles.py"),
                                  str(ROOT),str(archive),str(profile_data)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        result["windows"].append(profile.pid)
        for line in profile.stdout: sink.write(line)
        if profile.wait(150): raise RuntimeError("Release model workload failed")
    assert json.loads((profile_data/"result.json").read_bytes())["passed"]
    shutil.copytree(profile_data,output/"profile-telemetry")
    measure(app,output,result,"fixed_ocr",profile_data/"zh.png")
    (output/"ocr-done").write_text("done","ascii")
    if recover:measure(app,output,result,"recovery",None)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    for arg in ("archive","app","output"):p.add_argument(arg,type=Path)
    p.add_argument('--observer',type=Path,help='Published read-only PortableObserver.exe')
    p.add_argument('--orb-dataset',type=Path,help='Frozen labelled ORB directory including manifest.json')
    p.add_argument('--recovery',action='store_true',help='Trace copies, both drive layouts, and recovery after lock or ACL refusal')
    p.add_argument('--calibrate',action='store_true',help='Check disk, both pipe controls and a model-list-only workload')
    a=p.parse_args()
    observe(a.archive.resolve(),a.app.resolve(),a.output.resolve(),a.observer,a.orb_dataset,a.recovery,a.calibrate)
