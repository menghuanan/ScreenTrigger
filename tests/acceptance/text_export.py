"""Exercise real OCR, installed host ABI, script and continuous export against an owned window."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
import unicodedata

from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageWin

ROOT = Path(__file__).resolve().parents[2]
if not sys.flags.isolated:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/probes/windows"))
from backend.vision.textmatch import levenshtein
from common import MiniWindow, gdi32, set_per_monitor_v2, user32
from verify_p1_runtime import Host, Window, wait

X, Y, WIDTH, HEIGHT = 100, 150, 720, 140
GENERAL = "PP-OCRv6_medium_rec"


def single_line_snapshot(blocks):
    """Expected UTF-8 bytes for this fixture's single horizontal line."""
    words = sorted((b for b in blocks if b["text"]), key=lambda b: (
        min(p["x"] for p in b["polygon"]), min(p["y"] for p in b["polygon"])))
    line = ""
    for block in words:
        word = block["text"]
        if line and not line[-1].isspace() and not word[0].isspace():
            line += " "
        line += word
    line = line.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if line and not line.endswith("\r\n"):
        line += "\r\n"
    return line.encode("utf-8")


def fixture(pipe, directory):
    set_per_monitor_v2()
    win = MiniWindow("ScreenTrigger · OCR export fixture", X, Y, WIDTH, HEIGHT, popup=True)
    user32.GetDC.argtypes, user32.GetDC.restype = [wintypes.HWND], wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.SetWindowPos(win.hwnd, -1, X, Y, WIDTH, HEIGHT, 0x10)
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    dib = ImageWin.Dib(image)
    try:
        pipe.send(win.hwnd)
        while True:
            win.pump(12)
            new = pipe.poll()
            if new:
                spec = pipe.recv()
                if spec is None:
                    break
                image = Image.new("RGB", (WIDTH, HEIGHT), "white")
                font = ImageFont.truetype(str(Path(os.environ["WINDIR"]) / "Fonts" / spec["font"]), spec["size"])
                ImageDraw.Draw(image).text((24, 24), spec["text"], fill="black", font=font)
                image.save(directory / (spec["sample"] + ".png"))
                dib = ImageWin.Dib(image)
            dc = user32.GetDC(win.hwnd)
            try:
                dib.draw(ImageWin.HDC(dc), (0, 0, WIDTH, HEIGHT))
                gdi32.GdiFlush()
            finally:
                user32.ReleaseDC(win.hwnd, dc)
            if new:
                wait(lambda: ImageGrab.grab(bbox=(X,Y,X+WIDTH,Y+HEIGHT)).convert("RGB").tobytes() == image.tobytes(),
                     "actual fixture pixels visible", 3)
                pipe.send(hashlib.sha256(image.tobytes()).hexdigest())
    finally:
        win.destroy()


def run(dll, directory):
    directory.mkdir(parents=True, exist_ok=False)
    samples = json.loads((ROOT / "tests/acceptance/fixtures/ocr.json").read_bytes())
    started = time.time()
    results = {"status": "FAIL", "argv": sys.argv, "model_dir": os.environ.get("ST_OCR_MODELS_DIR"),
               "dll_sha256": hashlib.sha256(dll.read_bytes()).hexdigest(), "observations": []}
    pipe, other = multiprocessing.Pipe()
    child = multiprocessing.Process(target=fixture, args=(other, directory))
    child.start()
    host = None
    destination = None

    def paint(name, text, *, font="arial.ttf", size=72):
        pipe.send(dict(sample=name, text=text, font=font, size=size))
        assert pipe.poll(5), "owned window not ready"
        return pipe.recv()

    def pick_callback(addr, length, output, capacity):
        request = json.loads(ctypes.string_at(addr, length))
        if request.get("op") != "pick_text_output":
            return -1
        encoded = json.dumps({"data": str(destination) if destination else None}).encode()
        if len(encoded) > capacity:
            return -1
        ctypes.memmove(output, encoded, len(encoded))
        return len(encoded)

    def pick(path, mode):
        nonlocal destination
        destination = path
        return host.call("pick_text_output", session_id=host.session, mode=mode)["output_token"]

    def finished(execution):
        def query():
            state = host.state(execution)
            if state["status"] == "failed":
                raise AssertionError(state)
            return state if state["status"] == "completed" else None
        return wait(query, "completed OCR script", 30)

    def inspect(data):
        results["observations"].append(data)
        (directory / "observations.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    def exports(execution):
        return [e["payload"] for e in host.events if e.get("name") == "text_exported"
                and e["payload"]["execution_id"] == execution]

    try:
        assert pipe.poll(8)
        hwnd = pipe.recv()
        host = Host(dll, directory)
        host.window = Window(pick_callback)
        assert host.dll.st_host_attach_window(host.ptr, host.window) == 0
        host.session = host.call("start")["session_id"]
        hello = host.request("hello")
        inspect(dict(hello=hello, hwnd=hwnd))
        assert next(c["state"] for c in hello["capabilities"] if c["id"] == "ocr") in ("available", "pending")
        displays = host.request("get_display_list")["displays"]
        display = next(d["id"] for d in displays if d["primary"])
        region = dict(space="screen_physical", display_id=display, x=X, y=Y, width=WIDTH, height=HEIGHT)
        scope = dict(display_id=display, region=region)
        assert pick(None, "append") is None
        exported = directory / "four-languages.txt"
        combined = b""
        empty_samples = []
        for lang in ("zh", "en", "ja", "ko"):
            group = samples[lang]
            sample = group["samples"][0]
            text = sample["reference"]
            pixels = paint(lang, text, font=group["font"], size=sample["font_size"])
            token = pick(exported, "append")
            assert exported.exists() == bool(combined)
            src = 'local blocks=ocr(nil,nil,"' + group["model"] + '"); return blocks'
            execution = host.request("execute_script", script_id=host.save(src), **scope, output_token=token)["execution_id"]
            state = finished(execution)
            actual = exported.read_bytes() if exported.exists() else b""
            blocks = state["result"]
            snapshot = single_line_snapshot(blocks)
            combined += snapshot
            reference = unicodedata.normalize("NFC", text)
            recognized = "".join(b["text"] for b in blocks)
            errors = levenshtein(reference, unicodedata.normalize("NFC", recognized))
            writes = wait(lambda: exports(execution), "script export event", 5) if snapshot else exports(execution)
            inspect(dict(lang=lang, pixels=pixels, state=state,
                         ocr=dict(reference=text, recognized=recognized, errors=errors,
                                  characters=len(reference), cer=errors / len(reference)),
                         export=dict(expected_hex=combined.hex(), actual_hex=actual.hex(),
                                     bytes_match=actual == combined, events=writes)))
            assert actual == combined and exported.exists() == bool(combined), (lang, actual, combined)
            if snapshot:
                assert len(writes) == 1 and writes[0]["bytes_written"] == len(snapshot), writes
                assert writes[0]["block_confidences"] == [b["confidence"] for b in blocks if b["text"]], writes
            else:
                assert not writes, writes
                empty_samples.append(lang)
        config = host.request("get_config")["config"]
        host.request("update_config", expected_revision=config["revision"], patch={"recognition":{"ocr_model_id":GENERAL}})
        paint("A", "A")
        shot = host.request("capture_region", region=region)["image"]
        # Always-missing target keeps monitor ticking. Sample OCR uses this exact same frame.
        draft = dict(name="Monitor no-match", image_id=shot["image_id"], anchor_condition_id="c",
                     conditions={"op":"all","children":[{"type":"color","id":"c","params":{
                         "color_space":"rgb","color":{"r":255,"g":0,"b":0},"min_pixel_count":10}}]})
        target = host.request("create_target", draft=draft)["target"]["id"]
        for mode in ("append", "overwrite"):
            path = directory / (mode + ".txt")
            token = pick(path, mode)
            assert not path.exists()
            execution = host.request("start_monitor", target_id=target, **scope, repeat=False,
                                     frequency_hz=.5, wait_timeout_ms=0, output_token=token)["execution_id"]
            expected = b""
            for i, letter in enumerate("AABA", 1):
                def events():
                    state = host.state(execution)
                    if state["status"] == "failed":
                        raise AssertionError(state)
                    items = exports(execution)
                    return items if len(items) >= i else None
                writes = wait(events, f"{mode} export event {i}", 12)
                expected = (expected if mode == "append" else b"") + (letter+"\r\n").encode()
                actual = path.read_bytes()
                inspect(dict(mode=mode, index=i, execution=execution, writes=writes,
                             expected_hex=expected.hex(), actual_hex=actual.hex()))
                assert len(writes) == i and len({e["action_id"] for e in writes}) == i, writes
                assert writes[-1]["bytes_written"] == len((letter + "\r\n").encode())
                assert writes[-1]["block_confidences"]
                assert actual == expected
                if i < 4:
                    paint("AABA"[i], "AABA"[i])
            host.request("stop_script", execution_id=execution)
            time.sleep(2.25)
            assert path.read_bytes() == expected and len(exports(execution)) == 4
        assert not empty_samples, ("OCR returned no text for these nonempty samples", empty_samples)
        results["status"] = "PASS"
    finally:
        try:
            if host:
                host.close()
        finally:
            if child.is_alive():
                pipe.send(None)
            child.join(4)
            if child.is_alive():
                child.terminate()
                child.join()
            results["elapsed"] = time.time() - started
            (directory / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    options = argparse.ArgumentParser()
    options.add_argument("dll", type=Path)
    options.add_argument("directory", type=Path)
    args = options.parse_args()
    run(args.dll.resolve(), args.directory.resolve())
