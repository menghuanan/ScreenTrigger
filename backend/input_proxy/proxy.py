# -*- coding: utf-8 -*-
"""输入窄代理（TASK-017 / M05）：Python → Rust 唯一物理输入所有者的**唯一入口**。

**职责**：把 Lua/脚本层的输入意图包装成 03 宿主命令并经
:class:`backend.ipc.hostclient.HostClient` 同步调用 Rust `InputManager`
（`input_action` / `acquire_input_lease` / `release_input_lease`；`window_snapshot`
归 TASK-018）。

**硬边界（01§M05 / 06§坐标区域与输入不变量）**：
- **不做坐标钳制（clamp）**、**不自动重试**、**不校验几何/范围**——校验权威在 Rust；
  越界一律由 Rust 整笔拒绝并回 `REGION_LIMIT_VIOLATION`，代理原样抛出。
- Python 侧不直接调用任何 Win32/SendInput（唯一物理输入所有者在 Rust）。
- 失败一律转 :class:`InputProxyError`（06 四字段透传）。

本任务只交付代理骨架 + 契约测试，**不接 Lua**（TASK-021 接入）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .errors import InputProxyError, raise_for_host_error

Logger = Callable[[str], None]


class InputProxy:
    """窄代理：包装单个 :class:`HostClient`，暴露受控输入动作接口。"""

    def __init__(self, hostclient: Any, logger: Optional[Logger] = None) -> None:
        self._hostclient = hostclient
        self._logger: Optional[Logger] = logger

    # -- 内部 ---------------------------------------------------------------

    def _call(self, command: str, params: Dict[str, Any], timeout_s: float = 5.0) -> Dict[str, Any]:
        """同步调用 + 解包：成功返回 data（dict）；失败抛 :class:`InputProxyError`。"""
        result = self._hostclient.request(command, params, timeout_s=timeout_s)
        if not getattr(result, "success", False):
            raise_for_host_error(getattr(result, "error", None))
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            # 成功但 data 非对象（协议异常）：诚实报错，不臆造成功。
            raise InputProxyError("INTERNAL_ERROR", "宿主返回的成功响应缺少 data 对象")
        return data

    # -- 租约 ---------------------------------------------------------------

    def acquire_lease(
        self,
        execution_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """获取输入租约，返回 `lease_id`（一根执行一个租约；>3 → BUSY）。

        不发送区域/几何校验——由 Rust 权威处理。
        """
        params: Dict[str, Any] = {"execution_id": execution_id}
        if context is not None:
            params["context"] = context
        data = self._call("acquire_input_lease", params)
        lease_id = data.get("lease_id")
        if not isinstance(lease_id, str) or not lease_id:
            raise InputProxyError("INTERNAL_ERROR", "宿主返回的 lease_id 非法")
        return lease_id

    def release_lease(self, execution_id: str) -> None:
        """释放该执行的租约（并尽力释放其持有键/鼠标）。"""
        self._call("release_input_lease", {"execution_id": execution_id})

    # -- 动作派发 -----------------------------------------------------------

    def dispatch_action(
        self,
        execution_id: str,
        lease_id: str,
        action_id: str,
        geometry_revision: int,
        action: Dict[str, Any],
        *, remaining_ms=None,
    ) -> bool:
        """派发一个输入动作；返回 `dispatched`（**仅表示调用完成**）。

        03：`input_action` params = `{execution_id,lease_id,action_id,geometry_revision,action}`；
        data = `{action_id,dispatched:true}`。代理**不做**任何重试/钳制/范围判断。
        """
        duration = action.get("duration_ms", 0)
        if action.get("kind") == "text":
            duration = action.get("interval_ms", 0) * len(action.get("text", ""))
        timeout_s = 5 + duration / 1000 if isinstance(duration, (int, float)) else 5
        if remaining_ms is not None:
            timeout_s = min(timeout_s, max(.001, remaining_ms / 1000))
        data = self._call(
            "input_action",
            {
                "execution_id": execution_id,
                "lease_id": lease_id,
                "action_id": action_id,
                "geometry_revision": int(geometry_revision),
                "action": action,
            },
            timeout_s=timeout_s,
        )
        return bool(data.get("dispatched", False))

    # -- 窗口快照（TASK-018 提供；本任务仅转调接口就位） -----------------------

    def window_snapshot(self, binding_id: str) -> Dict[str, Any]:
        """窗口几何快照转调（TASK-018 落地实现；本任务接口就位）。"""
        return self._call("window_snapshot", {"binding_id": binding_id})

    def query_pointer(self) -> Dict[str, Any]:
        return self._call("query_pointer", {})

    # -- 便捷：典型动作 ------------------------------------------------------

    def move(self, execution_id: str, lease_id: str, action_id: str, x: int, y: int,
             duration_ms: int = 0, geometry_revision: int = 0) -> bool:
        """便利封装 `move` 动作（坐标不做任何处理，交 Rust 校验）。"""
        return self.dispatch_action(
            execution_id,
            lease_id,
            action_id,
            geometry_revision,
            {"kind": "move", "x": int(x), "y": int(y), "duration_ms": int(duration_ms)},
        )

    def click(self, execution_id: str, lease_id: str, action_id: str, x: int, y: int,
              button: str = "left", clicks: int = 1, geometry_revision: int = 0) -> bool:
        """便利封装 `click` 动作。"""
        return self.dispatch_action(
            execution_id,
            lease_id,
            action_id,
            geometry_revision,
            {"kind": "click", "x": int(x), "y": int(y), "button": button, "clicks": int(clicks)},
        )

    def type_text(self, execution_id: str, lease_id: str, action_id: str, text: str,
                  interval_ms: int = 0, geometry_revision: int = 0) -> bool:
        """便利封装 `text` 动作（Unicode 走 Rust SendInput Unicode 路径）。"""
        return self.dispatch_action(
            execution_id,
            lease_id,
            action_id,
            geometry_revision,
            {"kind": "text", "text": text, "interval_ms": int(interval_ms)},
        )

    def hotkey(self, execution_id: str, lease_id: str, action_id: str, keys: List[str],
               geometry_revision: int = 0) -> bool:
        """便利封装 `hotkey` 动作（依序按下、逆序释放由 Rust 保证）。"""
        return self.dispatch_action(
            execution_id,
            lease_id,
            action_id,
            geometry_revision,
            {"kind": "hotkey", "keys": list(keys)},
        )


def build_input_proxy(hostclient: Any, logger: Optional[Logger] = None) -> InputProxy:
    """工厂：以给定 HostClient 构造输入窄代理。"""
    return InputProxy(hostclient, logger=logger)
