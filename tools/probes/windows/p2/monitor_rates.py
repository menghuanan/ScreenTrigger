"""P2 real-time monitor through the production controller, capture/store, OpenCV and Lua."""
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import cv2
import numpy as np
import psutil
from backend.main import (AssetDataRoot, ConditionTreeEvaluator, SidecarArgs, _AssetTargetImageResolver,
                          build_catalog_services, build_default_vision_service)
from backend.capture.ports import RawImage
from backend.capture.service import CaptureService
from backend.image.store import SessionImageStore
from backend.script.controller import ExecutionController
from backend.types.region import DisplayInfo, Rect

out = Path(sys.argv[1])
out.mkdir(exist_ok=False)
short = "--short" in sys.argv
rng = np.random.default_rng(74231)
scene = rng.integers(0,150,(600,800,3),dtype=np.uint8)
template = rng.integers(0,256,(100,100,3),dtype=np.uint8)
scene[125:225,340:440] = template
frame = RawImage(800,600,scene.tobytes())
class Frames:
    def list_displays(self):
        return [DisplayInfo("fixed",Rect(0,0,800,600),1,True)]
    def capture_bgr(self,x,y,w,h,cancel):
        cancel.throw_if_cancelled()
        assert (x,y,w,h) == (0,0,800,600)
        return frame
store=SessionImageStore("p2-rate")
args=SidecarArgs("p2-rate",data_dir=str((out/"data").resolve()))
repo,targets,scripts=build_catalog_services(args,store)
ref=store.put(RawImage(100,100,template.tobytes()),"png")
target=targets.create_target(dict(name="fixed",image_id=ref.image_id,anchor_condition_id="m",
    conditions={"op":"all","children":[{"id":"m","type":"template","params":{"threshold":.91}}]}))["id"]
script=scripts.save_script("测量动作","assert(args.match.found); return args.match.sample_no",
    [dict(name="match",type="object",required=True,default=None,description="")])["id"]
resolver=_AssetTargetImageResolver(store,AssetDataRoot(args.data_dir))
vision=build_default_vision_service(args,repository=repo,image_store=store,
    evaluator_factory=lambda reg:ConditionTreeEvaluator(registry=reg,target_image_resolver=resolver))
capture=CaptureService(Frames(),Frames(),None,store)
process=psutil.Process()
points=[]
hits=[]
failure=threading.Event()
def event(name,data):
    if name=="recognition_stats":
        points.append((time.perf_counter()-started,data["effective_hz"],data["throttled"],process.memory_info().rss,len(store._entries)))
    elif name=="target_detected":
        hits.append(data["result"])
    elif name=="script_status" and data.get("status")=="failed":
        failure.set()
controller=ExecutionController(scripts,capture,vision,None,dict,event,None)
report=dict(seed=74231,shape=[800,600],template=[100,100],opencv=cv2.__version__,
    roi_sha256=hashlib.sha256(scene.tobytes()).hexdigest(),template_sha256=hashlib.sha256(template.tobytes()).hexdigest(),runs=[])
with (out/"ticks.jsonl").open("x",encoding="utf8") as log:
    try:
        for hz,seconds in ([(5,2)] if short else [(1,600),(5,20),(10,15),(30,10)]):
            points.clear()
            hits.clear()
            failure.clear()
            started=time.perf_counter()
            eid=controller.command("start_monitor",dict(target_id=target,display_id="fixed",frequency_hz=hz,
                on_match="run_script",repeat=True,action_script_id=script,cooldown_ms=0,wait_timeout_ms=0))["execution_id"]
            if not failure.wait(max(0,started+seconds-time.perf_counter())):
                controller.command("stop_script",dict(execution_id=eid))
            with controller.changed:
                assert controller.changed.wait_for(lambda:eid not in controller.roots,5)
            state=controller.command("get_script_state",dict(execution_id=eid))["state"]
            assert state["status"]=="cancelled" and points and len(hits)==1,(state,len(hits),len(points))
            box=hits[0]["matches"][0]["bbox"]
            assert (box["x"],box["y"],box["width"],box["height"])==(340,125,100,100),box
            for t,actual,throttled,rss,images in points:
                log.write(json.dumps(dict(requested_hz=hz,t=t,effective_hz=actual,throttled=throttled,rss=rss,frame_entries=images))+"\n")
            log.flush()
            elapsed=time.perf_counter()-started
            summary=dict(requested_hz=hz,elapsed_s=elapsed,completed=len(points),actual_hz=len(points)/elapsed,
                skipped_ticks=max(0,seconds*hz-len(points)),throttled_samples=sum(p[2] for p in points),
                first_hit_box=box,edge_events=len(hits),peak_frame_entries=max(p[4] for p in points),
                max_rss=max(p[3] for p in points),terminal=state["status"],source="controlled BGR via production CaptureService")
            report["runs"].append(summary)
            print(json.dumps(summary),flush=True)
    finally:
        vision.close()
        capture.close()
        (out/"rates.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf8")
