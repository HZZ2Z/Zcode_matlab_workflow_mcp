#!/usr/bin/env python3
"""PDF 课件工具：页面渲染 + 文本抽取（matlab-sim 工作流及通用）。

用法:
  python pdf_tools.py pages <pdf> <outdir> [dpi]        # 每页渲染为 outdir/page-NNN.png，打印 "PAGES <n>"
  python pdf_tools.py page  <pdf> <页码> <out.png> [dpi] # 只渲染一页（1-based）
  python pdf_tools.py text  <pdf> [起始页] [结束页]      # 打印页文本（1-based 闭区间，默认全部）
首次使用若缺 pymupdf 会自动 pip 安装。依赖：Python 3.8+。
"""
import os
import subprocess
import sys


def ensure_pymupdf():
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        print("pymupdf 未安装，自动安装中 ...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "pymupdf"])
        import pymupdf  # noqa: F401


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    cmd, pdf = sys.argv[1], sys.argv[2]
    ensure_pymupdf()
    import pymupdf

    doc = pymupdf.open(pdf)
    if cmd == "pages" and len(sys.argv) >= 4:
        outdir = sys.argv[3]
        dpi = int(sys.argv[4]) if len(sys.argv) > 4 else 150
        os.makedirs(outdir, exist_ok=True)
        for i, page in enumerate(doc):
            page.get_pixmap(dpi=dpi).save(os.path.join(outdir, f"page-{i + 1:03d}.png"))
        print(f"PAGES {len(doc)}")
    elif cmd == "page" and len(sys.argv) >= 5:
        n = int(sys.argv[3])
        out = sys.argv[4]
        dpi = int(sys.argv[5]) if len(sys.argv) > 5 else 200
        doc[n - 1].get_pixmap(dpi=dpi).save(out)
        print(f"OK {out}")
    elif cmd == "text":
        a = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        b = int(sys.argv[4]) if len(sys.argv) > 4 else len(doc)
        for i in range(a - 1, min(b, len(doc))):
            print(f"\n===== p.{i + 1} =====")
            print(doc[i].get_text())
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
