"""Seed an isolated local UI fixture through the production catalog repository."""
import sys, random
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from backend.main import build_catalog_services, SidecarArgs
from backend.image.store import SessionImageStore
from backend.capture.ports import RawImage
root=Path(sys.argv[1])
root.mkdir(exist_ok=False)
images=SessionImageStore("fixture")
_, targets, scripts=build_catalog_services(SidecarArgs("fixture", data_dir=str(root)), images)
image=images.put(RawImage(24,24,random.Random(349).randbytes(24*24*3)), "png")
targets.create_target(dict(name="P2 随机模板", image_id=image.image_id, anchor_condition_id="anchor",
    conditions=dict(op="all", children=[dict(id="anchor", type="template", params=dict(threshold=1))])))
scripts.save_script("P2 调试", 'local n=2\nlocal t={n=n}\nprint("可见日志")\nreturn t.n', [])
