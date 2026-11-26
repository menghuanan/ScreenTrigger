"""Executable v1 detection-provider vectors; run this module with --observations for JSON."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from backend.ipc.envelope import MalformedFrameError, parse_frame
from backend.types.capabilities import TypeValidationError
from backend.types.region import Point, Rect, validate_frame
from backend.vision.cancel import CancelFlag, CancelledError
from backend.vision.object_detection import DetectorModel, Letterbox, detect_objects
from backend.vision.registry import ProviderRegistry
from backend.vision.types import RecognitionError


ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "schema/object-detection-v1.cases.json").read_bytes())
MODEL = DetectorModel.from_manifest(VECTORS["model"])
FRAME = validate_frame(VECTORS["frame"])


class FixtureDetector:
    """Contains only already decoded synthetic rows. Never registered in the production factory."""

    def __init__(self, case, frame):
        self.case = case
        self.frame = frame

    def require_provider(self, condition):
        assert condition == "object_detect"
        return self

    def recognize(self, frame, params, cancel):
        assert frame == self.frame and params["model_id"] == MODEL.model_id
        rows = deepcopy(self.case["rows"])
        injection = self.case.get("inject")
        if injection == "nan_score":
            rows[0][4] = float("nan")
        elif injection == "cancel_after_decode":
            cancel.cancel()
        elif injection == "move_after_decode":
            self.frame = replace(frame, geometry_revision=frame.geometry_revision + 1)
        return rows


def observe(case, frame=FRAME, model=MODEL):
    provider = FixtureDetector(case, frame)
    try:
        return detect_objects(provider, model, frame, VECTORS["request"], lambda: provider.frame, CancelFlag())
    except RecognitionError as exc:
        return exc.to_dict()
    except CancelledError:
        return {"status": "cancelled"}


def hello_without_weights():
    """Real sidecar/NDJSON with a fresh data root and Python site packages disabled."""
    session = "contract-yolo"
    envelope = dict(protocol_version=1, session_id=session, type="request", id="fixture-hello",
                    command="hello", params={})
    with tempfile.TemporaryDirectory(prefix="st-detection-") as folder:
        process = subprocess.run([sys.executable, "-S", "-X", "utf8", "-m", "backend.main",
                                  "--session-id", session, "--data-dir", folder], cwd=ROOT,
                                 capture_output=True, input=json.dumps(envelope) + "\n",
                                 encoding="utf-8", timeout=60)
    if process.returncode:
        raise AssertionError(f"sidecar exited {process.returncode}: {process.stderr}")
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    response = next(row for row in responses if row.get("id") == envelope["id"])
    if not response["success"]:
        raise AssertionError(response)
    return response["data"]


def malformed_json():
    line = '{"type":"request","protocol_version":1,"session_id":"synthetic","id":"q",' \
           '"command":"hello","params":{"score":NaN}}'
    try:
        parse_frame(line)
    except MalformedFrameError as exc:
        return exc.reason
    raise AssertionError("Accepted non-finite NDJSON")


def absent_provider():
    registry = ProviderRegistry(None)
    try:
        detect_objects(registry, MODEL, FRAME, VECTORS["request"], lambda: FRAME, CancelFlag())
    except RecognitionError as exc:
        return {"capability": registry.capability_reason("object_detect"), "error": exc.to_dict()}
    raise AssertionError("Unconfigured provider returned successfully")


class DetectionContractTests(unittest.TestCase):
    def test_frozen_vectors(self):
        for case in VECTORS["cases"]:
            with self.subTest(case=case["id"]):
                result = observe(case)
                expected = case["expect"]
                if "matches" in expected:
                    self.assertEqual(result["frame_id"], FRAME.frame_id)
                    self.assertEqual(result["geometry_revision"], FRAME.geometry_revision)
                    self.assertFalse(result["truncated"])
                    self.assertEqual(len(result["matches"]), len(expected["matches"]))
                    for actual, wanted in zip(result["matches"], expected["matches"]):
                        x, y, w, h = wanted["bbox"]
                        self.assertEqual(actual, {"class_id": wanted["class_id"], "confidence": wanted["confidence"],
                                                 "space": FRAME.space, "bbox": dict(x=x, y=y, width=w, height=h),
                                                 "center": dict(x=x + w // 2, y=y + h // 2)})
                else:
                    for key, value in expected.items():
                        self.assertEqual(result[key], value)
                    self.assertNotIn("matches", result)

    def test_origin_once_odd_padding_and_nms_policy(self):
        frame = replace(FRAME, origin=Point(-300, 12), space="screen_physical")
        result = observe(VECTORS["cases"][0], frame)
        self.assertEqual(result["matches"][0]["bbox"], dict(x=-190, y=72, width=40, height=30))
        odd = Letterbox.fit(Rect(1, 2, 7, 4), replace(MODEL, input_width=8, input_height=8))
        self.assertEqual((odd.width, odd.height, odd.left, odd.top), (8, 5, 0, 1))
        self.assertEqual(odd.restore([0, 0, 8, 8], Point(0, 0)), odd.roi)
        case = next(case for case in VECTORS["cases"] if case["id"] == "nms")
        self.assertEqual(len(observe(case, model=replace(MODEL, external_nms=False))["matches"]), 2)
        many = {"rows": [case["rows"][0]] * 101}
        limited = observe(many, model=replace(MODEL, external_nms=False))
        self.assertEqual(len(limited["matches"]), 100)
        self.assertTrue(limited["truncated"])

    def test_invalid_manifest_and_request(self):
        for changes in ({"class_ids": [2, 2]}, {"input_height": 0}, {"contract_version": True},
                        {"output_layout": "unregistered"}, {"external_nms": 1}):
            with self.subTest(changes=changes), self.assertRaises(TypeValidationError):
                DetectorModel.from_manifest(dict(VECTORS["model"], **changes))
        for changes in ({"model_id": "wrong"}, {"threshold": float("inf")}, {"class_id": 42},
                        {"roi": dict(x=399, y=1, width=2, height=1)}):
            with self.subTest(changes=changes), self.assertRaises(TypeValidationError):
                detect_objects(ProviderRegistry(None), MODEL, FRAME, dict(VECTORS["request"], **changes),
                               lambda: FRAME, CancelFlag())

    def test_model_unavailable_malformed_wire_and_pre_cancel(self):
        missing = absent_provider()
        self.assertEqual(missing["error"]["code"], "CAPABILITY_DENIED")
        self.assertEqual(missing["capability"][0], "unavailable")
        self.assertEqual(malformed_json(), "invalid_json")
        flag = CancelFlag()
        flag.cancel()
        with self.assertRaises(CancelledError):
            detect_objects(ProviderRegistry(None), MODEL, FRAME, VECTORS["request"], lambda: FRAME, flag)

    def test_sidecar_capabilities_without_detection_runtime(self):
        hello = hello_without_weights()
        self.assertTrue(hello["capabilities"])
        self.assertFalse(any(cap["id"] == "object_detect" and cap["state"] == "available"
                             for cap in hello["capabilities"]))
        self.assertFalse(any(m["model_id"] == MODEL.model_id for m in hello["model_manifests"]))


if __name__ == "__main__" and "--observations" in sys.argv:
    print(json.dumps({"model": VECTORS["model"], "frame": VECTORS["frame"], "request": VECTORS["request"],
                      "cases": [dict(case, actual=observe(case)) for case in VECTORS["cases"]],
                      "absent_provider": absent_provider(), "malformed_wire": malformed_json(),
                      "sidecar_hello": hello_without_weights()}, ensure_ascii=False, indent=2))
elif __name__ == "__main__":
    unittest.main()
