"""Exercise saved settings against two local model libraries and a frozen OCR frame."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate import ROOT, save, sha
if not sys.flags.isolated:
    sys.path.insert(0, str(ROOT))
from ocr_quality import render_sample
from backend.capture.ports import RawImage
from backend.capture.service import CaptureService
from backend.config.service import ConfigError, ConfigService
from backend.image.png_decode import decode_png_to_rgb
from backend.image.store import SessionImageStore
from backend.main import SidecarArgs, build_catalog_services
from backend.script.api.vision import ScreenApi
from backend.types.region import Rect, DisplayInfo
from backend.vision.cancel import NullCancelToken
from backend.vision.registry import build_default_vision_service
from backend.vision.types import RecognitionError


class FrozenFrame:
    name = "frozen"

    def __init__(self, raw):
        self.raw = raw

    def list_displays(self):
        return [DisplayInfo("fixture", Rect(0, 0, self.raw.width, self.raw.height), 1., True)]

    def capture_bgr(self, x, y, width, height, cancel):
        assert (x, y, width, height) == (0, 0, self.raw.width, self.raw.height)
        return self.raw


def copy_weights(source, target):
    def link_or_copy(path, dest):
        try:
            os.link(path, dest)
        except OSError:
            shutil.copyfile(path, dest)
    shutil.copytree(source, target, copy_function=link_or_copy)


def verify(data, weights):
    group = json.loads((ROOT / "tests/acceptance/fixtures/ocr.json").read_bytes())["en"]
    sample = group["samples"][0]
    font = Path(os.environ["WINDIR"]) / "Fonts" / group["font"]
    assert sha(font) == group["font_sha256"]
    raw = RawImage(720, 140, render_sample(sample, font))
    assert hashlib.sha256(raw.data).hexdigest() == sample["image_sha256"]
    source = FrozenFrame(raw)
    config = ConfigService(str(data))
    config.load()
    def change(patch):
        return config.update_config(patch, config.get_config()["revision"])

    records = []
    for directory in ("weights-one", "weights-two", "missing"):
        if directory != "missing":
            copy_weights(weights, data / directory)
        change({"recognition": {"model_directory": directory, "ocr_model_id": group["model"],
                                "ocr_min_confidence": 0.}})
        config = ConfigService(str(data))
        settings = config.load()
        args = SidecarArgs("settings-" + directory, data_dir=str(data))
        images = SessionImageStore(args.session_id, compression_level=config.png_compression_level)
        repo, targets, _ = build_catalog_services(args, images)
        capture = CaptureService(source, source, None, images)
        vision = build_default_vision_service(args, repository=repo, image_store=images)
        try:
            state, reason = vision.capability_state("ocr")
            print(directory, state, reason, flush=True)
            if directory == "missing":
                assert state == "unavailable" and "MODEL_UNAVAILABLE" in reason
                assert vision.capability_state("template_match")[0] == "available"
                records.append(dict(directory=directory, state=state, reason=reason))
                continue
            assert state == "available", reason
            provider = vision.registry.provider_for("ocr")
            assert Path(provider.library_root) == data / directory
            assert Path(provider.models_dir) == data / directory
            models = {key: sha(data / directory / key / "manifest.json")
                      for manifest in vision.installed_model_manifests() for key in manifest.model_ids}
            assert group["model"] in models
            root = SimpleNamespace(controller=SimpleNamespace(capture=capture, vision=vision),
                                   config=SimpleNamespace(display_id="fixture", binding_id=None, region=None),
                                   settings=settings, gate=NullCancelToken(), catalog=None, pause_generation=0)
            screen = ScreenApi(root)
            found, blocks = screen.find_text(sample["reference"])
            assert found and 0 < blocks[0]["confidence"] < 1, blocks
            new_settings = change({"recognition": {"ocr_min_confidence": 1.}})
            assert screen.find_text(sample["reference"])[0]  # old execution's snapshot
            new_root = SimpleNamespace(**dict(vars(root), settings=new_settings))
            assert not ScreenApi(new_root).find_text(sample["reference"])[0]
            try:
                screen.ocr(model_id="not-installed")
                raise AssertionError("Unknown model returned a result")
            except RecognitionError as exc:
                assert exc.code == "MODEL_UNAVAILABLE"
            ref = images.put(raw, "png")
            sizes = {}
            for level in (0, 9):
                change({"capture": {"png_compression_level": level}})
                preview = base64.b64decode(images.preview(ref.image_id, 1024)[1])
                draft = dict(name=f"{directory}-{level}", image_id=ref.image_id,
                             anchor_condition_id="c", conditions={"op": "all", "children": [
                                 {"id": "c", "type": "template", "params": {"threshold": .8}}]})
                target = targets.create_target(draft)
                asset = (data / target["image"]["path"]).read_bytes()
                for png in (preview, asset):
                    width, height, rgb = decode_png_to_rgb(png)
                    assert (width, height, rgb) == (raw.width, raw.height, raw.data)
                sizes[level] = (len(preview), len(asset))
            assert all(lo > hi for lo, hi in zip(sizes[0], sizes[9]))
            records.append(dict(directory=directory, models=models, confidence=blocks[0]["confidence"],
                                thresholds=[0, 1], matches=[True, False], original_execution_matched=True,
                                png_bytes=sizes, source_pixels_sha256=sample["image_sha256"]))
        finally:
            vision.close()
            repo.close()
    try:
        change({"recognition": {"model_directory": "../outside"}})
        raise AssertionError("Model path escape accepted")
    except ConfigError as exc:
        assert exc.code == "INVALID_PARAMS" and exc.details["field"] == "recognition.model_directory"
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("weights", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    options.output.mkdir(exist_ok=False, parents=True)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(options.output.resolve() / "cache")
    with tempfile.TemporaryDirectory(dir=options.output.resolve()) as folder:
        results = verify(Path(folder), options.weights.resolve(strict=True))
    save(options.output / "settings.json", dict(interpreter=sys.executable, runs=results))
