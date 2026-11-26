"""Validate persisted policy v1 and merge independent overrides before physical dispatch."""
from .capabilities import check_keys, fail, require_object


def _pair(value, minimum, maximum, *, ordered, field):
    if not isinstance(value, list) or len(value) != 2:
        fail("INVALID_PARAMS", field, "需要两个整数")
    if any(type(n) is not int or not minimum <= n <= maximum for n in value):
        fail("INVALID_PARAMS", field, "数值超出整数范围")
    if ordered and value[0] > value[1]:
        fail("INVALID_PARAMS", field, "区间的下界不能超过上界")


def validate_policy(value):
    data = require_object(value, "action_policy")
    check_keys(data, ("version", "curve", "offset", "delay_ms"), "action_policy")
    if type(data.get("version")) is not int or data["version"] != 1:
        fail("INVALID_PARAMS", "action_policy.version", "需要版本 1")

    for name, keys in (("curve", ("c1", "c2")), ("offset", ("x", "y"))):
        if name not in data or data[name] is False:
            continue
        field = "action_policy." + name
        axes = require_object(data[name], field)
        check_keys(axes, keys, field)
        for key in keys:
            _pair(axes.get(key), -32768, 32768, ordered=name == "offset", field=field + "." + key)
    if "delay_ms" in data and data["delay_ms"] is not False:
        _pair(data["delay_ms"], 0, 5000, ordered=True, field="action_policy.delay_ms")
    return data


def effective_policy(defaults, override, kind):
    applicable = {"delay_ms"}
    if kind in ("click", "click_anchor", "move", "drag"):
        applicable.add("curve")
    if kind in ("click", "click_anchor"):
        applicable.add("offset")
    for key in ("curve", "offset"):
        if key not in applicable and override.get(key, False) is not False:
            fail("INVALID_PARAMS", "action_policy." + key, "该动作不适用此策略")
    result = {"version": 1}
    for key in sorted(applicable):
        value = override.get(key, defaults.get(key, False))
        if value is not False:
            result[key] = value
    return result
