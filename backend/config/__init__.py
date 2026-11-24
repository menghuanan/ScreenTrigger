# -*- coding: utf-8 -*-
"""backend.config barrel（TASK-006 批 B2）。"""

from .defaults import (
    CONFIG_LIMITS,
    CONFIG_SCHEMA_VERSION,
    INITIAL_REVISION,
    default_config,
    load_schema_limits,
    schema_defaults_path,
)
from .service import ConfigError, ConfigService, register_config_commands

__all__ = [
    "CONFIG_LIMITS",
    "CONFIG_SCHEMA_VERSION",
    "INITIAL_REVISION",
    "ConfigError",
    "ConfigService",
    "default_config",
    "load_schema_limits",
    "register_config_commands",
    "schema_defaults_path",
]
