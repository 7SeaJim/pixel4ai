#!/usr/bin/env bash
# 安装并自检 pixel4ai：命令行 pxl、编辑器 pxl-edit、Claude Code Skill pixel-art。
# 可以重复执行；已经装好的会跳过，被别的文件占用的位置不会覆盖。
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
SKILLS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
problems=0

link() {
    local src=$1 dst=$2
    mkdir -p "$(dirname "$dst")"
    if [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$(readlink -f "$src")" ]; then
        echo "  已存在  $dst"
    elif [ -e "$dst" ] || [ -L "$dst" ]; then
        echo "  跳过    $dst 已被占用（不是指向本项目的软链接），请手动处理"
        problems=$((problems + 1))
    else
        ln -s "$src" "$dst"
        echo "  已创建  $dst -> $src"
    fi
}

echo "pixel4ai 项目目录：$ROOT"
chmod +x "$ROOT/bin/pxl" "$ROOT/bin/pxl-edit" "$ROOT/skills/pixel-art/scripts/pxl"
link "$ROOT/bin/pxl" "$BIN/pxl"
link "$ROOT/bin/pxl-edit" "$BIN/pxl-edit"
link "$ROOT/skills/pixel-art" "$SKILLS/pixel-art"

echo
echo "自检"
case ":$PATH:" in
    *":$BIN:"*) echo "  ✓ $BIN 在 PATH 里，终端里可以直接用 pxl / pxl-edit" ;;
    *) echo "  ! $BIN 不在 PATH 里：终端里要写全路径，或在 ~/.bashrc 加 export PATH=\"$BIN:\$PATH\"（AI 调用不受影响）" ;;
esac
if "$SKILLS/pixel-art/scripts/pxl" --help >/dev/null 2>&1; then
    echo "  ✓ Skill 入口 $SKILLS/pixel-art/scripts/pxl 可以运行（AI 用它调用，不依赖 PATH）"
else
    echo "  ✗ Skill 入口运行失败：$SKILLS/pixel-art/scripts/pxl --help"
    problems=$((problems + 1))
fi
if python3 -c 'import PyQt6' 2>/dev/null; then
    echo "  ✓ PyQt6 已安装，编辑器可用"
else
    echo "  - 没有 PyQt6：编辑器不可用（sudo apt install python3-pyqt6），命令行不受影响"
fi

echo
echo "注意：已经打开的 Claude Code 会话看不到新装的 Skill，请新开会话再让 AI 画图。"
exit $problems
