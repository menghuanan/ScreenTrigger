# -*- coding: utf-8 -*-
"""TASK-007 CaptureService 单测（backend/capture/service.py）。

覆盖（正常/异常/误操作/边界/超时/重试/取消）：

- 正常：显示器全屏、screen 区域、窗口客户区捕获与 Frame 字段（origin/
  geometry_revision/binding_id/space/captured_at）；
- 负坐标（虚拟桌面）、显示器间空隙、越界区域（不自动裁剪）、9×10/10×10 边界；
- 显示器拔除（DISPLAY_NOT_FOUND）、窗口关闭（WINDOW_NOT_FOUND）、最小化
  （WINDOW_UNAVAILABLE）、空间不匹配（image_pixel → INVALID_REGION）；
- 一次性 200ms 重试：瞬时 CAPTURE_FAILED 重试一次成功、永久失败只重试一次、
  非瞬时（retryable=false）不重试；
- 取消：调用前取消、重试等待期间取消（不再发起第二次后端调用）；
- 时间格式与 envelope 同源；几何纯函数半开矩形/空隙判定。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.capture import geometry  # noqa: E402
from backend.capture.cancel import CancelledError, NullCancelToken  # noqa: E402
from backend.capture.errors import CaptureError, capture_failed  # noqa: E402
from backend.capture.ports import format_utc_timestamp  # noqa: E402
from backend.ipc.envelope import format_timestamp  # noqa: E402
from backend.tests._capture_fakes import (  # noqa: E402
    FakeCaptureBackend,
    FakeClock,
    FakeWindowProvider,
    RecordingSleeper,
    SimpleCancelToken,
    display,
    make_service,
    solid_image,
    window_snapshot,
)
from backend.types.region import Region, Rect  # noqa: E402


def _screen_region(display_id="display-1", x=0, y=0, width=100, height=50) -> Region:
    return Region(space="screen_physical", x=x, y=y, width=width, height=height,
                  display_id=display_id)


def _client_region(binding_id="b1", x=0, y=0, width=100, height=50) -> Region:
    return Region(space="window_client_physical", x=x, y=y, width=width, height=height,
                  binding_id=binding_id)


class CaptureLifetimeTest(unittest.TestCase):
    def test_background_window_never_read(self):
        windows = FakeWindowProvider({"b1": window_snapshot("b1", 0, 0, 100, 80, foreground=False)})
        backend = FakeCaptureBackend()
        service = make_service(window_provider=windows, backend=backend)
        for capture in (lambda: service.capture(_client_region()), lambda: service.capture_window("b1")):
            with self.assertRaises(CaptureError) as raised:
                capture()
            self.assertEqual(raised.exception.code, "WINDOW_UNAVAILABLE")
        self.assertEqual(backend.calls, [])

    def test_window_moves_during_retry(self):
        windows = FakeWindowProvider({"b1": window_snapshot("b1", 0, 0, 120, 80)})
        def move(_delay):
            windows.snapshots["b1"] = window_snapshot("b1", 50, 50, 120, 80, geometry_revision=2)
        backend = FakeCaptureBackend([capture_failed("transient")])
        service = make_service(window_provider=windows, backend=backend, sleeper=RecordingSleeper(move))
        with self.assertRaises(CaptureError) as raised:
            service.capture_window("b1")
        self.assertEqual(raised.exception.code, "STALE_FRAME")
        self.assertEqual(len(backend.calls), 1)

    def test_session_ends_during_capture(self):
        service = make_service()
        class SlowFrame(FakeCaptureBackend):
            def capture_bgr(self, x, y, width, height, cancel):
                service.close()
                return super().capture_bgr(x, y, width, height, cancel)
        service = make_service(backend=SlowFrame())
        with self.assertRaises(CaptureError) as raised:
            service.capture(_screen_region())
        self.assertEqual(raised.exception.code, "IMAGE_EXPIRED")


class ScreenCaptureTest(unittest.TestCase):
    def test_capture_region_normal(self):
        backend = FakeCaptureBackend()
        service = make_service(
            displays=[display("display-1", 0, 0, 1920, 1080, primary=True)],
            backend=backend,
            clock=FakeClock(),
        )
        frame = service.capture(_screen_region(x=100, y=120, width=200, height=150))
        self.assertEqual(backend.calls, [(100, 120, 200, 150)])
        self.assertEqual(frame.origin.x, 100)
        self.assertEqual(frame.origin.y, 120)
        self.assertEqual(frame.space, "screen_physical")
        self.assertEqual(frame.geometry_revision, 0)  # 未绑定
        self.assertIsNone(frame.binding_id)
        self.assertEqual(frame.captured_at, "2026-10-09T12:00:05.000Z")
        self.assertEqual(frame.image_ref.width, 200)
        self.assertEqual(frame.image_ref.height, 150)
        self.assertTrue(service.image_store.has(frame.image_ref.image_id))

    def test_capture_full_screen(self):
        backend = FakeCaptureBackend()
        service = make_service(
            displays=[display("display-1", 0, 0, 1920, 1080, primary=True)],
            backend=backend,
        )
        frame = service.capture_full_screen("display-1")
        self.assertEqual(backend.calls, [(0, 0, 1920, 1080)])
        self.assertEqual((frame.origin.x, frame.origin.y), (0, 0))

    def test_negative_coordinates(self):
        backend = FakeCaptureBackend()
        service = make_service(
            displays=[
                display("left", -1920, 0, 1920, 1080),
                display("main", 0, 0, 1920, 1080, primary=True),
            ],
            backend=backend,
        )
        frame = service.capture(
            _screen_region(display_id="left", x=-900, y=200, width=120, height=40)
        )
        self.assertEqual(backend.calls, [(-900, 200, 120, 40)])
        self.assertEqual((frame.origin.x, frame.origin.y), (-900, 200))

    def test_display_gap_not_covered_by_virtual_bounds(self):
        # 两显示器间有 100px 空隙（1000..1100）；不能用虚拟屏包围盒放行
        service = make_service(
            displays=[
                display("d1", 0, 0, 1000, 1000),
                display("d2", 1100, 0, 1000, 1000),
            ]
        )
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", x=1050, y=100,
                                           width=40, height=40))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_region_larger_than_display_rejected(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 400)])
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", x=0, y=0, width=501, height=400))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_region_exactly_display_is_valid(self):
        backend = FakeCaptureBackend()
        service = make_service(displays=[display("d1", -100, -50, 300, 200)], backend=backend)
        service.capture(_screen_region(display_id="d1", x=-100, y=-50, width=300, height=200))
        self.assertEqual(backend.calls, [(-100, -50, 300, 200)])

    def test_minimum_edge_boundary(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 500)])
        # 10×10 合法
        frame = service.capture(_screen_region(display_id="d1", width=10, height=10))
        self.assertEqual(frame.image_ref.width, 10)
        # 9×10 拒绝（宽低于下限）
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", width=9, height=10))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")
        self.assertEqual(ctx.exception.details["field"], "region.width")
        # 10×9 拒绝（高低于下限）
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1", width=10, height=9))
        self.assertEqual(ctx.exception.details["field"], "region.height")

    def test_display_not_found(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 500)])
        with self.assertRaises(CaptureError) as ctx:
            service.capture_full_screen("display-gone")
        self.assertEqual(ctx.exception.code, "DISPLAY_NOT_FOUND")
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="display-gone"))
        self.assertEqual(ctx.exception.code, "DISPLAY_NOT_FOUND")

    def test_display_removed_between_enumeration_uses_current_set(self):
        enum_service = make_service(displays=[display("d1", 0, 0, 500, 500)])
        # 模拟拔除：枚举集合清空后同一服务再捕获 → DISPLAY_NOT_FOUND
        enum_service._enumerator.displays = []  # noqa: SLF001 - 测试替身
        with self.assertRaises(CaptureError) as ctx:
            enum_service.capture_full_screen("d1")
        self.assertEqual(ctx.exception.code, "DISPLAY_NOT_FOUND")

    def test_image_pixel_space_rejected(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 500)])
        region = Region(space="image_pixel", x=0, y=0, width=100, height=100, image_id="img-1")
        with self.assertRaises(CaptureError) as ctx:
            service.capture(region)
        self.assertEqual(ctx.exception.code, "INVALID_REGION")
        self.assertEqual(ctx.exception.details["field"], "region.space")


class WindowCaptureTest(unittest.TestCase):
    def test_capture_window_client_region(self):
        backend = FakeCaptureBackend()
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 300, 400, 500, 300, 7)})
        service = make_service(backend=backend, window_provider=provider)
        frame = service.capture(_client_region(binding_id="b1", x=10, y=20, width=100, height=50))
        self.assertEqual(backend.calls, [(310, 420, 100, 50)])
        self.assertEqual((frame.origin.x, frame.origin.y), (310, 420))
        self.assertEqual(frame.space, "screen_physical")  # 捕获转 screen_physical
        self.assertEqual(frame.geometry_revision, 7)
        self.assertEqual(frame.binding_id, "b1")

    def test_capture_window_full_client(self):
        backend = FakeCaptureBackend()
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 300, 400, 500, 300, 9)})
        service = make_service(backend=backend, window_provider=provider)
        frame = service.capture_window("b1")
        self.assertEqual(backend.calls, [(300, 400, 500, 300)])
        self.assertEqual(frame.geometry_revision, 9)
        self.assertEqual(frame.binding_id, "b1")

    def test_window_not_found(self):
        service = make_service()
        with self.assertRaises(CaptureError) as ctx:
            service.capture_window("missing")
        self.assertEqual(ctx.exception.code, "WINDOW_NOT_FOUND")

    def test_window_minimized_unavailable(self):
        provider = FakeWindowProvider(
            {"b1": window_snapshot("b1", 0, 0, 300, 200, minimized=True, visible=True)}
        )
        service = make_service(window_provider=provider)
        with self.assertRaises(CaptureError) as ctx:
            service.capture_window("b1")
        self.assertEqual(ctx.exception.code, "WINDOW_UNAVAILABLE")

    def test_window_region_out_of_client(self):
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 300, 400, 500, 300)})
        service = make_service(window_provider=provider)
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_client_region(binding_id="b1", x=450, y=0, width=100, height=50))
        self.assertEqual(ctx.exception.code, "INVALID_REGION")

    def test_tiny_client_rejected(self):
        provider = FakeWindowProvider({"b1": window_snapshot("b1", 0, 0, 9, 50)})
        service = make_service(window_provider=provider)
        with self.assertRaises(CaptureError) as ctx:
            service.capture_window("b1")
        self.assertEqual(ctx.exception.code, "INVALID_REGION")


class RetryTest(unittest.TestCase):
    def test_transient_failure_retried_once_then_succeeds(self):
        backend = FakeCaptureBackend([capture_failed("boom"), solid_image(100, 50)])
        sleeper = RecordingSleeper()
        service = make_service(
            displays=[display("d1", 0, 0, 500, 500)], backend=backend, sleeper=sleeper
        )
        frame = service.capture(_screen_region(display_id="d1"))
        self.assertEqual(len(backend.calls), 2)  # 首次 + 一次重试
        self.assertEqual(sleeper.delays, [0.2])  # 恰好一次 200ms 等待
        self.assertIsNotNone(frame.image_ref)

    def test_permanent_failure_retried_only_once(self):
        backend = FakeCaptureBackend(
            [capture_failed("boom"), capture_failed("boom")]
        )
        sleeper = RecordingSleeper()
        service = make_service(
            displays=[display("d1", 0, 0, 500, 500)], backend=backend, sleeper=sleeper
        )
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1"))
        self.assertEqual(ctx.exception.code, "CAPTURE_FAILED")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(backend.calls), 2)  # 不无限重试
        self.assertEqual(sleeper.delays, [0.2])

    def test_non_retryable_capture_failed_not_retried(self):
        backend = FakeCaptureBackend([capture_failed("permanent", retryable=False)])
        sleeper = RecordingSleeper()
        service = make_service(
            displays=[display("d1", 0, 0, 500, 500)], backend=backend, sleeper=sleeper
        )
        with self.assertRaises(CaptureError) as ctx:
            service.capture(_screen_region(display_id="d1"))
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(sleeper.delays, [])


class CancelTest(unittest.TestCase):
    def test_cancel_before_capture_no_backend_call(self):
        backend = FakeCaptureBackend()
        service = make_service(displays=[display("d1", 0, 0, 500, 500)], backend=backend)
        token = SimpleCancelToken()
        token.cancel()
        with self.assertRaises(CancelledError):
            service.capture(_screen_region(display_id="d1"), cancel=token)
        self.assertEqual(backend.calls, [])

    def test_cancel_during_retry_wait_skips_second_call(self):
        backend = FakeCaptureBackend([capture_failed("boom"), solid_image(100, 50)])
        token = SimpleCancelToken()
        sleeper = RecordingSleeper(on_sleep=lambda _s: token.cancel())
        service = make_service(
            displays=[display("d1", 0, 0, 500, 500)], backend=backend, sleeper=sleeper
        )
        with self.assertRaises(CancelledError):
            service.capture(_screen_region(display_id="d1"), cancel=token)
        self.assertEqual(len(backend.calls), 1)  # 等待期间取消 → 不再发起第二次

    def test_null_token_never_cancels(self):
        service = make_service(displays=[display("d1", 0, 0, 500, 500)])
        frame = service.capture(_screen_region(display_id="d1"), cancel=NullCancelToken())
        self.assertIsNotNone(frame.image_ref)


class GeometryTest(unittest.TestCase):
    def test_rect_within_half_open(self):
        outer = Rect(x=0, y=0, width=100, height=100)
        self.assertTrue(geometry.rect_within(Rect(0, 0, 100, 100), outer))
        self.assertTrue(geometry.rect_within(Rect(90, 90, 10, 10), outer))
        self.assertFalse(geometry.rect_within(Rect(91, 0, 10, 10), outer))  # 越右界

    def test_rect_contains_point_half_open(self):
        rect = Rect(x=-10, y=-10, width=10, height=10)
        self.assertTrue(geometry.rect_contains_point(rect, -10, -10))
        self.assertFalse(geometry.rect_contains_point(rect, 0, -10))  # 右界开
        self.assertFalse(geometry.rect_contains_point(rect, -10, 0))  # 下界开

    def test_display_at_point_gap_returns_none(self):
        displays = [display("d1", 0, 0, 100, 100), display("d2", 200, 0, 100, 100)]
        self.assertIsNone(geometry.display_at_point(displays, 150, 50))  # 空隙
        self.assertEqual(geometry.display_at_point(displays, 50, 50).id, "d1")

    def test_display_containing_rect_no_virtual_bounds(self):
        displays = [display("d1", 0, 0, 100, 100), display("d2", 200, 0, 100, 100)]
        self.assertIsNone(geometry.display_containing_rect(displays, 150, 0, 40, 40))

    def test_local_dip_rect_to_physical(self):
        # 反拖 + 边缘 ceil：a=(1,1) b=(0,0) scale=1.5 origin=(0,0)
        rect = geometry.local_dip_rect_to_physical((1, 1), (0, 0), (0, 0), 1.5)
        self.assertEqual((rect.width, rect.height), (2, 2))

    def test_local_dip_rect_requires_positive_scale(self):
        with self.assertRaises(ValueError):
            geometry.local_dip_rect_to_physical((0, 0), (1, 1), (0, 0), 0)


class ClockFormatTest(unittest.TestCase):
    def test_matches_envelope_format(self):
        for moment in (
            datetime(2026, 10, 9, 12, 0, 5, 678900, tzinfo=timezone.utc),
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            datetime(2026, 10, 9, 12, 0, 5),  # naive → 按 UTC
        ):
            self.assertEqual(format_utc_timestamp(moment), format_timestamp(moment))


if __name__ == "__main__":
    unittest.main()
