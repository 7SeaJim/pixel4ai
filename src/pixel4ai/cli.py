"""pxl 命令行。

    pxl [-q] 文件.pxl 命令 [参数]

每次修改都会原子写回文件，并打印「改了几格、在哪」和带坐标尺的视图，AI 靠它核对。
批量：pxl 文件.pxl apply <<'EOF' ... EOF，每行一条命令，全部成功才保存。
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

from . import core, render
from .core import PRESETS, TRANSPARENT, Canvas, PxlError

FEEDBACK_MARGIN = 2         # 大画布改动后，显示改动范围外扩几格
READ_ONLY = {"info", "view", "export", "edit", "inspect"}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PxlError(f"{message}\n{self.format_usage().strip().replace('usage:', '用法:', 1)}")


def build_parser() -> Parser:
    p = Parser(prog="pxl 文件.pxl", formatter_class=argparse.RawDescriptionHelpFormatter,
               description="坐标 (x,y) 从 0 开始，原点左上角，区域两端都包含。. 是透明，? 在行数据里表示保持原样。")
    sub = p.add_subparsers(dest="cmd", metavar="命令", required=True)

    def cmd(name, help_text):
        return sub.add_parser(name, help=help_text, description=help_text)

    def region(sp):
        sp.add_argument("--region", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"), help="只作用于这个区域")

    s = cmd("new", "创建画布")
    s.add_argument("w", type=int)
    s.add_argument("h", type=int)
    s.add_argument("--palette", default="mono", choices=sorted(PRESETS), help="预设调色板（默认 mono）")
    s.add_argument("--force", action="store_true", help="覆盖已有文件（旧内容可 undo 找回）")

    cmd("info", "尺寸、调色板和每色用量、内容范围")
    cmd("edit", "打开可视化编辑器（需要 PyQt6）")

    s = cmd("view", f"带坐标尺显示网格；超过 {render.MAX_VIEW} 行/列自动缩略，看细节用 --region")
    region(s)
    s.add_argument("--spaced", action="store_true", help="格子之间加空格，逐格核对位置时用")
    s.add_argument("--step", type=int, help="缩略倍数：每个字符代表 N×N 格；1 = 强制逐格（默认自动）")
    s.add_argument("--outline", action="store_true", help="只显示材质交界（同一色阶里的颜色算同一种材质）")

    s = cmd("inspect", "构图自检：近似直线的交界、没有过渡带的色带、大块平涂；加 --object 检查物体的接触阴影（≤30 行）")
    s.add_argument("--object", action="append", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"),
                   help="落地物体的范围，检查它底边下方有没有接触阴影；可以写多个")

    s = cmd("palette", "palette | palette set C RRGGBB [C RRGGBB ...] | palette rm C [--replace-with D] | palette preset 名字")
    s.add_argument("action", nargs="?", default="list", choices=["list", "set", "rm", "preset"])
    s.add_argument("args", nargs="*")
    s.add_argument("--replace-with", metavar="D")

    s = cmd("px", "单格：px X Y C [X Y C ...]")
    s.add_argument("items", nargs="+")

    for name, desc in (("line", "直线"), ("rect", "矩形，默认只画边框"), ("ellipse", "内切于矩形的椭圆/圆，默认只画边框")):
        s = cmd(name, f"{desc}：{name} X0 Y0 X1 Y1 C")
        for a in ("x0", "y0", "x1", "y1"):
            s.add_argument(a, type=int)
        s.add_argument("c")
        if name != "line":
            s.add_argument("--fill", action="store_true", help="实心")

    s = cmd("fill", "油漆桶（4 连通）：fill X Y C [--boundary 字符] [--closed]")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.add_argument("c")
    s.add_argument("--boundary", metavar="字符", help="跨过任何颜色扩散，碰到这些字符才停（先画轮廓再填色时用）")
    s.add_argument("--closed", action="store_true", help="区域碰到画布边就报错，用来确认轮廓确实围住了")

    def curve_opts(sp):
        sp.add_argument("--fill", metavar="D", help="从曲线向下（或向上）填充这个颜色")
        sp.add_argument("--dir", choices=["down", "up"], default="down", help="填充方向，默认 down")
        sp.add_argument("--until", metavar="字符", help="填充时碰到这些字符就停")

    s = cmd("curve", "经过这些点的平滑曲线（Catmull-Rom），最后一个参数是颜色：curve X Y X Y [X Y …] C；回显高度剖面")
    s.add_argument("items", nargs="+", metavar="X Y … C")
    curve_opts(s)

    s = cmd("horizon", "生成一条起伏的地平线 / 山脊（本质是 curve）：horizon Y0 Y1 C；回显等价的 curve 命令和高度剖面")
    s.add_argument("y0", type=int)
    s.add_argument("y1", type=int)
    s.add_argument("c")
    s.add_argument("--wave", type=int, help="大起伏的幅度（格），默认画布高度的 6%%")
    s.add_argument("--segments", type=int, help="大起伏的段数，默认每 40 格一段、至少 3 段")
    s.add_argument("--x0", type=int, help="起点 x，默认 0")
    s.add_argument("--x1", type=int, help="终点 x，默认画布最右")
    s.add_argument("--seed", type=int, default=0, help="随机种子，换一个得到不同的起伏")
    curve_opts(s)

    s = cmd("replace", "颜色 A 全部换成 B：replace A B")
    s.add_argument("a")
    s.add_argument("b")
    region(s)

    s = cmd("ramp", "色阶（同一材质从暗到亮）：ramp | ramp set 暗到亮 [暗到亮 ...] | ramp add 暗到亮 | "
                    "ramp rm 暗到亮 | ramp clear | ramp suggest")
    s.add_argument("action", nargs="?", default="list", choices=["list", "set", "add", "rm", "clear", "suggest"])
    s.add_argument("args", nargs="*")

    s = cmd("shade", "按色阶把形状范围内的每一格压暗或提亮（先用 ramp 声明色阶）："
                     "shade 形状 坐标… [形状 坐标…]，形状是 rect X0 Y0 X1 Y1 / ellipse X0 Y0 X1 Y1 / "
                     "poly X Y X Y X Y …；多个形状先合并再处理一次，重叠处不会重复压暗")
    s.add_argument("shapes", nargs="+", metavar="形状 坐标")
    s.add_argument("--steps", type=int, default=-1, help="移动几档：负数变暗（默认 -1），正数变亮")
    s.add_argument("--soft", type=int, default=0, help="边缘渐隐宽度（格）：边缘按有序抖动逐渐变稀")
    s.add_argument("--only", metavar="字符", help="只改这些颜色，比如投影只压暗地面")
    s.add_argument("--except", dest="skip", metavar="字符", help="不改这些颜色")
    s.add_argument("--extend", action="store_true",
                   help="色阶到头时自动在那一端补一个派生颜色（更暗的略偏冷、更亮的略偏暖）")

    s = cmd("stamp", "从 (X,Y) 起贴一块子网格，行可不等长：stamp X Y 行 [行 ...]（不给行就读 stdin / 脚本里读到 end）")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.add_argument("rows", nargs="*")

    s = cmd("rows", "从第 Y 行起整行覆盖，每行必须正好画布宽：rows Y 行 [行 ...]")
    s.add_argument("y", type=int)
    s.add_argument("rows", nargs="*")

    s = cmd("mirror", "一半镜像到另一半：lr 左→右，rl 右→左，tb 上→下，bt 下→上")
    s.add_argument("mode", choices=["lr", "rl", "tb", "bt"])
    region(s)

    s = cmd("flip", "翻转：h 水平，v 垂直")
    s.add_argument("axis", choices=["h", "v"])
    region(s)

    s = cmd("shift", "整体平移：shift DX DY")
    s.add_argument("dx", type=int)
    s.add_argument("dy", type=int)
    s.add_argument("--wrap", action="store_true", help="移出去的从另一边回来")

    s = cmd("resize", "改画布尺寸：resize W H")
    s.add_argument("w", type=int)
    s.add_argument("h", type=int)
    s.add_argument("--anchor", default="tl", choices=list(Canvas.ANCHORS), help="原内容贴在哪（默认 tl 左上）")

    s = cmd("clear", "全部填成某颜色（默认透明）")
    s.add_argument("c", nargs="?", default=core.TRANSPARENT)

    for name in ("undo", "redo"):
        s = cmd(name, "撤销" if name == "undo" else "重做")
        s.add_argument("n", nargs="?", type=int, default=1)

    s = cmd("apply", "批量执行脚本：每行一条命令，# 开头是注释；全部成功才保存")
    s.add_argument("-f", "--file", help="脚本文件，默认读 stdin")

    s = cmd("export", "导出：export png [输出] | export ascii [输出] | export ansi [输出]")
    s.add_argument("format", choices=["png", "ascii", "ansi"])
    s.add_argument("out", nargs="?", help="png 默认同名 .png；ascii/ansi 默认打印")
    s.add_argument("--scale", type=int, default=8, help="png 每格多少像素（默认 8）")
    s.add_argument("--grid", action="store_true", help="png 画网格线")
    s.add_argument("--bg", help="png 透明处填这个颜色")
    s.add_argument("--ramp", action="store_true", help="ascii 按亮度转成 @%%#*+=-:. 字符画")
    return p


def fmt_region(r) -> str:
    return f"({r[0]},{r[1]})-({r[2]},{r[3]})"


def is_mutating(ns) -> bool:
    return (ns.cmd not in READ_ONLY and not (ns.cmd == "palette" and ns.action == "list")
            and not (ns.cmd == "ramp" and ns.action in ("list", "suggest")))


SHAPES = ("rect", "ellipse", "poly")


def shade_cells(tokens: list[str]) -> set[tuple[int, int]]:
    """解析 shade 的形状序列：rect X0 Y0 X1 Y1 / ellipse X0 Y0 X1 Y1 / poly X Y X Y X Y …，可以连着写多个，结果取并集。"""
    if not tokens or tokens[0] not in SHAPES:
        raise PxlError("shade 后面要以形状名开头：rect / ellipse / poly")
    shapes: list[tuple[str, list[int]]] = []
    for t in tokens:
        if t in SHAPES:
            shapes.append((t, []))
            continue
        try:
            shapes[-1][1].append(int(t))
        except ValueError:
            raise PxlError(f"shade 参数 {t!r} 既不是形状名（rect / ellipse / poly），也不是整数") from None
    cells: set[tuple[int, int]] = set()
    for kind, pts in shapes:
        if kind in ("rect", "ellipse"):
            if len(pts) != 4:
                raise PxlError(f"shade {kind} 要 4 个数：X0 Y0 X1 Y1（实际给了 {len(pts)} 个）")
            x0, y0, x1, y1 = pts
            xa, xb, ya, yb = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            cells |= core.rect_cells(xa, ya, xb, yb) if kind == "rect" else core.ellipse_cells(xa, ya, xb, yb, True)
        else:
            if len(pts) < 6 or len(pts) % 2:
                raise PxlError("shade poly 要 3 个以上顶点，按 X Y 成对给出")
            cells |= core.polygon_cells(list(zip(pts[::2], pts[1::2])))
    return cells


def execute(cv: Canvas | None, ns, path: Path, read_rows) -> tuple[Canvas, str | None]:
    """执行一条命令（原地修改 cv），返回 (画布, 输出文本)。只读命令返回文本，修改命令返回 None。"""
    c = ns.cmd
    if c == "new":
        return Canvas(ns.w, ns.h, PRESETS[ns.palette]), None
    if cv is None:
        raise PxlError(f"{path} 还不存在；先 new 宽 高")
    if c == "info":
        return cv, render.info(cv, path)
    if c == "view":
        return cv, view_text(cv, tuple(ns.region) if ns.region else None, ns.spaced, ns.step, ns.outline)
    if c == "inspect":
        from . import lint
        return cv, lint.report(cv, path.name, [tuple(o) for o in ns.object or []])[0]
    if c == "export":
        return cv, export(cv, ns, path)
    if c == "ramp" and ns.action in ("list", "suggest"):
        return cv, render.ramps_text(cv) if ns.action == "list" else render.ramp_suggest_text(cv)
    if c == "palette":
        args = ns.args
        if ns.action == "list":
            return cv, render.info(cv, path)
        if ns.action == "set":
            if not args or len(args) % 2:
                raise PxlError("palette set 参数按 C RRGGBB 两个一组")
            for ch, color in zip(args[::2], args[1::2]):
                cv.set_color(ch, color)
        elif ns.action == "rm":
            for ch in args:
                cv.remove_color(ch, ns.replace_with)
        elif ns.action == "preset":
            if len(args) != 1 or args[0] not in PRESETS:
                raise PxlError(f"palette preset 名字：{' / '.join(PRESETS)}")
            for ch, color in PRESETS[args[0]].items():
                cv.set_color(ch, color)
    elif c == "ramp":
        if ns.action in ("set", "add", "rm") and not ns.args:
            raise PxlError(f"ramp {ns.action} 后面要跟色阶，比如 ramp {ns.action} JjHh")
        if ns.action == "set":
            cv.set_ramps(ns.args)
        elif ns.action == "add":
            cv.set_ramps(cv.ramps + ns.args)
        elif ns.action == "rm":
            missing = [r for r in ns.args if r not in cv.ramps]
            if missing:
                raise PxlError(f"没有这些色阶：{' '.join(missing)}（现有：{' '.join(cv.ramps) or '无'}）")
            cv.set_ramps([r for r in cv.ramps if r not in ns.args])
        else:
            cv.ramps = []
    elif c == "shade":
        cells = shade_cells(ns.shapes)
        if not any(0 <= x < cv.w and 0 <= y < cv.h for x, y in cells):
            raise PxlError("shade 的形状完全在画布外面")
        ns.note = render.shade_note(cv.shade(cells, ns.steps, ns.soft, ns.only, ns.skip, ns.extend), ns.steps)
    elif c == "px":
        if len(ns.items) % 3:
            raise PxlError("px 参数按 X Y C 三个一组")
        points = []
        for i in range(0, len(ns.items), 3):
            x, y, ch = ns.items[i:i + 3]
            try:
                points.append((int(x), int(y), ch))
            except ValueError:
                raise PxlError(f"px 第 {i // 3} 组 {x} {y} {ch}：X Y 要是整数") from None
        cv.px(points)
    elif c == "line":
        cv.line(ns.x0, ns.y0, ns.x1, ns.y1, ns.c)
    elif c in ("rect", "ellipse"):
        getattr(cv, c)(ns.x0, ns.y0, ns.x1, ns.y1, ns.c, ns.fill)
    elif c == "fill":
        ns.note = render.fill_note(cv.fill(ns.x, ns.y, ns.c, ns.boundary, ns.closed), cv)
    elif c in ("curve", "horizon"):
        if c == "curve":
            *nums, color = ns.items
            if len(nums) < 4 or len(nums) % 2:
                raise PxlError("curve 要 2 个以上的点（X Y 成对），最后一个参数是颜色：curve X Y X Y … C")
            try:
                vals = [int(v) for v in nums]
            except ValueError:
                raise PxlError(f"curve 的坐标要是整数，最后一个参数才是颜色：{' '.join(ns.items)}") from None
            points, prefix = list(zip(vals[::2], vals[1::2])), ""
        else:
            x0 = 0 if ns.x0 is None else ns.x0
            x1 = cv.w - 1 if ns.x1 is None else ns.x1
            cv.need_point(x0, 0, "--x0")
            cv.need_point(x1, 0, "--x1")
            if x1 - x0 < 8:
                raise PxlError("horizon 的 x 范围至少要 9 格（--x0 < --x1）")
            wave = max(1, round(cv.h * 0.06)) if ns.wave is None else ns.wave
            segments = max(3, round((x1 - x0) / 40)) if ns.segments is None else ns.segments
            if wave < 0 or segments < 1:
                raise PxlError("--wave 不能是负数，--segments 至少是 1")
            points = core.horizon_points(cv.w, cv.h, ns.y0, ns.y1, wave, segments, x0, x1, ns.seed)
            color = ns.c
            prefix = "等价于 curve " + " ".join(f"{x} {y}" for x, y in points) + f" {color}；"
        st = cv.curve(points, color, ns.fill, ns.dir, ns.until)
        ns.note = prefix + render.curve_note(st, cv, ns.fill, ns.dir, ns.until)
    elif c == "replace":
        cv.replace(ns.a, ns.b, tuple(ns.region) if ns.region else None)
    elif c == "stamp":
        cv.stamp(ns.x, ns.y, ns.rows or read_rows())
    elif c == "rows":
        cv.set_rows(ns.y, ns.rows or read_rows())
    elif c in ("mirror", "flip"):
        getattr(cv, c)(ns.mode if c == "mirror" else ns.axis, tuple(ns.region) if ns.region else None)
    elif c == "shift":
        cv.shift(ns.dx, ns.dy, ns.wrap)
    elif c == "resize":
        cv.resize(ns.w, ns.h, ns.anchor)
    elif c == "clear":
        cv.clear(ns.c)
    else:
        raise PxlError(f"这里不能用 {c}")
    return cv, None


def change_summary(before: Canvas | None, after: Canvas) -> tuple[str, core.Region | None]:
    if before is None:
        return f"新画布 {after.w}×{after.h}", None
    d = core.diff(before, after)
    if d is None:
        return f"尺寸 {before.w}×{before.h} → {after.w}×{after.h}", None
    n, bbox = d
    parts = [f"改动 {n} 格" + (f" {fmt_region(bbox)}" if bbox else "")]
    if before.palette != after.palette:
        parts.append("调色板已更新")
    return "，".join(parts), bbox


def export(cv: Canvas, ns, path: Path) -> str:
    if ns.format == "png":
        out = Path(ns.out) if ns.out else path.with_suffix(".png")
        data = render.png_bytes(cv, ns.scale, ns.grid, ns.bg)
        core.atomic_write(out, data)
        extra = cv.w * ns.scale + (cv.w + 1 if ns.grid else 0), cv.h * ns.scale + (cv.h + 1 if ns.grid else 0)
        return f"已导出 {out}（{extra[0]}×{extra[1]} px，每格 {ns.scale}px）"
    text = render.ascii_art(cv, ns.ramp) if ns.format == "ascii" else render.ansi(cv)
    if not ns.out:
        return text
    core.atomic_write(Path(ns.out), text + "\n")
    return f"已导出 {ns.out}"


def view_text(cv: Canvas, region=None, spaced: bool = False, step: int | None = None, outline: bool = False) -> str:
    """step=None 时自动：区域超过 MAX_VIEW 行/列就缩略，保证输出不会爆掉。outline 时只显示材质交界。"""
    r = cv.need_region(region)
    if step is None:
        step = render.auto_step(r[2] - r[0] + 1, r[3] - r[1] + 1)
    if step < 1:
        raise PxlError("--step 至少是 1")
    if not outline:
        return render.view(cv, r, spaced, step)
    from . import lint
    lines = render.view(lint.outline_canvas(cv), r, spaced, step).splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*\d+  )(.*)$", line)
        if m:
            lines[i] = (m.group(1) + m.group(2).replace(TRANSPARENT, " ")).rstrip()
    note = "（只显示材质交界；同一色阶里的颜色算同一种材质" + ("" if cv.ramps else "，没声明色阶时每种颜色各算一种") + "）"
    return note + "\n" + "\n".join(lines)


def feedback(cv: Canvas, bbox) -> str:
    """改动后的视图：放得下就整张显示；大画布只显示改动附近；改动范围本身太大就缩略。"""
    lines = [render.palette_line(cv)]
    if max(cv.w, cv.h) <= render.MAX_VIEW or bbox is None:
        lines.append(view_text(cv))
    else:
        m = FEEDBACK_MARGIN
        r = (max(0, bbox[0] - m), max(0, bbox[1] - m), min(cv.w - 1, bbox[2] + m), min(cv.h - 1, bbox[3] + m))
        lines.append(f"（画布 {cv.w}×{cv.h}，只显示改动附近 {fmt_region(r)}；看全图用 view）")
        lines.append(view_text(cv, r))
    return "\n".join(lines)


def run_script(cv: Canvas | None, text: str, path: Path, parser: Parser) -> tuple[Canvas, list[str]]:
    lines = text.splitlines()
    log: list[str] = []
    i = 0
    while i < len(lines):
        lineno, raw = i + 1, lines[i].strip()
        i += 1
        if not raw or raw.startswith("#"):
            continue
        try:
            ns = parser.parse_args(shlex.split(raw))
            if ns.cmd in ("apply", "undo", "redo", "edit"):
                raise PxlError(f"脚本里不能用 {ns.cmd}")
            if ns.cmd == "new" and path.exists() and not ns.force:
                raise PxlError(f"{path} 已存在；要覆盖写 new ... --force")
            if ns.cmd in ("stamp", "rows") and not ns.rows:
                block = []
                while True:
                    if i >= len(lines):
                        raise PxlError(f"{ns.cmd} 的行数据没有以单独一行 end 结束")
                    row = lines[i].strip()
                    i += 1
                    if row == "end":
                        break
                    if row:
                        block.append(row)
                ns.rows = block
            before = cv.copy() if cv is not None and is_mutating(ns) else None
            cv, out = execute(cv, ns, path, read_rows=lambda: [])
            if out is not None:
                log.append(f"{lineno:>3}  {ns.cmd}\n{out}")
            else:
                note = getattr(ns, "note", None)
                log.append(f"{lineno:>3}  {ns.cmd}: {change_summary(before, cv)[0]}" + (f"；{note}" if note else ""))
        except (PxlError, ValueError) as e:
            raise PxlError(f"第 {lineno} 行 `{raw}`：{e}\n脚本已中止，{path} 没有被修改") from None
    if cv is None:
        raise PxlError("脚本里没有任何命令")
    return cv, log


def read_stdin_rows() -> list[str]:
    if sys.stdin.isatty():
        raise PxlError("没有行数据：把行作为参数给，或从 stdin 输入")
    return [r.strip() for r in sys.stdin.read().splitlines() if r.strip()]


def launch_editor(path: Path | None) -> int:
    try:
        from .editor import run_editor
    except ImportError as e:
        raise PxlError(f"编辑器需要 PyQt6（Debian: sudo apt install python3-pyqt6）：{e}") from None
    return run_editor(path)


def dispatch(path: Path, rest: list[str], quiet: bool, no_lint: bool = False) -> int:
    parser = build_parser()
    ns = parser.parse_args(rest)
    history = core.History(path)
    exists = path.exists()

    if ns.cmd == "edit":
        return launch_editor(path)

    if ns.cmd in ("undo", "redo"):
        cv, done = history.step(core.load(path), ns.n, ns.cmd)
        if not done:
            print(f"没有可{'撤销' if ns.cmd == 'undo' else '重做'}的步骤")
            return 0
        core.save(cv, path)
        print(f"{ns.cmd} {done} 步")
        if not quiet:
            print(feedback(cv, None))
        return 0

    before = core.load(path) if exists else None

    if ns.cmd == "apply":
        text = Path(ns.file).read_text(encoding="utf-8") if ns.file else sys.stdin.read()
        cv, log = run_script(before.copy() if before else None, text, path, parser)
        print("\n".join(log))
    else:
        if ns.cmd == "new" and exists and not ns.force:
            raise PxlError(f"{path} 已存在；要覆盖加 --force（旧内容可 undo 找回）")
        cv, out = execute(before.copy() if before else None, ns, path, read_stdin_rows)
        if not is_mutating(ns):
            print(out)
            return 0

    summary, bbox = change_summary(before, cv)
    note = getattr(ns, "note", None)
    if before is not None and before.same(cv):
        print(f"没有变化：{summary}" + (f"\n  {note}" if note else ""))
        return 0
    if ns.cmd == "new" and before is None:
        history.reset()
    elif before is not None:
        history.push(before)
    core.save(cv, path)
    print(f"{ns.cmd}: {summary}")
    if note:
        print(f"  {note}")
    if ns.cmd == "apply" and not no_lint:
        from . import lint
        lint_line = lint.summary(cv, path.name)
        if lint_line:
            print(lint_line)
    if not quiet:
        print(feedback(cv, bbox))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = any(a in ("-q", "--quiet") for a in argv)
    no_lint = "--no-lint" in argv
    argv = [a for a in argv if a not in ("-q", "--quiet", "--no-lint")]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("用法: pxl [-q] 文件.pxl 命令 [参数]    （-q 修改后不打印视图）")
        print("      pxl ref [库] [条目 | --find 关键词 | --list]    查参考库（比如抖动），不需要画布文件\n")
        print(build_parser().format_help())
        return 0
    if argv[0] == "ref" and not Path("ref").exists():
        from . import refs
        try:
            print(refs.main(argv[1:]))
            return 0
        except PxlError as e:
            print(f"错误: {e}", file=sys.stderr)
            return 1
    if argv[0] == "edit" and len(argv) <= 2 and not Path("edit").exists():
        try:
            return launch_editor(Path(argv[1]) if len(argv) == 2 else None)
        except PxlError as e:
            print(f"错误: {e}", file=sys.stderr)
            return 1
    path, rest = Path(argv[0]), argv[1:] or ["view"]
    commands = {"new", "info", "view", "edit", "inspect", "palette", "ramp", "shade", "curve", "horizon", "px", "line", "rect",
                "ellipse", "fill", "replace",
                "stamp", "rows", "mirror", "flip", "shift", "resize", "clear", "undo", "redo", "apply", "export"}
    if argv[0] in commands and not path.exists():
        print(f"错误: 文件要写在命令前面，比如 pxl 画.pxl {argv[0]} ...", file=sys.stderr)
        return 2
    try:
        return dispatch(path, rest, quiet, no_lint)
    except PxlError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
