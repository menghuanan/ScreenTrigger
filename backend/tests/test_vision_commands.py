# -*- coding: utf-8 -*-
"""TASK-013 命令接线单测（``backend.vision.commands``；§6.1）。

覆盖：参数白名单（未声明参数 → INVALID_PARAMS 定位多余字段）、类型校验、threshold∈[0,1]、
**同步错误不建 job**（response.error）、合法 → 同步返回 ``{job_id}``（异步 job 化）。
经**真实 Dispatcher** 路由（验证与 06 四字段归一）。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.ipc.dispatcher import Dispatcher  # noqa: E402
from backend.ipc.jobs import JobManager  # noqa: E402
from backend.tests._vision_fakes import (  # noqa: E402
    FakeProvider,
    FakeSession,
    RecordingPixelReader,
    RecordingRecognitionEmitter,
    RecordingTransport,
    constant_monotonic,
    make_manifest,
    wait_until,
)
from backend.vision.commands import register_vision_commands  # noqa: E402
from backend.vision.registry import ProviderRegistry  # noqa: E402
from backend.vision.service import VisionService  # noqa: E402
from backend.vision.worker import ThreadInferenceWorker  # noqa: E402


def _build(provider=None):
    if provider is None:
        provider = FakeProvider(make_manifest("p", condition_types=("template",)))
    pixels = RecordingPixelReader()
    pixels.add("img")
    registry = ProviderRegistry(pixels, factories={"p": lambda pr: provider})
    registry.initialize_all(tempfile.mkdtemp(prefix="st-vision-cmd-"))
    jobs = JobManager()
    service = VisionService(
        registry, pixels=pixels, jobs=jobs,
        worker=ThreadInferenceWorker(monotonic=constant_monotonic()),
        emitter=RecordingRecognitionEmitter(),
    )
    transport = RecordingTransport()
    dispatcher = Dispatcher(
        transport=transport, session=FakeSession(), stderr=io.StringIO(), job_manager=jobs
    )
    register_vision_commands(dispatcher, service)
    return dispatcher, transport, service, provider


def _dispatch(dispatcher, command, params, request_id="r1"):
    dispatcher.dispatch_command({
        "protocol_version": 1, "session_id": "s-test", "type": "request",
        "id": request_id, "command": command, "params": params,
    })
    return dispatcher.transport.sent[-1]


class ValidCommandTest(unittest.TestCase):
    def test_match_template_returns_job_id(self):
        dispatcher, _t, _s, _p = _build()
        response = _dispatch(dispatcher, "match_template",
                             {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(response["success"])
        self.assertIsInstance(response["data"]["job_id"], str)

    def test_default_threshold_injected(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)))
        dispatcher, _t, service, provider = _build(provider)
        _dispatch(dispatcher, "match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(lambda: provider.calls == 1))
        self.assertEqual(provider.last_params["threshold"], 0.85)


class SyncValidationTest(unittest.TestCase):
    def _assert_sync_invalid(self, command, params, field):
        dispatcher, _t, service, _p = _build()
        response = _dispatch(dispatcher, command, params)
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"].get("field"), field)
        self.assertEqual(service.active_count, 0)  # 未建 job

    def test_unknown_param_rejected(self):
        self._assert_sync_invalid(
            "recognize_text", {"image_id": "img", "model_id": "m", "extra": 1}, "extra"
        )

    def test_missing_image_id(self):
        self._assert_sync_invalid("match_template", {"template_image_id": "img"}, "image_id")

    def test_image_id_type(self):
        self._assert_sync_invalid("match_template", {"image_id": 5, "template_image_id": "img"},
                                  "image_id")

    def test_missing_model_id(self):
        self._assert_sync_invalid("recognize_text", {"image_id": "img"}, "model_id")

    def test_threshold_out_of_range(self):
        self._assert_sync_invalid(
            "match_template", {"image_id": "img", "template_image_id": "img", "threshold": 1.5},
            "threshold",
        )

    def test_threshold_type(self):
        self._assert_sync_invalid(
            "match_template", {"image_id": "img", "template_image_id": "img", "threshold": "x"},
            "threshold",
        )

    def test_detect_color_missing_params(self):
        self._assert_sync_invalid("detect_color", {"image_id": "img"}, "params")

    def test_evaluate_missing_target_id(self):
        self._assert_sync_invalid("evaluate_target_conditions", {"image_id": "img"}, "target_id")

    def test_test_recognition_missing_draft(self):
        self._assert_sync_invalid("test_recognition", {"image_id": "img"}, "draft")


class BusyAndRegistrationTest(unittest.TestCase):
    def test_six_commands_registered(self):
        dispatcher, _t, _s, _p = _build()
        for command in ("recognize_text", "match_template", "match_contour", "detect_color",
                        "evaluate_target_conditions", "test_recognition"):
            self.assertIn(command, dispatcher._handlers)

    def test_busy_is_async_error_via_dispatcher(self):
        provider = FakeProvider(
            make_manifest("p", condition_types=("template",)), spin=True, spinner=10_000_000
        )
        pixels = RecordingPixelReader()
        pixels.add("img")
        registry = ProviderRegistry(pixels, factories={"p": lambda pr: provider})
        registry.initialize_all(tempfile.mkdtemp(prefix="st-vision-cmd-"))
        jobs = JobManager()
        service = VisionService(
            registry, pixels=pixels, jobs=jobs,
            worker=ThreadInferenceWorker(monotonic=constant_monotonic()),
            emitter=RecordingRecognitionEmitter(), max_inflight=1,
        )
        transport = RecordingTransport()
        dispatcher = Dispatcher(transport=transport, session=FakeSession(),
                                stderr=io.StringIO(), job_manager=jobs)
        register_vision_commands(dispatcher, service)
        first = _dispatch(dispatcher, "match_template",
                          {"image_id": "img", "template_image_id": "img"}, "r1")
        self.assertTrue(first["success"])
        second = _dispatch(dispatcher, "match_template",
                           {"image_id": "img", "template_image_id": "img"}, "r2")
        self.assertFalse(second["success"])
        self.assertEqual(second["error"]["code"], "BUSY")
        jobs.cancel(first["data"]["job_id"])


if __name__ == "__main__":
    unittest.main()
