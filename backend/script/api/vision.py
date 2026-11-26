"""Screen, image and target operations on the current root's recognition area."""
from contextlib import contextmanager
from dataclasses import asdict

from ...capture.geometry import rect_within
from ...capture.ports import RawImage
from ...storage.errors import StorageError
from ...types.conditions import _validate_color_params
from ...types.region import validate_region
from ...vision.textmatch import DEFAULT_MIN_CONFIDENCE, MATCH_MODES, match_text
from ..hostapi import EnvNative


def fail(field, message):
    raise StorageError("INVALID_PARAMS", message, field=field)


class Resample(Exception):
    """The root was paused while a read-only operation was in flight."""


class ScreenApi:
    def __init__(self, root):
        self.root = root
        self.capture = root.controller.capture
        self.vision = root.controller.vision
        self.capture_options = ({"window_mode": "background"}
                                if getattr(root.config, "window_mode", "foreground") == "background" else {})

    def region(self, value=None):
        if self.capture is None:
            raise StorageError("CAPABILITY_DENIED", "捕获服务不可用")
        config = self.root.config
        area = self.capture.run_region(config.display_id, config.binding_id, **self.capture_options)
        for narrower in (config.region, validate_region(value) if value is not None else None):
            if narrower is None:
                continue
            if (area.space, area.display_id, area.binding_id) != (
                    narrower.space, narrower.display_id, narrower.binding_id) or not rect_within(narrower, area):
                raise StorageError("INVALID_REGION", "区域超出当前识别范围或空间不符", field="region")
            area = narrower
        return area

    @contextmanager
    def frame(self, region=None):
        frame = self.capture_frame(region)
        try:
            yield frame
        finally:
            self.capture.release_image(frame.image_ref.image_id)

    def capture_frame(self, region=None):
        generation = self.root.pause_generation
        area = self.region(region)
        frame = self.capture.capture(area, self.root.gate, **self.capture_options)
        if generation != self.root.pause_generation:
            self.capture.release_image(frame.image_ref.image_id)
            raise Resample()
        return frame

    def capture_screen(self, region=None):
        frame = self.capture_frame(region)
        ref = asdict(frame.image_ref)
        self.root.images[ref["image_id"]] = ref
        return ref

    def owned(self, image):
        image_id = image.get("image_id") if isinstance(image, dict) else None
        if not isinstance(image_id, str) or self.root.images.get(image_id) != image:
            raise StorageError("IMAGE_EXPIRED", "图像不属于当前执行或引用已失效")
        return image_id

    def release_image(self, image):
        key = image.get("image_id") if isinstance(image, dict) else None
        if isinstance(key, str) and self.root.released_images.get(key) == image:
            return ()
        image_id = self.owned(image)
        self.capture.release_image(image_id)
        self.root.released_images[image_id] = self.root.images.pop(image_id)
        if len(self.root.released_images) > self.capture.image_store.DEFAULT_RELEASED_CAPACITY:
            self.root.released_images.popitem(last=False)
        return ()

    def infer(self, command, image_id, **params):
        if self.vision is None:
            raise StorageError("CAPABILITY_DENIED", "识别服务不可用")
        generation = self.root.pause_generation
        result = self.vision.run_sync(command, dict(params, image_id=image_id),
                                      self.root.gate, catalog=self.root.catalog)
        if generation != self.root.pause_generation:
            raise Resample()
        return result

    def target(self, target_id, image_id):
        if not isinstance(target_id, str) or not target_id:
            fail("target_id", "目标 ID 必须为非空字符串")
        self.root.reference(target_id)
        return self.infer("evaluate_target_conditions", image_id, target_id=target_id)

    def find_target(self, target_id, region=None):
        with self.frame(region) as frame:
            result = self.target(target_id, frame.image_ref.image_id)
            return result["found"], result["matches"]

    def poll(self, mode, ids, timeout, interval=None):
        from . import number
        timeout = number(timeout, "timeout", minimum=0, default=30)
        if interval is not None:
            interval = number(interval, "interval", minimum=0)
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids)):
            fail("target_ids", "需要 1 到 100 个不同的目标 ID")
        start = self.root.active_time()
        while True:
            self.root.gate.checkpoint()
            matches = {}
            with self.frame() as frame:
                for item in ids:
                    result = self.target(item, frame.image_ref.image_id)
                    if result["found"]:
                        hit = result["matches"][0]
                        if mode == "one":
                            return True, hit
                        if mode == "any":
                            return item, hit
                        matches[item] = hit
                    elif mode == "gone":
                        return True
            if mode == "all" and len(matches) == len(ids):
                return True, matches
            delay = interval if interval is not None else 1 / self.root.frequency
            if timeout:
                remaining = timeout - (self.root.active_time() - start)
                if remaining <= 0:
                    nil = EnvNative(None)
                    return {"one": (False, nil), "any": (nil, nil), "all": (False, {}), "gone": False}[mode]
                delay = min(delay, remaining)
            self.root.wait(delay)

    def get_pixel_color(self, x, y):
        from . import integer
        area = self.region()
        return self.capture.capture_point(integer(x, "x"), integer(y, "y"), area, self.root.gate, **self.capture_options)

    @contextmanager
    def ocr_image(self, image, region):
        if image is None:
            with self.frame(region) as frame:
                yield frame.image_ref.image_id, frame.space, frame.origin.x, frame.origin.y, frame.geometry_revision
            return
        image_id = self.owned(image)
        if region is None:
            yield image_id, "image_pixel", 0, 0, None
            return
        roi = validate_region(region)
        stored = self.capture.image_store.get(image_id)
        if (roi.space != "image_pixel" or roi.image_id != image_id or roi.x < 0 or roi.y < 0
                or roi.x + roi.width > stored.width or roi.y + roi.height > stored.height):
            raise StorageError("INVALID_REGION", "OCR 子区超出图像边界", field="region")
        pixels = memoryview(stored.data)
        rows = []
        for y in range(roi.y, roi.y + roi.height):
            start = (y * stored.width + roi.x) * 3
            rows.append(pixels[start:start + roi.width * 3])
        ref = self.capture.image_store.put(RawImage(roi.width, roi.height, b"".join(rows)), "png")
        try:
            yield ref.image_id, "image_pixel", roi.x, roi.y, None
        finally:
            self.capture.release_image(ref.image_id)

    def ocr(self, image=None, region=None, model_id=None):
        if model_id is None:
            model_id = self.root.settings.get("recognition", {}).get("ocr_model_id")
        if model_id is not None and (not isinstance(model_id, str) or not model_id):
            fail("model_id", "OCR 模型 ID 无效")
        with self.ocr_image(image, region) as (image_id, space, x, y, revision):
            blocks = self.infer("recognize_text", image_id, model_id=model_id)["blocks"]
            if getattr(self.root, "export_enabled", False):
                binding = self.root.config.binding_id
                if binding and revision is not None:
                    snapshot = self.root.controller.input.window_snapshot(binding)
                    if snapshot["geometry_revision"] != revision:
                        raise Resample()
                self.root.export_blocks(blocks)
            return [dict(block, space=space, polygon=[{"x": p["x"] + x, "y": p["y"] + y}
                         for p in block["polygon"]]) for block in blocks]

    def find_text(self, text, match_mode=None, region=None):
        mode = "exact" if match_mode is None else match_mode
        if not isinstance(text, str) or not text or mode not in MATCH_MODES:
            fail("text", "文本或匹配模式无效")
        minimum = self.root.settings.get("recognition", {}).get(
            "ocr_min_confidence", DEFAULT_MIN_CONFIDENCE)
        blocks = [b for b in self.ocr(None, region) if b["confidence"] >= minimum
                  and match_text(b["text"], text, mode=mode) is not None]
        return bool(blocks), blocks

    def find_color(self, params, region=None):
        if not isinstance(params, dict):
            fail("params", "颜色参数必须为对象")
        _validate_color_params(params)
        with self.frame(region) as frame:
            values = self.infer("detect_color", frame.image_ref.image_id, params=params)["matches"]
            matches = []
            for item in values:
                candidate = dict(item, space=frame.space)
                for field in ("bbox", "center"):
                    point = item[field]
                    candidate[field] = dict(point, x=point["x"] + frame.origin.x,
                                            y=point["y"] + frame.origin.y)
                matches.append(candidate)
            return bool(matches), matches
