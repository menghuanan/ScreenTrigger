# -*- coding: utf-8 -*-
"""TASK-013 隔离与装配可执行化单测（硬要求 2/3）。

- **AST 断言**：``backend/vision/**`` 禁止 import ``pyautogui``/``pynput``/``win32*``/
  ``ctypes.windll``/``backend.input*``/``input_proxy``；``backend/vision/providers/**`` 禁止 import
  ``targets.repository``/``ipc``/``emitter``；``service.py`` 禁止 import ``targets.repository``；
- **文本断言**：源码不得出现 ``SendInput``/``mouse_event``/``keybd_event``；
- **运行期断言**：Provider 只拿只读像素端口（无 ``put``/``release``/``persist``）；
- **装配级断言（§5.3）**：真实 ``build_catalog_services`` + ``build_default_vision_service`` +
  ``SidecarApp`` —— vision 与 target/script **共用同一 Repository**、与 capture/import
  **共用同一 image_store**；能力诚实置位（TASK-014 三算法 / TASK-015 ocr 均**环境分支**，
  两分支都真实执行）；``build_catalog_services`` 仍返回三元组。

任一违规 → **FAIL**（非 skip）。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

#: 引擎依赖是否可用（受管 Python：None；探针 venv：可用）——能力披露断言据此**环境分支**。
_HAS_ENGINE = importlib.util.find_spec("cv2") is not None

from backend.image.store import SessionImageStore  # noqa: E402
from backend.main import SidecarApp, SidecarArgs, build_catalog_services  # noqa: E402
from backend.tests._vision_fakes import (  # noqa: E402
    RecordingPixelReader,
    RecordingTransport,
    make_manifest,
)
from backend.vision.ports import SessionPixelReader  # noqa: E402
from backend.vision.providers.base import FakeProvider  # noqa: E402
from backend.vision.providers.ocr import OCREngineProvider  # noqa: E402
from backend.vision.providers.ocr_models import OcrModelCatalog, REPO_CATALOG_PATH, resolve_library_root  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402
from backend.vision.registry import ProviderRegistry, build_default_vision_service  # noqa: E402

_VISION_DIR = os.path.join(_REPO_ROOT, "backend", "vision")
_PROVIDERS_DIR = os.path.join(_VISION_DIR, "providers")


def _has_ocr_stack():
    if importlib.util.find_spec("paddleocr") is None:
        return False
    root, _ = resolve_library_root()
    try:
        OcrModelCatalog.load(REPO_CATALOG_PATH).verify(root)
    except RecognitionError:
        return False
    return True


_HAS_OCR_STACK = _has_ocr_stack()

# 全局禁令（AST）：第三方输入/输入注入相关模块名片段
_GLOBAL_FORBIDDEN = (
    "pyautogui", "pynput", "win32", "ctypes.windll", "backend.input", "input_proxy",
)
# Provider 禁令（AST）：不得触及 Repository 写 / IPC / UI 事件
_PROVIDER_FORBIDDEN = (
    "targets.repository", "ipc", "emitter", "backend.input", "input_proxy",
    "pyautogui", "pynput", "win32",
)
# Provider 顶层重依赖禁令（AST，仅**模块顶层**）：惰性导入只允许在函数体内
_HEAVY_TOPLEVEL = ("cv2", "numpy", "PIL")
# Provider Capture 禁令（AST，任意深度）：不得引入 Vision→Capture 依赖
_CAPTURE_FORBIDDEN = ("capture",)
# 文本禁令：源码不得出现原生输入调用
_FORBIDDEN_TEXT = ("SendInput", "mouse_event", "keybd_event")


def _iter_py(root: str):
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _import_targets(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            targets.append("." * node.level + module)
    return targets


def _hits(targets, patterns):
    found = []
    for raw in targets:
        normalized = raw.lstrip(".")
        for pattern in patterns:
            if pattern in normalized:
                found.append((raw, pattern))
    return found


def _toplevel_import_targets(path: str):
    """仅收集**模块顶层**（``tree.body``）的 import 目标；函数体内惰性 import 不计入。"""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    targets = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            targets.append("." * node.level + module)
    return targets


class AstIsolationTest(unittest.TestCase):
    def test_no_input_imports_in_vision(self):
        for path in _iter_py(_VISION_DIR):
            hits = _hits(_import_targets(path), _GLOBAL_FORBIDDEN)
            self.assertEqual(hits, [], "%s 含被禁 import: %s" % (path, hits))

    def test_providers_forbid_repository_ipc_emitter(self):
        for path in _iter_py(_PROVIDERS_DIR):
            hits = _hits(_import_targets(path), _PROVIDER_FORBIDDEN)
            self.assertEqual(hits, [], "%s 含被禁 import: %s" % (path, hits))

    def test_providers_no_toplevel_heavy_imports(self):
        # 惰性导入硬约束：providers/** 顶层不得 import cv2/numpy/PIL（只能在函数体内惰性导入）。
        for path in _iter_py(_PROVIDERS_DIR):
            hits = _hits(_toplevel_import_targets(path), _HEAVY_TOPLEVEL)
            self.assertEqual(hits, [], "%s 顶层含重依赖 import: %s" % (path, hits))

    def test_providers_forbid_capture_import(self):
        # 结构禁令：providers/** 不得引入 Capture 依赖（含任意深度 import）。
        for path in _iter_py(_PROVIDERS_DIR):
            hits = _hits(_import_targets(path), _CAPTURE_FORBIDDEN)
            self.assertEqual(hits, [], "%s 含被禁 import: %s" % (path, hits))

    def test_service_does_not_import_targets_repository(self):
        service_path = os.path.join(_VISION_DIR, "service.py")
        hits = _hits(_import_targets(service_path), ("targets.repository",))
        self.assertEqual(hits, [], "service.py 不得 import targets.repository")

    def test_no_native_input_calls_in_source(self):
        for path in _iter_py(_VISION_DIR):
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            for token in _FORBIDDEN_TEXT:
                self.assertNotIn(token, source, "%s 含被禁文本: %s" % (path, token))


class RuntimeIsolationTest(unittest.TestCase):
    def test_pixel_port_has_no_write_methods(self):
        port = RecordingPixelReader()
        for forbidden in ("put", "release", "persist"):
            self.assertFalse(hasattr(port, forbidden),
                             "只读像素端口不应有写方法 %s" % forbidden)

    def test_session_pixel_reader_is_read_only(self):
        store = SessionImageStore(session_id="iso")
        reader = SessionPixelReader(store)
        for forbidden in ("put", "release", "persist"):
            self.assertFalse(hasattr(reader, forbidden))

    def test_provider_factory_only_receives_pixel_reader(self):
        captured = {}
        pixel_reader = RecordingPixelReader()

        def factory(port):
            captured["port"] = port
            return FakeProvider(make_manifest("p", condition_types=("template",)), port)

        registry = ProviderRegistry(pixel_reader, factories={"p": factory})
        registry.initialize_all(tempfile.mkdtemp(prefix="st-vision-iso-"))
        self.assertIs(captured["port"], pixel_reader)
        for forbidden in ("put", "release", "persist"):
            self.assertFalse(hasattr(captured["port"], forbidden))


class AssemblySharingTest(unittest.TestCase):
    """§5.3 装配级集成测试：真实组合根路径（单测替身会抹平偏差，只有真实装配能暴露）。"""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="st-vision-asm-")
        self.image_store = SessionImageStore(session_id="s-asm")
        self.args = SidecarArgs(session_id="s-asm", data_dir=self.data_dir)

    def test_vision_shares_repository_and_image_store(self):
        built = build_catalog_services(self.args, self.image_store)
        self.assertEqual(len(built), 3)  # 签名/三元组返回不变
        repository, target_service, _script_service = built
        vision_service = build_default_vision_service(
            self.args, repository=repository, image_store=self.image_store
        )
        # 结构性身份：同一 Repository / 同一 image_store
        self.assertIs(vision_service.targets._repository, repository)
        self.assertIs(vision_service.targets._repository, target_service.repository)
        self.assertIs(vision_service.pixels._store, self.image_store)

    def test_vision_behaviorally_reads_shared_store(self):
        repository, _ts, _ss = build_catalog_services(self.args, self.image_store)
        vision_service = build_default_vision_service(
            self.args, repository=repository, image_store=self.image_store
        )
        raw = types.SimpleNamespace(width=2, height=2, data=bytes([1, 2, 3]) * 4)
        image_ref = self.image_store.put(raw, "png")
        # 只有共用同一 store，才能读到该 image_id（否则 IMAGE_EXPIRED）
        pixels = vision_service.pixels.read(image_ref.image_id)
        self.assertEqual(pixels.sha256, image_ref.sha256)

    def test_ocr_provider_shares_same_image_store_and_models_dir(self):
        """§B.1.2 装配级同一性：真实组合根下 OCR Provider 读**同一** image_store。

        OCR Provider 读到的像素/帧与 capture/import 为**同一** ``SessionImageStore`` 实例（``is``），
        且 ``models_dir`` 与装配期传入值一致。**非**测试替身：工厂来自真实组合根的石板配置。
        """
        repository, _ts, _ss = build_catalog_services(self.args, self.image_store)
        vision_service = build_default_vision_service(
            self.args, repository=repository, image_store=self.image_store
        )
        registry = vision_service.registry
        factory = registry.factories["builtin.ocr"]
        provider = factory(registry._pixel_reader)  # noqa: SLF001
        self.assertIsInstance(provider, OCREngineProvider)
        # 真实 OCR 工厂拿到的只读像素端口 ⇔ 装配期同一 Reader ⇔ 同一 store
        self.assertIs(provider._pixels, vision_service.pixels)  # noqa: SLF001
        self.assertIs(provider._pixels._store, self.image_store)  # noqa: SLF001
        self.assertIs(vision_service.pixels._store, self.image_store)
        # frame 解析器读同一 store（image_id → Frame 真实还原）
        self.assertIs(vision_service._frames._store, self.image_store)  # noqa: SLF001
        expected_models_dir = os.path.join(os.path.abspath(self.data_dir), "models")
        if _HAS_OCR_STACK:
            ready = registry.ready_providers.get("builtin.ocr")
            self.assertIsNotNone(ready, "探针 venv 下 OCR Provider 应就绪")
            self.assertIs(ready._pixels, vision_service.pixels)  # noqa: SLF001
            self.assertIs(ready._pixels._store, self.image_store)  # noqa: SLF001
            self.assertEqual(ready.models_dir, expected_models_dir)
            self.assertEqual(ready.library_root, resolve_library_root()[0])
        else:
            self.assertIsNone(registry.ready_providers.get("builtin.ocr"))

    def test_sidecar_app_wires_vision_and_honest_capabilities(self):
        repository, target_service, script_service = build_catalog_services(
            self.args, self.image_store
        )
        vision_service = build_default_vision_service(
            self.args, repository=repository, image_store=self.image_store
        )
        transport = RecordingTransport()
        app = SidecarApp(
            args=self.args,
            transport=transport,
            stderr=io.StringIO(),
            lua_probe=lambda: "Lua 5.4",
            target_service=target_service,
            script_service=script_service,
            vision_service=vision_service,
        )
        # 6 条识别命令已注册
        for command in ("recognize_text", "match_template", "match_contour", "detect_color",
                        "evaluate_target_conditions", "test_recognition"):
            self.assertIn(command, app.dispatcher._handlers)
        # 共享同一 JobManager（识别 job 与 get_job/cancel_job 同源）
        self.assertIs(vision_service.jobs, app.jobs)
        # 能力披露**依环境而异**，断言**环境分支**（两分支均真实执行，非 skip）：
        #   受管 Python（无 cv2）→ 三算法 unavailable + reason；
        #   探针 venv（有 cv2）→ 装配后三算法 available（reason is null）。
        caps = {cap["id"]: cap for cap in app.capabilities()}
        if _HAS_ENGINE:
            for cap_id in ("template_match", "contour_match", "color_match"):
                self.assertEqual(caps[cap_id]["state"], "available",
                                 "%s 在引擎可用环境应为 available" % cap_id)
                self.assertIsNone(caps[cap_id]["reason"])
        else:
            for cap_id in ("template_match", "contour_match", "color_match"):
                self.assertEqual(caps[cap_id]["state"], "unavailable",
                                 "%s 在缺引擎环境应为 unavailable" % cap_id)
                self.assertIsNotNone(caps[cap_id]["reason"])
        # TASK-015：ocr 据**实际装配**置位，断言**环境分支**（两分支均真实执行，非 skip）：
        #   受管 Python（无 paddleocr）→ unavailable + reason（ENGINE_UNAVAILABLE 规范格式）；
        #   探针 venv（有 paddleocr + 已装模型）→ available + reason is None。
        # 判据（§B.1.1）：若装配后 ocr 已 available 却仍断言 unavailable，判 FAIL。
        if _HAS_OCR_STACK:
            self.assertEqual(caps["ocr"]["state"], "available",
                             "探针 venv 装配后 ocr 应为 available（模型可加载）")
            self.assertIsNone(caps["ocr"]["reason"])
        else:
            self.assertEqual(caps["ocr"]["state"], "unavailable")
            self.assertIsNotNone(caps["ocr"]["reason"])
        # hello.model_manifests 据实际装配的识别 Provider 已安装模型填写（两分支真实执行）。
        manifests = app.cmd_hello()["model_manifests"]
        if _HAS_OCR_STACK:
            self.assertGreaterEqual(len(manifests), 1,
                                    "OCR 就绪时应披露已安装模型（§B.1.2 装配级同一性）")
        else:
            self.assertEqual(manifests, [])


if __name__ == "__main__":
    unittest.main()
