"""Bounded catalog worker. The IPC reader remains available to stop the engine."""
from contextlib import nullcontext
import threading

from backend.ipc.dispatcher import DispatcherError
from .backup import BackupService
from .errors import StorageError


def register_backup_commands(dispatcher, jobs, repository, controller, emit):
    service = BackupService(repository, idle=controller.idle_catalog if controller else nullcontext)
    worker = threading.BoundedSemaphore(1)
    actions = {
        "list_catalog_backups": (service.list, ()),
        "create_catalog_backup": (service.create, ()),
        "preview_catalog_restore": (service.preview, ("backup_id",)),
        "restore_catalog_backup": (service.restore, ("backup_id", "expected_current", "expected_backup")),
    }

    def handler(name, operation, fields):
        def invoke(envelope):
            params = envelope["params"]
            if set(params) != set(fields) or any(not isinstance(params[k], str) for k in fields):
                raise DispatcherError("INVALID_PARAMS", "目录操作参数无效", {}, False)
            if name == "list_catalog_backups":
                try:
                    return service.list()
                except StorageError as exc:
                    raise DispatcherError(exc.code, exc.message, exc.details, exc.retryable) from exc
            if not worker.acquire(blocking=False):
                raise DispatcherError("BUSY", "另一项备份或恢复操作正在进行", {}, True)
            ident = jobs.create_job(name, cancellable_while_running=False)

            def run():
                try:
                    if not jobs.mark_running(ident):
                        return
                    result = operation(**params)
                    jobs.complete(ident, result)
                    if name == "restore_catalog_backup":
                        emit("catalog_changed", {"generation": result["generation"], "revision": 1})
                except StorageError as exc:
                    jobs.fail(ident, exc.to_dict())
                except Exception as exc:
                    jobs.fail(ident, StorageError("INTERNAL_ERROR", str(exc)).to_dict())
                finally:
                    worker.release()
            try:
                threading.Thread(target=run, name="catalog-backup", daemon=True).start()
            except RuntimeError:
                worker.release()
                jobs.fail(ident, StorageError("RESOURCE_LIMIT", "无法启动存储作业").to_dict())
            return {"job_id": ident}
        return invoke

    for name, (operation, fields) in actions.items():
        dispatcher.register(name, handler(name, operation, fields))
