# IPC 通信协议

## 目录
1. [协议概述](#1-协议概述)
2. [命令列表](#2-命令列表)
3. [事件列表](#3-事件列表)
4. [错误处理](#4-错误处理)
5. [命令详细定义](#5-命令详细定义)

---

## 1. 协议概述

### 1.1 通信方式
- **Tauri → Python**: 通过 Tauri Command 调用
- **Python → Tauri**: 通过 Event Emit 发送事件

### 1.2 数据格式
所有数据使用 JSON 序列化，统一格式：

#### 请求格式
```json
{
  "command": "command_name",
  "params": {
    "param1": "value1",
    "param2": 123
  }
}
```

#### 响应格式（成功）
```json
{
  "success": true,
  "data": {
    "result": "..."
  }
}
```

#### 响应格式（失败）
```json
{
  "success": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "错误描述",
    "details": {}
  }
}
```

---

## 2. 命令列表

### 2.1 屏幕捕获类
| 命令名称 | 功能 | 优先级 |
|---------|------|--------|
| `get_display_list` | 获取显示器列表 | P0 |
| `capture_full_screen` | 全屏截图 | P0 |
| `capture_region` | 区域截图 | P0 |
| `capture_window` | 窗口截图 | P1 |
| `start_selection_mode` | 启动框选模式 | P0 |
| `stop_selection_mode` | 停止框选模式 | P0 |

### 2.2 目标管理类
| 命令名称 | 功能 | 优先级 |
|---------|------|--------|
| `create_target` | 创建目标 | P0 |
| `get_target` | 获取目标详情 | P0 |
| `list_targets` | 列出目标列表 | P0 |
| `update_target` | 更新目标 | P0 |
| `delete_target` | 删除目标 | P0 |
| `search_targets` | 搜索目标 | P1 |
| `create_group` | 创建分组 | P0 |
| `update_group` | 更新分组 | P0 |
| `delete_group` | 删除分组 | P0 |
| `move_target_to_group` | 移动目标到分组 | P0 |

### 2.3 识别类
| 命令名称 | 功能 | 优先级 |
|---------|------|--------|
| `recognize_text` | OCR识别 | P0 |
| `match_template` | 模板匹配 | P0 |
| `detect_color` | 颜色检测 | P0 |
| `evaluate_target_conditions` | 评估目标条件 | P0 |
| `test_recognition` | 测试识别（调试用） | P1 |

### 2.4 脚本执行类
| 命令名称 | 功能 | 优先级 |
|---------|------|--------|
| `execute_script` | 执行脚本 | P0 |
| `pause_script` | 暂停脚本 | P0 |
| `resume_script` | 恢复脚本 | P0 |
| `stop_script` | 停止脚本 | P0 |
| `get_script_state` | 获取脚本状态 | P0 |
| `list_scripts` | 列出脚本 | P0 |
| `save_script` | 保存脚本 | P0 |
| `load_script` | 加载脚本 | P0 |
| `validate_script` | 验证脚本语法 | P1 |

### 2.5 调试类
| 命令名称 | 功能 | 优先级 |
|---------|------|--------|
| `add_breakpoint` | 添加断点 | P0 |
| `remove_breakpoint` | 移除断点 | P0 |
| `list_breakpoints` | 列出断点 | P0 |
| `step_over` | 单步跳过 | P0 |
| `step_into` | 单步进入 | P0 |
| `step_out` | 单步跳出 | P0 |
| `continue_execution` | 继续执行 | P0 |
| `get_variables` | 获取变量 | P0 |
| `evaluate_expression` | 执行表达式 | P1 |

### 2.6 输入模拟类
| 命令名称 | 功能 | 优先级 |
|---------|------|--------|
| `move_mouse` | 移动鼠标 | P0 |
| `click_mouse` | 点击鼠标 | P0 |
| `scroll_mouse` | 滚动鼠标 | P0 |
| `press_key` | 按下按键 | P0 |
| `type_text` | 输入文本 | P0 |
| `press_hotkey` | 按下快捷键 | P0 |
| `set_region_limit` | 设置区域限制 | P1 |
| `clear_region_limit` | 清除区域限制 | P1 |

### 2.7 窗口管理类
| 命令名称 | 功能 | 优先级 |
|---------|------|--------|
| `list_windows` | 列出窗口 | P1 |
| `get_window_info` | 获取窗口信息 | P1 |
| `bind_window` | 绑定窗口 | P1 |
| `unbind_window` | 解绑窗口 | P1 |
| `get_bound_window` | 获取绑定的窗口 | P1 |

### 2.8 配置类
| 命令名称 | 功能 | 优先级 |
|---------|------|--------|
| `get_config` | 获取配置 | P0 |
| `update_config` | 更新配置 | P0 |
| `reset_config` | 重置配置 | P1 |
| `register_hotkey` | 注册热键 | P1 |
| `unregister_hotkey` | 注销热键 | P1 |

---

## 3. 事件列表

### 3.1 系统事件
| 事件名称 | 触发时机 | 数据 |
|---------|---------|------|
| `system_ready` | Python后端启动完成 | `{"version": "1.0.0"}` |
| `system_error` | 系统错误 | `{"error": {...}}` |

### 3.2 捕获事件
| 事件名称 | 触发时机 | 数据 |
|---------|---------|------|
| `selection_started` | 开始框选 | `{}` |
| `selection_updated` | 框选区域变化 | `{"x": 100, "y": 100, "width": 200, "height": 150}` |
| `selection_completed` | 框选完成 | `{"region": {...}, "image_data": "base64..."}` |
| `selection_cancelled` | 取消框选 | `{}` |

### 3.3 识别事件
| 事件名称 | 触发时机 | 数据 |
|---------|---------|------|
| `target_detected` | 检测到目标 | `{"target_id": "...", "position": {...}, "confidence": 0.92}` |
| `recognition_failed` | 识别失败 | `{"target_id": "...", "reason": "..."}` |

### 3.4 脚本执行事件
| 事件名称 | 触发时机 | 数据 |
|---------|---------|------|
| `script_started` | 脚本开始执行 | `{"execution_id": "...", "script_id": "..."}` |
| `script_paused` | 脚本暂停 | `{"execution_id": "...", "line": 42}` |
| `script_resumed` | 脚本恢复 | `{"execution_id": "..."}` |
| `script_stopped` | 脚本停止 | `{"execution_id": "...", "reason": "manual"}` |
| `script_completed` | 脚本完成 | `{"execution_id": "...", "result": {...}}` |
| `script_error` | 脚本错误 | `{"execution_id": "...", "error": {...}}` |
| `script_iteration` | 循环迭代 | `{"execution_id": "...", "iteration": 5}` |

### 3.5 调试事件
| 事件名称 | 触发时机 | 数据 |
|---------|---------|------|
| `breakpoint_hit` | 断点命中 | `{"breakpoint_id": "...", "line": 15, "variables": {...}}` |
| `variable_changed` | 变量变化 | `{"name": "count", "old_value": 1, "new_value": 2}` |
| `step_completed` | 单步执行完成 | `{"line": 16, "variables": {...}}` |

### 3.6 日志事件
| 事件名称 | 触发时机 | 数据 |
|---------|---------|------|
| `log_message` | 日志输出 | `{"level": "info", "message": "...", "timestamp": "..."}` |

---

## 4. 错误处理

### 4.1 错误码定义
```typescript
enum ErrorCode {
  // 通用错误 (1000-1099)
  UNKNOWN_ERROR = 1000,
  INVALID_PARAMS = 1001,
  INTERNAL_ERROR = 1002,
  NOT_IMPLEMENTED = 1003,
  
  // 捕获错误 (1100-1199)
  DISPLAY_NOT_FOUND = 1100,
  CAPTURE_FAILED = 1101,
  INVALID_REGION = 1102,
  
  // 目标错误 (1200-1299)
  TARGET_NOT_FOUND = 1200,
  TARGET_ALREADY_EXISTS = 1201,
  GROUP_NOT_FOUND = 1202,
  
  // 识别错误 (1300-1399)
  OCR_FAILED = 1300,
  TEMPLATE_MATCH_FAILED = 1301,
  COLOR_DETECT_FAILED = 1302,
  
  // 脚本错误 (1400-1499)
  SCRIPT_NOT_FOUND = 1400,
  SCRIPT_SYNTAX_ERROR = 1401,
  SCRIPT_RUNTIME_ERROR = 1402,
  SCRIPT_TIMEOUT = 1403,
  
  // 输入错误 (1500-1599)
  INPUT_FAILED = 1500,
  REGION_LIMIT_VIOLATION = 1501,
  
  // 窗口错误 (1600-1699)
  WINDOW_NOT_FOUND = 1600,
  WINDOW_OPERATION_FAILED = 1601,
}
```

### 4.2 错误响应示例
```json
{
  "success": false,
  "error": {
    "code": 1200,
    "message": "目标不存在",
    "details": {
      "target_id": "550e8400-e29b-41d4-a716-446655440000",
      "requested_at": "2026-09-21T15:30:00Z"
    }
  }
}
```

---

## 5. 命令详细定义

### 5.1 屏幕捕获类

#### get_display_list
获取所有显示器信息

**请求**
```json
{
  "command": "get_display_list",
  "params": {}
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "displays": [
      {
        "index": 0,
        "x": 0,
        "y": 0,
        "width": 1920,
        "height": 1080,
        "dpi_scale": 1.0,
        "is_primary": true,
        "name": "显示器 1"
      }
    ]
  }
}
```

#### capture_region
截取指定区域

**请求**
```json
{
  "command": "capture_region",
  "params": {
    "x": 100,
    "y": 100,
    "width": 200,
    "height": 150,
    "display_index": 0,
    "return_format": "base64"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "image_data": "iVBORw0KGgoAAAANSUhEUgA...",
    "format": "png",
    "width": 200,
    "height": 150,
    "captured_at": "2026-09-21T15:30:00Z"
  }
}
```

#### start_selection_mode
启动屏幕框选模式

**请求**
```json
{
  "command": "start_selection_mode",
  "params": {
    "display_index": 0,
    "show_preview": true,
    "show_coordinates": true
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "session_id": "selection-001"
  }
}
```

**相关事件**
- `selection_started`
- `selection_updated` (持续触发)
- `selection_completed` 或 `selection_cancelled`

---

### 5.2 目标管理类

#### create_target
创建新目标

**请求**
```json
{
  "command": "create_target",
  "params": {
    "name": "登录按钮",
    "group_id": "group-001",
    "image_data": "base64...",
    "save_compressed": false,
    "conditions": [
      {
        "type": "template",
        "operator": "and",
        "params": {
          "threshold": 0.85
        }
      }
    ],
    "metadata": {
      "notes": "游戏登录界面"
    }
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "target_id": "550e8400-e29b-41d4-a716-446655440000",
    "created_at": "2026-09-21T15:30:00Z"
  }
}
```

#### list_targets
列出目标

**请求**
```json
{
  "command": "list_targets",
  "params": {
    "group_id": "group-001",
    "limit": 50,
    "offset": 0,
    "sort_by": "created_at",
    "order": "desc"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "targets": [
      {
        "id": "...",
        "name": "登录按钮",
        "group_id": "group-001",
        "thumbnail_url": "file:///.../thumbnails/xxx_thumb.png",
        "created_at": "2026-09-21T15:30:00Z"
      }
    ],
    "total": 100,
    "limit": 50,
    "offset": 0
  }
}
```

#### update_target
更新目标

**请求**
```json
{
  "command": "update_target",
  "params": {
    "target_id": "550e8400-e29b-41d4-a716-446655440000",
    "updates": {
      "name": "新名称",
      "conditions": [...]
    }
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "updated_at": "2026-09-21T16:00:00Z"
  }
}
```

#### delete_target
删除目标

**请求**
```json
{
  "command": "delete_target",
  "params": {
    "target_id": "550e8400-e29b-41d4-a716-446655440000",
    "delete_image": true
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {}
}
```

---

### 5.3 识别类

#### recognize_text
OCR文字识别

**请求**
```json
{
  "command": "recognize_text",
  "params": {
    "image_data": "base64...",
    "language": "multi",
    "min_confidence": 0.7,
    "region": null
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "results": [
      {
        "text": "登录",
        "confidence": 0.95,
        "bbox": [
          {"x": 100, "y": 100},
          {"x": 150, "y": 100},
          {"x": 150, "y": 130},
          {"x": 100, "y": 130}
        ],
        "language": "zh"
      }
    ],
    "recognition_time": 0.15
  }
}
```

#### match_template
模板匹配

**请求**
```json
{
  "command": "match_template",
  "params": {
    "source_image": "base64...",
    "template_image": "base64...",
    "threshold": 0.85,
    "max_results": 10,
    "region": null
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "matches": [
      {
        "x": 500,
        "y": 300,
        "width": 100,
        "height": 50,
        "confidence": 0.92
      }
    ],
    "recognition_time": 0.05
  }
}
```

#### detect_color
颜色检测

**请求**
```json
{
  "command": "detect_color",
  "params": {
    "image_data": "base64...",
    "color_space": "rgb",
    "color": {
      "r": 59,
      "g": 109,
      "b": 255
    },
    "tolerance": {
      "r": 15,
      "g": 15,
      "b": 15
    },
    "region": null
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "matches": [
      {
        "x": 200,
        "y": 200,
        "pixel_count": 150,
        "percentage": 5.2
      }
    ],
    "recognition_time": 0.02
  }
}
```

#### evaluate_target_conditions
评估目标的所有条件

**请求**
```json
{
  "command": "evaluate_target_conditions",
  "params": {
    "target_id": "550e8400-e29b-41d4-a716-446655440000",
    "image_data": "base64...",
    "region": null
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "result": true,
    "details": [
      {
        "condition_index": 0,
        "type": "template",
        "matched": true,
        "data": {
          "confidence": 0.92,
          "position": {"x": 500, "y": 300}
        }
      },
      {
        "condition_index": 1,
        "type": "ocr",
        "matched": true,
        "data": {
          "text": "登录",
          "confidence": 0.95
        }
      }
    ],
    "evaluation_time": 0.2
  }
}
```

---

### 5.4 脚本执行类

#### execute_script
执行脚本

**请求**
```json
{
  "command": "execute_script",
  "params": {
    "script_id": "script-001",
    "mode": "loop_until_stop",
    "max_iterations": null,
    "args": {
      "username": "player1",
      "retry_times": 3
    },
    "bound_window_handle": null,
    "region_limit": null
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "execution_id": "exec-20260921-153045"
  }
}
```

**相关事件**
- `script_started`
- `script_iteration` (循环模式)
- `script_completed` 或 `script_error`

#### pause_script
暂停脚本

**请求**
```json
{
  "command": "pause_script",
  "params": {
    "execution_id": "exec-20260921-153045"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "paused_at_line": 42
  }
}
```

#### get_script_state
获取脚本执行状态

**请求**
```json
{
  "command": "get_script_state",
  "params": {
    "execution_id": "exec-20260921-153045"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "status": "running",
    "current_line": 42,
    "current_iteration": 5,
    "variables": {
      "retry_count": 2
    },
    "elapsed_time": 12.5
  }
}
```

#### save_script
保存脚本

**请求**
```json
{
  "command": "save_script",
  "params": {
    "script_id": "script-001",
    "name": "自动登录",
    "code": "-- Lua代码\nfunction main()\n  ...\nend",
    "description": "自动完成登录",
    "parameters": [
      {
        "name": "username",
        "type": "string",
        "required": true,
        "default": ""
      }
    ]
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "script_id": "script-001",
    "saved_at": "2026-09-21T16:00:00Z"
  }
}
```

---

### 5.5 调试类

#### add_breakpoint
添加断点

**请求**
```json
{
  "command": "add_breakpoint",
  "params": {
    "script_path": "scripts/auto_login.lua",
    "line": 15,
    "condition": "retry_count > 2"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "breakpoint_id": "bp-001"
  }
}
```

#### step_over
单步跳过

**请求**
```json
{
  "command": "step_over",
  "params": {
    "execution_id": "exec-20260921-153045"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "current_line": 16,
    "variables": {
      "count": 2
    }
  }
}
```

#### get_variables
获取变量

**请求**
```json
{
  "command": "get_variables",
  "params": {
    "execution_id": "exec-20260921-153045",
    "scope": "local"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "variables": {
      "username": "player1",
      "retry_count": 2,
      "last_result": true
    }
  }
}
```

---

### 5.6 输入模拟类

#### move_mouse
移动鼠标

**请求**
```json
{
  "command": "move_mouse",
  "params": {
    "x": 500,
    "y": 300,
    "duration": 0.2,
    "relative": false
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {}
}
```

#### click_mouse
点击鼠标

**请求**
```json
{
  "command": "click_mouse",
  "params": {
    "x": 500,
    "y": 300,
    "button": "left",
    "clicks": 1
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {}
}
```

#### type_text
输入文本

**请求**
```json
{
  "command": "type_text",
  "params": {
    "text": "Hello World",
    "interval": 0.05
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {}
}
```

#### press_hotkey
按下快捷键

**请求**
```json
{
  "command": "press_hotkey",
  "params": {
    "keys": ["ctrl", "c"]
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {}
}
```

#### set_region_limit
设置区域限制

**请求**
```json
{
  "command": "set_region_limit",
  "params": {
    "region": {
      "x": 100,
      "y": 100,
      "width": 800,
      "height": 600,
      "allow_exit": true
    }
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {}
}
```

---

### 5.7 窗口管理类

#### list_windows
列出所有窗口

**请求**
```json
{
  "command": "list_windows",
  "params": {
    "include_hidden": false
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "windows": [
      {
        "handle": 123456,
        "title": "游戏窗口",
        "class_name": "UnityWndClass",
        "process_id": 7890,
        "process_name": "Game.exe",
        "rect": {
          "x": 0,
          "y": 0,
          "width": 1920,
          "height": 1080
        },
        "is_visible": true,
        "is_minimized": false
      }
    ]
  }
}
```

#### bind_window
绑定窗口

**请求**
```json
{
  "command": "bind_window",
  "params": {
    "handle": 123456
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "window_info": {
      "handle": 123456,
      "title": "游戏窗口"
    }
  }
}
```

---

### 5.8 配置类

#### get_config
获取配置

**请求**
```json
{
  "command": "get_config",
  "params": {
    "section": "recognition"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "config": {
      "ocr": {
        "min_confidence": 0.7,
        "use_gpu": false
      },
      "template": {
        "default_threshold": 0.85
      }
    }
  }
}
```

#### update_config
更新配置

**请求**
```json
{
  "command": "update_config",
  "params": {
    "section": "recognition.ocr",
    "value": {
      "min_confidence": 0.8,
      "use_gpu": true
    }
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "updated_at": "2026-09-21T16:00:00Z"
  }
}
```

#### register_hotkey
注册全局热键

**请求**
```json
{
  "command": "register_hotkey",
  "params": {
    "name": "start_selection",
    "keys": ["ctrl", "alt", "s"],
    "action": "start_selection_mode"
  }
}
```

**响应**
```json
{
  "success": true,
  "data": {
    "hotkey_id": "hk-001"
  }
}
```

---

## 6. 性能要求

### 6.1 响应时间
- 捕获命令: < 100ms
- 识别命令: < 500ms
- 输入模拟: < 50ms
- 查询命令: < 100ms

### 6.2 并发处理
- 同时支持最多3个脚本执行
- 识别操作可并行执行
- 输入操作串行执行

### 6.3 超时设置
- 命令超时: 30秒
- 脚本执行超时: 3600秒（可配置）
- 识别超时: 5秒
