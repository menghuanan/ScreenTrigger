# -*- coding: utf-8 -*-
"""config.json 默认值与范围常量（TASK-006 批 B2，设计 §5.1；02 逐字零发明）。

- 默认值 = schema/config.schema.json 各字段 default 逐字段（= 02 config.json
  完整示例），以字面量声明；漂移防护由 test_config_service.py 运行时比对
  schema default（设计 §4.2"运行时读取比对防漂移"，比对在测试期执行，
  生产进程不依赖 schema 文件存在）；
- CONFIG_LIMITS = configLimits 块常量（与 schema/contract-cases.json
  thresholds.configLimits 逐字一致，B1 契约测试已双向比对）。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict

CONFIG_SCHEMA_VERSION: int = 2
INITIAL_REVISION: int = 1

DEFAULT_GENERAL: Dict[str, Any] = {
    "language": "zh-CN",
    "theme": "system",
}

DEFAULT_RECOGNITION: Dict[str, Any] = {
    "default_frequency_hz": 1,
    "default_threshold": 0.85,
    "rgb_tolerance": {"r": 15, "g": 15, "b": 15},
    "hsv_tolerance": {"h": 10, "s": 20, "v": 20},
    "ocr_model_id": None,
    "ocr_min_confidence": 0.7,
    "model_directory": "models",
}

DEFAULT_CAPTURE: Dict[str, Any] = {
    "preview_hz": 10,
    "png_compression_level": 6,
}

DEFAULT_INPUT: Dict[str, Any] = {
    "allow_exit": False,
    "move_duration_ms": 200,
    "type_interval_ms": 50,
}

DEFAULT_EXECUTION: Dict[str, Any] = {
    "total_timeout_ms": 0,
}

DEFAULT_HOTKEYS: Dict[str, Any] = {
    "start_selection": "Ctrl+Alt+S",
    "stop_script": "Ctrl+Alt+X",
    "pause_script": "Ctrl+Alt+P",
}

DEFAULT_LOGGING: Dict[str, Any] = {
    "level": "info",
    "rotate_bytes": 10485760,
    "file_count": 5,
}

# configLimits 块常量（schema/config.schema.json 顶层 configLimits 逐字）
CONFIG_LIMITS: Dict[str, Any] = {
    "defaultFrequencyHz": {"min": 0.1, "max": 30},
    "threshold": {"min": 0, "max": 1},
    "ocrMinConfidence": {"min": 0, "max": 1},
    "pngCompressionLevel": {"min": 0, "max": 9},
    "previewHz": {"min": 1, "max": 20},
    "moveDurationMs": {"min": 0, "max": 5000},
    "typeIntervalMs": {"min": 0, "max": 5000},
    "totalTimeoutMs": {"min": 0},
    "rotateBytesMin": 1048576,
    "rotateBytesMax": 104857600,
    "fileCount": {"min": 1, "max": 20},
    "themes": ["system", "light", "dark"],
    "loggingLevels": ["debug", "info", "warning", "error"],
    "languages": ["zh-CN"],
}


def default_config() -> Dict[str, Any]:
    """返回默认配置文档深拷贝（revision 初始 1；调用方可直接改写不伤常量）。"""
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "revision": INITIAL_REVISION,
        "general": copy.deepcopy(DEFAULT_GENERAL),
        "recognition": copy.deepcopy(DEFAULT_RECOGNITION),
        "capture": copy.deepcopy(DEFAULT_CAPTURE),
        "input": copy.deepcopy(DEFAULT_INPUT),
        "execution": copy.deepcopy(DEFAULT_EXECUTION),
        "hotkeys": copy.deepcopy(DEFAULT_HOTKEYS),
        "logging": copy.deepcopy(DEFAULT_LOGGING),
    }


def load_schema_limits(schema_path: str) -> Dict[str, Any]:
    """读取 schema/config.schema.json 的 configLimits 块（防漂移比对用）。"""
    with open(schema_path, "r", encoding="utf-8") as handle:
        return json.load(handle).get("configLimits", {})


def schema_defaults_path() -> str:
    """仓库内 config.schema.json 路径（测试期比对用；生产冻结环境不调用）。"""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "schema",
        "config.schema.json",
    )
