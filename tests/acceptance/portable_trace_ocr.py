"""Time actual repeated recognition of the same frozen image through the packaged Rust ABI."""
import ctypes
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import psutil
from PIL import Image

root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/"tools/packaging"))
from portable_layout import host_path
sys.path.insert(0,str(root/"tools/probes/windows"))
from verify_p1_runtime import Host,Window
app=Path(sys.executable).resolve().parent
out=Path(sys.argv[1]).resolve()
out.mkdir(parents=True,exist_ok=False)
data=Path(sys.argv[2]).resolve()
table=json.loads((root/"tests/acceptance/fixtures/ocr.json").read_bytes())["zh"]
with Image.open(data) as image:
    assert hashlib.sha256(image.convert("RGB").tobytes("raw","BGR")).hexdigest()==table["samples"][0]["image_sha256"]
def picker(s,n,dest,cap):
    if json.loads(ctypes.string_at(s,n))["op"]!="pick_image":return -1
    payload=json.dumps({"data":str(data)}).encode()
    assert len(payload)<=cap
    ctypes.memmove(dest,payload,len(payload));return len(payload)
process=psutil.Process(); stop=threading.Event(); peak=[0]
def rss():
    total=0;pids=[]
    for p in [process,*process.children(recursive=True)]:
        try:total+=p.memory_info().rss;pids.append(p.pid)
        except (psutil.NoSuchProcess,psutil.AccessDenied):pass
    peak[0]=max(peak[0],total)
    return dict(bytes=total,pids=pids)
def sample():
    while not stop.is_set():
        rss();stop.wait(.05)
t=threading.Thread(target=sample,daemon=True);t.start()
h=Host(host_path(app),out)
rows=[]
try:
    h.window=Window(picker);assert h.dll.st_host_attach_window(h.ptr,h.window)==0
    h.session=h.call("start")["session_id"]
    before=h.request("list_ocr_models")
    assert next(p for p in before["pipelines"] if p["model_id"]==table["model"])["state"]=="installed"
    ticket=h.call("pick_image",session_id=h.session)["import_token"]
    img=h.request("import_image",import_token=ticket)["image"]["image_id"]
    for i in range(6):
        begin=time.perf_counter_ns()
        jid=h.request("recognize_text",model_id=table["model"],image_id=img)["job_id"]
        while True:
            result=h.request("get_job",job_id=jid)
            if result["status"] not in ("pending","running"):break
            if time.perf_counter_ns()-begin>90_000_000_000:raise TimeoutError(jid)
            time.sleep(.015)
        end=time.perf_counter_ns()
        actual="".join(b["text"] for b in (result.get("result") or {}).get("blocks",[]))
        row=dict(iteration=i,job=jid,status=result["status"],begin_ns=begin,end_ns=end,
            elapsed_ms=(end-begin)/1e6,actual=actual,expected=table["samples"][0]["reference"],rss=rss())
        rows.append(row)
        assert result["status"]=="completed" and actual==row["expected"],row
finally:
    h.close();stop.set();t.join()
    (out/"fixed-ocr.json").write_text(json.dumps(dict(image=str(data),model=table["model"],
        pixel_sha256=table["samples"][0]["image_sha256"],rows=rows,peak_tree_rss_bytes=peak[0],
        sample_period_seconds=.05),ensure_ascii=False,indent=2),encoding="utf8")
