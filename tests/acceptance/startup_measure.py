"""Measure launch and script completion through the released Explorer and WinUI entry points."""
import argparse
import ctypes
import datetime as dt
import hashlib
import json
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
import traceback
import zipfile

import psutil
from portable_media import CORE, MARKER, metadata_dir

SUITE = Path(__file__).resolve().parent
KINDS = ("lua", "zh", "ko")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write(path, content):
    with path.open("x", encoding="utf8") as stream:
        json.dump(content, stream, ensure_ascii=False, indent=2, allow_nan=False)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def roots(executable):
    for process in psutil.process_iter(["name"]):
        if (process.info["name"] or "").lower() != executable.name.lower():
            continue
        try:
            if Path(process.exe()) == executable:
                yield process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


class ProcessTree:
    def __init__(self, executable, output):
        self.executable = executable
        self.output = output
        self.processes = {}
        self.roots = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.error = None

    def sample(self):
        try:
            with self.output.open("x", encoding="utf8") as out:
                while not self.stop.is_set():
                    for process in roots(self.executable):
                        self.roots[process.pid] = process
                        try:
                            for child in [process, *process.children(recursive=True)]:
                                self.processes[child.pid, child.create_time()] = child
                        except psutil.NoSuchProcess:
                            pass
                    active = []
                    for (_, birth), proc in self.processes.items():
                        try:
                            if proc.is_running():
                                cpu = proc.cpu_times()
                                active.append(dict(pid=proc.pid, parent=proc.ppid(), birth=birth, exe=proc.exe(),
                                                   rss=proc.memory_info().rss, cpu_seconds=cpu.user+cpu.system))
                        except psutil.NoSuchProcess:
                            continue
                    print(json.dumps(dict(qpc_ns=time.perf_counter_ns(), system_cpu=psutil.cpu_percent(),
                                          tree=active, rss=sum(p["rss"] for p in active))), file=out, flush=True)
                    self.stop.wait(.10)
        except Exception:
            self.error = traceback.format_exc()

    def finish(self):
        until = time.monotonic()+10
        while time.monotonic() < until and any(p.is_running() for p in self.processes.values()):
            time.sleep(.1)
        self.stop.set()
        self.thread.join(5)
        live = [p.pid for p in self.processes.values() if p.is_running()]
        return dict(roots=list(self.roots), identities=[dict(pid=pid, birth=birth) for pid,birth in self.processes],
                    surviving=live, error=self.error)


def verified_snapshot(snapshot, spec, destination):
    assert not destination.exists(), 'The data root must start absent'
    expected = spec["snapshot"]
    actual = {p.relative_to(snapshot).as_posix():digest(p) for p in snapshot.rglob("*") if p.is_file()}
    assert actual == expected, "Preset data has changed"
    assert not any(p.startswith(("history/", "cache/")) for p in actual)
    shutil.copytree(snapshot, destination)


def evaluate(row, data, mode, scenario, ui):
    from startup_results import decode
    return decode(row, data, mode, scenario, ui)


def measure(app, snapshots, output, index, kind, mode):
    row = output / f"{index:02d}-{kind}"
    row.mkdir()
    data = app / "data"
    spec = read(snapshots / "snapshots.json")["groups"][kind]
    fixture = row / "fixture"
    fixture.mkdir()
    scenario = dict(name=spec["name"], script_id=spec["script_id"], kind=kind, model_id=spec["model_id"],
                    expected=spec["expected"], source_sha256=spec["source_sha256"],
                    count={'warm':21,'pilot':3}.get(mode,1), history=str(data/"history"), fixture=str(fixture),
                    timing=str(data/'diagnostics/startup-timing.jsonl'), worker_executable=str(app/'resources/python/python.exe'),
                    images=[dict(item, path=str(data/item["file"])) for item in spec["images"]])
    write(row/"scenario.json", scenario)
    exe = app/"ScreenTrigger.exe"
    assert not list(roots(exe)), 'Earlier test executable still running'
    verified_snapshot(snapshots/kind/"data", spec, data)
    sampler = ProcessTree(exe, row/"resources.jsonl")
    result = dict(kind=kind,index=index,started_at=utc(),mode=mode,expected=scenario["expected"])
    result['start_system_cpu_percent']=psutil.cpu_percent(interval=.15)
    sampler.thread.start()
    image_worker = None
    try:
        with (row/"console.log").open("x", encoding="utf8") as log:
            if kind != "lua":
                image_worker = subprocess.Popen([sys.executable,"-B",str(SUITE/"show_fixture.py"),str(fixture),
                                                 str(row/"scenario.json")], stdout=log, stderr=subprocess.STDOUT)
                until = time.monotonic()+15
                while not (fixture/"shown-0.json").exists():
                    if image_worker.poll() is not None or time.monotonic() > until:
                        raise RuntimeError("Initial visible pixels were not confirmed")
                    time.sleep(.04)
            command = ["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(SUITE/"portable_window.ps1"),
                       "-Executable",str(exe),"-OutputDirectory",str(row/"ui"),"-ExplorerLaunch",
                       "-StartupScenario",str(row/"scenario.json")]
            write(row/"command.json",command)
            rc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
            result["ui_exit"] = rc
            ui = read(row/"ui/window.json")
            if rc or ui.get("status") != "completed":
                raise RuntimeError(ui.get("error") or f"UI driver exit {rc}")
            result["executions"] = evaluate(row, data, mode, scenario, ui)
            assert len(result["executions"]) == scenario["count"]
            result["correct"] = True
    except Exception:
        result.update(correct=False, error=traceback.format_exc())
        if mode=='warm' and (row/'ui/window.json').exists():
            from startup_results import decode
            ui=read(row/'ui/window.json')
            result['warm_attempts']=[]
            for attempted in ui.get('runs',[]):
                if not attempted['index']: continue
                sample=dict(index=attempted['index'],duration_ms=None)
                try:
                    decoded,=decode(row,data,mode,scenario,dict(ui,runs=[attempted]),complete_series=False)
                    sample['duration_ms']=decoded['duration_ms']
                except Exception as error:
                    sample['error']=str(error)
                result['warm_attempts'].append(sample)
    finally:
        if image_worker:
            (fixture/"fixture-stop").touch()
            try:
                image_worker.wait(6)
            except subprocess.TimeoutExpired:
                image_worker.terminate()
                image_worker.wait(5)
                result.update(correct=False,fixture_cleanup="timeout")
        clean = sampler.finish()
        samples = [json.loads(ln) for ln in (row/'resources.jsonl').read_text('utf8').splitlines()]
        active = [x for x in samples if x['tree']]
        result['resources'] = dict(samples=len(samples),peak_tree_rss=max((x['rss'] for x in active),default=0),
                                   peak_tree_count=max((len(x['tree']) for x in active),default=0),
                                   last_active_rss=active[-1]['rss'] if active else None)
        executions=result.get('executions',[])
        if executions and mode!='baseline':
            first=executions[0]
            ready=next((e['qpc_ns'] for e in first['events'] if e['event']=='ocr_worker_ready'),first['endpoint_qpc_ns'])
            startup=[s['rss'] for s in active if s['qpc_ns']<=ready]
            loaded=[s['rss'] for s in active if ready<=s['qpc_ns']<=executions[-1]['endpoint_qpc_ns']]
            result['resources'].update(initialization_peak_rss=max(startup,default=None),
                                       loaded_tree_median_rss=statistics.median(loaded) if loaded else None,
                                       loaded_phase_samples=len(loaded))
            worker_ids={x['worker_pid'] for x in executions if 'worker_pid' in x}
            observed_ids={p['pid'] for s in active for p in s['tree']}
            if not worker_ids.issubset(observed_ids):
                result.update(correct=False,resource_error='An inference PID was not sampled in the product tree')
        result["processes"] = clean
        if len(clean["roots"]) != 1 or clean["error"] or clean["surviving"]:
            result.update(correct=False, cleanup_failure=True)
        for sub in ["diagnostics/startup-timing.jsonl","history"]:
            item=data/sub
            if item.is_dir(): shutil.copytree(item, row/item.name)
            if item.is_file(): shutil.copy2(item, row/item.name)
        retain = app.parent/(app.name+"-data")/output.name
        retain.mkdir(exist_ok=True, parents=True)
        if not clean["surviving"]:
            data.rename(retain/row.name)
        result["retained_data"] = str(retain/row.name) if not clean["surviving"] else str(data)
        result["finished_at"] = utc()
        result["evidence"] = {f.relative_to(row).as_posix():digest(f) for f in sorted(row.rglob("*")) if f.is_file()}
        write(row/"result.json",result)
        print(row.name,result["correct"],result.get("error", ""),result.get("executions", []), flush=True)
    return result


def environment(exe):
    kernel = ctypes.windll.kernel32
    serial = ctypes.c_uint()
    ok = kernel.GetVolumeInformationW(exe.anchor, None, 0, ctypes.byref(serial), None, None, None, 0)
    assert ok
    power_bytes = subprocess.check_output(['powercfg','/getactivescheme'])
    try: power=power_bytes.decode('utf8').strip()
    except UnicodeDecodeError: power=power_bytes.decode('oem').strip()
    return dict(windows=platform.platform(), build=sys.getwindowsversion().build, cpu=platform.processor(),
                logical=psutil.cpu_count(), physical=psutil.cpu_count(logical=False),
                disk=dict(root=exe.anchor,serial=serial.value,type=kernel.GetDriveTypeW(exe.anchor)),
                boot_utc=dt.datetime.fromtimestamp(psutil.boot_time(),dt.timezone.utc).isoformat(),
                power=power,
                screen=[ctypes.windll.user32.GetSystemMetrics(i) for i in (76,77,78,79,80)],
                idle_cpu_percent=psutil.cpu_percent(interval=.3), elevated=bool(ctypes.windll.shell32.IsUserAnAdmin()))


def main():
    if not __debug__: raise RuntimeError('Use Python with assertions enabled for this acceptance suite')
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("archive","app","snapshots","output"): parser.add_argument(field,type=Path)
    parser.add_argument("--mode",choices=["baseline","pilot","cold","warm"],required=True)
    parser.add_argument("--cases",nargs="+",choices=KINDS,default=list(KINDS))
    args=parser.parse_args()
    if args.mode in ('cold','warm') and args.cases!=list(KINDS):
        parser.error('The formal measurement requires lua, zh, ko in this order')
    archive,app,snapshots,output = (p.resolve() for p in (args.archive,args.app,args.snapshots,args.output))
    if not app.name.startswith("startup040-") or app.parent.name != "automation-work":
        raise ValueError("Use a dedicated startup040- disposable product copy under dist/automation-work")
    assert not (app/"data").exists() and not ctypes.windll.shell32.IsUserAnAdmin()
    output.mkdir(parents=True,exist_ok=False)
    opts = read(snapshots/"snapshots.json")
    metadata = metadata_dir(app)
    marker_file = metadata / MARKER
    with zipfile.ZipFile(archive) as package:
        assert package.read(marker_file.relative_to(app).as_posix()) == marker_file.read_bytes()
        assert package.read("ScreenTrigger.exe") == (app/"ScreenTrigger.exe").read_bytes()
    state = read(marker_file)
    assert opts["archive_source"] == state["source_sha256"] and set(state["factory"]) == {v["model_id"] for v in opts["groups"].values() if v["model_id"]}
    factory=app/'resources/manifest/factory-models.json'
    assert {x['recognition_model'] for x in read(factory)['engines']}==set(state['factory'])
    assert digest(metadata/CORE)==state['core_sha256']
    if not ctypes.windll.user32.GetShellWindow() or not any((p.info['name'] or '').lower()=='explorer.exe' for p in psutil.process_iter(['name'])):
        raise RuntimeError('A normal interactive Windows shell and its visible process table are required')
    shutil.copy2(factory,output/'factory-models.json')
    shutil.copyfile(snapshots/"snapshots.json",output/"snapshots.json")
    drivers=output/'drivers'
    drivers.mkdir()
    for name in ('startup_measure.py','startup_results.py','startup_snapshots.py','show_fixture.py','portable_window.ps1','startup_run_ui.ps1','portable_media.py'):
        shutil.copy2(SUITE/name,drivers/name)
    shutil.copy2(SUITE.parents[1]/"tools/packaging/portable_layout.py", drivers/"portable_layout.py")
    write(output/"plan.json",dict(archive=str(archive),sha256=digest(archive),marker=state,
                                   marker_sha256=digest(marker_file),executable_sha256=digest(app/"ScreenTrigger.exe"),
                                   factory_manifest_sha256=digest(factory),model_directory=str(app/'resources/weights'),
                                   normal_entry=str(app/"ScreenTrigger.exe"),mode=args.mode,environment=environment(app),
                                   entries={path.name:digest(path) for path in drivers.iterdir()},
                                   argv=sys.argv,started_at=utc()))
    rows=[]
    for index in range(20 if args.mode == "cold" else 1):
        for kind in args.cases:
            result=measure(app,snapshots,output,index,kind,args.mode)
            rows.append(result)
            if result["processes"]["surviving"] or not result["correct"] and args.mode in ('baseline','pilot'):
                break
        if rows[-1]["processes"]["surviving"]: break
    from startup_results import summarize
    summary=summarize(rows,args.mode,args.cases)
    write(output/"result.json",summary)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
