# -*- coding: utf-8 -*-
"""TASK-013 异步 job 化单测（``backend.vision.service``；§6 / §10 / §16）。

覆盖：6 命令 job 化（5s 返回 job_id）、``get_job`` 双路、``cancel_job`` 幂等与传导、
**终态一次**、**未命中 ≠ 故障**（``recognition_completed``，不发 error）、超时
（``RECOGNITION_TIMEOUT`` retryable=true + restart）、能力未提供 → ``CAPABILITY_DENIED``、
provider 故障 → ``RECOGNITION_FAILED``、在途上限 → ``BUSY``（Q1）。
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
    FakeConditionEvaluator,
    FakeMonotonic,
    FakeProvider,
    FakeTargetSnapshotProvider,
    RecordingPixelReader,
    RecordingRecognitionEmitter,
    constant_monotonic,
    make_candidate,
    make_manifest,
    wait_until,
)
from backend.vision.registry import ProviderRegistry  # noqa: E402
from backend.vision.service import VisionService  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402
from backend.vision.worker import ThreadInferenceWorker  # noqa: E402

TERMINAL = ("completed", "error", "cancelled")


def settled(service, job_id):
    """job 进入终态且 worker 已完成（active_count 归零，事件必已发出）。"""
    snapshot = service.jobs.get(job_id)
    return (
        isinstance(snapshot, dict)
        and snapshot.get("status") in TERMINAL
        and service.active_count == 0
    )


def build_service(
    provider,
    *,
    pixels=None,
    targets=None,
    evaluator=None,
    emitter=None,
    monotonic=None,
    max_inflight=3,
    jobs=None,
):
    pixels = pixels if pixels is not None else RecordingPixelReader()
    registry = ProviderRegistry(pixels, factories={"p": lambda pr: provider})
    registry.initialize_all(tempfile.mkdtemp(prefix="st-vision-jobs-"))
    service = VisionService(
        registry,
        pixels=pixels,
        targets=targets,
        evaluator=evaluator,
        jobs=jobs if jobs is not None else JobManager(),
        worker=ThreadInferenceWorker(monotonic=monotonic if monotonic is not None else constant_monotonic()),
        emitter=emitter if emitter is not None else RecordingRecognitionEmitter(),
        max_inflight=max_inflight,
    )
    return service, pixels


class MatchJobTest(unittest.TestCase):
    def test_match_completes_with_candidates(self):
        provider = FakeProvider(
            make_manifest("p", condition_types=("template",)),
            candidates=[make_candidate(0, 0, 4, 4, 0.9)],
        )
        service, pixels = build_service(provider)
        pixels.add("img")
        job_id = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        snapshot = service.jobs.get(job_id)
        self.assertEqual(len(snapshot["result"]["matches"]), 1)
        self.assertFalse(snapshot["result"]["truncated"])
        # 终态只发一次 recognition_completed
        self.assertEqual(service.emitter.names(), ["recognition_completed"])

    def test_miss_is_not_error(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)), candidates=[])
        service, pixels = build_service(provider)
        pixels.add("img")
        job_id = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        snapshot = service.jobs.get(job_id)
        self.assertEqual(snapshot["result"]["matches"], [])  # 未命中=数据，非错误
        self.assertIsNone(snapshot["error"])
        self.assertEqual(service.emitter.names(), ["recognition_completed"])

    def test_provider_failure_emits_error(self):
        provider = FakeProvider(
            make_manifest("p", condition_types=("template",)), raise_code="RECOGNITION_FAILED"
        )
        service, pixels = build_service(provider)
        pixels.add("img")
        job_id = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        snapshot = service.jobs.get(job_id)
        self.assertEqual(snapshot["error"]["code"], "RECOGNITION_FAILED")
        self.assertEqual(service.emitter.names(), ["recognition_error"])


class CapabilityDeniedTest(unittest.TestCase):
    def test_no_provider_for_condition(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)))
        service, pixels = build_service(provider)
        pixels.add("img")
        job_id = service.submit("match_contour", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        self.assertEqual(service.jobs.get(job_id)["error"]["code"], "CAPABILITY_DENIED")

    def test_evaluator_not_injected(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)))
        service, pixels = build_service(provider)  # evaluator=None（基座缺省）
        pixels.add("img")
        job_id = service.submit("evaluate_target_conditions", {"image_id": "img", "target_id": "t1"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        self.assertEqual(service.jobs.get(job_id)["error"]["code"], "CAPABILITY_DENIED")


class CancelJobTest(unittest.TestCase):
    def test_cancel_propagates_to_provider(self):
        provider = FakeProvider(
            make_manifest("p", condition_types=("template",)), spin=True, spinner=10_000_000
        )
        service, pixels = build_service(provider)
        pixels.add("img")
        job_id = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(lambda: service.jobs.get(job_id)["status"] == "running"))
        self.assertTrue(service.jobs.cancel(job_id))
        self.assertTrue(wait_until(lambda: service.jobs.get(job_id)["status"] == "cancelled"))
        # 终态由外部 cancel_job 写入；识别事件由 worker 观测到取消后发（等待其完成）
        self.assertTrue(wait_until(lambda: service.emitter.names() == ["recognition_cancelled"]))
        self.assertEqual(service.active_count, 0)

    def test_cancel_terminal_is_idempotent(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)), candidates=[])
        service, pixels = build_service(provider)
        pixels.add("img")
        job_id = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        # 终态取消 → False（03），且不产生额外事件
        self.assertFalse(service.jobs.cancel(job_id))
        self.assertEqual(service.emitter.names(), ["recognition_completed"])


class TimeoutTest(unittest.TestCase):
    def test_uncooperative_provider_expires_before_late_return(self):
        import threading
        from backend.tests._vision_fakes import ConcurrencyProvider
        released = threading.Event()
        entered = threading.Event()
        provider = ConcurrencyProvider(make_manifest("p", condition_types=("template",)))
        original = provider.recognize
        def blocked(frame, params, cancel):
            entered.set()
            released.wait(2)
            return original(frame, params, cancel)
        provider.recognize = blocked
        service, pixels = build_service(provider)
        service._deadline_ms = 100
        pixels.add("img")
        try:
            job = service.submit("match_template", {"image_id":"img", "template_image_id":"img"})
            self.assertTrue(entered.wait(1))
            self.assertTrue(wait_until(lambda: service.jobs.get(job)["status"] == "error", timeout=.7))
            self.assertEqual(service.jobs.get(job)["error"]["code"], "RECOGNITION_TIMEOUT")
        finally:
            released.set()
            service.worker.shutdown(timeout=1)
        self.assertEqual(service.jobs.get(job)["status"], "error")
        self.assertEqual(service.worker.restarts, 1)

    def test_timeout_recognition_timeout_and_restart(self):
        provider = FakeProvider(
            make_manifest("p", condition_types=("template",)), spin=True, spinner=1_000_000
        )
        service, pixels = build_service(provider, monotonic=FakeMonotonic(step=1.0))
        pixels.add("img")
        job_id = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        error = service.jobs.get(job_id)["error"]
        self.assertEqual(error["code"], "RECOGNITION_TIMEOUT")
        self.assertTrue(error["retryable"])
        self.assertEqual(service.emitter.names(), ["recognition_error"])
        self.assertGreaterEqual(service.worker.restarts, 1)

    def test_get_unknown_job_returns_none(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)))
        service, _pixels = build_service(provider)
        self.assertIsNone(service.jobs.get("job-does-not-exist"))


class InFlightCapTest(unittest.TestCase):
    def test_busy_when_inflight_exceeded(self):
        provider = FakeProvider(
            make_manifest("p", condition_types=("template",)), spin=True, spinner=10_000_000
        )
        service, pixels = build_service(provider, max_inflight=1)
        pixels.add("img")
        job_id = service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        with self.assertRaises(RecognitionError) as ctx:
            service.submit("match_template", {"image_id": "img", "template_image_id": "img"})
        self.assertEqual(ctx.exception.code, "BUSY")
        self.assertTrue(ctx.exception.retryable)
        # 清理：取消在途 job
        service.jobs.cancel(job_id)
        self.assertTrue(wait_until(lambda: service.jobs.get(job_id)["status"] == "cancelled"))


class EvaluatorPathTest(unittest.TestCase):
    def test_evaluate_target_conditions_found(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)))
        targets = FakeTargetSnapshotProvider()
        targets.add("t1", revision=3)
        evaluator = FakeConditionEvaluator()
        service, pixels = build_service(provider, targets=targets, evaluator=evaluator)
        pixels.add("img")
        job_id = service.submit("evaluate_target_conditions", {"image_id": "img", "target_id": "t1"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        result = service.jobs.get(job_id)["result"]
        self.assertTrue(result["found"])
        # 短路叶：skipped=true 且 matched=null（不伪造分数）
        evidence = result["matches"][0]["evidence"]
        skipped = [leaf for leaf in evidence if leaf["skipped"]]
        self.assertTrue(skipped)
        self.assertIsNone(skipped[0]["matched"])

    def test_test_recognition_draft_forces_null_target(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)))
        evaluator = FakeConditionEvaluator()
        service, pixels = build_service(provider, evaluator=evaluator)
        pixels.add("img")
        job_id = service.submit("test_recognition", {"image_id": "img", "draft": {"name": "d"}})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        result = service.jobs.get(job_id)["result"]
        self.assertIsNone(result["target_id"])
        self.assertIsNone(result["sample_no"])

    def test_target_not_found(self):
        provider = FakeProvider(make_manifest("p", condition_types=("template",)))
        targets = FakeTargetSnapshotProvider()
        evaluator = FakeConditionEvaluator()
        service, pixels = build_service(provider, targets=targets, evaluator=evaluator)
        pixels.add("img")
        job_id = service.submit("evaluate_target_conditions", {"image_id": "img", "target_id": "nope"})
        self.assertTrue(wait_until(lambda: settled(service, job_id)))
        self.assertEqual(service.jobs.get(job_id)["error"]["code"], "TARGET_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
