"""Exercise a permanently blocked provider through the normal job and script paths."""
import concurrent.futures
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.ipc.jobs import JobManager
from backend.tests._vision_fakes import RecordingPixelReader, make_manifest, wait_until
from backend.vision.cancel import NullCancelToken
from backend.vision.emitter import RecordingRecognitionEmitter
from backend.vision.providers.base import BaseProvider
from backend.vision.registry import ProviderRegistry
from backend.vision.service import VisionService


class BlockingProvider(BaseProvider):
    def __init__(self, pixels):
        super().__init__(make_manifest("fixture", condition_types=("template",)))
        self.pixels = pixels

    def initialize(self, models_dir):
        self.entered = Path(models_dir) / "entered"

    def recognize(self, frame, params, cancel, roi=None):
        self.pixels.read(frame.image_ref.image_id)
        if params.get("hang"):
            self.entered.write_text(str(os.getpid()), encoding="ascii")
            threading.Event().wait()
        return self._provider_result()


class OtherProvider(BlockingProvider):
    def __init__(self, pixels):
        super().__init__(pixels)

    def capabilities(self):
        return make_manifest("other", condition_types=("color",))


class InferenceLifecycleTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.entered = Path(folder.name) / "entered"
        pixels = RecordingPixelReader()
        pixels.add("img")
        registry = ProviderRegistry(pixels, factories={"fixture": BlockingProvider, "other": OtherProvider},
                                    isolate_native=True)
        registry.initialize_all(folder.name)
        self.service = VisionService(registry, pixels=pixels, jobs=JobManager(),
                                     emitter=RecordingRecognitionEmitter(), deadline_ms=1000)
        self.addCleanup(self.service.close)

    def run_hang(self, cancel):
        service = self.service
        params = {"image_id": "img", "template_image_id": "img"}
        job = service.submit("match_template", dict(params, hang=True))
        self.assertTrue(wait_until(self.entered.exists))
        old_pid = int(self.entered.read_text(encoding="ascii"))
        other_pid = service.registry.provider_for("color")._process.pid
        other = service.run_sync("detect_color", {"image_id": "img"}, NullCancelToken())
        self.assertEqual(other, {"matches": [], "truncated": False})
        start = time.monotonic()
        if cancel:
            self.assertTrue(service.jobs.cancel(job))
        self.assertTrue(wait_until(lambda: service.active_count == 0, timeout=2))
        reap_ms = (time.monotonic() - start) * 1000
        self.assertNotIn(old_pid, [p.pid for p in multiprocessing.active_children()])
        self.assertEqual(service.registry.provider_for("color")._process.pid, other_pid)
        state = service.jobs.get(job)
        if cancel:
            self.assertEqual(state["status"], "cancelled")
            self.assertEqual(service.emitter.names(), ["recognition_cancelled"])
        else:
            self.assertEqual(state["error"]["code"], "RECOGNITION_TIMEOUT")
            self.assertEqual(service.emitter.names(), ["recognition_error"])
        self.assertIsNone(state["result"])
        result = service.run_sync("match_template", params, NullCancelToken())
        self.assertEqual(result, {"matches": [], "truncated": False})
        print(f"retired_pid={old_pid}, replacement_pid={service.registry.provider_for('template')._process.pid}, "
              f"unaffected_pid={other_pid}, terminal={state['status']}, reap_ms={reap_ms:.3f}, "
              f"active_jobs={service.active_count}")

    def test_timeout_removes_process_and_allows_next_inference(self):
        self.run_hang(False)

    def test_cancel_removes_process_and_retains_other_provider(self):
        self.run_hang(True)


class TerminalRetentionTest(unittest.TestCase):
    def test_concurrent_terminals_retain_bounded_history_and_preserve_running(self):
        now = [0.]
        jobs = JobManager(monotonic=lambda: now[0])
        running = jobs.create_job()
        jobs.mark_running(running)
        ids = [jobs.create_job() for _ in range(1000)]
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            self.assertTrue(all(pool.map(jobs.cancel, ids)))
            self.assertFalse(any(pool.map(jobs.complete, ids, ids)))
        before_expiry = sum(jobs.get(job) is not None for job in ids)
        self.assertEqual(before_expiry, 256)
        now[0] = 299.999
        self.assertEqual(sum(jobs.get(job) is not None for job in ids), 256)
        now[0] = 300.
        self.assertFalse(any(jobs.get(job) for job in ids))
        self.assertEqual(jobs.get(running)["status"], "running")
        print(f"terminal_created=1000, terminal_retained={before_expiry}, retained_at_300s=0, running=1")
