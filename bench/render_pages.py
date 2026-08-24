"""渲染基准测试页为 PNG，并导出原生文本层。

用法：
    python3 bench/render_pages.py [--dpi 150]   # 单档位 → bench/out/
    python3 bench/render_pages.py --tiers       # 多档位矩阵 → bench/out_tiers/<dpi>/

多档位矩阵：keynote 类跑 4 档找质量下限；研报类跑 2 档验证 150 是否可降。
文本层与 DPI 无关，只在 bench/out/ 存一份。

产物：
    bench/out/<id>.png                单档位渲染图
    bench/out/<id>.txt                PDF 原生文本层（可能为空，如 GTC keynote）
    bench/out_tiers/<dpi>/<id>.png    多档位渲染图
"""

import argparse
import json
import pathlib
import sys

import pymupdf

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "bench" / "pages.json"
OUT = ROOT / "bench" / "out"
OUT_TIERS = ROOT / "bench" / "out_tiers"
CORPUS = ROOT / "case_study"

# 多档位矩阵：类别 → DPI 列表
# keynote(40×22.5″)：150 为已验证的质量上限档，52 对应"幻灯片按字高归一"的理论档
# 研报(letter/A4)：150 为按 ~7pt 正文推导的生产档，100 测试可否再降
TIER_MATRIX = {
    "pure_image":    [150, 100, 72, 52],
    "vector_chart":  [150, 100],
    "dense_table":   [150, 100],
    "rating_action": [150, 100],
}


def render_one(spec, dpi, out_dir, write_text=False):
    pdf_path = CORPUS / spec["file"]
    if not pdf_path.exists():
        raise FileNotFoundError(spec["file"])
    with pymupdf.open(pdf_path) as doc:
        page = doc[spec["page"] - 1]  # manifest 用 1-indexed
        pix = page.get_pixmap(dpi=dpi)
        pix.save(out_dir / f"{spec['id']}.png")
        if write_text:
            (OUT / f"{spec['id']}.txt").write_text(page.get_text().strip(), encoding="utf-8")
    return pix.width, pix.height


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--tiers", action="store_true", help="按 TIER_MATRIX 渲染全部档位")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)

    # PyMuPDF 对损坏的 XObject 引用会写 stderr 噪声；语料中确实存在此类文件
    pymupdf.TOOLS.mupdf_display_errors(False)

    ok = failed = 0
    for spec in manifest["pages"]:
        dpis = TIER_MATRIX[spec["cat"]] if args.tiers else [args.dpi]
        for dpi in dpis:
            out_dir = (OUT_TIERS / str(dpi)) if args.tiers else OUT
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                # 文本层只需一份（与 DPI 无关）
                w, h = render_one(spec, dpi, out_dir,
                                  write_text=not (OUT / f"{spec['id']}.txt").exists())
            except Exception as exc:  # 单页失败不应中断整批 — 与摄取管线同一原则
                print(f"[FAIL] {spec['id']}@{dpi}: {exc}", file=sys.stderr)
                failed += 1
                continue
            print(f"[ OK ] {spec['id']:3s} {spec['cat']:14s} dpi={dpi:3d} {w:4d}x{h:<4d}")
            ok += 1

    print(f"\n渲染 {ok} 张成功，{failed} 张失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
