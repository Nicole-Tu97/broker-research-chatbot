"""对基准测试页跑转录（多档位），产出待评分的转录结果。

Responses API + detail=original（与生产配置一致，见 ARCHITECTURE.md §7）。
零第三方依赖（urllib），与 providers.py 的"外部调用集中一处"哲学一致。

前置：
    export $(grep -v '^#' .env | xargs)        # OPENAI_API_KEY / OPENAI_VISION_MODEL / OPENAI_IMAGE_DETAIL
    python3 bench/render_pages.py --tiers      # 先渲染多档位

用法：
    python3 bench/run_bench.py --tiers                  # 全部页 × 全部档位（§8.1 全量）
    python3 bench/run_bench.py --tiers --only A3 D1     # 指定页
    python3 bench/run_bench.py --tiers --cat pure_image # 指定类别
    python3 bench/run_bench.py                          # 单档位（bench/out/，legacy）

产物：
    bench/out_tiers/<dpi>/<id>.transcript.md   各档位转录结果
    bench/out_tiers/_usage.json                逐次 token 用量与耗时
    （单档位模式产物在 bench/out/ 下，同名）

评分：对照 bench/ground_truth.md 逐项核对，标准见 bench/pages.json 的 criteria。
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

PRICE_IN, PRICE_OUT = 5.0, 30.0  # $/1M tokens，Sol 假设价（§14：官方定价待核对）

# ---- 转录 prompt v3（与 research/providers.py 逐字一致，那里是唯一真源） ----

SYSTEM = """你是金融文档转录引擎。你的输出会成为券商研报检索系统中该页的唯一文本表示。

绝对约束：
1. 不得输出页面上不存在的任何信息。宁可遗漏，不可编造。
2. 转录中的每一个数字都必须能在页面上找到。你不做任何计算、推断或换算。
3. 数字保留页面上的原始写法：千分位逗号、货币符号、百分号、尾零一律照抄，不得重新格式化。
4. 若某字符/数字辨认不清，写 [?]，不要猜测。

理由：本系统会带页码引用地向分析师呈现你的转录内容。一个错误的数字会被自信地引用，
且无任何纠错路径；而一处遗漏可以由原始页面图兜底。"""

USER_TEMPLATE = """以下是一页券商研报/演示文稿。

<raw_text>
{raw_text}
</raw_text>

<说明>
raw_text 是从 PDF 精确抽取的原生文本层，逐字准确。
若其为空或极短，说明该页内容以图像形式存在，此时完全依据页面图转录。
</说明>

请产出该页的 markdown 转录，遵循：

【正文】
- raw_text 非空时，正文直接采用其内容，仅整理段落与标题层级。
- 不要从图像中重新读取已存在于 raw_text 的文字。

【表格】
- 用 markdown 表格重建，保留表头层级与行标签。
- 单元格数值必须落在正确的行列位置。
- 每行的单元格数必须与表头列数一致；原文的空单元格保留为空——不得填 0、不得左右移位补齐。
- 合并单元格用重复值或空单元格表示，勿丢弃结构。

【图表】
每个图表输出一个区块：标题、图表类型、坐标轴（名称与单位）、
数据系列（名称 + 可读出的关键数值：首尾端点、极值、有数据标签者）、趋势的一句话描述。
读不出具体数值时，描述形状与相对关系，不要编造数字。

【忽略】
- 页眉页脚、页码、法律免责声明
- 水印（常见形式：斜向或竖排的邮箱、机构名、时间戳）

【输出格式】
先输出一行元数据，再输出转录正文：

HAS_VISUAL: true|false
（true 表示本页含图表、图片或表格，需要在检索时向模型回传原始页面图）

---

（转录正文）"""


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
    except Exception as exc:  # 单次失败不中断整批
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
    ap.add_argument("--only", nargs="*", help="只跑指定页 id，如 A3 D1")
    ap.add_argument("--cat", help="只跑指定类别")
    ap.add_argument("--tiers", action="store_true", help="跑 out_tiers/ 下的全部档位")
    ap.add_argument("--model", default=os.environ.get("OPENAI_VISION_MODEL"))
    ap.add_argument("--detail", default=os.environ.get("OPENAI_IMAGE_DETAIL", "original"))
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("错误：未设置 OPENAI_API_KEY（export $(grep -v '^#' .env | xargs)）", file=sys.stderr)
        return 2
    if not args.model:
        print("错误：未设置 OPENAI_VISION_MODEL（或用 --model 指定），见 ARCHITECTURE.md §7", file=sys.stderr)
        return 2

    pages = json.loads((BENCH / "pages.json").read_text(encoding="utf-8"))["pages"]
    if args.only:
        pages = [p for p in pages if p["id"] in set(args.only)]
    if args.cat:
        pages = [p for p in pages if p["cat"] == args.cat]
    if not pages:
        print("没有匹配的页面", file=sys.stderr)
        return 2

    # 组装 (页, 档位, 图目录) 任务列表
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
        print("错误：找不到渲染图。先运行 python3 bench/render_pages.py" +
              (" --tiers" if args.tiers else ""), file=sys.stderr)
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
    # 按 (id, dpi) 合并进已有记录，部分重跑不清空整份日志
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
        print(f"\n{len(done)}/{len(tasks)} 次完成 | in={tin:,} out={tout:,} tokens | ~${cost:.2f}")
        if args.tiers:
            print("\n档位小结（in tokens 均值 / 页）：")
            by = {}
            for r in done:
                by.setdefault((r["dpi"], r["cat"]), []).append(r["input_tokens"])
            for (dpi, cat), vals in sorted(by.items()):
                print(f"  dpi={dpi:3d} {cat:14s} n={len(vals)} avg_in={sum(vals)//len(vals):6,d}")
    print(f"\n转录结果 → {(OUT_TIERS if args.tiers else OUT)}/<dpi>/<id>.transcript.md")
    print(f"下一步：对照 {BENCH}/ground_truth.md 评分（标准见 pages.json criteria）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())