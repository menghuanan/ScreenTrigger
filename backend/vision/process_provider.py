"""Keep each native provider warm in a private, replaceable process.

The engine owns image references, job state and IPC. The child receives only the
immutable pixels needed for one call and returns a ProviderResult on its pipe.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from concurrent.futures import Future
from backend import startup_timing
from backend.worker_process import OcrWorkerProcess, WorkerProcess

from .cancel import CancelledError, NullCancelToken
from .types import RecognitionError
from .worker import DeadlineCancelToken


class _RequestPixels:
    def __init__(self):
        self.images = {}

    def read(self, image_id):
        return self.images[image_id]


def _serve(connection, factory, models_dir, options):
    # Native libraries can print to fd 1; only the sidecar owns protocol stdout.
    os.dup2(2, 1)
    pixels = _RequestPixels()
    provider = factory(pixels, **options)
    try:
        provider.initialize(models_dir)
        disclosure = getattr(provider, "disclosure", lambda: {})()
        fingerprint = getattr(provider, "cache_fingerprint", lambda _: {})
        connection.send(("ready", (disclosure, {
            key: fingerprint(key) for key in provider.capabilities().model_ids})))
        while True:
            try:
                frame, params, roi, images = connection.recv()
            except EOFError:
                return
            try:
                pixels.images = images
                reply = provider.recognize(frame, params, NullCancelToken(), roi=roi)
                connection.send(("result", reply))
            except RecognitionError as exc:
                connection.send(("error", exc.to_dict()))
            except Exception as exc:
                error = RecognitionError("RECOGNITION_FAILED", "识别故障: %s" % exc)
                connection.send(("error", error.to_dict()))
            finally:
                pixels.images = {}
                del images
    except RecognitionError as exc:
        connection.send(("error", exc.to_dict()))
    finally:
        connection.close()
        provider.close()


def _receive(connection):
    kind, value = connection.recv()
    if kind == "error":
        raise RecognitionError(value["code"], value["message"], value["details"].get("field"),
                               value["retryable"])
    return value


class ProcessProvider:
    """Serialize calls to one process. Waiting callers may cancel independently."""

    def __init__(self, pixels, factory, *, inference_deadline_ms=None, **options):
        self._pixels = pixels
        self._factory = factory
        self._options = options
        self._inference_deadline_ms = inference_deadline_ms
        self._manifest = factory(pixels, **options).capabilities()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._process = None
        self._pipe = None
        self._ready = False
        self.models_dir = None
        self.library_root = None
        self.opencv_algorithms = {}
        self._fingerprints = {}

    def capabilities(self):
        return self._manifest

    @property
    def process_state(self):
        if self._ready:
            return "ready"
        return "loading" if self._process is not None else "installed"

    def cache_fingerprint(self, model_id):
        return self._fingerprints.get(model_id, {})

    def _spawn(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        worker_type = OcrWorkerProcess if self._manifest.provider_id == "builtin.ocr" else WorkerProcess
        process = worker_type(target=_serve, args=(child, self._factory, self.models_dir, self._options),
                              name=self._manifest.provider_id, daemon=True)
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self._process = process
        if startup_timing.enabled():
            startup_timing.operation("ocr_worker_started", worker_pid=process.pid)
        self._pipe = parent
        self._startup_deadline = time.monotonic() + 60
        self._ready = False

    def _stop(self):
        if self._process is None:
            return
        if self._process.is_alive():
            self._process.kill()
        self._process.join()
        self._pipe.close()
        self._process.close()
        self._pipe = None
        self._process = None
        self._ready = False

    def _check(self, cancel):
        if self._closed.is_set():
            raise CancelledError("识别服务已关闭")
        cancel.throw_if_cancelled()

    def _await_ready(self, cancel):
        while not self._pipe.poll(.02):
            self._check(cancel)
            if not self._process.is_alive():
                raise RecognitionError("ENGINE_UNAVAILABLE", f"识别进程提前退出，退出码 {self._process.exitcode}", retryable=True)
            if time.monotonic() >= self._startup_deadline:
                self._stop()
                raise RecognitionError("ENGINE_UNAVAILABLE", "识别进程初始化超时", retryable=True)
        try:
            disclosure, self._fingerprints = _receive(self._pipe)
        except Exception:
            self._stop()
            raise
        self.library_root = disclosure.get("library_root")
        self.opencv_algorithms = disclosure.get("opencv_algorithms", {})
        self._ready = True
        if startup_timing.enabled():
            startup_timing.operation("ocr_worker_ready", worker_pid=self._process.pid)

    def initialize(self, models_dir, cancel=None):
        self.models_dir = models_dir
        with self._lock:
            self._spawn()
            try:
                self._await_ready(cancel or NullCancelToken())
            except BaseException:
                self._stop()
                raise

    def recognize(self, frame, params, cancel, roi=None):
        while not self._lock.acquire(timeout=.02):
            self._check(cancel)
        try:
            self._check(cancel)
            if self._process is None:
                self._spawn()
            if not self._ready:
                # A previous job's replacement is warming. A waiting job's own
                # cancellation must not discard a healthy replacement.
                self._await_ready(cancel)
            ids = [frame.image_ref.image_id]
            if params.get("template_image_id"):
                ids.append(params["template_image_id"])
            images = {key: self._pixels.read(key) for key in ids}
            self._check(cancel)
            inference = (DeadlineCancelToken(cancel, deadline_ms=self._inference_deadline_ms)
                         if self._inference_deadline_ms else cancel)
            # A full pipe can block on a large frame if the child stops reading.
            # Keep cancellation independent of both sending and receiving.
            pipe = self._pipe
            if startup_timing.enabled():
                startup_timing.operation("ocr_inference", worker_pid=self._process.pid)
            reply = Future()

            def exchange():
                try:
                    pipe.send((frame, params, roi, images))
                    reply.set_result(_receive(pipe))
                except Exception as exc:
                    reply.set_exception(exc)

            io_thread = threading.Thread(target=exchange, name="vision-provider-io", daemon=True)
            io_thread.start()
            try:
                while io_thread.is_alive():
                    io_thread.join(.02)
                    self._check(inference)
            except CancelledError:
                self._stop()
                io_thread.join()
                if not self._closed.is_set() and self._inference_deadline_ms is None:
                    self._spawn()
                if self._inference_deadline_ms and inference.timed_out():
                    raise RecognitionError("RECOGNITION_TIMEOUT", "单次 OCR 推理超过 5 秒", retryable=True)
                raise
            self._check(inference)
            return reply.result()
        except (EOFError, OSError) as exc:
            self._stop()
            raise RecognitionError("RECOGNITION_FAILED", "识别进程通信中断", retryable=True) from exc
        finally:
            self._lock.release()

    def close(self):
        self._closed.set()
        with self._lock:
            self._stop()
