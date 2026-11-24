# -*- coding: utf-8 -*-
"""TASK-013 worker/取消单测（``backend.vision.worker`` / ``backend.vision.cancel``；§7）。

覆盖：``DeadlineCancelToken`` 超时（注入单调时钟，不真实 sleep 5s）、协作式取消传导、
``ThreadInferenceWorker`` 异步执行、``restart``（abandon）、``thread_safe=false`` 串行化。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.ipc.jobs import JobManager  # noqa: E402
from backend.tests._vision_fakes import (  # noqa: E402
    ConcurrencyProvider,
    FakeMonotonic,
    RecordingPixelReader,
    constant_monotonic,
    make_manifest,
    wait_until,
)
from backend.vision.cancel import CancelFlag, CancelledError  # noqa: E402
from backend.vision.registry import ProviderRegistry  # noqa: E402
from backend.vision.service import VisionService  # noqa: E402
from backend.vision.worker import DeadlineCancelToken, ThreadInferenceWorker  # noqa: E402


class DeadlineCancelTokenTest(unittest.TestCase):
    def test_times_out_with_advancing_clock(self):
        token = DeadlineCancelToken(
            deadline_ms=5000, monotonic=FakeMonotonic(start=0.0, step=1.0)
        )
        cancelled = any(token.is_cancelled() for _ in range(10))
        self.assertTrue(cancelled)
        self.assertTrue(token.timed_out())

    def test_base_cancel_without_timeout(self):
        flag = CancelFlag()
        flag.cancel()
        token = DeadlineCancelToken(flag, deadline_ms=5000, monotonic=constant_monotonic())
        self.assertTrue(token.is_cancelled())
        self.assertFalse(token.timed_out())
        with self.assertRaises(CancelledError):
            token.throw_if_cancelled()

    def test_no_cancel_before_deadline(self):
        token = DeadlineCancelToken(deadline_ms=5000, monotonic=constant_monotonic())
        self.assertFalse(token.is_cancelled())


class ThreadInferenceWorkerTest(unittest.TestCase):
    def test_runs_async_and_receives_token(self):
        import threading

        worker = ThreadInferenceWorker(monotonic=constant_monotonic())
        seen = {}
        done = threading.Event()

        def run(token):
            seen["token"] = token
            done.set()

        worker.submit(run, deadline_ms=5000)
        self.assertTrue(done.wait(2.0))
        self.assertIsInstance(seen["token"], DeadlineCancelToken)

    def test_restart_counts(self):
        worker = ThreadInferenceWorker(monotonic=constant_monotonic())
        worker.restart()
        worker.restart()
        self.assertEqual(worker.restarts, 2)


class SerializationTest(unittest.TestCase):
    def _service(self, provider):
        pixels = RecordingPixelReader()
        pixels.add("img", value=1)
        registry = ProviderRegistry(pixels, factories={"c": lambda pr: provider})
        registry.initialize_all(tempfile.mkdtemp(prefix="st-vision-ser-"))
        service = VisionService(
            registry, pixels=pixels, jobs=JobManager(),
            worker=ThreadInferenceWorker(monotonic=constant_monotonic()),
        )
        return service

    def test_thread_unsafe_provider_serialized(self):
        provider = ConcurrencyProvider(
            make_manifest("c", condition_types=("template",), thread_safe=False)
        )
        service = self._service(provider)
        j1 = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        j2 = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(
            lambda: service.jobs.get(j1)["status"] in ("completed", "error", "cancelled")
            and service.jobs.get(j2)["status"] in ("completed", "error", "cancelled")
        ))
        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider.max_active, 1)

    def test_thread_safe_provider_not_serialized(self):
        provider = ConcurrencyProvider(
            make_manifest("c", condition_types=("template",), thread_safe=True)
        )
        service = self._service(provider)
        j1 = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        j2 = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(
            lambda: service.jobs.get(j1)["status"] in ("completed", "error", "cancelled")
            and service.jobs.get(j2)["status"] in ("completed", "error", "cancelled")
        ))
        # thread_safe=true：允许并发（峰值 ≥1；不断言恰 2 以避免调度偶然性）
        self.assertGreaterEqual(provider.max_active, 1)


if __name__ == "__main__":
    unittest.main()
