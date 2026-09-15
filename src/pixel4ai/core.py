"""pixel4ai 核心：网格文档与绘图操作。

文档 = W×H 的格子，每格只存一个调色板字符，所以「一格一色」由数据结构本身保证。
    "."  透明（保留字符，永远在调色板里）
    "?"  只在 stamp / rows 的行数据里出现，表示「这格保持原样」
坐标 (x, y) 从 0 开始，原点在左上角；矩形、区域的两端都包含。
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import shutil
import string
import zlib
from collections import Counter, deque
from pathlib import Path

TRANSPARENT = "."
KEEP = "?"
VERSION = 1
MAX_SIDE = 1024
HISTORY_LIMIT = 50
# 不含 "-"（会被命令行当成选项）、引号和反斜杠（shell 里难写）
COLOR_CHARS = set(string.ascii_letters + string.digits + "#@%&*+=~^$:;<>|/!")

PRESETS: dict[str, dict[str, str]] = {
    "none": {},
    "mono": {"k": "#111111", "w": "#faf9f4"},
    "gb": {"a": "#0f380f", "b": "#306230", "c": "#8bac0f", "d": "#9bbc0f"},
    "pico8": {
        "k": "#000000", "n": "#1d2b53", "m": "#7e2553", "f": "#008751",
        "b": "#ab5236", "d": "#5f574f", "l": "#c2c3c7", "w": "#fff1e8",
        "r": "#ff004d", "o": "#ffa300", "y": "#ffec27", "g": "#00e436",
        "c": "#29adff", "v": "#83769c", "p": "#ff77a8", "s": "#ffccaa",
    },
}

Region = tuple[int, int, int, int]
_HEX = re.compile(r"#?([0-9a-fA-F]{6})")


class PxlError(Exception):
    """给人和 AI 看的错误，命令行直接打印 message。"""


def parse_color(value) -> str:
    m = _HEX.fullmatch(value.strip()) if isinstance(value, str) else None
    if not m:
        raise PxlError(f"颜色 {value!r} 无效：要 6 位十六进制，比如 1a1a1a")
    return "#" + m.group(1).lower()


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def line_cells(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Bresenham，含两端点。"""
    cells = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x1 >= x0 else -1), (1 if y1 >= y0 else -1)
    err = dx + dy
    while True:
        cells.append((x0, y0))
        if (x0, y0) == (x1, y1):
            return cells
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def ellipse_cells(x0: int, y0: int, x1: int, y1: int, fill: bool) -> set[tuple[int, int]]:
    """内切于矩形的椭圆。半径外扩 0.3 格，小圆才会是像素画里常见的圆角形状而不是菱形/方块。"""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    ex, ey = (x1 - x0) / 2 + 0.3, (y1 - y0) / 2 + 0.3
    inside = {(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)
              if ((x - cx) / ex) ** 2 + ((y - cy) / ey) ** 2 <= 1.0}
    if fill:
        return inside
    return {(x, y) for x, y in inside
            if any((x + dx, y + dy) not in inside for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))}


BAYER4 = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
NEIGHBORS8 = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
FREE_CHAR_ORDER = string.ascii_lowercase + string.ascii_uppercase + string.digits + "#@%&*+=~^$:;<>|/!"


def hash01(x: int, y: int) -> float:
    """按坐标得到 [0, 1) 的固定伪随机数：同一格每次一样，相邻格互不相关。"""
    return ((x * 73856093) ^ (y * 19349663) ^ 0x5BD1E995) % 1000 / 1000


def derive_shade(color: str, darker: bool) -> str | None:
    """从 color 派生一个更暗（色相略偏冷）或更亮（色相略偏暖）的颜色；已经接近纯黑 / 纯白时返回 None。"""
    import colorsys

    r, g, b = (v / 255 for v in hex_to_rgb(color))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    target = 250 / 360 if darker else 50 / 360
    turn = (target - h + 0.5) % 1 - 0.5
    h = (h + max(-0.025, min(0.025, turn))) % 1          # 色相最多偏 9°
    if darker:
        if l < 0.06:
            return None
        l *= 0.78
    else:
        if l > 0.96:
            return None
        l += (1 - l) * 0.35
    rr, gg, bb = colorsys.hls_to_rgb(h, l, s * 0.92)
    out = "#%02x%02x%02x" % tuple(round(v * 255) for v in (rr, gg, bb))
    return None if out == color else out


def rect_cells(x0: int, y0: int, x1: int, y1: int) -> set[tuple[int, int]]:
    return {(x, y) for y in range(min(y0, y1), max(y0, y1) + 1) for x in range(min(x0, x1), max(x0, x1) + 1)}


def polygon_cells(points: list[tuple[int, int]]) -> set[tuple[int, int]]:
    """多边形覆盖的格子：格子中心按奇偶规则判断在不在里面，再补上各条边，细长的形状也不会断开。"""
    if len(points) < 3:
        raise PxlError("多边形至少要 3 个顶点（X Y 成对给出）")
    cells: set[tuple[int, int]] = set()
    n = len(points)
    ys = [p[1] for p in points]
    for y in range(min(ys), max(ys) + 1):
        xs = []
        for i in range(n):
            (xa, ya), (xb, yb) = points[i], points[(i + 1) % n]
            if ya <= y < yb or yb <= y < ya:
                xs.append(xa + (y - ya) * (xb - xa) / (yb - ya))
        xs.sort()
        for a, b in zip(xs[::2], xs[1::2]):
            cells.update((x, y) for x in range(math.ceil(a), math.floor(b) + 1))
    for i in range(n):
        cells.update(line_cells(*points[i], *points[(i + 1) % n]))
    return cells


def inner_distance(cells: set[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """形状内每格到形状外面的步数（8 邻接），最外一圈是 1。"""
    dist: dict[tuple[int, int], int] = {}
    queue: deque = deque()
    for x, y in cells:
        if any((x + dx, y + dy) not in cells for dx, dy in NEIGHBORS8):
            dist[(x, y)] = 1
            queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        for dx, dy in NEIGHBORS8:
            c = (x + dx, y + dy)
            if c in cells and c not in dist:
                dist[c] = dist[(x, y)] + 1
                queue.append(c)
    return dist


def catmull_rom_cells(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """经过所有点的平滑曲线（均匀 Catmull-Rom）。采样后逐段用 Bresenham 连起来，再去掉 L 形拐角（像素完美）。"""
    if len(points) < 2:
        raise PxlError("曲线至少要 2 个点")
    pts = [(float(x), float(y)) for x, y in points]
    samples = [tuple(points[0])]
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else pts[i + 1]
        steps = max(4, int(3 * max(abs(p2[0] - p1[0]), abs(p2[1] - p1[1]))))
        for s in range(1, steps + 1):
            t = s / steps
            t2, t3 = t * t, t * t * t
            x, y = (0.5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3)
                    for a, b, c, d in zip(p0, p1, p2, p3))
            samples.append((round(x), round(y)))
    cells = [samples[0]]
    for p in samples[1:]:
        if p != cells[-1]:
            cells.extend(line_cells(*cells[-1], *p)[1:])
    out: list[tuple[int, int]] = []
    for i, c in enumerate(cells):
        if out and i < len(cells) - 1:
            a, b = out[-1], cells[i + 1]
            if (abs(a[0] - b[0]) == 1 and abs(a[1] - b[1]) == 1
                    and max(abs(c[0] - a[0]), abs(c[1] - a[1])) == 1 and max(abs(c[0] - b[0]), abs(c[1] - b[1])) == 1):
                continue        # c 是 a、b 之间的 L 形拐角，去掉后仍然 8 连通
        out.append(c)
    return out


def horizon_points(w: int, h: int, y0: int, y1: int, wave: int, segments: int, x0: int, x1: int,
                   seed: int = 0) -> list[tuple[int, int]]:
    """起伏地平线的途经点：y0→y1 的斜线上，每段一个大起伏（±wave），段与段中间再加一个小起伏（±0.35·wave）。"""
    import random

    rng = random.Random(seed)
    segments = max(1, min(segments, (x1 - x0) // 2))
    coarse = [rng.uniform(-1, 1) * wave for _ in range(segments + 1)]
    pts = []
    for i in range(segments * 2 + 1):
        x = x0 + (x1 - x0) * i / (segments * 2)
        base = y0 + (y1 - y0) * i / (segments * 2)
        if i % 2 == 0:
            off = coarse[i // 2]
        else:
            off = (coarse[i // 2] + coarse[i // 2 + 1]) / 2 + rng.uniform(-1, 1) * wave * 0.35
        pts.append((round(x), round(base + off)))
    return pts


class Canvas:
    def __init__(self, w: int, h: int, palette: dict[str, str] | None = None):
        if not (1 <= w <= MAX_SIDE and 1 <= h <= MAX_SIDE):
            raise PxlError(f"尺寸 {w}×{h} 无效：宽高都要在 1..{MAX_SIDE}")
        self.w, self.h = w, h
        self.palette: dict[str, str | None] = {TRANSPARENT: None}
        for ch, color in (palette or {}).items():
            self.set_color(ch, color)
        self.ramps: list[str] = []
        self.grid = [[TRANSPARENT] * w for _ in range(h)]

    # ───────── 序列化 ─────────

    @classmethod
    def from_dict(cls, d: dict) -> "Canvas":
        try:
            w, h = (int(v) for v in d["size"])
            palette = dict(d.get("palette", {}))
            rows = list(d["rows"])
        except (KeyError, TypeError, ValueError) as e:
            raise PxlError(f"文件结构不对（需要 size / palette / rows）：{e}") from None
        palette.pop(TRANSPARENT, None)
        cv = cls(w, h, palette)
        if len(rows) != h:
            raise PxlError(f"rows 有 {len(rows)} 行，但 size 写的高是 {h}")
        for y, row in enumerate(rows):
            if not isinstance(row, str) or len(row) != w:
                raise PxlError(f"rows 第 {y} 行长度 {len(row)}，应为宽 {w}")
            cv._check_chars(row, f"rows 第 {y} 行")
            cv.grid[y] = list(row)
        ramps = d.get("ramps", [])
        if not isinstance(ramps, list) or not all(isinstance(r, str) for r in ramps):
            raise PxlError("ramps 应该是字符串数组，每个字符串是一组从暗到亮的调色板字符")
        cv.set_ramps(ramps)
        return cv

    def to_dict(self) -> dict:
        d = {"version": VERSION, "size": [self.w, self.h], "palette": dict(self.palette)}
        if self.ramps:
            d["ramps"] = list(self.ramps)
        d["rows"] = ["".join(r) for r in self.grid]
        return d

    def to_json(self) -> str:
        """每行一个字符串、上下对齐，文件本身就能当 ASCII 画看。"""
        pal = ",\n".join(f"    {json.dumps(k)}: {json.dumps(v)}" for k, v in self.palette.items())
        rows = ",\n".join(f"    {json.dumps(''.join(r))}" for r in self.grid)
        ramps = f'  "ramps": {json.dumps(self.ramps, ensure_ascii=False)},\n' if self.ramps else ""
        return (f'{{\n  "version": {VERSION},\n  "size": [{self.w}, {self.h}],\n'
                f'  "palette": {{\n{pal}\n  }},\n{ramps}  "rows": [\n{rows}\n  ]\n}}\n')

    def copy(self) -> "Canvas":
        cv = Canvas.__new__(Canvas)
        cv.w, cv.h, cv.palette = self.w, self.h, dict(self.palette)
        cv.ramps = list(self.ramps)
        cv.grid = [r[:] for r in self.grid]
        return cv

    def same(self, other: "Canvas") -> bool:
        """内容完全一样（比 to_dict() 比较快得多，大画布上很重要）。"""
        return ((self.w, self.h) == (other.w, other.h) and self.palette == other.palette
                and self.ramps == other.ramps and self.grid == other.grid)

    # ───────── 校验 ─────────

    def palette_chars(self) -> str:
        return " ".join(self.palette)

    def need(self, ch: str) -> str:
        if ch not in self.palette:
            raise PxlError(f"颜色 {ch!r} 不在调色板里（可用：{self.palette_chars()}；"
                           f"要新增用 palette set {ch} RRGGBB）")
        return ch

    def need_point(self, x: int, y: int, label: str = "点"):
        if not (0 <= x < self.w and 0 <= y < self.h):
            raise PxlError(f"{label} ({x},{y}) 超出画布：x 要在 0..{self.w - 1}，y 要在 0..{self.h - 1}")

    def need_region(self, region: Region | None) -> Region:
        if region is None:
            return 0, 0, self.w - 1, self.h - 1
        x0, y0, x1, y1 = region
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        self.need_point(x0, y0, "左上角")
        self.need_point(x1, y1, "右下角")
        return x0, y0, x1, y1

    def _check_chars(self, text: str, where: str, allow_keep: bool = False):
        allowed = self.palette.keys() | ({KEEP} if allow_keep else set())
        if set(text) <= allowed:
            return
        for i, ch in enumerate(text):
            if ch not in allowed:
                raise PxlError(f"{where}第 {i} 个字符 {ch!r} 不在调色板里（可用：{self.palette_chars()}"
                               + ("，? 表示保持原样" if allow_keep else "") + "）")

    # ───────── 调色板 ─────────

    def set_color(self, ch: str, color: str):
        if ch in (TRANSPARENT, KEEP):
            raise PxlError(f"{ch!r} 是保留字符（. 透明，? 保持原样），不能设颜色")
        if len(ch) != 1 or ch not in COLOR_CHARS:
            extra = "".join(sorted(COLOR_CHARS - set(string.ascii_letters + string.digits)))
            raise PxlError(f"调色板字符 {ch!r} 无效：要单个字母、数字或 {extra} 之一")
        self.palette[ch] = parse_color(color)

    def remove_color(self, ch: str, replace_with: str | None = None):
        if ch == TRANSPARENT:
            raise PxlError("透明 . 不能删除")
        self.need(ch)
        used = self.count(ch)
        if used:
            if replace_with is None:
                raise PxlError(f"{ch!r} 还用在 {used} 格上；加 --replace-with 另一个字符")
            self.replace(ch, replace_with)
        del self.palette[ch]
        self.ramps = [r for r in (r.replace(ch, "") for r in self.ramps) if len(r) >= 2]

    # ───────── 绘制 ─────────

    def px(self, points: list[tuple[int, int, str]]):
        for x, y, c in points:
            self.need(c)
            self.need_point(x, y)
        for x, y, c in points:
            self.grid[y][x] = c

    def line(self, x0: int, y0: int, x1: int, y1: int, c: str):
        self.need(c)
        self.need_point(x0, y0, "起点")
        self.need_point(x1, y1, "终点")
        for x, y in line_cells(x0, y0, x1, y1):
            self.grid[y][x] = c

    def rect(self, x0: int, y0: int, x1: int, y1: int, c: str, fill: bool = False):
        self.need(c)
        x0, y0, x1, y1 = self.need_region((x0, y0, x1, y1))
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if fill or x in (x0, x1) or y in (y0, y1):
                    self.grid[y][x] = c

    def ellipse(self, x0: int, y0: int, x1: int, y1: int, c: str, fill: bool = False):
        self.need(c)
        x0, y0, x1, y1 = self.need_region((x0, y0, x1, y1))
        for x, y in ellipse_cells(x0, y0, x1, y1, fill):
            self.grid[y][x] = c

    def curve(self, points: list[tuple[int, int]], c: str, fill: str | None = None, direction: str = "down",
              until: str | None = None) -> dict:
        """经过 points 的平滑曲线。fill 时从曲线向下（或向上）逐列填色，碰到 until 里的字符停下；先填色再画线。

        返回 drawn（画在画布里的格数）、filled、profile（每列曲线最上面一格的 y，用来放置物体）。
        """
        self.need(c)
        if fill is not None:
            self.need(fill)
            xs = [p[0] for p in points]
            if any(b <= a for a, b in zip(xs, xs[1:])):
                raise PxlError("--fill 要求各点的 x 从左到右严格递增（地平线、山脊这类曲线）")
        if direction not in ("down", "up"):
            raise PxlError("--dir 只能是 down 或 up")
        if until:
            self._check_chars(until, "--until 里")
        cells = catmull_rom_cells(points)
        inside = [(x, y) for x, y in cells if 0 <= x < self.w and 0 <= y < self.h]
        if not inside:
            raise PxlError("曲线完全在画布外面")
        top: dict[int, int] = {}
        bottom: dict[int, int] = {}
        for x, y in cells:
            if 0 <= x < self.w:
                top[x] = min(top.get(x, y), y)
                bottom[x] = max(bottom.get(x, y), y)
        filled = 0
        if fill is not None:
            for x in top:
                ys = range(max(0, bottom[x] + 1), self.h) if direction == "down" else range(min(self.h - 1, top[x] - 1), -1, -1)
                for y in ys:
                    if until and self.grid[y][x] in until:
                        break
                    self.grid[y][x] = fill
                    filled += 1
        for x, y in inside:
            self.grid[y][x] = c
        # 局部曲线填充后，端点那一列和外侧相邻列颜色不同的格子会连成一条竖直的直边（生硬的「悬崖」）
        cliffs = []
        if fill is not None:
            mine = {fill, c}
            for x, side in ((min(top), -1), (max(top), 1)):
                nx = x + side
                if 0 <= nx < self.w:
                    n = sum(1 for y in range(self.h) if self.grid[y][x] in mine and self.grid[y][nx] not in mine)
                    if n > 3:
                        cliffs.append(("左" if side < 0 else "右", x, n))
        return {"drawn": len(inside), "filled": filled, "profile": top, "cliffs": cliffs}

    def fill(self, x: int, y: int, c: str, boundary: str | None = None, closed: bool = False) -> dict:
        """油漆桶（4 连通）。默认只扩散到同色的相邻格；给了 boundary 时跨过任何颜色扩散，碰到 boundary 里的字符停下。

        closed=True 时区域碰到画布边就报错（说明轮廓没围住），画布保持不变。返回 filled 和 edges（碰到的画布边）。
        """
        self.need(c)
        self.need_point(x, y)
        if boundary:
            self._check_chars(boundary, "--boundary 里")
            if self.grid[y][x] in boundary:
                raise PxlError(f"起点 ({x},{y}) 本身是边界字符 {self.grid[y][x]!r}，换一个轮廓里面的点")
            passable = lambda ch: ch not in boundary     # noqa: E731
        else:
            target = self.grid[y][x]
            passable = lambda ch: ch == target           # noqa: E731
        region = {(x, y)}
        queue = deque([(x, y)])
        while queue:
            cx, cy = queue.popleft()
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if 0 <= nx < self.w and 0 <= ny < self.h and (nx, ny) not in region and passable(self.grid[ny][nx]):
                    region.add((nx, ny))
                    queue.append((nx, ny))
        xs, ys = [p[0] for p in region], [p[1] for p in region]
        edges = [name for name, hit in (("上", min(ys) == 0), ("下", max(ys) == self.h - 1),
                                        ("左", min(xs) == 0), ("右", max(xs) == self.w - 1)) if hit]
        if closed and edges:
            raise PxlError(f"区域没有封闭：从 ({x},{y}) 扩散到了画布的{'、'.join(edges)}边"
                           + (f"，边界字符 {boundary!r} 没有围住" if boundary else "") + "；画布没有修改")
        for cx, cy in region:
            self.grid[cy][cx] = c
        return {"filled": len(region), "edges": edges}

    def replace(self, a: str, b: str, region: Region | None = None):
        self.need(a)
        self.need(b)
        x0, y0, x1, y1 = self.need_region(region)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if self.grid[y][x] == a:
                    self.grid[y][x] = b

    def stamp(self, x: int, y: int, rows: list[str]):
        """从 (x, y) 起贴一块子网格；行可以不等长，"?" 保持原样。"""
        if not rows:
            raise PxlError("stamp 没有行数据")
        self.need_point(x, y, "stamp 左上角")
        if y + len(rows) > self.h:
            raise PxlError(f"stamp 有 {len(rows)} 行，从 y={y} 起会到 y={y + len(rows) - 1}，"
                           f"超出画布高 {self.h}（最多 {self.h - y} 行）")
        for i, row in enumerate(rows):
            if x + len(row) > self.w:
                raise PxlError(f"stamp 第 {i} 行 {row!r} 长 {len(row)}，从 x={x} 起会到 x={x + len(row) - 1}，"
                               f"超出画布宽 {self.w}（最多 {self.w - x} 个字符）")
            self._check_chars(row, f"stamp 第 {i} 行", allow_keep=True)
        for i, row in enumerate(rows):
            for j, ch in enumerate(row):
                if ch != KEEP:
                    self.grid[y + i][x + j] = ch

    def set_rows(self, y: int, rows: list[str]):
        """从第 y 行起整行覆盖，每行长度必须正好等于画布宽。"""
        if not rows:
            raise PxlError("rows 没有行数据")
        self.need_point(0, y, "起始行")
        if y + len(rows) > self.h:
            raise PxlError(f"rows 有 {len(rows)} 行，从 y={y} 起会到 y={y + len(rows) - 1}，超出画布高 {self.h}")
        for i, row in enumerate(rows):
            if len(row) != self.w:
                raise PxlError(f"rows 第 {i} 行（y={y + i}）{row!r} 长 {len(row)}，应正好等于画布宽 {self.w}")
            self._check_chars(row, f"rows 第 {i} 行（y={y + i}）", allow_keep=True)
        self.stamp(0, y, rows)

    # ───────── 色阶与明暗 ─────────

    def set_ramps(self, ramps: list[str]):
        """色阶：同一材质从暗到亮的一串调色板字符，比如 "JjHh"。shade 靠它知道「暗一档」是哪个颜色。"""
        owner: dict[str, str] = {}
        for r in ramps:
            if len(r) < 2:
                raise PxlError(f"色阶 {r!r} 至少要 2 个字符（从暗到亮）")
            if len(set(r)) != len(r):
                raise PxlError(f"色阶 {r!r} 里有重复字符")
            for ch in r:
                if ch in (TRANSPARENT, KEEP):
                    raise PxlError(f"色阶 {r!r} 里不能有 {ch!r}")
                self.need(ch)
                if ch in owner:
                    raise PxlError(f"字符 {ch!r} 同时出现在色阶 {owner[ch]!r} 和 {r!r} 里；一个颜色只能属于一个色阶")
                owner[ch] = r
        self.ramps = list(ramps)

    def free_char(self) -> str | None:
        return next((c for c in FREE_CHAR_ORDER if c not in self.palette), None)

    def extend_ramps(self, chars: set[str], steps: int) -> list[tuple[str, str, str]]:
        """chars 里的颜色沿色阶移动 steps 档会越过尽头时，在那一端补上派生的新颜色。返回 [(新字符, 颜色, 接在谁后面)]。"""
        added, ramps = [], []
        for r in self.ramps:
            over = 0
            for i, ch in enumerate(r):
                if ch in chars:
                    j = i + steps
                    over = max(over, -j if j < 0 else j - (len(r) - 1))
            for _ in range(over):
                base = r[0] if steps < 0 else r[-1]
                color, ch = derive_shade(self.palette[base], darker=steps < 0), self.free_char()
                if color is None or ch is None:
                    break
                self.palette[ch] = color
                r = ch + r if steps < 0 else r + ch
                added.append((ch, color, base))
            ramps.append(r)
        self.set_ramps(ramps)
        return added

    def shade(self, cells, steps: int = -1, soft: int = 0, only: str | None = None,
              skip: str | None = None, extend: bool = False) -> dict:
        """把 cells 里的每一格沿自己的色阶移动 steps 档（负数变暗，正数变亮，到头为止）。

        soft > 0 时，离形状边缘 soft 格以内逐渐变稀：有序抖动阈值再叠加按坐标的固定扰动，
        边缘既不是一刀切，也不会沿直边排成整齐的行。
        extend=True 时，色阶到头会自动在那一端补一个派生颜色（暗色略偏冷、亮色略偏暖）。
        返回统计：changed 改了几格、at_end 已在色阶尽头、no_ramp 颜色不在任何色阶里、
        filtered 被 only / skip 排除、dithered_out 边缘渐隐跳过、added 新增的颜色。透明格不处理。
        """
        if not self.ramps:
            raise PxlError("还没有声明色阶，shade 不知道「暗一档」是哪个颜色：先 ramp set 暗到亮 …"
                           "（ramp suggest 可以给出建议）")
        if steps == 0:
            raise PxlError("--steps 不能是 0：负数变暗，正数变亮")
        if soft < 0:
            raise PxlError("--soft 不能是负数")
        for label, chars in (("--only", only), ("--except", skip)):
            if chars:
                self._check_chars(chars, f"{label} 里")
        cells = set(cells)
        stats = {"changed": 0, "at_end": 0, "filtered": 0, "dithered_out": 0, "no_ramp": Counter(), "added": []}
        if extend:
            present = {self.grid[y][x] for x, y in cells if 0 <= x < self.w and 0 <= y < self.h}
            present = {ch for ch in present if (not only or ch in only) and not (skip and ch in skip)}
            stats["added"] = self.extend_ramps(present, steps)
            if stats["added"]:
                stats["long_ramps"] = [r for r in self.ramps if len(r) >= 7]
        shift = {ch: r[max(0, min(len(r) - 1, i + steps))] for r in self.ramps for i, ch in enumerate(r)}
        dist = inner_distance(cells) if soft else {}
        for x, y in cells:
            if not (0 <= x < self.w and 0 <= y < self.h):
                continue
            ch = self.grid[y][x]
            if ch == TRANSPARENT:
                continue
            if (only and ch not in only) or (skip and ch in skip):
                stats["filtered"] += 1
                continue
            d = dist.get((x, y), soft + 1)
            if soft and d <= soft and BAYER4[y % 4][x % 4] >= 16 * (d - hash01(x, y)) / (soft + 1):
                stats["dithered_out"] += 1
                continue
            if ch not in shift:
                stats["no_ramp"][ch] += 1
                continue
            if shift[ch] == ch:
                stats["at_end"] += 1
                continue
            self.grid[y][x] = shift[ch]
            stats["changed"] += 1
        return stats

    # ───────── 变换 ─────────

    def mirror(self, mode: str, region: Region | None = None):
        """lr 左半边→右半边，rl 右→左，tb 上→下，bt 下→上。奇数宽/高时中线不动。"""
        x0, y0, x1, y1 = self.need_region(region)
        g = self.grid
        if mode in ("lr", "rl"):
            for y in range(y0, y1 + 1):
                for i in range((x1 - x0 + 1) // 2):
                    a, b = x0 + i, x1 - i
                    if mode == "lr":
                        g[y][b] = g[y][a]
                    else:
                        g[y][a] = g[y][b]
        elif mode in ("tb", "bt"):
            for i in range((y1 - y0 + 1) // 2):
                a, b = y0 + i, y1 - i
                src, dst = (a, b) if mode == "tb" else (b, a)
                g[dst][x0:x1 + 1] = g[src][x0:x1 + 1]
        else:
            raise PxlError(f"mirror 模式 {mode!r} 无效：lr / rl / tb / bt")

    def flip(self, axis: str, region: Region | None = None):
        x0, y0, x1, y1 = self.need_region(region)
        g = self.grid
        if axis == "h":
            for y in range(y0, y1 + 1):
                g[y][x0:x1 + 1] = g[y][x0:x1 + 1][::-1]
        elif axis == "v":
            block = [g[y][x0:x1 + 1] for y in range(y0, y1 + 1)][::-1]
            for i, y in enumerate(range(y0, y1 + 1)):
                g[y][x0:x1 + 1] = block[i]
        else:
            raise PxlError(f"flip 方向 {axis!r} 无效：h / v")

    def shift(self, dx: int, dy: int, wrap: bool = False):
        new = [[TRANSPARENT] * self.w for _ in range(self.h)]
        for y in range(self.h):
            for x in range(self.w):
                nx, ny = x + dx, y + dy
                if wrap:
                    nx, ny = nx % self.w, ny % self.h
                if 0 <= nx < self.w and 0 <= ny < self.h:
                    new[ny][nx] = self.grid[y][x]
        self.grid = new

    ANCHORS = {"tl": (0, 0), "t": (1, 0), "tr": (2, 0), "l": (0, 1), "c": (1, 1),
               "r": (2, 1), "bl": (0, 2), "b": (1, 2), "br": (2, 2)}

    def resize(self, w: int, h: int, anchor: str = "tl"):
        if not (1 <= w <= MAX_SIDE and 1 <= h <= MAX_SIDE):
            raise PxlError(f"尺寸 {w}×{h} 无效：宽高都要在 1..{MAX_SIDE}")
        if anchor not in self.ANCHORS:
            raise PxlError(f"锚点 {anchor!r} 无效：{' / '.join(self.ANCHORS)}")
        ax, ay = self.ANCHORS[anchor]
        ox, oy = (w - self.w) * ax // 2, (h - self.h) * ay // 2
        new = [[TRANSPARENT] * w for _ in range(h)]
        for y in range(self.h):
            for x in range(self.w):
                if 0 <= x + ox < w and 0 <= y + oy < h:
                    new[y + oy][x + ox] = self.grid[y][x]
        self.w, self.h, self.grid = w, h, new

    def clear(self, c: str = TRANSPARENT):
        self.need(c)
        self.grid = [[c] * self.w for _ in range(self.h)]

    # ───────── 查询 ─────────

    def count(self, ch: str) -> int:
        return sum(r.count(ch) for r in self.grid)

    def counts(self) -> dict[str, int]:
        return {ch: self.count(ch) for ch in self.palette}

    def bbox(self) -> Region | None:
        """非透明内容的范围。按行用字符串操作找两端，不逐格遍历。"""
        x0 = y0 = x1 = y1 = None
        for y, r in enumerate(self.grid):
            s = "".join(r)
            left = len(s) - len(s.lstrip(TRANSPARENT))
            if left == len(s):
                continue
            right = len(s.rstrip(TRANSPARENT)) - 1
            if y0 is None:
                y0, x0, x1 = y, left, right
            x0, x1, y1 = min(x0, left), max(x1, right), y
        return None if y0 is None else (x0, y0, x1, y1)


def suggest_ramps(cv: Canvas) -> list[str]:
    """按色相把颜色分组，组内从暗到亮排好。只是起点：色相接近但材质不同的颜色要手动拆开。"""
    import colorsys

    items = []
    for ch, color in cv.palette.items():
        if color is None:
            continue
        r, g, b = (v / 255 for v in hex_to_rgb(color))
        hue, _, sat = colorsys.rgb_to_hls(r, g, b)
        items.append((ch, hue * 360, sat, 0.2126 * r + 0.7152 * g + 0.0722 * b))
    grays = [i for i in items if i[2] < 0.12]
    groups: list[list] = []
    for it in sorted((i for i in items if i[2] >= 0.12), key=lambda i: i[1]):
        # 必须和组里第一个颜色色相接近、和组的平均饱和度接近才能加入：避免色相一路相邻串成一条长链，
        # 也把色相相同但饱和度差很多的材质（比如棕色木头和橙色天空）分开
        for g in groups:
            hue_gap = abs(it[1] - g[0][1]) % 360
            if min(hue_gap, 360 - hue_gap) <= 24 and abs(it[2] - sum(m[2] for m in g) / len(g)) <= 0.3:
                g.append(it)
                break
        else:
            groups.append([it])
    if grays:
        groups.append(grays)
    return ["".join(i[0] for i in sorted(g, key=lambda i: i[3])) for g in groups]


def diff(a: Canvas, b: Canvas) -> tuple[int, Region | None] | None:
    """改动的格数和范围；尺寸不同返回 None。整行相同的直接跳过。"""
    if (a.w, a.h) != (b.w, b.h):
        return None
    n, xs, ys = 0, [], []
    for y, (ra, rb) in enumerate(zip(a.grid, b.grid)):
        if ra == rb:
            continue
        cols = [x for x, (p, q) in enumerate(zip(ra, rb)) if p != q]
        n += len(cols)
        xs += (cols[0], cols[-1])
        ys.append(y)
    if not n:
        return 0, None
    return n, (min(xs), ys[0], max(xs), ys[-1])


# ───────── 文件 ─────────

def atomic_write(path: Path, data: str | bytes):
    """先写临时文件再改名，编辑器监听文件时不会读到写了一半的内容。"""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    os.replace(tmp, path)


def load(path: Path) -> Canvas:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PxlError(f"{path} 不存在；先用 pxl {path} new 宽 高 创建") from None
    try:
        return Canvas.from_dict(json.loads(text))
    except json.JSONDecodeError as e:
        raise PxlError(f"{path} 不是合法 JSON：{e}") from None


def save(cv: Canvas, path: Path):
    atomic_write(Path(path), cv.to_json())


class History:
    """撤销/重做栈，存在旁边的隐藏目录 .<name>.history/：每一步一个 gzip 快照，index.json 记顺序。

    每次操作只读写一个快照，大画布不会越改越慢（旧版把所有快照塞进一个 JSON，512×512 时十几 MB）。
    """

    def __init__(self, path: Path):
        path = Path(path)
        self.dir = path.with_name(f".{path.name}.history")
        self.index = self.dir / "index.json"
        self.legacy = path.with_name(f".{path.name}.history.json")     # 旧版单文件格式，读到就迁移

    def _read(self) -> dict:
        try:
            d = json.loads(self.index.read_text(encoding="utf-8"))
            return {"undo": list(d["undo"]), "redo": list(d["redo"]), "next": int(d["next"])}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        d = {"undo": [], "redo": [], "next": 0}
        if self.legacy.exists():
            try:
                old = json.loads(self.legacy.read_text(encoding="utf-8"))
                for stack in ("undo", "redo"):
                    d[stack] = [self._put(d, doc) for doc in old[stack]]
                self._write(d)
            except (OSError, ValueError, KeyError, TypeError):
                d = {"undo": [], "redo": [], "next": 0}
            self.legacy.unlink(missing_ok=True)
        return d

    def _write(self, d: dict):
        self.dir.mkdir(exist_ok=True)
        atomic_write(self.index, json.dumps(d))

    def _snap(self, sid: int) -> Path:
        return self.dir / f"{sid}.json.gz"

    def _put(self, d: dict, doc: dict) -> int:
        self.dir.mkdir(exist_ok=True)
        sid = d["next"]
        d["next"] += 1
        atomic_write(self._snap(sid), gzip.compress(json.dumps(doc, separators=(",", ":")).encode("utf-8"), 1))
        return sid

    def _get(self, sid: int) -> Canvas:
        try:
            return Canvas.from_dict(json.loads(gzip.decompress(self._snap(sid).read_bytes())))
        except (OSError, ValueError, EOFError, zlib.error) as e:
            raise PxlError(f"撤销历史损坏（{self._snap(sid).name}）：{e}") from None

    def _drop(self, ids):
        for sid in ids:
            self._snap(sid).unlink(missing_ok=True)

    def push(self, before: Canvas):
        d = self._read()
        d["undo"].append(self._put(d, before.to_dict()))
        stale = d["redo"] + d["undo"][:-HISTORY_LIMIT]
        d["undo"], d["redo"] = d["undo"][-HISTORY_LIMIT:], []
        self._write(d)
        self._drop(stale)

    def reset(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.legacy.unlink(missing_ok=True)

    def step(self, current: Canvas, n: int, direction: str) -> tuple[Canvas, int]:
        """direction = "undo" / "redo"，返回 (新状态, 实际走了几步)。"""
        d = self._read()
        src, dst = (d["undo"], d["redo"]) if direction == "undo" else (d["redo"], d["undo"])
        done, used = 0, []
        while done < n and src:
            prev = self._get(src[-1])
            used.append(src.pop())
            dst.append(self._put(d, current.to_dict()))
            current = prev
            done += 1
        if done:
            used += d["undo"][:-HISTORY_LIMIT] + d["redo"][:-HISTORY_LIMIT]
            d["undo"], d["redo"] = d["undo"][-HISTORY_LIMIT:], d["redo"][-HISTORY_LIMIT:]
            self._write(d)
            self._drop(used)
        return current, done
