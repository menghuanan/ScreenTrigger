# -*- coding: utf-8 -*-
"""宿主帧登记（DEF-TASK008-01 修复，方向 A：ImageRef 由引擎拥有）。

**问题**：`confirm_selection` 的 `ImageRef.image_id` 由 Rust `HostImageStore`
生成（前缀 `sel-img-`），而公开命令 `get_image_preview` / `release_image`
（路由 `ForwardPython`）只认引擎 ``SessionImageStore``（前缀 `img-`）→ 公开契约
（03）被破坏：框选结果的 `ImageRef` 无法被公开命令解析。

**修复（方向 A）**：任何经**公开命令返回或公开事件**对外的 `ImageRef` 都必须由
**引擎 ``SessionImageStore`` 拥有并签发**。Rust 宿主捕获帧后：

1. 把最终帧 PNG 写入数据根下 **host 管理的受控临时区**（`<data_dir>/tmp/selection/`）；
2. 签发**一次性 host-frame token**（HMAC-SHA256 签名，绑定：文件名 + sha256 +
   宽高 + nonce + TTL + 会话）——与 TASK-009 import token **同一安全模型**
   （引擎**只认 token，不接受任意路径**）；
3. 经**内部命令** ``register_host_frame {token}`` 交引擎；引擎校验
   token/containment/sha256/尺寸 → 读入 → 存入 ``SessionImageStore`` →
   返回**引擎签发** `ImageRef`，并删除临时文件。

本模块是引擎侧消费端（对应 Rust ``src-tauri/src/selection/hostframe.rs`` 的签发端）。
token 线格式与 Rust 侧**逐字一致**（详见 ``test_host_frame_token.py`` 的跨语言固定向量）。

关键约束（07 §6）：临时区读入路径必须做**规范路径（realpath）+ 路径组件包含**
判定并**拒绝符号链接/reparse 逃逸**——不得使用 ``str.startswith``。

零第三方依赖：stdlib ``hmac``/``hashlib``/``base64``/``json``/``os``/``time``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Callable, Optional, Set

from ..capture.errors import CaptureError, image_expired, invalid_image, invalid_params
from ..types.region import ImageRef

from .decode_common import decode_image_to_bgr
from .store import SessionImageStore

# 与 Rust `hostframe::HOST_FRAME_TOKEN_PREFIX` 逐字一致。
HOST_FRAME_TOKEN_PREFIX = "sthost1"
# 一次性窗口：宿主签发后立即消费（远短于 import token 的 300s）。
DEFAULT_HOST_FRAME_TTL_SECONDS = 60
# 受控临时区（数据根相对）：宿主写、引擎读并删除。
TEMP_SUBDIR = ("tmp", "selection")


def temp_dir_for(data_dir: str) -> str:
    """受控临时区绝对路径：``<data_dir>/tmp/selection``。"""
    return os.path.join(data_dir, *TEMP_SUBDIR)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(secret: bytes, payload_b64: str) -> str:
    return _b64e(hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest())


def _now_seconds(now: Optional[float]) -> float:
    import time

    return time.time() if now is None else now


def mint_host_frame_token(
    secret: bytes,
    session_id: str,
    filename: str,
    sha256: str,
    width: int,
    height: int,
    ttl_seconds: int = DEFAULT_HOST_FRAME_TTL_SECONDS,
    now: Optional[float] = None,
    nonce: Optional[str] = None,
) -> str:
    """签发一次性 host-frame token（**仅供宿主/测试**，脚本不得调用）。

    载荷字段顺序固定 ``v, sid, f, sha, w, h, n, exp``（与 Rust serde 结构体
    字段顺序一致），紧凑 JSON + base64url，保证两侧序列化字节逐字一致。
    """
    if not isinstance(filename, str) or not filename:
        raise ValueError("filename 必须为非空字符串")
    moment = _now_seconds(now)
    payload = {
        "v": 1,
        "sid": session_id,
        "f": filename,
        "sha": sha256,
        "w": int(width),
        "h": int(height),
        "n": nonce if nonce is not None else os.urandom(16).hex(),
        "exp": int(moment) + int(ttl_seconds),
    }
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return "%s.%s.%s" % (HOST_FRAME_TOKEN_PREFIX, payload_b64, _sign(secret, payload_b64))


@dataclass(frozen=True)
class ResolvedHostFrame:
    """token 解析结果：受控临时区内的文件名 + 完整性/尺寸绑定。"""

    filename: str
    sha256: str
    width: int
    height: int


class HostFrameTokenAuthority:
    """HMAC 签名 host-frame token 的消费端（一次性 + 会话绑定 + TTL）。"""

    def __init__(
        self,
        secret: bytes,
        ttl_seconds: int = DEFAULT_HOST_FRAME_TTL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not secret:
            raise ValueError("secret 不能为空")
        self._secret = bytes(secret)
        self._ttl = int(ttl_seconds)
        self._clock: Callable[[], float] = (
            clock if clock is not None else (lambda: _now_seconds(None))
        )
        self._logger = logger
        self._used: Set[str] = set()

    def consume(self, token: str, session_id: str) -> ResolvedHostFrame:
        """校验并消费 token。

        Raises:
            CaptureError: IMAGE_EXPIRED（格式/签名/过期/跨会话/已使用/字段缺失）。
        """
        if not isinstance(token, str) or not token:
            raise invalid_params("token", "token 必须为非空字符串")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != HOST_FRAME_TOKEN_PREFIX:
            raise image_expired("host-frame token 格式非法")
        payload_b64, signature = parts[1], parts[2]
        expected = _sign(self._secret, payload_b64)
        if not hmac.compare_digest(expected, signature):
            raise image_expired("host-frame token 签名校验失败")
        try:
            payload = json.loads(_b64d(payload_b64).decode("utf-8"))
        except Exception:
            raise image_expired("host-frame token 载荷损坏")
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise image_expired("host-frame token 版本不受支持")
        if payload.get("sid") != session_id:
            raise image_expired("host-frame token 不属于当前会话")
        exp = payload.get("exp")
        if not isinstance(exp, int) or self._clock() > exp:
            raise image_expired("host-frame token 已过期")
        nonce = payload.get("n")
        if not isinstance(nonce, str) or not nonce:
            raise image_expired("host-frame token 缺少一次性标识")
        if nonce in self._used:
            raise image_expired("host-frame token 已被使用（一次性）")
        filename = payload.get("f")
        sha256 = payload.get("sha")
        width = payload.get("w")
        height = payload.get("h")
        if not isinstance(filename, str) or not filename:
            raise image_expired("host-frame token 未绑定文件名")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise image_expired("host-frame token 未绑定有效 sha256")
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            raise image_expired("host-frame token 未绑定有效尺寸")
        self._used.add(nonce)
        self._logger and self._logger("host-frame token 已消费 file=%s" % filename)
        return ResolvedHostFrame(filename=filename, sha256=sha256, width=width, height=height)


class NullHostFrameAuthority:
    """未装配宿主帧通道时的诚实缺省：一律拒绝（不伪造可用）。"""

    def consume(self, token: str, session_id: str) -> ResolvedHostFrame:
        raise image_expired("host-frame token 无法解析（宿主帧通道未装配）")


class PathEscapeError(CaptureError):
    """临时区路径越界/链接逃逸（06 INVALID_PARAMS；非静默）。"""


def _is_within(child: str, parent: str) -> bool:
    """规范路径**组件关系**判定（07 §6：禁止 ``startswith``）。

    先做大小写规范化（Windows），再按路径组件逐段比较，``child`` 必须位于
    ``parent`` 之下（或相等）。
    """
    child_parts = _norm_parts(child)
    parent_parts = _norm_parts(parent)
    if len(child_parts) < len(parent_parts):
        return False
    return child_parts[: len(parent_parts)] == parent_parts


def _norm_parts(path: str) -> list:
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    return [drive.lower()] + [
        part.lower() for part in rest.replace("\\", "/").split("/") if part
    ]


def resolve_temp_file(data_dir: str, filename: str) -> str:
    """把 token 绑定的文件名解析为受控临时区内**安全**的绝对路径。

    拒绝：含路径分隔符/``..`` 的文件名；realpath 逃逸出临时区；符号链接（含
    叶子 reparse）。返回**规范绝对路径**（调用方据此读文件）。

    Raises:
        PathEscapeError: 越界/链接逃逸（code=INVALID_PARAMS，field=token）。
    """
    if not isinstance(filename, str) or not filename:
        raise invalid_params("token", "临时文件名非法")
    # 仅允许纯 basename：显式拒绝路径分隔符与 "." / ".." 本体
    # （真正逃逸由下方 realpath 组件包含判定兜底，不用 startswith）。
    if "/" in filename or "\\" in filename:
        raise invalid_params("token", "临时文件名禁止路径分隔符/上级引用")
    if filename in (".", ".."):
        raise invalid_params("token", "临时文件名非法")
    temp_root = os.path.realpath(os.path.join(data_dir, *TEMP_SUBDIR))
    candidate = os.path.join(temp_root, filename)
    # 叶子不得为符号链接/reparse（realpath 后与候选不一致即视为逃逸）
    real = os.path.realpath(candidate)
    if not _is_within(real, temp_root):
        raise invalid_params("token", "临时文件解析越界（拒绝链接逃逸）")
    if os.path.islink(candidate):
        raise invalid_params("token", "临时文件不得为符号链接（拒绝链接逃逸）")
    return real


class HostFrameRegistrationService:
    """宿主帧登记：token → 读入受控临时区 → 引擎 SessionImageStore → ImageRef。"""

    def __init__(
        self,
        store: SessionImageStore,
        authority: object,
        data_dir: str,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not data_dir:
            raise ValueError("data_dir 不能为空")
        self._store = store
        self._authority = authority
        self._data_dir = data_dir
        self._logger = logger

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    def register(self, token: str, session_id: str) -> ImageRef:
        """消费 token 登记一帧，返回**引擎签发**的 ImageRef。

        Raises:
            CaptureError: IMAGE_EXPIRED（token 非法）/ INVALID_PARAMS（越界路径）/
                DATA_CORRUPT（sha256/尺寸不符）/ INVALID_IMAGE（解码失败）/
                RESOURCE_LIMIT（超预算）。
        """
        resolved: ResolvedHostFrame = self._authority.consume(token, session_id)
        path = resolve_temp_file(self._data_dir, resolved.filename)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise image_expired("宿主帧临时文件不可读: %s" % exc) from exc

        actual_sha = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual_sha, resolved.sha256):
            raise CaptureError(
                "DATA_CORRUPT",
                "宿主帧 sha256 与 token 绑定不符（拒绝登记）",
                field="sha256",
            )
        bgr, width, height = decode_image_to_bgr(data, "png")
        if width != resolved.width or height != resolved.height:
            raise CaptureError(
                "DATA_CORRUPT",
                "宿主帧尺寸与 token 绑定不符（%dx%d != %dx%d）"
                % (width, height, resolved.width, resolved.height),
                field="dimensions",
            )
        image_ref = self._store.put(_RawImage(width=width, height=height, data=bgr), "png")
        # 登记成功后删除临时文件（best-effort；失败不阻断登记结果）
        try:
            os.remove(path)
        except OSError:
            pass
        self._log(
            "host-frame 已登记 image_id=%s %dx%d" % (image_ref.image_id, width, height)
        )
        return image_ref


@dataclass(frozen=True)
class _RawImage:
    """``SessionImageStore.put`` 的 duck-typed 入参（BGR uint8 行主序）。"""

    width: int
    height: int
    data: bytes
