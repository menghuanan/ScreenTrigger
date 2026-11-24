# -*- coding: utf-8 -*-
"""TASK-015 T02 单测：文本匹配纯函数 + 消费层坐标平移（``backend.vision.textmatch``）。

覆盖（仅标准库；两环境一致执行）：

- ``normalize_text``：NFC 归一回 + 可选 ``casefold``；**不做**空白归一（对齐 02/06）；
- ``levenshtein``/``fuzzy_similarity``：确定性编辑距离与相似度边界；
- ``match_text`` 三模式（exact/contains/fuzzy）、大小写、非法模式 → ``ValueError``；
- **Q7**：返回值为「匹配分」，命名**不含** ``confidence``/``probability``；
- ``match_text_block_text``：``min_confidence`` 预过滤、取最佳分；
- **Q1**：``translate_polygon_to_space`` 对 ``origin=(0,0)`` 恒等，对**非零/负 origin** 真实平移，
  Provider **不做**平移（此处为唯一消费层入口）；不伪造 ``screen_physical``。
"""

from __future__ import annotations

import inspect
import os
import sys
import unicodedata
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.types.region import Point  # noqa: E402
from backend.vision import textmatch  # noqa: E402
from backend.vision.textmatch import (  # noqa: E402
    DEFAULT_FUZZY_THRESHOLD,
    DEFAULT_MIN_CONFIDENCE,
    MATCH_MODES,
    fuzzy_similarity,
    levenshtein,
    match_text,
    match_text_block_text,
    normalize_params,
    normalize_text,
    translate_polygon_to_space,
)


class _Block:
    """鸭子类型 ``TextBlock``（只需 ``text``/``confidence``）。"""

    def __init__(self, text: str, confidence: float = 0.9) -> None:
        self.text = text
        self.confidence = confidence


class NormalizeTest(unittest.TestCase):
    def test_nfc_normalization(self):
        # 组合字符 é（e + U+0301）→ NFC 单码点
        decomposed = "e\u0301"
        self.assertEqual(normalize_text(decomposed, case_sensitive=True), unicodedata.normalize("NFC", decomposed))
        self.assertNotEqual(normalize_text(decomposed, case_sensitive=True), decomposed)

    def test_casefold_only_when_case_insensitive(self):
        self.assertEqual(normalize_text("AbC", case_sensitive=False), "abc")
        self.assertEqual(normalize_text("AbC", case_sensitive=True), "AbC")

    def test_no_whitespace_folding(self):
        # 仅 NFC + 可选 casefold；空白原样保留（OCR 丢空格是真实行为，不硬修）
        self.assertEqual(normalize_text("a b", case_sensitive=True), "a b")

    def test_non_string_coerced(self):
        self.assertEqual(normalize_text(123, case_sensitive=True), "123")


class DistanceTest(unittest.TestCase):
    def test_levenshtein_known(self):
        self.assertEqual(levenshtein("kitten", "sitting"), 3)
        self.assertEqual(levenshtein("", "abc"), 3)
        self.assertEqual(levenshtein("abc", ""), 3)
        self.assertEqual(levenshtein("same", "same"), 0)

    def test_fuzzy_similarity_bounds(self):
        self.assertEqual(fuzzy_similarity("", ""), 1.0)
        self.assertEqual(fuzzy_similarity("", "abc"), 0.0)
        self.assertAlmostEqual(fuzzy_similarity("abcd", "abce"), 0.75)


class MatchTextTest(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(match_text("hello", "hello", mode="exact"), 1.0)
        self.assertIsNone(match_text("hello", "hell", mode="exact"))

    def test_contains(self):
        self.assertEqual(match_text("hello world", "world", mode="contains"), 1.0)
        self.assertIsNone(match_text("hello", "zzz", mode="contains"))

    def test_fuzzy_threshold(self):
        self.assertIsNotNone(match_text("hello", "hallo", mode="fuzzy", fuzzy_threshold=0.7))
        self.assertIsNone(match_text("hello", "xxxxx", mode="fuzzy"))

    def test_case_sensitivity(self):
        self.assertIsNone(match_text("Hello", "hello", mode="exact", case_sensitive=True))
        self.assertEqual(match_text("Hello", "hello", mode="exact", case_sensitive=False), 1.0)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            match_text("a", "a", mode="regex")

    def test_defaults(self):
        self.assertEqual(MATCH_MODES, ("exact", "contains", "fuzzy"))
        self.assertAlmostEqual(DEFAULT_FUZZY_THRESHOLD, 0.8)
        self.assertAlmostEqual(DEFAULT_MIN_CONFIDENCE, 0.7)

    def test_return_names_avoid_confidence(self):
        # Q7：textmatch 的公开函数不得有名为 confidence/probability 的输出/参数
        # （`min_confidence` 是预过滤参数，属允许项，非输出命名）。
        for name in ("match_text", "match_text_block_text", "fuzzy_similarity"):
            parameters = inspect.signature(getattr(textmatch, name)).parameters
            self.assertNotIn("confidence", parameters)
            self.assertNotIn("probability", parameters)
        # 匹配分是纯 float（非概率对象）
        self.assertIsInstance(match_text("a", "a"), float)


class MatchBlockTest(unittest.TestCase):
    def test_best_score_selected(self):
        blocks = [_Block("apple"), _Block("pineapple"), _Block("banana")]
        score, block = match_text_block_text(blocks, "pineapple", mode="contains")
        self.assertEqual(score, 1.0)
        self.assertIs(block, blocks[1])

    def test_min_confidence_prefilter(self):
        blocks = [_Block("target", confidence=0.2)]
        score, block = match_text_block_text(blocks, "target", mode="exact", min_confidence=0.7)
        self.assertIsNone(score)
        self.assertIsNone(block)

    def test_no_match_returns_none_pair(self):
        self.assertEqual(match_text_block_text([], "x"), (None, None))


class TranslatePolygonTest(unittest.TestCase):
    def test_identity_for_zero_origin(self):
        polygon = (Point(1, 2), Point(3, 2), Point(3, 4), Point(1, 4))
        moved = translate_polygon_to_space(polygon, Point(0, 0))
        self.assertEqual(moved, polygon)

    def test_nonzero_origin_shifts(self):
        polygon = (Point(1, 2), Point(3, 4))
        moved = translate_polygon_to_space(polygon, Point(10, 20))
        self.assertEqual(moved, (Point(11, 22), Point(13, 24)))

    def test_negative_origin_shifts(self):
        # 虚拟桌面负坐标：origin 可负
        polygon = (Point(5, 5),)
        moved = translate_polygon_to_space(polygon, Point(-3, -7))
        self.assertEqual(moved, (Point(2, -2),))

    def test_order_preserved(self):
        polygon = (Point(0, 0), Point(2, 0), Point(2, 2), Point(0, 2))
        moved = translate_polygon_to_space(polygon, Point(1, 1))
        self.assertEqual([(p.x, p.y) for p in moved], [(1, 1), (3, 1), (3, 3), (1, 3)])


class NormalizeParamsTest(unittest.TestCase):
    def test_defaults_filled(self):
        self.assertEqual(
            normalize_params({}),
            {"match_mode": "exact", "case_sensitive": True,
             "fuzzy_threshold": DEFAULT_FUZZY_THRESHOLD, "min_confidence": DEFAULT_MIN_CONFIDENCE},
        )

    def test_values_taken(self):
        params = normalize_params({"match_mode": "fuzzy", "case_sensitive": False,
                                   "fuzzy_threshold": 0.5, "min_confidence": 0.1})
        self.assertEqual(params["match_mode"], "fuzzy")
        self.assertFalse(params["case_sensitive"])

    def test_non_dict_safe(self):
        self.assertEqual(normalize_params(None)["match_mode"], "exact")


if __name__ == "__main__":
    unittest.main()
