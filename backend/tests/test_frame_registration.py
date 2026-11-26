# -*- coding: utf-8 -*-
"""TASK-014 帧登记单测（store 扩展 + 写入点 + StoreFrameResolver + 装配共享；**受管 Python 真实执行**）。

覆盖：

- ``SessionImageStore``：``put(..., frame_meta=)`` / ``get_frame`` / ``release`` 联动 /
  ``close`` 清空 / 未传 ``frame_meta`` 等价旧行为 / ``_frames`` 有界（随像素同生共死）；
- 写入语义：``capture_region(screen)`` → ``screen_physical``/真实 origin/gr=0/binding=null；
  窗口客户区 → 真实 origin/gr/binding；``import_image`` → ``image_pixel``/(0,0)/0/null；
  ``register_host_frame`` → **不登记**（诚实回退）；
- ``StoreFrameResolver``：已登记 → 真实还原；未登记/已释放 → 回退 ``image_pixel``（严禁伪造）；
- 装配：``build_default_vision_service`` 默认 ``StoreFrameResolver`` 读**同一** image_store。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.capture.ports import RawImage  # noqa: E402
from backend.image import FrameMeta, SessionImageStore  # noqa: E402
from backend.image.host_frame import (  # noqa: E402
    HostFrameRegistrationService,
    HostFrameTokenAuthority,
    mint_host_frame_token,
    temp_dir_for,
)
from backend.image.png import encode_png_bgr  # noqa: E402
from backend.main import SidecarArgs, build_catalog_services  # noqa: E402
from backend.tests._capture_fakes import (  # noqa: E402
    FakeWindowProvider,
    display,
    make_service,
    window_snapshot,
)
from backend.tests._import_fakes import FakeFileReader, FakeTokenAuthority  # noqa: E402
from backend.types.region import ImageRef, Point, Region  # noqa: E402
from backend.vision.ports import StoreFrameResolver  # noqa: E402
from backend.vision.registry import build_default_vision_service  # noqa: E402

SECRET = b"unit-test-hostframe-secret-32bytes!"


def _image(width=8, height=6, bgr=(9, 9, 9)) -> RawImage:
    return RawImage(width=width, height=height, data=bytes(bgr) * (width * height))


def _meta(space="screen_physical", origin=(10, 20), gr=0, binding=None, frame_id="frame-1") -> FrameMeta:
    return FrameMeta(
        space=space, origin=Point(origin[0], origin[1]), geometry_revision=gr,
        binding_id=binding, captured_at="2026-10-12T00:00:00.000Z", frame_id=frame_id,
    )


class StoreFrameMetaTest(unittest.TestCase):
    def test_put_get_frame_roundtrip(self):
        store = SessionImageStore("s-1")
        meta = _meta(origin=(3, 4), gr=5, binding="b1", frame_id="frame-x")
        ref = store.put(_image(), "png", frame_meta=meta)
        got = store.get_frame(ref.image_id)
        self.assertEqual(got, meta)
        self.assertEqual(got.origin, Point(3, 4))

    def test_put_without_frame_meta_is_legacy_behaviour(self):
        store = SessionImageStore("s-1")
        ref = store.put(_image(), "png")
        self.assertIsNone(store.get_frame(ref.image_id))  # 未登记 → None（旧行为）
        self.assertTrue(store.has(ref.image_id))

    def test_get_frame_unknown_is_none_not_error(self):
        store = SessionImageStore("s-1")
        self.assertIsNone(store.get_frame("img-nope"))

    def test_release_removes_frame_meta(self):
        store = SessionImageStore("s-1")
        ref = store.put(_image(), "png", frame_meta=_meta())
        self.assertTrue(store.release(ref.image_id))
        self.assertIsNone(store.get_frame(ref.image_id))

    def test_close_clears_frame_meta(self):
        store = SessionImageStore("s-1")
        store.put(_image(), "png", frame_meta=_meta())
        store.close()
        self.assertEqual(len(store._frames), 0)  # noqa: SLF001 - 断言有界清理

    def test_frames_bounded_by_live_entries(self):
        store = SessionImageStore("s-1")
        refs = [store.put(_image(4, 4), "png", frame_meta=_meta(frame_id="f%d" % i))
                for i in range(5)]
        self.assertEqual(len(store._frames), 5)  # noqa: SLF001
        for ref in refs:
            store.release(ref.image_id)
        self.assertEqual(len(store._frames), 0)  # noqa: SLF001

    def test_bad_frame_meta_type_rejected(self):
        store = SessionImageStore("s-1")
        with self.assertRaises(ValueError):
            store.put(_image(), "png", frame_meta={"space": "image_pixel"})  # type: ignore[arg-type]


class CaptureRegistrationTest(unittest.TestCase):
    def test_capture_screen_registers_screen_physical(self):
        store = SessionImageStore("s-capture")
        service = make_service(
            displays=[display("display-1", 0, 0, 1920, 1080, primary=True)], store=store
        )
        region = Region(space="screen_physical", x=100, y=120, width=200, height=150,
                        display_id="display-1")
        frame = service.capture(region)
        meta = store.get_frame(frame.image_ref.image_id)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.space, "screen_physical")
        self.assertEqual(meta.origin, Point(100, 120))
        self.assertEqual(meta.geometry_revision, 0)
        self.assertIsNone(meta.binding_id)
        self.assertEqual(meta.frame_id, frame.frame_id)  # 与 Frame 一致
        self.assertEqual(meta.captured_at, frame.captured_at)

    def test_capture_window_client_registers_real_origin_and_binding(self):
        store = SessionImageStore("s-capture")
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 300, 400, 500, 300, 7)})
        service = make_service(store=store, window_provider=provider)
        region = Region(space="window_client_physical", x=10, y=20, width=100, height=50,
                        binding_id="b1")
        frame = service.capture(region)
        meta = store.get_frame(frame.image_ref.image_id)
        self.assertEqual(meta.space, "screen_physical")
        self.assertEqual(meta.origin, Point(310, 420))  # client 原点 + 局部偏移
        self.assertEqual(meta.geometry_revision, 7)
        self.assertEqual(meta.binding_id, "b1")


class ImportRegistrationTest(unittest.TestCase):
    def _service(self, store, data, name="a.png"):
        path = "C:/pick/" + name
        authority = FakeTokenAuthority({"tok": path})
        reader = FakeFileReader({path: data})
        from backend.image.import_service import ImageImportService

        return ImageImportService(
            session_id="s1", store=store, token_authority=authority, file_reader=reader
        )

    def test_import_registers_image_pixel(self):
        store = SessionImageStore("s1")
        png = encode_png_bgr(bytes(6 * 4 * 3), 6, 4)
        svc = self._service(store, png)
        image_ref, _asset = svc.import_image("tok")
        meta = store.get_frame(image_ref.image_id)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.space, "image_pixel")  # 非 screen_physical
        self.assertEqual(meta.origin, Point(0, 0))
        self.assertEqual(meta.geometry_revision, 0)
        self.assertIsNone(meta.binding_id)
        self.assertTrue(meta.frame_id)


class HostFrameNotRegisteredTest(unittest.TestCase):
    def test_register_host_frame_does_not_register_meta(self):
        # Q1：host-frame token 不携带几何 → 本任务**不登记**（诚实回退 image_pixel）。
        data_dir = tempfile.mkdtemp(prefix="st-hostframe-")
        store = SessionImageStore("s-host")
        png = encode_png_bgr(bytes(5 * 5 * 3), 5, 5)
        sha = hashlib.sha256(png).hexdigest()
        filename = "frame.png"
        os.makedirs(temp_dir_for(data_dir), exist_ok=True)
        with open(os.path.join(temp_dir_for(data_dir), filename), "wb") as handle:
            handle.write(png)
        authority = HostFrameTokenAuthority(SECRET)
        service = HostFrameRegistrationService(store=store, authority=authority, data_dir=data_dir)
        token = mint_host_frame_token(SECRET, "s-host", filename, sha, 5, 5)
        ref = service.register(token, "s-host")
        self.assertIsNone(store.get_frame(ref.image_id))  # 未登记
        # StoreFrameResolver 诚实回退 image_pixel（不报错、不伪造 screen_physical）
        frame = StoreFrameResolver(store).resolve(ref)
        self.assertEqual(frame.space, "image_pixel")
        self.assertEqual(frame.origin, Point(0, 0))
        self.assertEqual(frame.geometry_revision, 0)


class StoreFrameResolverTest(unittest.TestCase):
    def _ref(self, store, image_id="img-x"):
        stored = store.put(_image(2, 2), "png", frame_meta=_meta(space="screen_physical",
                                                                  origin=(7, 8), gr=3,
                                                                  binding="b9", frame_id="frame-9"))
        return stored

    def test_resolve_reads_registered_meta(self):
        store = SessionImageStore("s-res")
        ref = self._ref(store)
        frame = StoreFrameResolver(store).resolve(ref)
        self.assertEqual(frame.space, "screen_physical")
        self.assertEqual(frame.origin, Point(7, 8))
        self.assertEqual(frame.geometry_revision, 3)
        self.assertEqual(frame.binding_id, "b9")
        self.assertEqual(frame.frame_id, "frame-9")

    def test_resolve_unregistered_falls_back_to_identity(self):
        store = SessionImageStore("s-res")
        ref = store.put(_image(2, 2), "png")  # 未登记
        frame = StoreFrameResolver(store).resolve(ref)
        self.assertEqual(frame.space, "image_pixel")
        self.assertEqual(frame.origin, Point(0, 0))

    def test_resolve_after_release_falls_back(self):
        store = SessionImageStore("s-res")
        ref = self._ref(store)
        store.release(ref.image_id)
        frame = StoreFrameResolver(store).resolve(ref)
        self.assertEqual(frame.space, "image_pixel")

    def test_resolver_without_get_frame_falls_back(self):
        class _NoGetFrame:
            pass

        ref = ImageRef(image_id="img-z", width=2, height=2, format="png", sha256="0" * 64)
        frame = StoreFrameResolver(_NoGetFrame()).resolve(ref)
        self.assertEqual(frame.space, "image_pixel")


class AssemblyStoreSharingTest(unittest.TestCase):
    def test_default_vision_service_uses_store_frame_resolver_sharing_store(self):
        data_dir = tempfile.mkdtemp(prefix="st-frame-asm-")
        image_store = SessionImageStore(session_id="s-asm")
        args = SidecarArgs(session_id="s-asm", data_dir=data_dir)
        repository, _ts, _ss = build_catalog_services(args, image_store)
        vision_service = build_default_vision_service(
            args, repository=repository, image_store=image_store
        )
        # 默认解析器为 StoreFrameResolver，且包裹**同一** store
        self.addCleanup(vision_service.close)
        self.assertIsInstance(vision_service._frames, StoreFrameResolver)  # noqa: SLF001
        self.assertIs(vision_service._frames._store, image_store)  # noqa: SLF001
        # 行为：capture 登记 → vision 解析出真实 screen_physical
        service = make_service(
            displays=[display("display-1", 0, 0, 800, 600, primary=True)], store=image_store
        )
        frame = service.capture(
            Region(space="screen_physical", x=5, y=6, width=20, height=20, display_id="display-1")
        )
        resolved = vision_service._frames.resolve(frame.image_ref)  # noqa: SLF001
        self.assertEqual(resolved.space, "screen_physical")
        self.assertEqual(resolved.origin, Point(5, 6))
        self.assertEqual(resolved.frame_id, frame.frame_id)


if __name__ == "__main__":
    unittest.main()
