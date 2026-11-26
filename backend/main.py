# -*- coding: utf-8 -*-
"""ScreenTrigger Python sidecar 入口（TASK-005 批 B1：IPC 协议层接线）。

与 Rust Host 通过 stdio 交换 NDJSON 协议（docs/03-IPC协议.md）：

- stdout 只写协议帧；一切日志/诊断写 stderr（R10：日志一律 stderr）；
- UTF-8、一行一个 JSON、LF 分帧，内容换行由 JSON 转义；单帧硬上限 2MiB；
- 单 writer 队列线程保证行完整、不交错（03：单 writer 队列保证行完整）；
- TASK-005：读循环移交 backend/ipc/dispatcher.py（框架命令表 hello/ping/
  query_request/cancel_request/cancel_job/get_job + 业务空注册表
  UNKNOWN_COMMAND）；JobManager/hostclient 框架就位；畸形帧连续 10 帧
  （中间合法帧清零）→ stderr 记日志后退出 code 3（§6.5 双端对称）；
- 本文件保留：入口/参数解析/fault 故障注入/health/session/configure_stdio/
  StdioTransport（单 writer 语义不变）。

信封编解码平移至 backend/ipc/envelope.py（行为不变，旧测试经 re-export 回归）。
命令行约定见 TASK-004 设计说明 §4.2：--session-id 必填，--fault-scenario 可选。
"""

from __future__ import annotations

# ===========================================================================
# 入口引导（AUDIT-20261012-001）：必须位于**第一个标准库 import 之前**。
#
# 直接以 `python backend/main.py` 执行时，
# 解释器把脚本所在目录 <repo>/backend 置于 sys.path[0]。该目录下的
# backend/types/ 会以**顶层模块名 `types`** 遮蔽标准库 types：
#     import argparse → re → enum → `from types import MappingProxyType, ...`
# 命中 backend/types/__init__.py，其链式导入 dataclasses → copy → weakref →
# _weakrefset → `from types import GenericAlias` 失败 → ImportError、进程 exit 1、
# stdout 零响应（原始证据 reports/raw/progress-audit-20261012/sidecar-default.*）。
#
# 修复（最小、入口级；**不重命名 backend/types**——它是三端共享契约名）：
#   1) 从 sys.path 移除脚本自身目录（含 `''` 经 abspath 解析后等于该目录的情形）；
#   2) 把仓库根（脚本目录的父目录）插入 sys.path 首位，令 backend.* 仅以包路径解析。
#
# os / sys 在此处导入是安全的：二者在解释器启动阶段（sys 为内建；os 由 site
# 引导导入）已进入 sys.modules，`import os` / `import sys` 仅为字典命中，不会触发
# 任何 stdlib 链式导入，因而不会被 types 遮蔽影响（回归测试
# backend/tests/test_entry_bootstrap.py 以此为前提）。
#
# 幂等 / 无副作用：
#   - `python -m backend.main` 时 sys.path[0] 为 cwd(=仓库根)，不等于脚本目录，
#     不会被误删；仓库根已在 sys.path 中时不重复插入。
#   - PyInstaller 冻结入口（sys.frozen）无脚本目录语义，直接跳过引导。
# ===========================================================================
import os
import sys

_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT: str = os.path.dirname(_SCRIPT_DIR)


def _bootstrap_import_path() -> None:
    """入口引导：移除脚本目录并确保仓库根位于 sys.path 首位（AUDIT-20261012-001）。

    幂等且无副作用；对 `python -m backend.main`（sys.path 首位为 cwd）与
    PyInstaller 冻结入口（``sys.frozen``）均为 no-op 或安全操作。
    """
    if getattr(sys, "frozen", False):
        return  # PyInstaller 冻结入口：无脚本目录语义，路径由冻结引导接管
    script_dir = os.path.normcase(os.path.normpath(_SCRIPT_DIR))
    to_remove: List[str] = []
    for entry in sys.path:
        # `''` 表示 cwd；经 abspath 后即可与脚本目录做等价比较（大小写/分隔符归一）。
        resolved = os.path.normcase(os.path.normpath(os.path.abspath(entry)))
        if resolved == script_dir:
            to_remove.append(entry)
    for entry in to_remove:
        try:
            sys.path.remove(entry)
        except ValueError:  # 已被移除（重复条目）：忽略，保持幂等
            pass
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)


_bootstrap_import_path()

import argparse
import io
import queue
import re
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import time
from backend import startup_timing
from typing import Any, Callable, Dict, List, Optional, Tuple

# --- 包导入引导（AUDIT-20261012-001）：sys.path 引导已在文件顶部完成
# （首个标准库 import 之前）——脚本目录已移除、仓库根已置首位，故下方
# `backend.*` 包导入可直接解析。此处不再重复插入 sys.path，避免两套引导。 ---

from backend.ipc.dispatcher import (
    CONSECUTIVE_MALFORMED_LIMIT,
    PROTOCOL_ERROR_EXIT_CODE,
    Dispatcher,
)
from backend.ipc.envelope import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    REQUEST_REQUIRED_FIELDS,
    FrameTooLargeError,
    MalformedFrameError,
    encode_envelope,
    format_timestamp,
    parse_frame,
)
from backend.ipc.hostclient import HostClient
from backend.ipc.jobs import JobManager
from backend.input_proxy import InputProxy
from backend.config.service import ConfigService, register_config_commands
from backend.capture import build_default_capture_service, register_capture_commands
from backend.image import (
    HostFrameRegistrationService,
    HostFrameTokenAuthority,
    HmacTokenAuthority,
    NullHostFrameAuthority,
    NullTokenAuthority,
    SessionImageStore,
    build_import_service,
    register_host_frame_command,
    register_image_commands,
)
from backend.storage import DataRoot as AssetDataRoot
from backend.storage import fsops as _fsops
from backend.storage import sha256_hex as _sha256_hex
from backend.storage.imagestore import ImageStore as AssetImageStore
from backend.image.decode_common import decode_image_to_bgr as _decode_image_to_bgr
from backend.scripts import (
    ScriptService,
    ScriptSourceStore,
    register_script_commands,
)
from backend.targets import (
    Repository as TargetRepository,
    StdioCatalogEmitter,
    TargetService,
    register_target_commands,
)
from backend.vision import (
    ConditionTreeEvaluator,
    StdioRecognitionEmitter,
    build_default_vision_service,
    register_vision_commands,
)
from backend.vision.types import RecognitionError as _RecognitionError

# ---------------------------------------------------------------------------
# 协议/应用常量（与 docs/03、TASK-004 设计 §4.3 逐字对齐；禁止同义改名）
# ---------------------------------------------------------------------------

# PROTOCOL_VERSION / MAX_FRAME_BYTES / 信封编解码自 backend.ipc.envelope 导入
# （re-export 供既有测试与调用方兼容）。
ENGINE_VERSION: str = "0.1.0-skeleton"
SCHEMA_VERSION: int = 2

__all__ = [
    "PROTOCOL_VERSION",
    "ENGINE_VERSION",
    "SCHEMA_VERSION",
    "MAX_FRAME_BYTES",
    "REQUEST_REQUIRED_FIELDS",
    "CONSECUTIVE_MALFORMED_LIMIT",
    "PROTOCOL_ERROR_EXIT_CODE",
    "FrameTooLargeError",
    "MalformedFrameError",
    "encode_envelope",
    "parse_frame",
    "format_timestamp",
    "detect_lua_version",
    "StdioTransport",
    "SidecarArgs",
    "HealthState",
    "SessionState",
    "SidecarApp",
    "configure_stdio",
    "build_catalog_services",
    "main",
]

# --fault-scenario 枚举（设计 §4.2）；仅测试路径使用，不得伪造正常路径成功（§6.4）
FAULT_SCENARIOS: Tuple[str, ...] = (
    "crash_on_start",
    "silent_no_ready",
    "stderr_flood",
    "stdout_close_after_ready",
    "exit_after_ready",
    "stdout_garbage",
)
STDERR_FLOOD_LINES: int = 100_000  # 设计 §5.2：~100k 行 stderr 洪流
FAULT_CRASH_EXIT_CODE: int = 7     # 设计 §5.2 示例语义："process_exited: code 7"

# 骨架能力清单：7 项全部 unavailable；id/state/reason 逐字对齐设计 §4.3 JSON 示例
CAPABILITIES: Tuple[Dict[str, Any], ...] = (
    {"id": "ocr", "state": "unavailable", "reason": "骨架未接入识别，模型未由引擎加载"},
    {"id": "lua_script", "state": "unavailable", "reason": "Lua 运行时未由骨架接入"},
    {"id": "template_match", "state": "unavailable", "reason": "骨架未实现"},
    {"id": "feature_match", "state": "unavailable", "reason": "ORB 未就绪"},
    {"id": "contour_match", "state": "unavailable", "reason": "骨架未实现"},
    {"id": "color_match", "state": "unavailable", "reason": "骨架未实现"},
    {"id": "input", "state": "unavailable", "reason": "M05 输入闸门未实现"},
    {"id": "capture", "state": "unavailable", "reason": "M01 捕获未实现"},
)

# stdout_garbage 故障注入的畸形帧样本（仍经单 writer 队列发送，供宿主计数验证）
FAULT_GARBAGE_LINES: Tuple[str, ...] = (
    "this is not json at all",
    '{"protocol_version":1,"session_id":',
    "]]}{{{",
)

SESSION_ID_MAX_LENGTH: int = 128
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9-]+")

LuaProbe = Callable[[], Optional[str]]
Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    """默认时钟：UTC 墙钟；单测经 SidecarApp(clock=...) 注入固定时钟（§6.3）。"""
    return datetime.now(timezone.utc)


def detect_lua_version() -> Optional[str]:
    """软探测 lupa 的 **显式 lua54 后端** 读 Lua ``_VERSION``（D06 冻结决策）。

    D06：Lupa 必须显式使用 Lua 5.4 后端（lupa.lua54；TASK-001 探针实测
    default 后端为 5.5、lua54 为 5.4，不得用默认运行时）。任一步失败
    （未安装/后端不可用/结果非字符串）一律返回 None；调用方据此置
    lua_version=null 且 lua_script 恒 unavailable——不虚构运行时事实（§9-1）。
    """
    try:
        from lupa import lua54  # type: ignore[import-not-found]
    except Exception:
        return None
    runtime_factory = getattr(lua54, "LuaRuntime", None)
    if runtime_factory is None:
        return None
    try:
        version = runtime_factory().eval("_VERSION")
    except Exception:
        return None
    return version if isinstance(version, str) and version else None


# ---------------------------------------------------------------------------
# 参数解析（设计 §4.2：Rust → Python，参数数组，无 shell）
# ---------------------------------------------------------------------------

class SidecarArgs:
    """sidecar 命令行参数。

    Attributes:
        session_id: 会话标识，非空、≤128 字符、仅 [A-Za-z0-9-]。
        fault_scenario: --fault-scenario 枚举值或 None。
        data_dir: 数据根目录（--data-dir 必填，Rust 总是显式传；release=
            app_local_data_dir、dev=ScreenTrigger-dev/、SIDECAR_DATA_DIR 可覆盖
            ——解析逻辑归 Rust 侧，Python 只收参并校验可创建/可写）。
    """

    __slots__ = ("session_id", "fault_scenario", "data_dir")

    def __init__(
        self,
        session_id: str,
        fault_scenario: Optional[str] = None,
        data_dir: Optional[str] = None,
    ) -> None:
        self.session_id: str = session_id
        self.fault_scenario: Optional[str] = fault_scenario
        self.data_dir: Optional[str] = data_dir

    @staticmethod
    def validate_session_id(value: str) -> Optional[str]:
        """校验 session_id；合法返回 None，否则返回面向 stderr 日志的错误说明。"""
        if not value:
            return "--session-id 不能为空"
        if len(value) > SESSION_ID_MAX_LENGTH:
            return "--session-id 长度超过 %d 字符" % SESSION_ID_MAX_LENGTH
        if SESSION_ID_PATTERN.fullmatch(value) is None:
            return "--session-id 仅允许字符 [A-Za-z0-9-]"
        return None

    @staticmethod
    def validate_data_dir(value: Optional[str]) -> Optional[str]:
        """校验 data_dir：可创建且可写（探测文件写入后删除）；合法返回 None。

        缺失/不可创建/不可写均报错退出（防误写：绝不默认到用户目录）。
        """
        if not value:
            return "--data-dir 不能为空"
        try:
            os.makedirs(value, exist_ok=True)
        except OSError as exc:
            return "--data-dir 不可创建：%s" % exc
        probe_path = os.path.join(value, ".write-probe")
        try:
            with open(probe_path, "w", encoding="utf-8") as handle:
                handle.write("probe")
            os.remove(probe_path)
        except OSError as exc:
            return "--data-dir 不可写：%s" % exc
        return None

    @classmethod
    def parse(cls, argv: Optional[List[str]] = None) -> "SidecarArgs":
        """解析并校验 argv；任何违规输出 stderr 并以退出码 2 终止（§4.2）。

        argparse 自身的错误（--session-id/--data-dir 缺失、--fault-scenario
        非法枚举）同样打印到 stderr 并以退出码 2 终止。
        """
        parser = argparse.ArgumentParser(
            prog="backend/main.py",
            description="ScreenTrigger Python sidecar（NDJSON over stdio）",
        )
        parser.add_argument(
            "--session-id",
            required=True,
            help="会话标识（非空，≤128 字符，仅 [A-Za-z0-9-]）",
        )
        parser.add_argument(
            "--data-dir",
            required=True,
            help="数据根目录（Rust 显式传入；release=app_local_data_dir，dev 隔离）",
        )
        parser.add_argument(
            "--fault-scenario",
            choices=FAULT_SCENARIOS,
            default=None,
            help="故障注入场景（仅测试路径使用）",
        )
        parsed = parser.parse_args(argv)
        error = cls.validate_session_id(parsed.session_id)
        if error is not None:
            print(error, file=sys.stderr)
            sys.exit(2)
        error = cls.validate_data_dir(parsed.data_dir)
        if error is not None:
            print(error, file=sys.stderr)
            sys.exit(2)
        return cls(
            session_id=parsed.session_id,
            fault_scenario=parsed.fault_scenario,
            data_dir=parsed.data_dir,
        )


# ---------------------------------------------------------------------------
# 状态组件（TASK-004 设计 §4.6 类图：HealthState / SessionState）
# ---------------------------------------------------------------------------

class HealthState:
    """引擎侧健康阶段（骨架：starting → ready → exited；终态不可复活，06）。"""

    def __init__(self) -> None:
        self.phase: str = "starting"
        self.event_seq: int = 0
        self.exit_reason: Optional[str] = None

    def next_seq(self) -> int:
        """事件序号自 1 起递增（Python 侧事件计数器；UI seq 重分配属 Rust 事件总线）。"""
        self.event_seq += 1
        return self.event_seq

    def to_ready(self) -> None:
        """starting → ready；其余阶段忽略（终态不可复活）。"""
        if self.phase == "starting":
            self.phase = "ready"

    def to_exited(self, reason: str) -> None:
        """进入终态 exited；首次原因保留，后续调用不再覆盖。"""
        if self.phase != "exited":
            self.exit_reason = reason
            self.phase = "exited"


class SessionState:
    """当前会话标识与信封归属校验（03：启动每次新 session_id，旧会话消息全拒绝）。"""

    def __init__(self, session_id: str) -> None:
        self.session_id: str = session_id

    def verify(self, envelope: Dict[str, Any]) -> bool:
        """session_id 匹配返回 True；缺失或不匹配返回 False（调用方丢弃并记 stderr）。"""
        return envelope.get("session_id") == self.session_id


# ---------------------------------------------------------------------------
# stdio 传输（单 writer 队列保证行完整；TASK-004 R4 已验证语义不变）
# ---------------------------------------------------------------------------

class StdioTransport:
    """NDJSON stdio 传输层。

    stdout 全部输出（协议帧与故障注入的畸形行）都经单个队列、由单个 writer
    线程逐行写出并即时 flush，保证行完整不交错；stdin 由调用方线程经
    read_line() 同步读取。日志/诊断由调用方写 stderr，本类只写协议。
    """

    def __init__(
        self,
        stdout: Optional[io.TextIOBase] = None,
        stdin: Optional[io.TextIOBase] = None,
        stderr: Optional[io.TextIOBase] = None,
        on_delivery_failure: Optional[Callable[[], None]] = None,
    ) -> None:
        self._stdout: io.TextIOBase = stdout if stdout is not None else sys.stdout
        self._stdin: io.TextIOBase = stdin if stdin is not None else sys.stdin
        self._stderr: io.TextIOBase = stderr if stderr is not None else sys.stderr
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=128)
        self._delivery_failure = on_delivery_failure
        self._writer_thread: Optional[threading.Thread] = None
        self._closed: bool = False
        self._close_stream_on_finish: bool = False

    def start(self) -> None:
        """启动 writer 线程（幂等）。"""
        if self._writer_thread is None:
            self._writer_thread = threading.Thread(
                target=self.writer_loop, name="stdio-writer", daemon=True
            )
            self._writer_thread.start()

    def send(self, envelope: Dict[str, Any]) -> bool:
        """编码并入队一帧；成功入队返回 True。

        单帧超过 2MiB 时拒绝发送、向 stderr 记 PROTOCOL_ERROR 类诊断并返回
        False（03：超限中止该消息并报告 PROTOCOL_ERROR）；stdout 已关闭同样
        返回 False。
        """
        try:
            frame = encode_envelope(envelope)
        except FrameTooLargeError as exc:
            self._stderr.write(
                "[sidecar] PROTOCOL_ERROR：单帧 %d 字节超过 2MiB 上限，拒绝发送\n" % exc.size_bytes
            )
            self._stderr.flush()
            return False
        return self.send_raw(frame)

    def send_raw(self, frame: str) -> bool:
        """把预编码帧（无 LF 结尾则补上）入队；stdout 已关闭时返回 False。

        仅供 stdout_garbage 故障注入使用——畸形行仍必须走单 writer 队列，
        保证与正常协议帧不交错。
        """
        if self._closed:
            return False
        if not frame.endswith("\n"):
            frame = frame + "\n"
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self._fail_delivery()
            return False
        return True

    def _fail_delivery(self) -> None:
        self._closed = True
        if self._delivery_failure is not None:
            self._delivery_failure()

    def read_line(self) -> Optional[str]:
        """从 stdin 读一行原始文本；EOF 返回 None（优雅退出信号）。"""
        limit = MAX_FRAME_BYTES + 2
        source = getattr(self._stdin, "buffer", self._stdin)
        line = source.readline(limit)
        newline = b"\n" if isinstance(line, bytes) else "\n"
        if len(line) == limit and not line.endswith(newline):
            # Consume the rest of this frame so that its tail cannot be parsed as a new request.
            while True:
                tail = source.readline(limit)
                if not tail or tail.endswith(newline):
                    break
            raise MalformedFrameError("PROTOCOL_ERROR: frame_too_large")
        if not line:
            return None
        if isinstance(line, bytes):
            try:
                return line.decode("utf-8")
            except UnicodeDecodeError:
                raise MalformedFrameError("invalid_utf8") from None
        return line

    def close_stdout(self, close_stream: bool = False) -> None:
        """投递结束哨兵：writer flush 余帧后退出。

        close_stream=True 时 writer 随后关闭底层 stdout 流（注意：CPython 的
        sys.stdout.close() 不关 OS 句柄——真实断流须 os.close(1)，见 fault 分支）。
        """
        if close_stream:
            self._close_stream_on_finish = True
        self._closed = True
        try:
            self._queue.put(None, timeout=0.25)
        except queue.Full:
            self._fail_delivery()

    def join_writer(self, timeout: Optional[float] = None) -> bool:
        """等待 writer 线程结束；返回其是否已退出（进程退出前保证 flush）。"""
        if self._writer_thread is None:
            return True
        self._writer_thread.join(timeout)
        return not self._writer_thread.is_alive()

    def writer_loop(self) -> None:
        """writer 主循环：逐行写 stdout 并即时 flush；遇 None 哨兵收尾退出。"""
        while True:
            item = self._queue.get()
            if item is None:
                try:
                    self._stdout.flush()
                finally:
                    if self._close_stream_on_finish:
                        self._stdout.close()
                return
            try:
                self._stdout.write(item)
                self._stdout.flush()
            except (OSError, ValueError):
                self._fail_delivery()
                return


# ---------------------------------------------------------------------------
# 应用主体（TASK-004 设计 §4.6 SidecarApp；TASK-005 读循环移交 dispatcher）
# ---------------------------------------------------------------------------

class SidecarApp:
    """sidecar 应用：参数/会话/健康/传输 + dispatcher/job/hostclient 装配。

    Attributes:
        stderr_flood_lines: stderr 洪流行数（实例属性，单测可调小；默认 100k）。
        malformed_count: 入站畸形帧总数（委托 dispatcher，兼容既有测试口径）。
    """

    def __init__(
        self,
        args: SidecarArgs,
        transport: StdioTransport,
        clock: Optional[Clock] = None,
        stderr: Optional[io.TextIOBase] = None,
        lua_probe: Optional[LuaProbe] = None,
        capture_service: Optional[Any] = None,
        import_service: Optional[Any] = None,
        host_frame_service: Optional[Any] = None,
        target_service: Optional[Any] = None,
        catalog_emitter: Optional[Any] = None,
        script_service: Optional[Any] = None,
        vision_service: Optional[Any] = None,
        enable_input: bool = False,
        config_service: Optional[ConfigService] = None,
    ) -> None:
        self.args = args
        self.session = SessionState(args.session_id)
        self.health = HealthState()
        self.transport = transport
        self._clock: Clock = clock if clock is not None else _default_clock
        self._stderr: io.TextIOBase = stderr if stderr is not None else sys.stderr
        self._lua_probe: LuaProbe = lua_probe if lua_probe is not None else detect_lua_version
        self._lua_version: Optional[str] = self._lua_probe()
        self._instance_token: str = str(uuid.uuid4())  # 仅供 stderr 日志关联，不入协议
        self.stderr_flood_lines: int = STDERR_FLOOD_LINES
        self._shutdown_done: bool = False
        # TASK-005：job 框架与宿主调用客户端（骨架期无调用方，接口就位）
        self.jobs = JobManager()
        self.hostclient = HostClient(transport, self.session, stderr=self._stderr)
        # 输入装配与停止热键共同决定能力；每个 Lua 根经窄代理使用独立租约。
        self.input_proxy: Optional[Any] = (
            InputProxy(self.hostclient, logger=self._log) if enable_input else None
        )
        self._input_ready = self.input_proxy is not None and os.environ.get("ST_INPUT_MANAGED") != "1"
        self._input_reason = "停止热键尚未完成注册"
        self.dispatcher = Dispatcher(
            transport=transport,
            session=self.session,
            stderr=self._stderr,
            logger=self._log,
            job_manager=self.jobs,
            hostclient=self.hostclient,
        )
        # 框架命令表：hello/ping 归应用层（版本/时钟在此），dispatcher 托管路由
        self.dispatcher.register("hello", lambda envelope: self.cmd_hello())
        self.dispatcher.register("ping", lambda envelope: self.cmd_ping())
        self.dispatcher.register("set_input_availability", self._set_input_availability)
        self.dispatcher.register("pause_for_selection", self._pause_for_selection)
        # TASK-006：config 三命令（get/update/reset）路由给 Python（03 命令目录；
        # 设计 §2.3——HOST_COMMANDS 移除后经通用面转发，数据权威在 backend/config）
        self.config_service: Optional[ConfigService] = None
        if getattr(args, "data_dir", None):
            self.config_service = config_service or ConfigService(args.data_dir, logger=self._log)
            try:
                self.config_service.load()
            except Exception as exc:  # ConfigError：坏档/未来 schema/存储故障不阻断启动
                # 只记 code 与 message（无参数全文）；命令调用期按 §8 错误矩阵回报
                self._log("config 初始化未就绪：%s" % exc)
            register_config_commands(self.dispatcher, self.config_service)

        # TASK-007：捕获六命令（get_display_list/capture_region/capture_full_screen/
        # capture_window/release_image/get_image_preview）。捕获服务由调用方装配
        # （main() 用 GDI/BitBlt 后端；单测可注入替身）。装配成功 → hello/system_ready
        # 的 capture 能力据实置 available，否则维持骨架 unavailable（不伪造）。
        self.capture_service: Optional[Any] = capture_service
        if capture_service is not None:
            if os.environ.get("ST_WINDOW_AVAILABLE") == "1":
                from backend.capture.window_capture import HostWindowProvider
                capture_service.attach_window_provider(HostWindowProvider(self.hostclient))
                self.dispatcher.enable_workers("capture_region", "capture_window", "get_window_capture_capabilities")
            register_capture_commands(self.dispatcher, capture_service)

        # TASK-009：图片导入命令（import_image）。导入服务复用同一 SessionImageStore
        # （与捕获共享 ImageRef 生命周期，release_image/get_image_preview 通用）。
        # token 权威缺省为 NullTokenAuthority（未装配文件选择器时诚实拒绝）。
        self.import_service: Optional[Any] = import_service
        if import_service is not None:
            register_image_commands(self.dispatcher, import_service)

        # DEF-TASK008-01：内部命令 register_host_frame —— 宿主框选帧经
        # 一次性 host-frame token 登记进引擎 SessionImageStore，返回**引擎签发**
        # 的 ImageRef，保证公开命令 release_image/get_image_preview 可解析（方向 A）。
        # 未装配（未注入 ST_HOSTFRAME_TOKEN_SECRET）时不注册该命令（诚实缺省）。
        self.host_frame_service: Optional[Any] = host_frame_service
        if host_frame_service is not None:
            register_host_frame_command(self.dispatcher, host_frame_service)

        # TASK-011：目标/分组 CRUD 十命令（create_target/get_target/list_targets/
        # update_target/delete_target/create_group/list_groups/update_group/delete_group/
        # move_target_to_group）。服务由 main() 装配（含 Repository + 共享资产存储 +
        # clock/id_gen）；此处用 transport/health/session/clock 装配 StdioCatalogEmitter，
        # 使 catalog_changed 事件在 commit 成功后经单 writer 队列发出（03 事件目录）。
        self.target_service: Optional[Any] = target_service
        self.script_service: Optional[Any] = script_service
        if target_service is not None or script_service is not None:
            emitter = catalog_emitter
            if emitter is None:
                emitter = StdioCatalogEmitter(
                    self.transport, self.health, self.session, self._clock
                )
            # TASK-011：目标/分组 CRUD 十命令。
            if target_service is not None:
                target_service.emitter = emitter
                register_target_commands(self.dispatcher, target_service)
            # TASK-012：脚本 5 命令（list_scripts/load_script/save_script/delete_script/
            # validate_script）。与 TargetService 共用**同一个** emitter（§15.2 防脑裂）。
            if script_service is not None:
                script_service.emitter = emitter
                register_script_commands(self.dispatcher, script_service)

        # TASK-013：识别基座服务（6 条识别命令：recognize_text/match_template/
        # match_contour/detect_color/evaluate_target_conditions/test_recognition）。
        # **共享同一 JobManager**（识别 job 与内置 get_job/cancel_job 同源）；识别事件经
        # StdioRecognitionEmitter 与 system_ready/catalog_changed **共用同一 next_seq()**。
        # 未装配（缺省 None）→ 行为不变（纯协议单测不受影响）；4 项识别能力据实置位。
        self.vision_service: Optional[Any] = vision_service
        if vision_service is not None:
            vision_service.jobs = self.jobs
            vision_service.emitter = StdioRecognitionEmitter(
                self.transport, self.health, self.session, self._clock, logger=self._log
            )
            register_vision_commands(self.dispatcher, vision_service)
            models = getattr(getattr(vision_service, "registry", None), "managed_ocr", None)
            if models is not None:
                from backend.vision.model_commands import register_model_commands
                self.close_model_jobs = register_model_commands(self.dispatcher, self.jobs, models,
                                                                self.config_service, script_service)

        self.executions = None
        self.history_store = None
        self._startup_history_warning = None

        def emit(name, payload):
            self.transport.send({"protocol_version": PROTOCOL_VERSION, "session_id": self.session.session_id,
                                 "type": "event", "seq": self.health.next_seq(), "name": name,
                                 "timestamp": format_timestamp(self._clock()), "payload": payload})

        def handler(owner, name):
            def invoke(envelope):
                from backend.ipc.dispatcher import DispatcherError
                try:
                    return owner.command(name, envelope["params"])
                except Exception as exc:
                    error = exc.to_dict() if hasattr(exc, "to_dict") else {}
                    raise DispatcherError(error.get("code", "INTERNAL_ERROR"), str(exc),
                                          error.get("details"), error.get("retryable", False)) from exc
            return invoke

        if script_service is not None and getattr(args, "data_dir", None):
            from backend.history.store import HistoryStore
            def history_warning(message):
                self._log(message)
                payload = {"error": {"code":"STORAGE_ERROR", "message":message,
                                     "details":{"service":"history"}, "retryable":True}}
                if self.health.event_seq:
                    emit("system_error", payload)
                else:
                    self._startup_history_warning = lambda: emit("system_error", payload)
            self.history_store = HistoryStore(args.data_dir, history_warning)
            for name in ("list_execution_history", "get_execution_logs", "clear_execution_logs"):
                self.dispatcher.register(name, handler(self.history_store, name))
            self.dispatcher.enable_workers("get_execution_logs", "clear_execution_logs")

        if script_service is not None and self._lua_version is not None:
            from backend.script.controller import ExecutionController
            from backend.debug.service import STEPS, BREAKPOINTS
            def beep(hz, milliseconds):
                import winsound
                winsound.Beep(hz, milliseconds)

            settings = self.config_service.get_config if self.config_service else dict
            def text_output(op, execution_id, **fields):
                from backend.storage.errors import StorageError
                result = self.hostclient.request("text_output", dict(op=op, execution_id=execution_id, **fields))
                if not result.success:
                    err = result.error
                    raise StorageError(err["code"], err["message"], details=err.get("details"),
                                       retryable=err.get("retryable", False))
                return result.data

            self.executions = ExecutionController(script_service, capture_service, vision_service,
                                                  self.input_proxy, settings, emit, beep, self.history_store,
                                                  text_output=text_output)
            script_service.repository.attach_reference_port(self.executions.referenced)
            for name in (*STEPS, *BREAKPOINTS, "get_variables", "get_stack", "execute_script", "start_monitor",
                         "get_script_state", "query_execution", "pause_script", "resume_script", "stop_script"):
                self.dispatcher.register(name, handler(self.executions, name))
            if self.history_store is None:
                self.dispatcher.register("get_execution_logs", handler(self.executions, "get_execution_logs"))
            self.dispatcher.enable_workers("resume_script", *STEPS, "add_breakpoint", "start_monitor")
            self.dispatcher.register("stop_active_executions", lambda env: self.executions.stop_all())
            self.dispatcher.register("binding_state_changed",
                                     lambda env: self.executions.binding_changed(env["params"]))

        if script_service is not None and getattr(script_service, "repository", None) is not None:
            from backend.storage.backup_commands import register_backup_commands
            register_backup_commands(self.dispatcher, self.jobs, script_service.repository, self.executions, emit)

    # -- 日志 ---------------------------------------------------------------

    def _log(self, message: str) -> None:
        """诊断日志：一律 stderr，stdout 只写协议（R10/03）。"""
        self._stderr.write("[sidecar] %s\n" % message)
        self._stderr.flush()

    @property
    def malformed_count(self) -> int:
        """入站畸形帧总数（委托 dispatcher；兼容 TASK-004 测试口径）。"""
        return self.dispatcher.malformed_count

    # -- 协议载荷（hello 与 system_ready 共用；字段与设计 §4.3 逐字一致） ----

    def capabilities(self) -> List[Dict[str, Any]]:
        """能力清单深拷贝（骨架 7 项 unavailable 为基线，§6.4）。

        TASK-007 起：装配了捕获服务时，``capture`` 据实置 ``available``
        （reason=null）；未装配（如纯协议单测构造）时维持骨架基线，
        绝不虚报可用（03：unavailable 的功能不能显示可用按钮）。
        """
        caps = [dict(item) for item in CAPABILITIES]
        for cap in caps:
            if cap["id"] == "lua_script" and self.executions is not None:
                cap.update(state="available", reason=None)
        if self.capture_service is not None:
            for cap in caps:
                if cap["id"] == "capture":
                    cap["state"] = "available"
                    cap["reason"] = None
        # TASK-017：装配了输入窄代理（宿主 InputManager 已装配）→ input 据实置 available
        # （reason=null）；未装配维持骨架 unavailable + reason（03：不可显示可用按钮）。
        if self.input_proxy is not None:
            for cap in caps:
                if cap["id"] == "input":
                    cap["state"] = "available" if self._input_ready else "unavailable"
                    cap["reason"] = None if self._input_ready else self._input_reason
        # TASK-013：识别类 4 项（ocr/template_match/contour_match/color_match）据**实际装配**置位。
        # 基座装配（内置工厂表为空）下仍全 unavailable（带 reason）——**未装配不得显示 available**。
        if self.vision_service is not None:
            for cap in caps:
                if cap["id"] in ("ocr", "template_match", "contour_match", "color_match", "feature_match"):
                    state, reason = self.vision_service.capability_state(cap["id"])
                    cap["state"] = state
                    cap["reason"] = reason
        return caps

    def _set_input_availability(self, envelope):
        params = envelope["params"]
        self._input_ready = self.input_proxy is not None and params.get("enabled") is True
        self._input_reason = str(params.get("reason") or "")
        return {"ok": True}

    def _pause_for_selection(self, envelope: Dict[str, Any]) -> Dict[str, bool]:
        controller = getattr(self, "executions", None)
        return {"paused": controller.pause_for_selection() if controller is not None else False}

    def versions(self) -> Dict[str, Any]:
        """版本四元组；lua_version 为软探测结果，失败为 null（§9-1）。"""
        return {
            "protocol_version": PROTOCOL_VERSION,
            "engine_version": ENGINE_VERSION,
            "lua_version": self._lua_version,
            "schema_version": SCHEMA_VERSION,
        }

    def emit_system_ready(self) -> None:
        """发送 system_ready 事件（payload 只含 capabilities+versions，§4.3①）。"""
        envelope: Dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.session.session_id,
            "type": "event",
            "seq": self.health.next_seq(),
            "name": "system_ready",
            "timestamp": format_timestamp(self._clock()),
            "payload": {
                "capabilities": self.capabilities(),
                "versions": self.versions(),
            },
        }
        self.transport.send(envelope)
        self.health.to_ready()
        startup_timing.mark("system_ready")
        if self._startup_history_warning:
            self._startup_history_warning()
            self._startup_history_warning = None
        self._log("system_ready 已发送（seq=%d）" % self.health.event_seq)

    # -- 框架命令 handler（经 dispatcher 命令表路由） ---------------------------

    def cmd_hello(self) -> Dict[str, Any]:
        """hello data：版本/能力/模型列表（03：每会话先调用 hello）。

        ``model_manifests``：TASK-013 起据**实际装配**的识别 Provider 已安装模型填写
        （基座装配为空列表，与现状一致）。
        """
        manifests: List[Any] = []
        if self.vision_service is not None:
            for manifest in self.vision_service.installed_model_manifests():
                manifests.append(asdict(manifest) if is_dataclass(manifest) else manifest)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "engine_version": ENGINE_VERSION,
            "lua_version": self._lua_version,
            "schema_version": SCHEMA_VERSION,
            "capabilities": self.capabilities(),
            "model_manifests": manifests,
        }

    def cmd_ping(self) -> Dict[str, Any]:
        """ping data：{engine_time}（03：无副作用；时钟可注入，§6.3）。"""
        return {"engine_time": format_timestamp(self._clock())}

    # -- 读循环与退出（读循环托管给 dispatcher） ----------------------------------

    def _read_loop(self) -> int:
        """stdin 读循环托管；返回 0（EOF 优雅退出）或 3（连续畸形关会话，§6.5）。"""
        return self.dispatcher.run()

    def _finish(self, exit_code: int) -> int:
        """统一收尾：健康终态 + flush writer 队列（幂等）。"""
        self.health.to_exited("stdin_eof" if exit_code == 0 else "protocol_error")
        self._shutdown()
        return exit_code

    def _shutdown(self) -> None:
        """收尾：投递哨兵并等待 writer flush 余帧（幂等）。"""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        if hasattr(self, "close_model_jobs"):
            self.close_model_jobs()
        if self.executions is not None:
            self.executions.stop_all("engine_exit")
            with self.executions.changed:
                self.executions.changed.wait_for(lambda: not self.executions.roots, timeout=1)
        if self.vision_service is not None:
            self.vision_service.close()
        if self.history_store is not None:
            self.history_store.close()
        self.transport.close_stdout()
        self.transport.join_writer(timeout=5.0)
        self._log("sidecar 退出")

    # -- 故障注入（§6.2；仅测试路径使用，不得伪造正常路径成功 §6.4） -----------

    def _fault_stderr_flood(self) -> None:
        """向 stderr 写 ~100k 行（可经实例属性调小），验证宿主持续 drain 不死锁。"""
        self._log("fault stderr_flood：开始写入 %d 行" % self.stderr_flood_lines)
        for index in range(self.stderr_flood_lines):
            self._stderr.write("stderr-flood %06d fill-the-pipe\n" % index)
        self._stderr.flush()
        self._log("fault stderr_flood：写入完成")

    def _fault_stdout_garbage(self) -> None:
        """经单 writer 队列注入畸形 stdout 行，验证宿主 malformed_line_count 递增。"""
        for frame in FAULT_GARBAGE_LINES:
            self.transport.send_raw(frame)
        # BUG-6 复验用例（2026-10-09 判据修正）：非 UTF-8 字节经 os.write 直接注入
        # （文本层 encoding=utf-8 无法产出非法字节），验证宿主 for_each_line_lossy
        # 「解码失败 → 计数 + 日志，不断流」（§5.2 畸形行语义）。
        injected = len(FAULT_GARBAGE_LINES)
        try:
            import os as _os
            _os.write(1, b"\xff\xfe\x80 not-valid-utf8 line\n")
            injected += 1
        except OSError:
            pass  # 单测等 stdout 为替换对象（无 OS fd）的环境：跳过字节级注入
        self._log("fault stdout_garbage：注入 %d 行畸形帧" % injected)

    def run_fault_scenario(self) -> int:
        """按 --fault-scenario 分派故障分支（§6.2），返回进程退出码。

        各分支复刻正常路径并在精确位置注入故障：crash_on_start 不产出任何
        协议帧即退出；silent_no_ready 跳过 system_ready（宿主看门狗超时）；
        stderr_flood 在 ready 前洪泛 stderr；stdout_garbage 在 ready 前注入
        畸形帧；stdout_close_after_ready 在 ready 后真实断流（os.close(1)）；
        exit_after_ready 在 ready 后崩溃退出。
        """
        fault = self.args.fault_scenario
        if fault == "crash_on_start":
            self._log("fault crash_on_start：不发送任何协议帧，立即退出 code %d" % FAULT_CRASH_EXIT_CODE)
            self.health.to_exited("fault_crash_on_start")
            return FAULT_CRASH_EXIT_CODE

        self.transport.start()
        if fault == "stderr_flood":
            self._fault_stderr_flood()
        if fault == "stdout_garbage":
            self._fault_stdout_garbage()
        if fault != "silent_no_ready":
            self.emit_system_ready()
        if fault == "stdout_close_after_ready":
            self.transport.close_stdout()
            self.transport.join_writer(timeout=2.0)
            # 判据修正（主理人 2026-10-09 指令；依据 T6 探针 + CPython 语义实证）：
            # CPython 的 sys.stdout.close() 不关闭 OS 层句柄（os.close(1) 在其
            # closed 后仍成功），任何传输下仅关闭文本包装层而进程存活时父端都
            # 不可能收到 EOF。§6.4 故障注入须模拟真实断流：os.close(1) 真正
            # 关闭管道写端（管道 EOF 先于进程退出，进程继续读 stdin）。
            sys.stdout.flush()
            import os as _os
            _os.close(1)
            self._log("fault stdout_close_after_ready：os.close(1) 已真实关闭写端，继续读 stdin 直至 EOF")
        if fault == "exit_after_ready":
            self.health.to_exited("fault_exit_after_ready")
            self._shutdown()  # 保证 ready 帧在进程退出前 flush
            return FAULT_CRASH_EXIT_CODE

        exit_code = self._read_loop()
        return self._finish(exit_code)

    # -- 主入口 ----------------------------------------------------------------

    def run(self) -> int:
        """主入口：正常路径 = writer 就绪 → system_ready → 读循环 → 优雅退出。

        读循环退出码：0=stdin EOF；3=连续畸形帧达阈值关会话（§6.5，Rust 端
        经 EOF 既有 stdout_closed/process_exited 路径清理）。
        """
        self._log(
            "sidecar 启动 session_id=%s instance=%s"
            % (self.session.session_id, self._instance_token)
        )
        if self.args.fault_scenario is not None:
            return self.run_fault_scenario()
        self.transport.start()
        self.emit_system_ready()
        exit_code = self._read_loop()
        return self._finish(exit_code)


# ---------------------------------------------------------------------------
# 进程入口
# ---------------------------------------------------------------------------

def configure_stdio() -> None:
    """把 stdio 显式固定为 UTF-8（02/03 契约），不依赖 PYTHONUTF8 环境变量。

    - stdin：utf-8 读取（协议帧按字符解析）；
    - stdout：utf-8 + newline="\\n"——Windows 文本模式默认把 "\\n" 翻译成
      "\\r\\n"，会破坏 LF 分帧语义，必须禁用该翻译；缓冲与 flush 语义不变，
      行完整性仍由单 writer 队列保证（每行写后立即 flush）；
    - stderr：utf-8（日志/能力 reason 含中文文案）。

    兜底：reconfigure 是 TextIOWrapper（Python 3.7+）的方法；若流不是
    TextIOWrapper（如被测试替身替换）或解释器极老（<3.7，非目标环境），
    则跳过并向 stderr 记一条诊断——不得抛异常阻断启动。
    """
    targets = (
        (sys.stdin, {"encoding": "utf-8"}),
        (sys.stdout, {"encoding": "utf-8", "newline": "\n"}),
        (sys.stderr, {"encoding": "utf-8"}),
    )
    for stream, kwargs in targets:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            sys.stderr.write(
                "[sidecar] stdio 流不支持 reconfigure（%s），编码保持默认；"
                "目标环境 Python 3.13 恒支持\n" % type(stream).__name__
            )
            continue
        reconfigure(**kwargs)


def _build_import_token_authority(
    secret_hex: Optional[str] = None,
    stderr: Optional[Any] = None,
) -> Any:
    """按生产方式从 ``ST_IMPORT_TOKEN_SECRET`` 构造 token 校验器（TASK-009）。

    宿主（Rust ``ImportTokenIssuer``）经环境变量注入密钥的**十六进制**表示——hex
    仅是**传输编码**；签名 canonical key 为解码后的 **raw 32 字节**，与 Rust 侧
    ``issue()`` 使用的 ``self.secret``（原始字节）逐字一致（DEF-009-1 修复）。

    容错（fail-safe 诚实拒绝，**绝不崩溃、不静默放行**）：环境值缺失 / 非十六进制 /
    长度 ≠32 字节 → 回落 :class:`NullTokenAuthority`（一律拒绝）并向 stderr 记一条
    诊断。
    """
    if secret_hex is None:
        secret_hex = os.environ.get("ST_IMPORT_TOKEN_SECRET")
    stream = stderr if stderr is not None else sys.stderr
    if not secret_hex:
        stream.write(
            "[sidecar] ST_IMPORT_TOKEN_SECRET 未注入：导入能力置禁用（诚实拒绝）\n"
        )
        stream.flush()
        return NullTokenAuthority()
    try:
        raw = bytes.fromhex(secret_hex)
    except ValueError:
        stream.write(
            "[sidecar] ST_IMPORT_TOKEN_SECRET 非十六进制：导入能力置禁用（诚实拒绝）\n"
        )
        stream.flush()
        return NullTokenAuthority()
    if len(raw) != 32:
        stream.write(
            "[sidecar] ST_IMPORT_TOKEN_SECRET 长度 %d≠32 字节：导入能力置禁用（诚实拒绝）\n"
            % len(raw)
        )
        stream.flush()
        return NullTokenAuthority()
    return HmacTokenAuthority(raw)


def _build_host_frame_token_authority(
    secret_hex: Optional[str] = None,
    stderr: Optional[Any] = None,
) -> Any:
    """按生产方式从 ``ST_HOSTFRAME_TOKEN_SECRET`` 构造 host-frame 校验器。

    DEF-TASK008-01（方向 A）：与 import token **同一安全模型**——宿主（Rust
    ``selection::hostframe``）经环境变量注入密钥的十六进制表示；签名 canonical
    key 为解码后的 **raw 32 字节**（与 Rust 侧逐字一致）。

    容错（fail-safe 诚实拒绝，绝不崩溃/不静默放行）：缺失 / 非十六进制 /
    长度 ≠32 字节 → 回落 :class:`NullHostFrameAuthority`（一律拒绝）并记 stderr。
    """
    if secret_hex is None:
        secret_hex = os.environ.get("ST_HOSTFRAME_TOKEN_SECRET")
    stream = stderr if stderr is not None else sys.stderr
    if not secret_hex:
        stream.write(
            "[sidecar] ST_HOSTFRAME_TOKEN_SECRET 未注入：宿主帧登记通道禁用（诚实拒绝）\n"
        )
        stream.flush()
        return NullHostFrameAuthority()
    try:
        raw = bytes.fromhex(secret_hex)
    except ValueError:
        stream.write(
            "[sidecar] ST_HOSTFRAME_TOKEN_SECRET 非十六进制：宿主帧登记通道禁用（诚实拒绝）\n"
        )
        stream.flush()
        return NullHostFrameAuthority()
    if len(raw) != 32:
        stream.write(
            "[sidecar] ST_HOSTFRAME_TOKEN_SECRET 长度 %d≠32 字节：宿主帧登记通道禁用（诚实拒绝）\n"
            % len(raw)
        )
        stream.flush()
        return NullHostFrameAuthority()
    return HostFrameTokenAuthority(raw)


def _stderr_log(message: str) -> None:
    """catalog 服务诊断日志：一律 stderr（R10；stdout 只写协议）。"""
    sys.stderr.write("[sidecar] %s\n" % message)
    sys.stderr.flush()


class _BgrImage:
    """``SessionImageStore.put`` 所需的 duck-typed BGR 承载（``width``/``height``/``data``）。"""

    __slots__ = ("width", "height", "data")

    def __init__(self, width: int, height: int, data: bytes) -> None:
        self.width = int(width)
        self.height = int(height)
        self.data = bytes(data)


class _AssetTargetImageResolver:
    """``TargetImageResolver`` 组合根实现（TASK-016 T16-3）。

    把目标模板 ``ImageAsset`` 物化为会话 ``ImageRef``：读资产（``DataRoot.resolve`` +
    ``fsops.read_bytes``）→ 校验 ``sha256``（不符 → ``DATA_CORRUPT``）→ 解码 BGR
    （``decode_image_to_bgr``）→ ``SessionImageStore.put`` → ``ImageRef``。按资产
    ``sha256`` 会话内**有界缓存**（同一模板复用引用，避免重复解码）。
    """

    MAX_CACHE: int = 64

    def __init__(self, session_store: Any, data_root: Any, *,
                 logger: Optional[Callable[[str], None]] = None) -> None:
        self._store = session_store
        self._root = data_root
        self._logger = logger
        self._cache: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.RLock()
        self._pins = {}

    def resolve(self, asset: Any) -> Any:
        with self._lock:
            return self._resolve_locked(asset)

    @contextmanager
    def pin(self, asset):
        with self._lock:
            ref = self._resolve_locked(asset)
            self._pins[ref.image_id] = self._pins.get(ref.image_id, 0) + 1
        try:
            yield ref
        finally:
            with self._lock:
                self._pins[ref.image_id] -= 1
                if not self._pins[ref.image_id]:
                    del self._pins[ref.image_id]

    def _resolve_locked(self, asset):
        sha256 = _asset_field(asset, "sha256")
        path = _asset_field(asset, "path")
        image_format = _asset_field(asset, "format") or "png"
        if not isinstance(sha256, str) or not sha256:
            raise _RecognitionError("DATA_CORRUPT", "目标图像资产缺少 sha256",
                                    field="image.sha256")
        if not isinstance(path, str) or not path:
            raise _RecognitionError("DATA_CORRUPT", "目标图像资产缺少 path",
                                    field="image.path")
        cached = self._cache.get(sha256)
        if cached is not None:
            self._cache.move_to_end(sha256)
            return cached
        try:
            absolute = self._root.resolve(path)
        except Exception as exc:
            raise _RecognitionError("DATA_CORRUPT", "目标图像路径非法: %s" % exc,
                                    field="image.path") from exc
        if not _fsops.exists(absolute):
            raise _RecognitionError("DATA_CORRUPT", "目标图像资产缺失: %s" % path,
                                    field="image.path")
        try:
            data = _fsops.read_bytes(absolute)
        except OSError as exc:
            raise _RecognitionError("DATA_CORRUPT", "目标图像读取失败: %s" % exc,
                                    field="image.path") from exc
        if _sha256_hex(data) != sha256:
            raise _RecognitionError("DATA_CORRUPT", "目标图像资产 hash 不符: %s" % path,
                                    field="image.sha256")
        try:
            bgr, width, height = _decode_image_to_bgr(data, image_format)
        except Exception as exc:
            raise _RecognitionError("DATA_CORRUPT", "目标图像解码失败: %s" % exc,
                                    field="image.path") from exc
        for key, old in list(self._cache.items()):
            if (len(self._cache) < self.MAX_CACHE
                    and self._store.bytes_used + len(bgr) <= self._store.budget_bytes):
                break
            if old.image_id not in self._pins:
                self._store.release(old.image_id)
                del self._cache[key]
        image_ref = self._store.put(_BgrImage(width, height, bgr), "png")
        self._cache[sha256] = image_ref
        return image_ref


def _asset_field(asset: Any, name: str) -> Any:
    """按属性/映射两种形态读取 ``ImageAsset`` 字段（``ImageAsset`` 为 frozen dataclass）。"""
    if isinstance(asset, dict):
        return asset.get(name)
    return getattr(asset, name, None)


def build_catalog_services(
    args: SidecarArgs,
    image_store: SessionImageStore,
    *,
    logger: Optional[Callable[[str], None]] = None,
) -> Tuple[Any, Any, Any]:
    """装配 catalog 相关服务（TASK-011 目标/分组 + TASK-012 脚本元数据/源码）。

    返回 ``(repository, target_service, script_service)``。

    **防脑裂（设计 §15.2，关键）**：脚本服务与目标服务**共用同一个 ``Repository``
    实例**（同一 ``catalog/current`` 与同一内存快照）——**不得**新建第二个
    ``Repository(args.data_dir)``，否则两处 ``read_snapshot`` 各持一份
    ``script_metadata``，``save_script`` 与 ``create_target`` 互相覆盖。
    ``CatalogEmitter`` 于 ``SidecarApp`` 装配期共享同一实例。

    **共享会话图像存储（TASK-011 §13.2）**：资产存储包裹**同一个**
    ``SessionImageStore``（capture/import/host-frame 共用），否则 ``image_id``
    由 ``args.session_id`` 的 store 签发、却到 Repository 自建的另一 store 里查，
    会话所有权校验必然失败（全部 ``IMAGE_EXPIRED``）。

    Raises:
        ValueError: ``args.data_dir`` 缺失（服务需数据根）。
    """
    if not getattr(args, "data_dir", None):
        raise ValueError("build_catalog_services 需要 args.data_dir")
    log = logger if logger is not None else _stderr_log
    asset_store = AssetImageStore(image_store, AssetDataRoot(args.data_dir))
    repository = TargetRepository(args.data_dir, image_store=asset_store, logger=log)
    target_service = TargetService(repository=repository)
    # TASK-012：脚本服务复用**同一个** repository（§15.2 防脑裂）。
    source_store = ScriptSourceStore(repository.data_root, logger=log)
    script_service = ScriptService(repository=repository, source_store=source_store)
    return repository, target_service, script_service


def main(argv: Optional[List[str]] = None) -> int:
    """进程入口：固定 stdio 编码 → 解析参数 → 装配捕获/传输 → 运行。

    捕获服务（TASK-007）：GDI/BitBlt 后端；非 Windows 或缺 API 时构建失败，仅记
    stderr 并保持 capture 能力 unavailable（不阻断引擎启动）。
    """
    entry_ns = time.perf_counter_ns()
    configure_stdio()
    args = SidecarArgs.parse(argv)
    from backend.storage.lock import engine_session
    from backend.storage.errors import StorageError
    try:
        with engine_session(args.data_dir), startup_timing.session(args.data_dir, args.session_id):
            startup_timing.mark("sidecar_entry", qpc_ns=entry_ns)
            return _run_engine(args)
    except StorageError as exc:
        sys.stderr.write(exc.message + "\n")
        return 2


def _run_engine(args: SidecarArgs) -> int:
    # Terminal replies must be reliable. Losing the output channel ends the owning session
    # so that the host closes the input gate and fails its outstanding requests.
    transport = StdioTransport(stdout=sys.stdout, stdin=sys.stdin, stderr=sys.stderr,
                               on_delivery_failure=lambda: os._exit(PROTOCOL_ERROR_EXIT_CODE))
    # 捕获与导入共享同一会话级图像注册表（ImageRef 生命周期一致，release/preview 通用）。
    config = ConfigService(args.data_dir, logger=_stderr_log) if args.data_dir else None
    image_store = SessionImageStore(session_id=args.session_id,
                                   compression_level=config.png_compression_level if config else None)
    capture_service = None
    try:
        capture_service = build_default_capture_service(
            session_id=args.session_id, image_store=image_store
        )
    except Exception as exc:  # 非 Windows/缺 API：能力保持 unavailable，引擎照常起
        sys.stderr.write("[sidecar] capture 后端不可用，capture 能力置 unavailable：%s\n" % exc)
        sys.stderr.flush()
        capture_service = None
    # 导入服务：文件选择器 token 由宿主签发；密钥经环境注入（hex → raw 32 字节，
    # 未注入/非法则记诊断并诚实拒绝，见 _build_import_token_authority）。
    token_authority = _build_import_token_authority()
    import_service = build_import_service(
        session_id=args.session_id, store=image_store, token_authority=token_authority
    )
    # DEF-TASK008-01：宿主帧登记服务（内部命令 register_host_frame）。
    # 引擎只认 token（不接受任意路径）；受控临时区位于 <data_dir>/tmp/selection/。
    host_frame_service: Optional[Any] = None
    if args.data_dir:
        host_frame_service = HostFrameRegistrationService(
            store=image_store,
            authority=_build_host_frame_token_authority(),
            data_dir=args.data_dir,
        )
    # TASK-011/TASK-012：目标/分组 + 脚本元数据/源码服务。**关键**：脚本服务复用
    # **同一个** target_repository 实例（§15.2 防脑裂）；资产存储包裹**同一个**
    # SessionImageStore（capture/import/host-frame 共用），否则 image_id 跨 store
    # 所有权校验必然失败（全部 IMAGE_EXPIRED，TASK-011 §13.2）。
    target_service: Optional[Any] = None
    script_service: Optional[Any] = None
    repository: Optional[Any] = None
    vision_service: Optional[Any] = None
    if args.data_dir:
        repository, target_service, script_service = build_catalog_services(
            args, image_store, logger=_stderr_log
        )
        # TASK-013：识别基座**复用同一 Repository 与同一 image_store**（§5.3 防脑裂）；
        # 装配失败不阻断引擎启动（识别能力保持 unavailable，诚实）。
        try:
            # TASK-016：目标模板像素物化器（读资产 PNG → 解码 BGR → 会话 ImageRef），
            # 供条件树 evaluator 解析 Target.image；与捕获/导入共用同一 image_store。
            target_image_resolver = _AssetTargetImageResolver(
                image_store, AssetDataRoot(args.data_dir), logger=_stderr_log
            )
            vision_service = build_default_vision_service(
                args, repository=repository, image_store=image_store, config_service=config, logger=_stderr_log,
                evaluator_factory=lambda reg: ConditionTreeEvaluator(
                    registry=reg,
                    target_image_resolver=target_image_resolver,
                    logger=_stderr_log,
                ),
            )
        except Exception as exc:  # 基座无重依赖，正常不触发；防御性诚实降级
            sys.stderr.write(
                "[sidecar] vision 基座装配失败，识别能力置 unavailable：%s\n" % exc
            )
            sys.stderr.flush()
            vision_service = None
    # TASK-017：输入能力置位——宿主（Rust）装配 InputManager 时 spawn 注入
    # ST_INPUT_AVAILABLE=1；据此构建 InputProxy 并把 input 能力据实置 available，
    # 否则维持 unavailable（诚实降级）。
    enable_input = os.environ.get("ST_INPUT_AVAILABLE") == "1"
    app = SidecarApp(
        args=args,
        transport=transport,
        capture_service=capture_service,
        import_service=import_service,
        host_frame_service=host_frame_service,
        target_service=target_service,
        script_service=script_service,
        vision_service=vision_service,
        enable_input=enable_input,
        config_service=config,
    )
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
