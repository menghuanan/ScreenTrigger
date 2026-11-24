# Lua 脚本 API 文档

## 目录
1. [全局变量](#1-全局变量)
2. [目标检测API](#2-目标检测api)
3. [输入控制API](#3-输入控制api)
4. [识别API](#4-识别api)
5. [流程控制API](#5-流程控制api)
6. [脚本互调API](#6-脚本互调api)
7. [工具函数API](#7-工具函数api)
8. [示例脚本](#8-示例脚本)

---

## 1. 全局变量

### 1.1 args (table)
脚本启动时传入的参数

```lua
-- 示例：用户传入 {"username": "player1", "retry": 3}
print(args.username)  -- "player1"
print(args.retry)     -- 3
```

### 1.2 SCRIPT_ID (string)
当前脚本的唯一标识

```lua
print(SCRIPT_ID)  -- "script-001"
```

### 1.3 EXECUTION_ID (string)
当前执行会话的唯一标识

```lua
print(EXECUTION_ID)  -- "exec-20260921-153045"
```

### 1.4 VERSION (string)
ScreenTrigger 引擎版本

```lua
print(VERSION)  -- "1.0.0"
```

---

## 2. 目标检测API

### 2.1 wait_for_target
等待目标出现

```lua
wait_for_target(target_id: string, [timeout: number], [interval: number]) 
  -> (found: boolean, position: table|nil)
```

**参数**
- `target_id`: 目标ID
- `timeout`: 超时时间（秒），默认30秒，0表示无限等待
- `interval`: 检测间隔（秒），默认使用全局配置

**返回值**
- `found`: 是否找到
- `position`: 位置信息 `{x, y, width, height, confidence}`

**示例**
```lua
local found, pos = wait_for_target("target-001", 10, 1)
if found then
  print("找到目标，位置:", pos.x, pos.y)
  print("置信度:", pos.confidence)
else
  print("超时未找到")
end
```

### 2.2 find_target
立即检测目标（不等待）

```lua
find_target(target_id: string, [region: table]) 
  -> (found: boolean, positions: table)
```

**参数**
- `target_id`: 目标ID
- `region`: 搜索区域 `{x, y, width, height}`，nil表示全屏

**返回值**
- `found`: 是否找到
- `positions`: 位置数组（可能多个匹配）

**示例**
```lua
local found, positions = find_target("target-001")
if found then
  for i, pos in ipairs(positions) do
    print("匹配", i, ":", pos.x, pos.y, "置信度:", pos.confidence)
  end
end
```

### 2.3 wait_for_any_target
等待多个目标中的任意一个出现

```lua
wait_for_any_target(target_ids: table, [timeout: number]) 
  -> (found_id: string|nil, position: table|nil)
```

**参数**
- `target_ids`: 目标ID数组
- `timeout`: 超时时间（秒）

**返回值**
- `found_id`: 找到的目标ID
- `position`: 位置信息

**示例**
```lua
local targets = {"target-001", "target-002", "target-003"}
local found_id, pos = wait_for_any_target(targets, 15)
if found_id then
  print("找到目标:", found_id)
end
```

### 2.4 wait_for_all_targets
等待所有目标都出现

```lua
wait_for_all_targets(target_ids: table, [timeout: number]) 
  -> (found: boolean, positions: table)
```

**参数**
- `target_ids`: 目标ID数组
- `timeout`: 超时时间（秒）

**返回值**
- `found`: 是否全部找到
- `positions`: 位置映射表 `{[target_id] = position}`

**示例**
```lua
local found, positions = wait_for_all_targets({"target-001", "target-002"}, 20)
if found then
  print("目标1位置:", positions["target-001"].x)
  print("目标2位置:", positions["target-002"].x)
end
```

### 2.5 wait_until_target_disappear
等待目标消失

```lua
wait_until_target_disappear(target_id: string, [timeout: number]) 
  -> (disappeared: boolean)
```

**示例**
```lua
-- 等待加载动画消失
local disappeared = wait_until_target_disappear("loading-icon", 30)
if disappeared then
  print("加载完成")
end
```

---

## 3. 输入控制API

### 3.1 鼠标控制

#### move_to
移动鼠标到指定坐标

```lua
move_to(x: number, y: number, [duration: number]) -> nil
```

**参数**
- `x, y`: 目标坐标（屏幕绝对坐标）
- `duration`: 移动时长（秒），0表示瞬移，默认0.2

**示例**
```lua
move_to(500, 300)           -- 瞬移
move_to(500, 300, 0.5)      -- 0.5秒平滑移动
```

#### move_to_target
移动鼠标到目标位置

```lua
move_to_target(target_id: string, [offset_x: number], [offset_y: number], [duration: number]) 
  -> (success: boolean)
```

**参数**
- `target_id`: 目标ID
- `offset_x, offset_y`: 相对目标中心的偏移，默认(0, 0)
- `duration`: 移动时长

**示例**
```lua
-- 移动到按钮中心
move_to_target("button-login")

-- 移动到目标右上角
move_to_target("button-login", 40, -20)
```

#### click
点击鼠标

```lua
click([button: string], [clicks: number]) -> nil
```

**参数**
- `button`: "left" | "right" | "middle"，默认"left"
- `clicks`: 点击次数，默认1

**示例**
```lua
click()                  -- 左键单击
click("right")           -- 右键单击
click("left", 2)         -- 左键双击
```

#### click_at
在指定位置点击

```lua
click_at(x: number, y: number, [button: string], [clicks: number]) -> nil
```

**示例**
```lua
click_at(500, 300)              -- 左键单击 (500, 300)
click_at(500, 300, "right")     -- 右键单击
```

#### click_target
点击目标

```lua
click_target(target_id: string, [offset_x: number], [offset_y: number], [button: string]) 
  -> (success: boolean)
```

**示例**
```lua
-- 点击按钮中心
click_target("button-login")

-- 点击目标的偏移位置
click_target("dropdown", 0, 20, "left")
```

#### drag
拖拽鼠标

```lua
drag(from_x: number, from_y: number, to_x: number, to_y: number, [duration: number]) -> nil
```

**示例**
```lua
-- 从 (100, 100) 拖拽到 (300, 300)，耗时1秒
drag(100, 100, 300, 300, 1.0)
```

#### scroll
滚动鼠标滚轮

```lua
scroll(delta: number) -> nil
```

**参数**
- `delta`: 滚动量，正数向上，负数向下

**示例**
```lua
scroll(5)    -- 向上滚动5格
scroll(-3)   -- 向下滚动3格
```

### 3.2 键盘控制

#### press_key
按下并释放按键

```lua
press_key(key: string) -> nil
```

**参数**
- `key`: 按键名称（见按键名称表）

**示例**
```lua
press_key("a")
press_key("enter")
press_key("F1")
```

#### key_down / key_up
按下/释放按键

```lua
key_down(key: string) -> nil
key_up(key: string) -> nil
```

**示例**
```lua
key_down("shift")
press_key("a")      -- Shift+A
key_up("shift")
```

#### type_text
输入文本

```lua
type_text(text: string, [interval: number]) -> nil
```

**参数**
- `text`: 要输入的文本
- `interval`: 字符间隔（秒），默认0.05

**示例**
```lua
type_text("Hello World")
type_text("快速输入", 0.02)
```

#### hotkey
按下快捷键

```lua
hotkey(...keys: string) -> nil
```

**示例**
```lua
hotkey("ctrl", "c")           -- Ctrl+C
hotkey("ctrl", "shift", "s")  -- Ctrl+Shift+S
hotkey("alt", "F4")           -- Alt+F4
```

#### paste_text
粘贴文本（通过剪贴板）

```lua
paste_text(text: string) -> nil
```

**示例**
```lua
paste_text("大量文本...")  -- 比 type_text 更快
```

### 3.3 按键名称表
- **字母**: "a" - "z" (小写)
- **数字**: "0" - "9"
- **功能键**: "F1" - "F12"
- **方向键**: "up", "down", "left", "right"
- **控制键**: "enter", "space", "tab", "backspace", "delete", "esc"
- **修饰键**: "ctrl", "shift", "alt", "win"
- **其他**: "home", "end", "pageup", "pagedown", "insert", "printscreen"

---

## 4. 识别API

### 4.1 capture_screen
截取屏幕

```lua
capture_screen([region: table]) -> (image_data: string)
```

**参数**
- `region`: 区域 `{x, y, width, height}`，nil表示全屏

**返回值**
- `image_data`: Base64编码的图像数据

**示例**
```lua
local image = capture_screen()
local region_image = capture_screen({x=100, y=100, width=200, height=150})
```

### 4.2 ocr
OCR文字识别

```lua
ocr([image_data: string], [region: table]) -> (texts: table)
```

**参数**
- `image_data`: 图像数据（Base64），nil表示截取当前屏幕
- `region`: 识别区域

**返回值**
- `texts`: 识别结果数组

**示例**
```lua
local texts = ocr()
for i, result in ipairs(texts) do
  print("文本:", result.text)
  print("置信度:", result.confidence)
  print("位置:", result.bbox[1].x, result.bbox[1].y)
end

-- 只识别指定区域
local texts = ocr(nil, {x=100, y=100, width=300, height=50})
```

### 4.3 find_text
查找文本

```lua
find_text(text: string, [fuzzy: boolean], [region: table]) 
  -> (found: boolean, positions: table)
```

**参数**
- `text`: 要查找的文本
- `fuzzy`: 是否模糊匹配，默认false
- `region`: 搜索区域

**示例**
```lua
local found, positions = find_text("登录")
if found then
  print("找到文本，位置:", positions[1].x, positions[1].y)
end

-- 模糊匹配
local found, positions = find_text("開始", true)
```

### 4.4 find_color
查找颜色

```lua
find_color(color: table, [tolerance: table], [region: table]) 
  -> (found: boolean, positions: table)
```

**参数**
- `color`: RGB `{r, g, b}` 或 HSV `{h, s, v}`
- `tolerance`: 容差 `{r, g, b}` 或 `{h, s, v}`
- `region`: 搜索区域

**示例**
```lua
-- 查找红色
local found, positions = find_color(
  {r=255, g=0, b=0}, 
  {r=15, g=15, b=15}
)

-- 查找蓝色（HSV）
local found, positions = find_color(
  {h=220, s=75, v=100},
  {h=10, s=20, v=20}
)
```

### 4.5 get_pixel_color
获取像素颜色

```lua
get_pixel_color(x: number, y: number) -> (color: table)
```

**返回值**
- `color`: RGB颜色 `{r, g, b}`

**示例**
```lua
local color = get_pixel_color(500, 300)
print("颜色:", color.r, color.g, color.b)
```

---

## 5. 流程控制API

### 5.1 wait
等待指定时间

```lua
wait(seconds: number) -> nil
```

**示例**
```lua
wait(1)      -- 等待1秒
wait(0.5)    -- 等待0.5秒
```

### 5.2 wait_random
随机等待

```lua
wait_random(min: number, max: number) -> nil
```

**示例**
```lua
wait_random(0.5, 1.5)  -- 随机等待0.5-1.5秒
```

### 5.3 log
输出日志

```lua
log(message: string, [level: string]) -> nil
```

**参数**
- `message`: 日志内容
- `level`: "debug" | "info" | "warning" | "error"，默认"info"

**示例**
```lua
log("开始执行脚本")
log("检测到目标", "debug")
log("执行失败", "error")
```

### 5.4 stop_script
停止脚本

```lua
stop_script([reason: string]) -> nil
```

**示例**
```lua
if not found then
  log("未找到目标，停止脚本", "error")
  stop_script("目标未找到")
end
```

### 5.5 should_stop
检查是否应该停止

```lua
should_stop() -> (should_stop: boolean)
```

**示例**
```lua
while true do
  if should_stop() then
    log("收到停止信号")
    break
  end
  -- 执行任务
  wait(1)
end
```

---

## 6. 脚本互调API

### 6.1 call_script
调用其他脚本

```lua
call_script(script_path: string, [args: table]) -> (result: any)
```

**参数**
- `script_path`: 脚本路径（相对于scripts目录）
- `args`: 传递给子脚本的参数

**返回值**
- `result`: 子脚本的返回值

**示例**
```lua
-- 调用计算脚本
local result = call_script("utils/calculate.lua", {
  a = 10,
  b = 20
})
print("计算结果:", result)

-- 调用复杂操作脚本
call_script("tasks/complex_task.lua", {
  target_id = "target-001",
  retry = 3
})
```

### 6.2 子脚本返回值
子脚本通过 `return` 返回值

```lua
-- calculate.lua
function main()
  local sum = args.a + args.b
  return sum
end

return main()
```

---

## 7. 工具函数API

### 7.1 get_screen_size
获取屏幕尺寸

```lua
get_screen_size([display_index: number]) -> (width: number, height: number)
```

**示例**
```lua
local width, height = get_screen_size()
print("屏幕尺寸:", width, "x", height)
```

### 7.2 get_mouse_position
获取鼠标位置

```lua
get_mouse_position() -> (x: number, y: number)
```

**示例**
```lua
local x, y = get_mouse_position()
print("鼠标位置:", x, y)
```

### 7.3 set_recognition_frequency
设置识别频率

```lua
set_recognition_frequency(fps: number) -> nil
```

**参数**
- `fps`: 每秒识别次数（0.1 - 5.0）

**示例**
```lua
set_recognition_frequency(2)  -- 每秒识别2次
```

### 7.4 get_clipboard
获取剪贴板内容

```lua
get_clipboard() -> (text: string)
```

**示例**
```lua
local text = get_clipboard()
print("剪贴板内容:", text)
```

### 7.5 set_clipboard
设置剪贴板内容

```lua
set_clipboard(text: string) -> nil
```

**示例**
```lua
set_clipboard("复制的文本")
```

### 7.6 random
生成随机数

```lua
random([min: number], [max: number]) -> (value: number)
```

**示例**
```lua
local r = random()           -- 0-1之间的随机数
local r = random(10)         -- 0-10之间的随机整数
local r = random(5, 15)      -- 5-15之间的随机整数
```

### 7.7 get_time
获取当前时间戳

```lua
get_time() -> (timestamp: number)
```

**示例**
```lua
local start = get_time()
-- 执行任务
local elapsed = get_time() - start
log("耗时: " .. elapsed .. "秒")
```

### 7.8 beep
播放提示音

```lua
beep([frequency: number], [duration: number]) -> nil
```

**参数**
- `frequency`: 频率（Hz），默认1000
- `duration`: 时长（秒），默认0.2

**示例**
```lua
beep()              -- 默认提示音
beep(2000, 0.5)     -- 2000Hz，0.5秒
```

---

## 8. 示例脚本

### 8.1 基础示例：自动登录
```lua
-- auto_login.lua
function main()
  log("开始自动登录")
  
  -- 等待登录按钮出现
  local found, pos = wait_for_target("button-login", 30)
  if not found then
    log("未找到登录按钮", "error")
    return false
  end
  
  -- 点击登录按钮
  click_target("button-login")
  wait(1)
  
  -- 输入用户名
  local found_username = wait_for_target("input-username", 10)
  if found_username then
    click_target("input-username")
    wait(0.5)
    type_text(args.username)
  end
  
  -- 输入密码
  wait(0.5)
  press_key("tab")
  type_text(args.password)
  
  -- 点击确认
  wait(0.5)
  press_key("enter")
  
  log("登录完成")
  return true
end

return main()
```

### 8.2 循环检测：自动采集
```lua
-- auto_collect.lua
function main()
  log("开始自动采集")
  
  local count = 0
  local max_count = args.max_count or 10
  
  while count < max_count do
    -- 检查是否应该停止
    if should_stop() then
      log("收到停止信号")
      break
    end
    
    -- 查找采集目标
    local found, positions = find_target("collect-icon")
    
    if found then
      -- 点击所有找到的目标
      for i, pos in ipairs(positions) do
        click_at(pos.x, pos.y)
        wait_random(0.3, 0.5)
        count = count + 1
      end
      log("已采集 " .. count .. " 个目标")
    else
      -- 没有目标，等待刷新
      log("等待目标刷新...")
      wait(5)
    end
  end
  
  log("采集完成，共 " .. count .. " 个")
  beep()
  return count
end

return main()
```

### 8.3 条件判断：根据颜色执行不同操作
```lua
-- check_status.lua
function main()
  log("检查状态")
  
  -- 截取状态栏区域
  local region = {x=1800, y=50, width=100, height=30}
  local image = capture_screen(region)
  
  -- 检测颜色
  local is_green = find_color({r=0, g=255, b=0}, {r=20, g=20, b=20}, region)
  local is_red = find_color({r=255, g=0, b=0}, {r=20, g=20, b=20}, region)
  
  if is_green then
    log("状态正常（绿色）")
    -- 继续任务
    call_script("tasks/continue_task.lua")
  elseif is_red then
    log("状态异常（红色）", "warning")
    -- 执行恢复操作
    call_script("tasks/recovery.lua")
  else
    log("状态未知", "warning")
  end
end

return main()
```

### 8.4 复杂流程：多目标等待
```lua
-- complex_flow.lua
function main()
  log("开始复杂流程")
  
  -- 等待三个目标中的任意一个
  local targets = {
    "result-success",
    "result-failure", 
    "timeout-icon"
  }
  
  local found_id, pos = wait_for_any_target(targets, 60)
  
  if found_id == "result-success" then
    log("任务成功！")
    beep(1500, 0.3)
    return "success"
    
  elseif found_id == "result-failure" then
    log("任务失败，重试...", "warning")
    wait(2)
    -- 递归调用自己重试
    return call_script("complex_flow.lua", args)
    
  elseif found_id == "timeout-icon" then
    log("任务超时", "error")
    return "timeout"
    
  else
    log("未检测到任何结果", "error")
    return "unknown"
  end
end

return main()
```

### 8.5 OCR识别：提取数字
```lua
-- extract_number.lua
function main()
  log("提取数字")
  
  -- 识别指定区域的文字
  local region = {x=100, y=100, width=300, height=50}
  local texts = ocr(nil, region)
  
  -- 提取数字
  local number = nil
  for i, result in ipairs(texts) do
    -- 尝试转换为数字
    local n = tonumber(result.text)
    if n then
      number = n
      log("找到数字: " .. number)
      break
    end
  end
  
  if number then
    return number
  else
    log("未找到数字", "warning")
    return nil
  end
end

return main()
```

### 8.6 高级技巧：坐标计算
```lua
-- advanced_click.lua
function main()
  -- 找到参考点
  local found, pos = wait_for_target("reference-point", 10)
  if not found then
    return false
  end
  
  -- 计算目标位置（相对参考点）
  local target_x = pos.x + 200
  local target_y = pos.y + 50
  
  -- 先移动到目标，观察是否正确
  move_to(target_x, target_y, 0.5)
  wait(1)
  
  -- 确认后点击
  click()
  
  log("在计算位置点击完成")
  return true
end

return main()
```

### 8.7 错误处理：重试机制
```lua
-- retry_task.lua
function main()
  local max_retry = args.max_retry or 3
  local retry_count = 0
  
  while retry_count < max_retry do
    log("尝试执行任务，第 " .. (retry_count + 1) .. " 次")
    
    -- 尝试执行任务
    local success = execute_task()
    
    if success then
      log("任务成功")
      return true
    else
      retry_count = retry_count + 1
      
      if retry_count < max_retry then
        log("任务失败，等待后重试...", "warning")
        wait(2)
      end
    end
  end
  
  log("任务失败，已达最大重试次数", "error")
  return false
end

function execute_task()
  -- 具体任务逻辑
  local found = wait_for_target("task-button", 10)
  if found then
    click_target("task-button")
    wait(2)
    
    -- 检查是否成功
    local success = wait_for_target("success-icon", 5)
    return success
  end
  
  return false
end

return main()
```

---

## 9. 最佳实践

### 9.1 错误处理
```lua
-- 总是检查目标是否找到
local found, pos = wait_for_target("target-id", 10)
if not found then
  log("未找到目标", "error")
  return false
end

-- 使用重试机制
for i = 1, 3 do
  if try_action() then
    break
  end
  wait(1)
end
```

### 9.2 可配置参数
```lua
-- 使用 args 传递参数，提供默认值
local username = args.username or "default_user"
local retry_times = args.retry_times or 3
local wait_timeout = args.wait_timeout or 30
```

### 9.3 日志记录
```lua
-- 关键步骤记录日志
log("开始执行任务A")
log("等待目标出现...")
log("找到目标，位置: " .. pos.x .. ", " .. pos.y)
log("任务完成")
```

### 9.4 随机化模拟人类
```lua
-- 随机等待时间
wait_random(0.3, 0.8)

-- 随机点击偏移
local offset_x = random(-5, 5)
local offset_y = random(-5, 5)
click_target("button", offset_x, offset_y)
```

### 9.5 资源清理
```lua
function main()
  -- 设置状态
  set_some_state()
  
  -- 使用 pcall 捕获错误
  local success, result = pcall(execute_task)
  
  -- 无论成功失败都清理资源
  cleanup_state()
  
  if success then
    return result
  else
    log("执行出错: " .. tostring(result), "error")
    return false
  end
end
```

---

## 10. 调试技巧

### 10.1 打印变量
```lua
log("变量值: " .. tostring(my_var))

-- 打印表
function print_table(t)
  for k, v in pairs(t) do
    log(k .. " = " .. tostring(v))
  end
end
```

### 10.2 单步测试
在关键位置设置断点，逐步执行查看变量

### 10.3 截图验证
```lua
-- 执行前截图
local before = capture_screen()

-- 执行操作
click_target("button")

-- 执行后截图对比
local after = capture_screen()
```

---

## 11. 性能注意事项

1. **避免过高识别频率**：默认1次/秒足够大多数场景
2. **限制搜索区域**：指定region参数减少识别范围
3. **缓存目标位置**：如果目标位置固定，保存坐标直接使用
4. **合理使用wait**：不要使用忙等待（空循环）
5. **及时释放资源**：大图片数据使用后置为nil

```lua
-- 不好的做法
while true do
  if find_target("target") then break end
  -- 忙等待，占用CPU
end

-- 好的做法
wait_for_target("target", 30)
```
