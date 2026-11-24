# -*- coding: utf-8 -*-
"""TASK-008：GdiCaptureBackend 的 CAPTUREBLT 开关单测（不改 TASK-007 既有语义）。

- 默认 ``use_captureblt=True`` → BitBlt rop = ``SRCCOPY | CAPTUREBLT``（TASK-007 行为不变）；
- ``use_captureblt=False`` → rop = ``SRCCOPY``（分层窗口不被捕获，供框选覆盖层排除）。

不依赖真实 GDI 调用：只断言光栅操作码选择逻辑（可在任意平台运行；gdi 依赖
``ctypes.windll``，非 Windows 导入失败时整文件 skip）。
"""

from __future__ import annotations

import unittest

try:
    from backend.capture import gdi

    _IMPORT_OK = True
except Exception:  # pragma: no cover - 非 Windows
    gdi = None  # type: ignore
    _IMPORT_OK = False


@unittest.skipUnless(_IMPORT_OK, "gdi 后端仅在 Windows 可导入（ctypes.windll）")
class GdiCaptureBltFlagTest(unittest.TestCase):
    def test_default_keeps_captureblt(self) -> None:
        backend = gdi.GdiCaptureBackend()
        self.assertTrue(backend.use_captureblt)
        self.assertEqual(backend._blt_rop(), gdi.SRCCOPY | gdi.CAPTUREBLT)

    def test_disabled_drops_captureblt(self) -> None:
        backend = gdi.GdiCaptureBackend(use_captureblt=False)
        self.assertFalse(backend.use_captureblt)
        self.assertEqual(backend._blt_rop(), gdi.SRCCOPY)
        # 关键断言：不带 CAPTUREBLT（分层窗口捕捉位）——排除覆盖层的判据
        self.assertEqual(backend._blt_rop() & gdi.CAPTUREBLT, 0)


if __name__ == "__main__":
    unittest.main()
