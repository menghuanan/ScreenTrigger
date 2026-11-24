# -*- coding: utf-8 -*-
"""文本匹配纯函数（TASK-015 T02；**仅标准库**，供 TASK-016 复用为单一事实源）。

语义（对齐 02 OCR 叶 params 与 06）：

- 归一化：两侧均 ``unicodedata.normalize("NFC", s)``（02「NFC 归一化后比较；原始文本保留」）；
- 大小写：``case_sensitive=False`` → 先 NFC 再 ``str.casefold()``；``True`` → 不改；
- 空白：**不做 strip/折叠**（仅 NFC）——OCR 实测会丢空格/断行（TASK-003 探针），
  「空格丢失」是真实 OCR 行为，不当 bug 硬修；空格敏感场景建议用 ``fuzzy``；
- 三模式：``exact``（全串相等）/``contains``（子串，NFC 后码点级）/``fuzzy``
  （``similarity = 1 - levenshtein(H,N)/max(len(H),len(N))`` ≥ ``fuzzy_threshold``）；
- **分数不是概率**：``match_text`` 的返回值为「匹配分」；命名**不得含** ``confidence``/``probability``；
  OCR ``confidence`` 只用于 ``min_confidence`` 预过滤，**不参与**匹配判定（Q7）；
- ``translate_polygon_to_space``：06 不变量 2「从裁剪局部平移回 ``frame.space``」的**消费层**
  纯函数（Q1）；Provider **不做** origin 平移（沿用 TASK-014），**严禁伪造** ``screen_physical``。

模块顶层只 import 标准库（``unicodedata``/``re``/``typing``）；``Point`` 在函数体内惰性导入。
"""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 匹配模式（对齐 02：exact/contains/fuzzy）；非法模式 → ``INVALID_PARAMS`` 由调用方判定。
MATCH_MODES: Tuple[str, ...] = ("exact", "contains", "fuzzy")

#: fuzzy 默认阈值（02：``fuzzy_threshold`` 默认 0.8）。
DEFAULT_FUZZY_THRESHOLD: float = 0.8

#: 匹配前 ``min_confidence`` 默认（02：``min_confidence`` 默认 0.7）。
DEFAULT_MIN_CONFIDENCE: float = 0.7


def normalize_text(s: str, *, case_sensitive: bool, form: str = "NFC") -> str:
    """归一化：先 Unicode 归一（缺省 NFC），``case_sensitive=False`` 时再 ``casefold``。

    **不做空白归一化**（仅 NFC + 可选大小写折叠）；非字符串按 ``str()`` 处理（防御）。
    """
    if not isinstance(s, str):
        s = str(s)
    normalized = unicodedata.normalize(form, s)
    if not case_sensitive:
        normalized = normalized.casefold()
    return normalized


def levenshtein(a: str, b: str) -> int:
    """标准编辑距离（插入/删除/替换各计 1；纯标准库 DP，确定性、可复现）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def fuzzy_similarity(a: str, b: str) -> float:
    """``1 - levenshtein(a,b)/max(len(a),len(b))``（0..1）；**非概率**。

    边界：两侧空 → 1.0（空=空）；一侧空 → 0.0。
    """
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - (levenshtein(a, b) / float(longest))


def match_text(
    haystack: str,
    needle: str,
    *,
    mode: str = "exact",
    case_sensitive: bool = True,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> Optional[float]:
    """返回匹配分（命中 0..1）；未命中 ``None``。**命名不含 confidence/probability**。

    Raises:
        ValueError: ``mode`` ∉ ``MATCH_MODES``（调用方归一为 ``INVALID_PARAMS``）。
    """
    if mode not in MATCH_MODES:
        raise ValueError("未知匹配模式: %r（应为 %s 之一）" % (mode, "/".join(MATCH_MODES)))
    h = normalize_text(haystack, case_sensitive=case_sensitive)
    n = normalize_text(needle, case_sensitive=case_sensitive)
    if mode == "exact":
        return 1.0 if h == n else None
    if mode == "contains":
        return 1.0 if n in h else None
    similarity = fuzzy_similarity(h, n)
    return similarity if similarity >= float(fuzzy_threshold) else None


def match_text_block_text(
    text_blocks: Sequence[Any],
    needle: str,
    *,
    mode: str = "exact",
    case_sensitive: bool = True,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> Tuple[Optional[float], Optional[Any]]:
    """在 ``TextBlock[]`` 上匹配：先按 ``confidence >= min_confidence`` 过滤，再逐块匹配。

    Returns:
        ``(best_match_score, best_block)``；无命中 ``(None, None)``。OCR ``confidence``
        仅用于预过滤，**不写入** match 分（Q7）。``text_blocks`` 元素按鸭子类型读
        ``.text``/``.confidence``（``TextBlock`` 或等价对象）。
    """
    threshold = float(min_confidence)
    best_score: Optional[float] = None
    best_block: Optional[Any] = None
    for block in text_blocks or ():
        confidence = getattr(block, "confidence", None)
        if confidence is not None and float(confidence) < threshold:
            continue
        score = match_text(
            getattr(block, "text", "") or "",
            needle,
            mode=mode,
            case_sensitive=case_sensitive,
            fuzzy_threshold=fuzzy_threshold,
        )
        if score is None:
            continue
        if best_score is None or score > best_score:
            best_score = score
            best_block = block
    return best_score, best_block


def translate_polygon_to_space(points: Sequence[Any], origin: Any) -> Tuple[Any, ...]:
    """把**源图局部像素**多边形平移到 ``frame.space``：``p + origin``（06 不变量 2）。

    - 对 ``image_pixel``（origin=(0,0)）为**恒等**；
    - 对 ``screen_physical``/``window_client_physical`` 且 origin≠(0,0) 才产生位移；
    - **绝不**凭空构造 ``screen_physical``（``origin`` 必须来自已登记帧元数据）；
    - origin 支持负值（虚拟桌面），整数运算（``int()`` 截断到像素）。
    """
    from ..types.region import Point  # 惰性导入：保持模块顶层仅标准库

    dx = int(getattr(origin, "x"))
    dy = int(getattr(origin, "y"))
    translated: List[Any] = []
    for point in points or ():
        translated.append(Point(x=int(getattr(point, "x")) + dx, y=int(getattr(point, "y")) + dy))
    return tuple(translated)


def normalize_params(params: Any) -> Dict[str, Any]:
    """把 OCR 叶 params 中**参与匹配**的字段规范为缓存键参数（TASK-016 复用；确定性）。

    仅取出与匹配相关的键（缺省补 02 默认）；用于把匹配模式/阈值折入缓存键。
    """
    source = params if isinstance(params, dict) else {}
    return {
        "match_mode": source.get("match_mode", "exact"),
        "case_sensitive": bool(source.get("case_sensitive", True)),
        "fuzzy_threshold": float(source.get("fuzzy_threshold", DEFAULT_FUZZY_THRESHOLD)),
        "min_confidence": float(source.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
    }
