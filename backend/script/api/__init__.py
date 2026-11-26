"""Bindings for the documented Lua 5.4 host API. All physical input goes through the lease."""
import math
import random
import time
from uuid import uuid4

from ...storage.errors import StorageError
from ...types.anchor import AnchoredPoint
from ...types.action_policy import effective_policy, validate_policy
from ..cancel import CheckpointCancelled
from .vision import Resample, ScreenApi, fail


def number(value, field, minimum=None, maximum=None, default=None):
    if value is None:
        value = default
    if (type(value) not in (int, float) or not math.isfinite(value)
            or minimum is not None and value < minimum or maximum is not None and value > maximum):
        fail(field, "数值不在允许范围内")
    return value


def integer(value, field):
    if type(value) is not int:
        fail(field, "需要整数")
    return value


def milliseconds(value, default):
    return math.ceil(number(value, "duration", 0, 5, default) * 1000)


class ScriptApi:
    def __init__(self, root):
        self.root = root
        self.screen = ScreenApi(root)
        settings = root.settings.get("input", {})
        self.move_ms = settings.get("move_duration_ms", 200)
        self.type_ms = settings.get("type_interval_ms", 50)
        self.override = {}

    def dispatch(self, kind, revision=None, **action):
        root = self.root
        proxy = root.controller.input
        if proxy is None or not root.lease:
            raise StorageError("CAPABILITY_DENIED", "当前执行没有输入租约")
        if revision is None:
            revision = 0
            if root.config.binding_id:
                revision = proxy.window_snapshot(root.config.binding_id)["geometry_revision"]
        root.gate.checkpoint()
        action_id = str(uuid4())
        stack = getattr(root, "stack", ())
        defaults = root.scripts[stack[-1][0]].get("action_policy", {}) if stack else {}
        active = effective_policy(defaults, self.override, kind)
        if len(active)>1:
            action["policy"] = active
        try:
            proxy.dispatch_action(root.id, root.lease, action_id, revision, dict(action, kind=kind),
                                  remaining_ms=root.gate.remaining_ms())
        except Exception as exc:
            root.log("error", "input_action", "输入动作失败", action_id=action_id, kind=kind,
                     error_code=getattr(exc, "code", "INPUT_FAILED"), sample_no=root.sample_no)
            if getattr(exc, "code", None) in ("INTERNAL_ERROR", "ENGINE_UNAVAILABLE", "PROTOCOL_ERROR"):
                if root.gate.deadline_exceeded():
                    raise CheckpointCancelled() from exc
                raise StorageError("INPUT_FAILED", "宿主未确认输入的最终状态",
                                   details={"delivery_unknown": True}) from exc
            raise
        root.log("info", "input_action", "输入已派发", action_id=action_id, kind=kind,
                 dispatched=True, sample_no=root.sample_no)
        return ()

    def pointer(self):
        proxy = self.root.controller.input
        if proxy is None:
            raise StorageError("CAPABILITY_DENIED", "未连接宿主指针服务")
        if getattr(self.root.config, "window_mode", "foreground") == "background":
            point = proxy.query_pointer(self.root.id, self.root.lease)
        else:
            point = proxy.query_pointer()
        return point["x"], point["y"]

    def move_to(self, x, y, duration=None):
        return self.dispatch("move", x=integer(x, "x"), y=integer(y, "y"),
                             duration_ms=self.move_ms if duration is None else milliseconds(duration, .2))

    def click_at(self, x, y, button=None, clicks=None):
        return self.dispatch("click", x=integer(x, "x"), y=integer(y, "y"),
                             button="left" if button is None else button, clicks=1 if clicks is None else clicks)

    def click(self, button=None, clicks=None):
        return self.click_at(*self.pointer(), button, clicks)

    def click_anchor(self, anchor, button=None, clicks=None, hold_ms=None):
        AnchoredPoint.parse(anchor)
        return self.dispatch("click_anchor", anchor=anchor, button="left" if button is None else button,
                             clicks=1 if clicks is None else clicks,
                             **({} if hold_ms is None else {"hold_ms": integer(hold_ms, "hold_ms")}))

    def drag_anchor(self, points, duration_ms):
        return self.dispatch("drag_anchor", points=points, duration_ms=integer(duration_ms, "duration_ms"))

    def scroll_anchor(self, anchor, delta_raw):
        AnchoredPoint.parse(anchor)
        return self.dispatch("scroll_anchor", anchor=anchor, delta_raw=integer(delta_raw, "delta_raw"))

    def recorded_keys(self, scope, events):
        return self.dispatch("recorded_keys", scope=scope, events=events)

    def drag(self, from_x, from_y, to_x, to_y, duration=None):
        start = {"x": integer(from_x, "from_x"), "y": integer(from_y, "from_y")}
        end = {"x": integer(to_x, "to_x"), "y": integer(to_y, "to_y")}
        return self.dispatch("drag", **{"from": start, "to": end}, duration_ms=milliseconds(duration, 1))

    def target_action(self, kind, target_id, ox=None, oy=None, option=None):
        ox = integer(0 if ox is None else ox, "offset_x")
        oy = integer(0 if oy is None else oy, "offset_y")
        for attempt in range(2):
            try:
                generation = self.root.pause_generation
                found, matches = self.screen.find_target(target_id)
                if not found:
                    return False
                hit = matches[0]
                point = dict(x=hit["center"]["x"] + ox, y=hit["center"]["y"] + oy)
                if kind == "move":
                    point["duration_ms"] = self.move_ms if option is None else milliseconds(option, .2)
                else:
                    point.update(button="left" if option is None else option, clicks=1)
                self.root.gate.checkpoint()
                if generation != self.root.pause_generation:
                    raise Resample()
                self.dispatch(kind, hit["geometry_revision"], **point)
                return True
            except Exception as exc:
                if attempt or getattr(exc, "code", None) != "STALE_FRAME":
                    raise

    def screen_size(self, display_id=None):
        displays = self.screen.capture.list_displays() if self.screen.capture else ()
        if display_id is None:
            display_id = self.root.config.display_id
            if self.root.config.binding_id:
                rect = self.root.controller.input.window_snapshot(self.root.config.binding_id)["client_rect"]
                def overlap(display):
                    r = display.rect
                    return (max(0, min(rect["x"] + rect["width"], r.x + r.width) - max(rect["x"], r.x))
                            * max(0, min(rect["y"] + rect["height"], r.y + r.height) - max(rect["y"], r.y)))
                best = max(displays, key=overlap, default=None)
                display_id = best.id if best and overlap(best) else None
        for display in displays:
            if display.id == display_id:
                return display.rect.width, display.rect.height
        raise StorageError("DISPLAY_NOT_FOUND", "显示器不存在", field="display_id")

    def wait(self, seconds):
        self.root.wait(number(seconds, "seconds", 0))
        return ()

    def wait_random(self, minimum, maximum):
        lo = number(minimum, "min", 0)
        return self.wait(random.uniform(lo, number(maximum, "max", lo)))

    def random(self, *args):
        if not args:
            return random.random()
        if len(args) != 2:
            fail("random", "需同时提供两个整数边界")
        lo, hi = (integer(v, "random") for v in args)
        if lo > hi:
            fail("min", "min 超出 max")
        return random.randint(lo, hi)

    def beep(self, frequency=None, duration=None):
        hz = integer(800 if frequency is None else frequency, "frequency")
        if not 37 <= hz <= 32767:
            fail("frequency", "频率需在 37 至 32767 Hz 范围内")
        remaining = milliseconds(duration, .2)
        while remaining:
            self.root.gate.checkpoint()
            part = min(50, remaining)
            self.root.controller.beep(hz, part)
            remaining -= part
        return ()

    def log(self, message, level=None):
        level = "info" if level is None else level
        if not isinstance(message, str) or len(message) > 4096 or level not in ("debug", "info", "warning", "error"):
            fail("log", "日志级别或消息无效")
        self.root.log(level, "script_log", message)
        return ()

    def stop(self, reason=None):
        if reason is not None and not isinstance(reason, str):
            fail("reason", "原因必须为字符串")
        self.root.stop(reason or "script_requested")
        raise CheckpointCancelled()

    def frequency(self, hz):
        self.root.frequency = number(hz, "hz", .1, 30)
        return ()

    def guarded(self, function):
        def invoke(*args):
            while True:
                self.root.gate.checkpoint()
                try:
                    return function(*args)
                except Resample:
                    continue
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    if code == "WINDOW_UNAVAILABLE":
                        self.root.suspend_window()
                        continue
                    details = getattr(exc, "details", {}) or {}
                    if code == "INPUT_FAILED" and (details.get("partially_dispatched") or details.get("delivery_unknown")):
                        self.root.fail(exc)
                        raise CheckpointCancelled() from exc
                    if code == "WINDOW_NOT_FOUND" and self.root.config.binding_id:
                        self.root.fail(exc)
                        raise CheckpointCancelled() from exc
                    raise
        return invoke

    def install(self, vm):
        s = self.screen
        functions = {
            "find_target": s.find_target,
            "wait_for_target": lambda id, timeout=None, interval=None: s.poll("one", [id], timeout, interval),
            "wait_for_any_target": lambda ids, timeout=None: s.poll("any", ids, timeout),
            "wait_for_all_targets": lambda ids, timeout=None: s.poll("all", ids, timeout),
            "wait_until_target_disappear": lambda id, timeout=None: s.poll("gone", [id], timeout),
            "move_to": self.move_to, "click": self.click, "click_at": self.click_at,
            "click_anchor": self.click_anchor, "drag": self.drag,
            "drag_anchor": self.drag_anchor, "scroll_anchor": self.scroll_anchor, "recorded_keys": self.recorded_keys,
            "move_to_target": lambda *args: self.target_action("move", *args),
            "click_target": lambda *args: self.target_action("click", *args),
            "scroll": lambda delta: self.dispatch("scroll", delta=integer(delta, "delta")),
            "press_key": lambda key: self.dispatch("key", key=key, state="tap"),
            "key_down": lambda key: self.dispatch("key", key=key, state="down"),
            "key_up": lambda key: self.dispatch("key", key=key, state="up"),
            "hotkey": lambda *keys: self.dispatch("hotkey", keys=list(keys)),
            "type_text": lambda text, interval=None: self.dispatch("text", text=text,
                interval_ms=self.type_ms if interval is None else milliseconds(interval, .05)),
            "capture_screen": s.capture_screen, "release_image": s.release_image, "ocr": s.ocr,
            "find_text": s.find_text, "find_color": s.find_color, "get_pixel_color": s.get_pixel_color,
            "get_screen_size": self.screen_size, "get_mouse_position": self.pointer,
            "wait": self.wait, "wait_random": self.wait_random, "random": self.random, "beep": self.beep,
            "get_time": time.monotonic, "log": self.log, "stop_script": self.stop,
            "should_stop": self.root.gate.is_cancelled, "set_recognition_frequency": self.frequency,
            "call_script": self.root.call,
        }
        input_names = ("move_to","move_to_target","click","click_at","click_anchor","click_target",
                       "drag","scroll","press_key","key_down","key_up","hotkey","type_text",
                       "drag_anchor","scroll_anchor","recorded_keys")
        def with_policy(options, name, *args):
            if name not in input_names:
                fail("api_name","需要已注册的输入动作")
            policy = validate_policy(options)
            previous, self.override = self.override, policy
            try:
                return functions[name](*args)
            finally:
                self.override = previous
        functions["action_with_policy"] = with_policy
        for name, fn in functions.items():
            vm.registry.register(name, self.guarded(fn))
        tostring = vm.interop.eval("tostring")
        vm.registry.register("print", lambda *values: self.log("\t".join(tostring(v) for v in values)), raw=True)
