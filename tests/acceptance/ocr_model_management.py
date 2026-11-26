"""Exercise model changes through the real desktop ABI, model workers and saved Lua."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probes/windows"))
from verify_p1_runtime import Host, Window
from PIL import Image, ImageDraw, ImageFont
import psutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("weights", type=Path)
    parser.add_argument("library", type=Path)
    args = parser.parse_args()
    output, weights = args.output.resolve(), args.weights.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    table = json.loads((ROOT / "tests/acceptance/fixtures/ocr.json").read_bytes())
    os.environ.update(ST_OCR_MODELS_DIR=str(weights), PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
                      PADDLE_PDX_CACHE_HOME=str(output / "cache"))
    records = output / "observations.jsonl"

    def log(label, result):
        with records.open("a", encoding="utf-8") as stream:
            print(json.dumps({"step": label, "monotonic": time.monotonic(), "actual": result},
                             ensure_ascii=False), file=stream)
        return result

    def start():
        instance = Host(args.library, output)
        def picker(ptr, length, dest, capacity):
            if json.loads(ctypes.string_at(ptr, length))["op"] != "pick_image":
                return -1
            data = json.dumps({"data": str(filename)}).encode()
            ctypes.memmove(dest, data, len(data))
            return len(data)
        instance.window = Window(picker)
        assert instance.dll.st_host_attach_window(instance.ptr, instance.window) == 0
        instance.session = instance.call("start")["session_id"]
        return instance

    def finish(job, error=None):
        end = time.monotonic() + 85
        while time.monotonic() < end:
            row = host.request("get_job", job_id=job)
            if row["status"] not in ("pending", "running"):
                log("job " + job, row)
                if error:
                    assert row["status"] == "error" and row["error"]["code"] == error, row
                    return None
                assert row["status"] == "completed", row
                return row["result"]
            time.sleep(.05)
        raise TimeoutError(job)

    def command(name, **params):
        return host.request(name, **params)["job_id"]

    def preview(op, model, **extra):
        return finish(command("preview_ocr_model_change", operation=op, model_id=model, **extra))

    def apply(pre, error=None):
        return finish(command("apply_ocr_model_change", preview_id=pre["preview_id"],
                              expected_revision=pre["revision"]), error)

    def infer(lang, i=0, error=None):
        nonlocal filename
        group = table[lang]
        sample = group["samples"][i]
        text = sample["reference"]
        image = Image.new("RGB", (720, 140), "white")
        font = Path(os.environ["WINDIR"]) / "Fonts" / group["font"]
        assert hashlib.sha256(font.read_bytes()).hexdigest() == group["font_sha256"]
        ImageDraw.Draw(image).text((24, 40), text, fill="black",
                                   font=ImageFont.truetype(str(font), sample["font_size"]))
        assert hashlib.sha256(image.tobytes("raw", "BGR")).hexdigest() == sample["image_sha256"]
        filename = output / f"{lang}-{i}.png"
        if not filename.exists():
            image.save(filename)
        token = host.call("pick_image", session_id=host.session)["import_token"]
        image_id = host.request("import_image", import_token=token)["image"]["image_id"]
        job = command("recognize_text", model_id=group["model"], image_id=image_id)
        result = finish(job, error)
        if result is not None:
            actual = "".join(b["text"] for b in result["blocks"])
            log("OCR", dict(lang=lang, model=group["model"], expected=text, actual=actual))
            assert actual == text
        return image_id

    def snapshot():
        return log("list_ocr_models", host.request("list_ocr_models"))

    def states():
        return {p["model_id"]: p["state"] for p in snapshot()["pipelines"]}

    def blank():
        nonlocal filename
        filename = output / "blank.png"
        Image.new("RGB", (320, 90), "white").save(filename)
        token = host.call("pick_image", session_id=host.session)["import_token"]
        return host.request("import_image", import_token=token)["image"]["image_id"]

    filename = None
    generic, korean = table["zh"]["model"], table["ko"]["model"]
    host = start()
    try:
        assert set(states().values()) == {"installed"}
        infer("zh")
        assert states() == {generic: "ready", korean: "installed"}
        before = {child.pid for child in psutil.Process().children(recursive=True)}
        previous = infer("ko")
        assert set(states().values()) == {"ready"}
        owned = [child for child in psutil.Process().children(recursive=True) if child.pid not in before]
        assert len(owned) == 1, owned
        korean_worker = owned[0]
        image_id = blank()
        korean_worker.suspend()
        try:
            start_time = time.monotonic()
            finish(command("recognize_text", model_id=korean, image_id=image_id), "RECOGNITION_TIMEOUT")
            elapsed = time.monotonic() - start_time
            log("suspended owned OCR worker", {"pid": korean_worker.pid, "elapsed_seconds": elapsed})
            assert 4.8 <= elapsed < 12
        finally:
            if korean_worker.is_running():
                try:
                    korean_worker.resume()
                except psutil.NoSuchProcess:
                    pass
        assert finish(command("recognize_text", model_id=korean, image_id=image_id))["blocks"] == []
        assert states()[korean] == "ready"
        rm = preview("remove", korean)
        stale = preview("remove", generic)
        after = apply(rm)
        assert after["revision"] == rm["revision"] + 1
        apply(stale, "CONFLICT")
        finish(command("recognize_text", model_id=korean, image_id=previous), "MODEL_UNAVAILABLE")
        infer("zh", 1)
        display = host.request("get_display_list")["displays"][0]["id"]
        run = host.request("execute_script", script_id=host.save("return 38"), display_id=display)
        assert log("pure Lua", host.until(run["execution_id"], "completed"))["result"] == 38
        current = snapshot()["revision"]
        damaged = output / "broken-media"
        damaged.mkdir()
        apply(preview("import", korean, directory=str(damaged)), "DATA_CORRUPT")
        assert snapshot()["revision"] == current
        host.close()
        host = None
        host = start()
        assert states() == {generic: "installed", korean: "missing"}
        infer("zh", 1)
        apply(preview("import", korean, directory=str(weights)))
        assert states()[korean] == "ready"
        infer("ko")
        revision = snapshot()["revision"]
        assert apply(preview("import", korean, directory=str(weights)))["revision"] == revision
        assert set(log("final languages", [m["model_ids"][0] for m in host.request("hello")["model_manifests"]])) == {generic, korean}
        log("completed", True)
    finally:
        if host is not None:
            host.close()


if __name__ == "__main__":
    main()
