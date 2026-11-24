# -*- coding: utf-8 -*-
"""NDJSON 信封编解码（TASK-005 批 B1，自 backend/main.py 平移，行为不变）。

新增（设计 §3.2/§5.4）：
- parse_frame 按五类型（request/response/event/host_request/host_response）
  的畸形必填集校验（§5.4 逐字；与 schema/envelope.schema.json 同源）；
- build_host_request：Python → Rust 的宿主调用构造（id=py-<uuid>，03）。

不变量（用户冻结契约③）：session_id 缺失/不匹配由 SessionState 丢弃并记
stderr 日志、不计畸形帧；畸形判定 = JSON 非法/非对象/缺必填/字段类型不符/
协议版本≠1/未知 type/data-error 互斥违背。
"""

from __future__ import annotations

import json
from backend.strict_json import loads as strict_loads
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 协议常量（三端一致；schema/envelope.schema.json 为 schema 层事实源）
# ---------------------------------------------------------------------------

PROTOCOL_VERSION: int = 1
MAX_FRAME_BYTES: int = 2 * 1024 * 1024  # 03：单帧最大 2MiB，超限拒绝

# 通用必填（session_id 有意不在列：缺失/不匹配 → 丢弃不计畸形，契约③）
GENERIC_REQUIRED_FIELDS: Tuple[str, ...] = ("protocol_version", "type")

# 按类型的附加必填（§5.4 表逐字）
TYPE_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "request": ("id", "command"),
    "response": ("id", "success"),
    "event": ("seq", "name", "timestamp"),
    "host_request": ("id", "command", "params"),
    "host_response": ("id", "success"),
}

# request 类型完整畸形必填集（兼容常量：TASK-004 语义保持，供文档/测试引用）
REQUEST_REQUIRED_FIELDS: Tuple[str, ...] = GENERIC_REQUIRED_FIELDS + TYPE_REQUIRED_FIELDS["request"]

# host_request 白名单（03 明示的宿主命令；HostClient 发起方向唯一许可集）
HOST_COMMAND_WHITELIST: Tuple[str, ...] = (
    "input_action",
    "window_snapshot",
    "acquire_input_lease",
    "release_input_lease",
)


# ---------------------------------------------------------------------------
# 时间格式化（自 main.py 平移，行为不变）
# ---------------------------------------------------------------------------

def format_timestamp(moment: datetime) -> str:
    """把 datetime 格式化为 RFC3339 UTC 毫秒精度（如 2026-10-09T12:00:00.000Z）。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


# ---------------------------------------------------------------------------
# 编码（自 main.py 平移，行为不变）
# ---------------------------------------------------------------------------

class FrameTooLargeError(ValueError):
    """编码后单帧超过 2MiB 上限（03：超限拒绝发送并报告 PROTOCOL_ERROR 类诊断）。"""

    def __init__(self, size_bytes: int) -> None:
        super().__init__("frame too large: %d bytes > %d bytes" % (size_bytes, MAX_FRAME_BYTES))
        self.size_bytes = size_bytes


def encode_envelope(envelope: Dict[str, Any]) -> str:
    """把信封 dict 序列化为一帧：紧凑 JSON + 单个结尾 LF；内容换行由 JSON 转义。

    超过 MAX_FRAME_BYTES 抛 FrameTooLargeError（调用方负责拒绝发送并记 stderr）。
    """
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    frame = text + "\n"
    size = len(frame.encode("utf-8"))
    if size > MAX_FRAME_BYTES:
        raise FrameTooLargeError(size)
    return frame


# ---------------------------------------------------------------------------
# 解析（自 main.py 平移 + §5.4 多类型必填集扩展）
# ---------------------------------------------------------------------------

class MalformedFrameError(ValueError):
    """入站行不是结构合法的协议帧（invalid_json/not_an_object/missing_field:...）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_frame(line: str) -> Dict[str, Any]:
    """把一行原始 NDJSON 解析为信封 dict（按 §5.4 必填集校验）。

    畸形判定（计数语义由调用方 dispatcher 处理，连续 10 帧 → 关会话）：
    invalid_json / not_an_object / missing_field:<f> / bad_field_type:<f> /
    unsupported_protocol_version / unsupported_type:<t> / data_error_exclusive。

    session_id 有意不在此校验：缺失或不匹配由 SessionState.verify 丢弃
    （stderr 记日志，不计畸形，用户冻结契约③）。
    """
    text = line.rstrip("\n")
    if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise MalformedFrameError("PROTOCOL_ERROR: frame_too_large")
    try:
        value = strict_loads(text)
    except (ValueError, RecursionError):
        raise MalformedFrameError("invalid_json") from None
    if not isinstance(value, dict):
        raise MalformedFrameError("not_an_object")
    for field in GENERIC_REQUIRED_FIELDS:
        if field not in value:
            raise MalformedFrameError("missing_field:" + field)
    protocol_version = value["protocol_version"]
    if type(protocol_version) is not int or protocol_version != PROTOCOL_VERSION:
        raise MalformedFrameError("unsupported_protocol_version")
    msg_type = value["type"]
    required = TYPE_REQUIRED_FIELDS.get(msg_type) if isinstance(msg_type, str) else None
    if required is None:
        raise MalformedFrameError("unsupported_type:" + str(msg_type))
    for field in required:
        if field not in value:
            raise MalformedFrameError("missing_field:" + field)
    # 字段类型校验（畸形帧；schema/envelope.schema.json 同源约束）
    if "id" in required and not isinstance(value["id"], str):
        raise MalformedFrameError("bad_field_type:id")
    if "command" in required and not isinstance(value["command"], str):
        raise MalformedFrameError("bad_field_type:command")
    if "params" in required and not isinstance(value["params"], dict):
        raise MalformedFrameError("bad_field_type:params")
    if "success" in required and not isinstance(value["success"], bool):
        raise MalformedFrameError("bad_field_type:success")
    if msg_type == "event":
        seq = value["seq"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise MalformedFrameError("bad_field_type:seq")
        if not isinstance(value["name"], str) or not value["name"]:
            raise MalformedFrameError("bad_field_type:name")
        if not isinstance(value["timestamp"], str):
            raise MalformedFrameError("bad_field_type:timestamp")
    if msg_type in ("response", "host_response"):
        # data/error 互斥必有一（§5.4；03：成功含 data、失败含 error）
        if ("data" in value) == ("error" in value):
            raise MalformedFrameError("data_error_exclusive")
        if value["success"] != ("data" in value):
            raise MalformedFrameError("success_payload_mismatch")
    return value


# ---------------------------------------------------------------------------
# host_request 构造（Python → Rust，03：id=py-<uuid>）
# ---------------------------------------------------------------------------

def build_host_request(
    session_id: str,
    command: str,
    params: Dict[str, Any],
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """构造一条 host_request 信封；request_id 缺省生成 "py-<uuid4>"。"""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session_id,
        "type": "host_request",
        "id": request_id if request_id is not None else "py-" + str(uuid.uuid4()),
        "command": command,
        "params": params,
    }
