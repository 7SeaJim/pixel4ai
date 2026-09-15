"""构图自检：pxl 文件 inspect，以及 apply 之后的一行摘要。

检查项来自 dusk_cabin 的排查（见 TODO.md「复杂场景」）：
    交界近似直线（起伏不到画布高度 3%）、同一材质的色带之间没有过渡、
    同一材质各交界的过渡宽度都一样、大块单色平涂、
    物体和地面相接处没有变暗（缺接触阴影，需要用 --object 指定物体范围）。
检查是启发式的：只提示，不阻止；最后仍要导出 PNG 目测。

「材质」按色阶判断：同一组色阶（ramps）里的颜色算同一种材质；没声明色阶时每种颜色各算一种。

接触阴影为什么不自动识别物体：试过按形状和位置区分地面、背景、物体，在层层遮挡的真实场景里
（dusk_cabin）要么把云和天空碎块当成物体误报，要么把没贴画布边的草地当成物体、把木屋合进去漏报。
AI 放物体时本来就知道它的范围（按 curve / horizon 的高度剖面放的），直接指定最可靠。
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, deque

from .core import TRANSPARENT, Canvas, PxlError, hex_to_rgb

SEAM_MIN_COVER = 0.5        # 交界至少横跨画布宽度的一半才统计
RELIEF_MIN = 0.03           # 起伏低于画布高度的 3% 算近似直线
FLAT_LIST = 0.08            # 单色连通区占画布 8% 以上列出
FLAT_WARN = 0.12            # 占 12% 以上且不贴顶才警告（贴顶的通常是天空）
CONTACT_DARKER = 8          # 物体底边下方要比两侧地面暗这么多（亮度 0–255）才算有接触阴影
LINT_MIN_SIDE = 64
LINT_MAX_CELLS = 512 * 512
REPORT_MAX_SEAMS = 8


def lum(color: str) -> float:
    r, g, b = hex_to_rgb(color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def material_of(cv: Canvas) -> dict[str, str]:
    mat = {ch: ch for ch in cv.palette}
    for ramp in cv.ramps:
        for ch in ramp:
            mat[ch] = ramp
    return mat


def smooth_column(col: list[str], half: int = 3) -> list[str]:
    """竖直滑动窗口（2·half+1 格）取多数颜色，把抖动混合带平滑成一条切换线。"""
    n = len(col)
    counts = Counter(col[:half + 1])
    out = []
    for y in range(n):
        out.append(max(counts, key=counts.__getitem__))
        if y + half + 1 < n:
            counts[col[y + half + 1]] += 1
        if y - half >= 0:
            gone = col[y - half]
            counts[gone] -= 1
            if not counts[gone]:
                del counts[gone]
    return out


def mix_width(cv: Canvas, x: int, y: int, a: str, b: str) -> int:
    """交界 (x, y) 附近 a、b 上下相邻的行跨了几行：没有抖动的硬边是 2，抖动带越宽数值越大。"""
    rows = []
    for r in range(max(0, y - 10), min(cv.h, y + 11)):
        ch = cv.grid[r][x]
        if ch in (a, b):
            other = b if ch == a else a
            if (r > 0 and cv.grid[r - 1][x] == other) or (r + 1 < cv.h and cv.grid[r + 1][x] == other):
                rows.append(r)
    return rows[-1] - rows[0] + 1 if rows else 0


def find_seams(cv: Canvas) -> list[dict]:
    by_pair: dict[tuple[str, str], dict[int, list[int]]] = {}
    for x in range(cv.w):
        sm = smooth_column([cv.grid[y][x] for y in range(cv.h)])
        runs, start = [], 0
        for y in range(1, cv.h + 1):
            if y == cv.h or sm[y] != sm[start]:
                runs.append((sm[start], start, y - start))
                start = y
        for (a, _, la), (b, sb, lb) in zip(runs, runs[1:]):
            if la >= 3 and lb >= 3:
                by_pair.setdefault((a, b), {}).setdefault(x, []).append(sb)
    seams = []
    for (a, b), cols in by_pair.items():
        if len(cols) < cv.w * SEAM_MIN_COVER:
            continue
        every = sorted(y for ys in cols.values() for y in ys)
        med = every[len(every) // 2]
        track = {x: min(ys, key=lambda y: abs(y - med)) for x, ys in cols.items()}
        ys = sorted(track.values())
        p10, p90 = ys[int(0.1 * (len(ys) - 1))], ys[int(0.9 * (len(ys) - 1))]
        seams.append({"a": a, "b": b, "cover": len(track), "y0": ys[0], "y1": ys[-1], "relief": p90 - p10,
                      "mix": statistics.median(mix_width(cv, x, y, a, b) for x, y in track.items())})
    return sorted(seams, key=lambda s: s["y0"])


def flat_regions(cv: Canvas) -> list[dict]:
    """单色 4 连通区，按面积从大到小，只返回占画布 FLAT_LIST 以上的。"""
    W, H, grid = cv.w, cv.h, cv.grid
    seen = [[False] * W for _ in range(H)]
    regions = []
    for sy in range(H):
        for sx in range(W):
            if seen[sy][sx]:
                continue
            ch = grid[sy][sx]
            seen[sy][sx] = True
            queue = deque([(sx, sy)])
            n, x0, y0, x1, y1 = 0, sx, sy, sx, sy
            while queue:
                x, y = queue.popleft()
                n += 1
                x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < W and 0 <= ny < H and not seen[ny][nx] and grid[ny][nx] == ch:
                        seen[ny][nx] = True
                        queue.append((nx, ny))
            if n >= FLAT_LIST * W * H:
                regions.append({"char": ch, "size": n, "share": n / (W * H), "bbox": (x0, y0, x1, y1),
                                "warn": n >= FLAT_WARN * W * H and y0 > 0 and ch != TRANSPARENT})
    return sorted(regions, key=lambda r: -r["size"])


def base_check(cv: Canvas, box: tuple[int, int, int, int]) -> dict:
    """物体 box 底边下方 3 行的平均亮度，和同一高度、物体两侧 5–24 格的地面比较。

    两侧取不到（物体太宽或贴边）时，改和正下方 6–12 行的地面比。
    """
    x0, y0, x1, y1 = cv.need_region(box)
    L = {ch: lum(c) for ch, c in cv.palette.items() if c}

    def mean(cells):
        vals = [L[cv.grid[y][x]] for x, y in cells
                if 0 <= x < cv.w and 0 <= y < cv.h and cv.grid[y][x] != TRANSPARENT]
        return sum(vals) / len(vals) if vals else None

    near = mean([(x, y) for y in range(y1 + 1, y1 + 4) for x in range(x0, x1 + 1)])
    ref = mean([(x, y) for y in range(y1 + 1, y1 + 4)
                for x in list(range(x0 - 24, x0 - 4)) + list(range(x1 + 5, x1 + 25))])
    where = "两侧同高度地面"
    if ref is None:
        ref, where = mean([(x, y) for y in range(y1 + 6, y1 + 13) for x in range(x0, x1 + 1)]), "正下方更远的地面"
    if near is None or ref is None:
        return {"box": (x0, y0, x1, y1), "near": near, "ref": ref, "where": where, "ok": None}
    return {"box": (x0, y0, x1, y1), "near": near, "ref": ref, "where": where, "ok": near - ref < -CONTACT_DARKER}


def analyze(cv: Canvas, objects: list[tuple[int, int, int, int]] | None = None) -> dict:
    mat = material_of(cv)
    need = math.ceil(cv.h * RELIEF_MIN)
    warn: Counter = Counter()
    seams = find_seams(cv)
    gradient_mix = []
    for s in seams:
        s["flags"] = []
        if s["relief"] < need:
            s["flags"].append(f"近似直线（起伏 < {need} 格）")
            warn["近似直线的交界"] += 1
        ca, cb = cv.palette.get(s["a"]), cv.palette.get(s["b"])
        same_ramp = mat.get(s["a"]) == mat.get(s["b"]) and len(mat.get(s["a"], "")) > 1
        close = not cv.ramps and ca and cb and abs(lum(ca) - lum(cb)) < 40
        s["gradient"] = bool(same_ramp or close)
        if s["gradient"]:
            gradient_mix.append(s["mix"])
            if s["mix"] <= 2:
                s["flags"].append("没有过渡带")
                warn["没有过渡带的色带交界"] += 1
    uniform = len(gradient_mix) >= 3 and max(gradient_mix) - min(gradient_mix) <= 1
    if uniform:
        warn["宽度都一样的过渡带"] += 1
    flats = flat_regions(cv)[:3]
    warn["大块平涂"] += sum(1 for f in flats if f["warn"])
    bases = [base_check(cv, box) for box in objects or []]
    warn["没有接触阴影的物体"] += sum(1 for b in bases if b["ok"] is False)
    return {"seams": seams, "uniform": uniform, "flats": flats, "bases": bases, "objects_given": bool(objects),
            "warn": +warn}


def report(cv: Canvas, name: str, objects: list[tuple[int, int, int, int]] | None = None) -> tuple[str, Counter]:
    data = analyze(cv, objects)
    lines = [f"构图自检 {name} {cv.w}×{cv.h}"
             + ("" if cv.ramps else "（没有声明色阶：每种颜色各算一种材质，声明后判断更准）")]
    seams = data["seams"]
    lines.append(f"交界（横跨 ≥{SEAM_MIN_COVER:.0%} 画布宽度）：{len(seams)} 条" + ("" if seams else "，没有"))
    for s in seams[:REPORT_MAX_SEAMS]:
        flags = "".join(f"  ⚠ {f}" for f in s["flags"])
        lines.append(f"  {s['a']}→{s['b']}  横跨 {s['cover']} 列  y {s['y0']}–{s['y1']}  起伏 {s['relief']} 格  "
                     f"混合带 {s['mix']:g} 行{flags}")
    if len(seams) > REPORT_MAX_SEAMS:
        lines.append(f"  …另有 {len(seams) - REPORT_MAX_SEAMS} 条")
    if data["uniform"]:
        lines.append("  ⚠ 同一材质的各条交界，混合带宽度都一样：让它们有宽有窄")
    if data["flats"]:
        lines.append(f"大块平涂（单色连通区 ≥{FLAT_LIST:.0%} 画布）：")
        for f in data["flats"]:
            color = cv.palette.get(f["char"]) or "透明"
            lines.append(f"  {f['char']} {color}  {f['size']} 格（{f['share']:.0%}）  {tuple(f['bbox'])}"
                         + ("  ⚠ 考虑加明暗变化或纹理" if f["warn"] else ""))
    if data["objects_given"]:
        lines.append("物体接触阴影（底边下方 3 行 vs 参考地面的平均亮度）：")
        for b in data["bases"][:8]:
            if b["ok"] is None:
                lines.append(f"  {b['box']}  底边下方或参考地面在画布外，无法判断")
                continue
            mark = "✓ 有接触阴影" if b["ok"] else f"⚠ 没有接触阴影（要比{b['where']}暗 {CONTACT_DARKER} 以上）"
            lines.append(f"  {b['box']}  下方 {b['near']:.0f}，{b['where']} {b['ref']:.0f}（差 {b['near'] - b['ref']:+.0f}）  {mark}")
    else:
        lines.append("物体接触阴影：没有检查。用 --object X0 Y0 X1 Y1 指定每个落地物体的范围（可以写多个）")
    warn = data["warn"]
    if warn:
        lines.append("结论：" + " · ".join(f"⚠ {n} 处{k}" for k, n in warn.items()))
    else:
        lines.append("结论：没有发现问题（检查是启发式的，仍需导出 PNG 目测）")
    return "\n".join(lines), warn


def summary(cv: Canvas, name: str) -> str | None:
    """apply 之后的一行摘要（不含接触阴影，那一项要指定物体范围）；画布太小（sprite）或太大（太慢）时跳过。"""
    if min(cv.w, cv.h) < LINT_MIN_SIDE or cv.w * cv.h > LINT_MAX_CELLS:
        return None
    warn = analyze(cv)["warn"]
    if not warn:
        return None
    return ("构图检查：" + "，".join(f"⚠ {n} 处{k}" for k, n in warn.items())
            + f"。详情：pxl {name} inspect（关闭这行提示：--no-lint）")


def outline_canvas(cv: Canvas) -> Canvas:
    """只保留材质交界的格子，其余换成透明，用来给 view --outline 显示。"""
    mat, g = material_of(cv), cv.grid
    out = cv.copy()
    for y in range(cv.h):
        for x in range(cv.w):
            m = mat[g[y][x]]
            edge = any(0 <= nx < cv.w and 0 <= ny < cv.h and mat[g[ny][nx]] != m
                       for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
            out.grid[y][x] = g[y][x] if edge else TRANSPARENT
    return out


__all__ = ["analyze", "report", "summary", "outline_canvas", "base_check", "find_seams", "flat_regions", "PxlError"]
