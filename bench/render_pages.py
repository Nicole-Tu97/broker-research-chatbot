"""Render the benchmark pages to PNG and export the native text layer.

Usage:
    python3 bench/render_pages.py [--dpi 150]   # single tier → bench/out/
    python3 bench/render_pages.py --tiers       # multi-tier matrix → bench/out_tiers/<dpi>/

Tier matrix: keynote pages run 4 tiers to find the quality floor; research-report
pages run 2 tiers to check whether 150 can be lowered.
The text layer is DPI-independent, so a single copy is kept in bench/out/.

Outputs:
    bench/out/<id>.png                single-tier render
    bench/out/<id>.txt                native PDF text layer (may be empty, e.g. GTC keynote)
    bench/out_tiers/<dpi>/<id>.png    multi-tier renders
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

# Tier matrix: category → list of DPIs
# keynote (40×22.5″): 150 is the validated quality ceiling; 52 is the theoretical
#   tier from normalizing slides by text height
# reports (letter/A4): 150 is the production tier derived from ~7pt body text;
#   100 tests whether it can go lower
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
        page = doc[spec["page"] - 1]  # manifest is 1-indexed
        pix = page.get_pixmap(dpi=dpi)
        pix.save(out_dir / f"{spec['id']}.png")
        if write_text:
            (OUT / f"{spec['id']}.txt").write_text(page.get_text().strip(), encoding="utf-8")
    return pix.width, pix.height


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--tiers", action="store_true", help="render every tier per TIER_MATRIX")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)

    # PyMuPDF writes stderr noise on broken XObject refs; the corpus does contain such files
    pymupdf.TOOLS.mupdf_display_errors(False)

    ok = failed = 0
    for spec in manifest["pages"]:
        dpis = TIER_MATRIX[spec["cat"]] if args.tiers else [args.dpi]
        for dpi in dpis:
            out_dir = (OUT_TIERS / str(dpi)) if args.tiers else OUT
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                # Only one text layer is needed (DPI-independent)
                w, h = render_one(spec, dpi, out_dir,
                                  write_text=not (OUT / f"{spec['id']}.txt").exists())
            except Exception as exc:  # one bad page must not abort the batch — same rule as ingestion
                print(f"[FAIL] {spec['id']}@{dpi}: {exc}", file=sys.stderr)
                failed += 1
                continue
            print(f"[ OK ] {spec['id']:3s} {spec['cat']:14s} dpi={dpi:3d} {w:4d}x{h:<4d}")
            ok += 1

    print(f"\nRendered {ok} pages OK, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
