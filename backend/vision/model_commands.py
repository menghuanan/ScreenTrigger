"""Session-scoped model previews; copying and verification never block the IPC reader."""
import threading
import uuid

from backend.ipc.dispatcher import DispatcherError
from backend.storage.errors import StorageError
from .cancel import CancelFlag, CancelledError, JobBoundCancelToken
from .model_library import local_path
from .types import RecognitionError


def references(model, config, scripts, cancel):
    refs = []
    if config and config.load()["recognition"]["ocr_model_id"] == model:
        refs.append({"kind": "config", "id": "recognition.ocr_model_id"})
    if scripts is not None:
        snapshot = scripts.repository.read_snapshot()

        def includes(value):
            if isinstance(value, dict):
                return value.get("model_id") == model or any(includes(item) for item in value.values())
            return isinstance(value, list) and any(includes(item) for item in value)

        for target in snapshot.targets:
            if includes(target.get("conditions")):
                refs.append({"kind": "target", "id": target["id"]})
        for script in (snapshot.script_metadata or {}).get("scripts", []):
            cancel.throw_if_cancelled()
            if model in scripts.source_for(script):
                refs.append({"kind": "script", "id": script["id"]})
    return refs


def register_model_commands(dispatcher, jobs, provider, config, scripts):
    previews = {}
    worker = threading.BoundedSemaphore()
    closing = CancelFlag()
    def invoke(name, envelope):
        params = envelope["params"]
        if name == "list_ocr_models":
            if params:
                raise DispatcherError("INVALID_PARAMS", "查询无参数", {}, False)
            return provider.snapshot()
        if name == "preview_ocr_model_change":
            operation = params.get("operation")
            keys = {"operation", "model_id", "directory"} if operation == "import" else {"operation", "model_id"}
            if (set(params) != keys or operation not in ("import", "remove")
                    or params["model_id"] not in provider.catalog.recognition_models()
                    or (operation == "import" and not isinstance(params["directory"], str))):
                raise DispatcherError("INVALID_PARAMS", "变更类型、管线或本地目录无效", {}, False)
        elif (set(params) != {"preview_id", "expected_revision"}
              or not isinstance(params["preview_id"], str) or type(params["expected_revision"]) is not int):
            raise DispatcherError("INVALID_PARAMS", "需要预览编号及修订", {}, False)
        if not worker.acquire(blocking=False):
            raise DispatcherError("BUSY", "另一项模型操作正在进行", {}, True)
        ident = jobs.create_job(name)
        cancel = JobBoundCancelToken(ident, jobs, closing)

        def run():
            try:
                if not jobs.mark_running(ident):
                    return
                if name == "preview_ocr_model_change":
                    model = params["model_id"]
                    directory = params.get("directory")
                    if params["operation"] == "import" and not local_path(directory).is_dir():
                        raise RecognitionError("MODEL_UNAVAILABLE", "找不到所选模型介质目录")
                    snap = provider.snapshot()
                    row = next(r for r in snap["pipelines"] if r["model_id"] == model)
                    preview_id = uuid.uuid4().hex
                    result = dict(row, preview_id=preview_id, revision=snap["revision"],
                                  references=references(model, config, scripts, cancel), dynamic_references_unknown=True)
                    cancel.throw_if_cancelled()
                    previews[preview_id] = (model, params["operation"], directory, snap["revision"])
                    if len(previews) > 32:
                        del previews[next(iter(previews))]
                else:
                    details = previews.get(params["preview_id"])
                    if details is None or details[3] != params["expected_revision"]:
                        raise RecognitionError("CONFLICT", "预览已失效，请重新预览")
                    result = provider.change(*details, cancel, lambda: jobs.begin_commit(ident))
                jobs.complete(ident, result)
            except CancelledError:
                jobs.cancel(ident)
            except (RecognitionError, StorageError) as exc:
                jobs.fail(ident, exc.to_dict())
            except OSError as exc:
                jobs.fail(ident, RecognitionError("STORAGE_ERROR", str(exc)).to_dict())
            except Exception as exc:
                jobs.fail(ident, RecognitionError("INTERNAL_ERROR", str(exc)).to_dict())
            finally:
                worker.release()

        try:
            threading.Thread(target=run, name="ocr-model-change", daemon=True).start()
        except RuntimeError:
            worker.release()
            jobs.fail(ident, RecognitionError("RESOURCE_LIMIT", "无法启动模型作业").to_dict())
        return {"job_id": ident}

    for name in ("list_ocr_models", "preview_ocr_model_change", "apply_ocr_model_change"):
        dispatcher.register(name, lambda envelope, name=name: invoke(name, envelope))

    def close():
        closing.cancel()
        with worker:
            previews.clear()
    return close
