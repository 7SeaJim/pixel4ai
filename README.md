# pixel4ai — 给 AI 用的像素网格画板

画布被分成 W×H 个格子，**每格只能是一种颜色**。AI 用 `pxl` 命令画，人用编辑器看着渲染效果直接改，两边改的是同一个文件。

| 组成 | 给谁用 | 做什么 |
| --- | --- | --- |
| `pxl` 命令行 | AI（通过 Skill 调用） | 画点/线/矩形/椭圆、油漆桶、贴子网格、镜像……每次修改都回显带坐标尺的视图，方便核对 |
| `pxl 文件 edit` 编辑器 | 人 | 看着实际颜色点格子修改；AI 改了文件会自动刷新 |
| `skills/pixel-art` | AI | 工作流程、命令速查、像素画规则 |

工作原理和设计取舍见 [DESIGN.md](DESIGN.md)。

依赖：Python 3.10+。编辑器需要 PyQt6（Debian: `sudo apt install python3-pyqt6`），命令行不需要。

## 目录

```
pixel4ai/
├── bin/
│   ├── pxl              命令行入口（AI 用）
│   └── pxl-edit         编辑器入口（人用）
├── src/pixel4ai/
│   ├── core.py          画布数据模型、绘图操作、撤销历史
│   ├── render.py        坐标尺视图、PNG / ASCII / ANSI 导出
│   ├── cli.py           pxl 命令解析、批量脚本
│   └── editor.py        PyQt6 编辑器
├── skills/pixel-art/    给 AI 的 Skill
├── refs/dither/         像素抖动参考库：效果图 + 描述（AI 按需查询，不整库读入）
├── examples/            样例 .pxl 和导出的 PNG
├── docs/                README 用的截图
└── tests/               自测
```

## 安装

```bash
~/Projects/pixel4ai/install.sh
```

可以重复执行，已经装好的会跳过，被别的文件占用的位置不会覆盖。它会创建三个软链接，然后自检：

| 链接 | 用途 |
| --- | --- |
| `~/.local/bin/pxl` → `bin/pxl` | 终端里直接用命令行 |
| `~/.local/bin/pxl-edit` → `bin/pxl-edit` | 终端里直接打开编辑器 |
| `~/.claude/skills/pixel-art` → `skills/pixel-art` | 让 Claude Code 发现这个 Skill |

**装完要新开 Claude Code 会话。** Claude Code 在进程启动时扫描 `~/.claude/skills`，已经在跑的会话看不到新装的 Skill。后台任务用的是守护进程**预先启动**好的进程，所以安装之后立刻开的后台任务也可能看不到。2026-09-10 就遇到过：Skill 20:07 装好，20:13 开的后台会话用的是 19:23 就启动的进程，AI 不知道有这个 Skill，自己写脚本画图。遇到这种情况就再开一个新会话。

**AI 调用不需要你告诉它路径。** SKILL.md 里的命令写成 `${CLAUDE_SKILL_DIR}/scripts/pxl`，Claude Code 加载 Skill 时会把变量换成 Skill 的实际目录；入口脚本再顺着软链接找到项目，不依赖 PATH。如果 Skill 是复制安装的（不是软链接），设置环境变量 `PIXEL4AI_HOME=项目目录`。

## 快速开始

```bash
pxl slime.pxl new 16 16 --palette pico8
pxl slime.pxl ellipse 2 5 13 14 g --fill
pxl slime.pxl edit                          # 打开编辑器，边看边改
pxl slime.pxl export png --scale 16         # slime.png
pxl slime.pxl export ansi                   # 终端里直接看彩色效果
```

也可以直接跟 Claude 说「用 pixel-art 画一个 16×16 的史莱姆」，然后打开编辑器手动修；修完告诉它「我改了，继续」，它会先读最新文件再动手。

`examples/mushroom.pxl` 是按 Skill 流程画的样例：

![mushroom](examples/mushroom.png)

## 编辑器

![editor](docs/editor.png)

启动方式任选一种：
- `pxl 画.pxl edit` 或 `pxl edit 画.pxl`：文件不存在时会弹窗新建
- `pxl edit`：弹窗选择打开已有文件或新建
- `pxl-edit [画.pxl]`：不给文件同样会弹窗
- `python3 src/pixel4ai/editor.py [画.pxl]`：不安装也能直接运行

窗口里也能随时 Ctrl+N 新建、Ctrl+O 打开。

| 操作 | 效果 |
| --- | --- |
| 左键 / 拖动 | 用当前工具和颜色画 |
| 右键 / 拖动 | 擦成透明 |
| Alt + 左键 | 吸色 |
| B E G I L R O S | 铅笔 / 橡皮 / 油漆桶 / 吸管 / 直线 / 矩形 / 椭圆 / 选区 |
| 矩形、椭圆按住 Shift | 实心 |
| M | 左右对称绘制 |
| 选区 | 坐标 `(x0,y0)-(x1,y1)` 复制到剪贴板，贴给 AI 就能说「改这块」；Delete 清空 |
| 0-9 | 选调色板第 N 个颜色（0 是透明） |
| 双击色块 / 右键色块 | 改颜色 / 删除颜色 |
| Ctrl+滚轮、+ -、Ctrl+0 | 缩放、适应窗口 |
| Ctrl+Z、Ctrl+Shift+Z | 撤销、重做（和命令行共用同一个撤销栈） |

每一笔松开鼠标就保存。保存时先读磁盘上的最新内容再叠加这一笔，所以 AI 刚做的改动不会被覆盖。

## 文件格式 `.pxl`

纯 JSON，每行一个字符串、上下对齐，文件本身就能当 ASCII 画看，也方便 git diff：

```json
{
  "version": 1,
  "size": [6, 3],
  "palette": {
    ".": null,
    "k": "#111111",
    "r": "#d9434b"
  },
  "rows": [
    ".kkkk.",
    "krrrrk",
    ".kkkk."
  ]
}
```

- `.` 是透明，固定在调色板里。调色板字符可以是单个字母、数字或 `#@%&*+=~^$:;<>|/!`。
- 坐标 `(x, y)` 从 0 开始，原点在左上角，区域两端都包含。
- 画布宽高 1..1024。超过 64 行或列时，`view` 和修改后的回显自动变成缩略视图（`--region` 看逐格细节）；编辑器只绘制可见区域，坐标尺吸附在视野边上。
- 撤销历史存在旁边的隐藏目录 `.文件名.pxl.history/`，每步一个压缩快照，最多 50 步。

## 命令

`pxl -h` 看完整列表，常用命令和批量脚本 `apply` 的写法见 [skills/pixel-art/SKILL.md](skills/pixel-art/SKILL.md)。

查参考库（不需要画布文件）：

```bash
pxl ref                           # 有哪些参考库
pxl ref dither                    # 抖动：按场景选择 + 规则 + 反例
pxl ref dither reference-strip    # 单个条目：做法、禁忌、预览图路径（--full 看全部）
pxl ref dither --find 光晕        # 按关键词找条目和场景
pxl ref dither --check            # 校验 catalog.json 和它引用的文件
```

参考库的内容在 [refs/dither/](refs/dither/README.md)。

批量脚本是原子操作：任意一行出错，整个文件保持不变，并告诉你是第几行、为什么错。

## 自测

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests
```

覆盖：文档校验、各种绘图操作的形状、越界和调色板报错、撤销重做、PNG 解码、批量脚本的原子性、命令行里的 `#` 字符。编辑器部分会离屏模拟鼠标画一笔，再模拟外部修改文件，确认编辑器自动刷新、自己的一笔不会覆盖外部改动，最后测试撤销。

## 待办

按优先级整理的待实现功能见 [TODO.md](TODO.md)。
