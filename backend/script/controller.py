"""Root execution lifecycle. Each worker owns its Lua VMs and one immutable catalog snapshot."""
import copy
import json
import threading
import time
from collections import OrderedDict, deque
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from ..scripts import domain
from ..storage.errors import StorageError
from ..types.runtime import validate_run_config
from .api import ScriptApi
from .cancel import CancelFlag, CheckpointCancelled, ExecutionGate
from .call_stack import arguments, call

TERMINAL = {"completed", "cancelled", "failed"}
DEFAULT_RUN = dict(args={}, mode="once", debug=False, region=None, display_id=None,
                   binding_id=None, allow_exit=False, frequency_hz=1, total_timeout_ms=0)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def error_data(exc):
    return dict(code=getattr(exc, "code", "SCRIPT_RUNTIME_ERROR"),
                message=getattr(exc, "message", str(exc)), details=dict(getattr(exc, "details", {}) or {}),
                retryable=bool(getattr(exc, "retryable", False)))


class RootGate(ExecutionGate):
    def __init__(self, root):
        self.root = root
        super().__init__(cancel=CancelFlag(), deadline_ms=root.config.total_timeout_ms or None,
                         monotonic=root.active_time, _start=0.0)

    def checkpoint(self):
        with self.root.changed:
            while self.is_paused() and not self.is_cancelled():
                self.root.changed.wait(.05)
            super().checkpoint()

    def throw_if_cancelled(self):
        self.checkpoint()


class Root:
    def __init__(self, controller, catalog, config, settings):
        self.controller = controller
        self.changed = controller.changed
        self.id = str(uuid4())
        self.config, self.settings, self.catalog = config, settings, catalog
        self.scripts = {s["id"]: s for s in domain.scripts_from_snapshot(catalog)}
        self.refs = {config.script_id, config.stop_target_id} - {None}
        self.start = time.monotonic()
        self.pause_generation = 0
        self.paused_at, self.paused_total = None, 0.
        self.gate = RootGate(self)
        self.status, self.reason = "starting", None
        self.started_at = self.updated_at = stamp()
        self.iteration, self.frequency = 0, config.frequency_hz
        self.effective_hz, self.throttled = None, False
        self.result, self.error, self.lease = None, None, None
        self.images, self.stack, self.logs = {}, [], deque(maxlen=1000)
        self.released_images = OrderedDict()
        self.api = ScriptApi(self)

    def active_time(self):
        until = self.paused_at if self.paused_at is not None else time.monotonic()
        return until - self.start - self.paused_total

    def reference(self, entity):
        with self.changed:
            self.refs.add(entity)

    def state(self):
        with self.changed:
            return dict(execution_id=self.id, kind="script", status=self.status, reason=self.reason,
                        script_id=self.config.script_id, iteration=self.iteration, current_line=None,
                        started_at=self.started_at, updated_at=self.updated_at, result=copy.deepcopy(self.result),
                        error=copy.deepcopy(self.error), effective_hz=self.effective_hz, throttled=self.throttled)

    def log(self, level, event, message, **details):
        with self.changed:
            self.logs.append(dict(timestamp=stamp(), execution_id=self.id, level=level, event=event,
                                  message=message, details=details))

    def transition(self, status, reason=None):
        # Caller holds the root condition shared by control and completion.
        self.status, self.reason, self.updated_at = status, reason, stamp()
        self.log("info", "script_state_changed", status, reason=reason)
        self.controller.emit("script_state_changed", dict(execution_id=self.id, status=status, reason=reason))
        self.changed.notify_all()

    def pause(self, reason):
        with self.changed:
            if self.status not in ("starting", "running"):
                raise StorageError("INVALID_STATE", "执行当前不能暂停")
            self.paused_at = time.monotonic()
            self.pause_generation += 1
            self.gate.pause.pause()
            self.transition("paused", reason)

    def suspend_window(self):
        with self.changed:
            if self.status in ("running", "starting"):
                self.pause("window_unavailable")
        self.gate.checkpoint()

    def resume(self):
        with self.changed:
            if self.status != "paused":
                raise StorageError("INVALID_STATE", "执行没有暂停")
        if self.config.binding_id or self.config.region or self.controller.capture:
            self.api.screen.region()
        with self.changed:
            if self.status != "paused":
                raise StorageError("INVALID_STATE", "执行状态已改变")
            self.paused_total += time.monotonic() - self.paused_at
            self.paused_at = None
            self.gate.pause.resume()
            self.transition("running")

    def stop(self, reason):
        with self.changed:
            if self.status not in TERMINAL and self.status != "stopping":
                self.gate.cancel.cancel()
                self.transition("stopping", reason)

    def fail(self, exc):
        with self.changed:
            if self.status not in TERMINAL and self.status != "stopping":
                self.error = error_data(exc)
                self.stop(self.error["code"])

    def wait(self, seconds):
        until = self.active_time() + seconds
        if not seconds:
            time.sleep(0)
        with self.changed:
            while True:
                self.gate.checkpoint()
                remaining = until - self.active_time()
                if remaining <= 0:
                    return
                self.changed.wait(min(.05, remaining))

    def call(self, script_id, args=None):
        return call(self, script_id, args)

    def run(self):
        result, error, reason = None, None, None
        try:
            self.gate.checkpoint()
            if self.controller.input is not None:
                cfg = asdict(self.config)
                ctx = {k: cfg[k] for k in ("region", "display_id", "binding_id", "allow_exit")}
                self.lease = self.controller.input.acquire_lease(self.id, ctx)
            if self.controller.capture or self.config.binding_id or self.config.region:
                self.api.guarded(self.api.screen.region)()
            with self.changed:
                self.gate.checkpoint()
                if self.status == "starting":
                    self.transition("running")
            next_tick = self.active_time()
            last_tick = None
            while True:
                self.gate.checkpoint()
                if self.config.mode == "until_target":
                    self.wait(max(0, next_tick - self.active_time()))
                    now = self.active_time()
                    if last_tick is not None and now > last_tick:
                        self.effective_hz = 1 / (now - last_tick)
                        self.throttled = self.effective_hz < self.frequency * .95
                    last_tick = now
                    next_tick = now + 1 / self.frequency
                    def sample():
                        with self.api.screen.frame() as frame:
                            return self.api.screen.target(self.config.stop_target_id, frame.image_ref.image_id)
                    detection = self.api.guarded(sample)()
                    if detection["found"]:
                        self.controller.emit("target_detected", dict(execution_id=self.id, result=detection))
                        reason = "target_detected"
                        break
                result = self.call(self.config.script_id, self.config.args)
                self.gate.checkpoint()
                with self.changed:
                    self.iteration += 1
                    self.controller.emit("script_iteration", {"execution_id": self.id, "iteration": self.iteration})
                if self.config.mode == "once" or self.config.mode == "count" and self.iteration >= self.config.iterations:
                    break
        except CheckpointCancelled:
            pass
        except Exception as exc:
            error = error_data(exc)
        finally:
            for image_id in self.images:
                try:
                    self.controller.capture.release_image(image_id)
                except Exception as exc:
                    if getattr(exc, "code", None) != "IMAGE_EXPIRED":
                        error = error or error_data(exc)
            if self.lease:
                try:
                    self.controller.input.release_lease(self.id)
                except Exception as exc:
                    error = error or error_data(exc)
            with self.changed:
                if self.status == "stopping":
                    error = self.error
                    terminal = "failed" if error else "cancelled"
                    reason = self.reason
                elif self.gate.deadline_exceeded():
                    terminal, reason = "failed", "timeout"
                    error = error_data(StorageError("SCRIPT_TIMEOUT", "脚本已达到根期限"))
                else:
                    terminal = "failed" if error else "completed"
                self.error = error
                self.result = result if terminal == "completed" else None
                self.images.clear()
                self.transition(terminal, reason)
                self.controller.retire(self)


class ExecutionController:
    def __init__(self, scripts, capture, vision, input_proxy, settings, emit, beep):
        self.scripts, self.capture, self.vision, self.input = scripts, capture, vision, input_proxy
        self.settings, self.emit, self.beep = settings, emit, beep
        self.changed = threading.Condition(threading.RLock())
        self.roots, self.clients, self.history = {}, {}, OrderedDict()

    def referenced(self, entity):
        with self.changed:
            return any(entity in r.refs for r in self.roots.values())

    def execute(self, params):
        raw = dict(params)
        client = raw.pop("client_run_id", None)
        if client is not None and (not isinstance(client, str) or not client):
            raise StorageError("INVALID_PARAMS", "client_run_id 必须为非空字符串", field="client_run_id")
        config = validate_run_config(dict(DEFAULT_RUN, **raw))
        signature = json.dumps(asdict(config), ensure_ascii=False, sort_keys=True, allow_nan=False)
        settings = self.settings()
        catalog = self.scripts.repository.read_snapshot()
        with self.changed:
            if client in self.clients:
                previous, root_id = self.clients[client]
                if signature != previous:
                    raise StorageError("INVALID_PARAMS", "client_run_id 已用于其他配置", field="client_run_id")
                return {"execution_id": root_id, "status": "starting"}
            if len(self.roots) >= 3:
                raise StorageError("BUSY", "已有三个活动根执行", retryable=True)
            root = Root(self, catalog, config, settings)
            arguments(domain.ensure_script_found(root.scripts.get(config.script_id), config.script_id), config.args)
            if config.stop_target_id and not catalog.target_by_id(config.stop_target_id):
                raise StorageError("TARGET_NOT_FOUND", "停止目标不存在", field="stop_target_id")
            self.roots[root.id] = root
            if client:
                self.clients[client] = signature, root.id
            root.transition("starting")
            threading.Thread(target=root.run, name="lua-" + root.id, daemon=True).start()
            return {"execution_id": root.id, "status": "starting"}

    def retire(self, root):
        self.history[root.id] = (root.state(), list(root.logs))
        self.roots.pop(root.id, None)
        while len(self.history) > 100:
            self.history.popitem(last=False)

    def lookup(self, params, query=False):
        execution, client = params.get("execution_id"), params.get("client_run_id")
        if query and (execution is None) == (client is None):
            raise StorageError("INVALID_PARAMS", "必须指定一个执行或客户端 ID")
        if query and client is not None and isinstance(client, str):
            execution = self.clients.get(client, (None, None))[1]
        if not isinstance(execution, str) or execution not in self.roots and execution not in self.history:
            raise StorageError("INVALID_STATE", "执行记录不存在", field="execution_id")
        return execution

    def command(self, name, params):
        if name == "execute_script":
            return self.execute(params)
        allowed = {"execution_id"}
        if name == "query_execution":
            allowed.add("client_run_id")
        elif name == "get_execution_logs":
            allowed.update(("offset", "limit"))
        extra = next((key for key in params if key not in allowed), None)
        if extra:
            raise StorageError("INVALID_PARAMS", "存在未声明的参数", field=extra)
        with self.changed:
            execution = self.lookup(params, name == "query_execution")
            root = self.roots.get(execution)
            if name == "get_execution_logs":
                offset, limit = params.get("offset", 0), params.get("limit", 100)
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
                    raise StorageError("INVALID_PARAMS", "日志分页无效")
                logs = list(root.logs) if root else self.history[execution][1]
                return {"items": copy.deepcopy(logs[offset:offset + limit]), "total": len(logs)}
            if name in ("query_execution", "get_script_state") or root is None and name == "stop_script":
                return {"state": root.state() if root else copy.deepcopy(self.history[execution][0])}
            if root is None:
                raise StorageError("INVALID_STATE", "已结束的执行无法更改")
            if name == "pause_script":
                root.pause("user_requested")
            elif name == "stop_script":
                root.stop("user_requested")
        if name == "resume_script":
            root.resume()
        return {"state": root.state()}

    def stop_all(self, reason="hotkey"):
        with self.changed:
            for root in list(self.roots.values()):
                root.stop(reason)
        return {"ok": True}

    def pause_for_selection(self):
        with self.changed:
            roots = [r for r in self.roots.values() if r.status in ("running", "starting")]
            for root in roots:
                root.pause("selection")
            return bool(roots)

    def binding_changed(self, params):
        with self.changed:
            for root in list(self.roots.values()):
                if root.config.binding_id != params.get("binding_id"):
                    continue
                if params.get("state") == "closed":
                    root.fail(StorageError("WINDOW_NOT_FOUND", "绑定窗口已关闭"))
                elif root.status in ("running", "starting") and params.get("state") in ("minimized", "foreground_lost"):
                    root.pause("window_unavailable")
        return {"ok": True}
