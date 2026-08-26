"""Run transcription on the benchmark pages (multi-tier), producing transcripts to grade.

Responses API + detail=original (matches the production config).
Zero third-party dependencies (urllib), consistent with the providers.py
philosophy of keeping all external calls in one place.

Prerequisites:
    export $(grep -v '^#' .env | xargs)   # OPENAI_API_KEY / OPENAI_VISION_MODEL / OPENAI_IMAGE_DETAIL
    python3 bench/render_pages.py --tiers      # render the tiers first

Usage:
    python3 bench/run_bench.py --tiers                  # all pages x all tiers (full matrix)
    python3 bench/run_bench.py --tiers --only A3 D1     # specific pages
    python3 bench/run_bench.py --tiers --cat pure_image # specific category
    python3 bench/run_bench.py                          # single tier (bench/out/, legacy)

Outputs:
    bench/out_tiers/<dpi>/<id>.transcript.md   per-tier transcripts
    bench/out_tiers/_usage.json                per-call token usage and timing
    (single-tier mode writes the same filenames under bench/out/)

Grading: check each item against bench/ground_truth.md; criteria are in bench/pages.json.
"""

import argparse
import base64
import concurrent.futures
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
OUT = BENCH / "out"
OUT_TIERS = BENCH / "out_tiers"

PRICE_IN, PRICE_OUT = 5.0, 30.0  # $/1M tokens, assumed Sol pricing (official rates unverified)

# ---- Transcription prompt v3: imported from research/providers.py (single source of truth) ----

# The benchmark must exercise the exact production prompt: import it instead of
# carrying a copy that could drift.
sys.path.insert(0, str(ROOT))
from research.providers import TRANSCRIBE_SYSTEM as SYSTEM, TRANSCRIBE_USER as USER_TEMPLATE  # noqa: E402


def call_responses(model, detail, raw_text, png_bytes):
    payload = {
        "model": model,
        "instructions": SYSTEM,
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": USER_TEMPLATE.format(raw_text=raw_text or "(空)")},
            {"type": "input_image", "detail": detail,
             "image_url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()},
        ]}],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        res = json.load(r)
    text = " ".join(c.get("text", "")
                    for o in res.get("output", []) if o.get("type") == "message"
                    for c in o.get("content", []))
    u = res.get("usage", {})
    return text, u.get("input_tokens", 0), u.get("output_tokens", 0)


def run_one(spec, dpi, png_dir, model, detail):
    pid = spec["id"]
    png = (png_dir / f"{pid}.png").read_bytes()
    raw_text = (OUT / f"{pid}.txt").read_text(encoding="utf-8")
    t0 = time.time()
    try:
        text, tin, tout = call_responses(model, detail, raw_text, png)
    except urllib.error.HTTPError as exc:
        return {"id": pid, "dpi": dpi, "error": f"HTTP {exc.code}: {exc.read().decode()[:200]}"}
    except Exception as exc:  # a single failure must not abort the batch
        return {"id": pid, "dpi": dpi, "error": str(exc)}
    (png_dir / f"{pid}.transcript.md").write_text(text, encoding="utf-8")
    m = re.search(r"HAS_VISUAL:\s*(true|false)", text, re.I)
    return {"id": pid, "dpi": dpi, "cat": spec["cat"], "model": model,
            "input_tokens": tin, "output_tokens": tout,
            "seconds": round(time.time() - t0, 1),
            "has_visual": m.group(1).lower() if m else None,
            "png_bytes": len(png)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="run only these page ids, e.g. A3 D1")
    ap.add_argument("--cat", help="run only this category")
    ap.add_argument("--tiers", action="store_true", help="run every tier under out_tiers/")
    ap.add_argument("--model", default=os.environ.get("OPENAI_VISION_MODEL"))
    ap.add_argument("--detail", default=os.environ.get("OPENAI_IMAGE_DETAIL", "original"))
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY not set (export $(grep -v '^#' .env | xargs))", file=sys.stderr)
        return 2
    if not args.model:
        print("Error: OPENAI_VISION_MODEL not set (or pass --model)", file=sys.stderr)
        return 2

    pages = json.loads((BENCH / "pages.json").read_text(encoding="utf-8"))["pages"]
    if args.only:
        pages = [p for p in pages if p["id"] in set(args.only)]
    if args.cat:
        pages = [p for p in pages if p["cat"] == args.cat]
    if not pages:
        print("No matching pages", file=sys.stderr)
        return 2

    # Build the (page, tier, image dir) task list
    tasks = []
    if args.tiers:
        for dpi_dir in sorted(OUT_TIERS.glob("[0-9]*")):
            dpi = int(dpi_dir.name)
            for spec in pages:
                if (dpi_dir / f"{spec['id']}.png").exists():
                    tasks.append((spec, dpi, dpi_dir))
    else:
        tasks = [(spec, None, OUT) for spec in pages
                 if (OUT / f"{spec['id']}.png").exists()]
    if not tasks:
        print("Error: no rendered images found. Run python3 bench/render_pages.py" +
              (" --tiers" if args.tiers else "") + " first", file=sys.stderr)
        return 2

    usage_log = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_one, s, d, pdir, args.model, args.detail): (s, d)
                for s, d, pdir in tasks}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            usage_log.append(r)
            if "error" in r:
                print(f"[FAIL] {r['id']}@{r['dpi']}: {r['error']}", file=sys.stderr)
            else:
                print(f"[ OK ] {r['id']:3s} dpi={str(r['dpi']):>4s} in={r['input_tokens']:6d} "
                      f"out={r['output_tokens']:5d} {r['seconds']:5.1f}s "
                      f"has_visual={r['has_visual'] or '??'}")

    log_path = (OUT_TIERS if args.tiers else OUT) / "_usage.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Merge into existing records by (id, dpi) so a partial rerun doesn't wipe the whole log
    merged = {}
    if log_path.exists():
        for r in json.loads(log_path.read_text(encoding="utf-8")):
            merged[(r["id"], r.get("dpi"))] = r
    for r in usage_log:
        merged[(r["id"], r.get("dpi"))] = r
    out_log = sorted(merged.values(), key=lambda r: (str(r.get("dpi")), r["id"]))
    log_path.write_text(json.dumps(out_log, indent=2, ensure_ascii=False), encoding="utf-8")

    done = [r for r in usage_log if "error" not in r]
    if done:
        tin = sum(r["input_tokens"] for r in done)
        tout = sum(r["output_tokens"] for r in done)
        cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
        print(f"\n{len(done)}/{len(tasks)} calls done | in={tin:,} out={tout:,} tokens | ~${cost:.2f}")
        if args.tiers:
            print("\nTier summary (mean input tokens / page):")
            by = {}
            for r in done:
                by.setdefault((r["dpi"], r["cat"]), []).append(r["input_tokens"])
            for (dpi, cat), vals in sorted(by.items()):
                print(f"  dpi={dpi:3d} {cat:14s} n={len(vals)} avg_in={sum(vals)//len(vals):6,d}")
    print(f"\nTranscripts → {(OUT_TIERS if args.tiers else OUT)}/<dpi>/<id>.transcript.md")
    print(f"Next: grade against {BENCH}/ground_truth.md (criteria in pages.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())