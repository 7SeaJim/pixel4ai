"""参考库查询：pxl ref [库] [条目] [--find 关键词] [--list] [--full] [--check]

参考库的真实文件在项目的 refs/<库>/ 下，每个库一个 catalog.json。
AI 通过这里按需取一小段，输出有行数上限：把整个库读进上下文太浪费算力。

    pxl ref                          有哪些参考库
    pxl ref dither                   按场景选择 + 规则 + 反例（≤30 行）
    pxl ref dither <条目>            单个条目：做法、禁忌、预览图路径（≤20 行，--full 看分段）
    pxl ref dither --find 光晕       按关键词找条目和场景
    pxl ref dither --list            全部条目，每条一行
    pxl ref dither --check           校验 catalog.json 和文件
"""
from __future__ import annotations

import difflib
import json
import os
import struct
from pathlib import Path

from .core import PxlError

DIGEST_MAX_LINES = 30
ENTRY_MAX_LINES = 20
FIND_MAX_RESULTS = 20
LIST_MAX_RESULTS = 50
PREVIEW_MAX_SIDE = 384
KINDS = {"tile", "ramp", "edge"}
FAMILIES = {"ordered", "checker", "lines", "scatter", "cluster", "mixed"}
STATUSES = {"reference", "generated", "hand-tuned", "draft"}
REQUIRED = ("id", "kind", "family", "status", "title", "size", "files")


def refs_root() -> Path:
    """项目的 refs/ 目录：本文件在 <项目>/src/pixel4ai/ 下，往上两层就是项目根。可用 PIXEL4AI_REFS 覆盖。"""
    env = os.environ.get("PIXEL4AI_REFS")
    root = Path(env).expanduser() if env else Path(__file__).resolve().parents[2] / "refs"
    if not root.is_dir():
        raise PxlError(f"找不到参考库目录 {root}（需要从项目源码运行，或设置 PIXEL4AI_REFS）")
    return root


def libraries(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if (p / "catalog.json").is_file())


def load(name: str) -> tuple[Path, dict]:
    root = refs_root()
    libs = libraries(root)
    if name not in libs:
        raise PxlError(f"没有参考库 {name!r}；现有：{' '.join(libs) or '（空）'}")
    base = root / name
    try:
        cat = json.loads((base / "catalog.json").read_text(encoding="utf-8"))
    except ValueError as e:
        raise PxlError(f"{base / 'catalog.json'} 不是合法 JSON：{e}") from None
    if not isinstance(cat.get("entries"), list):
        raise PxlError(f"{base / 'catalog.json'} 缺少 entries 数组")
    return base, cat


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or len(head) < 24:
        return None
    return struct.unpack(">II", head[16:24])


def clip(lines: list[str], limit: int) -> str:
    if len(lines) > limit:
        lines = lines[:limit - 1] + [f"…（输出已截断，共 {len(lines)} 行；请换更具体的查询）"]
    return "\n".join(lines)


def fmt_density(d) -> str:
    if isinstance(d, (list, tuple)):
        return f"{d[0]:.0%}–{d[1]:.0%}"
    return f"{d:.0%}"


# ───────── 校验 ─────────

def check(base: Path, cat: dict) -> list[str]:
    problems = []
    entries = cat.get("entries", [])
    ids = [e.get("id") for e in entries]
    for dup in sorted({i for i in ids if ids.count(i) > 1 and i is not None}):
        problems.append(f"id 重复：{dup}")
    for e in entries:
        tag = e.get("id", "?")
        problems += [f"{tag}: 缺少字段 {f}" for f in REQUIRED if f not in e]
        for field, allowed in (("kind", KINDS), ("family", FAMILIES), ("status", STATUSES)):
            if field in e and e[field] not in allowed:
                problems.append(f"{tag}: {field} {e[field]!r} 应为 {' / '.join(sorted(allowed))}")
        files = e.get("files") or {}
        for key, rel in files.items():
            path = base / rel
            if not path.is_file():
                problems.append(f"{tag}: files.{key} 不存在：{rel}")
            elif key == "png":
                size = png_size(path)
                if size is None:
                    problems.append(f"{tag}: {rel} 不是 PNG")
                elif max(size) > PREVIEW_MAX_SIDE:
                    problems.append(f"{tag}: 预览图 {size[0]}×{size[1]} 超过 {PREVIEW_MAX_SIDE}px")
            elif key == "pxl":
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    problems.append(f"{tag}: {rel} 不是合法 JSON")
                    continue
                if "size" in e and list(doc.get("size", [])) != list(e["size"]):
                    problems.append(f"{tag}: size {e['size']} 与 {rel} 的 {doc.get('size')} 不一致")
        other = e.get("contrast_with")
        if other and other not in ids:
            problems.append(f"{tag}: contrast_with 指向不存在的条目 {other}")
    return problems


# ───────── 输出 ─────────

def entry_exists(ref: str, ids: set[str]) -> bool:
    """场景里写 sky-band，条目可以是 sky-band 或成对的 sky-band.bad / sky-band.good。"""
    return ref in ids or any(i.startswith(ref + ".") for i in ids)


def scene_line(s: dict, ids: set[str]) -> str:
    ref = s.get("entry")
    mark = "" if not ref else f" [{ref}]" if entry_exists(ref, ids) else f" [{ref} 待补]"
    return f"  {s['scene']} → {s['use']}：{s['tips']}{mark}"


def libraries_text() -> str:
    root = refs_root()
    lines = ["参考库（refs/）："]
    for name in libraries(root):
        _, cat = load(name)
        lines.append(f"  {name:<10} {cat.get('title', '')} · {len(cat['entries'])} 个条目")
    lines.append("用法：pxl ref <库>　查看条目：pxl ref <库> <条目>　搜索：pxl ref <库> --find 关键词")
    return "\n".join(lines)


def digest_text(name: str, cat: dict) -> str:
    entries = cat["entries"]
    ids = {e["id"] for e in entries if "id" in e}
    d = cat.get("digest") or {}
    lines = [f"{cat.get('title', name)}（refs/{name}）· {len(entries)} 个条目"]
    conventions = [c for c in (cat.get("shade_convention"), cat.get("density_convention")) if c]
    if conventions:
        lines.append("约定：" + "；".join(conventions))
    if d.get("scenes"):
        lines.append("按场景选择：")
        lines += [scene_line(s, ids) for s in d["scenes"]]
    if d.get("rules"):
        lines.append("规则：")
        lines += [f"  {i}. {r}" for i, r in enumerate(d["rules"], 1)]
    if d.get("anti"):
        lines.append("反例：" + "；".join(d["anti"]))
    shown = [e["id"] for e in entries][:8]
    more = f" 等 {len(entries)} 个" if len(entries) > 8 else ""
    lines.append("条目：" + ("、".join(shown) + more if shown else "（暂无）"))
    lines.append(f"查看：pxl ref {name} <条目>　搜索：pxl ref {name} --find 关键词　全部：pxl ref {name} --list")
    return clip(lines, DIGEST_MAX_LINES)


def entry_text(name: str, base: Path, cat: dict, key: str, full: bool = False) -> str:
    entries = {e.get("id"): e for e in cat["entries"]}
    e = entries.get(key)
    if e is None:
        close = difflib.get_close_matches(key, [i for i in entries if i], n=3, cutoff=0.3)
        close = close or [i for i in entries if i and key in i][:3]
        hint = f"是不是：{'、'.join(close)}" if close else f"用 pxl ref {name} --list 查看全部"
        raise PxlError(f"参考库 {name} 里没有条目 {key!r}；{hint}")
    lines = [f"{e['id']} — {e.get('title', '')}"]
    meta = [f"类型 {e.get('kind')}/{e.get('family')}", f"状态 {e.get('status')}"]
    if e.get("size"):
        meta.append(f"{e['size'][0]}×{e['size'][1]} 格")
    if e.get("density") is not None:
        meta.append("密度 " + fmt_density(e["density"]))
    if e.get("canvas_scale"):
        meta.append(f"适合画布 {e['canvas_scale']}")
    lines.append(" · ".join(meta))
    if e.get("shades"):
        lines.append("色阶 " + "  ".join(f"{k}={v}" for k, v in e["shades"].items()))
    for label, field in (("适用", "use_for"), ("禁忌", "avoid")):
        if e.get(field):
            lines.append(f"{label}：" + "；".join(e[field]))
    if e.get("why"):
        lines.append("为什么：" + e["why"])
    if e.get("lessons"):
        lines.append("做法：")
        lines += [f"  - {x}" for x in e["lessons"]]
    if full and e.get("segments"):
        lines.append("分段：")
        lines += [f"  {s['cols'][0]}–{s['cols'][1]} 列：{s['desc']}" for s in e["segments"]]
    if e.get("contrast_with"):
        lines.append(f"对照：pxl ref {name} {e['contrast_with']}")
    files = e.get("files") or {}
    if "png" in files:
        path = (base / files["png"]).resolve()
        size = png_size(path)
        cost = f"（{size[0]}×{size[1]}px，读图约 {max(1, size[0] * size[1] // 750)} token）" if size else ""
        lines.append(f"预览图：{path}{cost}")
    if "pxl" in files:
        lines.append(f"源文件：{(base / files['pxl']).resolve()}")
    if e.get("recipe"):
        lines.append("复现：" + e["recipe"])
    if e.get("source"):
        lines.append(f"出处：refs/{name}/{e['source']}")
    if not full and e.get("segments"):
        lines.append(f"（另有 {len(e['segments'])} 段分段说明，加 --full 查看）")
    return "\n".join(lines) if full else clip(lines, ENTRY_MAX_LINES)


def find_text(name: str, cat: dict, query: str) -> str:
    terms = query.lower().split()
    ids = {e["id"] for e in cat["entries"] if "id" in e}

    def match(text: str) -> bool:
        return all(t in text.lower() for t in terms)

    hits = [e for e in cat["entries"]
            if match(" ".join([e.get("id", ""), e.get("title", "")] + e.get("tags", []) + e.get("use_for", [])))]
    scenes = [s for s in (cat.get("digest") or {}).get("scenes", [])
              if match(" ".join(str(v) for v in s.values() if v))]
    lines = [f"  {e['id']:<22} {e.get('kind', ''):<5} {e.get('title', '')}" for e in hits[:FIND_MAX_RESULTS]]
    if len(hits) > FIND_MAX_RESULTS:
        lines.append(f"  …还有 {len(hits) - FIND_MAX_RESULTS} 条，换个更具体的关键词")
    if lines:
        lines.insert(0, "条目：")
    if scenes:
        lines.append("场景：")
        lines += [scene_line(s, ids) for s in scenes[:FIND_MAX_RESULTS]]
    if not lines:
        return f"没有匹配「{query}」的条目或场景。按场景选择：pxl ref {name}；全部条目：pxl ref {name} --list"
    return "\n".join(lines)


def list_text(name: str, cat: dict) -> str:
    entries = cat["entries"]
    lines = [f"{e.get('id', '?'):<22} {e.get('kind', ''):<5} {e.get('status', ''):<10} {e.get('title', '')}"
             for e in entries[:LIST_MAX_RESULTS]]
    if len(entries) > LIST_MAX_RESULTS:
        lines.append(f"…还有 {len(entries) - LIST_MAX_RESULTS} 条，用 --find 缩小范围")
    return "\n".join(lines) or "（暂无条目）"


def main(argv: list[str]) -> str:
    from .cli import Parser

    p = Parser(prog="pxl ref", description="查参考库。输出有长度上限，按需取用，不要整库读入。")
    p.add_argument("library", nargs="?", help="参考库名，比如 dither")
    p.add_argument("entry", nargs="?", help="条目 id")
    p.add_argument("--find", metavar="关键词", help="按关键词找条目和场景（多个词用空格分开，需全部匹配）")
    p.add_argument("--list", action="store_true", help="全部条目，每条一行")
    p.add_argument("--full", action="store_true", help="条目的完整内容（含分段说明，不截断）")
    p.add_argument("--check", action="store_true", help="校验 catalog.json 和它引用的文件")
    ns = p.parse_args(argv)
    if not ns.library:
        return libraries_text()
    base, cat = load(ns.library)
    if ns.check:
        problems = check(base, cat)
        if problems:
            raise PxlError(f"refs/{ns.library} 有 {len(problems)} 个问题：\n  " + "\n  ".join(problems))
        return f"✓ refs/{ns.library} 没有问题（{len(cat['entries'])} 个条目）"
    if ns.find:
        return find_text(ns.library, cat, ns.find)
    if ns.list:
        return list_text(ns.library, cat)
    if ns.entry:
        return entry_text(ns.library, base, cat, ns.entry, ns.full)
    return digest_text(ns.library, cat)
