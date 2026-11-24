"""TASK-001 探针：Lua 5.4（Lupa）运行时兼容性验证。

验证场景（对应 docs/agent/任务拆解-P0.md TASK-001 与 07-实现细节清单 §3）：
1. _VERSION 必须为 "Lua 5.4"（不得默认 lupa 一定内嵌 5.4）；
2. Lua table → JSON 显式区分数组/对象/null，拒绝函数、userdata、混合键、NaN；
3. line hook（断点基础）与 call/return 深度（经 Lua debug 库）；
4. count hook 周期取消忙循环 `while true do end`（间隔 10000 指令）；
5. 内存接口（set_max_memory/get_memory_used；allocator 限额实际暴露）；
6. 同步子调用（Lua 函数调用宿主函数再返回，仅值复制）。

类型判断遵循 07§3：不使用 isinstance 猜测，通过 Lua 运行时自身 type() 判断；
Python 异常经 lupa 回调传播为 Lua error，count hook 借此实现忙循环停止。

退出码：0=全部 PASS；1=存在 FAIL；2=lupa 不可用（有效探针结论，非工具故障）。

用法：
  python tools/probes/lua54_probe.py --json-out reports/TASK-001/lua-probe-result.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
from datetime import datetime, timezone

COUNT_HOOK_INTERVAL = 10000  # 07§3：count 间隔从 10000 指令评估
COUNT_HOOK_MAX_EVENTS = 50   # 50 × 10000 指令后必须取消


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def lua_type_name(rt, value) -> str:
    """权威类型判断：把值放回 Lua 运行时用 type() 判断（07§3 禁止 isinstance 猜测）。"""
    rt.globals()["__probe_value"] = value
    return str(rt.eval("type(__probe_value)"))


def lua_to_json(rt, value, depth=0):
    """显式转换：区分数组/对象/null；拒绝函数、userdata、混合键、NaN（R09/07§3）。"""
    if depth > 16:
        raise ValueError("nesting depth exceeds 16")
    t = lua_type_name(rt, value)
    if t == "nil":
        return None
    if t in ("boolean", "string"):
        return value
    if t == "number":
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            raise ValueError("number is NaN/inf, not JSON-representable")
        return value
    if t == "table":
        keys = list(value)  # 迭代 _LuaTable 产出键；类型判断仍走 Lua type()
        if not keys:
            return {"__empty_table__": True}  # 空 table 歧义显式标记，由调用方约定
        all_int_seq = all(isinstance(k, int) and not isinstance(k, bool) for k in keys) \
            and sorted(keys) == list(range(1, len(keys) + 1))
        if all_int_seq:
            return [lua_to_json(rt, value[k], depth + 1) for k in keys]
        if all(isinstance(k, str) for k in keys):
            return {k: lua_to_json(rt, value[k], depth + 1) for k in keys}
        raise ValueError("mixed table keys are not JSON-representable")
    raise ValueError(f"unsupported lua type: {t}")


def scenario_version(rt) -> dict:
    version = str(rt.eval("_VERSION"))
    api_version = rt.lua_version
    if version == "Lua 5.4" and tuple(api_version) == (5, 4):
        return {"status": "pass", "detail": f"_VERSION={version}, lua_version={api_version}"}
    return {"status": "fail", "detail": f"_VERSION={version!r}, lua_version={api_version!r}，要求 Lua 5.4"}


def scenario_json(rt) -> dict:
    try:
        cases = {
            "array": rt.eval("{1, 2, 3}"),
            "object": rt.eval('{a = 1, b = "x"}'),
            "null": rt.eval("nil"),
            "nested": rt.eval("{outer = {inner = {1, 2}}}"),
        }
        got = {name: lua_to_json(rt, v) for name, v in cases.items()}
        expect = {
            "array": [1, 2, 3],
            "object": {"a": 1, "b": "x"},
            "null": None,
            "nested": {"outer": {"inner": [1, 2]}},
        }
        if got != expect:
            return {"status": "fail", "detail": f"mismatch: {got!r}"}
        rejects = [
            ("function", rt.eval("print")),
            ("nan", rt.eval("0/0")),
            ("mixed_keys", rt.eval("{1, a = 2}")),
        ]
        for name, v in rejects:
            try:
                lua_to_json(rt, v)
                return {"status": "fail", "detail": f"{name} 未被拒绝"}
            except ValueError:
                continue
        return {"status": "pass", "detail": "round-trip 与拒绝用例全部符合"}
    except Exception as exc:
        return {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"[:300]}


def scenario_line_hook(rt) -> dict:
    """line hook 支持断点基础；call/return hook 维护调用深度（经 Lua debug 库）。"""
    try:
        lines = []
        depth = {"cur": 0, "max": 0}

        def line_hook(event, line=None):
            if str(event) == "line":
                lines.append(line)

        def trace_hook(event, line=None):
            e = str(event)
            if e == "call":
                depth["cur"] += 1
                depth["max"] = max(depth["max"], depth["cur"])
            elif e in ("return", "tail return"):
                # hook 设置于 chunk 中间时会出现先行 return 事件，钳制非负
                depth["cur"] = max(depth["cur"] - 1, 0)

        rt.execute("function probe_inner() return 1 end")
        rt.execute("function probe_outer() return probe_inner() + 1 end")
        rt.globals()["__probe_linehook"] = line_hook
        rt.globals()["__probe_tracehook"] = trace_hook
        # debug.sethook 只接受 Lua 函数：用 Lua 闭包包装 Python 回调
        rt.execute('debug.sethook(function(e, l) __probe_linehook(e, l) end, "l")')
        rt.execute("probe_outer()")
        rt.execute("debug.sethook()")
        if not lines:
            return {"status": "fail", "detail": "line hook 未触发"}
        rt.execute('debug.sethook(function(e, l) __probe_tracehook(e, l) end, "cr")')
        rt.execute("probe_outer()")
        rt.execute("debug.sethook()")
        if depth["max"] < 2:
            return {"status": "fail", "detail": f"call 深度异常: {depth}"}
        return {"status": "pass", "detail": f"line events={len(lines)}, max_depth={depth['max']}"}
    except Exception as exc:
        return {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"[:300]}


def scenario_count_hook_cancel(rt) -> dict:
    """count hook 周期检查取消：忙循环必须能被停止（不承诺固定耗时）。"""
    try:
        hits = {"n": 0}

        def count_hook(event, line=None):
            hits["n"] += 1
            if hits["n"] >= COUNT_HOOK_MAX_EVENTS:
                raise RuntimeError("PROBE_CANCELLED")

        rt.globals()["__probe_counthook"] = count_hook
        rt.execute(f'debug.sethook(function(e, l) __probe_counthook(e, l) end, "", {COUNT_HOOK_INTERVAL})')
        try:
            rt.execute("local i = 0 while true do i = i + 1 end")
            rt.execute("debug.sethook()")
            return {"status": "fail", "detail": "忙循环未被取消"}
        except Exception as exc:
            rt.execute("debug.sethook()")
            if "PROBE_CANCELLED" in str(exc):
                return {"status": "pass", "detail": f"hook 触发 {hits['n']} 次后停止忙循环"}
            return {"status": "fail", "detail": f"异常不符: {type(exc).__name__}: {exc}"[:300]}
    except Exception as exc:
        return {"status": "fail", "detail": f"setup: {type(exc).__name__}: {exc}"[:300]}


def scenario_memory(lupa_mod) -> dict:
    """内存接口：allocator 限额须在 LuaRuntime 创建时设定（set_max_memory 创建参数）。"""
    rt2 = None
    try:
        limit = 8 * 1024 * 1024  # 8 MiB
        rt2 = lupa_mod.lua54.LuaRuntime(max_memory=limit)
        exceeded = False
        try:
            rt2.execute('local t = {} for i = 1, 400000 do t[i] = string.rep("x", 128) end')
        except Exception:
            exceeded = True
        used_after = float(rt2.get_memory_used())
        if not exceeded:
            return {"status": "fail", "detail": f"限额 {limit}B 未触发分配失败"}
        return {"status": "pass", "detail": f"set_max_memory 限额生效（失败时 heap≈{used_after/1024:.0f}KB），API 已由候选暴露"}
    except Exception as exc:
        return {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"[:300]}


def scenario_subcall(rt) -> dict:
    """同步子调用：Lua → 宿主函数 → 返回值参与计算；宿主函数仅值复制（R09）。"""
    try:
        calls = {"n": 0}

        def host_add(a, b):
            calls["n"] += 1
            return int(a) + int(b)

        rt.globals()["host_add"] = host_add
        result = rt.eval("host_add(20, 22) * 2")
        if result == 84 and calls["n"] == 1:
            return {"status": "pass", "detail": "host function 值往返正确"}
        return {"status": "fail", "detail": f"result={result!r}, calls={calls['n']}"}
    except Exception as exc:
        return {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"[:300]}


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-001 Lua 5.4 探针")
    parser.add_argument("--json-out", default="reports/TASK-001/lua-probe-result.json")
    args = parser.parse_args()

    result = {
        "probe": "lua54_probe",
        "task": "TASK-001",
        "rebuild_command": "python tools/probes/lua54_probe.py --json-out " + args.json_out,
        "recorded_at": utc_now(),
        "environment": {
            "os": platform.platform(),
            "python": sys.version.replace("\n", " "),
            "architecture": platform.machine(),
        },
        "package": {"dist": "lupa", "version": None, "available": False, "reason": None},
        "lua_abi": {
            "_version": None,
            "note": "Lua ABI 与 lupa 包版本分开记录（TASK-001.DESIGN.02）",
        },
        "scenarios": {},
    }

    try:
        import lupa
        lupa_module = lupa
        result["package"]["available"] = True
        result["package"]["version"] = getattr(lupa, "__version__", "unknown")
        rt = lupa.lua54.LuaRuntime(unpack_returned_tuples=True)
        result["lua_abi"]["backend"] = "lupa.lua54"
        result["lua_abi"]["lua_version"] = list(rt.lua_version)
    except Exception as exc:
        result["package"]["reason"] = f"import_failed: {type(exc).__name__}: {exc}"[:300]

    if result["package"]["available"]:
        for name, fn in [
            ("version", scenario_version),
            ("json_roundtrip", scenario_json),
            ("line_hook", scenario_line_hook),
            ("count_hook_cancel", scenario_count_hook_cancel),
            ("memory", lambda rt: scenario_memory(lupa_module)),
            ("subcall", scenario_subcall),
        ]:
            result["scenarios"][name] = fn(rt)
        result["lua_abi"]["_version"] = result["scenarios"].get("version", {}).get("detail")

    os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    result["report_sha256"] = sha256_file(args.json_out)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    statuses = {v["status"] for v in result["scenarios"].values()}
    print(f"[lua54_probe] lupa={result['package']['available']} abi={result['lua_abi']}")
    for k, v in result["scenarios"].items():
        print(f"  scenario {k}: {v['status']} - {v['detail'][:120]}")
    if not result["package"]["available"]:
        return 2
    if "fail" in statuses:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
