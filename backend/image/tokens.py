# -*- coding: utf-8 -*-
"""导入 token 的签发与一次性消费（TASK-009，M01 import_image）。

安全模型（01/03）：脚本**不得**把任意路径传给 ``import_image``；只能出示由
**文件选择器**签发的一次性 token。token 为自包含、带 HMAC-SHA256 签名的载荷，
会话绑定 + TTL + 一次性；后端校验签名后解出真实路径，**伪造/过期/跨会话/重放**
一律拒绝。

- ``mint_import_token``：签发（**仅供文件选择器/测试**；脚本不得调用）；
- ``HmacTokenAuthority``：消费端（签名校验 + 会话 + 过期 + 一次性）；
- ``NullTokenAuthority``：未装配选择器时的诚实缺省（一律拒绝，不伪造可用）。

零第三方依赖：stdlib ``hmac``/``hashlib``/``base64``/``json``/``time``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Callable, Optional, Set

from ..capture.errors import image_expired, invalid_params

TOKEN_PREFIX = "stimp1"
DEFAULT_TTL_SECONDS = 300  # 5 分钟（选择器→导入的一次性窗口）


@dataclass(frozen=True)
class ResolvedImport:
    """token 解析结果：真实绝对路径 + 供展示的文件名。"""

    path: str
    display_name: str = ""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(secret: bytes, payload_b64: str) -> str:
    return _b64e(hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest())


def mint_import_token(
    secret: bytes,
    session_id: str,
    path: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: Optional[float] = None,
    nonce: Optional[str] = None,
) -> str:
    """签发一次性导入 token（**仅供文件选择器/测试**，脚本不得调用）。

    Args:
        secret: 与消费端共享的 HMAC 密钥。
        session_id: 绑定的会话（跨会话消费拒绝）。
        path: 文件选择器解析得到的绝对路径（不进入 IPC params）。
        ttl_seconds: 有效期秒数（默认 300）。
        now: 当前 epoch 秒（可注入以便测试）。
        nonce: 一次性随机数（缺省用 os.urandom）。
    """
    if not isinstance(path, str) or not path:
        raise ValueError("path 必须为非空字符串")
    moment = _now_seconds(now)
    # 载荷保持纯 ASCII（path 经 base64url 编码），使 Python 与 Rust 侧
    # 序列化字节逐字一致（避免非 ASCII 路径的 JSON 转义差异）。
    payload = {
        "v": 1,
        "sid": session_id,
        "pb": _b64e(path.encode("utf-8")),
        "n": nonce if nonce is not None else os.urandom(16).hex(),
        "exp": int(moment) + int(ttl_seconds),
    }
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return "%s.%s.%s" % (TOKEN_PREFIX, payload_b64, _sign(secret, payload_b64))


def _now_seconds(now: Optional[float]) -> float:
    import time

    return time.time() if now is None else now


class HmacTokenAuthority:
    """HMAC 签名 token 的消费端（一次性 + 会话绑定 + TTL）。"""

    def __init__(
        self,
        secret: bytes,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not secret:
            raise ValueError("secret 不能为空")
        self._secret = bytes(secret)
        self._ttl = int(ttl_seconds)
        self._clock: Callable[[], float] = clock if clock is not None else (lambda: _now_seconds(None))
        self._logger = logger
        self._used: Set[str] = set()

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    def consume(self, token: str, session_id: str) -> ResolvedImport:
        """校验并消费 token。

        Raises:
            CaptureError: IMAGE_EXPIRED（格式/签名/过期/跨会话/已使用）。
        """
        if not isinstance(token, str) or not token:
            raise invalid_params("import_token", "import_token 必须为非空字符串")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            raise image_expired("导入 token 格式非法")
        payload_b64, signature = parts[1], parts[2]
        expected = _sign(self._secret, payload_b64)
        if not hmac.compare_digest(expected, signature):
            raise image_expired("导入 token 签名校验失败")
        try:
            payload = json.loads(_b64d(payload_b64).decode("utf-8"))
        except Exception:
            raise image_expired("导入 token 载荷损坏")
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise image_expired("导入 token 版本不受支持")
        if payload.get("sid") != session_id:
            raise image_expired("导入 token 不属于当前会话")
        exp = payload.get("exp")
        if not isinstance(exp, int) or self._clock() > exp:
            raise image_expired("导入 token 已过期")
        nonce = payload.get("n")
        if not isinstance(nonce, str) or not nonce:
            raise image_expired("导入 token 缺少一次性标识")
        if nonce in self._used:
            raise image_expired("导入 token 已被使用（一次性）")
        path_b64 = payload.get("pb")
        if not isinstance(path_b64, str) or not path_b64:
            raise image_expired("导入 token 未绑定有效路径")
        try:
            path = _b64d(path_b64).decode("utf-8")
        except Exception:
            raise image_expired("导入 token 路径编码损坏")
        self._used.add(nonce)
        self._log("导入 token 已消费 path=%s" % path)
        return ResolvedImport(path=path, display_name=path.replace("\\", "/").rsplit("/", 1)[-1])


class NullTokenAuthority:
    """未装配文件选择器时的诚实缺省：一律拒绝（不伪造可用）。"""

    def consume(self, token: str, session_id: str) -> ResolvedImport:
        raise image_expired("导入 token 无法解析（文件选择器未装配）")
