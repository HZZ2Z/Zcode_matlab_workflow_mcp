---
name: pdf-courseware
description: Use when an agent needs to read a PDF courseware/document page by page — render pages to PNG images (formulas, figures intact) and extract text per page. Created for the matlab-sim workflow but usable by any agent. Auto-installs pymupdf on first use. Also loadable as pdf-courseware.
---

# PDF 课件工具（pdf-courseware）

单文件工具：`pdf_tools.py`（与本文件同目录）。首次运行自动 `pip install pymupdf`，无需预装任何东西（只要机器有 Python 3.8+）。

## 命令

```bash
# 渲染全部页面 → outdir/page-001.png、page-002.png …，并打印总页数 "PAGES <n>"
python "<本skill目录>/pdf_tools.py" pages "<课件.pdf>" "<输出目录>" [dpi=150]

# 只渲染一页（题面看不清时用 dpi=200 重渲）
python "<本skill目录>/pdf_tools.py" page "<课件.pdf>" <页码> "<输出.png>" [dpi=200]

# 抽取页文本（1-based 闭区间；用于快速扫描找题，公式在文本里通常是乱码，以图像为准）
python "<本skill目录>/pdf_tools.py" text "<课件.pdf>" [起始页] [结束页]
```

## 给 agent 的要点

- **公式、矩阵、图必须看渲染出的 PNG**（用 Read 工具查看），文本层会丢失上下标和矩阵结构。
- 先用 `text` 扫描定位出题页，再对出题页看图——省钱省时。
- 图像默认 150dpi；看不清就 `page` 子命令单页 200dpi 重渲，不要重渲全本。
- matlab-sim 工作流依赖本 skill。本 skill 随 matlab-mcp 插件分发（仓库 `skills/pdf-courseware/`），单独分享时把整个 `pdf-courseware` 文件夹拷给对方，放进对方 `~/.zcode/skills/`。
