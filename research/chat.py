"""对话循环（ARCHITECTURE.md §6.3）：标准 function calling，两个工具。

- 语料边界注入：启动时从库里算（覆盖区间、券商、篇数），事实进 system。
- 消息持久化只存图像引用；构造请求时仅当前轮工具结果 rehydrate 原图
  （§6.0，DECISION-LOG §七.7）。
- 回答后处理全部为确定性纯函数：溯源徽章（数字 ↔ 被引页）、时效性标签
  （同券商同 ticker 是否有更新报告）、成本脚注（usage 累计）。零额外 API 调用。
"""

import base64
import json
import re
import time

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from . import providers, tools
from .models import Conversation, Document, Page
from .numeric import numbers_in

PRICE_IN, PRICE_OUT = 5.0, 30.0  # $/1M，Sol 假设价（§14）
MAX_TOOL_ROUNDS = 6

BEHAVIOR_RULES = """
Answer rules (non-negotiable):
1. Corpus boundary: answer ONLY from reports in the knowledge base. Never
   extrapolate or fabricate beyond it. Two distinct cases:
   - PARTIAL overlap: the asked scope overlaps coverage only partly (e.g. "the past
     two years" while the corpus covers a few months inside them) -> declare the
     true window first, then ANSWER FULLY for the covered part.
   - ZERO overlap, NO SCOPE SUBSTITUTION: the asked scope has no overlap at all
     (a year entirely outside the window; a broker or ticker absent from the
     corpus) -> state the boundary and STOP. Do not volunteer data from a
     different scope ("if you meant 2025..."). You may offer, in one sentence,
     to answer for what IS covered — provide it only if the user asks.
2. Comparative / temporal / numeric questions: lead with a markdown table (e.g.
   Broker | Date | Rating | Price target | Prior), a citation at the end of each row,
   then a short synthesis paragraph.
3. Never blend numbers across brokers: different brokers' estimates must be shown
   side by side — no averaging, no silently picking one. Every number must trace to a
   specific broker, date, and page.
4. Citation format: [broker, date, p.N] inline; numbers must come from the cited page.
5. If page 1 lacks the needed value, follow the recovery path the tools suggest
   (ticker_hit_pages / filtered search_pages).
6. When the question names a specific document (e.g. "the keynote", a broker's report
   of a given date, a deck): first LOCATE that document via metadata (list_reports, or
   search_pages with a broker filter; NVIDIA's own decks have broker "NVIDIA"), then
   take numbers only from it — never substitute similar figures from other sources.
7. Language: reply in the language the user asked in; default to English.
"""


def corpus_boundary() -> str:
    """启动时算一次的语料边界事实（§6.3 规则 1 的数据侧）。"""
    docs = Document.objects.filter(status=Document.Status.DONE)
    n = docs.count()
    if not n:
        return "The knowledge base is currently empty."
    lo = docs.exclude(published_date=None).order_by("published_date").first()
    hi = docs.exclude(published_date=None).order_by("-published_date").first()
    per_broker: dict[str, int] = {}
    for b in docs.values_list("broker", flat=True):
        per_broker[b] = per_broker.get(b, 0) + 1
    brokers = ", ".join(f"{b} ({c})" for b, c in sorted(per_broker.items()))
    return (f"Knowledge base: {n} reports covering {lo.published_date} to "
            f"{hi.published_date}. Brokers: {brokers}.")


def system_prompt() -> str:
    return (
        "You are a broker-research Q&A assistant for an asset-management team. "
        "Analysts use you to get cited answers across reports, brokers, and time. "
        "You have two retrieval tools; pages containing visuals come back with their "
        "original page image — verify transcriptions against the image when present."
        "\n\n" + corpus_boundary() + "\n" + BEHAVIOR_RULES
    )


def _png_b64(png_path: str) -> str | None:
    f = settings.PAGE_ASSET_DIR / png_path
    if not f.exists():
        return None
    return base64.b64encode(f.read_bytes()).decode()


def _tool_output_items(call_id: str, payload: dict,
                       sent_images: set | None = None) -> tuple[dict, dict]:
    """(带图的 API item, 只带引用的存储 item)。原图只进当前轮请求，不进历史。

    sent_images：本轮已附过图的 (document_id, page_number) 集合。轮内 api_input
    是累积的——第一次附的图在后续每轮上下文里都还在，同页重附纯属烧钱
    （实测一次失败回合 115 次附图仅 37 张不同页，§十七）。"""
    slim = json.dumps(payload, ensure_ascii=False)
    image_refs = []
    content: list[dict] = [{"type": "input_text", "text": slim}]
    for r in payload.get("results", []) or [r["first_page"] for r in payload.get("reports", []) if r.get("first_page")]:
        if r and r.get("has_visual") and r.get("png_path"):
            key = (r["document_id"], r["page_number"])
            if sent_images is not None:
                if key in sent_images:
                    continue  # 本轮上下文里已有这张原图
                sent_images.add(key)
            b64 = _png_b64(r["png_path"])
            if b64:
                content.append({"type": "input_image", "detail": settings.OPENAI_IMAGE_DETAIL,
                                "image_url": f"data:image/png;base64,{b64}"})
                image_refs.append({"document_id": r["document_id"],
                                   "page_number": r["page_number"],
                                   "png_path": r["png_path"]})
    api_item = {"type": "function_call_output", "call_id": call_id, "output": content}
    store_item = {"type": "function_call_output", "call_id": call_id,
                  "output_text": slim, "image_refs": image_refs}
    return api_item, store_item


def _history_to_api(messages: list[dict]) -> list[dict]:
    """历史消息 → API input：只保留 user/assistant 消息。

    工具流量（function_call / 其结果 / reasoning）不跨轮回传：推理模型要求
    function_call 与其 reasoning item 成对出现，而合成答案已携带结论、
    页面可随时通过工具重取（§6.0 的动机——历史图像不重放——推广到全部工具流量）。
    完整工具流量仍持久化在 Conversation.messages 里供审计与 UI。"""
    return [m for m in messages if m.get("role") in ("user", "assistant")]


_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
# 日期接受多种表面形式：模型不总输出 ISO（实测写过 "September 2025"），解析层
# 必须比 prompt 的措辞更宽——引用解析失败的代价是链接/徽章/Sources 全部消失。
_MONTH = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
          r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
          r"Nov(?:ember)?|Dec(?:ember)?")
_DATE = (r"\d{4}-\d{2}-\d{2}"
         rf"|(?:{_MONTH})\.?\s+\d{{1,2}},?\s+\d{{4}}"
         rf"|(?:{_MONTH})\.?\s+\d{{4}}"
         r"|n\.d\."
         r"|\d{4}")
CITATION_RE = re.compile(rf"(.+?),\s*({_DATE}),\s*p\.?\s*(\d+)", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _date_prefix(date_s: str) -> str | None:
    """引用里的日期表面形式 → ISO 前缀（与 published_date 做前缀比对）。

    "2025-09-05" → "2025-09-05"；"September 2025" → "2025-09"；"2025" → "2025"；
    "n.d." 或解析不出 → None（不按日期过滤——broker+页号+本轮检索集仍是硬门）。"""
    s = date_s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) or re.fullmatch(r"\d{4}", s):
        return s
    m = re.fullmatch(rf"({_MONTH})\.?\s+(?:(\d{{1,2}}),?\s+)?(\d{{4}})", s,
                     re.IGNORECASE)
    if m:
        month = _MONTHS[m.group(1)[:3].lower()]
        day = m.group(2)
        return (f"{m.group(3)}-{month:02d}-{int(day):02d}" if day
                else f"{m.group(3)}-{month:02d}")
    return None


def _citation_fragments(answer: str):
    """支持复合引用 [A, date, p.1; B, date, p.3]：按 ';' 拆分逐条解析。"""
    for bm in _BRACKET_RE.finditer(answer):
        for frag in bm.group(1).split(";"):
            m = CITATION_RE.fullmatch(frag.strip())
            if m:
                yield f"[{frag.strip()}]", m


def grounding_badges(answer: str, turn_pages: dict[tuple, Page]) -> list[dict]:
    """溯源徽章（§6.3）：答案里的每条引用 → 数字是否都在被引页上。纯函数。"""
    answer_nums = set(numbers_in(answer))
    badges = []
    for label, m in _citation_fragments(answer):
        broker_frag, date_s, page_no = m.group(1).strip(), m.group(2), int(m.group(3))
        # 防伪造的硬门 = 该页必须在本轮检索结果里（broker+页号）。日期是附加校验：
        # 仅当文档本身有日期时才比对（ds="None" 即无日期——如 NVIDIA 自家 deck，
        # 文件名与首页都无日期可解析；此时模型引用里的日期无从核对，不作为否决项）。
        want = _date_prefix(date_s)
        page = next((p for (b, ds, n), p in turn_pages.items()
                     if broker_frag.lower() in b.lower() and n == page_no
                     and (want is None or ds in ("None", "")
                          or ds.startswith(want))), None)
        if page is None:
            badges.append({"citation": label, "status": "unknown",
                           "note": "cited page not among this turn's retrieved results"})
            continue
        page_nums = set(numbers_in(f"{page.raw_text}\n{page.markdown or ''}"))
        # 只核对"既在答案里、又落在这条引用附近语境"过重——取保守口径：
        # 答案数字 ∩ 页面数字 ≥ 1 且无"答案独有且被归于此页"的能力做不到纯函数判定，
        # 徽章口径 = 该页数字与答案数字有交集 → 绿；无交集 → 警示（可能是纯定性引用）
        overlap = answer_nums & page_nums
        badges.append({
            "citation": label,
            "document_id": page.document_id,
            "page_number": page.page_number,
            "png_path": page.png_path,
            "has_visual": page.has_visual,
            "status": "grounded" if overlap or not answer_nums else "no_numeric_overlap",
            "matched_numbers": sorted(overlap)[:8],
            "suspect_numbers": page.numeric_flags or [],
        })
    return badges


FIGURE_CROP_CAP = 2      # 每回答最多定位 2 张图（约 $0.025/张，控制成本与延迟）
_CROP_PAD = 2.0          # 外扩 2%：宁多裁一圈，不切掉轴标签


def _valid_crop(box) -> dict | None:
    """bbox 的确定性校验与外扩（§十八）。纯函数。

    拒绝：坐标缺失/越界/退化、面积 <8%（可能定位错）或 >85%（等于整页，
    直接回退整页更诚实）。通过 → 外扩 _CROP_PAD 并夹回 [0,100]。"""
    try:
        x0, y0 = float(box["x0"]), float(box["y0"])
        x1, y1 = float(box["x1"]), float(box["y1"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100):
        return None
    area = (x1 - x0) * (y1 - y0) / 100.0
    if not (8 <= area <= 85):
        return None
    return {"x0": round(max(0, x0 - _CROP_PAD), 1), "y0": round(max(0, y0 - _CROP_PAD), 1),
            "x1": round(min(100, x1 + _CROP_PAD), 1), "y1": round(min(100, y1 + _CROP_PAD), 1)}


def _figure_crops(question: str, badges: list[dict], emit) -> tuple[int, int, int]:
    """对被引且含图的页（去重、上限 FIGURE_CROP_CAP）做三选一判定（§十八/§二十一）：

    - 页上有与问题相关的图表/表格 → badge 带 crop（前端裁剪嵌入）
    - 这页本身就是一张图（如 keynote slide）→ badge 带 show_page（整页嵌入）
    - 纯文字提取页 → 什么都不标（前端不插图——引用链接足够，§二十一 用户反馈）

    找到了相关图形但坐标没过校验 → 退回 show_page（图确实在这页上，整页比不给强）；
    调用失败/解析失败 → 不插图（宁静默，不冗余）。纯展示层，仅 UI 流式路径调用。"""
    seen: set[tuple] = set()
    u_in = u_out = calls = 0
    for b in badges:
        if len(seen) >= FIGURE_CROP_CAP:
            break
        if not b.get("document_id") or not b.get("has_visual") or not b.get("png_path"):
            continue
        key = (b["document_id"], b["page_number"])
        if key in seen:
            continue
        seen.add(key)
        f = settings.PAGE_ASSET_DIR / b["png_path"]
        if not f.exists():
            continue
        emit({"type": "stage", "text": "Locating the exact figure on the cited page…"})
        try:
            box, u = providers.figure_bbox(f.read_bytes(), question)
        except Exception:
            continue
        u_in += u.get("input_tokens", 0)
        u_out += u.get("output_tokens", 0)
        calls += 1
        if not box or box.get("no_figure"):
            continue
        if box.get("whole_page"):
            mark = {"show_page": True}
        else:
            crop = _valid_crop(box)
            mark = {"crop": crop} if crop else {"show_page": True}
        for bb in badges:
            if (bb.get("document_id"), bb.get("page_number")) == key:
                bb.update(mark)
    return u_in, u_out, calls


def recency_labels(badges: list[dict]) -> list[dict]:
    """时效性标签（§6.3）：被引报告的同券商同主 ticker 是否存在更新的报告。"""
    labels = []
    for b in badges:
        if "document_id" not in b:
            continue
        doc = Document.objects.filter(id=b["document_id"]).first()
        if not doc or not doc.published_date or not doc.tickers:
            continue
        newer = (Document.objects
                 .filter(status=Document.Status.DONE, broker=doc.broker,
                         tickers__contains=[doc.tickers[0]],
                         published_date__gt=doc.published_date)
                 .aggregate(d=Max("published_date"))["d"])
        if newer:
            labels.append({"citation": b["citation"],
                           "superseded_by": str(newer),
                           "note": f"{doc.broker} has a newer report on {doc.tickers[0]} ({newer})"})
    return labels


def run_turn(conversation: Conversation, text: str,
             image_b64: str | None = None, pdf_b64: str | None = None,
             pdf_name: str = "upload.pdf", emit=None) -> dict:
    """一轮完整对话。emit 提供时发进度事件（SSE 用）：round / tool / tool_result /
    delta；不提供时（evaluate 等离线路径）行为与旧非流式版完全一致。"""
    streaming = emit is not None
    emit = emit or (lambda e: None)
    # 非流式路径（evaluate 等）on_delta=None → providers.chat 走旧的一次性 POST
    on_delta = (lambda s: emit({"type": "delta", "text": s})) if streaming else None
    t0 = time.time()
    usage_in = usage_out = usage_cached = calls = 0
    trace: list[dict] = []
    turn_pages: dict[tuple, Page] = {}
    sent_images: set[tuple] = set()  # 轮内已附原图的页（§十七 去重）

    user_content: list[dict] = [{"type": "input_text", "text": text}]
    if image_b64:  # 图片直接进 user message（§6.3，不做描述中转）
        user_content.append({"type": "input_image", "detail": "high",
                             "image_url": f"data:image/png;base64,{image_b64}"})
    if pdf_b64:  # PDF 走 input_file 直传，不入索引（§6.3）
        user_content.append({"type": "input_file", "filename": pdf_name,
                             "file_data": f"data:application/pdf;base64,{pdf_b64}"})
    user_msg = {"role": "user", "content": user_content}

    api_input = _history_to_api(conversation.messages) + [user_msg]
    # 存储侧：上传件不落库（体积），只存占位说明
    store_user = {"role": "user", "content": [{"type": "input_text", "text": text}]}
    if image_b64 or pdf_b64:
        store_user["content"].append(
            {"type": "input_text", "text": "(User attached an image/file this turn; not persisted.)"})
    new_items: list[dict] = [store_user]

    instructions = system_prompt()
    answer = ""
    for _round in range(MAX_TOOL_ROUNDS):
        emit({"type": "round", "round": _round + 1})
        res = providers.chat(api_input, instructions, tools.TOOL_SCHEMAS,
                             on_delta=on_delta)
        u = res.get("usage", {})
        usage_in += u.get("input_tokens", 0)
        usage_out += u.get("output_tokens", 0)
        usage_cached += (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
        calls += 1

        fcalls = [o for o in res.get("output", []) if o.get("type") == "function_call"]
        msgs = [o for o in res.get("output", []) if o.get("type") == "message"]
        if not fcalls:
            answer = " ".join(c.get("text", "") for o in msgs for c in o.get("content", []))
            new_items.append({"role": "assistant",
                              "content": [{"type": "output_text", "text": answer}]})
            break

        # 推理模型要求：function_call 必须连同其 reasoning item 一起回传，
        # 故轮内把本次响应的全部 output items 原样接回 input。
        api_input.extend(res.get("output", []))
        for fc in fcalls:
            new_items.append({"type": "function_call", "call_id": fc["call_id"],
                              "name": fc["name"], "arguments": fc["arguments"]})
            args = json.loads(fc["arguments"] or "{}")
            emit({"type": "tool", "name": fc["name"], "args": args})
            try:
                payload = tools.dispatch(fc["name"], dict(args))
            except Exception as exc:  # 工具错误回给模型自我修正，不炸整轮
                payload = {"error": str(exc)[:300]}
            n_res = len(payload.get("results", [])) or payload.get("count") or 0
            emit({"type": "tool_result", "name": fc["name"], "n_results": n_res})
            trace.append({"tool": fc["name"], "args": args,
                          "trace": payload.get("trace"),
                          "n_results": len(payload.get("results", [])) or payload.get("count")})
            for r in payload.get("results", []) + [rp["first_page"] for rp in payload.get("reports", []) if rp.get("first_page")]:
                if r:
                    p = Page.objects.filter(document_id=r["document_id"],
                                            page_number=r["page_number"]).select_related("document").first()
                    if p:
                        turn_pages[(p.document.broker,
                                    str(p.document.published_date), p.page_number)] = p
            api_item, store_item = _tool_output_items(fc["call_id"], payload, sent_images)
            api_input.append(api_item)
            new_items.append(store_item)
    else:
        # 撞轮次上限 → 强制一轮无工具的尽力作答（放弃消息是严格更差的输出）
        api_input.append({"role": "user", "content": [{"type": "input_text", "text":
            "(System) Tool-round limit reached. Answer now from the content retrieved "
            "above; explicitly state what could not be retrieved. Do not fabricate. "
            "Do not call any more tools."}]})
        emit({"type": "round", "round": MAX_TOOL_ROUNDS + 1})
        res = providers.chat(api_input, instructions, on_delta=on_delta)  # 不传 tools
        u = res.get("usage", {})
        usage_in += u.get("input_tokens", 0)
        usage_out += u.get("output_tokens", 0)
        usage_cached += (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
        calls += 1
        answer = " ".join(c.get("text", "")
                          for o in res.get("output", []) if o.get("type") == "message"
                          for c in o.get("content", []))
        new_items.append({"role": "assistant",
                          "content": [{"type": "output_text", "text": answer}]})

    badges = grounding_badges(answer, turn_pages)
    if streaming:  # 图形定位仅 UI 路径（§十八）；evaluate 不跑，评估成本为零
        ci, co, cc = _figure_crops(text, badges, emit)
        usage_in += ci
        usage_out += co
        calls += cc
    # 原子追加（审查确认的 lost-update 修复）：请求线程加载的快照做
    # read-modify-write 会让并发轮（双开标签页/断连后立刻重问）互相覆盖。
    # 行锁内重取再追加；updated_at 是 auto_now，必须列入 update_fields 才会刷新。
    with transaction.atomic():
        locked = Conversation.objects.select_for_update().get(id=conversation.id)
        locked.messages = locked.messages + new_items
        locked.save(update_fields=["messages", "updated_at"])
    conversation.messages = locked.messages

    # 缓存命中的输入按 1/10 价估算（OpenAI 自动前缀缓存的典型折扣；官方价核实前
    # 与 $5/$30 同为假设价——见 §14）。此前按全价估，多轮回合显著虚高。
    cost = ((usage_in - usage_cached) / 1e6 * PRICE_IN
            + usage_cached / 1e6 * PRICE_IN * 0.1
            + usage_out / 1e6 * PRICE_OUT)
    return {
        "answer": answer,
        "citations": badges,
        "recency": recency_labels(badges),
        "trace": trace,
        "footer": {
            "cost_usd": round(cost, 4),
            "seconds": round(time.time() - t0, 1),
            "api_calls": calls,
            "tool_calls": len(trace),
            "input_tokens": usage_in,
            "output_tokens": usage_out,
            "cached_tokens": usage_cached,
        },
    }
