"""Run the installed engine with a fixed 800x600 frame and record long-term resources."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate import resources, save, sha
import numpy as np
import psutil
from backend.capture.ports import RawImage
from backend.capture.service import CaptureService
from backend.image.store import SessionImageStore
from backend.main import (AssetDataRoot, ConditionTreeEvaluator, SidecarArgs, _AssetTargetImageResolver,
                          build_catalog_services, build_default_vision_service)
from backend.script.controller import ExecutionController
from backend.script.lua_vm import LuaVm
from backend.targets.repository import CatalogChange
from backend.types.region import DisplayInfo, Rect


class FixedDisplay:
    def __init__(self, frame):
        self.frame = frame

    def list_displays(self):
        return [DisplayInfo("fixed", Rect(0,0,800,600),1,True)]

    def capture_bgr(self, x, y, width, height, cancel):
        cancel.throw_if_cancelled()
        assert (x,y,width,height) == (0,0,800,600)
        return self.frame


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("--soak-seconds", type=int, default=7200)
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args()
    args.output.mkdir(exist_ok=False)
    installed = resources()
    os.environ.update(ST_OCR_MODELS_DIR=str(installed / "weights"), PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
                      PADDLE_PDX_CACHE_HOME=str(args.output.resolve() / "cache"))
    rng = np.random.default_rng(74231)
    scene = rng.integers(0,150,(600,800,3),dtype=np.uint8)
    template = rng.integers(0,256,(100,100,3),dtype=np.uint8)
    scene[125:225,340:440] = template
    port = FixedDisplay(RawImage(800,600,scene.tobytes()))
    store = SessionImageStore("perf")
    sidecar = SidecarArgs("perf", data_dir=str(args.output / "data"))
    repo, targets, scripts = build_catalog_services(sidecar,store)
    ref = store.put(RawImage(100,100,template.tobytes()),"png")
    target = targets.create_target(dict(name="fixed",image_id=ref.image_id,anchor_condition_id="m",
        conditions={"op":"all","children":[{"id":"m","type":"template","params":{"threshold":.91}}]}))["id"]
    script = scripts.save_script("测量动作","assert(args.match.found); return args.match.sample_no",
        [dict(name="match",type="object",required=True,default=None,description="")])["id"]
    before = repo.read_snapshot()
    first = before.targets[0]
    stamp = first["created_at"]
    groups = tuple(dict(id=f"g{i}",name=f"Group {i}",revision=1,parent_group_id=None,depth=0,
                        created_at=stamp,updated_at=stamp) for i in range(1000))
    all_targets = (first,)+tuple(dict(first,id=f"t{i}",sample_no=i,name=f"Target {i}",group_id=f"g{i%1000}")
                                for i in range(2,10001))
    repo.commit(CatalogChange(targets=all_targets,groups=groups), before.generation)
    restarted = repo.reload()
    assert len(restarted.targets)==10000 and len(restarted.groups)==1000 and restarted.next_sample_no==10001
    resolver = _AssetTargetImageResolver(store,AssetDataRoot(sidecar.data_dir))
    vision = build_default_vision_service(sidecar,repository=repo,image_store=store,
        evaluator_factory=lambda reg:ConditionTreeEvaluator(registry=reg,target_image_resolver=resolver))
    capture = CaptureService(port,port,None,store)
    proc = psutil.Process()
    runs = [(5,3)] if args.smoke else [(1,600),(5,20),(10,20),(30,20),(1,args.soak_seconds)]
    failed = threading.Event()
    totals = {"ticks":0,"actions":0,"heap":0,"failures":0}
    original_close = LuaVm.close
    def observe_heap(vm):
        totals["heap"] = vm.memory_used()
        original_close(vm)
    def event(name,data):
        if name == "recognition_stats":
            totals["ticks"] += 1
        elif name == "target_detected":
            box = data["result"]["matches"][0]["bbox"]
            if (box["x"],box["y"],box["width"],box["height"]) != (340,125,100,100):
                failed.set()
            totals["actions"] += 1
        elif name == "script_state_changed" and data["status"] == "failed":
            totals["failures"] += 1
            failed.set()
    controller = ExecutionController(scripts,capture,vision,None,dict,event,None)
    summary = dict(machine=platform.platform(),cpu=platform.processor(),ram=psutil.virtual_memory().total,
                   package_manifest_sha256=sha(installed.parent / "package.json"), targets=10000,groups=1000,
                   frame_sha256=hashlib.sha256(scene.tobytes()).hexdigest(),
                   template_sha256=hashlib.sha256(template.tobytes()).hexdigest(),runs=[])
    save(args.output/"environment.json",summary)
    try:
        with patch.object(LuaVm,"close",observe_heap), (args.output/"resources.jsonl").open("x",encoding="utf-8") as log:
            for hz,seconds in runs:
                totals.update(ticks=0,actions=0,heap=0,failures=0)
                start = time.perf_counter()
                eid = controller.command("start_monitor",dict(target_id=target,display_id="fixed",frequency_hz=hz,
                    on_match="run_script",repeat=True,trigger="every_match",cooldown_ms=0,
                    action_script_id=script,wait_timeout_ms=0))["execution_id"]
                samples = []
                while not failed.is_set():
                    elapsed = time.perf_counter()-start
                    children = [proc,*proc.children(recursive=True)]
                    rss = sum(child.memory_info().rss for child in children if child.is_running())
                    row = dict(hz=hz,requested_seconds=seconds,elapsed_s=elapsed,tree_rss_bytes=rss,
                               processes=len(children),frame_bytes=store.bytes_used,**totals)
                    log.write(json.dumps(row)+"\n")
                    log.flush()
                    samples.append((elapsed,rss))
                    if elapsed >= seconds:
                        break
                    failed.wait(min(10,seconds-elapsed))
                controller.command("stop_script",dict(execution_id=eid))
                with controller.changed:
                    assert controller.changed.wait_for(lambda:eid not in controller.roots,5), "stop timeout"
                state = controller.command("get_script_state",dict(execution_id=eid))["state"]
                assert state["status"]=="cancelled" and not failed.is_set() and totals["heap"]>0,(state,totals)
                elapsed=time.perf_counter()-start
                item=dict(hz=hz,seconds=seconds,elapsed_s=elapsed,effective_hz=totals["ticks"]/elapsed,
                          skipped_slots=max(0,int(seconds*hz)-totals["ticks"]),**totals)
                if seconds>=7200:
                    early=statistics.median(value for t,value in samples if 300<=t<2100)
                    late=statistics.median(value for t,value in samples if t>=seconds-1800)
                    item["rss_growth"]=(late-early)/early
                summary["runs"].append(item)
                print(json.dumps(item),flush=True)
        save(args.output/"metrics.json",summary)
        return int(any(r.get("rss_growth",0)>.10 for r in summary["runs"]))
    finally:
        capture.close()
        vision.close()
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
