#!/usr/bin/env bash
# 安装 / 卸载 / 自检 pixel4ai：命令行 pxl、编辑器 pxl-edit、Claude Code Skill pixel-art。
#
#   ./install.sh               安装（可重复执行；已装好的跳过，被别的文件占用的位置不覆盖）
#   ./install.sh --uninstall   卸载：只删除指向本项目的软链接，不动项目文件
#   ./install.sh --help        显示本说明
#
# 安装位置可用环境变量修改：XDG_BIN_HOME（默认 ~/.local/bin）、CLAUDE_CONFIG_DIR（默认 ~/.claude）

if [ -z "${BASH_VERSION:-}" ]; then
    echo "请用 bash 运行：bash install.sh（或 ./install.sh）" >&2
    exit 1
fi
set -uo pipefail

mode=install
case "${1:-}" in
    "" | --install) ;;
    --uninstall | uninstall) mode=uninstall ;;
    -h | --help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1（用 ./install.sh --help 查看用法）" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
SKILLS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
problems=0

if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ 找不到 python3。请先安装 Python 3.10 或更高版本（见 README「安装 → 1. 环境要求」）。" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "✗ python3 的版本是 $(python3 -c 'import platform; print(platform.python_version())')，pixel4ai 需要 3.10 或更高。" >&2
    exit 1
fi

# 用 python3 解析真实路径：macOS 较老的 readlink 不支持 -f
realpath_() { python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
points_here() { [ -L "$1" ] && [ "$(realpath_ "$1")" = "$(realpath_ "$2")" ]; }

link() {
    local src=$1 dst=$2
    mkdir -p "$(dirname "$dst")"
    if points_here "$dst" "$src"; then
        echo "  已存在  $dst"
    elif [ -L "$dst" ] && [ ! -e "$dst" ]; then
        # 失效的软链接（通常是项目目录被移动或重新克隆过），直接指向新位置
        ln -sfn "$src" "$dst"
        echo "  已修复  $dst -> $src（原链接已失效）"
    elif [ -e "$dst" ] || [ -L "$dst" ]; then
        echo "  ✗ 跳过  $dst 已被占用（不是指向本项目的软链接）。确认不需要后删除它，再运行一次 ./install.sh"
        problems=$((problems + 1))
    else
        ln -s "$src" "$dst"
        echo "  已创建  $dst -> $src"
    fi
}

unlink_() {
    local src=$1 dst=$2
    if points_here "$dst" "$src"; then
        rm "$dst"
        echo "  已删除  $dst"
    elif [ -e "$dst" ] || [ -L "$dst" ]; then
        echo "  保留    $dst（不是指向本项目的软链接）"
    else
        echo "  不存在  $dst"
    fi
}

TARGETS=(
    "$ROOT/bin/pxl|$BIN/pxl"
    "$ROOT/bin/pxl-edit|$BIN/pxl-edit"
    "$ROOT/skills/pixel-art|$SKILLS/pixel-art"
)

echo "pixel4ai 项目目录：$ROOT"

if [ "$mode" = uninstall ]; then
    for t in "${TARGETS[@]}"; do unlink_ "${t%%|*}" "${t#*|}"; done
    echo
    echo "卸载完成。项目目录没有删除，不再需要的话可以直接删掉：$ROOT"
    exit 0
fi

chmod +x "$ROOT/bin/pxl" "$ROOT/bin/pxl-edit" "$ROOT/skills/pixel-art/scripts/pxl"
for t in "${TARGETS[@]}"; do link "${t%%|*}" "${t#*|}"; done

echo
echo "自检"
case ":$PATH:" in
    *":$BIN:"*)
        echo "  ✓ $BIN 在 PATH 里：终端里可以直接输入 pxl、pxl-edit"
        ;;
    *)
        echo "  ! $BIN 不在 PATH 里：终端里暂时要输入完整路径 $BIN/pxl（AI 调用不受影响）"
        shell_name=$(basename "${SHELL:-bash}")
        if [ "$shell_name" = fish ]; then
            echo "    修复：执行一次  fish_add_path $BIN  然后重开终端"
        else
            if [ "$shell_name" = zsh ]; then rc='~/.zshrc'
            elif [ "$(uname)" = Darwin ]; then rc='~/.bash_profile'
            else rc='~/.bashrc'; fi
            echo "    修复：执行一次  echo 'export PATH=\"$BIN:\$PATH\"' >> $rc  然后重开终端"
        fi
        ;;
esac
if "$SKILLS/pixel-art/scripts/pxl" --help >/dev/null 2>&1; then
    echo "  ✓ 命令行可以运行（AI 通过 $SKILLS/pixel-art/scripts/pxl 调用，不依赖 PATH）"
else
    echo "  ✗ 命令行运行失败，手动执行看报错：$SKILLS/pixel-art/scripts/pxl --help"
    problems=$((problems + 1))
fi
if "$SKILLS/pixel-art/scripts/pxl" ref dither --check >/dev/null 2>&1; then
    echo "  ✓ 抖动参考库完整"
else
    echo "  ! 抖动参考库校验没通过，执行看详情：pxl ref dither --check（不影响画图）"
fi
if python3 -c 'import PyQt6.QtWidgets' 2>/dev/null; then
    echo "  ✓ PyQt6 已安装，编辑器可用"
else
    echo "  - 没有 PyQt6：编辑器暂时不能用，命令行和 AI 不受影响（安装方法见 README「安装 → 3. 安装 PyQt6」）"
fi
if command -v claude >/dev/null 2>&1; then
    echo "  ✓ 找到 Claude Code"
else
    echo "  - 没找到 claude 命令：只用命令行和编辑器的话可以忽略"
fi

echo
if [ "$problems" -eq 0 ]; then
    echo "安装完成。"
    echo "  · 终端：新开一个终端，输入 pxl --help"
    echo "  · Claude Code：必须新开会话（已经打开的会话看不到新装的 Skill），输入 /skills 应能看到 pixel-art"
else
    echo "有 $problems 个问题需要处理，见上面标 ✗ 的行。"
fi
exit "$problems"
