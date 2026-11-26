"""Single-flight monitor strategy. The root supplies an active clock and cancellable wait."""
import math

from ..script.call_stack import arguments
from ..scripts import domain
from ..storage.errors import StorageError


DEFAULT_MONITOR = dict(region=None, display_id=None, binding_id=None, allow_exit=False,
                       frequency_hz=1, total_timeout_ms=0, debug=False, repeat=False,
                       wait_timeout_ms=30000, on_match="stop", action_script_id=None,
                       action_args={}, on_timeout="stop", timeout_script_id=None,
                       timeout_args={}, max_retries=0, retry_delay_ms=1000,
                       trigger="rising_edge", cooldown_ms=1000)


def validate_references(root):
    config = root.config
    if not root.catalog.target_by_id(config.target_id):
        raise StorageError("TARGET_NOT_FOUND", "监控目标不存在", field="target_id")
    root.reference(config.target_id)
    for field, script_id, supplied, injected in (
        ("action_args", config.action_script_id, config.action_args, "match"),
        ("timeout_args", config.timeout_script_id, config.timeout_args, "failure"),
    ):
        if script_id is None:
            continue
        script = domain.ensure_script_found(root.scripts.get(script_id), script_id)
        if not any(p["name"] == injected and p["type"] == "object" for p in script["parameters"]):
            raise StorageError("INVALID_PARAMS", f"脚本须声明对象参数 {injected}", field=field)
        arguments(script, dict(supplied, **{injected: {}}))
        validation = root.controller.scripts.validate_script(root.controller.scripts.source_for(script))
        if not validation["valid"]:
            diagnostic = validation["diagnostics"][0]
            raise StorageError("SCRIPT_SYNTAX_ERROR", diagnostic["message"],
                               details={"script_id": script_id, "line": diagnostic["line"]})
        root.reference(script_id)


def run_monitor(root, sample):
    config = root.config
    timeout = config.wait_timeout_ms / 1000
    next_tick = root.active_time()
    deadline = next_tick + timeout if timeout else math.inf
    previous_found = False
    retries = 0
    match_count, last_match = 0, None
    previous_sample = None
    next_action = 0.0
    last_logged_found = None
    while True:
        root.wait(max(0, min(next_tick, deadline) - root.active_time()))
        started = root.active_time()
        if started < deadline:
            result = sample(config.target_id)
            root.gate.checkpoint()
            now = root.active_time()
            if now < deadline:
                found = result["found"]
                root.iteration += 1
                root.sample_no = result.get("sample_no")
                if found != last_logged_found:
                    root.log("info", "recognition", "命中" if found else "未命中", sample_no=root.sample_no)
                    last_logged_found = found
                elapsed = now - (previous_sample if previous_sample is not None else started)
                root.effective_hz = min(root.frequency, 1 / elapsed) if elapsed > 0 else root.frequency
                root.throttled = root.effective_hz < root.frequency * .95
                previous_sample = now
                root.controller.emit("recognition_stats", dict(execution_id=root.id, effective_hz=root.effective_hz,
                                     throttled=root.throttled, last_found=found))
                trigger = now >= next_action and found and (config.trigger == "every_match" or not previous_found)
                previous_found = found
                if found:
                    retries = 0
                    deadline = now + timeout if timeout else math.inf
                if trigger:
                    root.gate.checkpoint()
                    last_match = result
                    match_count += 1
                    root.controller.emit("target_detected", dict(execution_id=root.id, result=result))
                    match = result["matches"][0]
                    root.log("info", "target_detected", "命中", sample_no=result.get("sample_no"),
                             bbox=match.get("bbox"), match_count=match_count)
                    if config.action_script_id:
                        root.call(config.action_script_id, dict(config.action_args, match=result))
                    root.gate.checkpoint()
                    if not config.repeat:
                        return dict(match_count=match_count, last_match=last_match), "target_detected"
                    if getattr(root, "export_enabled", False):
                        next_action = root.active_time() + config.cooldown_ms / 1000
                    else:
                        root.wait(config.cooldown_ms / 1000)
                    deadline = root.active_time() + timeout if timeout else math.inf
                period = 1 / root.frequency
                steps = max(1, math.ceil((root.active_time() - next_tick) / period - 1e-9))
                next_tick += steps * period
                continue

        root.gate.checkpoint()
        failure = dict(reason="timeout", attempts=retries + 1, target_id=config.target_id)
        root.log("warning", "wait_timeout", "监控等待超时", **failure)
        if config.on_timeout == "retry" and retries < config.max_retries:
            retries += 1
            root.wait(config.retry_delay_ms / 1000)
            next_tick = root.active_time()
            deadline = next_tick + timeout
            continue
        if config.on_timeout == "run_script":
            root.call(config.timeout_script_id, dict(config.timeout_args, failure=failure))
            root.gate.checkpoint()
            return dict(match_count=match_count, last_match=last_match), "timeout_handled"
        raise StorageError("WAIT_TIMEOUT", "监控等待到期", details=failure)
