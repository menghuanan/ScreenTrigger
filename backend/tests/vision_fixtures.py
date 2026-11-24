# -*- coding: utf-8 -*-
"""TASK-014 程序化合成夹具（**纯标准库**，两环境均可导入）。

设计原则（§8.1）：`随机种子固定`、直接构造 **BGR 行主序** 字节、**不引外部素材**；
期望标签来自**生成器真值**（"我把物体放在 (x,y) 尺寸 (w,h)"），**不来自实现**——
避免"用实现验证实现"。

提供：``Canvas``（读写 BGR 像素）、``solid``、``distinctive_patch``（高方差结构模板）、
``circle_mask``/``star_mask``（形状）、``filled_patch``/``textured_patch``（同形状不同纹理）。
"""

from __future__ import annotations

import hashlib
import random
from typing import List, Sequence, Tuple

from backend.capture.ports import RawImage
from backend.types.region import Frame, ImageRef, Point
from backend.vision.ports import RawPixels
from backend.vision.types import RecognitionError

Bgr = Tuple[int, int, int]


def solid(width: int, height: int, bgr: Bgr) -> bytes:
    """纯色 BGR 缓冲。"""
    return bytes(bgr) * (width * height)


def raw_image(width: int, height: int, data: bytes) -> RawImage:
    """构造 RawImage（BGR 行主序）。"""
    return RawImage(width=width, height=height, data=bytes(data))


class MemoryPixelReader:
    """只读像素端口替身（可放任意 BGR 数据；**无** put/release/persist）。"""

    def __init__(self) -> None:
        self._images = {}

    def add(self, image_id: str, raw: RawImage) -> str:
        self._images[image_id] = RawPixels(
            image_id=image_id,
            width=int(raw.width),
            height=int(raw.height),
            sha256=hashlib.sha256(raw.data).hexdigest(),
            format="png",
            bgr=bytes(raw.data),
        )
        return image_id

    def read(self, image_id: str) -> RawPixels:
        raw = self._images.get(image_id)
        if raw is None:
            raise RecognitionError("IMAGE_EXPIRED", "image 不存在: %s" % image_id, field="image_id")
        return raw


def make_frame(raw: RawImage, image_id: str = "src", *, space: str = "image_pixel",
               frame_id: str = "frame-1") -> Frame:
    """构造识别输入帧（默认 ``image_pixel``）。"""
    return Frame(
        frame_id=frame_id,
        image_ref=ImageRef(image_id=image_id, width=raw.width, height=raw.height,
                           format="png", sha256=hashlib.sha256(raw.data).hexdigest()),
        space=space,
        origin=Point(0, 0),
        captured_at="2026-10-12T00:00:00.000Z",
        geometry_revision=0,
        binding_id=None,
    )


class Canvas:
    """可变 BGR 画布（便于按像素/矩形布置夹具）。"""

    def __init__(self, width: int, height: int, fill: Bgr = (0, 0, 0)) -> None:
        self.width = int(width)
        self.height = int(height)
        self.data = bytearray(solid(width, height, fill))

    # -- 像素 ----------------------------------------------------------------
    def set_pixel(self, x: int, y: int, bgr: Bgr) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            index = (y * self.width + x) * 3
            self.data[index] = bgr[0]
            self.data[index + 1] = bgr[1]
            self.data[index + 2] = bgr[2]

    def get_pixel(self, x: int, y: int) -> Bgr:
        index = (y * self.width + x) * 3
        return (self.data[index], self.data[index + 1], self.data[index + 2])

    # -- 矩形/补丁 ------------------------------------------------------------
    def fill_rect(self, x: int, y: int, width: int, height: int, bgr: Bgr) -> None:
        for yy in range(y, y + height):
            for xx in range(x, x + width):
                self.set_pixel(xx, yy, bgr)

    def paste(self, x: int, y: int, patch: bytes, patch_width: int, patch_height: int) -> None:
        for yy in range(patch_height):
            for xx in range(patch_width):
                index = (yy * patch_width + xx) * 3
                self.set_pixel(
                    x + xx, y + yy,
                    (patch[index], patch[index + 1], patch[index + 2]),
                )

    def to_bytes(self) -> bytes:
        return bytes(self.data)

    def to_raw(self) -> RawImage:
        return RawImage(width=self.width, height=self.height, data=self.to_bytes())


# ---------------------------------------------------------------------------
# 模板夹具（高方差结构模板，避免纯色零方差）
# ---------------------------------------------------------------------------

def distinctive_patch(width: int, height: int, seed: int = 1,
                      low: int = 30, high: int = 220) -> bytes:
    """高方差灰度结构模板（竖条纹 + 轻噪声）；TM_CCOEFF_NORMED 分母非 0。"""
    rng = random.Random(seed)
    out = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            base = low if (x % 4 < 2) else high
            value = max(0, min(255, base + rng.randint(-10, 10)))
            index = (y * width + x) * 3
            out[index] = value
            out[index + 1] = value
            out[index + 2] = value
    return bytes(out)


def template_scene(
    canvas_width: int = 64, canvas_height: int = 48,
    template_width: int = 16, template_height: int = 16,
    at: Tuple[int, int] = (20, 12), seed: int = 1, background: int = 128,
) -> Tuple[RawImage, Tuple[int, int, int, int], bytes]:
    """在平灰底上放置一个结构模板实例。

    Returns:
        ``(raw_image, bbox=(x,y,w,h), template_bytes)``——``bbox`` 为**生成器真值**
        （实例真实位置），非实现输出。
    """
    canvas = Canvas(canvas_width, canvas_height, (background, background, background))
    patch = distinctive_patch(template_width, template_height, seed)
    canvas.paste(at[0], at[1], patch, template_width, template_height)
    return canvas.to_raw(), (at[0], at[1], template_width, template_height), patch


# ---------------------------------------------------------------------------
# 形状夹具（contour / 纹理变色）
# ---------------------------------------------------------------------------

def circle_mask(size: int, radius: float) -> List[List[bool]]:
    """直径 ``size`` 的圆盘布尔掩码。"""
    center = (size - 1) / 2.0
    mask: List[List[bool]] = []
    for y in range(size):
        row = []
        for x in range(size):
            row.append((x - center) ** 2 + (y - center) ** 2 <= radius * radius)
        mask.append(row)
    return mask


def star_mask(size: int, outer: float, inner: float, points: int = 5) -> List[List[bool]]:
    """``points`` 角星形布尔掩码（与圆不同，利于区分形状）。"""
    import math

    center = (size - 1) / 2.0
    mask: List[List[bool]] = []
    vertex_count = points * 2
    for y in range(size):
        row = []
        for x in range(size):
            dx = x - center
            dy = y - center
            radius = math.hypot(dx, dy)
            angle = (math.atan2(dy, dx) + math.pi) / (2 * math.pi)  # 0..1
            phase = angle * vertex_count
            frac = phase - math.floor(phase)
            # 三角波：在 outer/inner 之间往复
            tri = 1.0 - abs(frac * 2.0 - 1.0)
            limit = inner + (outer - inner) * tri
            row.append(radius <= limit)
        mask.append(row)
    return mask


def filled_patch(mask: Sequence[Sequence[bool]], bgr: Bgr) -> bytes:
    """用纯色填充掩码区域（外部为黑）→ 形状相同、内部像素可不同。"""
    size_h = len(mask)
    size_w = len(mask[0])
    out = bytearray(size_w * size_h * 3)
    for y in range(size_h):
        for x in range(size_w):
            if mask[y][x]:
                index = (y * size_w + x) * 3
                out[index] = bgr[0]
                out[index + 1] = bgr[1]
                out[index + 2] = bgr[2]
    return bytes(out)


def halves_patch(mask: Sequence[Sequence[bool]], top: Bgr, bottom: Bgr) -> bytes:
    """掩码区域内**上半/下半**两色填充（结构化参考形状，非零方差）。

    用于「纹理变色但形状不变」夹具的**参考实例**：其内部结构与噪声实例显著不同，
    使模板匹配分数下降（< 阈值），而外轮廓一致使轮廓匹配命中（Hu 距离≈0）。
    """
    size_h = len(mask)
    size_w = len(mask[0])
    out = bytearray(size_w * size_h * 3)
    for y in range(size_h):
        color = top if y < size_h // 2 else bottom
        for x in range(size_w):
            if mask[y][x]:
                index = (y * size_w + x) * 3
                out[index] = color[0]
                out[index + 1] = color[1]
                out[index + 2] = color[2]
    return bytes(out)


def textured_patch(mask: Sequence[Sequence[bool]], seed: int = 7,
                   low: int = 120, high: int = 255) -> bytes:
    """用随机纹理填充掩码区域（**外轮廓不变**，内部纹理/颜色不同）。

    默认下界 120（远离黑色背景）以保证形状边界为**强边缘**，使 Canny 外轮廓闭合稳定。
    """
    size_h = len(mask)
    size_w = len(mask[0])
    rng = random.Random(seed)
    out = bytearray(size_w * size_h * 3)
    for y in range(size_h):
        for x in range(size_w):
            if mask[y][x]:
                index = (y * size_w + x) * 3
                value = rng.randint(low, high)
                out[index] = value
                out[index + 1] = value
                out[index + 2] = value
    return bytes(out)


def rect_mask(width: int, height: int) -> List[List[bool]]:
    """全 ``True`` 的矩形掩码（外轮廓=矩形，边界稳健，利于 contour/template 对照）。"""
    return [[True] * int(width) for _ in range(int(height))]


def rotated_rect_mask(size: int, width: float, height: float,
                      angle_deg: float) -> List[List[bool]]:
    """``size×size`` 画布内**中心对齐**、旋转 ``angle_deg`` 的实心矩形掩码。

    解析式（逐像素点是否在旋转矩形内）**生成锐利像素**，避免仿射插值造成的
    边缘模糊——使 Canny 外轮廓闭合稳健，便于验证 contour 的**旋转不变性**
    （Hu 矩不变量）。``angle_deg=0`` 即轴对齐矩形。
    """
    import math

    center = (size - 1) / 2.0
    angle = math.radians(angle_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    mask: List[List[bool]] = []
    for y in range(int(size)):
        row: List[bool] = []
        for x in range(int(size)):
            dx = x - center
            dy = y - center
            u = dx * cos_a + dy * sin_a
            v = -dx * sin_a + dy * cos_a
            row.append(abs(u) <= width / 2.0 and abs(v) <= height / 2.0)
        mask.append(row)
    return mask


def _erode_mask(mask: Sequence[Sequence[bool]], thickness: int) -> List[List[bool]]:
    """形态学腐蚀：仅当 ``(2*thickness+1)²`` 邻域**全在**掩码内才保留（用于内侧描边）。"""
    size_h = len(mask)
    size_w = len(mask[0])
    out: List[List[bool]] = [[True] * size_w for _ in range(size_h)]
    for y in range(size_h):
        for x in range(size_w):
            keep = True
            for dy in range(-thickness, thickness + 1):
                yy = y + dy
                if yy < 0 or yy >= size_h:
                    keep = False
                    break
                for dx in range(-thickness, thickness + 1):
                    xx = x + dx
                    if xx < 0 or xx >= size_w or not mask[yy][xx]:
                        keep = False
                        break
                if not keep:
                    break
            out[y][x] = keep
    return out


def _fill_masked(mask: Sequence[Sequence[bool]], value_of, border_bgr: int,
                 border: int) -> bytes:
    """按掩码填灰度值；``border_bgr`` 非 None 时在形状**内侧**画 ``border`` 像素实色描边。

    描边的作用：使形状**边界**为**均匀强边缘**（恒定亮色 vs 背景），从而 Canny
    产生**闭合外轮廓**；同时内部纹理的边被描边包围（成洞）→ 不影响外轮廓。
    """
    size_h = len(mask)
    size_w = len(mask[0])
    core = _erode_mask(mask, border) if border_bgr is not None else None
    out = bytearray(size_w * size_h * 3)
    for y in range(size_h):
        for x in range(size_w):
            if mask[y][x]:
                idx = (y * size_w + x) * 3
                if core is not None and not core[y][x]:
                    value = border_bgr
                else:
                    value = value_of(x, y)
                out[idx] = value
                out[idx + 1] = value
                out[idx + 2] = value
    return bytes(out)


def striped_patch(mask: Sequence[Sequence[bool]], period: int = 4, orient: str = "v",
                  low: int = 60, high: int = 255,
                  border_bgr: int = None, border: int = 2) -> bytes:
    """掩码内**条纹纹理**（``orient`` 为 ``"v"`` 竖条纹 / ``"h"`` 横条纹）。

    ``low``/``high`` 为暗/亮条纹灰度；``border_bgr`` 给定时画内侧描边（见 ``_fill_masked``）。
    竖条纹与横条纹**相互正交**（TM_CCOEFF_NORMED 交叉相关低）→ 用于「纹理变色但形状不变」。
    """

    def value_of(x: int, y: int) -> int:
        axis = x if orient == "v" else y
        return high if (axis % period) < (period / 2) else low

    return _fill_masked(mask, value_of, border_bgr, border)


def noisy_patch(mask: Sequence[Sequence[bool]], seed: int = 7, low: int = 60,
                high: int = 255, border_bgr: int = None, border: int = 2) -> bytes:
    """掩码内**随机纹理**（固定种子）；可选内侧描边（见 ``_fill_masked``）。"""
    rng = random.Random(seed)

    def value_of(_x: int, _y: int) -> int:
        return rng.randint(low, high)

    return _fill_masked(mask, value_of, border_bgr, border)


def shape_scene(
    mask: Sequence[Sequence[bool]], interior: bytes, at: Tuple[int, int],
    canvas_width: int, canvas_height: int, background: Bgr = (0, 0, 0),
) -> Tuple[RawImage, Tuple[int, int, int, int]]:
    """把形状补丁放到画布上；返回 ``(raw_image, bbox=(x,y,w,h))``（真值）。"""
    size_h = len(mask)
    size_w = len(mask[0])
    canvas = Canvas(canvas_width, canvas_height, background)
    canvas.paste(at[0], at[1], interior, size_w, size_h)
    return canvas.to_raw(), (at[0], at[1], size_w, size_h)


def patch_only(interior: bytes, width: int, height: int) -> RawImage:
    """把补丁本身作为一张图（用于"模板"侧）。"""
    return RawImage(width=width, height=height, data=bytes(interior))


# ---------------------------------------------------------------------------
# 颜色夹具
# ---------------------------------------------------------------------------

def color_scene_two_blobs(
    blob: Bgr, background: Bgr = (0, 0, 0),
    blob_size: int = 8, gap: int = 16, margin: int = 4,
) -> Tuple[RawImage, Tuple[Tuple[int, int, int, int], Tuple[int, int, int, int]]]:
    """两块**左右分离**的同色方块；返回 ``(raw, (bbox_left, bbox_right))``（真值）。

    用于断言「**绝不**把分散像素拼成一个巨大框」：应得 2 个候选，各自紧致 bbox。
    """
    width = margin + blob_size + gap + blob_size + margin
    height = margin + blob_size + margin
    canvas = Canvas(width, height, background)
    left = (margin, margin)
    right = (margin + blob_size + gap, margin)
    canvas.fill_rect(left[0], left[1], blob_size, blob_size, blob)
    canvas.fill_rect(right[0], right[1], blob_size, blob_size, blob)
    bbox_left = (left[0], left[1], blob_size, blob_size)
    bbox_right = (right[0], right[1], blob_size, blob_size)
    return canvas.to_raw(), (bbox_left, bbox_right)
