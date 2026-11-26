"""Compile complete in-memory recording actions to the public, editable Lua API."""
from ..storage.errors import StorageError
from ..types.anchor import AnchoredPoint
from .domain import validate_source


SCOPES = ("window", "region", "display")
MAX_EVENTS = 8192
MAX_MS = 30 * 60 * 1000
KEYS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789") | {
    "up", "down", "left", "right", "enter", "space", "tab", "backspace", "delete", "esc",
    "ctrl", "shift", "alt", "win", "home", "end", "pageup", "pagedown", "insert", "printscreen",
    "kana", "convert", "nonconvert", *(f"f{i}" for i in range(1, 13)),
}


def require(condition, field):
    if not condition:
        raise StorageError("INVALID_PARAMS", "录制草稿字段无效", field=field)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def point(value, scope):
    a = AnchoredPoint.parse(value)
    require(a.scope == scope, "anchor.scope")
    return (f'{{version=1,scope="{a.scope}",strategy="{a.strategy}",reference_width={a.reference_width},'
            f'reference_height={a.reference_height},x={a.x},y={a.y}}}')


def key_sequence(events, duration):
    require(isinstance(events, list) and 2 <= len(events) <= MAX_EVENTS, "events")
    held = set()
    previous = 0
    result = []
    for index, e in enumerate(events):
        require(isinstance(e, dict) and type(e.get("down")) is bool, "events.down")
        t = e.get("offset_ms")
        require(integer(t, previous, duration) and (index > 0 or t == 0), "events.offset_ms")
        if set(e) == {"offset_ms", "down", "key"}:
            key = e["key"]
            require(type(key) is str and key in KEYS, "events.key")
            fields = f'key="{key}"'
        else:
            require(set(e) == {"offset_ms", "down", "scan_code", "extended"}
                    and integer(e["scan_code"], 1, 127) and type(e["extended"]) is bool, "events.scan_code")
            key = (e["scan_code"], e["extended"])
            fields = f'scan_code={key[0]},extended={str(key[1]).lower()}'
        if e["down"]:
            held.add(key)
        else:
            require(key in held, "events.pair")
            held.remove(key)
        require(held or index == len(events) - 1, "events.pair")
        result.append(f'{{offset_ms={t},down={str(e["down"]).lower()},{fields}}}')
        previous = t
    require(not held and previous == duration, "events.pair")
    return "{" + ",".join(result) + "}"


def action_source(a, scope, duration):
    require(isinstance(a, dict), "action")
    fields = set(a)
    kind = a.get("kind")
    if kind == "click" and fields == {"kind", "point", "button"}:
        require(a["button"] in ("left", "middle", "right"), "action.button")
        return f'click_anchor({point(a["point"], scope)},"{a["button"]}",1,{duration})'
    if kind == "drag" and fields == {"kind", "from", "path", "to"}:
        require(isinstance(a["path"], list) and len(a["path"]) <= MAX_EVENTS, "action.path")
        points = ",".join(point(p, scope) for p in [a["from"], *a["path"], a["to"]])
        return f"drag_anchor({{{points}}},{duration})"
    if kind == "scroll" and fields == {"kind", "point", "delta"}:
        require(integer(a["delta"], -32768, 32767) and duration == 0, "action.delta")
        return f'scroll_anchor({point(a["point"], scope)},{a["delta"]})'
    if kind == "key_sequence" and fields == {"kind", "events"}:
        return f'recorded_keys("{scope}",{key_sequence(a["events"], duration)})'
    raise StorageError("INVALID_PARAMS", "此动作无法等价回放", field="action.kind")


def compile_recording(params):
    scope, actions, interruptions = (params.get(k) for k in ("scope", "actions", "interrupted"))
    require(scope in SCOPES, "scope")
    require(isinstance(actions, list) and len(actions) <= MAX_EVENTS, "actions")
    require(isinstance(interruptions, list) and len(interruptions) <= MAX_EVENTS, "interrupted")
    omitted = []
    for entry in interruptions:
        require(isinstance(entry, dict) and set(entry) == {"after", "reason"}
                and integer(entry["after"], 0, len(actions)) and isinstance(entry["reason"], str)
                and 0 < len(entry["reason"]) <= 128, "interrupted")
        omitted.append(dict(entry))
    lines = []
    previous_end = played_end = emitted = 0
    for seq, row in enumerate(actions):
        require(isinstance(row, dict) and set(row) == {"seq", "start_ms", "end_ms", "action"}
                and integer(row["seq"], seq, seq), "actions.seq")
        start, end = row["start_ms"], row["end_ms"]
        require(integer(start, previous_end, MAX_MS) and integer(end, start, MAX_MS), "actions.time")
        previous_end = end
        try:
            call = action_source(row["action"], scope, end - start)
        except StorageError as error:
            omitted.append({"after": seq, "reason": error.details.get("field", "invalid_action")})
            continue
        gap = start - played_end
        if gap or not emitted:
            lines.append(f"wait({gap // 1000}.{gap % 1000:03d})")
        lines.append(call)
        emitted += 1
        played_end = end
    source = "\n".join(lines) + ("\n" if lines else "")
    validate_source(source)
    return dict(scope=scope, source=source, action_count=emitted, omitted=sorted(omitted, key=lambda v: v["after"]))
