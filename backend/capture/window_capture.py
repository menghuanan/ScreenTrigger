# -*- coding: utf-8 -*-
"""窗口客户区端口（TASK-007；M06/TASK-018 的可注入接入点）。

窗口身份、客户区几何与 geometry_revision 归 **M06/Rust**（TASK-018）。本轮不实现
M06，只定义窄端口与被捕获流程调用：

- ``WindowSnapshot``：binding 解析后的客户区快照（客户端物理 rect + 版本 + 可见性）；
- ``WindowProvider``：按 ``binding_id`` 解析；无有效绑定 → ``WINDOW_NOT_FOUND``；
- ``NullWindowProvider``：缺省实现，恒抛 ``WINDOW_NOT_FOUND``（M06 未装配时的诚实答案，
  不返回空 rect、不伪造几何）。

TASK-018 落地后以 Rust 宿主 ``window_snapshot``（host_request）实现本端口，不改本接口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..types.region import Rect

from .errors import window_not_found


@dataclass(frozen=True)
class WindowSnapshot:
    """客户区快照（02 WindowSnapshot 的捕获侧投影）。

    Attributes:
        binding_id: 不透明绑定 ID（身份元组校验归 M06）。
        client_rect: 客户区物理屏幕 rect（ClientToScreen 原点，非 GetWindowRect）。
        geometry_revision: 移动/缩放/DPI/客户区变化时递增；0 表示未绑定。
        visible: IsWindowVisible。
        minimized: IsIconic；最小化时不得捕获（调用方判 WINDOW_UNAVAILABLE）。
        foreground: 是否前台。捕获绑定客户区时重新检查。
    """

    binding_id: str
    client_rect: Rect
    geometry_revision: int
    visible: bool
    minimized: bool
    foreground: bool


@runtime_checkable
class WindowProvider(Protocol):
    """按 binding_id 解析窗口客户区快照（M06 的可注入端口）。"""

    def snapshot(self, binding_id: str) -> WindowSnapshot:
        """返回快照。

        Raises:
            CaptureError: code=WINDOW_NOT_FOUND（身份失效/已关闭）。
        """
        ...


class NullWindowProvider:
    """缺省窗口端口：M06 未装配，恒抛 ``WINDOW_NOT_FOUND``（不伪造几何）。

    这使 ``capture_window`` 在生产骨架期诚实地返回 WINDOW_NOT_FOUND；单测注入
    内存替身以覆盖正常/异常分支。
    """

    def snapshot(self, binding_id: str) -> WindowSnapshot:  # noqa: D401
        raise window_not_found(binding_id)

class HostWindowProvider:
    """Read client geometry from the Rust catalog bound to this engine session."""
    def __init__(self, hostclient):
        self._host = hostclient

    def snapshot(self, binding_id: str) -> WindowSnapshot:
        from .errors import CaptureError
        response = self._host.request("window_snapshot", {"binding_id": binding_id})
        if not response.success:
            err = response.error or {}
            raise CaptureError(err.get("code", "WINDOW_UNAVAILABLE"), err.get("message", "无法读取绑定窗口"),
                               retryable=bool(err.get("retryable")), details=err.get("details"))
        data = response.data
        if not isinstance(data, dict) or data.get("binding", {}).get("binding_id") != binding_id:
            raise CaptureError("PROTOCOL_ERROR", "窗口快照与请求的绑定不一致")
        return WindowSnapshot(binding_id, Rect(**data["client_rect"]), data["geometry_revision"],
                              data["visible"], data["minimized"], data["foreground"])
