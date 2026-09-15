"""python3 -m unittest discover -s tests"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pixel4ai import core, refs, render  # noqa: E402
from pixel4ai.core import Canvas, PxlError  # noqa: E402


def grid(cv: Canvas) -> list[str]:
    return ["".join(r) for r in cv.grid]


class CoreTest(unittest.TestCase):
    def test_roundtrip_json(self):
        cv = Canvas(5, 3, core.PRESETS["mono"])
        cv.px([(0, 0, "k"), (4, 2, "w")])
        again = Canvas.from_dict(json.loads(cv.to_json()))
        self.assertEqual(again.to_dict(), cv.to_dict())
        self.assertIn('    "k....",', cv.to_json())

    def test_rejects_bad_documents(self):
        good = Canvas(3, 2, {"k": "111111"}).to_dict()
        for mutate in (lambda d: d["rows"].pop(),
                       lambda d: d["rows"].__setitem__(0, "kk"),
                       lambda d: d["rows"].__setitem__(0, "kzk"),
                       lambda d: d["palette"].__setitem__("k", "red")):
            d = json.loads(json.dumps(good))
            mutate(d)
            with self.assertRaises(PxlError):
                Canvas.from_dict(d)

    def test_circle_shape(self):
        cv = Canvas(7, 7, {"k": "000000"})
        cv.ellipse(0, 0, 6, 6, "k", fill=True)
        self.assertEqual(grid(cv), ["..kkk..", ".kkkkk.", "kkkkkkk", "kkkkkkk", "kkkkkkk", ".kkkkk.", "..kkk.."])
        cv = Canvas(5, 5, {"k": "000000"})
        cv.ellipse(0, 0, 4, 4, "k")
        self.assertEqual(grid(cv), [".kkk.", "k...k", "k...k", "k...k", ".kkk."])

    def test_line_rect_fill(self):
        cv = Canvas(6, 4, {"k": "000000", "r": "ff0000"})
        cv.line(0, 0, 5, 3, "k")
        self.assertEqual(cv.count("k"), 6)
        cv.clear()
        cv.rect(1, 1, 4, 3, "k")
        cv.fill(2, 2, "r")
        self.assertEqual(grid(cv), ["......", ".kkkk.", ".krrk.", ".kkkk."])
        cv.fill(0, 0, "r")
        self.assertEqual(cv.count("."), 0)

    def test_bounds_and_palette_errors_are_specific(self):
        cv = Canvas(4, 4, {"k": "000000"})
        with self.assertRaisesRegex(PxlError, r"x 要在 0\.\.3"):
            cv.px([(4, 0, "k")])
        with self.assertRaisesRegex(PxlError, "不在调色板"):
            cv.rect(0, 0, 1, 1, "z")
        with self.assertRaisesRegex(PxlError, "长 3，应正好等于画布宽 4"):
            cv.set_rows(0, ["kkk"])
        with self.assertRaisesRegex(PxlError, "最多 2 个字符"):
            cv.stamp(2, 0, ["kkk"])
        with self.assertRaises(PxlError):
            cv.set_color("-", "000000")
        cv.px([(1, 1, "k")])
        with self.assertRaisesRegex(PxlError, "1 格"):
            cv.remove_color("k")

    def test_px_is_all_or_nothing(self):
        cv = Canvas(3, 3, {"k": "000000"})
        with self.assertRaises(PxlError):
            cv.px([(0, 0, "k"), (9, 9, "k")])
        self.assertEqual(cv.count("k"), 0)

    def test_stamp_keep_and_mirror_flip(self):
        cv = Canvas(5, 2, {"k": "000000", "r": "ff0000"})
        cv.clear("r")
        cv.stamp(0, 0, ["k?k", "?k"])
        self.assertEqual(grid(cv), ["krkrr", "rkrrr"])
        cv.mirror("lr")
        self.assertEqual(grid(cv), ["krkrk", "rkrkr"])
        cv.flip("v")
        self.assertEqual(grid(cv), ["rkrkr", "krkrk"])

    def test_shift_resize(self):
        cv = Canvas(3, 3, {"k": "000000"})
        cv.px([(0, 0, "k")])
        cv.shift(1, 1)
        self.assertEqual(grid(cv), ["...", ".k.", "..."])
        cv.resize(5, 5, "c")
        self.assertEqual(cv.grid[2][2], "k")
        cv.resize(1, 1, "c")
        self.assertEqual(grid(cv), ["k"])

    def test_history(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.pxl"
            h = core.History(path)
            a = Canvas(2, 1, {"k": "000000"})
            b = a.copy()
            b.px([(0, 0, "k")])
            h.push(a)
            back, n = h.step(b, 1, "undo")
            self.assertEqual((grid(back), n), (["..", ], 1))
            fwd, n = h.step(back, 5, "redo")
            self.assertEqual((grid(fwd), n), (["k."], 1))


    def test_ramps_roundtrip_and_validation(self):
        cv = Canvas(3, 1, {"a": "111111", "b": "555555", "c": "999999", "z": "ff0000"})
        cv.set_ramps(["abc"])
        again = Canvas.from_dict(json.loads(cv.to_json()))
        self.assertEqual(again.ramps, ["abc"])
        self.assertIn('"ramps": ["abc"]', cv.to_json())
        self.assertTrue(cv.same(again))
        for bad in (["a"], ["ab", "bc"], ["a.b"], ["aqb"], ["aba"]):
            with self.assertRaises(PxlError, msg=bad):
                cv.set_ramps(bad)
        cv.remove_color("b")
        self.assertEqual(cv.ramps, ["ac"])
        cv.remove_color("c")
        self.assertEqual(cv.ramps, [])
        self.assertNotIn("ramps", Canvas(1, 1).to_json())

    def test_polygon_cells(self):
        tri = core.polygon_cells([(0, 0), (6, 0), (0, 6)])
        for cell in ((0, 0), (6, 0), (0, 6), (1, 1)):
            self.assertIn(cell, tri)
        self.assertNotIn((5, 5), tri)
        thin = core.polygon_cells([(0, 0), (8, 1), (0, 1)])
        self.assertTrue(all(c in thin for c in core.line_cells(0, 0, 8, 1)))
        with self.assertRaises(PxlError):
            core.polygon_cells([(0, 0), (1, 1)])

    def test_shade(self):
        cv = Canvas(8, 8, {"1": "111111", "2": "444444", "3": "888888", "r": "ff0000"})
        cv.clear("3")
        cv.px([(0, 0, "r"), (1, 0, "1")])
        with self.assertRaisesRegex(PxlError, "色阶"):
            cv.shade(core.rect_cells(0, 0, 7, 7))
        cv.set_ramps(["123"])
        st = cv.shade(core.rect_cells(0, 0, 7, 7), steps=-1)
        self.assertEqual((cv.grid[3][3], cv.grid[0][0], cv.grid[0][1]), ("2", "r", "1"))
        self.assertEqual((st["no_ramp"]["r"], st["at_end"], st["changed"]), (1, 1, 62))
        st = cv.shade(core.rect_cells(0, 0, 7, 7), steps=2, only="2")
        self.assertEqual(cv.grid[3][3], "3")          # 越过尽头时停在最亮一档
        self.assertEqual(st["filtered"], 2)

    def test_shade_soft_edge_gets_denser_inward(self):
        cv = Canvas(61, 61, {"1": "111111", "2": "888888"})
        cv.clear("2")
        cv.set_ramps(["12"])
        disk = core.ellipse_cells(0, 0, 60, 60, True)
        cv.shade(disk, soft=3)
        dist = core.inner_distance(disk)
        density = {d: sum(cv.grid[y][x] == "1" for (x, y), dd in dist.items() if dd == d) /
                   sum(1 for dd in dist.values() if dd == d) for d in (1, 2, 3)}
        self.assertLess(density[1], density[2])
        self.assertLess(density[2], density[3])
        self.assertLess(density[1], 0.3)
        self.assertTrue(all(cv.grid[y][x] == "1" for (x, y), dd in dist.items() if dd >= 4))
        # 直边上不能出现整行全空或整行全满：逐行统计矩形上边缘那一圈
        cv2 = Canvas(40, 8, {"1": "111111", "2": "888888"})
        cv2.clear("2")
        cv2.set_ramps(["12"])
        cv2.shade(core.rect_cells(0, 0, 39, 7), soft=2)
        top = sum(cv2.grid[0][x] == "1" for x in range(1, 39))
        self.assertTrue(0 < top < 38, top)

    def test_shade_extend_adds_darker_color(self):
        cv = Canvas(4, 4, {"a": "806040", "b": "c09060"})
        cv.clear("a")
        cv.set_ramps(["ab"])
        st = cv.shade(core.rect_cells(0, 0, 3, 3))
        self.assertEqual((st["changed"], st["at_end"]), (0, 16))
        st = cv.shade(core.rect_cells(0, 0, 3, 3), extend=True)
        self.assertEqual(st["changed"], 16)
        (ch, color, base), = st["added"]
        self.assertEqual((base, cv.ramps, cv.grid[0][0]), ("a", [ch + "ab"], ch))
        self.assertLess(render._lum(color), render._lum("#806040"))
        lighter = cv.shade(core.rect_cells(0, 0, 0, 0), steps=3, extend=True)["added"]
        self.assertEqual(len(lighter), 1)      # ch→a→b 之后还差 1 档，补 1 个更亮的
        self.assertGreater(render._lum(lighter[0][1]), render._lum("#c09060"))

    def test_suggest_ramps_splits_materials(self):
        cv = Canvas(1, 1, {"u": "5a3a22", "v": "8a5a33", "w": "b98252",   # 棕色木头
                           "o": "ff8c1a", "O": "ffb366",                   # 高饱和橙
                           "p": "b04a70", "P": "e07aa0"})                  # 粉
        groups = [set(g) for g in core.suggest_ramps(cv)]
        for expected in ({"u", "v", "w"}, {"o", "O"}, {"p", "P"}):
            self.assertIn(expected, groups)
        self.assertEqual(core.suggest_ramps(cv)[[set(g) for g in core.suggest_ramps(cv)].index({"u", "v", "w"})], "uvw")


    def test_catmull_rom_smooth_and_pixel_perfect(self):
        pts = [(0, 10), (20, 4), (40, 14), (60, 8)]
        cells = core.catmull_rom_cells(pts)
        for px, py in pts:
            self.assertTrue(any(abs(x - px) <= 1 and abs(y - py) <= 1 for x, y in cells), (px, py))
        for a, b in zip(cells, cells[1:]):
            self.assertLessEqual(max(abs(a[0] - b[0]), abs(a[1] - b[1])), 1)        # 8 连通，不断开
        for a, _, c in zip(cells, cells[1:], cells[2:]):
            self.assertFalse(abs(a[0] - c[0]) == 1 and abs(a[1] - c[1]) == 1, (a, c))  # 没有 L 形拐角
        with self.assertRaises(PxlError):
            core.catmull_rom_cells([(0, 0)])

    def test_curve_fill_and_until(self):
        cv = Canvas(20, 12, {"k": "111111", "g": "448844", "s": "aaccff"})
        cv.clear("s")
        cv.rect(0, 10, 19, 11, "k", fill=True)
        st = cv.curve([(0, 4), (10, 6), (19, 3)], "k", fill="g", until="k")
        self.assertEqual(set(st["profile"]), set(range(20)))
        self.assertEqual((cv.grid[9][10], cv.grid[10][10], cv.grid[0][10]), ("g", "k", "s"))
        with self.assertRaisesRegex(PxlError, "从左到右"):
            cv.curve([(10, 4), (5, 6), (19, 3)], "k", fill="g")
        up = Canvas(10, 10, {"k": "111111", "b": "3355aa"})
        up.curve([(0, 5), (9, 5)], "k", fill="b", direction="up")
        self.assertEqual((up.grid[0][3], up.grid[5][3], up.grid[8][3]), ("b", "k", "."))

    def test_curve_partial_fill_reports_vertical_edges(self):
        st = Canvas(40, 20, {"k": "111111"}).curve([(10, 8), (20, 6), (30, 9)], "k", fill="k")
        self.assertEqual([side for side, _, _ in st["cliffs"]], ["左", "右"])
        self.assertEqual(st["cliffs"][0][1], 10)
        full = Canvas(40, 20, {"k": "111111"}).curve([(0, 8), (39, 8)], "k", fill="k")
        self.assertEqual(full["cliffs"], [])
        self.assertEqual(Canvas(40, 20, {"k": "111111"}).curve([(10, 8), (30, 9)], "k")["cliffs"], [])   # 不填充不检查

    def test_horizon_points(self):
        args = (256, 256, 150, 160, 10, 6, 0, 255)
        a = core.horizon_points(*args, seed=1)
        self.assertEqual(a, core.horizon_points(*args, seed=1))
        self.assertNotEqual(a, core.horizon_points(*args, seed=2))
        self.assertEqual((a[0][0], a[-1][0]), (0, 255))
        self.assertTrue(all(q[0] > p[0] for p, q in zip(a, a[1:])))
        ys = [p[1] for p in a]
        self.assertTrue(150 - 15 <= min(ys) and max(ys) <= 160 + 15, ys)

    def test_fill_boundary_and_closed(self):
        cv = Canvas(10, 8, {"k": "111111", "r": "ff0000", "g": "00ff00"})
        cv.rect(2, 2, 7, 6, "k")
        cv.px([(4, 4, "g")])                              # 轮廓里有杂色，也要一起填
        st = cv.fill(4, 3, "r", boundary="k", closed=True)
        self.assertEqual((st["filled"], st["edges"]), (12, []))
        self.assertEqual((cv.grid[4][4], cv.grid[2][2]), ("r", "k"))
        cv.px([(7, 4, ".")])                               # 轮廓开一个口
        before = [r[:] for r in cv.grid]
        with self.assertRaisesRegex(PxlError, "没有封闭"):
            cv.fill(4, 3, "g", boundary="k", closed=True)
        self.assertEqual(cv.grid, before)
        self.assertEqual(set(cv.fill(4, 3, "g", boundary="k")["edges"]), {"上", "下", "左", "右"})
        with self.assertRaisesRegex(PxlError, "边界字符"):
            cv.fill(2, 2, "g", boundary="k")


class RenderTest(unittest.TestCase):
    def test_view_rulers(self):
        cv = Canvas(12, 2, {"k": "000000"})
        lines = render.view(cv).splitlines()
        self.assertEqual(lines[0], "             1")
        self.assertEqual(lines[1], "   012345678901")
        self.assertEqual(lines[2], "0  ............")
        spaced = render.view(cv, (9, 0, 11, 1), spaced=True).splitlines()
        self.assertEqual(spaced[:3], ["     1", "   9 0 1", "0  . . ."])

    def test_png_decodes(self):
        cv = Canvas(3, 2, {"r": "ff0000"})
        cv.px([(1, 0, "r")])
        data = render.png_bytes(cv, scale=2)
        self.assertTrue(data.startswith(b"\x89PNG"))
        idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), 4 * (1 + 6 * 4))
        self.assertEqual(raw[1 + 8:1 + 12], bytes((255, 0, 0, 255)))
        self.assertEqual(raw[1:5], bytes(4))

    def test_ascii_and_ansi(self):
        cv = Canvas(2, 3, {"k": "000000", "w": "ffffff"})
        cv.set_rows(0, ["kw", "k.", ".."])
        self.assertEqual(render.ascii_art(cv), "kw\nk.\n..")
        self.assertEqual(render.ascii_art(cv, ramp=True).splitlines()[0], "@@")
        self.assertEqual(len(render.ansi(cv).splitlines()), 2)


class LargeCanvasTest(unittest.TestCase):
    def test_size_limits(self):
        Canvas(512, 512)
        Canvas(1024, 1024)
        with self.assertRaises(PxlError):
            Canvas(1025, 16)

    def test_overview(self):
        cv = Canvas(512, 512, {"k": "000000"})
        cv.line(0, 100, 511, 100, "k")
        self.assertEqual(render.auto_step(512, 512), 8)
        lines = render.view(cv, step=8).splitlines()
        self.assertLess(len(lines), 72)
        self.assertLessEqual(max(len(line) for line in lines[1:]), 3 + 2 + 64)
        self.assertIn(" 96  " + "k" * 64, lines)            # 1 格粗的线在缩略图里也看得见
        self.assertEqual([lines[1][-1], lines[2][-1], lines[3][-1]], ["5", "0", "4"])   # 最后一列 x=504

    def test_bbox_diff_same(self):
        cv = Canvas(300, 200, {"k": "000000"})
        self.assertIsNone(cv.bbox())
        b = cv.copy()
        b.px([(10, 20, "k"), (250, 150, "k")])
        self.assertEqual(b.bbox(), (10, 20, 250, 150))
        self.assertEqual(core.diff(cv, b), (2, (10, 20, 250, 150)))
        self.assertFalse(cv.same(b))
        self.assertTrue(b.same(b.copy()))

    def test_history_snapshots_and_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            h = core.History(Path(d) / "big.pxl")
            cv = Canvas(512, 512, {"k": "000000"})
            for i in range(core.HISTORY_LIMIT + 5):
                cv.px([(i, 0, "k")])
                h.push(cv)
            snaps = lambda: len(list(h.dir.glob("*.json.gz")))
            self.assertEqual(snaps(), core.HISTORY_LIMIT)
            back, n = h.step(cv, 3, "undo")
            self.assertEqual((n, back.grid[0][52], back.grid[0][53]), (3, "k", "."))
            self.assertEqual(snaps(), core.HISTORY_LIMIT)
            h.push(back)                                    # 新改动清掉 redo 的快照
            self.assertEqual(snaps(), core.HISTORY_LIMIT - 2)

            legacy = Path(d) / ".old.pxl.history.json"
            old = Canvas(2, 1, {"k": "000000"})
            legacy.write_text(json.dumps({"undo": [old.to_dict()], "redo": []}))
            cur = old.copy()
            cur.px([(0, 0, "k")])
            prev, n = core.History(Path(d) / "old.pxl").step(cur, 1, "undo")
            self.assertEqual((n, grid(prev)), (1, [".."]))
            self.assertFalse(legacy.exists())


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.file = self.dir / "s.pxl"

    def tearDown(self):
        self.tmp.cleanup()

    def pxl(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(ROOT / "bin" / "pxl"), *args], input=stdin, text=True,
                              capture_output=True, cwd=self.dir)

    def test_script_with_hash_color_and_block(self):
        self.assertEqual(self.pxl("s.pxl", "new", "6", "3").returncode, 0)
        r = self.pxl("s.pxl", "apply", stdin="# 注释\npalette set # 222222\nstamp 1 0\n#?#\nk#k\nend\nmirror lr\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(grid(core.load(self.file)), [".#..#.", ".k##k.", "......"])
        self.assertIn("改动", r.stdout)

    def test_failed_script_changes_nothing(self):
        self.pxl("s.pxl", "new", "4", "4")
        before = self.file.read_text()
        r = self.pxl("s.pxl", "apply", stdin="rect 0 0 3 3 k\nrows 0 kk\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("第 2 行", r.stderr)
        self.assertEqual(self.file.read_text(), before)

    def test_undo_redo_and_quiet(self):
        self.pxl("s.pxl", "new", "4", "4")
        self.pxl("s.pxl", "rect", "0", "0", "3", "3", "k", "--fill", "-q")
        self.assertEqual(core.load(self.file).count("k"), 16)
        self.assertEqual(self.pxl("s.pxl", "undo").returncode, 0)
        self.assertEqual(core.load(self.file).count("k"), 0)
        self.pxl("s.pxl", "redo")
        self.assertEqual(core.load(self.file).count("k"), 16)

    def test_new_refuses_overwrite(self):
        self.pxl("s.pxl", "new", "4", "4")
        r = self.pxl("s.pxl", "new", "8", "8")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--force", r.stderr)

    def test_ramp_and_shade_commands(self):
        self.pxl("s.pxl", "new", "6", "4", "--palette", "gb")
        r = self.pxl("s.pxl", "shade", "rect", "0", "0", "5", "3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ramp", r.stderr)
        self.assertIn("ramp set", self.pxl("s.pxl", "ramp", "suggest").stdout)
        self.pxl("s.pxl", "clear", "d", "-q")
        self.assertEqual(self.pxl("s.pxl", "ramp", "set", "abcd", "-q").returncode, 0)
        r = self.pxl("s.pxl", "apply", stdin="shade ellipse 0 0 5 3 --steps -2\nshade poly 0 0 5 0 0 3 --steps 1 --only b\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("压暗", r.stdout)
        self.assertIn("提亮", r.stdout)
        cv = core.load(self.file)
        self.assertEqual(cv.ramps, ["abcd"])
        self.assertIn("b", "".join(grid(cv)))
        self.assertIn("色阶", self.pxl("s.pxl", "info").stdout)
        r = self.pxl("s.pxl", "shade", "rect", "50", "50", "60", "60")
        self.assertIn("画布外", r.stderr)

    def test_shade_union_darkens_overlap_once(self):
        self.pxl("s.pxl", "new", "8", "1", "--palette", "none")
        self.pxl("s.pxl", "palette", "set", "a", "222222", "b", "555555", "c", "999999", "-q")
        self.pxl("s.pxl", "clear", "c", "-q")
        self.pxl("s.pxl", "ramp", "set", "abc", "-q")
        r = self.pxl("s.pxl", "shade", "rect", "0", "0", "4", "0", "rect", "3", "0", "7", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(grid(core.load(self.file)), ["bbbbbbbb"])
        r = self.pxl("s.pxl", "shade", "rect", "0", "0", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("4 个数", r.stderr)
        r = self.pxl("s.pxl", "shade", "0", "0", "1", "1")
        self.assertIn("rect / ellipse / poly", r.stderr)

    def test_curve_horizon_fill_commands(self):
        self.pxl("s.pxl", "new", "64", "64", "--palette", "pico8")
        r = self.pxl("s.pxl", "horizon", "30", "34", "g", "--fill", "g", "--seed", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("高度剖面", r.stdout)
        self.assertIn("等价于 curve", r.stdout)
        r = self.pxl("s.pxl", "horizon", "40", "40", "b", "--wave", "0", "-q")
        self.assertIn("像一条直线", r.stdout)
        r = self.pxl("s.pxl", "apply", stdin="curve 0 50 30 46 63 52 k --fill k\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(core.load(self.file).grid[63][10], "k")
        r = self.pxl("s.pxl", "fill", "10", "5", "r", "--boundary", "g", "--closed")
        self.assertEqual(r.returncode, 1)
        self.assertIn("没有封闭", r.stderr)
        self.assertEqual(self.pxl("s.pxl", "curve", "0", "5", "k").returncode, 1)

    def test_large_canvas_feedback_stays_short(self):
        self.pxl("s.pxl", "new", "512", "512")
        r = self.pxl("s.pxl", "rect", "0", "0", "511", "511", "k", "--fill")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(len(r.stdout.splitlines()), 90)
        self.assertIn("缩略视图", r.stdout)
        r = self.pxl("s.pxl", "px", "300", "300", "w")
        self.assertIn("只显示改动附近 (298,298)-(302,302)", r.stdout)
        self.assertLess(len(r.stdout.splitlines()), 20)
        r = self.pxl("s.pxl", "view", "--region", "290", "290", "310", "310")
        self.assertNotIn("缩略", r.stdout)
        self.assertEqual(self.pxl("s.pxl", "undo", "-q").returncode, 0)
        self.assertEqual(core.load(self.file).grid[300][300], "k")

    def test_export_png(self):
        self.pxl("s.pxl", "new", "4", "4")
        r = self.pxl("s.pxl", "export", "png", "--scale", "4", "--grid")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.dir / "s.png").read_bytes().startswith(b"\x89PNG"))


class LintTest(unittest.TestCase):
    """构图自检：交界近似直线、没有过渡带、大块平涂、缺接触阴影、outline。"""

    def scene(self, wavy=False, shaded=False):
        from pixel4ai import lint  # noqa: F401
        cv = Canvas(128, 64, {"a": "c9b6e4", "b": "b8a2d6", "G": "4f6d45", "l": "3a5233", "o": "c98d5e"})
        cv.set_ramps(["ba", "lG"])
        cv.clear("a")
        if wavy:
            cv.curve([(0, 20), (30, 15), (64, 25), (100, 14), (127, 22)], "b", fill="b")
            cv.curve([(0, 42), (40, 36), (80, 46), (127, 38)], "G", fill="G")
        else:
            cv.rect(0, 20, 127, 63, "b", fill=True)
            cv.rect(0, 40, 127, 63, "G", fill=True)
            cv.rect(50, 26, 70, 39, "o", fill=True)
            if shaded:
                cv.shade(core.ellipse_cells(46, 39, 74, 45, True))
        return cv

    def test_straight_bands_and_missing_contact_shadow(self):
        from pixel4ai import lint
        cv = self.scene()
        pairs = {(s["a"], s["b"]) for s in lint.analyze(cv)["seams"]}
        self.assertTrue({("a", "b"), ("b", "G")} <= pairs, pairs)
        text, warn = lint.report(cv, "t.pxl", objects=[(50, 26, 70, 39)])
        for needle in ("近似直线", "没有过渡带", "没有接触阴影"):
            self.assertIn(needle, text)
        self.assertLessEqual(len(text.splitlines()), 30)
        ab = next(s for s in lint.analyze(cv)["seams"] if (s["a"], s["b"]) == ("a", "b"))
        self.assertTrue(ab["gradient"])
        bg = next(s for s in lint.analyze(cv)["seams"] if (s["a"], s["b"]) == ("b", "G"))
        self.assertFalse(bg["gradient"])                 # 不同材质之间的清晰边界不算缺过渡

    def test_wavy_bands_not_straight(self):
        from pixel4ai import lint
        self.assertNotIn("近似直线", lint.report(self.scene(wavy=True), "t.pxl")[0])

    def test_contact_shadow_found_after_shade(self):
        from pixel4ai import lint
        text = lint.report(self.scene(shaded=True), "t.pxl", objects=[(50, 26, 70, 39)])[0]
        self.assertNotIn("没有接触阴影", text)
        self.assertIn("✓ 有接触阴影", text)

    def test_contact_check_needs_explicit_objects(self):
        from pixel4ai import lint
        text = lint.report(self.scene(), "t.pxl")[0]
        self.assertIn("--object", text)                 # 不指定物体时只给提示，不猜哪块是物体
        self.assertNotIn("没有接触阴影", text)
        self.assertNotIn("接触阴影", lint.summary(self.scene(), "t.pxl") or "")
        with self.assertRaises(PxlError):
            lint.report(self.scene(), "t.pxl", objects=[(0, 0, 500, 10)])

    def test_flats_and_outline(self):
        from pixel4ai import lint
        cv = self.scene()
        self.assertEqual(lint.analyze(cv)["flats"][0]["char"], "G")
        out = lint.outline_canvas(cv)
        self.assertEqual((out.grid[50][10], out.grid[40][10], out.grid[20][10]), (".", "G", "."))

    def test_inspect_command_and_apply_lint(self):
        with tempfile.TemporaryDirectory() as d:
            run = lambda *a, stdin=None: subprocess.run([sys.executable, str(ROOT / "bin" / "pxl"), *a], input=stdin,
                                                        text=True, capture_output=True, cwd=d, timeout=60)
            run("s.pxl", "new", "128", "64", "--palette", "none")
            script = "palette set a c9b6e4 b b8a2d6 G 4f6d45\nclear a\nrect 0 20 127 63 b --fill\nrect 0 40 127 63 G --fill\n"
            r = run("s.pxl", "apply", "-q", stdin=script)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("构图检查", r.stdout)
            self.assertNotIn("构图检查", run("s.pxl", "apply", "-q", "--no-lint", stdin="px 0 0 b\n").stdout)
            r = run("s.pxl", "inspect")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("近似直线", r.stdout)
            r = run("s.pxl", "inspect", "--object", "10", "10", "20", "19", "--object", "60", "30", "70", "39")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.count("⚠ 没有接触阴影（"), 2)
            r = run("s.pxl", "view", "--outline", "--region", "0", "15", "20", "45")
            self.assertIn("只显示材质交界", r.stdout)
            self.assertIn("G" * 21, r.stdout)


class RefsTest(unittest.TestCase):
    """pxl ref：AI 按需查参考库，输出必须有长度上限。"""

    def pxl(self, *args, env=None, cwd=None):
        return subprocess.run([sys.executable, str(ROOT / "bin" / "pxl"), *args], capture_output=True, text=True,
                              timeout=60, env=env, cwd=cwd)

    def test_catalog_is_valid(self):
        base, cat = refs.load("dither")
        self.assertEqual(refs.check(base, cat), [])
        r = self.pxl("ref", "dither", "--check")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_library_list(self):
        r = self.pxl("ref")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dither", r.stdout)

    def test_digest_is_bounded(self):
        r = self.pxl("ref", "dither")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLessEqual(len(r.stdout.strip().splitlines()), refs.DIGEST_MAX_LINES)
        self.assertNotIn("已截断", r.stdout)
        self.assertIn("只在相邻色阶之间抖动", r.stdout)
        self.assertIn("[reference-strip]", r.stdout)

    def test_digest_matches_readme(self):
        """catalog 里给 AI 的精简版，条数要和 README 第 3、4 节一致，防止只改了一边。"""
        text = (ROOT / "refs" / "dither" / "README.md").read_text(encoding="utf-8")
        table_rows = lambda s: [l for l in s.splitlines() if l.startswith("| ") and "---" not in l][1:]
        scenes = table_rows(text.split("## 3.")[1].split("## 4.")[0])
        sec4 = text.split("## 4.")[1]
        rules = re.findall(r"^\d+\. ", sec4.split("### 反例清单")[0], re.M)
        anti = table_rows(sec4.split("### 反例清单")[1].split("\n---")[0])
        digest = refs.load("dither")[1]["digest"]
        self.assertEqual(len(digest["scenes"]), len(scenes))
        self.assertEqual(len(digest["rules"]), len(rules))
        self.assertEqual(len(digest["anti"]), len(anti))

    def test_entry_is_bounded_and_points_to_files(self):
        r = self.pxl("ref", "dither", "reference-strip")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLessEqual(len(r.stdout.strip().splitlines()), refs.ENTRY_MAX_LINES)
        self.assertNotIn("已截断", r.stdout)
        preview = re.search(r"预览图：(\S+?\.png)", r.stdout)
        self.assertTrue(preview and Path(preview.group(1)).is_file(), r.stdout)
        full = self.pxl("ref", "dither", "reference-strip", "--full")
        self.assertIn("分段：", full.stdout)
        self.assertNotIn("分段：", r.stdout)

    def test_find_and_suggestions(self):
        r = self.pxl("ref", "dither", "--find", "棋盘格")
        self.assertIn("reference-strip", r.stdout)
        r = self.pxl("ref", "dither", "--find", "光晕")
        self.assertIn("glow-radial", r.stdout)
        r = self.pxl("ref", "dither", "reference")
        self.assertEqual(r.returncode, 1)
        self.assertIn("reference-strip", r.stderr)
        r = self.pxl("ref", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("dither", r.stderr)

    def test_check_reports_problems(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "demo"
            lib.mkdir()
            bad = {"id": "a", "kind": "blob", "family": "ordered", "status": "draft", "title": "t", "size": [2, 2],
                   "files": {"png": "missing.png"}, "contrast_with": "zzz"}
            (lib / "catalog.json").write_text(json.dumps({"version": 1, "entries": [bad, {"id": "a"}]}))
            env = {**os.environ, "PIXEL4AI_REFS": d}
            r = self.pxl("ref", "demo", "--check", env=env)
            self.assertEqual(r.returncode, 1)
            for needle in ("id 重复", "kind 'blob'", "不存在：missing.png", "缺少字段 files", "contrast_with"):
                self.assertIn(needle, r.stderr)

    def test_ref_via_skill_wrapper_without_path(self):
        self.assertIn("pxl ref dither", (ROOT / "skills" / "pixel-art" / "SKILL.md").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "skills" / "pixel-art"
            link.parent.mkdir()
            link.symlink_to(ROOT / "skills" / "pixel-art")
            r = subprocess.run([str(link / "scripts" / "pxl"), "ref", "dither"], env={"HOME": d, "PATH": "/usr/bin:/bin"},
                               cwd=d, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLessEqual(len(r.stdout.strip().splitlines()), refs.DIGEST_MAX_LINES)


class SkillTest(unittest.TestCase):
    """AI 在另一个窗口里调用：不能依赖 PATH，也不能要求用户告诉它项目路径。"""
    SKILL = ROOT / "skills" / "pixel-art"

    def run_wrapper(self, wrapper: Path, *args, env: dict, cwd: str):
        return subprocess.run([str(wrapper), *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=60)

    def test_frontmatter(self):
        text = (self.SKILL / "SKILL.md").read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m, "SKILL.md 缺少 frontmatter")
        fields = dict(line.split(": ", 1) for line in m.group(1).splitlines())
        self.assertEqual(fields["name"], "pixel-art")
        self.assertNotIn(": ", fields["description"], "描述里出现 ': ' 会让 YAML 解析失败，Skill 会被忽略")
        self.assertIn("${CLAUDE_SKILL_DIR}/scripts/pxl", text)
        self.assertTrue(os.access(self.SKILL / "scripts" / "pxl", os.X_OK), "scripts/pxl 需要可执行权限")

    def test_wrapper_via_symlink_without_path(self):
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "skills" / "pixel-art"
            link.parent.mkdir()
            link.symlink_to(self.SKILL)
            # HOME 指向临时目录，排除 ~/Projects/pixel4ai 这个兜底，只靠顺着软链接找项目
            env = {"HOME": d, "PATH": "/usr/bin:/bin"}
            r = self.run_wrapper(link / "scripts" / "pxl", str(ROOT / "examples" / "mushroom.pxl"), "info", env=env, cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("16×16", r.stdout)

    def test_copied_skill_needs_pixel4ai_home(self):
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / "pixel-art"
            shutil.copytree(self.SKILL, copy)
            env = {"HOME": d, "PATH": "/usr/bin:/bin"}
            target = str(ROOT / "examples" / "mushroom.pxl")
            r = self.run_wrapper(copy / "scripts" / "pxl", target, "info", env=env, cwd=d)
            self.assertEqual(r.returncode, 1)
            self.assertIn("PIXEL4AI_HOME", r.stderr)
            r = self.run_wrapper(copy / "scripts" / "pxl", target, "info", env={**env, "PIXEL4AI_HOME": str(ROOT)}, cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr)


@unittest.skipIf(os.environ.get("PXL_SKIP_GUI"), "PXL_SKIP_GUI")
class EditorTest(unittest.TestCase):
    """离屏驱动编辑器：画一笔、外部修改后自动刷新、撤销。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PyQt6.QtWidgets import QApplication
        except ImportError:
            raise unittest.SkipTest("没有 PyQt6")
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, ms):
        from PyQt6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def test_stroke_reload_undo(self):
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        from pixel4ai import editor

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "e.pxl"
            core.save(Canvas(8, 8, core.PRESETS["mono"]), path)
            win = editor.Editor(path)
            win.show()
            view, z = win.view, win.view.zoom

            def center(x, y):
                return QPoint(editor.RULER + x * z + z // 2, editor.RULER + y * z + z // 2)

            QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center(1, 1))
            QTest.mouseMove(view, center(5, 1))
            QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center(5, 1))
            self.assertEqual(grid(core.load(path))[1], ".kkkkk..")

            # AI 从外部改文件 → 编辑器自动刷新
            ext = core.load(path)
            ext.px([(0, 7, "w")])
            core.save(ext, path)
            self.wait(editor.POLL_MS * 2 + 100)
            self.assertEqual(win.cv.grid[7][0], "w")

            # 编辑器再画一笔，不会丢掉外部改动
            win.tool = "rect"
            QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, center(3, 3))
            QTest.mouseMove(view, center(4, 4))
            QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, center(4, 4))
            saved = core.load(path)
            self.assertEqual(saved.grid[7][0], "w")
            self.assertEqual(saved.count("k"), 5 + 4)

            win.history_step("undo")
            self.assertEqual(core.load(path).count("k"), 5)

            other = Path(d) / "other.pxl"
            core.save(Canvas(3, 5, core.PRESETS["gb"]), other)
            win.open_path(other)
            self.assertEqual((win.cv.w, win.cv.h, win.path), (3, 5, other))
            self.assertIn("other.pxl", win.windowTitle())
            out = os.environ.get("PXL_SCREENSHOT")
            if out:
                win.grab().save(out)
            win.close()

    def test_editor_keeps_ramps(self):
        from pixel4ai import editor

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.pxl"
            cv = Canvas(4, 4, {"a": "111111", "b": "999999"})
            cv.set_ramps(["ab"])
            core.save(cv, path)
            win = editor.Editor(path)
            win.show()
            win.commit([(1, 1, "a")])
            self.assertEqual(core.load(path).ramps, ["ab"])
            win.history_step("undo")
            self.assertEqual(core.load(path).ramps, ["ab"])
            win.close()

    def test_checker_aligned_with_cells(self):
        """透明处的棋盘格在每个缩放倍数下都要和格子对齐。"""
        from pixel4ai import editor

        n = 12
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.pxl"
            core.save(Canvas(n, n), path)
            win = editor.Editor(path)
            win.resize(1200, 1000)
            win.show()
            win.view.show_grid = False
            for z in editor.ZOOMS:
                s = editor.checker_cell_size(z)
                self.assertTrue(s % z == 0 or z % s == 0, (z, s))
                win.view.zoom = z
                win.view.refit()
                img = win.view.grab().toImage()
                for cx in range(4):
                    for cy in range(4):
                        if (max(cx, cy) + 1) * s > n * z:
                            continue
                        # 每块取四个角附近的点，x、y 偏移要分开变：只取中心，错开半块时正好落在交界上；
                        # 只取对角线上的点，横竖错开同样多时深浅奇偶不变，也查不出来
                        for ox, oy in ((1, 1), (1, s - 2), (s - 2, 1), (s - 2, s - 2)):
                            px, py = editor.RULER + cx * s + ox, editor.RULER + cy * s + oy
                            if px >= img.width() or py >= img.height():
                                continue
                            dark = img.pixelColor(px, py).red() < 220
                            self.assertEqual(dark, (cx + cy) % 2 == 0, f"zoom={z} 棋盘块 ({cx},{cy}) 取样偏移 ({ox},{oy})")
            win.close()

    def test_large_canvas_scroll_and_paint(self):
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        from pixel4ai import editor

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "big.pxl"
            core.save(Canvas(512, 512, core.PRESETS["mono"]), path)
            win = editor.Editor(path)
            win.resize(900, 700)
            win.show()
            self.assertEqual(win.view.zoom, 1)
            self.assertLessEqual(win.preview.pixmap().width(), editor.PREVIEW_SIDE)
            win.view.zoom = 8
            win.view.refit()
            self.wait(50)
            win.scroll.horizontalScrollBar().setValue(2000)
            win.scroll.verticalScrollBar().setValue(2000)
            self.wait(50)
            vis = win.view.visibleRegion().boundingRect()
            self.assertGreater(vis.left(), 1000)
            pos = QPoint(vis.left() + 200, vis.top() + 200)
            x, y = (pos.x() - editor.RULER) // 8, (pos.y() - editor.RULER) // 8
            QTest.mouseClick(win.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
            self.assertEqual(core.load(path).grid[y][x], "k")
            # 点在吸附到可见区域左边的坐标尺上：不画
            QTest.mouseClick(win.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(vis.left() + 5, vis.top() + 300))
            self.assertEqual(core.load(path).count("k"), 1)
            win.zoom_fit()
            self.assertLessEqual(editor.RULER + 512 * win.view.zoom, win.scroll.viewport().width())
            out = os.environ.get("PXL_SCREENSHOT_LARGE")
            if out:
                win.view.zoom = 8
                win.view.refit()
                win.scroll.horizontalScrollBar().setValue(2000)
                win.scroll.verticalScrollBar().setValue(2000)
                self.wait(50)
                win.grab().save(out)
            win.close()

    def test_editor_runs_as_script(self):
        """在包目录里直接 python3 editor.py 不能因为相对导入报错。"""
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        r = subprocess.run([sys.executable, "editor.py", "--help"], cwd=ROOT / "src" / "pixel4ai", env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("用法", r.stdout)
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "pxl-edit"), "--help"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("用法", r.stdout)


if __name__ == "__main__":
    unittest.main()
