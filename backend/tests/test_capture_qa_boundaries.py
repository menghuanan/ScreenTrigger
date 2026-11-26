# -*- coding: utf-8 -*-
"""TASK-007 独立 QA 边界/错误路径验证（QA 工程师 严过关；非工程师自证）。

目的：**不 rubber-stamp**。本文件独立复核 TASK-007「捕获适配器与帧生命周期」：

1. 契约：6 命令 params/data 字段与 02/03 逐字一致；未声明参数 → INVALID_PARAMS
   且 details.field 定位；错误码全部取自 06 注册表；无同义字段。
2. 像素/坐标：半开矩形边界、局部 DIP→物理 floor/ceil、客户区原点换算、
   显示器空隙（不能只用虚拟屏包围盒）。
3. 生命周期：跨会话/过期引用拒绝、release 幂等与所有权、释放后 get/preview、
   max_edge 范围 1–800、预览 ≤512KiB（含极端大图，判据实测）。
4. 捕获不调用 Input：运行时断言（命令产物无 input_action/host_request）
   + 静态 AST 扫描。
5. 一次性 200ms 重试、非瞬时/永久失败不无限重试、等待期取消。
6. 异常分支：DISPLAY_NOT_FOUND / WINDOW_NOT_FOUND / INVALID_REGION / 负坐标 /
   空隙 / 空间不匹配。
7. Frame 语义：未绑定 gr=0、binding_id=null、origin、captured_at RFC3339 UTC。

所有替身来自 backend/tests/_capture_fakes.py；真实 OS 由 reports/TASK-007 证据承担。
"""

from __future__ import annotations

import ast
import base64
import json
import os
import random
import struct
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.capture.cancel import CancelledError  # noqa: E402
from backend.capture.commands import (  # noqa: E402
    PREVIEW_MAX_EDGE_DEFAULT,
    PREVIEW_MAX_EDGE_MAX,
    PREVIEW_MAX_EDGE_MIN,
    register_capture_commands,
)
from backend.capture.errors import CaptureError, capture_failed  # noqa: E402
from backend.capture.ports import RawImage  # noqa: E402
from backend.capture.service import CaptureService  # noqa: E402
from backend.ipc.dispatcher import Dispatcher  # noqa: E402
from backend.image.png import PREVIEW_MAX_BYTES, encode_png_bgr  # noqa: E402
from backend.image.store import SessionImageStore  # noqa: E402
from backend.tests._capture_fakes import (  # noqa: E402
    CaptureTransport,
    FakeCaptureBackend,
    FakeWindowProvider,
    RecordingSleeper,
    SessionStub,
    SimpleCancelToken,
    display,
    make_service,
    solid_image,
    window_snapshot,
)
from backend.capture import geometry  # noqa: E402
from backend.types.region import Rect, Region, validate_rfc3339_utc  # noqa: E402

CAPTURE_COMMANDS = (
    "get_display_list",
    "capture_region",
    "capture_full_screen",
    "capture_window",
    "release_image",
    "get_image_preview",
)

# 06 注册表中本任务允许出现的错误码（其它码不应从捕获路径冒出）
REGISTRY_CODES = frozenset(
    {
        "INVALID_PARAMS",
        "INVALID_REGION",
        "INVALID_IMAGE",
        "IMAGE_EXPIRED",
        "DISPLAY_NOT_FOUND",
        "CAPTURE_FAILED",
        "WINDOW_NOT_FOUND",
        "WINDOW_UNAVAILABLE",
        "RESOURCE_LIMIT",
    }
)


def _screen_region(display_id="d1", x=0, y=0, width=100, height=50) -> Region:
    return Region(space="screen_physical", x=x, y=y, width=width, height=height,
                  display_id=display_id)


def _client_region(binding_id="b1", x=0, y=0, width=100, height=50) -> Region:
    return Region(space="window_client_physical", x=x, y=y, width=width, height=height,
                  binding_id=binding_id)


def _ihdr(png: bytes):
    length = struct.unpack(">I", png[8:12])[0]
    assert png[12:16] == b"IHDR" and length == 13
    return struct.unpack(">IIBB", png[16:26])


def _noisy_image(width, height, seed=1234) -> RawImage:
    data = random.Random(seed).randbytes(width * height * 3)
    return RawImage(width=width, height=height, data=data)


class CommandRunnerMixin:
    """装载 6 命令的 dispatcher 直连夹具（内存传输）。"""

    def _make_runner(self, displays=None, backend=None, window_provider=None):
        displays = displays or [display("d1", 0, 0, 1920, 1080, primary=True)]
        self.backend = backend if backend is not None else FakeCaptureBackend()
        self.provider = window_provider or FakeWindowProvider(
            {"b1": window_snapshot("b1", 300, 400, 500, 300, 7)}
        )
        self.service = make_service(
            displays=displays, backend=self.backend, window_provider=self.provider
        )
        self.transport = CaptureTransport()
        self.dispatcher = Dispatcher(transport=self.transport, session=SessionStub())
        register_capture_commands(self.dispatcher, self.service)
        return self.service

    def _request(self, command, params, request_id="r"):
        envelope = {
            "protocol_version": 1,
            "session_id": SessionStub.session_id,
            "type": "request",
            "id": request_id,
            "command": command,
            "params": params,
        }
        self.dispatcher.dispatch_command(envelope)
        return self.transport.sent[-1]

    def _region_dict(self, space="screen_physical", x=0, y=0, width=100, height=50,
                     display_id="d1", binding_id=None):
        value = {"space": space, "x": x, "y": y, "width": width, "height": height}
        if space == "screen_physical":
            value["display_id"] = display_id
        elif space == "window_client_physical":
            value["binding_id"] = binding_id
        return value


class ContractConsistencyTest(CommandRunnerMixin, unittest.TestCase):
    """逐字契约：命令集/params/data/未声明参数定位/错误码来源。"""

    def test_six_commands_registered(self):
        self._make_runner()
        for command in CAPTURE_COMMANDS:
            self.assertIn(command, self.dispatcher._handlers)

    def test_data_shapes_match_doc03(self):
        self._make_runner()
        # get_display_list → {displays: DisplayInfo[]}
        listed = self._request("get_display_list", {}, "r1")
        self.assertEqual(set(listed["data"].keys()), {"displays"})
        # capture_region/full_screen/window → {image:ImageRef, frame_id:string}
        for command, params in (
            ("capture_region", {"region": self._region_dict()}),
            ("capture_full_screen", {"display_id": "d1"}),
            ("capture_window", {"binding_id": "b1"}),
        ):
            resp = self._request(command, params, "r-" + command)
            self.assertTrue(resp["success"], command)
            self.assertEqual(set(resp["data"].keys()), {"image", "frame_id"}, command)
            self.assertEqual(
                set(resp["data"]["image"].keys()),
                {"image_id", "width", "height", "format", "sha256"},
            )
            self.assertIsInstance(resp["data"]["frame_id"], str)
        # release_image → {ok:true}
        captured = self._request("capture_region", {"region": self._region_dict()}, "rc")
        image_id = captured["data"]["image"]["image_id"]
        released = self._request("release_image", {"image_id": image_id}, "rr")
        self.assertEqual(released["data"], {"ok": True})
        # get_image_preview → {mime, base64}
        captured2 = self._request("capture_region", {"region": self._region_dict()}, "rc2")
        preview = self._request(
            "get_image_preview", {"image_id": captured2["data"]["image"]["image_id"]}, "rp"
        )
        self.assertEqual(set(preview["data"].keys()), {"mime", "base64"})

    def test_undeclared_param_rejected_on_every_command(self):
        self._make_runner()
        # 每条命令至少带一个未声明键，必须 INVALID_PARAMS 且 details.field 定位该键
        samples = {
            "get_display_list": {},
            "capture_region": {"region": self._region_dict()},
            "capture_full_screen": {"display_id": "d1"},
            "capture_window": {"binding_id": "b1"},
            "release_image": {"image_id": "img-x"},
            "get_image_preview": {"image_id": "img-x"},
        }
        for command, base in samples.items():
            params = dict(base)
            params["unexpected_key"] = 1
            resp = self._request(command, params, "junk-" + command)
            self.assertFalse(resp["success"], command)
            self.assertEqual(resp["error"]["code"], "INVALID_PARAMS", command)
            self.assertEqual(resp["error"]["details"]["field"], "unexpected_key", command)

    def test_no_synonym_fields_accepted(self):
        # 02/执行约定：不得发明 position/pos/x、bbox/center 等同义字段
        self._make_runner()
        for synonym in ("position", "pos", "x", "bbox", "center"):
            resp = self._request("capture_full_screen", {"display_id": "d1", synonym: 1}, "s-" + synonym)
            self.assertFalse(resp["success"])
            self.assertEqual(resp["error"]["code"], "INVALID_PARAMS")
            self.assertEqual(resp["error"]["details"]["field"], synonym)

    def test_error_codes_within_registry(self):
        self._make_runner()
        scenarios = (
            ("get_display_list", {"junk": 1}),
            ("capture_full_screen", {"display_id": "gone"}),
            ("capture_window", {"binding_id": "missing"}),
            ("capture_region", {"region": self._region_dict(width=9)}),
            ("capture_region", {"region": self._region_dict(x=-100)}),
            ("release_image", {"image_id": "img-unknown"}),
            ("get_image_preview", {"image_id": "img-unknown"}),
        )
        for command, params in scenarios:
            resp = self._request(command, params, "e-" + command + str(params))
            self.assertFalse(resp["success"])
            code = resp["error"]["code"]
            self.assertIn(code, REGISTRY_CODES, "%s → %s" % (command, code))
            # 06 四字段结构齐备
            self.assertEqual(
                set(resp["error"].keys()), {"code", "message", "details", "retryable"}
            )

    def test_missing_required_params(self):
        self._make_runner()
        for command, field in (
            ("capture_region", "region"),
            ("capture_full_screen", "display_id"),
            ("capture_window", "binding_id"),
            ("release_image", "image_id"),
            ("get_image_preview", "image_id"),
        ):
            resp = self._request(command, {}, "m-" + command)
            self.assertFalse(resp["success"], command)
            self.assertEqual(resp["error"]["code"], "INVALID_PARAMS", command)
            self.assertEqual(resp["error"]["details"]["field"], field, command)

    def test_empty_string_required_params_rejected(self):
        self._make_runner()
        for command, field in (
            ("capture_full_screen", "display_id"),
            ("capture_window", "binding_id"),
            ("release_image", "image_id"),
        ):
            resp = self._request(command, {field: ""}, "empty-" + command)
            self.assertFalse(resp["success"])
            self.assertEqual(resp["error"]["code"], "INVALID_PARAMS")
            self.assertEqual(resp["error"]["details"]["field"], field)


class PreviewMaxEdgeBoundaryTest(CommandRunnerMixin, unittest.TestCase):
    """get_image_preview.max_edge 范围 1–800，缺省 400（03 逐字）。"""

    def _image_id(self):
        captured = self._request(
            "capture_region",
            {"region": self._region_dict(width=40, height=40)},
            "cap",
        )
        return captured["data"]["image"]["image_id"]

    def test_default_and_bounds_accepted(self):
        self._make_runner()
        image_id = self._image_id()
        # 缺省（省略）
        ok_default = self._request("get_image_preview", {"image_id": image_id}, "d0")
        self.assertTrue(ok_default["success"])
        # 下界与上界
        for edge in (PREVIEW_MAX_EDGE_MIN, PREVIEW_MAX_EDGE_MAX, PREVIEW_MAX_EDGE_DEFAULT):
            resp = self._request("get_image_preview", {"image_id": image_id, "max_edge": edge},
                                 "e%d" % edge)
            self.assertTrue(resp["success"], edge)

    def test_out_of_range_rejected_with_field(self):
        self._make_runner()
        image_id = self._image_id()
        for bad in (PREVIEW_MAX_EDGE_MIN - 1, PREVIEW_MAX_EDGE_MAX + 1, -1, 10 ** 6):
            resp = self._request("get_image_preview", {"image_id": image_id, "max_edge": bad},
                                 "bad%d" % bad)
            self.assertFalse(resp["success"], bad)
            self.assertEqual(resp["error"]["code"], "INVALID_PARAMS")
            self.assertEqual(resp["error"]["details"]["field"], "max_edge")

    def test_non_integer_rejected(self):
        self._make_runner()
        image_id = self._image_id()
        # bool 是 int 子类，必须显式拒绝；float / str 亦拒绝
        for bad in (True, False, 400.0, "400"):
            resp = self._request("get_image_preview", {"image_id": image_id, "max_edge": bad},
                                 "t%s" % repr(bad))
            self.assertFalse(resp["success"], repr(bad))
            self.assertEqual(resp["error"]["code"], "INVALID_PARAMS")
            self.assertEqual(resp["error"]["details"]["field"], "max_edge")


class PixelAndCoordinateTest(unittest.TestCase):
    """半开矩形 / floor-ceil / 客户区原点 / 空隙（不能只用虚拟屏包围盒）。"""

    def test_half_open_rect_exact_and_over_by_one(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 400)])
        # 恰好等于显示器尺寸：合法
        service.capture(_screen_region(display_id="d1", x=0, y=0, width=500, height=400))
        # 右多 1 像素：拒绝
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", x=0, y=0, width=501, height=400))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")
        # 下多 1 像素：拒绝
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", x=0, y=0, width=500, height=401))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_region_on_display_boundary_is_half_open(self):
        # display [-100,-50] 300x200 → 区域 x 从 -100..199；x=200 越界
        service = make_service(displays=[display("d1", -100, -50, 300, 200)])
        service.capture(_screen_region(display_id="d1", x=100, y=100, width=100, height=50))
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", x=101, y=100, width=100, height=50))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_negative_coordinate_region_uses_named_display(self):
        backend = FakeCaptureBackend()
        service = make_service(
            displays=[display("left", -1920, 0, 1920, 1080),
                      display("main", 0, 0, 1920, 1080, primary=True)],
            backend=backend,
        )
        frame = service.capture(_screen_region(display_id="left", x=-1000, y=300,
                                               width=120, height=40))
        self.assertEqual(backend.calls, [(-1000, 300, 120, 40)])
        self.assertEqual((frame.origin.x, frame.origin.y), (-1000, 300))

    def test_gap_between_displays_not_covered_by_virtual_bounds(self):
        # 虚拟屏包围盒 [0,0]2000x1000 会覆盖空隙 1000..1100；必须按真实显示器拒绝
        service = make_service(
            displays=[display("d1", 0, 0, 1000, 1000), display("d2", 1100, 0, 1000, 1000)]
        )
        bounds = geometry.merge_displays_bounds(service.list_displays())
        self.assertEqual((bounds.x, bounds.width), (0, 2100))  # 包围盒确实覆盖空隙
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", x=1050, y=100,
                                           width=40, height=40))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_region_spanning_two_adjacent_displays_rejected(self):
        service = make_service(
            displays=[display("d1", 0, 0, 1000, 1000), display("d2", 1000, 0, 1000, 1000)]
        )
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", x=900, y=0, width=200, height=50))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_space_mismatch_image_pixel_invalid_region(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 500)])
        region = Region(space="image_pixel", x=0, y=0, width=100, height=100, image_id="img-1")
        with self.assertRaises(CaptureError) as ctx:
            service.capture(region)
        self.assertEqual(ctx.exception.code, "INVALID_REGION")
        self.assertEqual(ctx.exception.details["field"], "region.space")

    def test_local_dip_floor_ceil_multiple_cases(self):
        # 正向拖动 (0,0)->(10,10) scale=1.0 origin=(100,200)
        r = geometry.local_dip_rect_to_physical((0, 0), (10, 10), (100, 200), 1.0)
        self.assertEqual((r.x, r.y, r.width, r.height), (100, 200, 10, 10))
        # 反向拖动等价
        r2 = geometry.local_dip_rect_to_physical((10, 10), (0, 0), (100, 200), 1.0)
        self.assertEqual((r2.x, r2.y, r2.width, r2.height), (100, 200, 10, 10))
        # scale=1.5：左/上 floor，右/下 ceil，宽高取差（避免少一列）
        r3 = geometry.local_dip_rect_to_physical((1.3, 1.3), (0.2, 0.2), (0, 0), 1.5)
        # left=floor(0.2*1.5)=0 top=0 right=ceil(1.3*1.5)=2 bottom=2 → 2x2
        self.assertEqual((r3.x, r3.y, r3.width, r3.height), (0, 0, 2, 2))
        # 非零 origin 平移
        r4 = geometry.local_dip_rect_to_physical((1, 1), (2, 2), (-50, -60), 2.0)
        self.assertEqual((r4.x, r4.y, r4.width, r4.height), (-48, -58, 2, 2))
        with self.assertRaises(ValueError):
            geometry.local_dip_rect_to_physical((0, 0), (1, 1), (0, 0), 0.0)

    def test_window_client_origin_translation(self):
        backend = FakeCaptureBackend()
        provider = FakeWindowProvider(
            {"b1": window_snapshot("b1", 300, 400, 500, 300, 7)}
        )
        service = make_service(backend=backend, window_provider=provider)
        # 客户区局部 (0,0) → 屏幕客户区原点 (300,400)（ClientToScreen 语义）
        frame = service.capture(_client_region(binding_id="b1", x=0, y=0, width=100, height=50))
        self.assertEqual(backend.calls, [(300, 400, 100, 50)])
        self.assertEqual((frame.origin.x, frame.origin.y), (300, 400))
        # 局部 (10,20) → 屏幕 (310,420)
        service.capture(_client_region(binding_id="b1", x=10, y=20, width=100, height=50))
        self.assertEqual(backend.calls[-1], (310, 420, 100, 50))

    def test_window_region_negative_local_rejected(self):
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 300, 400, 500, 300)})
        service = make_service(window_provider=provider)
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_client_region(binding_id="b1", x=-1, y=0, width=100, height=50))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_window_client_rect_origin_only_used_for_translation(self):
        # client_rect.x/y 是屏幕坐标；局部包含判定不得误用其位置（回归 engineer 自纠点）
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 1000, 1000, 200, 200)})
        service = make_service(window_provider=provider)
        # 局部 (150,150) 100x50 会越过客户区宽度 200 → 拒绝（与 client_rect.x 无关）
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_client_region(binding_id="b1", x=150, y=0, width=100, height=50))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")


class ImageRefLifecycleTest(unittest.TestCase):
    """跨会话/过期引用、release 幂等与所有权、释放后行为、预算。"""

    def test_cross_session_and_expired_rejected(self):
        store_a = SessionImageStore("s-A")
        store_b = SessionImageStore("s-B")
        ref = store_a.put(solid_image(4, 4), "png")
        for store in (store_b,):
            with self.assertRaises(CaptureError) as ctx:
                store.get(ref.image_id)
            self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")
            with self.assertRaises(CaptureError) as ctx:
                store.release(ref.image_id)
            self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")
        self.assertTrue(store_a.has(ref.image_id))  # A 的引用未被 B 的失败破坏

    def test_release_idempotent_and_get_after_release(self):
        store = SessionImageStore("s-1")
        ref = store.put(solid_image(4, 4), "png")
        self.assertTrue(store.release(ref.image_id))
        self.assertTrue(store.release(ref.image_id))  # 幂等
        self.assertFalse(store.has(ref.image_id))
        with self.assertRaises(CaptureError) as ctx:
            store.get(ref.image_id)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")
        with self.assertRaises(CaptureError) as ctx:
            store.preview(ref.image_id, 100)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_release_unknown_is_not_silent_success(self):
        store = SessionImageStore("s-1")
        with self.assertRaises(CaptureError) as ctx:
            store.release("img-never-existed")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_budget_resource_limit_and_release_frees(self):
        store = SessionImageStore("s-1", budget_bytes=48)
        ref = store.put(solid_image(4, 4), "png")  # 4*4*3 = 48
        with self.assertRaises(CaptureError) as ctx:
            store.put(solid_image(4, 4), "png")
        self.assertEqual(ctx.exception.code, "RESOURCE_LIMIT")
        self.assertEqual(store.bytes_used, 48)
        store.release(ref.image_id)
        self.assertEqual(store.bytes_used, 0)
        store.put(solid_image(4, 4), "png")  # 释放后可再登记

    def test_put_rejects_non_bgr_and_bad_format(self):
        store = SessionImageStore("s-1")
        with self.assertRaises(CaptureError) as ctx:
            store.put(solid_image(4, 4), "bmp")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")
        with self.assertRaises(CaptureError) as ctx:
            store.put(RawImage(width=4, height=4, data=b"\x00" * 5), "png")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")

    def test_preview_large_image_still_under_512kib(self):
        # 工程师自留疑点：极端大图预览是否可能超 512KiB → 判据实测
        store = SessionImageStore("s-1")
        img = _noisy_image(900, 900)  # 噪声近似不可压缩
        ref = store.put(img, "png")
        mime, b64 = store.preview(ref.image_id, 800)
        png = base64.b64decode(b64)
        self.assertEqual(mime, "image/png")
        self.assertLessEqual(len(png), PREVIEW_MAX_BYTES)
        width, height, depth, color = _ihdr(png)
        self.assertEqual((depth, color), (8, 2))
        self.assertLessEqual(max(width, height), 800)

    def test_preview_extreme_edge_collapses_below_requested(self):
        # 目标：证明 ≤512KiB 由「逐级降边」保证，最终 RESOURCE_LIMIT 分支不可达
        store = SessionImageStore("s-1")
        img = _noisy_image(1000, 1000)
        ref = store.put(img, "png")
        _mime, b64 = store.preview(ref.image_id, 1)  # 请求最小边 1
        png = base64.b64decode(b64)
        width, height, _d, _c = _ihdr(png)
        self.assertEqual((width, height), (1, 1))  # 折叠到 1×1，必然 ≤512KiB
        self.assertLessEqual(len(png), PREVIEW_MAX_BYTES)

    def test_encode_rejects_bad_buffer(self):
        with self.assertRaises(ValueError):
            encode_png_bgr(b"\x00" * 5, 2, 2)


class RetryAndCancelTest(unittest.TestCase):
    """一次性 200ms 重试、非瞬时/永久不无限重试、等待期取消。"""

    def setUp(self):
        self.displays = [display("d1", 0, 0, 500, 500)]

    def test_transient_failure_retried_exactly_once_200ms(self):
        backend = FakeCaptureBackend([capture_failed("transient"), solid_image(100, 50)])
        sleeper = RecordingSleeper()
        service = make_service(displays=self.displays, backend=backend, sleeper=sleeper)
        service.capture(_screen_region(display_id="d1"))
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(sleeper.delays, [0.2])

    def test_permanent_failure_stops_after_one_retry(self):
        backend = FakeCaptureBackend([capture_failed("a"), capture_failed("b")])
        sleeper = RecordingSleeper()
        service = make_service(displays=self.displays, backend=backend, sleeper=sleeper)
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1"))
        self.assertEqual(ctx.exception.code, "CAPTURE_FAILED")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(sleeper.delays, [0.2])

    def test_non_retryable_capture_failed_not_retried(self):
        backend = FakeCaptureBackend([capture_failed("perm", retryable=False)])
        sleeper = RecordingSleeper()
        service = make_service(displays=self.displays, backend=backend, sleeper=sleeper)
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1"))
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(sleeper.delays, [])

    def test_cancel_before_call_makes_no_backend_call(self):
        backend = FakeCaptureBackend()
        service = make_service(displays=self.displays, backend=backend)
        token = SimpleCancelToken()
        token.cancel()
        with self.assertRaises(CancelledError):
            service.capture(_screen_region(display_id="d1"), cancel=token)
        self.assertEqual(backend.calls, [])

    def test_cancel_during_retry_wait_skips_second_call(self):
        backend = FakeCaptureBackend([capture_failed("boom"), solid_image(100, 50)])
        token = SimpleCancelToken()
        sleeper = RecordingSleeper(on_sleep=lambda _s: token.cancel())
        service = make_service(displays=self.displays, backend=backend, sleeper=sleeper)
        with self.assertRaises(CancelledError):
            service.capture(_screen_region(display_id="d1"), cancel=token)
        self.assertEqual(len(backend.calls), 1)

    def test_os_error_normalized_but_documented_no_auto_retry(self):
        # 特征化：后端抛 OSError → 归一为 CAPTURE_FAILED(retryable=True)，
        # 但**不**触发服务内自动重试（仅原样 CAPTURE_FAILED 才重试）。
        # 记录该不一致；真实 GDI 后端不抛 OSError（直接 capture_failed），
        # 故生产路径不受影响（详见 QA 报告「源码观察」）。
        backend = FakeCaptureBackend([OSError("driver glitch"), solid_image(100, 50)])
        sleeper = RecordingSleeper()
        service = make_service(displays=self.displays, backend=backend, sleeper=sleeper)
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1"))
        self.assertEqual(ctx.exception.code, "CAPTURE_FAILED")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(backend.calls), 1)  # 无自动重试
        self.assertEqual(sleeper.delays, [])


class ExceptionBranchTest(unittest.TestCase):
    """显示器拔除 / 窗口关闭 / 无绑定 / 最小化 / 客户区过小。"""

    def test_display_disappears_between_enumerations(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 500)])
        service._enumerator.displays = []  # 模拟拔除
        with self.assertRaises(CaptureError) as ctx:
            service.capture_full_screen("d1")
        self.assertEqual(ctx.exception.code, "DISPLAY_NOT_FOUND")

    def test_window_not_found_and_null_provider(self):
        service = make_service()
        with self.assertRaises(CaptureError) as ctx:
            service.capture_window("missing")
        self.assertEqual(ctx.exception.code, "WINDOW_NOT_FOUND")
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_client_region(binding_id=""))
        self.assertEqual(ctx.exception.code, "WINDOW_NOT_FOUND")

    def test_null_window_provider_is_honest(self):
        from backend.capture.window_capture import NullWindowProvider

        with self.assertRaises(CaptureError) as ctx:
            NullWindowProvider().snapshot("b1")
        self.assertEqual(ctx.exception.code, "WINDOW_NOT_FOUND")

    def test_window_minimized_or_invisible_unavailable(self):
        for snapshot in (
            window_snapshot("b1", 0, 0, 300, 200, minimized=True, visible=True),
            window_snapshot("b1", 0, 0, 300, 200, minimized=False, visible=False),
        ):
            provider = FakeWindowProvider({"b1": snapshot})
            service = make_service(window_provider=provider)
            with self.assertRaises(CaptureError) as ctx:
                service.capture_window("b1")
            self.assertEqual(ctx.exception.code, "WINDOW_UNAVAILABLE")

    def test_tiny_window_client_rejected(self):
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 0, 0, 9, 50)})
        service = make_service(window_provider=provider)
        with self.assertRaises(CaptureError) as ctx:
            service.capture_window("b1")
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_min_region_edge_10_ok_9_rejected(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 500)])
        self.assertEqual(
            service.capture(_screen_region(display_id="d1", width=10, height=10)).image_ref.width,
            10,
        )
        for w, h, field in ((9, 10, "region.width"), (10, 9, "region.height")):
            with self.assertRaises(CaptureError) as ctx:
                service.capture(_screen_region(display_id="d1", width=w, height=h))
            self.assertEqual(ctx.exception.code, "INVALID_REGION")
            self.assertEqual(ctx.exception.details["field"], field)


class FrameSemanticsTest(unittest.TestCase):
    """未绑定 gr=0/binding=null、origin、captured_at RFC3339 UTC。"""

    def test_unbound_frame_fields(self):
        service = make_service(displays=[display("d1", 1100, 200, 800, 600)])
        frame = service.capture(_screen_region(display_id="d1", x=1180, y=645,
                                               width=200, height=150))
        self.assertEqual(frame.space, "screen_physical")
        self.assertEqual(frame.geometry_revision, 0)
        self.assertIsNone(frame.binding_id)
        self.assertEqual((frame.origin.x, frame.origin.y), (1180, 645))
        validate_rfc3339_utc(frame.captured_at, "frame.captured_at")  # 不抛即合规

    def test_bound_window_frame_fields(self):
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 300, 400, 500, 300, 9)})
        service = make_service(window_provider=provider)
        frame = service.capture_window("b1")
        self.assertEqual(frame.space, "screen_physical")  # 窗口捕获转 screen_physical
        self.assertEqual(frame.geometry_revision, 9)
        self.assertEqual(frame.binding_id, "b1")
        self.assertEqual((frame.origin.x, frame.origin.y), (300, 400))

    def test_full_screen_origin_matches_display(self):
        service = make_service(displays=[display("d1", -1920, 0, 1920, 1080)])
        frame = service.capture_full_screen("d1")
        self.assertEqual((frame.origin.x, frame.origin.y), (-1920, 0))
        self.assertEqual(frame.image_ref.width, 1920)


class NoInputInvocationTest(CommandRunnerMixin, unittest.TestCase):
    """捕获不调用 Input：运行时 + 静态双重独立验证。"""

    def test_runtime_products_contain_no_input(self):
        self._make_runner()
        self._request("get_display_list", {}, "a")
        self._request("capture_region", {"region": self._region_dict()}, "b")
        self._request("capture_full_screen", {"display_id": "d1"}, "c")
        self._request("capture_window", {"binding_id": "b1"}, "d")
        forbidden = ("input_action", "SendInput", "keybd_event", "mouse_event",
                     "host_request", "acquire_input_lease")
        for envelope in self.transport.sent:
            self.assertEqual(envelope["type"], "response")
            text = json.dumps(envelope)
            for token in forbidden:
                self.assertNotIn(token, text)

    def test_service_ports_have_no_input_surface(self):
        service = self._make_runner()
        # 结构上不存在输入端口（只有 backend/enumerator/window_provider/store/clock/sleeper）
        for name in ("input", "send_input", "input_backend", "input_proxy", "keyboard", "mouse"):
            self.assertFalse(hasattr(service, name), name)

    def test_static_ast_no_input_symbols(self):
        forbidden = {"SendInput", "keybd_event", "mouse_event", "SetCursorPos",
                     "input_action", "acquire_input_lease", "release_input_lease"}
        targets = []
        for pkg in ("backend/capture", "backend/image"):
            root = os.path.join(_REPO_ROOT, *pkg.split("/"))
            for name in sorted(os.listdir(root)):
                if name.endswith(".py"):
                    targets.append(os.path.join(root, name))
        self.assertTrue(targets)
        for path in targets:
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            used = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    used.add(node.value)  # 也拦截 getattr(x, "SendInput") 的字符串形式
            overlap = forbidden & used
            self.assertEqual(overlap, set(), "输入符号出现在 %s: %s" % (path, overlap))


class CapabilityDisclosureTest(unittest.TestCase):
    """装配捕获服务后 capture 由 unavailable→available；未装配保持基线。"""

    def _app(self, capture_service):
        import io

        from backend.main import CAPABILITIES, SidecarApp, SidecarArgs, StdioTransport

        err = io.StringIO()
        transport = StdioTransport(stdout=io.StringIO(), stdin=io.StringIO(""), stderr=err)
        args = SidecarArgs(session_id="s-test", fault_scenario=None)
        return SidecarApp(args=args, transport=transport, stderr=err,
                          lua_probe=lambda: "Lua 5.4", capture_service=capture_service), CAPABILITIES

    def test_unassembled_keeps_baseline(self):
        app, baseline = self._app(None)
        caps = app.capabilities()
        self.assertEqual([dict(c) for c in caps], [dict(c) for c in baseline])
        self.assertEqual([c for c in caps if c["id"] == "capture"][0]["state"], "unavailable")

    def test_assembled_marks_capture_available_only(self):
        service = make_service(displays=[display("d1", 0, 0, 800, 600, primary=True)])
        app, _ = self._app(service)
        caps = app.capabilities()
        capture = [c for c in caps if c["id"] == "capture"][0]
        self.assertEqual(capture["state"], "available")
        self.assertIsNone(capture["reason"])
        # 只有已装配的捕获服务可用。
        self.assertEqual(len(caps), 8)
        self.assertTrue(all(c["state"] == "unavailable" for c in caps if c["id"] != "capture"))
        # hello 同步披露
        hello_capture = [c for c in app.cmd_hello()["capabilities"] if c["id"] == "capture"][0]
        self.assertEqual(hello_capture["state"], "available")

    def test_capabilities_returns_deep_copy(self):
        service = make_service(displays=[display("d1", 0, 0, 800, 600, primary=True)])
        app, _ = self._app(service)
        first = app.capabilities()
        for cap in first:
            cap["state"] = "tampered"
        second = app.capabilities()
        capture = [c for c in second if c["id"] == "capture"][0]
        self.assertEqual(capture["state"], "available")  # 不污染后续调用


if __name__ == "__main__":
    unittest.main()
