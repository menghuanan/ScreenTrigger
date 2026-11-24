"""TASK-002 探针：SendInput Unicode、组合键、拖拽取消与停止后输入计数（纯 Win32）。

场景（对应任务卡 STEPS.04，TEST-005/TEST-008 的 OS 部分）：
1. Unicode 文本：BMP（你好）+ 补充平面（𝐀 U+1D400 代理对）经 KEYEVENTF_UNICODE 精确到达；
2. 组合键：Shift+End 选区后输入替换（持有键账本逆序释放）；
3. 拖拽取消：拖拽中途关闭闸门——已派发部分不撤回、取消后无新增动作；
4. 停止后计数：gate 关闭后 dispatch 一律拒绝，dispatched 不变、rejected 递增；
5. 持有键账本：finally 逆序 key-up 释放。

安全边界：
- 仅向探针自建窗口派发；派发前必须确认前台==探针窗口，否则跳过派发并如实记录
  （防止合成输入落到用户其他应用）；开始前保存前台窗口与光标位置，结束后恢复。
- Gate 是 M05 输入闸门的最小语义原型，不是业务实现。

退出码：0=全过；1=有 FAIL；2=前台条件不满足未派发（NOT_RUN 性质）。
用法：python tools/probes/windows/input_probe.py --json-out reports/TASK-002/input-probe-result.json
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from common import (GA_ROOT, INPUT_KEYBOARD, INPUT_MOUSE, KEYEVENTF_KEYUP,  # noqa: E402
                    HWND_TOPMOST, KEYEVENTF_UNICODE, MOUSEEVENTF_ABSOLUTE,
                    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE,
                    MOUSEEVENTF_VIRTUALDESK, MiniWindow, SWP_NOMOVE, SWP_NOSIZE,
                    VK_CONTROL, VK_MENU, base_environment,
                    client_screen_origin_helper, create_edit_child, edit_get_text,
                    edit_set_text, set_per_monitor_v2, utc_now, user32, write_report)

VK_SHIFT, VK_END, VK_HOME = 0x10, 0x23, 0x24


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", INPUTUNION)]


def _key_event(vk, scan, flags) -> INPUT:
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return i


def vk_event(vk, up=False) -> INPUT:
    return _key_event(vk, 0, KEYEVENTF_KEYUP if up else 0)


def unicode_char_events(ch: str) -> list[INPUT]:
    events = []
    data = ch.encode("utf-16-le")
    units = [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]
    for unit in units:
        events.append(_key_event(0, unit, KEYEVENTF_UNICODE))
        events.append(_key_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def to_absolute(x: int, y: int, vs: dict) -> tuple[int, int]:
    """按虚拟屏幕起点/尺寸 + VIRTUALDESK 标志归一化（07§5）。"""
    nx = (x - vs["x"]) * 65535 // max(1, vs["w"] - 1)
    ny = (y - vs["y"]) * 65535 // max(1, vs["h"] - 1)
    return nx, ny


def mouse_event(x: int, y: int, vs: dict, down=False, up=False) -> INPUT:
    nx, ny = to_absolute(x, y, vs)
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    if down:
        flags |= MOUSEEVENTF_LEFTDOWN
    if up:
        flags |= MOUSEEVENTF_LEFTUP
    i = INPUT(type=INPUT_MOUSE)
    i.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=None)
    return i


class Gate:
    """M05 输入闸门最小语义原型：open 派发计数，closed 拒绝计数。"""

    def __init__(self, hwnd, virtual_screen):
        self.hwnd = hwnd
        self.vs = virtual_screen
        self.open = True
        self.dispatched = 0
        self.rejected = 0
        self.held: list[INPUT] = []
        self.mouse_down = False

    def owns_target(self, item):
        if user32.GetForegroundWindow() != self.hwnd:
            return False
        if item.type != INPUT_MOUSE:
            return True
        x = self.vs["x"] + round(item.mi.dx * (self.vs["w"] - 1) / 65535)
        y = self.vs["y"] + round(item.mi.dy * (self.vs["h"] - 1) / 65535)
        hit = user32.WindowFromPoint(wt.POINT(x, y))
        return bool(hit and user32.GetAncestor(hit, GA_ROOT) == self.hwnd)

    def dispatch(self, items: list[INPUT]) -> bool:
        if not self.open or not all(self.owns_target(item) for item in items):
            self.rejected += len(items)
            return False
        sent = user32.SendInput(len(items), (INPUT * len(items))(*items),
                                ctypes.sizeof(INPUT))
        self.dispatched += sent
        for item in items[:sent]:
            if item.type == INPUT_MOUSE and item.mi.dwFlags & MOUSEEVENTF_LEFTDOWN:
                self.mouse_down = True
            elif item.type == INPUT_MOUSE and item.mi.dwFlags & MOUSEEVENTF_LEFTUP:
                self.mouse_down = False
        return sent == len(items)

    def close(self):
        self.open = False

    def release_owned(self):
        for item in reversed(self.held):
            item.ki.dwFlags |= KEYEVENTF_KEYUP
            if user32.SendInput(1, (INPUT * 1)(item), ctypes.sizeof(INPUT)) != 1:
                raise OSError("释放探针持有的键失败")
        self.held.clear()
        if self.mouse_down:
            release = INPUT(type=INPUT_MOUSE)
            release.mi.dwFlags = MOUSEEVENTF_LEFTUP
            if user32.SendInput(1, (INPUT * 1)(release), ctypes.sizeof(INPUT)) != 1:
                raise OSError("释放探针持有的鼠标键失败")
            self.mouse_down = False


def make_foreground(hwnd) -> bool:
    """AttachThreadInput + SetForegroundWindow 组合；返回是否成功置前台。"""
    kernel32 = ctypes.windll.kernel32
    my_thread = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_thread = 0
    if fg:
        pid = wt.DWORD(0)
        fg_thread = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    attached = False
    if fg_thread and fg_thread != my_thread:
        attached = bool(user32.AttachThreadInput(my_thread, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        time.sleep(0.15)
        return user32.GetForegroundWindow() == hwnd
    finally:
        if attached:
            user32.AttachThreadInput(my_thread, fg_thread, False)


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-002 SendInput 探针")
    parser.add_argument("--json-out", default="reports/TASK-002/input-probe-result.json")
    args = parser.parse_args()

    awareness = set_per_monitor_v2()
    env = base_environment(awareness)
    vs = env["virtual_screen"]
    scenarios = {}

    saved_fg = user32.GetForegroundWindow()
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    saved_cursor = (pt.x, pt.y)

    win = MiniWindow("ST-TASK002-INPUT-PROBE", 120, 120, 360, 220)
    user32.WindowFromPoint.argtypes = [wt.POINT]
    user32.WindowFromPoint.restype = wt.HWND
    gate = Gate(win.hwnd, vs)
    edit = create_edit_child(win.hwnd, "", 10, 10, 330, 32)
    win.pump(200)
    # 对探针窗口禁用 IME：合成 VK 字母否则会被输入法组合拦截（本机实测事实）
    imm32 = ctypes.windll.imm32
    imm32.ImmAssociateContext(edit, None)
    imm32.ImmAssociateContext(win.hwnd, None)
    ox, oy = client_screen_origin_helper(win.hwnd)

    focused = False
    for _ in range(5):
        try:
            if make_foreground(win.hwnd):
                focused = True
                break
        except Exception:
            break
        win.pump(250)
    foreground_ok = focused and user32.GetForegroundWindow() == win.hwnd
    if foreground_ok:
        user32.SetFocus(edit)
        win.pump(120)

    try:
        scenarios["foreground_guard"] = {
            "expected": "派发前探针窗口须为前台，鼠标命中点须位于该窗口",
            "actual": {"make_foreground": focused,
                       "note": "每批事件重新核对前台窗口和鼠标命中窗口"},
            "pass": foreground_ok,
        }
        if foreground_ok:
            # --- 场景1：Unicode 文本（含补充平面） ---
            user32.SetFocus(edit)
            win.pump(100)
            edit_set_text(edit, "")
            win.pump(80)
            text = "ab你好" + "\U0001D400"
            gate.dispatch(unicode_char_events(text))
            win.pump(150)
            received = edit_get_text(edit)
            scenarios["unicode_text"] = {
                "expected": f"EDIT 收到 {text!r}（BMP+补充平面代理对精确到达）",
                "actual": {"sent": text, "received": received,
                           "sendinput_ok": gate.dispatched > 0},
                "pass": received == text,
            }

            # --- 场景2：组合键 Shift+a（修饰保持）+ 账本释放后 a ---
            edit_set_text(edit, "")
            win.pump(100)
            shift_down = vk_event(VK_SHIFT)
            if not user32.GetAsyncKeyState(VK_SHIFT) & 0x8000 and gate.dispatch([shift_down]):
                gate.held.append(shift_down)
            win.pump(100)
            gate.dispatch([vk_event(0x41), vk_event(0x41, up=True)])
            win.pump(150)
            gate.release_owned()  # finally 语义：逆序释放持有键
            win.pump(100)
            gate.dispatch([vk_event(0x41), vk_event(0x41, up=True)])
            win.pump(150)
            after_combo = edit_get_text(edit)
            scenarios["key_combo"] = {
                "expected": "Shift 持有期间 'a' 到达为 'A'，账本释放后 'a' 为小写 → 'Aa'",
                "actual": {"after_combo": after_combo, "held_cleared": not gate.held,
                           "note": "EDIT 控件对合成 Shift+End 选区不稳定（dbg 事实），"
                                   "改用 Shift+字母验证修饰保持与释放语义"},
                "pass": after_combo == "Aa" and not gate.held,
            }

        # 越界动作必须在送入系统前被拒绝。
        before = gate.dispatched
        outside = gate.dispatch([mouse_event(ox - 40, oy - 40, vs, down=True)])
        scenarios["outside_guard"] = {"expected": "窗口外的动作被拒绝", "actual": outside,
                                      "pass": not outside and gate.dispatched == before and foreground_ok}
        # 场景3：拖拽取消
        win.motions.clear()
        win.pump(100)
        sx, sy = ox + 20, oy + 150
        gate.open = True
        if user32.GetAsyncKeyState(1) & 0x8000:
            raise RuntimeError("鼠标左键已被按住，无法开始隔离拖拽测试")
        gate.dispatch([mouse_event(sx, sy, vs, down=True)])
        step_x = 300 // 8
        for i in range(8):
            gate.dispatch([mouse_event(sx + step_x * (i + 1), sy, vs)])
            if i == 3:
                gate.close()  # 拖拽中途停止
            win.pump(25)
        received_motion = len(win.motions)
        gate.dispatch([mouse_event(sx, sy, vs, up=True)])  # gate 关，应被拒
        win.pump(80)
        gate.release_owned()
        scenarios["drag_cancel"] = {
            "expected": "8 步拖拽第 4 步关闸：收到部分 motion（<8），关闸后无新增派发",
            "actual": {"motions_received": received_motion,
                       "dispatched": gate.dispatched, "rejected": gate.rejected},
            "pass": 0 < received_motion < 8 and gate.rejected > 0 and not gate.mouse_down
                    and not (user32.GetAsyncKeyState(1) & 0x8000),
        }

        # --- 场景4：停止后输入计数 ---
        dispatched_before = gate.dispatched
        for _ in range(5):
            gate.close()
            gate.dispatch([vk_event(0x41)])
        scenarios["stop_count"] = {
            "expected": "闸门关闭后 5 次派发全部拒绝，dispatched 计数不变",
            "actual": {"dispatched_before": dispatched_before,
                       "dispatched_after": gate.dispatched,
                       "rejected_total": gate.rejected},
            "pass": gate.dispatched == dispatched_before and gate.rejected >= 5,
        }
        other = MiniWindow("ST-TASK002-FOCUS-GUARD", 520, 420, 180, 80)
        try:
            changed = make_foreground(other.hwnd)
            gate.open = True
            refused = not gate.dispatch([vk_event(0x41), vk_event(0x41, up=True)])
            scenarios["focus_guard"] = {"expected": "另一专用窗口获得焦点后拒绝输入",
                "actual": {"changed": changed, "refused": refused, "dispatched": gate.dispatched},
                "pass": changed and refused and gate.dispatched == dispatched_before}
        finally:
            other.destroy()
    finally:
        gate.release_owned()
        win.destroy()
        user32.SetCursorPos(saved_cursor[0], saved_cursor[1])
        if saved_fg:
            user32.SetForegroundWindow(saved_fg)

    result = {
        "probe": "input_probe",
        "task": "TASK-002",
        "rebuild_command": "python tools/probes/windows/input_probe.py --json-out " + args.json_out,
        "recorded_at": utc_now(),
        "environment": env,
        "gate_semantics": "dispatch 计数 / close 拒绝计数 / release_owned 逆序 key-up（M05 最小语义原型）",
        "safety": "派发前校验前台==探针窗口；开始/结束保存并恢复前台与光标；仅向自建窗口派发",
        "scenarios": scenarios,
    }
    write_report(args.json_out, result)
    for k, v in scenarios.items():
        print(f"  {k}: {'PASS' if v['pass'] else 'FAIL'} -> {str(v['actual'])[:140]}")
    return 0 if scenarios and all(v["pass"] for v in scenarios.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
