"""把画布变成给 AI / 人看的东西：带坐标尺的视图、ASCII、终端真彩色、PNG。"""
from __future__ import annotations

import math
import shlex
import struct
import zlib
from collections import Counter
from pathlib import Path

from .core import TRANSPARENT, Canvas, PxlError, Region, hex_to_rgb, parse_color, suggest_ramps

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
    if cv.ramps:
        lines.append("色阶（暗→亮） " + "  ".join(cv.ramps))
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


def _lum(color: str) -> float:
    r, g, b = hex_to_rgb(color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ramps_text(cv: Canvas) -> str:
    if not cv.ramps:
        return "还没有声明色阶。ramp suggest 给出按色相分组的建议；ramp set 暗到亮 … 声明。"
    lines = ["色阶（暗 → 亮）："]
    for r in cv.ramps:
        lums = [_lum(cv.palette[ch]) for ch in r]
        warn = "" if all(a <= b for a, b in zip(lums, lums[1:])) else "   ⚠ 顺序不是从暗到亮，shade 会按这个顺序移动"
        lines.append("  " + " → ".join(f"{ch} {cv.palette[ch]}" for ch in r) + warn)
    free = [ch for ch, c in cv.palette.items() if c and not any(ch in r for r in cv.ramps)]
    if free:
        lines.append("不在任何色阶里（shade 不会改变它们）：" + " ".join(free))
    return "\n".join(lines)


def ramp_suggest_text(cv: Canvas) -> str:
    groups = suggest_ramps(cv)
    multi = [g for g in groups if len(g) >= 2]
    single = [g for g in groups if len(g) == 1]
    lines = ["按色相分组、组内从暗到亮的建议。这只是起点：色相接近但属于不同材质的颜色"
             "（比如天空的紫和远山的紫）要手动拆开，否则 shade 会把天空色压到山的颜色上。"]
    lines += ["  " + " → ".join(f"{ch} {cv.palette[ch]}" for ch in g) for g in multi]
    if single:
        lines.append("没有同色相伙伴的单色：" + " ".join(single))
    if multi:
        lines.append("按建议声明：pxl 文件.pxl ramp set " + " ".join(shlex.quote(g) for g in multi))
    return "\n".join(lines)


def shade_note(stats: dict, steps: int) -> str:
    end = "最暗" if steps < 0 else "最亮"
    parts = [f"{'压暗' if steps < 0 else '提亮'} {stats['changed']} 格"]
    for ch, color, base in stats.get("added", []):
        parts.append(f"新增颜色 {ch} {color}（接在 {base} 的{end}一端）")
    if stats["at_end"]:
        hint = "，色阶不够长：加 --extend 自动补一个颜色" if stats["at_end"] > stats["changed"] and not stats.get("added") else ""
        parts.append(f"{stats['at_end']} 格已在色阶{end}一端没变{hint}")
    if stats["dithered_out"]:
        parts.append(f"边缘渐隐跳过 {stats['dithered_out']} 格")
    if stats["filtered"]:
        parts.append(f"被 --only / --except 排除 {stats['filtered']} 格")
    if stats["no_ramp"]:
        chars = " ".join(f"{ch}×{n}" for ch, n in stats["no_ramp"].most_common(8))
        parts.append(f"⚠ {sum(stats['no_ramp'].values())} 格的颜色不在任何色阶里没变（{chars}），用 ramp add 补上")
    if stats.get("long_ramps"):
        parts.append(f"⚠ 色阶 {' '.join(stats['long_ramps'])} 已有 7 档以上：重叠的影子可能在反复压暗，"
                     "同一光源的影子请把多个形状写进一条 shade")
    return "；".join(parts)


def curve_note(stats: dict, cv: Canvas, fill: str | None, direction: str, until: str | None) -> str:
    prof = stats["profile"]
    xs = sorted(prof)
    ys = [prof[x] for x in xs]
    amp = max(ys) - min(ys)
    step = max(1, (xs[-1] - xs[0]) // 16)
    sampled = xs[::step] + ([xs[-1]] if (len(xs) - 1) % step else [])
    parts = [f"曲线 {stats['drawn']} 格，x {xs[0]}–{xs[-1]}，y {min(ys)}–{max(ys)}（起伏 {amp} 格）"]
    if fill is not None:
        parts.append(f"{'向下' if direction == 'down' else '向上'}填充 {stats['filled']} 格"
                     + (f"（碰到 {until} 停下）" if until else ""))
    parts.append("高度剖面 x:y " + " ".join(f"{x}:{prof[x]}" for x in sampled))
    need = math.ceil(cv.h * 0.03)
    if xs[-1] - xs[0] + 1 >= cv.w // 2 and amp < need:
        parts.append(f"⚠ 起伏只有 {amp} 格，不到画布高度的 3%（{need} 格）：看起来会像一条直线")
    for side, x, n in stats.get("cliffs", []):
        parts.append(f"⚠ 填充在{side}端 x={x} 形成 {n} 格高的竖直边：之后画的近处一层要能盖住它"
                     "（端点 y 要低于那一层的地面），否则改成从画布边缘开始")
    return "；".join(parts)


def fill_note(stats: dict, cv: Canvas) -> str:
    note = f"填充 {stats['filled']} 格（占画布 {stats['filled'] / (cv.w * cv.h):.0%}）"
    if stats["edges"]:
        note += "；碰到画布边：" + "、".join(stats["edges"])
    return note


__all__ = ["view", "palette_line", "info", "ascii_art", "ansi", "png_bytes", "TRANSPARENT"]
