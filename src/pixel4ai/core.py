"""pixel4ai 核心：网格文档与绘图操作。

文档 = W×H 的格子，每格只存一个调色板字符，所以「一格一色」由数据结构本身保证。
    "."  透明（保留字符，永远在调色板里）
    "?"  只在 stamp / rows 的行数据里出现，表示「这格保持原样」
坐标 (x, y) 从 0 开始，原点在左上角；矩形、区域的两端都包含。
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import string
import zlib
from collections import deque
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


class Canvas:
    def __init__(self, w: int, h: int, palette: dict[str, str] | None = None):
        if not (1 <= w <= MAX_SIDE and 1 <= h <= MAX_SIDE):
            raise PxlError(f"尺寸 {w}×{h} 无效：宽高都要在 1..{MAX_SIDE}")
        self.w, self.h = w, h
        self.palette: dict[str, str | None] = {TRANSPARENT: None}
        for ch, color in (palette or {}).items():
            self.set_color(ch, color)
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
        return cv

    def to_dict(self) -> dict:
        return {"version": VERSION, "size": [self.w, self.h], "palette": dict(self.palette),
                "rows": ["".join(r) for r in self.grid]}

    def to_json(self) -> str:
        """每行一个字符串、上下对齐，文件本身就能当 ASCII 画看。"""
        pal = ",\n".join(f"    {json.dumps(k)}: {json.dumps(v)}" for k, v in self.palette.items())
        rows = ",\n".join(f"    {json.dumps(''.join(r))}" for r in self.grid)
        return (f'{{\n  "version": {VERSION},\n  "size": [{self.w}, {self.h}],\n'
                f'  "palette": {{\n{pal}\n  }},\n  "rows": [\n{rows}\n  ]\n}}\n')

    def copy(self) -> "Canvas":
        cv = Canvas.__new__(Canvas)
        cv.w, cv.h, cv.palette = self.w, self.h, dict(self.palette)
        cv.grid = [r[:] for r in self.grid]
        return cv

    def same(self, other: "Canvas") -> bool:
        """内容完全一样（比 to_dict() 比较快得多，大画布上很重要）。"""
        return (self.w, self.h) == (other.w, other.h) and self.palette == other.palette and self.grid == other.grid

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

    def fill(self, x: int, y: int, c: str):
        """油漆桶，4 连通。"""
        self.need(c)
        self.need_point(x, y)
        target = self.grid[y][x]
        if target == c:
            return
        self.grid[y][x] = c
        queue = deque([(x, y)])
        while queue:
            cx, cy = queue.popleft()
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if 0 <= nx < self.w and 0 <= ny < self.h and self.grid[ny][nx] == target:
                    self.grid[ny][nx] = c
                    queue.append((nx, ny))

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
