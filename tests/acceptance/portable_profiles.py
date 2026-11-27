"""Measure standard, single-pipeline, empty and damaged model states in a disposable release copy."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont
import psutil


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def exercise(repo, archive, raw):
    sys.path.insert(0, str(repo / "tools/packaging"))
    from portable_layout import MARKER, host_path, metadata_dir
    sys.path.insert(0, str(repo / "tools/probes/windows"))
    from verify_p1_runtime import Host, Window

    app = Path(sys.executable).resolve().parent
    assert app.is_relative_to(repo / "dist/automation-work") and sys.executable.lower().endswith("portable-abi-test.exe")
    marker = json.loads((metadata_dir(app) / MARKER).read_bytes())
    weights = app / "resources/weights"
    raw.mkdir(parents=True, exist_ok=False)
    table = json.loads((repo / "tests/acceptance/fixtures/ocr.json").read_bytes())
    general, korean = table["zh"]["model"], table["ko"]["model"]
    metadata = app / "data/models"
    start_size = sum(p.stat().st_size for p in metadata.rglob("*") if p.is_file())
    stop = threading.Event()
    resource = {"peak_tree_rss": 0, "peak_model_library_bytes": start_size, "runtime_modules": set(), "workers": {}}
    me = psutil.Process()
    def monitor():
        while not stop.is_set():
            processes = [me, *me.children(recursive=True)]
            rss = 0
            for p in processes:
                try:
                    rss += p.memory_info().rss
                    resource["workers"][str(p.pid)] = p.exe()
                    if p.name().lower() in ("python.exe", "portable-abi-test.exe"):
                        for module in p.memory_maps():
                            if module.path.lower().endswith((".dll", ".pyd")):
                                resource["runtime_modules"].add(module.path)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            size = 0
            for p in metadata.rglob("*"):
                try:
                    if p.is_file(): size += p.stat().st_size
                except FileNotFoundError:
                    continue
            resource["peak_tree_rss"] = max(rss, resource["peak_tree_rss"])
            resource["peak_model_library_bytes"] = max(size, resource["peak_model_library_bytes"])
            stop.wait(.25)
    thread = threading.Thread(target=monitor, daemon=True)
    records = []
    def record(case, actual):
        records.append(dict(case=case, time=time.monotonic(), actual=actual))
        with (raw / "observations.jsonl").open("a", encoding="utf8") as out:
            print(json.dumps(records[-1], ensure_ascii=False), file=out)
        return actual
    image = None
    def picker(ptr, length, dest, capacity):
        data = json.dumps({"data": str(image)}).encode()
        if json.loads(ctypes.string_at(ptr, length))["op"] != "pick_image" or len(data) > capacity: return -1
        ctypes.memmove(dest, data, len(data))
        return len(data)
    def launch():
        host = Host(host_path(app), raw)
        host.window = Window(picker)
        assert host.dll.st_host_attach_window(host.ptr, host.window) == 0
        host.session = host.call("start")["session_id"]
        return host
    def job(command, fault=None, **params):
        key = host.request(command, **params)["job_id"]
        end = time.monotonic() + 100
        while time.monotonic() < end:
            row = host.request("get_job", job_id=key)
            if row["status"] not in ("pending", "running"):
                record(command, row)
                if fault:
                    assert row["status"] == "error" and row["error"]["code"] == fault, row
                    return
                assert row["status"] == "completed", row
                return row["result"]
            time.sleep(.06)
        raise TimeoutError((command, key))
    def change(action, model):
        params = {"directory": str(weights)} if action == "import" else {}
        preview = job("preview_ocr_model_change", operation=action, model_id=model, **params)
        job("apply_ocr_model_change", preview_id=preview["preview_id"], expected_revision=preview["revision"])
    def profile(label, want):
        state = record(label, host.request("list_ocr_models"))
        actual = {p["model_id"]: p["state"] for p in state["pipelines"]}
        for k, accepted in want.items(): assert actual[k] in accepted, (label, actual)
        hello = host.request("hello")
        offered = {row["model_ids"][0] for row in hello["model_manifests"]}
        assert offered == {m for m, s in actual.items() if s in ("installed","ready")}, (actual, offered)
    def infer(lang, fault=None):
        nonlocal image
        group = table[lang]
        sample = group["samples"][0]
        font = Path(os.environ["WINDIR"]) / "Fonts" / group["font"]
        assert digest(font) == group["font_sha256"]
        im = Image.new("RGB", (720, 140), "white")
        ImageDraw.Draw(im).text((24,40), sample["reference"], fill="black", font=ImageFont.truetype(str(font),sample["font_size"]))
        assert hashlib.sha256(im.tobytes("raw","BGR")).hexdigest() == sample["image_sha256"]
        image = raw / (lang+".png")
        im.save(image)
        ticket = host.call("pick_image", session_id=host.session)["import_token"]
        asset = host.request("import_image", import_token=ticket)["image"]["image_id"]
        result = job("recognize_text", fault, image_id=asset, model_id=group["model"])
        if fault is None:
            observed = record("OCR " + lang, "".join(b["text"] for b in result["blocks"]))
            assert observed == sample["reference"], (observed, sample)
    def lua():
        display = next(d["id"] for d in host.request("get_display_list")["displays"] if d["primary"])
        eid = host.request("execute_script", script_id=host.save("return 39"), display_id=display)["execution_id"]
        assert record("Lua after removal", host.until(eid, "completed"))["result"] == 39

    available = ("ready", "installed")
    host = None
    damaged = weights / korean / "inference.yml"
    saved = damaged.with_name("inference.yml.profile-test")
    assert damaged.is_file() and not saved.exists()
    result = {"archive": str(archive), "sha256": digest(archive), "source": marker["source_sha256"], "model_bytes_before": start_size}
    thread.start()
    try:
        damaged.rename(saved)
        try:
            host = launch()
            profile("partial factory medium", {general:available, korean:("damaged",)})
            infer("zh")
            lua()
        finally:
            if host is not None: host.close(); host = None
            saved.rename(damaged)
        host = launch()
        profile("complete", {general:available,korean:available})
        for lang in ("zh","en","ja","ko"): infer(lang)
        change("remove", korean)
        profile("general only", {general:available, korean:("missing",)})
        infer("zh")
        infer("ko","MODEL_UNAVAILABLE")
        change("remove", general)
        profile("empty", {general:("missing",),korean:("missing",)})
        lua()
        host.close()
        host = launch()
        profile("empty after restart", {general:("missing",),korean:("missing",)})
        change("import",korean)
        profile("Korean only", {general:("missing",),korean:available})
        infer("ko")
        change("import",general)
        infer("ja")
        profile("restored complete", {general:available,korean:available})
        result["passed"] = True
    finally:
        if host is not None: host.close()
        stop.set()
        thread.join()
        resource["runtime_modules"] = sorted(resource["runtime_modules"])
        result.update(resource, sampling_interval_seconds=.25, records=len(records))
        (raw/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    for name in ("repo", "archive", "raw"): args.add_argument(name, type=Path)
    p = args.parse_args()
    exercise(p.repo.resolve(), p.archive.resolve(), p.raw.resolve())
