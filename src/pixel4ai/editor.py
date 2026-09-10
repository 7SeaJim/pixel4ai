"""pixel4ai 可视化编辑器（PyQt6）。

看着渲染效果直接改格子，不用碰字符。AI 用 pxl 命令改同一个文件时，这里自动刷新。
每一笔松开鼠标就保存（先读磁盘最新内容再叠加这一笔，不会覆盖 AI 刚做的改动），撤销栈和命令行共用。

    pxl 画.pxl edit               （或 pxl edit 画.pxl）
    pxl edit                      不给文件：弹窗选择打开或新建
    pxl-edit [画.pxl]
    python3 src/pixel4ai/editor.py [画.pxl]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if not __package__:
    # 直接 python3 editor.py 运行时没有包上下文，把 src/ 加进导入路径
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QPointF, QRect, Qt, QTimer
from PyQt6.QtGui import (QAction, QActionGroup, QBrush, QColor, QFont, QGuiApplication, QImage, QKeySequence,
                         QPainter, QPen, QPixmap, QTransform)
from PyQt6.QtWidgets import (QApplication, QColorDialog, QFileDialog, QGridLayout, QInputDialog, QLabel,
                             QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QToolBar, QVBoxLayout,
                             QWidget)

from pixel4ai import core, render
from pixel4ai.core import TRANSPARENT, Canvas, PxlError, ellipse_cells, line_cells

RULER = 30
ZOOMS = [1, 2, 3, 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 64]
PREVIEW_SIDE = 176
TOOLS = [("pencil", "铅笔", "B"), ("eraser", "橡皮", "E"), ("fill", "油漆桶", "G"), ("pick", "吸管", "I"),
         ("line", "直线", "L"), ("rect", "矩形", "R"), ("ellipse", "椭圆", "O"), ("select", "选区", "S")]
NEW_COLOR_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
POLL_MS = 300
PXL_FILTER = "像素画 (*.pxl);;所有文件 (*)"
HINT = ("左键画 · 右键擦\nAlt+左键 吸色\n矩形/椭圆按住 Shift 实心\nCtrl+滚轮 缩放 · Ctrl+0 适应窗口\n0-9 选调色板\n"
        "选区会复制坐标，贴给 AI\nDelete 清空选区")

_checker: dict[int, QBrush] = {}


def checker(size: int = 8) -> QBrush:
    if size not in _checker:
        pm = QPixmap(size * 2, size * 2)
        pm.fill(QColor(238, 238, 238))
        p = QPainter(pm)
        p.fillRect(0, 0, size, size, QColor(208, 208, 208))
        p.fillRect(size, size, size, size, QColor(208, 208, 208))
        p.end()
        _checker[size] = QBrush(pm)
    return _checker[size]


def checker_cell_size(z: int) -> int:
    """画布透明处棋盘格的边长，必须和格子边长互相整除才对得齐：
    大格子切成 2×2、4×4…（每块不超过 16px），小格子就几个格子拼成一块（至少 4px）。"""
    if z >= 8 and z % 2 == 0:
        s = z // 2
        while s > 16 and s % 2 == 0:
            s //= 2
        return s
    return z * -(-4 // z)


def cell_checker(z: int) -> QBrush:
    """画布用的棋盘格：从画布左上角开始平铺（纹理画刷默认从控件左上角铺，会被坐标尺的宽度错开）。"""
    brush = QBrush(checker(checker_cell_size(z)))
    brush.setTransform(QTransform.fromTranslate(RULER, RULER))
    return brush


def canvas_image(cv: Canvas) -> QImage:
    """1 格 = 1 像素的整张图。"""
    colors = {ch: bytes((*core.hex_to_rgb(c), 255)) if c else bytes(4) for ch, c in cv.palette.items()}
    get = colors.__getitem__
    data = b"".join(b"".join(map(get, row)) for row in cv.grid)
    return QImage(data, cv.w, cv.h, cv.w * 4, QImage.Format.Format_RGBA8888).copy()


def text_color_on(color: str | None) -> QColor:
    if color is None:
        return QColor(60, 60, 60)
    r, g, b = core.hex_to_rgb(color)
    return QColor(20, 20, 20) if 0.299 * r + 0.587 * g + 0.114 * b > 140 else QColor(245, 245, 245)


class CanvasView(QWidget):
    def __init__(self, editor: "Editor"):
        super().__init__()
        self.ed = editor
        self.zoom = 24
        self.show_grid = True
        self.hover: tuple[int, int] | None = None
        self.button: Qt.MouseButton | None = None
        self.mode = ""
        self.start = self.last = None
        self.stroke_char = TRANSPARENT
        self.pending: dict[tuple[int, int], str] = {}
        self._image: QImage | None = None
        self._image_src: Canvas | None = None
        self.setMouseTracking(True)

    def refit(self):
        cv = self.ed.cv
        self.setFixedSize(RULER + cv.w * self.zoom + 1, RULER + cv.h * self.zoom + 1)
        self.update()

    def cell_at(self, pos: QPointF, clamp: bool = False) -> tuple[int, int] | None:
        cv = self.ed.cv
        if not clamp:
            vis = self.visible()
            if pos.x() < vis.left() + RULER or pos.y() < vis.top() + RULER:
                return None     # 落在吸附在可见区域边上的坐标尺上，下面的格子被挡住了
        x, y = int((pos.x() - RULER) // self.zoom), int((pos.y() - RULER) // self.zoom)
        if clamp:
            return min(max(x, 0), cv.w - 1), min(max(y, 0), cv.h - 1)
        return (x, y) if 0 <= x < cv.w and 0 <= y < cv.h else None

    def cell_rect(self, x: int, y: int) -> QRect:
        return QRect(RULER + x * self.zoom, RULER + y * self.zoom, self.zoom, self.zoom)

    # ───────── 绘制 ─────────

    def image(self) -> QImage:
        """缓存的整张图。每次修改画布都会换成新的 Canvas 对象，对象变了才重建。"""
        cv = self.ed.cv
        if self._image_src is not cv:
            self._image, self._image_src = canvas_image(cv), cv
        return self._image

    def visible(self) -> QRect:
        vis = self.visibleRegion().boundingRect()
        return vis if not vis.isEmpty() else self.rect()

    def paintEvent(self, e):
        cv, z = self.ed.cv, self.zoom
        p = QPainter(self)
        exposed = e.rect()
        p.fillRect(exposed, QColor(250, 249, 246))

        # 只画露出来的格子：512 格放大 8 倍就是 4096px，整张画很浪费
        cx0, cy0 = max(0, (exposed.left() - RULER) // z), max(0, (exposed.top() - RULER) // z)
        cx1, cy1 = min(cv.w - 1, (exposed.right() - RULER) // z), min(cv.h - 1, (exposed.bottom() - RULER) // z)
        if cx0 <= cx1 and cy0 <= cy1:
            src = QRect(cx0, cy0, cx1 - cx0 + 1, cy1 - cy0 + 1)
            dst = QRect(RULER + cx0 * z, RULER + cy0 * z, src.width() * z, src.height() * z)
            p.fillRect(dst, cell_checker(z))
            if self.pending:
                p.drawImage(dst, self.image_with_pending(src))
            else:
                p.drawImage(dst, self.image(), src)
            if self.show_grid and z >= 6:
                minor, major = QPen(QColor(0, 0, 0, 28)), QPen(QColor(0, 0, 0, 80))
                for x in range(cx0, cx1 + 2):
                    p.setPen(major if x % 5 == 0 or x == cv.w else minor)
                    p.drawLine(RULER + x * z, dst.top(), RULER + x * z, dst.top() + dst.height())
                for y in range(cy0, cy1 + 2):
                    p.setPen(major if y % 5 == 0 or y == cv.h else minor)
                    p.drawLine(dst.left(), RULER + y * z, dst.left() + dst.width(), RULER + y * z)

        sel = self.ed.selection
        if sel:
            x0, y0, x1, y1 = sel
            r = QRect(RULER + x0 * z, RULER + y0 * z, (x1 - x0 + 1) * z, (y1 - y0 + 1) * z)
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.drawRect(r)
            p.setPen(QPen(QColor(20, 110, 255), 2, Qt.PenStyle.DashLine))
            p.drawRect(r)
        if self.hover:
            r = self.cell_rect(*self.hover)
            p.setPen(QPen(QColor(0, 0, 0), 1))
            p.drawRect(r.adjusted(0, 0, 0, 0))
            p.setPen(QPen(QColor(255, 255, 255), 1))
            p.drawRect(r.adjusted(1, 1, -1, -1))
        self.paint_rulers(p, self.visible())
        p.end()

    def image_with_pending(self, src: QRect) -> QImage:
        """可见区域的图，叠上还没提交的这一笔（画笔轨迹 / 形状预览）。"""
        img = self.image().copy(src)
        colors = {ch: QColor(c).rgba() if c else 0 for ch, c in self.ed.cv.palette.items()}
        for (x, y), ch in self.pending.items():
            if src.contains(x, y):
                img.setPixel(x - src.left(), y - src.top(), colors.get(ch, 0))
        return img

    def paint_rulers(self, p: QPainter, vis: QRect):
        """坐标尺贴在可见区域的上边和左边，滚动时跟着走。"""
        cv, z = self.ed.cv, self.zoom
        band = QColor(250, 249, 246)
        top, left = vis.top(), vis.left()
        p.fillRect(QRect(left, top, vis.width(), RULER - 2), band)
        p.fillRect(QRect(left, top, RULER - 3, vis.height()), band)
        font = QFont()
        font.setPixelSize(10)
        p.setFont(font)
        label_px = 7 * len(str(max(cv.w, cv.h) - 1)) + 6
        step = next((s for s in (1, 2, 5, 10, 20, 50, 100, 200, 500) if s * z >= label_px), 1000)
        hx, hy = self.hover if self.hover else (-1, -1)
        highlight, dim, dark = QColor(255, 214, 102), QColor(120, 120, 120), QColor(30, 30, 30)
        cx0, cx1 = max(0, (vis.left() - RULER) // z), min(cv.w - 1, (vis.right() - RULER) // z)
        cy0, cy1 = max(0, (vis.top() - RULER) // z), min(cv.h - 1, (vis.bottom() - RULER) // z)
        for x in range(cx0, cx1 + 1):
            if x == hx:
                p.fillRect(QRect(RULER + x * z, top, z, RULER - 2), highlight)
            if x % step == 0 or x == hx:
                p.setPen(dark if x == hx else dim)
                p.drawText(QRect(RULER + x * z + z // 2 - 30, top, 60, RULER - 2), Qt.AlignmentFlag.AlignCenter, str(x))
        for y in range(cy0, cy1 + 1):
            if y == hy:
                p.fillRect(QRect(left, RULER + y * z, RULER - 3, z), highlight)
            if y % step == 0 or y == hy:
                p.setPen(dark if y == hy else dim)
                p.drawText(QRect(left, RULER + y * z + z // 2 - 10, RULER - 5, 20),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(y))
        p.fillRect(QRect(left, top, RULER - 3, RULER - 2), band)

    # ───────── 鼠标 ─────────

    def mousePressEvent(self, e):
        cell = self.cell_at(e.position())
        btn, mods = e.button(), e.modifiers()
        if cell is None or btn not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return
        tool = self.ed.tool
        if btn == Qt.MouseButton.LeftButton and (tool == "pick" or mods & Qt.KeyboardModifier.AltModifier):
            self.ed.pick(cell)
            return
        erase = btn == Qt.MouseButton.RightButton or tool == "eraser"
        self.stroke_char = TRANSPARENT if erase else self.ed.color
        if tool == "fill" and not erase:
            self.ed.flood(cell, self.stroke_char)
            return
        self.button, self.start, self.last = btn, cell, cell
        if tool == "select" and not erase:
            self.mode = "select"
            self.ed.set_selection((*cell, *cell))
        elif tool in ("line", "rect", "ellipse") and not erase:
            self.mode = "shape"
            self.shape_preview(cell, mods)
        else:
            self.mode = "free"
            self.add_cells([cell])
        self.update()

    def mouseMoveEvent(self, e):
        raw = self.cell_at(e.position())
        if raw != self.hover:
            self.hover = raw
            self.ed.show_hover(raw)
        if self.button is not None:
            if self.mode == "free":
                if raw is not None:
                    self.add_cells(line_cells(*(self.last or raw), *raw))
                self.last = raw
            else:
                cell = self.cell_at(e.position(), clamp=True)
                if self.mode == "shape":
                    self.shape_preview(cell, e.modifiers())
                elif self.mode == "select":
                    self.ed.set_selection((*self.start, *cell))
        self.update()

    def mouseReleaseEvent(self, e):
        if self.button is None or e.button() != self.button:
            return
        self.button = None
        if self.mode == "select":
            self.ed.selection_done()
        elif self.pending:
            ops = [(x, y, ch) for (x, y), ch in self.pending.items()]
            self.ed.commit(ops)
        self.pending = {}
        self.update()

    def leaveEvent(self, _):
        self.hover = None
        self.ed.show_hover(None)
        self.update()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.ed.zoom_step(1 if e.angleDelta().y() > 0 else -1)
            e.accept()
        else:
            e.ignore()

    def add_cells(self, cells):
        for c in self.ed.with_mirror(cells):
            self.pending[c] = self.stroke_char

    def shape_preview(self, cell, mods):
        (x0, y0), (x1, y1) = self.start, cell
        filled = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        tool = self.ed.tool
        if tool == "line":
            cells = line_cells(x0, y0, x1, y1)
        else:
            xa, xb, ya, yb = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            if tool == "ellipse":
                cells = ellipse_cells(xa, ya, xb, yb, filled)
            else:
                cells = [(x, y) for y in range(ya, yb + 1) for x in range(xa, xb + 1)
                         if filled or x in (xa, xb) or y in (ya, yb)]
        self.pending = {}
        self.add_cells(cells)


class Swatch(QWidget):
    SIZE = 38

    def __init__(self, editor: "Editor", ch: str):
        super().__init__()
        self.ed, self.ch = editor, ch
        self.setFixedSize(self.SIZE, self.SIZE)
        color = editor.cv.palette[ch]
        self.setToolTip(f"{ch}  {color or '透明'}\n双击改颜色 · 右键菜单")

    def paintEvent(self, _):
        color = self.ed.cv.palette.get(self.ch)
        p = QPainter(self)
        inner = self.rect().adjusted(3, 3, -3, -3)
        p.fillRect(inner, checker(5) if color is None else QColor(color))
        selected = self.ed.color == self.ch
        p.setPen(QPen(QColor(20, 110, 255) if selected else QColor(0, 0, 0, 60), 3 if selected else 1))
        p.drawRect(self.rect().adjusted(1, 1, -2, -2) if selected else inner)
        font = QFont()
        font.setPixelSize(12)
        font.setBold(True)
        p.setFont(font)
        p.setPen(text_color_on(color))
        p.drawText(inner.adjusted(0, 0, -3, -1), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, self.ch)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.ed.select_color(self.ch)
        elif e.button() == Qt.MouseButton.RightButton and self.ch != TRANSPARENT:
            menu = QMenu(self)
            menu.addAction("改颜色…", lambda: self.ed.edit_color(self.ch))
            menu.addAction("删除（用到的格子变透明）", lambda: self.ed.remove_color(self.ch))
            menu.exec(e.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, _):
        self.ed.edit_color(self.ch)


class Editor(QMainWindow):
    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        self.history = core.History(self.path)
        self.cv = core.load(self.path)
        self.disk_bytes = self.read_disk()
        self._last_flash = ""
        self.tool = "pencil"
        self.mirror = False
        self.selection: tuple[int, int, int, int] | None = None
        self.color = next((ch for ch in self.cv.palette if ch != TRANSPARENT), TRANSPARENT)
        self._palette_sig = None

        self.view = CanvasView(self)
        self.view.zoom = fit_zoom(self.cv)
        scroll = QScrollArea()
        scroll.setWidget(self.view)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setStyleSheet("QScrollArea { background: #faf9f6; border: none; }")
        # 坐标尺吸附在可见区域边上，滚动后要整块重画
        scroll.horizontalScrollBar().valueChanged.connect(lambda _: self.view.update())
        scroll.verticalScrollBar().valueChanged.connect(lambda _: self.view.update())
        self.scroll = scroll
        self.setCentralWidget(scroll)

        self.build_tools()
        self.build_side()
        self.hover_label = QLabel()
        self.statusBar().addPermanentWidget(self.hover_label)

        self.set_canvas(self.cv)
        self.resize(1080, 800)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_disk)
        self.timer.start(POLL_MS)

    # ───────── 界面 ─────────

    def build_tools(self):
        tb = QToolBar("工具")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, tb)
        group = QActionGroup(self)
        self.tool_actions = {}
        for key, name, shortcut in TOOLS:
            act = QAction(f"{name}  {shortcut}", self, checkable=True)
            act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(lambda _=False, k=key: self.set_tool(k))
            group.addAction(act)
            tb.addAction(act)
            self.tool_actions[key] = act
        self.tool_actions["pencil"].setChecked(True)
        tb.addSeparator()

        def action(text, shortcut, fn, checkable=False, checked=False):
            act = QAction(text, self, checkable=checkable)
            if shortcut:
                act.setShortcuts([QKeySequence(s) for s in shortcut.split("|")])
            act.setChecked(checked)
            act.triggered.connect(fn)
            tb.addAction(act)
            return act

        action("左右对称  M", "M", lambda on: setattr(self, "mirror", on), checkable=True)
        action("网格  #", "#", self.toggle_grid, checkable=True, checked=True)
        action("放大  +", "+|=", lambda: self.zoom_step(1))
        action("缩小  -", "-", lambda: self.zoom_step(-1))
        action("适应窗口", "Ctrl+0", lambda: self.zoom_fit())
        tb.addSeparator()
        action("撤销", "Ctrl+Z", lambda: self.history_step("undo"))
        action("重做", "Ctrl+Shift+Z|Ctrl+Y", lambda: self.history_step("redo"))
        tb.addSeparator()
        action("新建…", "Ctrl+N", lambda: self.new_file())
        action("打开…", "Ctrl+O", lambda: self.open_file())
        action("导出 PNG…", "Ctrl+E", lambda: self.export_png())

    def build_side(self):
        side = QWidget()
        side.setFixedWidth(200)
        lay = QVBoxLayout(side)
        self.current_label = QLabel()
        self.current_label.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.current_label)
        self.palette_box = QWidget()
        self.palette_grid = QGridLayout(self.palette_box)
        self.palette_grid.setSpacing(2)
        self.palette_grid.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.palette_box)
        add = QPushButton("+ 新颜色")
        add.clicked.connect(self.add_color)
        lay.addWidget(add)
        lay.addSpacing(10)
        self.preview_caption = QLabel()
        lay.addWidget(self.preview_caption)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.preview)
        lay.addSpacing(10)
        hint = QLabel(HINT)
        hint.setStyleSheet("color: #777;")
        lay.addWidget(hint)
        lay.addStretch(1)
        tb = QToolBar("调色板")
        tb.setMovable(False)
        tb.addWidget(side)
        self.addToolBar(Qt.ToolBarArea.RightToolBarArea, tb)

    def rebuild_palette(self):
        while self.palette_grid.count():
            self.palette_grid.takeAt(0).widget().deleteLater()
        for i, ch in enumerate(self.cv.palette):
            self.palette_grid.addWidget(Swatch(self, ch), i // 4, i % 4)

    def refresh_side(self):
        color = self.cv.palette.get(self.color)
        n = self.cv.count(self.color)
        self.current_label.setText(f"<b>当前 {self.color}</b> &nbsp; {color or '透明'} &nbsp; <span style='color:#888'>{n} 格</span>")
        for i in range(self.palette_grid.count()):
            self.palette_grid.itemAt(i).widget().update()
        cv = self.cv
        img = self.view.image()
        if max(cv.w, cv.h) <= PREVIEW_SIDE:
            s = max(1, min(8, PREVIEW_SIDE // max(cv.w, cv.h)))
            img = img.scaled(cv.w * s, cv.h * s)
            caption = f"预览 {s}×（{cv.w}×{cv.h}）"
        else:
            img = img.scaled(PREVIEW_SIDE, PREVIEW_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            caption = f"预览（缩小，原图 {cv.w}×{cv.h}）"
        pm = QPixmap(img.size())
        p = QPainter(pm)
        p.fillRect(pm.rect(), checker(4))
        p.drawImage(0, 0, img)
        p.end()
        self.preview.setPixmap(pm)
        self.preview_caption.setText(caption)

    def set_canvas(self, cv: Canvas):
        self.cv = cv
        if self.color not in cv.palette:
            self.color = TRANSPARENT
        if self.selection:
            x0, y0, x1, y1 = self.selection
            if x1 >= cv.w or y1 >= cv.h:
                self.selection = None
        sig = tuple(cv.palette.items())
        if sig != self._palette_sig:
            self._palette_sig = sig
            self.rebuild_palette()
        self.setWindowTitle(f"{self.path.name} — pixel4ai  {cv.w}×{cv.h}")
        self.view.refit()
        self.refresh_side()

    def flash(self, msg: str):
        self.statusBar().showMessage(msg, 5000)

    def show_hover(self, cell):
        if cell is None:
            self.hover_label.setText("")
            return
        x, y = cell
        ch = self.cv.grid[y][x]
        self.hover_label.setText(f"x={x} y={y}   {ch}  {self.cv.palette[ch] or '透明'}")

    # ───────── 读写 ─────────

    def read_disk(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except OSError:
            return None

    def check_disk(self):
        """直接比内容：mtime 精度不够，AI 紧跟着编辑器保存、只改一格时会漏掉。文件很小，读一次很便宜。"""
        if self.view.button is not None:
            return          # 正在画，松手提交时会先读最新内容再叠加
        data = self.read_disk()
        if data is None or data == self.disk_bytes:
            return
        try:
            cv = Canvas.from_dict(json.loads(data))
        except (ValueError, PxlError) as e:
            msg = f"文件暂时读不了，保持当前画面：{e}"
            if msg != self._last_flash:     # 写了一半或手改坏了，下次轮询再试，不重复刷消息
                self._last_flash = msg
                self.flash(msg)
            return
        self.disk_bytes, self._last_flash = data, ""
        if not cv.same(self.cv):
            self.set_canvas(cv)
            self.flash("文件被外部修改（比如 AI），已刷新")

    def latest(self) -> Canvas:
        """磁盘上的最新内容。文件没变就直接用内存里的画布，不重新解析（1024×1024 解析一次要上百毫秒）。"""
        data = self.read_disk()
        if data is None or data == self.disk_bytes:
            return self.cv
        try:
            cv = Canvas.from_dict(json.loads(data))
        except (ValueError, PxlError):
            return self.cv
        self.disk_bytes = data
        return cv

    def write(self, cv: Canvas):
        data = cv.to_json().encode("utf-8")
        core.atomic_write(self.path, data)
        self.disk_bytes = data

    def mutate(self, fn, label: str = ""):
        """读磁盘最新版 → 应用改动 → 记历史 → 保存。"""
        before = self.latest()
        after = before.copy()
        try:
            fn(after)
        except PxlError as e:
            self.flash(str(e))
            self.set_canvas(before)
            return
        if not after.same(before):
            try:
                self.history.push(before)
                self.write(after)
            except OSError as e:
                QMessageBox.warning(self, "保存失败", str(e))
                return
            if label:
                self.flash(label)
        self.set_canvas(after)

    def commit(self, ops):
        def apply(cv: Canvas):
            for x, y, ch in ops:
                if 0 <= x < cv.w and 0 <= y < cv.h and ch in cv.palette:
                    cv.grid[y][x] = ch
        self.mutate(apply)

    def history_step(self, direction: str):
        try:
            cv, done = self.history.step(self.latest(), 1, direction)
        except PxlError as e:
            self.flash(str(e))
            return
        if not done:
            self.flash("没有可撤销的步骤" if direction == "undo" else "没有可重做的步骤")
            return
        self.write(cv)
        self.set_canvas(cv)
        self.flash("已撤销" if direction == "undo" else "已重做")

    # ───────── 操作 ─────────

    def zoom_fit(self):
        vp = self.scroll.viewport().size()
        fits = [z for z in ZOOMS if RULER + self.cv.w * z < vp.width() and RULER + self.cv.h * z < vp.height()]
        self.view.zoom = max(fits) if fits else ZOOMS[0]
        self.view.refit()
        self.flash(f"适应窗口：每格 {self.view.zoom}px")

    def set_tool(self, key: str):
        self.tool = key
        self.tool_actions[key].setChecked(True)

    def with_mirror(self, cells):
        cells = list(cells)
        if self.mirror:
            cells += [(self.cv.w - 1 - x, y) for x, y in cells]
        return cells

    def flood(self, cell, ch):
        def apply(cv: Canvas):
            for x, y in self.with_mirror([cell]):
                if 0 <= x < cv.w and 0 <= y < cv.h:
                    cv.fill(x, y, ch)
        self.mutate(apply)

    def pick(self, cell):
        x, y = cell
        self.select_color(self.cv.grid[y][x])
        if self.tool == "pick":
            self.set_tool("pencil")

    def select_color(self, ch: str):
        self.color = ch
        self.refresh_side()

    def edit_color(self, ch: str):
        if ch == TRANSPARENT:
            return
        color = QColorDialog.getColor(QColor(self.cv.palette[ch]), self, f"颜色 {ch}")
        if color.isValid():
            self.mutate(lambda cv: cv.set_color(ch, color.name()), f"{ch} 改成 {color.name()}")

    def add_color(self):
        color = QColorDialog.getColor(QColor("#888888"), self, "新颜色")
        if not color.isValid():
            return
        free = next((c for c in NEW_COLOR_CHARS if c not in self.cv.palette), "")
        ch, ok = QInputDialog.getText(self, "新颜色", "用哪个字符表示这个颜色（AI 用它指代）：", text=free)
        if not ok or not ch:
            return
        self.mutate(lambda cv: cv.set_color(ch, color.name()), f"新增 {ch} = {color.name()}")
        if ch in self.cv.palette:
            self.select_color(ch)

    def remove_color(self, ch: str):
        n = self.cv.count(ch)
        if n and QMessageBox.question(self, "删除颜色", f"{ch} 还用在 {n} 格上，删除后这些格子变透明。继续？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.mutate(lambda cv: cv.remove_color(ch, TRANSPARENT), f"已删除 {ch}")

    def set_selection(self, region):
        if region is None:
            self.selection = None
        else:
            x0, y0, x1, y1 = region
            self.selection = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self.view.update()

    def selection_done(self):
        if not self.selection:
            return
        x0, y0, x1, y1 = self.selection
        text = f"({x0},{y0})-({x1},{y1})"
        QGuiApplication.clipboard().setText(text)
        self.flash(f"选区 {text}，{x1 - x0 + 1}×{y1 - y0 + 1}，坐标已复制到剪贴板")

    def toggle_grid(self, on: bool):
        self.view.show_grid = on
        self.view.update()

    def zoom_step(self, d: int):
        i = min(range(len(ZOOMS)), key=lambda k: abs(ZOOMS[k] - self.view.zoom))
        self.view.zoom = ZOOMS[max(0, min(len(ZOOMS) - 1, i + d))]
        self.view.refit()
        self.flash(f"每格 {self.view.zoom}px")

    def open_path(self, path: Path):
        try:
            cv = core.load(path)
        except PxlError as e:
            QMessageBox.warning(self, "打不开", str(e))
            return
        self.path = Path(path)
        self.history = core.History(self.path)
        self.disk_bytes = self.read_disk()
        self.selection = None
        self.color = next((ch for ch in cv.palette if ch != TRANSPARENT), TRANSPARENT)
        self.view.zoom = fit_zoom(cv)
        self.set_canvas(cv)
        self.flash(f"已打开 {self.path}")

    def new_file(self):
        path = ask_new_file(self)
        if path:
            self.open_path(path)

    def open_file(self):
        path = ask_open_file(self)
        if path:
            self.open_path(path)

    def export_png(self):
        out, _ = QFileDialog.getSaveFileName(self, "导出 PNG", str(self.path.with_suffix(".png")), "PNG (*.png)")
        if not out:
            return
        scale, ok = QInputDialog.getInt(self, "导出 PNG", "每格多少像素：", 8, 1, 64)
        if not ok:
            return
        try:
            core.atomic_write(Path(out), render.png_bytes(self.cv, scale))
            self.flash(f"已导出 {out}")
        except (PxlError, OSError) as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def keyPressEvent(self, e):
        key = e.key()
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            chars = list(self.cv.palette)
            i = key - Qt.Key.Key_0
            if i < len(chars):
                self.select_color(chars[i])
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selection:
            x0, y0, x1, y1 = self.selection
            self.commit([(x, y, TRANSPARENT) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)])
        elif key == Qt.Key.Key_Escape:
            self.set_selection(None)
        else:
            super().keyPressEvent(e)


def fit_zoom(cv: Canvas) -> int:
    return max((z for z in ZOOMS if z * max(cv.w, cv.h) <= 720), default=ZOOMS[0])


def ask_new_file(parent=None, path: Path | None = None) -> Path | None:
    """依次问宽、高、调色板、保存位置，创建 .pxl；取消返回 None。给了 path 就不问保存位置。"""
    title = "新建画布"
    w, ok = QInputDialog.getInt(parent, title, "宽（格子数）：", 16, 1, core.MAX_SIDE)
    if not ok:
        return None
    h, ok = QInputDialog.getInt(parent, title, "高（格子数）：", w, 1, core.MAX_SIDE)
    if not ok:
        return None
    preset, ok = QInputDialog.getItem(parent, title, "调色板：", ["pico8", "mono", "gb", "none"], 0, False)
    if not ok:
        return None
    if path is None:
        out, _ = QFileDialog.getSaveFileName(parent, "保存到", str(Path.cwd() / "untitled.pxl"), PXL_FILTER)
        if not out:
            return None
        path = Path(out) if out.endswith(".pxl") else Path(out + ".pxl")
    history = core.History(path)
    try:
        history.push(core.load(path))   # 覆盖已有文件时，旧内容可以撤销找回
    except PxlError:
        history.reset()
    core.save(Canvas(w, h, core.PRESETS[preset]), path)
    return path


def ask_open_file(parent=None) -> Path | None:
    out, _ = QFileDialog.getOpenFileName(parent, "打开像素画", str(Path.cwd()), PXL_FILTER)
    return Path(out) if out else None


def ask_start_file() -> Path | None:
    box = QMessageBox()
    box.setWindowTitle("pixel4ai")
    box.setText("打开已有的 .pxl，还是新建一张画布？")
    open_btn = box.addButton("打开…", QMessageBox.ButtonRole.AcceptRole)
    new_btn = box.addButton("新建…", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    if box.clickedButton() == open_btn:
        return ask_open_file()
    if box.clickedButton() == new_btn:
        return ask_new_file()
    return None


def run_editor(path: Path | None = None) -> int:
    """不给 path 就弹窗选择打开或新建；path 不存在时直接进入新建。"""
    if path is not None and Path(path).exists():
        core.load(path)     # 文件损坏时先在命令行报错，不用等窗口
    app = QApplication.instance() or QApplication(sys.argv[:1])
    if path is None:
        path = ask_start_file()
    elif not Path(path).exists():
        path = ask_new_file(path=Path(path))
    if path is None:
        return 0
    try:
        win = Editor(Path(path))
    except PxlError as e:
        QMessageBox.warning(None, "打不开", str(e))
        return 1
    win.show()
    return app.exec()


USAGE = "用法: pxl-edit [画.pxl]    不给文件就弹窗选择打开或新建"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(args) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return run_editor(Path(args[0]) if args else None)
    except PxlError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
