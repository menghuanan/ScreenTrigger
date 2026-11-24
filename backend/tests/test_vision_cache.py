# -*- coding: utf-8 -*-
"""TASK-013 缓存契约单测（``backend.vision.cache``；§9 / 01 M02 / 07 §2）。

覆盖：键组成（source hash / 模型与算法版本 / 条件 revision / region / geometry_revision）、
新帧/新几何**天然 miss**、**负结果正确失效**、LRU 条目上限淘汰。
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.types.region import Rect  # noqa: E402
from backend.vision.cache import RecognitionCache, cache_key  # noqa: E402


def _key(**overrides):
    base = dict(
        command="match_template",
        provider_id="p",
        model_id=None,
        model_version="1.0.0",
        algo_version="1.0.0",
        frame_sha256="a" * 64,
        geometry_revision=0,
        space="image_pixel",
        region=None,
        params={"threshold": 0.85},
        condition_revision=None,
    )
    base.update(overrides)
    return cache_key(**base)


class CacheKeyTest(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(_key(), _key())

    def test_new_frame_misses(self):
        self.assertNotEqual(_key(), _key(frame_sha256="b" * 64))

    def test_new_geometry_misses(self):
        self.assertNotEqual(_key(), _key(geometry_revision=1))

    def test_condition_revision_misses(self):
        self.assertNotEqual(_key(), _key(condition_revision=2))

    def test_params_change_misses(self):
        self.assertNotEqual(_key(), _key(params={"threshold": 0.5}))

    def test_command_change_misses(self):
        self.assertNotEqual(_key(), _key(command="match_contour"))

    def test_region_normalized(self):
        first = _key(region=Rect(x=0, y=0, width=10, height=10))
        second = _key(region={"x": 0, "y": 0, "width": 10, "height": 10})
        self.assertEqual(first, second)


class RecognitionCacheTest(unittest.TestCase):
    def test_put_get_and_len(self):
        cache = RecognitionCache(capacity=2)
        cache.put("k1", {"matches": [], "truncated": False})
        self.assertEqual(cache.get("k1"), {"matches": [], "truncated": False})
        self.assertEqual(len(cache), 1)

    def test_negative_result_cached_and_invalidates(self):
        cache = RecognitionCache(capacity=8)
        negative = {"matches": [], "truncated": False}
        key_a = _key()
        key_b = _key(frame_sha256="c" * 64)  # 新帧 → 不同键
        cache.put(key_a, negative)
        cache.put(key_b, negative)
        # 负结果同样入缓存；新帧不复用旧键
        self.assertIsNotNone(cache.get(key_a))
        self.assertIsNotNone(cache.get(key_b))
        self.assertIsNone(cache.get(_key(frame_sha256="d" * 64)))

    def test_lru_eviction(self):
        cache = RecognitionCache(capacity=2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")  # 触碰 a → b 变为最旧
        cache.put("c", 3)  # 淘汰 b
        self.assertIsNone(cache.get("b"))
        self.assertIsNotNone(cache.get("a"))
        self.assertIsNotNone(cache.get("c"))

    def test_capacity_must_be_positive(self):
        with self.assertRaises(ValueError):
            RecognitionCache(capacity=0)


if __name__ == "__main__":
    unittest.main()
