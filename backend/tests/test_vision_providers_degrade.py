# -*- coding: utf-8 -*-
"""TASK-014 三算法 Provider 诚实降级（**两分支都真实执行**；禁止 skipTest）。

覆盖：

- **顶层零重依赖**（AST）：``providers/**`` **模块顶层**的 import **不含** cv2/numpy/PIL；
- **导入期**：``import backend.vision`` 后子进程 ``sys.modules`` 不含 cv2/numpy/PIL；
- **引擎不可用**（受管分支）：``initialize`` 抛 ``ENGINE_UNAVAILABLE``；注册表置能力
  ``unavailable + reason``；service 调用抛 ``ENGINE_UNAVAILABLE``（**非** ``CAPABILITY_DENIED``）；
- **引擎可用**（探针分支）：``initialize`` 成功；能力 ``available``；service 找到 Provider。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.tests._vision_fakes import RecordingPixelReader  # noqa: E402
from backend.vision.cancel import NullCancelToken  # noqa: E402
from backend.vision.providers import ColorProvider, ContourProvider, TemplateProvider  # noqa: E402
from backend.vision.registry import ProviderRegistry  # noqa: E402
from backend.vision.service import VisionService  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402

_PROVIDERS_DIR = os.path.join(_REPO_ROOT, "backend", "vision", "providers")
_HEAVY = ("cv2", "numpy", "PIL")

# 真实探测（两环境真实执行；不 skip）
try:
    import cv2  # noqa: F401
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False


def _iter_py(root: str):
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _top_level_imports(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    names = []
    for node in tree.body:  # 仅模块顶层语句（函数体内惰性 import 不算）
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append("." * node.level + (node.module or ""))
    return names


class TopLevelImportAstTest(unittest.TestCase):
    def test_providers_have_no_top_level_heavy_imports(self):
        for path in _iter_py(_PROVIDERS_DIR):
            for raw in _top_level_imports(path):
                normalized = raw.lstrip(".")
                headline = normalized.split(".")[0]
                self.assertNotIn(
                    headline, _HEAVY,
                    "%s 顶层 import 了重依赖 %s（必须惰性导入）" % (path, raw),
                )


class ImportTimeIsolationTest(unittest.TestCase):
    def test_importing_vision_does_not_load_heavy_modules(self):
        # 子进程隔离（避免其它测试模块在本进程提前 import cv2 造成的顺序依赖）。
        code = (
            "import sys, backend.vision; "
            "loaded=[m for m in ('cv2','numpy','PIL') if m in sys.modules]; "
            "assert not loaded, loaded; print('OK')"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=_REPO_ROOT, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)


class DegradeBehaviourTest(unittest.TestCase):
    def _providers(self):
        reader = RecordingPixelReader()
        return reader, [TemplateProvider(reader), ContourProvider(reader), ColorProvider(reader)]

    def test_initialize_matches_engine_availability(self):
        _reader, providers = self._providers()
        models_dir = tempfile.mkdtemp(prefix="st-vision-degrade-")
        for provider in providers:
            if _HAS_CV2:
                provider.initialize(models_dir)  # 探针分支：真实成功
            else:
                with self.assertRaises(RecognitionError) as ctx:
                    provider.initialize(models_dir)
                self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
                self.assertFalse(ctx.exception.retryable)

    def test_registry_capability_honest(self):
        reader = RecordingPixelReader()
        registry = ProviderRegistry(reader)  # 内置三工厂
        registry.initialize_all(tempfile.mkdtemp(prefix="st-vision-degrade-reg-"))
        expected = "available" if _HAS_CV2 else "unavailable"
        for condition_type in ("template", "contour", "color"):
            state, _reason = registry.capability_reason(condition_type)
            self.assertEqual(state, expected, condition_type)
        if not _HAS_CV2:
            for condition_type in ("template", "contour", "color"):
                self.assertEqual(registry.unavailable_code(condition_type), "ENGINE_UNAVAILABLE")

    def test_service_engine_unavailable_vs_capability_denied(self):
        reader = RecordingPixelReader()
        registry = ProviderRegistry(reader)
        registry.initialize_all(tempfile.mkdtemp(prefix="st-vision-degrade-svc-"))
        service = VisionService(registry, pixels=reader)
        token = NullCancelToken()
        if _HAS_CV2:
            # 探针分支：Provider 就绪 → 走到读像素（缺 image → IMAGE_EXPIRED），非 ENGINE_UNAVAILABLE
            with self.assertRaises(RecognitionError) as ctx:
                service._execute_provider("match_template", {"image_id": "missing"}, token)  # noqa: SLF001
            self.assertNotEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
        else:
            # 受管分支：引擎缺失 → ENGINE_UNAVAILABLE（非 CAPABILITY_DENIED）
            with self.assertRaises(RecognitionError) as ctx:
                service._execute_provider("match_template", {"image_id": "missing"}, token)  # noqa: SLF001
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")

    def test_unknown_condition_is_capability_denied(self):
        # ocr 无 Provider（未装配）→ CAPABILITY_DENIED（与 ENGINE_UNAVAILABLE 区分）
        reader = RecordingPixelReader()
        registry = ProviderRegistry(reader, factories={})
        registry.initialize_all(tempfile.mkdtemp(prefix="st-vision-degrade-none-"))
        service = VisionService(registry, pixels=reader)
        with self.assertRaises(RecognitionError) as ctx:
            service._execute_provider("recognize_text", {"image_id": "x"}, NullCancelToken())  # noqa: SLF001
        self.assertEqual(ctx.exception.code, "CAPABILITY_DENIED")


if __name__ == "__main__":
    unittest.main()
