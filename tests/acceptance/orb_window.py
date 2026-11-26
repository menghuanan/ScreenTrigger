"""Measure the production draft-recognition job on a real bound Win32 fixture window."""
import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import sys
from ctypes import wintypes

from PIL import Image
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"tools/probes/windows"))
sys.path.insert(0,str(ROOT/"tests/acceptance"))
import common
from verify_p1_runtime import Host, user, wait
from orb_quality import overlap


def check(library, dataset, folder):
    folder.mkdir(parents=True, exist_ok=False)
    common.set_per_monitor_v2()
    positive = json.loads((dataset/"manifest.json").read_bytes())["rows"][0]
    with Image.open(dataset/positive["sample"]["file"]) as sample, Image.open(dataset/positive["scene"]["file"]) as scene:
        offset = 140
        canvas = Image.new("RGB",(offset+scene.width,scene.height))
        canvas.paste(sample,(0,0))
        canvas.paste(scene,(offset,0))
        canvas.save(folder/"fixture.png")
        sample_size, scene_size = sample.size, scene.size
    host = Host(library,folder)
    process = None
    previous = user.GetForegroundWindow()
    previous_cursor = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(previous_cursor))
    moved_to = None
    try:
        host.session = host.call("start")["session_id"]
        caps = host.request("hello")["capabilities"]
        assert next(c for c in caps if c["id"]=="feature_match")["state"]=="available", caps
        process = subprocess.Popen([sys.executable,"-B",str(ROOT/"tests/acceptance/show_fixture.py"),str(folder)])
        ready = folder/"fixture-visible.json"
        def visible_frame():
            try:
                return json.loads(ready.read_bytes())
            except (FileNotFoundError, json.JSONDecodeError):
                return None
        visible = wait(visible_frame,"dedicated image window")
        hwnd = visible["hwnd"]
        window_pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(ctypes.c_void_p(hwnd),ctypes.byref(window_pid))
        fixture_tree = {p.pid for p in psutil.Process(process.pid).children(recursive=True)} | {process.pid}
        assert window_pid.value in fixture_tree, (window_pid.value,fixture_tree)
        window = next(w for w in host.request("list_windows")["windows"] if w["pid"]==window_pid.value)
        user.SetForegroundWindow(hwnd)
        if user.GetForegroundWindow() != hwnd:
            x,y = visible["origin"]
            p = wintypes.POINT(x+4,y+4)
            assert user.WindowFromPoint(p)==hwnd and not user.GetAsyncKeyState(1)&0x8000
            assert user.SetCursorPos(p.x,p.y) and user.WindowFromPoint(p)==hwnd
            moved_to = p
            user.mouse_event(2,0,0,0,0)
            user.mouse_event(4,0,0,0,0)
        wait(lambda:user.GetForegroundWindow()==hwnd,"fixture owns foreground")
        binding = host.request("bind_window",window_id=window["window_id"])["binding"]["binding_id"]
        def capture(x, size):
            region=dict(space="window_client_physical",binding_id=binding,x=x,y=0,width=size[0],height=size[1])
            return host.request("capture_region",region=region)
        template = capture(0,sample_size)
        graph = {"op":"all","children":[{"id":"orb","type":"feature_match","params":{}}]}
        draft = dict(name="ORB fixture",conditions=graph,anchor_condition_id="orb",image_id=template["image"]["image_id"])
        snapshot = capture(offset,scene_size)
        accepted = host.request("test_recognition",draft=draft,image_id=snapshot["image"]["image_id"])
        def terminal(job_id):
            row=host.request("get_job",job_id=job_id)
            return row if row["status"] in ("completed","error","cancelled") else None
        job=wait(lambda:terminal(accepted["job_id"]),"draft recognition job",20)
        stored=host.request("create_target",draft=draft)["target"]
        saved=host.request("evaluate_target_conditions",target_id=stored["id"],image_id=snapshot["image"]["image_id"])
        replay=wait(lambda:terminal(saved["job_id"]),"saved target job",20)
        report=dict(capabilities=caps, window=window, visible=visible, binding=binding, template=template,
                    snapshot=snapshot, job=job, saved=replay, truth=positive["truth"], annotation_space="scene-local")
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        assert job["status"]=="completed" and len(job["result"]["matches"])==1,job
        assert replay["status"]=="completed" and replay["result"]["target_id"]==stored["id"], replay
        match=job["result"]["matches"][0]
        assert replay["result"]["matches"][0]["bbox"]==match["bbox"],replay
        assert job["result"]["frame_id"]==snapshot["frame_id"] and match["space"]=="screen_physical",match
        b=match["bbox"]
        box=[b["x"]-visible["origin"][0]-offset,b["y"]-visible["origin"][1],b["width"],b["height"]]
        score=max(overlap(box,truth) for truth in positive["truth"])
        assert score>=.5,(box,positive["truth"],score)
        print(json.dumps(dict(frame=snapshot["frame_id"],local_box=box,iou=score,confidence=match["confidence"])))
    finally:
        if process is not None:
            (folder/"fixture-stop").touch()
            process.wait(timeout=12)
        if previous and user.IsWindow(ctypes.c_void_p(previous)):
            user.SetForegroundWindow(previous)
        current = wintypes.POINT()
        if moved_to and user.GetCursorPos(ctypes.byref(current)) and (current.x,current.y)==(moved_to.x,moved_to.y):
            user.SetCursorPos(previous_cursor.x,previous_cursor.y)
        host.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    for name in ("library","dataset","folder"):
        parser.add_argument(name,type=Path)
    args=parser.parse_args()
    check(args.library,args.dataset,args.folder)
