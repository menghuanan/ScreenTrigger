"""Shared JSON boundary for IPC frames and versioned documents."""
import json
import math


def _unique(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON key: " + name)
        value[name] = item
    return value


def _finite(number):
    result = float(number)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def loads(text):
    return json.loads(text, object_pairs_hook=_unique, parse_float=_finite, parse_constant=_finite)
