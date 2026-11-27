"""Run extract/move/copy/upgrade and failure scenarios against one actual portable archive."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

from portable_media import MARKER, attach, metadata_dir, sha

ROOT=Path(__file__).resolve().parents[2]


def exercise(archive, clean, raw, previous_app=None, previous_zip=None):
    scratch=(ROOT/"dist/automation-work"/("recovery-"+uuid4().hex[:10])).resolve()
    scratch.mkdir()
    raw.mkdir(parents=True,exist_ok=False)
    raw=raw.resolve()
    report={"package":str(archive),"sha256":sha(archive),"steps":[],"work":str(scratch)}
    prepared=clean.resolve()
    assert (metadata_dir(prepared)/MARKER).is_file() and not (prepared/"data").exists()
    def command(name,args,ok=True,env=None):
        started=time.monotonic()
        with (raw/(name+".log")).open("xb") as stream:
            result=subprocess.run([str(a) for a in args],cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=180)
        report["steps"].append({"case":name,"command":[str(a) for a in args],"code":result.returncode,
                                "elapsed":time.monotonic()-started,"log_sha256":sha(raw/(name+".log"))})
        if (result.returncode==0)!=ok:raise RuntimeError(name)
    def gui(name,app,expected="ready"):
        command(name,["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",ROOT/"tests/acceptance/portable_window.ps1",
            "-Executable",app/"ScreenTrigger.exe","-OutputDirectory",raw/(name+"-window"),"-Expect",expected,
            *(["-ExplorerLaunch"] if name=="unmodified-first" else [])])
    def abi(name,app,op,label="portable-039"):
        command(name,[app/"portable-abi-test.exe","-I","-B","-X","utf8",ROOT/"tests/acceptance/portable_abi.py",raw/(name+"-host"),op,label])
    def clone(name,source):
        path=scratch/name
        assert path.resolve().parent==scratch and source.resolve()!=path.resolve()
        shutil.copytree(source,path)
        return path
    def deny(name,app,directory):
        directory=directory.resolve()
        assert directory.is_relative_to(scratch) and directory.is_dir()
        command(name+"-acl",["icacls",directory,"/deny","*S-1-1-0:(WD)"])
        try:
            try:
                with (directory/"control-new").open("xb"): pass
            except PermissionError: pass
            else: raise AssertionError("Control file remained writable after ACL")
            gui(name,app,"error")
        finally:
            command(name+"-release-acl",["icacls",directory,"/remove:d","*S-1-1-0"])
    try:
        app=clone("first",prepared)
        gui("unmodified-first",app)
        assert (app/"data/config.json").is_file()
        attach(app)
        abi("factory",app,"factory")
        abi("save-before-move",app,"save")
        moved=scratch/"moved"
        assert app.resolve().parent==moved.resolve().parent==scratch
        app.rename(moved)
        abi("reopen-after-move",moved,"load")
        drive=next(f"{chr(letter)}:" for letter in range(ord("Z"),ord("N"),-1) if not os.path.exists(f"{chr(letter)}:/"))
        command("map-test-drive",["subst",drive,scratch])
        try:
            mapped=Path(drive+"/moved")
            assert os.path.samefile(mapped,moved)
            gui("changed-drive",mapped)
            abi("changed-drive-read",mapped,"load")
        finally:
            command("unmap-test-drive",["subst",drive,"/D"])
        command("map-drive-root",["subst",drive,moved])
        try:
            mapped_root=Path(drive+"/")
            assert os.path.samefile(mapped_root,moved)
            gui("direct-drive-root",mapped_root)
            abi("direct-drive-root-read",mapped_root,"load")
        finally:
            command("unmap-drive-root",["subst",drive,"/D"])
        second=clone("second",moved)
        abi("write-copy",second,"save","copy-only")
        abi("write-original",moved,"save","original-only")
        def names(app):
            cat=app/"data/catalog"
            current=json.loads((cat/"current.json").read_bytes())
            return {s["name"] for s in json.loads((cat/"generations"/current["generation"]/"script_metadata.json").read_bytes())["scripts"]}
        assert "copy-only" in names(second)-names(moved) and "original-only" in names(moved)-names(second)
        old=clone("selected-old-install",ROOT/"reports/automation-v2/raw/rc16-98a2e30ced/installed-data")
        upgrade=clone("upgrade",prepared)
        def migration(label,source,app):
            command(label,[app/"resources/python/python.exe","-I","-B","-X","utf8",app/"resources/portable_data.py","--copy-from",source])
        source_folder=moved
        if previous_app is not None:
            import zipfile
            with zipfile.ZipFile(previous_zip) as previous:
                old_marker=metadata_dir(previous_app)/MARKER
                marker=previous.read(old_marker.relative_to(previous_app).as_posix())
            assert marker==old_marker.read_bytes()
            assert marker!=(metadata_dir(prepared)/MARKER).read_bytes()
            report["previous_zip"]={"path":str(previous_zip),"sha256":sha(previous_zip)}
            source_folder=previous_app
        def contents(data):
            return {p.relative_to(data).as_posix():sha(p) for p in data.rglob("*")
                    if p.is_file() and p.name not in ("engine-session.lock","writer.lock",".portable-layout.lock")}
        before=contents(source_folder/"data")
        sampled=[]
        finished=threading.Event()
        def sample_staging():
            while not finished.is_set():
                size=0
                for directory in [*upgrade.glob(".migrate-*"),upgrade/"data"]:
                    for item in directory.rglob("*"):
                        try:
                            if item.is_file(): size+=item.stat().st_size
                        except FileNotFoundError:
                            continue
                sampled.append(size)
                finished.wait(.05)
        sampler=threading.Thread(target=sample_staging)
        sampler.start()
        try:
            migration("portable-upgrade",source_folder/"data",upgrade)
        finally:
            finished.set()
            sampler.join()
        transferred=contents(upgrade/"data")
        assert before==transferred==contents(source_folder/"data")
        final_size=sum((upgrade/"data"/name).stat().st_size for name in transferred)
        report["upgrade"]={"verified_files":len(before),"verified_bytes":final_size,
            "peak_staged_logical_bytes":max([final_size,*sampled]),"samples":len(sampled),"sample_interval_seconds":.05}
        (raw/"upgrade-hashes.json").write_text(json.dumps(before),encoding="utf8")
        attach(upgrade)
        abi("upgrade-old-lua",upgrade,"load")
        migrated=clone("legacy-migration",prepared)
        command("choose-roots",[migrated/"resources/python/python.exe","-I","-B",migrated/"resources/portable_data.py","--inspect",old,moved/"data"])
        migration("chosen-installed-root",old,migrated)
        gui("open-legacy-copy",migrated)
        assert names(migrated)=={s["name"] for s in json.loads(next((old/"catalog/generations").glob("*/script_metadata.json")).read_bytes())["scripts"]}
        lock_raw=raw/"holding"
        holding=subprocess.Popen([str(moved/"portable-abi-test.exe"),"-I","-B",str(ROOT/"tests/acceptance/portable_abi.py"),str(lock_raw),"hold"],
                                 cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+45
            while not (lock_raw/"ready").exists():
                if holding.poll() is not None or time.monotonic()>deadline:raise RuntimeError("lock holder failed")
                time.sleep(.05)
            command("second-writer",[moved/"portable-abi-test.exe","-I","-B",ROOT/"tests/acceptance/portable_abi.py",raw/"blocked-host","save","forbidden-concurrent"],ok=False)
        finally:
            (lock_raw/"release").write_text("release","ascii")
            holding.wait(30)
        assert "forbidden-concurrent" not in names(moved)
        abi("after-lock",moved,"load")
        deny("read-only-root",second,second)
        deny("read-only-cache",second,second/"data/cache")
        core=second/"便携版使用说明.txt"
        original=core.read_bytes()
        try:
            core.write_bytes(bytes([original[0]^1])+original[1:])
            gui("damaged-core",second,"error")
        finally: core.write_bytes(original)
        abi("after-core-repair",second,"load")
        report["passed"]=True
    finally:
        (raw/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("archive",type=Path)
    p.add_argument("clean",type=Path)
    p.add_argument("raw",type=Path)
    p.add_argument("--previous-app",type=Path)
    p.add_argument("--previous-zip",type=Path)
    args=p.parse_args()
    if bool(args.previous_app)!=bool(args.previous_zip):
        p.error("Select the original app and ZIP together")
    exercise(args.archive.resolve(),args.clean,args.raw,
             args.previous_app.resolve() if args.previous_app else None,
             args.previous_zip.resolve() if args.previous_zip else None)
