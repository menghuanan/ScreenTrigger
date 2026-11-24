# -*- coding: utf-8 -*-
"""输入窄代理包（TASK-017 / M05）：Python → Rust 输入宿主的唯一入口。

对外导出：
- :class:`InputProxy`：受控输入动作代理（`input_action`/`acquire_input_lease`/
  `release_input_lease`/`window_snapshot`）；
- :class:`InputProxyError`（别名 `DomainError`）：06 四字段错误；
- :func:`build_input_proxy`：工厂。
"""

from __future__ import annotations

from .errors import DomainError, InputProxyError, KNOWN_CODES, raise_for_host_error
from .proxy import InputProxy, build_input_proxy

__all__ = [
    "InputProxy",
    "InputProxyError",
    "DomainError",
    "KNOWN_CODES",
    "raise_for_host_error",
    "build_input_proxy",
]
