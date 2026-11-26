"""A private lazy worker per installed OCR pipeline, shared by one engine session."""
from dataclasses import dataclass, replace
from contextvars import ContextVar
from importlib.util import find_spec
from pathlib import Path
import threading
from backend import startup_timing
from backend.storage.lock import engine_session

from .cancel import CancelFlag, CancelledError
from .manifest import load_model_manifest, model_manifest_path
from .model_library import ModelLibrary, local_path
from .process_provider import ProcessProvider
from .providers.ocr import OCREngineProvider
from .providers.ocr_models import format_ocr_reason, resolve_library_root, seed_repo_model_metadata
from .types import RecognitionError
from .worker import INFERENCE_DEADLINE_MS


@dataclass(eq=False)
class Pipeline:
    model: str
    source: str | dict
    worker: ProcessProvider
    state: str = "installed"
    error: RecognitionError | None = None
    users: int = 0
    pins: int = 0
    retired: bool = False
    startup: threading.Thread | None = None
    cancel: CancelFlag | None = None


class ManagedOCR:
    def __init__(self, pixels, metadata, library_root):
        self.pixels = pixels
        # An explicit metadata directory can be shared by otherwise distinct data roots.
        self._library_session = engine_session(local_path(Path(metadata) / "library"))
        self._library_session.__enter__()
        try:
            self._open(metadata, library_root)
        except BaseException:
            self._library_session.__exit__(None, None, None)
            raise

    def _open(self, metadata, library_root):
        self.library = ModelLibrary(metadata, resolve_library_root(library_root)[0])
        self.catalog = self.library.catalog
        seed_repo_model_metadata(str(self.library.metadata))
        self.condition = threading.Condition()
        self.changes = threading.Lock()
        self.closed = False
        self.running = {}
        self.admitted = ContextVar("ocr_admission", default=None)
        self.retired = set()
        self.base = OCREngineProvider(self.pixels).capabilities()
        self.runtime_installed = all(find_spec(name) is not None for name in ("paddleocr", "paddle", "numpy"))
        for model, source in self.library.selection["pipelines"].items():
            if source is not None:
                self.running[model] = self._slot(model, source)

    def _slot(self, model, source):
        roots = self.library.roots(model, source)
        worker = ProcessProvider(self.pixels, OCREngineProvider, model_id=model, inference_deadline_ms=INFERENCE_DEADLINE_MS,
                                 library_root=str(self.library.factory), component_roots=roots)
        slot = Pipeline(model, source, worker)
        for component, root in roots.items():
            for item in self.catalog.entry(component).files:
                path = local_path(Path(root) / component / item.name)
                if not path.is_file() or path.stat().st_size != item.bytes:
                    slot.state = "damaged"
                    slot.error = RecognitionError("DATA_CORRUPT", f"模型文件缺失或长度不符: {component}/{item.name}")
                    return slot
        return slot

    def capabilities(self):
        return replace(self.base, thread_safe=True)

    def installed_manifests(self):
        with self.condition:
            ids = [model for model, slot in self.running.items() if slot.state != "damaged" and self.runtime_installed]
        return tuple(load_model_manifest(model_manifest_path(str(self.library.metadata), model)) for model in ids)

    def snapshot(self):
        with self.condition:
            rows = []
            for engine in self.catalog.engines:
                model = engine.recognition_model
                slot = self.running.get(model)
                parts = self.library.components(model)
                rows.append(dict(model_id=model, languages=list(engine.languages), components=list(parts),
                                 bytes=sum(f.bytes for c in parts for f in self.catalog.entry(c).files),
                                 state=(slot.worker.process_state if slot and slot.state == "ready"
                                        else slot.state if slot else "missing"),
                                 error=slot.error.to_dict() if slot and slot.error else None))
            return {"revision": self.library.selection["revision"], "pipelines": rows}

    def capability_reason(self):
        rows = [row for row in self.snapshot()["pipelines"] if row["state"] not in ("missing", "damaged")]
        if rows and self.runtime_installed:
            return "available", None
        code = "MODEL_UNAVAILABLE" if self.runtime_installed else "ENGINE_UNAVAILABLE"
        return "unavailable", format_ocr_reason(state="unavailable", code=code,
                    models=[r["model_id"] for r in rows], verified=sorted({s for r in rows for s in r["languages"]}),
                    models_dir=str(self.library.metadata), library=str(self.library.factory))

    def _require(self, model):
        if self.closed:
            raise CancelledError("OCR 会话已关闭")
        admission = self.admitted.get()
        slot = (admission[1] if admission is not None else self.running).get(model)
        if slot is None:
            raise RecognitionError("MODEL_UNAVAILABLE", f"模型未安装: {model}", "model_id")
        if slot.error:
            raise slot.error
        if not self.runtime_installed:
            raise RecognitionError("ENGINE_UNAVAILABLE", "本地 OCR 推理依赖不可用")
        return slot

    def pin(self, model=None):
        """Hold the admitted model revision while the job is queued or running."""
        with self.condition:
            slots = {key: slot for key, slot in self.running.items() if model is None or key == model}
            for slot in slots.values():
                slot.pins += 1
            return self.library.selection["revision"], slots

    def unpin(self, admission):
        with self.condition:
            release = []
            for slot in admission[1].values():
                slot.pins -= 1
                if slot.retired and not slot.pins and not slot.users:
                    release.append(slot)
        for slot in release:
            self._release(slot)

    def revision(self):
        admission = self.admitted.get()
        return admission[0] if admission is not None else self.library.selection["revision"]

    def cache_fingerprint(self, model):
        with self.condition:
            self._require(model)
            return {"model_sha256": self.catalog.weight_sha256(model), "model_revision": self.revision()}

    def _initialize(self, slot, operation):
        timing_binding = startup_timing.current_operation.set(operation)
        try:
            slot.worker.initialize(str(self.library.metadata), slot.cancel)
            state, error = "ready", None
        except CancelledError:
            state, error = "installed", None
        except Exception as exc:
            state = "damaged"
            error = exc if isinstance(exc, RecognitionError) else RecognitionError("ENGINE_UNAVAILABLE", str(exc))
        finally:
            startup_timing.current_operation.reset(timing_binding)
            with self.condition:
                slot.state, slot.error = state, error
                slot.startup = None
                self.condition.notify_all()

    def recognize(self, frame, params, cancel, roi=None):
        with self.condition:
            slot = self._require(params.get("model_id"))
            slot.users += 1
        try:
            with self.condition:
                while slot.state != "ready":
                    cancel.throw_if_cancelled()
                    if self.closed:
                        raise CancelledError("OCR 会话已关闭")
                    if slot.error:
                        raise slot.error
                    if slot.startup is None:
                        slot.state, slot.cancel = "loading", CancelFlag()
                        slot.startup = threading.Thread(target=self._initialize,
                            args=(slot, startup_timing.current_operation.get()), daemon=True)
                        slot.startup.start()
                    self.condition.wait(.02)
            return slot.worker.recognize(frame, params, cancel, roi)
        finally:
            with self.condition:
                slot.users -= 1
                if not slot.users and slot.startup:
                    slot.cancel.cancel()
                release = slot.retired and not slot.users and not slot.pins
            if release:
                self._release(slot)

    def _release(self, slot):
        slot.worker.close()
        with self.changes, self.condition:
            self.retired.discard(slot)
            self._collect()

    def _collect(self):
        keep = self.library.object_refs(self.library.selection)
        for slot in self.retired:
            if isinstance(slot.source, dict):
                keep.update(slot.source.values())
        self.library.collect(keep)

    def change(self, model, operation, directory, revision, cancel, begin_commit):
        with self.changes:
            if revision != self.library.selection["revision"]:
                raise RecognitionError("CONFLICT", "修订已改变，请重新预览", "expected_revision")
            old = self.running.get(model)
            replacement = None
            try:
                if operation == "import":
                    source = self.library.prepare(model, directory, cancel)
                    replacement = self._slot(model, source)
                    replacement.worker.initialize(str(self.library.metadata), cancel)
                    replacement.state = "ready"
                cancel.throw_if_cancelled()
                with self.condition:
                    if self.closed:
                        raise CancelledError("OCR 会话已关闭")
                    if old and replacement and old.source == replacement.source and old.state != "damaged":
                        return self.snapshot()
                    if not begin_commit():
                        raise CancelledError("模型变更已取消")
                    self.library.commit(model, replacement.source if replacement else None, revision)
                    if old:
                        old.retired = True
                        self.retired.add(old)
                    self.running.pop(model, None)
                    if replacement:
                        self.running[model] = replacement
                        replacement = None
                    dispose = old is not None and not old.users and not old.pins
                if dispose:
                    old.worker.close()
                    with self.condition:
                        self.retired.discard(old)
                return self.snapshot()
            finally:
                if replacement:
                    replacement.worker.close()
                with self.condition:
                    self._collect()

    def close(self):
        with self.condition:
            self.closed = True
            slots = set(self.running.values()) | self.retired
            for slot in slots:
                if slot.cancel:
                    slot.cancel.cancel()
            self.condition.notify_all()
        for slot in slots:
            slot.worker.close()
        self._library_session.__exit__(None, None, None)
