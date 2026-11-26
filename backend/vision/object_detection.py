"""Independent v1 adapter contract for decoded detections. No inference factory is installed."""
from dataclasses import asdict, dataclass
from math import ceil, floor
from typing import Callable

from ..types.capabilities import bool_field, check_keys, fail, int_field, require_object, str_field
from ..types.region import Frame, Rect, validate_rect
from .cancel import CancelToken
from .nms import cap_candidates, nms
from .types import Candidate, RecognitionError, center_of


@dataclass(frozen=True)
class DetectorModel:
    model_id: str
    version: str
    class_ids: frozenset[int]
    input_width: int
    input_height: int
    external_nms: bool

    @classmethod
    def from_manifest(cls, document):
        data = require_object(document, "model")
        formats = {"input_format": "RGB_NCHW_FLOAT32_0_1", "resize": "letterbox_center_114",
                   "output_layout": "N_XYXY_SCORE_CLASS"}
        check_keys(data, ("contract_version", "model_id", "version", "class_ids", "input_width",
                          "input_height", "external_nms", *formats), "model")
        int_field(data, "contract_version", "model.contract_version", 1, 1)
        for field, expected in formats.items():
            if data.get(field) != expected:
                fail("INVALID_PARAMS", "model." + field, f"Expected {expected}")
        ids = data.get("class_ids")
        if (not isinstance(ids, list) or not ids or any(type(k) is not int or k < 0 for k in ids)
                or len(ids) != len(set(ids))):
            fail("INVALID_PARAMS", "model.class_ids", "Declare distinct nonnegative integer classes")
        return cls(str_field(data, "model_id", "model.model_id"), str_field(data, "version", "model.version"),
                   frozenset(ids), int_field(data, "input_width", "model.input_width", 1, 16384),
                   int_field(data, "input_height", "model.input_height", 1, 16384),
                   bool_field(data, "external_nms", "model.external_nms"))


@dataclass(frozen=True)
class Letterbox:
    roi: Rect
    width: int
    height: int
    left: int
    top: int

    @classmethod
    def fit(cls, roi, model):
        # Integer half-up rounding, with the aspect ratio fitted to the limiting axis.
        if roi.width * model.input_height >= roi.height * model.input_width:
            width = model.input_width
            height = max(1, (2 * roi.height * width + roi.width) // (2 * roi.width))
        else:
            height = model.input_height
            width = max(1, (2 * roi.width * height + roi.height) // (2 * roi.height))
        return cls(roi, width, height, (model.input_width - width) // 2, (model.input_height - height) // 2)

    def restore(self, bounds, origin):
        x1, y1, x2, y2 = bounds
        x1, x2 = max(x1, self.left) - self.left, min(x2, self.left + self.width) - self.left
        y1, y2 = max(y1, self.top) - self.top, min(y2, self.top + self.height) - self.top
        if x1 >= x2 or y1 >= y2:
            return None
        left = floor(x1 * self.roi.width / self.width)
        top = floor(y1 * self.roi.height / self.height)
        right = ceil(x2 * self.roi.width / self.width)
        bottom = ceil(y2 * self.roi.height / self.height)
        return Rect(origin.x + self.roi.x + left, origin.y + self.roi.y + top, right - left, bottom - top)


def _bounded_number(value, maximum):
    # Both chained comparisons must hold, including for NaN and infinities.
    return type(value) in (int, float) and 0 <= value <= maximum


def _current(frame, read_current, cancel):
    cancel.throw_if_cancelled()
    if read_current() != frame:
        raise RecognitionError("STALE_FRAME", "Frame identity or geometry changed", retryable=True)


def detect_objects(registry, model: DetectorModel, frame: Frame, request: dict,
                   current_frame: Callable[[], Frame], cancel: CancelToken) -> dict:
    """Call the selected adapter and validate its decoded rows before releasing any results.

    An adapter implements recognize(frame, params, cancel), using its validated model and
    the supplied ROI. Its output must be N rows of [x1, y1, x2, y2, confidence, class_id].
    """
    _current(frame, current_frame, cancel)
    data = require_object(request, "object_detect")
    check_keys(data, ("model_id", "class_id", "threshold", "roi"), "object_detect")
    if data.get("model_id") != model.model_id:
        fail("INVALID_PARAMS", "object_detect.model_id", "Request and selected detector model differ")
    class_id = int_field(data, "class_id", "object_detect.class_id", 0)
    if class_id not in model.class_ids:
        fail("INVALID_PARAMS", "object_detect.class_id", "Unknown requested class")
    threshold = data.get("threshold")
    if not _bounded_number(threshold, 1):
        fail("INVALID_PARAMS", "object_detect.threshold", "Expected a finite threshold in [0,1]")
    roi = validate_rect(data.get("roi"))
    if (roi.x < 0 or roi.y < 0 or roi.x + roi.width > frame.image_ref.width
            or roi.y + roi.height > frame.image_ref.height):
        fail("INVALID_PARAMS", "object_detect.roi", "ROI outside source image")
    transform = Letterbox.fit(roi, model)
    provider = registry.require_provider("object_detect")
    rows = provider.recognize(frame, {"model_id": model.model_id, "version": model.version,
                                     "roi": asdict(roi)}, cancel)
    _current(frame, current_frame, cancel)
    if not isinstance(rows, (list, tuple)):
        raise RecognitionError("RECOGNITION_FAILED", "Expected decoded N×6 rows", "output")
    candidates = []
    for index, row in enumerate(rows):
        cancel.throw_if_cancelled()
        field = f"output[{index}]"
        if not isinstance(row, (list, tuple)) or len(row) != 6:
            raise RecognitionError("RECOGNITION_FAILED", "Expected six columns", field)
        x1, y1, x2, y2, score, label = row
        if type(label) is not int or label not in model.class_ids:
            raise RecognitionError("RECOGNITION_FAILED", "Undeclared detector class", field + ".class_id")
        if not _bounded_number(score, 1):
            raise RecognitionError("RECOGNITION_FAILED", "Invalid detector score", field + ".confidence")
        if (not all(_bounded_number(x, model.input_width) for x in (x1, x2))
                or not all(_bounded_number(y, model.input_height) for y in (y1, y2))
                or x1 >= x2 or y1 >= y2):
            raise RecognitionError("RECOGNITION_FAILED", "Invalid detector bounds", field + ".bbox")
        if label == class_id and score >= threshold:
            box = transform.restore(row[:4], frame.origin)
            if box is not None:
                candidates.append(Candidate(box, center_of(box), score, frame.space))
    ordered = nms(candidates) if model.external_nms else sorted(candidates, key=lambda c: -c.confidence)
    selected, truncated = cap_candidates(ordered)
    matches = [dict(candidate.to_dict(), class_id=class_id) for candidate in selected]
    _current(frame, current_frame, cancel)
    return {"frame_id": frame.frame_id, "geometry_revision": frame.geometry_revision,
            "matches": matches, "truncated": truncated}
