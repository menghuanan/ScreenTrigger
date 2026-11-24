# -*- coding: utf-8 -*-
"""ConfigService：config.json 读写服务（TASK-006 批 B2，设计 §5/§8）。

生命周期状态机（§5.3）：
  Uninitialized --config.json 不存在--> 写默认（revision=1，原子路径）--> Ready
  读取时坏档（JSON 非法/同版本未知字段/必填缺失/类型或范围错误）--> Corrupted：
    backups/config.json.bak 可解析且 schema 支持 --> 原子拷贝回 config.json
    （revision 以备份为准，记 stderr 日志）--> Ready
    无备份/备份同样损坏 --> ReadOnlyFailed
  schema_version>2 --> SCHEMA_UNSUPPORTED 直接 ReadOnlyFailed（拒写不删未知字段）
  ReadOnlyFailed --人工处置（替换/删除坏档）后下一次 load--> Ready
  不自动建空库覆盖；ReadOnlyFailed 下 get/update/reset 全部拒绝：
    get_config → DATA_CORRUPT（不返回编造数据）；update/reset → DATA_CORRUPT。

update_config 算法（§5.4）：进程内写锁 → 读当前文档（坏档 DATA_CORRUPT 不写盘）
  → expected_revision 比对（CONFLICT）→ 深合并+全文校验（全过才写盘——
  "非法 patch 不变旧配置"）→ revision+1 → 原子保存（tmp 同卷+flush+fsync →
  旧档拷 backups/config.json.bak 单份覆盖 → os.replace）→ 内存快照更新。
  三处故障注入点（tmp 写失败/fsync 失败/replace 失败/备份拷贝失败）→
  STORAGE_ERROR{retryable:true}，旧档与新档均不破坏。

敏感字段：config 无凭据/密钥/令牌字段；日志只记变更字段路径与 revision 结果，
不记 patch 参数全文（06"不记录参数全文"）。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from backend.config.defaults import default_config
from backend.strict_json import loads as strict_loads

Logger = Callable[[str], None]

# 深合并/校验允许的键集（与 schema/config.schema.json additionalProperties:false
# 逐字对应；path="" 为文档顶层）
TOP_LEVEL_KEYS: Tuple[str, ...] = (
    "schema_version", "revision", "general", "recognition", "capture",
    "input", "execution", "hotkeys", "logging",
)
_ALLOWED_KEYS: Dict[str, Tuple[str, ...]] = {
    "": TOP_LEVEL_KEYS,
    "general": ("language", "theme"),
    "recognition": (
        "default_frequency_hz", "default_threshold", "rgb_tolerance",
        "hsv_tolerance", "ocr_model_id", "ocr_min_confidence", "model_directory",
    ),
    "recognition.rgb_tolerance": ("r", "g", "b"),
    "recognition.hsv_tolerance": ("h", "s", "v"),
    "capture": ("preview_hz", "png_compression_level"),
    "input": ("allow_exit", "move_duration_ms", "type_interval_ms"),
    "execution": ("total_timeout_ms",),
    "hotkeys": ("start_selection", "stop_script", "pause_script"),
    "logging": ("level", "rotate_bytes", "file_count"),
}
_THEMES = ("system", "light", "dark")
_LOG_LEVELS = ("debug", "info", "warning", "error")
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")


def _copy_file(src: str, dst: str) -> None:
    """Publish a complete backup only after the copy and flush succeed."""
    staged = dst + ".tmp"
    try:
        shutil.copyfile(src, staged)
        with open(staged, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(staged, dst)
    finally:
        try:
            os.remove(staged)
        except FileNotFoundError:
            pass


class ConfigError(Exception):
    """ConfigService 错误（06 四字段结构；field 进 details 供 UI 定位）。"""

    def __init__(
        self,
        code: str,
        message: str,
        field: Optional[str] = None,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        merged: Dict[str, Any] = dict(details) if details else {}
        if field is not None:
            merged["field"] = field
        self.details = merged
        self.retryable = retryable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


# ---------------------------------------------------------------------------
# 字段级校验纯函数（code 参数区分加载期 DATA_CORRUPT / patch 期 INVALID_PARAMS）
# ---------------------------------------------------------------------------

def _fail(code: str, field: str, message: str) -> None:
    raise ConfigError(code, message, field=field)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _v_number(value: Any, field: str, code: str, lo: float, hi: Optional[float]) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(code, field, "%s 必须为数值" % field)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        _fail(code, field, "%s 必须为有限数（禁 NaN/Infinity）" % field)
    if number < lo or (hi is not None and number > hi):
        _fail(code, field, "%s 超出范围 [%s, %s]" % (field, lo, hi))


def _v_int(value: Any, field: str, code: str, lo: int, hi: Optional[int]) -> None:
    if not _is_int(value):
        _fail(code, field, "%s 必须为整数" % field)
    if value < lo or (hi is not None and value > hi):
        _fail(code, field, "%s 超出范围 [%s, %s]" % (field, lo, hi))


def _v_bool(value: Any, field: str, code: str) -> None:
    if not isinstance(value, bool):
        _fail(code, field, "%s 必须为布尔值" % field)


def _v_enum(value: Any, field: str, code: str, choices: Tuple[str, ...]) -> None:
    if not isinstance(value, str) or value not in choices:
        _fail(code, field, "%s 必须为 %s 之一" % (field, "/".join(choices)))


def _v_nonempty_str(value: Any, field: str, code: str) -> None:
    if not isinstance(value, str) or len(value) < 1:
        _fail(code, field, "%s 必须为非空字符串" % field)


def _v_str_or_null(value: Any, field: str, code: str) -> None:
    if value is not None and not isinstance(value, str):
        _fail(code, field, "%s 必须为字符串或 null" % field)


def _v_rgb(value: Any, field: str, code: str) -> None:
    if not isinstance(value, dict):
        _fail(code, field, "%s 必须为对象" % field)
    for key in value:
        if key not in ("r", "g", "b"):
            _fail(code, "%s.%s" % (field, key), "未声明字段")
    for channel in ("r", "g", "b"):
        if channel not in value:
            _fail(code, "%s.%s" % (field, channel), "缺失必填字段")
        _v_int(value[channel], "%s.%s" % (field, channel), code, 0, 255)


def _v_hsv(value: Any, field: str, code: str, h_max: int) -> None:
    if not isinstance(value, dict):
        _fail(code, field, "%s 必须为对象" % field)
    for key in value:
        if key not in ("h", "s", "v"):
            _fail(code, "%s.%s" % (field, key), "未声明字段")
    for channel in ("h", "s", "v"):
        if channel not in value:
            _fail(code, "%s.%s" % (field, channel), "缺失必填字段")
    _v_int(value["h"], "%s.h" % field, code, 0, h_max)
    for channel in ("s", "v"):
        _v_int(value[channel], "%s.%s" % (field, channel), code, 0, 100)


def _v_model_directory(value: Any, field: str, code: str) -> None:
    """02/07 §6：数据根相对目录——禁绝对路径、盘符、..（组件判断，非 startswith）。"""
    _v_nonempty_str(value, field, code)
    if _DRIVE_LETTER_RE.match(value) is not None:
        _fail(code, field, "%s 禁止盘符/绝对路径" % field)
    if value.startswith("/") or value.startswith("\\"):
        _fail(code, field, "%s 必须为数据根相对路径" % field)
    for component in value.replace("\\", "/").split("/"):
        if component == "..":
            _fail(code, field, "%s 禁止 .. 路径组件" % field)


_SECTION_VALIDATORS = {
    "general": lambda section, code: (
        _v_enum(section["language"], "general.language", code, ("zh-CN",)),
        _v_enum(section["theme"], "general.theme", code, _THEMES),
    ),
    "recognition": lambda section, code: (
        _v_number(section["default_frequency_hz"], "recognition.default_frequency_hz",
                  code, 0.1, 30),
        _v_number(section["default_threshold"], "recognition.default_threshold",
                  code, 0, 1),
        _v_rgb(section["rgb_tolerance"], "recognition.rgb_tolerance", code),
        _v_hsv(section["hsv_tolerance"], "recognition.hsv_tolerance", code, 180),
        _v_str_or_null(section["ocr_model_id"], "recognition.ocr_model_id", code),
        _v_number(section["ocr_min_confidence"], "recognition.ocr_min_confidence",
                  code, 0, 1),
        _v_model_directory(section["model_directory"], "recognition.model_directory",
                           code),
    ),
    "capture": lambda section, code: (
        _v_int(section["preview_hz"], "capture.preview_hz", code, 1, 20),
        _v_int(section["png_compression_level"], "capture.png_compression_level",
               code, 0, 9),
    ),
    "input": lambda section, code: (
        _v_bool(section["allow_exit"], "input.allow_exit", code),
        _v_int(section["move_duration_ms"], "input.move_duration_ms", code, 0, 5000),
        _v_int(section["type_interval_ms"], "input.type_interval_ms", code, 0, 5000),
    ),
    "execution": lambda section, code: (
        _v_int(section["total_timeout_ms"], "execution.total_timeout_ms", code, 0, None),
    ),
    "hotkeys": lambda section, code: tuple(
        _v_nonempty_str(section[key], "hotkeys.%s" % key, code)
        for key in ("start_selection", "stop_script", "pause_script")
    ),
    "logging": lambda section, code: (
        _v_enum(section["level"], "logging.level", code, _LOG_LEVELS),
        _v_int(section["rotate_bytes"], "logging.rotate_bytes", code, 1048576, 104857600),
        _v_int(section["file_count"], "logging.file_count", code, 1, 20),
    ),
}


def _validate_document(doc: Any, code: str) -> None:
    """全文校验（加载期 code=DATA_CORRUPT；patch 合并期 code=INVALID_PARAMS）。

    schema_version>2 在加载期报 SCHEMA_UNSUPPORTED（拒写不删未知字段）；
    patch 期同字段报 INVALID_PARAMS（patch 不允许把文档改成未来版本）。
    """
    if not isinstance(doc, dict):
        _fail(code, "", "config 文档必须为对象")
    # schema_version 最先判（设计 §5.3：未来 schema → SCHEMA_UNSUPPORTED 拒写，
    # 且"未知字段不静默删除"——不得被同版本未知字段分支抢先路由进备份恢复）
    schema_version = doc.get("schema_version")
    if not _is_int(schema_version):
        _fail(code, "schema_version", "schema_version 必须为整数")
    if schema_version > 2:
        raise ConfigError(
            "SCHEMA_UNSUPPORTED" if code == "DATA_CORRUPT" else code,
            "config schema_version=%s 高于当前支持的 2，请升级应用" % schema_version,
            field="schema_version",
        )
    if schema_version != 2:
        _fail(code, "schema_version", "schema_version 必须为 2")
    for key in doc:
        if key not in TOP_LEVEL_KEYS:
            _fail(code, key, "未声明字段: %s" % key)
    for key in TOP_LEVEL_KEYS:
        if key not in doc:
            _fail(code, key, "缺失必填字段: %s" % key)
    _v_int(doc["revision"], "revision", code, 1, None)
    for section, validator in _SECTION_VALIDATORS.items():
        value = doc[section]
        if not isinstance(value, dict):
            _fail(code, section, "%s 必须为对象" % section)
        for key in value:
            if key not in _ALLOWED_KEYS[section]:
                _fail(code, "%s.%s" % (section, key), "未声明字段: %s" % key)
        for key in _ALLOWED_KEYS[section]:
            if key not in value:
                _fail(code, "%s.%s" % (section, key), "缺失必填字段")
        validator(value, code)


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any], path: str,
                code: str) -> Dict[str, Any]:
    """深合并（§5.4d）：对象递归合并、标量替换、数组整体替换；未知字段拒绝。"""
    allowed = _ALLOWED_KEYS[path]
    merged = dict(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        sub_path = "%s.%s" % (path, key) if path else key
        if key not in allowed:
            _fail(code, sub_path, "未声明字段: %s" % sub_path)
        if isinstance(value, dict) and sub_path in _ALLOWED_KEYS:
            merged[key] = _deep_merge(base.get(key) or {}, value, sub_path, code)
        else:
            # 标量/数组/嵌套对象整体替换（可 null 字段显式置 null 即此路径）
            merged[key] = copy.deepcopy(value)
    return merged


def _walk_field(doc: Dict[str, Any], field: str) -> Any:
    """按 "a.b.c" 路径取值（共享用例 expect.field 断言用）。"""
    current: Any = doc
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(field)
        current = current[part]
    return current


# ---------------------------------------------------------------------------
# ConfigService
# ---------------------------------------------------------------------------

class ConfigService:
    """config.json 单文件配置服务（进程内写锁串行化 load/update/reset）。"""

    STATE_UNINITIALIZED = "uninitialized"
    STATE_READY = "ready"
    STATE_READ_ONLY_FAILED = "read_only_failed"

    def __init__(self, data_dir: str, logger: Optional[Logger] = None) -> None:
        self._data_dir: str = data_dir
        self._config_path: str = os.path.join(data_dir, "config.json")
        self._tmp_path: str = os.path.join(data_dir, "config.json.tmp")
        self._backup_path: str = os.path.join(data_dir, "backups", "config.json.bak")
        self._state: str = self.STATE_UNINITIALIZED
        self._doc: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()
        if logger is not None:
            self._log: Logger = logger
        else:
            def _default_log(message: str) -> None:
                sys.stderr.write("[config] %s\n" % message)
                sys.stderr.flush()

            self._log = _default_log

    # -- 状态 ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def config_path(self) -> str:
        return self._config_path

    @property
    def backup_path(self) -> str:
        return self._backup_path

    # -- 生命周期（§5.3 状态机；每次公开操作重读磁盘——坏档即时暴露、
    #    人工处置后下一次操作即恢复） ----------------------------------------

    def load(self) -> Dict[str, Any]:
        """启动时调用（幂等）；返回当前文档或抛 ConfigError。"""
        with self._lock:
            return self._load_from_disk()

    def _load_from_disk(self) -> Dict[str, Any]:
        if not os.path.exists(self._config_path):
            # Uninitialized → Ready：写默认（revision=1，原子路径），不覆盖既有档
            doc = default_config()
            try:
                self._atomic_write(doc, previous_exists=False)
            except ConfigError:
                self._state = self.STATE_READ_ONLY_FAILED
                raise
            self._doc = doc
            self._state = self.STATE_READY
            self._log("首次初始化：写入默认配置 revision=%d" % doc["revision"])
            return copy.deepcopy(doc)

        try:
            with open(self._config_path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            self._state = self.STATE_READ_ONLY_FAILED
            raise ConfigError("DATA_CORRUPT", "config.json 读取失败：%s" % exc) from exc
        try:
            text = raw.decode("utf-8")
            doc = strict_loads(text)
        except (ValueError, RecursionError) as exc:
            return self._recover_from_backup("config.json 解析失败（%s）" % exc)
        try:
            _validate_document(doc, code="DATA_CORRUPT")
        except ConfigError as exc:
            if exc.code == "SCHEMA_UNSUPPORTED":
                # 未来 schema：拒写不删未知字段，无恢复动作，直接 ReadOnlyFailed
                self._state = self.STATE_READ_ONLY_FAILED
                self._log("schema_version=%r 高于支持版本 2：拒绝读写，请升级应用"
                          % doc.get("schema_version"))
                raise
            return self._recover_from_backup(
                "config.json 校验失败（%s）" % (exc.details.get("field") or exc.code)
            )
        self._doc = doc
        self._state = self.STATE_READY
        return copy.deepcopy(doc)

    def _recover_from_backup(self, reason: str) -> Dict[str, Any]:
        """Corrupted → 备份可解析且 schema 支持 → 原子拷贝回 config.json。"""
        if not os.path.exists(self._backup_path):
            self._state = self.STATE_READ_ONLY_FAILED
            self._log("%s；无可用备份，进入只读失败态（不建空库覆盖）" % reason)
            raise ConfigError("DATA_CORRUPT", "%s；且无可用备份" % reason)
        try:
            with open(self._backup_path, "rb") as handle:
                backup_raw = handle.read()
            backup_doc = strict_loads(backup_raw.decode("utf-8"))
            _validate_document(backup_doc, code="DATA_CORRUPT")
        except (OSError, ValueError, RecursionError) as exc:
            self._state = self.STATE_READ_ONLY_FAILED
            self._log("%s；备份同样不可用（%s），进入只读失败态" % (reason, exc))
            raise ConfigError("DATA_CORRUPT", "%s；备份同样损坏" % reason) from exc
        except ConfigError as exc:
            self._state = self.STATE_READ_ONLY_FAILED
            self._log("%s；备份校验失败（%s），进入只读失败态"
                      % (reason, exc.details.get("field") or exc.code))
            raise
        try:
            self._write_bytes_atomic(self._config_path, backup_raw)
        except ConfigError as exc:
            self._state = self.STATE_READ_ONLY_FAILED
            raise
        self._doc = backup_doc
        self._state = self.STATE_READY
        self._log("%s；已从备份恢复（revision=%d）" % (reason, backup_doc["revision"]))
        return copy.deepcopy(backup_doc)

    # -- 读 -------------------------------------------------------------------

    def get_config(self) -> Dict[str, Any]:
        """返回完整合并文档（含 revision）；ReadOnlyFailed → DATA_CORRUPT 不编造。"""
        with self._lock:
            return copy.deepcopy(self._load_from_disk())

    # -- 写（§5.4 算法） ---------------------------------------------------------

    def update_config(self, patch: Any, expected_revision: Any) -> Dict[str, Any]:
        with self._lock:
            if not _is_int(expected_revision):
                raise ConfigError("INVALID_PARAMS", "expected_revision 必须为整数",
                                  field="expected_revision")
            if not isinstance(patch, dict):
                raise ConfigError("INVALID_PARAMS", "patch 必须为对象", field="patch")
            current = self._load_from_disk()
            if expected_revision != current["revision"]:
                raise ConfigError(
                    "CONFLICT",
                    "expected_revision=%d 与当前 revision=%d 不一致，请重新加载后重试"
                    % (expected_revision, current["revision"]),
                    details={"expected_revision": expected_revision,
                             "actual_revision": current["revision"]},
                )
            # 4. patch 校验+深合并（在副本上进行，非法 patch 不变旧配置）
            merged = _deep_merge(current, patch, "", "INVALID_PARAMS")
            _validate_document(merged, code="INVALID_PARAMS")
            merged["revision"] = current["revision"] + 1
            self._atomic_write(merged, previous_exists=True)
            self._doc = merged
            self._state = self.STATE_READY
            self._log("update_config revision %d→%d，变更路径：%s"
                      % (current["revision"], merged["revision"],
                         self._patch_paths(patch)))
            return copy.deepcopy(merged)

    def reset_config(self, expected_revision: Any) -> Dict[str, Any]:
        """内容=默认值、revision 递增；不删除目标/脚本（03，属其他数据文件）。"""
        with self._lock:
            if not _is_int(expected_revision):
                raise ConfigError("INVALID_PARAMS", "expected_revision 必须为整数",
                                  field="expected_revision")
            current = self._load_from_disk()
            if expected_revision != current["revision"]:
                raise ConfigError(
                    "CONFLICT",
                    "expected_revision=%d 与当前 revision=%d 不一致，请重新加载后重试"
                    % (expected_revision, current["revision"]),
                    details={"expected_revision": expected_revision,
                             "actual_revision": current["revision"]},
                )
            doc = default_config()
            doc["revision"] = current["revision"] + 1
            self._log("reset_config revision %d→%d（内容重置为默认值）"
                      % (current["revision"], doc["revision"]))
            self._atomic_write(doc, previous_exists=True)
            self._doc = doc
            self._state = self.STATE_READY
            return copy.deepcopy(doc)

    @staticmethod
    def _patch_paths(patch: Dict[str, Any], prefix: str = "") -> str:
        """patch 变更字段路径清单（只记路径不记值——06 不记录参数全文）。"""
        paths = []
        for key, value in patch.items():
            sub = "%s.%s" % (prefix, key) if prefix else key
            if isinstance(value, dict):
                paths.append(ConfigService._patch_paths(value, sub))
            else:
                paths.append(sub)
        return ",".join(paths)

    # -- 原子保存（§5.4 步骤 6；故障 → STORAGE_ERROR retryable，旧档不破坏） -----

    def _write_bytes_atomic(self, target_path: str, raw: bytes) -> None:
        """字节级原子写：tmp 同卷 → flush+fsync → os.replace。"""
        tmp_path = target_path + ".tmp"
        try:
            with open(tmp_path, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target_path)
        except OSError as exc:
            raise ConfigError(
                "STORAGE_ERROR",
                "原子写入失败：%s" % exc,
                retryable=True,
            ) from exc

    def _atomic_write(self, doc: Dict[str, Any], previous_exists: bool) -> None:
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            payload = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False)
            raw = payload.encode("utf-8")
            with open(self._tmp_path, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if previous_exists:
                # 旧档拷贝至 backups/config.json.bak（单份覆盖，设计待确认②采纳）
                os.makedirs(os.path.dirname(self._backup_path), exist_ok=True)
                _copy_file(self._config_path, self._backup_path)
            os.replace(self._tmp_path, self._config_path)
        except OSError as exc:
            raise ConfigError("STORAGE_ERROR", "原子保存失败：%s" % exc,
                              retryable=True) from exc


# ---------------------------------------------------------------------------
# dispatcher 接线（03 命令目录：get_config/update_config/reset_config，data={config:...}）
# ---------------------------------------------------------------------------

# 全命令统一"未知参数即拒"（架构师裁决 §15.5：与 update/reset 口径一致）：
# 各命令允许的 params 键集；携带白名单外参数 → INVALID_PARAMS 并定位字段
_CONFIG_COMMAND_PARAMS: Dict[str, Tuple[str, ...]] = {
    "get_config": (),
    "update_config": ("expected_revision", "patch"),
    "reset_config": ("expected_revision",),
}


def register_config_commands(dispatcher: Any, service: ConfigService) -> None:
    """把 config 三命令注册到 dispatcher；ConfigError → DispatcherError 映射。"""
    from backend.ipc.dispatcher import DispatcherError

    def _wrap(command: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        allowed = _CONFIG_COMMAND_PARAMS[command]

        def _handler(envelope: Dict[str, Any]) -> Dict[str, Any]:
            params = envelope.get("params")
            if not isinstance(params, dict):
                params = {}
            try:
                for key in params:
                    if key not in allowed:
                        raise ConfigError(
                            "INVALID_PARAMS",
                            "%s 未声明参数: %s" % (command, key),
                            field=key,
                        )
                return handler(params)
            except ConfigError as exc:
                raise DispatcherError(
                    exc.code, exc.message, details=exc.details,
                    retryable=exc.retryable,
                ) from exc

        return _handler

    dispatcher.register(
        "get_config", _wrap("get_config", lambda params: {"config": service.get_config()})
    )
    dispatcher.register(
        "update_config",
        _wrap("update_config", lambda params: {
            "config": service.update_config(
                params.get("patch"), params.get("expected_revision")
            )
        }),
    )
    dispatcher.register(
        "reset_config",
        _wrap("reset_config", lambda params: {
            "config": service.reset_config(params.get("expected_revision"))
        }),
    )
