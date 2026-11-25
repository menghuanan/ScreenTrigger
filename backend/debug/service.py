"""Hash-bound breakpoints and worker-owned, read-only Lua frame snapshots."""
import copy
import time
from pathlib import Path
from uuid import uuid4

from ..storage.errors import StorageError
from .lines import executable_lines

HOOK = Path(__file__).with_name("hook.lua").read_text(encoding="utf-8")
STEPS = {"step_into", "step_over", "step_out", "continue_execution"}
BREAKPOINTS = {"add_breakpoint", "remove_breakpoint", "list_breakpoints"}


def fields(params, names):
    for key in params:
        if key not in names:
            raise StorageError("INVALID_PARAMS", "存在未声明的参数", field=key)
    for key in names:
        value = params.get(key)
        if key == "line":
            if type(value) is int and value >= 1:
                continue
        elif isinstance(value, str) and value:
            continue
        raise StorageError("INVALID_PARAMS", "缺少有效的调试参数", field=key)


class Breakpoints:
    def __init__(self, controller):
        self.controller = controller
        self.points = {}

    def command(self, name, params):
        if name == "remove_breakpoint":
            fields(params, ("breakpoint_id",))
            with self.controller.changed:
                self.points.pop(params["breakpoint_id"], None)
            return {"ok": True}
        fields(params, ("script_id",) if name == "list_breakpoints" else ("script_id", "source_hash", "line"))
        script_id = params["script_id"]
        document = self.controller.scripts.load_script(script_id)
        digest = document["script"]["source_sha256"]
        line = params.get("line")
        if name == "add_breakpoint":
            if params["source_hash"] != digest:
                raise StorageError("CONFLICT", "源码已修改，请重新打开脚本", field="source_hash")
            if line not in executable_lines(document["source"]):
                raise StorageError("INVALID_PARAMS", "所选行没有可执行指令", field="line")
        with self.controller.changed:
            records = [point for point in self.points.values() if point["script_id"] == script_id]
            for point in records:
                point["enabled"] = point["source_hash"] == digest
            if name == "list_breakpoints":
                return {"items": copy.deepcopy(records)}
            for point in records:
                if point["enabled"] and point["line"] == line:
                    return {"breakpoint": dict(point)}
            key = str(uuid4())
            self.points[key] = dict(id=key, script_id=script_id, source_hash=digest, line=line, enabled=True)
            return {"breakpoint": dict(self.points[key])}

    def hits(self, script, line):
        return any(p["enabled"] and p["script_id"] == script["id"] and p["source_hash"] == script["source_sha256"]
                   and p["line"] == line for p in self.points.values())


def variable(item):
    value = item["value"]
    if item["type"] == "table" and value is not None:
        value = [variable(value[i]) for i in range(1, len(value) + 1)]
    row = dict(name=item["name"], type=item["type"], value=value, truncated=bool(item["truncated"]))
    if item["reference_id"] is not None:
        row["reference_id"] = item["reference_id"]
    return row


def frames(snapshot, script):
    result = []
    for i in range(1, len(snapshot) + 1):
        frame = snapshot[i]
        items = frame["variables"]
        variables = [variable(items[k]) for k in range(1, len(items) + 1)]
        def limited(row):
            return row["truncated"] or isinstance(row["value"], list) and any(limited(v) for v in row["value"])
        result.append((dict(frame_id=str(uuid4()), script_id=script["id"], line=frame["line"],
                            source_hash=script["source_sha256"]),
                       dict(variables=variables, truncated=bool(frame["truncated"]) or any(limited(v) for v in variables))))
    return result


class DebugSession:
    def __init__(self, root):
        self.root = root
        self.frames = []
        self.parents = []
        self.snapshotters = []
        self.step = None
        self.depth = 0

    def install(self, vm, script):
        if self.snapshotters:
            parent, snapshotter = self.snapshotters[-1]
            self.parents.append(frames(snapshotter(), parent))
        parent_depth = sum(len(group) for group in self.parents)
        def wanted(line, depth):
            with self.root.changed:
                if self.root.status != "running":
                    return None
                total = parent_depth + depth
                if self.root.controller.breakpoints.hits(script, line):
                    reason = "breakpoint_hit"
                elif (self.step == "step_into" or self.step == "step_over" and total <= self.depth
                      or self.step == "step_out" and total < self.depth):
                    reason = "step_completed"
                else:
                    return None
                self.depth = total
                return reason

        def pause(reason, snapshot):
            root = self.root
            with root.changed:
                if root.status != "running" or root.gate.is_cancelled():
                    return root.gate.is_cancelled()
                self.step = None
                self.frames = frames(snapshot, script)
                for group in reversed(self.parents):
                    self.frames.extend(group)
                top = self.frames[0][0]
                root.paused_at = time.monotonic()
                root.pause_generation += 1
                root.gate.pause.pause()
                root.transition("debug_paused", reason)
                root.controller.emit(reason, dict(execution_id=root.id, frame_id=top["frame_id"],
                                                  script_id=top["script_id"], line=top["line"]))
                root.changed.wait_for(lambda: root.status != "debug_paused")
                return root.gate.is_cancelled()

        from ..script.lua_vm import _sanitize_chunkname
        snapshotter = vm.interop.eval(HOOK)("@" + _sanitize_chunkname(script["id"]), vm._make_hook_check(),
                                             wanted, pause, vm.interop.cancel_sentinel, vm.count_hook_interval)
        self.snapshotters.append((script, snapshotter))

    def leave(self):
        self.snapshotters.pop()
        if self.parents:
            self.parents.pop()

    def inspect(self, name, params):
        if self.root.status != "debug_paused":
            raise StorageError("INVALID_STATE", "请在调试暂停时查看或单步")
        if name == "get_stack":
            return {"frames": [dict(frame) for frame, _ in self.frames]}
        for frame, values in self.frames:
            if params["frame_id"] == frame["frame_id"]:
                return copy.deepcopy(values)
        raise StorageError("INVALID_STATE", "变量快照已失效", field="frame_id")

    def resume(self, name):
        root = self.root
        with root.changed:
            if root.status != "debug_paused":
                raise StorageError("INVALID_STATE", "执行未在断点暂停")
            frame_id = self.frames[0][0]["frame_id"]
        if root.controller.capture or root.config.binding_id or root.config.region:
            root.api.screen.region()
        with root.changed:
            if root.status != "debug_paused" or self.frames[0][0]["frame_id"] != frame_id:
                raise StorageError("INVALID_STATE", "调试状态已改变")
            self.step = None if name == "continue_execution" else name
            self.frames = []
            root.paused_total += time.monotonic() - root.paused_at
            root.paused_at = None
            root.gate.pause.resume()
            root.transition("running")
            return {"state": root.state()}
