#!/usr/bin/env python3
"""生成飞牛应用图标（纯标准库，无 Pillow 依赖）。

输出：
  packaging/fnos/ICON.PNG                  64x64
  packaging/fnos/ICON_256.PNG              256x256
  packaging/fnos/app/ui/images/icon_64.png
  packaging/fnos/app/ui/images/icon_256.png
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    (ROOT / "packaging/fnos/ICON.PNG", 64),
    (ROOT / "packaging/fnos/ICON_256.PNG", 256),
    (ROOT / "packaging/fnos/app/ui/images/icon_64.png", 64),
    (ROOT / "packaging/fnos/app/ui/images/icon_256.png", 256),
]
SS = 4  # 超采样倍数


def _rounded(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _triangle(x: float, y: float, cx: float, cy: float, s: float) -> bool:
    # 朝右的等腰三角形：左边 x = cx - s*0.45，右边顶点 x = cx + s*0.55
    if x < cx - s * 0.45 or x > cx + s * 0.55:
        return False
    t = (x - (cx - s * 0.45)) / s
    half = s * 0.62 * (1 - t)
    return abs(y - cy) <= half


def _over(dst: tuple[float, float, float, float], color: tuple[float, float, float], alpha: float):
    r, g, b, a = dst
    na = alpha + a * (1 - alpha)
    if na <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    nr = (color[0] * alpha + r * a * (1 - alpha)) / na
    ng = (color[1] * alpha + g * a * (1 - alpha)) / na
    nb = (color[2] * alpha + b * a * (1 - alpha)) / na
    return (nr, ng, nb, na)


def render(size: int) -> bytes:
    n = size * SS
    pixels = [(0.0, 0.0, 0.0, 0.0)] * (n * n)
    for j in range(n):
        y = (j + 0.5) / n
        for i in range(n):
            x = (i + 0.5) / n
            px = (0.0, 0.0, 0.0, 0.0)
            if _rounded(x, y, 0.03, 0.03, 0.97, 0.97, 0.22):
                t = (x + y) / 2
                start = (0.31, 0.55, 1.0)
                end = (0.49, 0.36, 1.0)
                bg = tuple(start[k] + (end[k] - start[k]) * t for k in range(3))
                px = (bg[0], bg[1], bg[2], 1.0)
                if _rounded(x, y, 0.17, 0.25, 0.83, 0.60, 0.055):
                    px = _over(px, (1.0, 1.0, 1.0), 0.94)
                    if 0.30 < y < 0.335 and 0.21 < x < 0.55:
                        px = _over(px, (0.31, 0.55, 1.0), 0.45)
                    if 0.30 < y < 0.335 and 0.60 < x < 0.79:
                        px = _over(px, (0.31, 0.55, 1.0), 0.45)
                    if _triangle(x, y, 0.50, 0.455, 0.12):
                        px = _over(px, (0.31, 0.43, 0.97), 0.95)
                if _rounded(x, y, 0.17, 0.66, 0.83, 0.77, 0.055):
                    px = _over(px, (1.0, 1.0, 1.0), 0.82)
                    if _rounded(x, y, 0.215, 0.695, 0.52, 0.735, 0.02):
                        px = _over(px, (0.49, 0.36, 1.0), 0.9)
            pixels[j * n + i] = px

    # 盒式降采样 + 预乘 alpha，避免透明边缘发黑
    out = bytearray()
    for y in range(size):
        out.append(0)  # filter: none
        for x in range(size):
            r = g = b = a = 0.0
            for dy in range(SS):
                for dx in range(SS):
                    pr, pg, pb, pa = pixels[(y * SS + dy) * n + (x * SS + dx)]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            total = SS * SS
            if a > 0:
                r, g, b = r / a, g / a, b / a
            out.extend(
                (
                    max(0, min(255, round(r * 255))),
                    max(0, min(255, round(g * 255))),
                    max(0, min(255, round(b * 255))),
                    max(0, min(255, round(a / total * 255))),
                )
            )
    return bytes(out)


def write_png(path: Path, size: int) -> None:
    raw = render(size)
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    print(f"{path.relative_to(ROOT)}  {size}x{size}  {len(png)} bytes")


if __name__ == "__main__":
    for target, size in TARGETS:
        write_png(target, size)
