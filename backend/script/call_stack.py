"""Resolve a child against the root snapshot and run it in an isolated Lua VM."""
import copy
import re

from ..scripts import domain
from ..storage.errors import StorageError
from .lua_vm import LuaVm


def arguments(script, values):
    if not isinstance(values, dict):
        raise StorageError("INVALID_PARAMS", "脚本参数必须为对象", field="args")
    declarations = {p["name"]: p for p in script["parameters"]}
    if values.keys() - declarations.keys():
        raise StorageError("INVALID_PARAMS", "包含未声明的参数", field="args")
    result = {}
    for key, declaration in declarations.items():
        value = values.get(key, declaration.get("default"))
        if value is None:
            if declaration["required"]:
                raise StorageError("INVALID_PARAMS", "缺少必需参数", field="args." + key)
        elif not domain.parameter_type_matches(value, declaration["type"]):
            raise StorageError("INVALID_PARAMS", "参数类型不匹配", field="args." + key)
        if value is not None or key in values:
            result[key] = copy.deepcopy(value)
    return result


def call(root, script_id, args=None):
    if not isinstance(script_id, str) or not script_id:
        raise StorageError("INVALID_PARAMS", "script_id 必须为非空 ID", field="script_id")
    if len(root.stack) >= 5:
        raise StorageError("CALL_DEPTH_EXCEEDED", "脚本调用深度超过 5",
                           details={"script_id": script_id, "call_stack": [s for s, _ in root.stack]})
    script = domain.ensure_script_found(root.scripts.get(script_id), script_id)
    params = arguments(script, {} if args is None else args)
    root.reference(script_id)
    source = root.controller.scripts.source_for(script)
    memory = 128 - sum((vm.memory_used() + 1048575) // 1048576 for _, vm in root.stack)
    if memory <= 0:
        raise StorageError("RESOURCE_LIMIT", "根执行的 Lua 内存已用尽")
    vm = LuaVm(root.gate, memory_limit_mb=memory)
    root.stack.append((script_id, vm))
    installed = False
    try:
        vm.build()
        root.api.install(vm)
        if root.debugger:
            root.debugger.install(vm, script)
            installed = True
        return vm.run_source(source, script_id, params, script_id=script_id, execution_id=root.id)
    except StorageError as exc:
        exc.details.setdefault("script_id", script_id)
        exc.details.setdefault("call_stack", [s for s, _ in root.stack])
        line = re.search(r":(\d+):", exc.message)
        if line:
            exc.details.setdefault("line", int(line.group(1)))
        raise
    finally:
        if installed:
            root.debugger.leave()
        root.stack.pop()
        vm.close()
