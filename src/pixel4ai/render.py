"""把画布变成给 AI / 人看的东西：带坐标尺的视图、ASCII、终端真彩色、PNG。"""
from __future__ import annotations

import struct
import zlib
from collections import Counter
from pathlib import Path

from .core import TRANSPARENT, Canvas, PxlError, Region, hex_to_rgb, parse_color

MAX_PNG_SIDE = 8192
MAX_VIEW = 64               # 文字视图最多显示多少行/列，超过就缩略
ASCII_RAMP = "@%#*+=-:. "  # 暗 → 亮


def auto_step(width: int, height: int, limit: int = MAX_VIEW) -> int:
    """缩略到最长边不超过 limit 个字符需要的倍数。"""
    return max(1, -(-max(width, height) // limit))


def view(cv: Canvas, region: Region | None = None, spaced: bool = False, step: int = 1) -> str:
    """
            1
      0123456789012
    0 ....kkkk.....

    step > 1 时显示缩略视图，见 overview()。
    """
    x0, y0, x1, y1 = cv.need_region(region)
    if step > 1:
        return overview(cv, (x0, y0, x1, y1), step, spaced)
    sep = " " if spaced else ""
    label_w = len(str(y1))
    pad = " " * label_w + "  "
    lines = []
    for k in range(len(str(x1)) - 1, -1, -1):
        marks = []
        for x in range(x0, x1 + 1):
            s = str(x)
            if k == 0:
                marks.append(s[-1])
            elif (x % 10 ** k == 0 or x == x0) and len(s) > k:
                marks.append(s[-k - 1])
            else:
                marks.append(" ")
        lines.append((pad + sep.join(marks)).rstrip())
    for y in range(y0, y1 + 1):
        lines.append(f"{y:>{label_w}}  {sep.join(cv.grid[y][x0:x1 + 1])}")
    return "\n".join(lines)


def block_char(cv: Canvas, x0: int, y0: int, x1: int, y1: int) -> str:
    """块里最多的非透明颜色；整块透明才是 "."（细线在缩略图里也不会消失）。"""
    counts = Counter()
    for y in range(y0, y1 + 1):
        counts.update(cv.grid[y][x0:x1 + 1])
    solid = [(n, ch) for ch, n in counts.items() if ch != TRANSPARENT]
    return max(solid)[1] if solid else TRANSPARENT


def overview(cv: Canvas, region: Region, step: int, spaced: bool = False) -> str:
    """缩略视图：每个字符代表 step×step 格。行号是原图 y；列号竖着写，从上往下读就是原图 x。

             11
      04826048
      ...
    """
    x0, y0, x1, y1 = region
    xs, ys = range(x0, x1 + 1, step), range(y0, y1 + 1, step)
    sep = " " if spaced else ""
    label_w, digits = len(str(ys[-1])), len(str(xs[-1]))
    pad = " " * label_w + "  "
    lines = [f"缩略视图：每个字符 = {step}×{step} 格（块内最多的非透明颜色）。列号竖着读，坐标都是原图坐标；"
             f"看逐格细节用 view --region X0 Y0 X1 Y1（不超过 {MAX_VIEW}×{MAX_VIEW}）"]
    for k in range(digits):
        lines.append((pad + sep.join(str(x).rjust(digits)[k] for x in xs)).rstrip())
    for y in ys:
        row = sep.join(block_char(cv, x, y, min(x + step - 1, x1), min(y + step - 1, y1)) for x in xs)
        lines.append(f"{y:>{label_w}}  {row}")
    return "\n".join(lines)


def palette_line(cv: Canvas) -> str:
    return "调色板 " + " ".join(f"{ch}={c}" for ch, c in cv.palette.items() if c) + "  (.=透明)"


def info(cv: Canvas, path: Path) -> str:
    counts = cv.counts()
    bb = cv.bbox()
    lines = [f"{path}  {cv.w}×{cv.h}",
             "内容范围 " + (f"({bb[0]},{bb[1]})-({bb[2]},{bb[3]})" if bb else "空"),
             "调色板"]
    for ch, color in cv.palette.items():
        lines.append(f"  {ch}  {color or '透明':<8} {counts[ch]:>5} 格")
    return "\n".join(lines)


def ascii_art(cv: Canvas, ramp: bool = False) -> str:
    """ramp=False 原样输出调色板字符（能再导入）；ramp=True 按亮度换成 @%#*+=-:. 并横向加倍，任何调色板都能看。"""
    if not ramp:
        return "\n".join("".join(r) for r in cv.grid)
    chars = {}
    for ch, color in cv.palette.items():
        if color is None:
            chars[ch] = "  "
        else:
            r, g, b = hex_to_rgb(color)
            lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
            chars[ch] = ASCII_RAMP[min(len(ASCII_RAMP) - 1, int(lum * len(ASCII_RAMP)))] * 2
    return "\n".join("".join(chars[ch] for ch in r).rstrip() for r in cv.grid)


def ansi(cv: Canvas) -> str:
    """终端真彩色预览：一个字符 ▀ 显示上下两格。"""
    def rgb(ch):
        color = cv.palette[ch]
        return ";".join(map(str, hex_to_rgb(color))) if color else None

    out = []
    for y in range(0, cv.h, 2):
        parts = []
        for x in range(cv.w):
            top = rgb(cv.grid[y][x])
            bot = rgb(cv.grid[y + 1][x]) if y + 1 < cv.h else None
            if top is None and bot is None:
                parts.append("\x1b[0m ")
            elif top is None:
                parts.append(f"\x1b[0m\x1b[38;2;{bot}m▄")
            elif bot is None:
                parts.append(f"\x1b[0m\x1b[38;2;{top}m▀")
            else:
                parts.append(f"\x1b[38;2;{top};48;2;{bot}m▀")
        out.append("".join(parts) + "\x1b[0m")
    return "\n".join(out)


def png_bytes(cv: Canvas, scale: int = 8, grid: bool = False, bg: str | None = None) -> bytes:
    """最近邻放大的 RGBA PNG，只用标准库。grid=True 时格子之间画 1px 网格线。"""
    if scale < 1:
        raise PxlError("--scale 至少是 1")
    g = 1 if grid else 0
    width, height = cv.w * scale + g * (cv.w + 1), cv.h * scale + g * (cv.h + 1)
    if max(width, height) > MAX_PNG_SIDE:
        raise PxlError(f"输出 {width}×{height} px 太大（上限 {MAX_PNG_SIDE}），调小 --scale")
    empty = bytes((*hex_to_rgb(parse_color(bg)), 255)) if bg else bytes(4)
    colors = {ch: bytes((*hex_to_rgb(c), 255)) if c else empty for ch, c in cv.palette.items()}
    line_px = bytes((128, 128, 128, 110))
    grid_row = b"\x00" + line_px * width

    raw = bytearray()
    if not grid:
        scaled = {ch: c * scale for ch, c in colors.items()}
        for row in cv.grid:
            raw += (b"\x00" + b"".join(map(scaled.__getitem__, row))) * scale
    else:
        for row in cv.grid:
            raw += grid_row
            scan = bytearray(b"\x00" + line_px)
            for ch in row:
                scan += colors[ch] * scale + line_px
            raw += bytes(scan) * scale
        raw += grid_row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


__all__ = ["view", "palette_line", "info", "ascii_art", "ansi", "png_bytes", "TRANSPARENT"]
