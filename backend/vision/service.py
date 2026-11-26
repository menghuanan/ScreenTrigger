# -*- coding: utf-8 -*-
"""``VisionService``（TASK-013 §5.3/§6/§8/§9）：job 化编排 + provider 分派 + 结果归一 + 事件。

职责（分层：``commands → service → {registry, nms, types, cache, worker, cancel, emitter, ports}``）：

- ``submit(command, params)``：同步（≤5s）建 job、交 worker 执行、**立即**返回 ``job_id``；
- ``_run_job``：worker 线程内 mark_running → 分派 → 归一 → 终态（写 ``JobManager`` **后**发事件）；
- **未命中（``found=false``）≠ 故障**：match/color 空结果 → ``recognition_completed``（**不**发 error）；
- 超时（>5s）→ ``RECOGNITION_TIMEOUT``（``retryable=true``）+ ``worker.restart()``（丢弃结果）；
- 取消经 ``JobBoundCancelToken`` 观测既有 ``cancel_job`` 通道 → ``recognition_cancelled``；
- **终态仅一次**：由「首个终态写入者发事件」保证（§6.4）。

**Q1**：本基座**不改** ``jobs.py``；在途 job 上限由本服务**自记账**（超限 → ``BUSY``）。
**Q3**：``image_id → Frame`` 缺省 identity（``image_pixel``）；**严禁伪造 screen_physical**。
**Q10**：同一条件类型唯一 provider（装配期失败，见 registry）。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional, Set

from ..types.region import ImageRef
from .cache import RecognitionCache, cache_key
from .cancel import CancelFlag, CancelledError, JobBoundCancelToken
from .emitter import NullRecognitionEmitter, RecognitionEmitter
from .nms import IOU_THRESHOLD, MAX_CANDIDATES, cap_candidates, nms
from .ports import IdentityFrameResolver
from .registry import CAPABILITY_CONDITION_TYPES, ProviderRegistry
from .types import (
    RecognitionError,
    RecognitionResult,
    build_recognition_result,
    validate_recognition_result,
)
from .worker import INFERENCE_DEADLINE_MS, ThreadInferenceWorker, WorkerRunner

#: 每执行最多识别在途（06：并发 3 根执行、每执行 ≤1 在途；Q1 → 超限 BUSY）。
DEFAULT_MAX_INFLIGHT: int = 3

_run_catalog = ContextVar("vision_run_catalog", default=None)

#: 命令 → 条件类型（provider 分派）。
CONDITION_OF_COMMAND: Dict[str, str] = {
    "recognize_text": "ocr",
    "match_template": "template",
    "match_contour": "contour",
    "detect_color": "color",
}

#: 条件树求值命令（经 ``ConditionEvaluator`` 端口；TASK-016 提供实现）。
EVALUATOR_COMMANDS = ("evaluate_target_conditions", "test_recognition")


class VisionService:
    """识别基座服务（端口全注入；不 new 第二份 Repository/JobManager，防脑裂）。"""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        pixels: Any,
        targets: Any = None,
        evaluator: Any = None,
        frames: Any = None,
        jobs: Any = None,
        worker: Optional[WorkerRunner] = None,
        cache: Optional[RecognitionCache] = None,
        emitter: Optional[RecognitionEmitter] = None,
        logger: Optional[Callable[[str], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        clock: Any = None,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        deadline_ms: int = INFERENCE_DEADLINE_MS,
    ) -> None:
        if registry is None:
            raise ValueError("registry 不能为空")
        if pixels is None:
            raise ValueError("pixels 不能为空")
        self._registry = registry
        self._pixels = pixels
        self._targets = targets
        self._evaluator = evaluator
        self._frames = frames if frames is not None else IdentityFrameResolver(clock=clock)
        self._jobs = jobs
        self._monotonic: Callable[[], float] = monotonic if monotonic is not None else time.monotonic
        self._worker: WorkerRunner = (
            worker if worker is not None else ThreadInferenceWorker(monotonic=self._monotonic,
                                                                   logger=logger)
        )
        self._cache: RecognitionCache = cache if cache is not None else RecognitionCache()
        self._emitter: RecognitionEmitter = (
            emitter if emitter is not None else NullRecognitionEmitter()
        )
        self._logger = logger
        self._clock = clock
        self._max_inflight = int(max_inflight)
        self._deadline_ms = int(deadline_ms)
        self._lock = threading.Lock()
        self._active: Set[object] = set()
        self._closed = False

    # -- 访问器 --------------------------------------------------------------

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def pixels(self) -> Any:
        """只读像素端口（装配级断言：与 capture/import 共用同一 image_store）。"""
        return self._pixels

    @property
    def targets(self) -> Any:
        """只读目标快照端口（装配级断言：与 target/script 共用同一 Repository）。"""
        return self._targets

    @property
    def worker(self) -> WorkerRunner:
        return self._worker

    @property
    def cache(self) -> RecognitionCache:
        return self._cache

    @property
    def jobs(self) -> Any:
        return self._jobs

    @jobs.setter
    def jobs(self, value: Any) -> None:
        """注入共享 ``JobManager``（``SidecarApp`` 装配期，与 ``get_job``/``cancel_job`` 同源）。"""
        if value is None:
            raise ValueError("jobs 不能为空")
        self._jobs = value

    @property
    def emitter(self) -> RecognitionEmitter:
        return self._emitter

    @emitter.setter
    def emitter(self, value: RecognitionEmitter) -> None:
        """注入/替换事件发射端口（``SidecarApp`` 用 transport/health/session 装配 stdio 实现）。"""
        if value is None:
            raise ValueError("emitter 不能为空")
        self._emitter = value

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    # -- 能力披露 ------------------------------------------------------------

    def capability_state(self, capability_id: str) -> Any:
        """``capability_id``（ocr/template_match/contour_match/color_match）→ ``(state, reason)``。"""
        condition_type = CAPABILITY_CONDITION_TYPES.get(capability_id)
        if condition_type is None:
            return ("unavailable", "未知能力 id: %s" % capability_id)
        return self._registry.capability_reason(condition_type)

    def installed_model_manifests(self) -> tuple:
        """供 ``hello.model_manifests``（基座为空元组）。"""
        return self._registry.installed_model_manifests()

    # -- 受理（同步，≤5s） ----------------------------------------------------

    def submit(self, command: str, params: Dict[str, Any]) -> str:
        """受理一次识别：建 job → 交 worker → 返回 ``job_id``（**不等待**推理）。

        Raises:
            RecognitionError: BUSY（在途超上限）/ INTERNAL_ERROR（JobManager 未注入）。
        """
        params = params if isinstance(params, dict) else {}
        with self._lock:
            if self._jobs is None:
                raise RecognitionError("INTERNAL_ERROR", "job 管理器未注入（装配错误）")
            if len(self._active) >= self._max_inflight:
                raise RecognitionError(
                    "BUSY", "识别在途任务已达上限 %d" % self._max_inflight, retryable=True
                )
            job_id = self._jobs.create_job(command)
            self._active.add(job_id)
        job_token = JobBoundCancelToken(job_id, self._jobs, CancelFlag())

        def expire():
            with self._lock:
                if job_id not in self._active:
                    return
            if not job_token.is_cancelled():
                self._conclude_timeout(job_id)

        watchdog = threading.Timer(self._deadline_ms / 1000, expire)
        watchdog.daemon = True
        def _run(token: Any) -> None:
            try:
                self._run_job(job_id, command, params, token)
            finally:
                watchdog.cancel()

        watchdog.start()
        try:
            self._worker.submit(_run, deadline_ms=self._deadline_ms, cancel=job_token)
        except Exception:
            watchdog.cancel()
            with self._lock:
                self._active.discard(job_id)
            self._jobs.fail(job_id, RecognitionError("RECOGNITION_FAILED", "推理工作线程未启动").to_dict())
            raise
        return job_id

    # -- job 执行 ------------------------------------------------------------

    def _run_job(self, job_id: str, command: str, params: Dict[str, Any], token: Any) -> None:
        try:
            if not self._jobs.mark_running(job_id):
                # job 已被 cancel_job 取消（worker 启动前到达）：终态 cancelled
                if not self._emit_cancelled_if_terminal(job_id):
                    self._jobs.cancel(job_id)
                return
            try:
                normalized = self._execute(command, params, token)
            except CancelledError:
                if token.timed_out():
                    self._conclude_timeout(job_id)
                else:
                    self._conclude_cancel(job_id)
            except RecognitionError as exc:
                if token.timed_out():
                    self._conclude_timeout(job_id)
                else:
                    self._conclude_error(job_id, exc.to_dict())
            except Exception as exc:  # provider 抛未归一异常
                self._log("识别内部故障：%r" % (exc,))
                self._conclude_error(
                    job_id, RecognitionError("RECOGNITION_FAILED", "识别内部故障").to_dict()
                )
            else:
                if token.timed_out():
                    self._conclude_timeout(job_id)  # 协作式 provider 于 deadline 后返回 → 丢弃结果
                else:
                    self._conclude_completed(job_id, normalized)
        finally:
            with self._lock:
                self._active.discard(job_id)

    def run_sync(self, command, params, cancel, *, catalog=None):
        """Use the job execution path with a bounded wait on the script thread."""
        outcome = Future()
        expires = time.monotonic() + self._deadline_ms / 1000
        # Include script inference in the same budget as interactive recognition jobs.
        slot = object()
        with self._lock:
            if len(self._active) >= self._max_inflight:
                raise RecognitionError("BUSY", "识别在途任务已达上限", retryable=True)
            self._active.add(slot)

        def run(token):
            binding = _run_catalog.set(catalog)
            try:
                value = self._execute(command, params, token)
                token.throw_if_cancelled()
                outcome.set_result(value)
            except Exception as exc:
                outcome.set_exception(exc)
            finally:
                _run_catalog.reset(binding)
                with self._lock:
                    self._active.discard(slot)

        try:
            self._worker.submit(run, deadline_ms=self._deadline_ms, cancel=cancel)
        except Exception:
            with self._lock:
                self._active.discard(slot)
            raise
        while True:
            cancel.throw_if_cancelled()
            remaining = expires - time.monotonic()
            if remaining <= 0:
                self._worker.restart()
                raise RecognitionError("RECOGNITION_TIMEOUT", "单次推理超过期限", retryable=True)
            try:
                return outcome.result(timeout=min(remaining, .05))
            except FutureTimeout:
                continue
            except CancelledError:
                cancel.throw_if_cancelled()
                self._worker.restart()
                raise RecognitionError("RECOGNITION_TIMEOUT", "单次推理超过期限", retryable=True)

    def _execute(self, command: str, params: Dict[str, Any], token: Any) -> Dict[str, Any]:
        token.throw_if_cancelled()
        if command in CONDITION_OF_COMMAND:
            return self._execute_provider(command, params, token)
        if command in EVALUATOR_COMMANDS:
            return self._execute_evaluator(command, params, token)
        raise RecognitionError("INVALID_PARAMS", "未知识别命令: %s" % command, field="command")

    # -- provider 分派路径 ----------------------------------------------------

    def _execute_provider(self, command: str, params: Dict[str, Any], token: Any) -> Dict[str, Any]:
        condition_type = CONDITION_OF_COMMAND[command]
        provider = self._registry.require_provider(condition_type)
        manifest = self._registry.manifest_for(provider)
        raw = self._pixels.read(params.get("image_id"))
        frame = self._frames.resolve(self._image_ref(raw))
        model_id = params.get("model_id")
        if condition_type == "ocr" and model_id is not None:
            if manifest is not None and model_id not in manifest.model_ids:
                raise RecognitionError("MODEL_UNAVAILABLE", "模型未安装: %s" % model_id,
                                       field="model_id")
        # TASK-014 §7：match_* 额外把**模板内容 sha256** 折入缓存参数（模板内容变 → 键变，
        # 防误命中；template_image_id 仍保留用于会话去重）。读模板像素失败 → IMAGE_EXPIRED。
        cache_params: Dict[str, Any] = params
        if command in ("match_template", "match_contour"):
            template_image_id = params.get("template_image_id")
            if isinstance(template_image_id, str) and template_image_id:
                template_raw = self._pixels.read(template_image_id)
                cache_params = dict(params)
                cache_params["template_sha256"] = template_raw.sha256
        # TASK-015 §9：OCR 把**模型内容 hash + 语言**折入缓存参数（不新增 cache_key 形参，
        # 对齐 TASK-014 的 template_sha256 手法）。Provider 可选暴露 ``cache_fingerprint``。
        fingerprint = getattr(provider, "cache_fingerprint", None)
        if callable(fingerprint):
            extra = fingerprint(model_id)
            if isinstance(extra, dict) and extra:
                cache_params = dict(cache_params)
                cache_params.update(extra)
        key = cache_key(
            command=command,
            provider_id=(manifest.provider_id if manifest is not None else "unknown"),
            model_id=model_id,
            model_version=(manifest.provider_version if manifest is not None else ""),
            algo_version=(manifest.provider_version if manifest is not None else ""),
            frame_sha256=raw.sha256,
            geometry_revision=frame.geometry_revision,
            space=frame.space,
            region=None,
            params=cache_params,
            condition_revision=None,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        token.throw_if_cancelled()
        provider_result = self._call_provider(provider, manifest, frame, params, token)
        token.throw_if_cancelled()
        normalized = self._normalize_provider_result(condition_type, provider_result)
        self._cache.put(key, normalized)
        return normalized

    def _call_provider(self, provider: Any, manifest: Any, frame: Any,
                       params: Dict[str, Any], token: Any) -> Any:
        """调用 provider；``thread_safe=false`` 时串行化（§7.3）。"""
        if manifest is not None and not manifest.thread_safe:
            with self._registry.serial_lock(provider):
                return provider.recognize(frame, params, token)
        return provider.recognize(frame, params, token)

    @staticmethod
    def _normalize_provider_result(condition_type: str, provider_result: Any) -> Dict[str, Any]:
        """归一：OCR → ``{blocks}``；匹配/颜色 → ``{matches, truncated}``（NMS+100 上限）。"""
        if condition_type == "ocr":
            return {"blocks": [block.to_dict() for block in provider_result.text_blocks]}
        deduped = nms(list(provider_result.candidates), IOU_THRESHOLD)
        kept, truncated = cap_candidates(deduped, MAX_CANDIDATES)
        return {"matches": [candidate.to_dict() for candidate in kept], "truncated": truncated}

    # -- 条件树求值路径（TASK-016 提供 evaluator） ----------------------------

    def _execute_evaluator(self, command: str, params: Dict[str, Any], token: Any) -> Dict[str, Any]:
        if self._evaluator is None:
            raise RecognitionError("CAPABILITY_DENIED", "条件树求值能力未提供（TASK-016）")
        raw = self._pixels.read(params.get("image_id"))
        frame = self._frames.resolve(self._image_ref(raw))
        force_draft = command == "test_recognition"
        if force_draft:
            snapshot: Any = params.get("draft")
            cache_params: Dict[str, Any] = {"draft": snapshot}
            condition_revision: Optional[int] = None
        else:
            if self._targets is None:
                raise RecognitionError("CAPABILITY_DENIED", "目标快照能力未提供")
            catalog = _run_catalog.get()
            if catalog is None:
                snapshot = self._targets.snapshot(params.get("target_id"))
            else:
                snapshot = self._targets.from_catalog(catalog, params.get("target_id"))
            cache_params = {"target_id": params.get("target_id")}
            condition_revision = getattr(snapshot, "revision", None)
        cache_params = dict(cache_params, origin=[frame.origin.x, frame.origin.y],
                            binding_id=frame.binding_id)
        key = cache_key(
            command=command,
            provider_id="condition_evaluator",
            model_id=None,
            model_version="",
            algo_version="",
            frame_sha256=raw.sha256,
            geometry_revision=frame.geometry_revision,
            space=frame.space,
            region=None,
            params=cache_params,
            condition_revision=condition_revision,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return dict(cached, frame_id=frame.frame_id)
        token.throw_if_cancelled()
        result = self._evaluator.evaluate(frame, snapshot, token)
        token.throw_if_cancelled()
        normalized = self._normalize_result(result, frame.frame_id, force_draft=force_draft)
        self._cache.put(key, normalized)
        return normalized

    @staticmethod
    def _normalize_result(result: Any, frame_id: str, *, force_draft: bool) -> Dict[str, Any]:
        if isinstance(result, RecognitionResult):
            rr = result
        elif isinstance(result, dict):
            rr = validate_recognition_result(result)
        else:
            raise RecognitionError("RECOGNITION_FAILED", "条件求值返回类型非法")
        if force_draft:
            rebuilt = build_recognition_result(
                target_id=None, sample_no=None, target_revision=None,
                frame_id=frame_id or rr.frame_id, matches=tuple(rr.matches),
                elapsed_ms=rr.elapsed_ms, truncated=rr.truncated,
            )
        else:
            rebuilt = build_recognition_result(
                target_id=rr.target_id, sample_no=rr.sample_no, target_revision=rr.target_revision,
                frame_id=frame_id or rr.frame_id, matches=tuple(rr.matches),
                elapsed_ms=rr.elapsed_ms, truncated=rr.truncated,
            )
        return rebuilt.to_dict()

    @staticmethod
    def _image_ref(raw: Any) -> ImageRef:
        return ImageRef(
            image_id=raw.image_id,
            width=raw.width,
            height=raw.height,
            format=raw.format,
            sha256=raw.sha256,
        )

    # -- 终态（写 JobManager 后发事件；首个终态写入者发事件） -----------------

    def _conclude_completed(self, job_id: str, result: Dict[str, Any]) -> None:
        if self._jobs.complete(job_id, result):
            self._emitter.emit_completed(job_id=job_id, result=result)
            return
        self._emit_cancelled_if_terminal(job_id)

    def _conclude_error(self, job_id: str, error: Dict[str, Any]) -> None:
        if self._jobs.fail(job_id, error):
            self._emitter.emit_error(job_id=job_id, error=error)
            return
        self._emit_cancelled_if_terminal(job_id)

    def _conclude_timeout(self, job_id: str) -> None:
        """超时：丢弃结果 + ``worker.restart()`` + ``RECOGNITION_TIMEOUT``（retryable=true）。"""
        error = RecognitionError(
            "RECOGNITION_TIMEOUT", "单次推理超过 %dms 上限" % self._deadline_ms, retryable=True
        ).to_dict()
        if self._jobs.fail(job_id, error):
            self._worker.restart()
            self._emitter.emit_error(job_id=job_id, error=error)
            return
        self._emit_cancelled_if_terminal(job_id)

    def _conclude_cancel(self, job_id: str) -> None:
        """取消：确保终态 ``cancelled`` 并发 ``recognition_cancelled``（仅一次）。"""
        snapshot = self._jobs.get(job_id)
        status = snapshot.get("status") if isinstance(snapshot, dict) else None
        if status in ("completed", "error"):
            return  # 其它终态已赢得竞态，不发 cancelled
        if status in ("pending", "running"):
            self._jobs.cancel(job_id)
        self._emitter.emit_cancelled(job_id=job_id)

    def _emit_cancelled_if_terminal(self, job_id: str) -> bool:
        """若当前终态为 ``cancelled`` → 发 ``recognition_cancelled``（返回是否发送）。"""
        snapshot = self._jobs.get(job_id)
        if isinstance(snapshot, dict) and snapshot.get("status") == "cancelled":
            self._emitter.emit_cancelled(job_id=job_id)
            return True
        return False

    # -- 收尾 ----------------------------------------------------------------

    def close(self) -> None:
        """收尾：关闭 provider、尽力 join worker 线程（幂等）。"""
        if self._closed:
            return
        self._closed = True
        self._registry.close_all()
        self._worker.shutdown(timeout=5.0)
