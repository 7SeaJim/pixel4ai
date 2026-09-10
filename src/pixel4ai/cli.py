"""pxl 命令行。

    pxl [-q] 文件.pxl 命令 [参数]

每次修改都会原子写回文件，并打印「改了几格、在哪」和带坐标尺的视图，AI 靠它核对。
批量：pxl 文件.pxl apply <<'EOF' ... EOF，每行一条命令，全部成功才保存。
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import core, render
from .core import PRESETS, Canvas, PxlError

FEEDBACK_MARGIN = 2         # 大画布改动后，显示改动范围外扩几格
READ_ONLY = {"info", "view", "export", "edit"}


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

    s = cmd("fill", "油漆桶（4 连通）：fill X Y C")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.add_argument("c")

    s = cmd("replace", "颜色 A 全部换成 B：replace A B")
    s.add_argument("a")
    s.add_argument("b")
    region(s)

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
    return ns.cmd not in READ_ONLY and not (ns.cmd == "palette" and ns.action == "list")


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
        return cv, view_text(cv, tuple(ns.region) if ns.region else None, ns.spaced, ns.step)
    if c == "export":
        return cv, export(cv, ns, path)
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
        cv.fill(ns.x, ns.y, ns.c)
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


def view_text(cv: Canvas, region=None, spaced: bool = False, step: int | None = None) -> str:
    """step=None 时自动：区域超过 MAX_VIEW 行/列就缩略，保证输出不会爆掉。"""
    r = cv.need_region(region)
    if step is None:
        step = render.auto_step(r[2] - r[0] + 1, r[3] - r[1] + 1)
    if step < 1:
        raise PxlError("--step 至少是 1")
    return render.view(cv, r, spaced, step)


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
                log.append(f"{lineno:>3}  {ns.cmd}: {change_summary(before, cv)[0]}")
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


def dispatch(path: Path, rest: list[str], quiet: bool) -> int:
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
    if before is not None and before.same(cv):
        print(f"没有变化：{summary}")
        return 0
    if ns.cmd == "new" and before is None:
        history.reset()
    elif before is not None:
        history.push(before)
    core.save(cv, path)
    print(f"{ns.cmd}: {summary}")
    if not quiet:
        print(feedback(cv, bbox))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = any(a in ("-q", "--quiet") for a in argv)
    argv = [a for a in argv if a not in ("-q", "--quiet")]
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
    commands = {"new", "info", "view", "edit", "palette", "px", "line", "rect", "ellipse", "fill", "replace",
                "stamp", "rows", "mirror", "flip", "shift", "resize", "clear", "undo", "redo", "apply", "export"}
    if argv[0] in commands and not path.exists():
        print(f"错误: 文件要写在命令前面，比如 pxl 画.pxl {argv[0]} ...", file=sys.stderr)
        return 2
    try:
        return dispatch(path, rest, quiet)
    except PxlError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
