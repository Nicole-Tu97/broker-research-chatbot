"""Chat loop: standard function calling with two tools.

- Corpus-boundary injection: computed from the DB at startup (coverage window,
  brokers, report count); the facts go into the system prompt.
- Message persistence stores image references only; when building a request, only
  the current turn's tool results rehydrate the original images.
- All answer post-processing is deterministic pure functions: grounding badges
  (numbers vs. cited pages), recency labels (whether the same broker has a newer
  report on the same ticker), cost footer (accumulated usage). Zero extra API calls.
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

PRICE_IN, PRICE_OUT = 5.0, 30.0  # $/1M, assumed prices for Sol (official pricing TBD)
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
    """Corpus-boundary facts computed once at startup (the data side of behavior rule 1)."""
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
    """(API item with images, storage item with references only). Original images
    go only into the current turn's request, never into history.

    sent_images: set of (document_id, page_number) already attached this turn.
    Within a turn api_input is cumulative — an image attached once stays in every
    later round's context, so re-attaching the same page just burns money (one
    observed failing turn attached images 115 times for only 37 distinct pages)."""
    slim = json.dumps(payload, ensure_ascii=False)
    image_refs = []
    content: list[dict] = [{"type": "input_text", "text": slim}]
    for r in payload.get("results", []) or [r["first_page"] for r in payload.get("reports", []) if r.get("first_page")]:
        if r and r.get("has_visual") and r.get("png_path"):
            key = (r["document_id"], r["page_number"])
            if sent_images is not None:
                if key in sent_images:
                    continue  # this original image is already in this turn's context
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


def _prior_pages(messages: list[dict]) -> dict[tuple, Page]:
    """Pages retrieved in EARLIER turns of this conversation, keyed like turn_pages.

    A follow-up ("and what was it before this revision?") is legitimately answered from
    conversation memory without a new tool call; its citations must still verify against
    the pages that were retrieved earlier, not be marked unknown. Rebuilt from the stored
    tool outputs (each carries document_id / page_number)."""
    keys: set[tuple[int, int]] = set()
    for m in messages:
        if m.get("type") != "function_call_output":
            continue
        try:
            out = json.loads(m.get("output_text") or "{}")
        except ValueError:
            continue
        rows = out.get("results", []) + [r["first_page"] for r in out.get("reports", []) if r.get("first_page")]
        for r in rows:
            if r and r.get("document_id") and r.get("page_number"):
                keys.add((r["document_id"], r["page_number"]))
    pages: dict[tuple, Page] = {}
    for doc_id, pno in keys:
        p = Page.objects.filter(document_id=doc_id, page_number=pno).select_related("document").first()
        if p:
            pages[(p.document.broker, str(p.document.published_date), p.page_number)] = p
    return pages


def _history_to_api(messages: list[dict]) -> list[dict]:
    """History messages -> API input: keep only user/assistant messages.

    Tool traffic (function_call / its results / reasoning) is not replayed across
    turns: reasoning models require each function_call to appear paired with its
    reasoning item, the synthesized answer already carries the conclusions, and
    pages can be re-fetched via the tools at any time (the "don't replay past
    images" rationale generalized to all tool traffic). Full tool traffic is still
    persisted in Conversation.messages for auditing and the UI."""
    return [m for m in messages if m.get("role") in ("user", "assistant")]


_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
# Dates accept multiple surface forms: the model does not always emit ISO (it has
# been seen writing "September 2025"), so the parsing layer must be looser than the
# prompt's wording — a failed citation parse costs all links/badges/Sources.
_MONTH = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
          r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
          r"Nov(?:ember)?|Dec(?:ember)?")
_DATE = (r"\d{4}-\d{2}-\d{2}"
         r"|\d{4}-\d{2}"                       # "2025-10" (seen in the wild on undated decks)
         rf"|(?:{_MONTH})\.?\s+\d{{1,2}},?\s+\d{{4}}"
         rf"|(?:{_MONTH})\.?\s+\d{{4}}"
         r"|n\.d\."
         r"|\d{4}")
CITATION_RE = re.compile(rf"(.+?),\s*({_DATE}),\s*p\.?\s*(\d+)", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _date_prefix(date_s: str) -> str | None:
    """Date surface form in a citation -> ISO prefix (prefix-matched against published_date).

    "2025-09-05" -> "2025-09-05"; "September 2025" -> "2025-09"; "2025" -> "2025";
    "n.d." or unparseable -> None (no date filtering — broker + page number +
    this turn's retrieved set remain the hard gate)."""
    s = date_s.strip()
    if re.fullmatch(r"\d{4}(?:-\d{2}){0,2}", s):  # YYYY / YYYY-MM / YYYY-MM-DD → prefix as-is
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
    """Support compound citations [A, date, p.1; B, date, p.3]: split on ';', parse each."""
    for bm in _BRACKET_RE.finditer(answer):
        for frag in bm.group(1).split(";"):
            m = CITATION_RE.fullmatch(frag.strip())
            if m:
                yield f"[{frag.strip()}]", m


def grounding_badges(answer: str, turn_pages: dict[tuple, Page]) -> list[dict]:
    """Grounding badges: for each citation in the answer, are its numbers on the
    cited page. Pure function."""
    # Citation labels carry page numbers and dates ("[UBS, 2025-07-08, p.4]") that are
    # not claims about the page — strip them before collecting the answer's numbers.
    answer_nums = set(numbers_in(_BRACKET_RE.sub("", answer)))
    badges = []
    for label, m in _citation_fragments(answer):
        broker_frag, date_s, page_no = m.group(1).strip(), m.group(2), int(m.group(3))
        # Anti-fabrication hard gate = the page must be among this turn's retrieved
        # results (broker + page number). The date is an extra check, compared only
        # when the document itself has one (ds="None" means no date — e.g. NVIDIA's
        # own decks, where neither filename nor first page yields a parseable date;
        # then the citation's date cannot be verified and is not treated as a veto).
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
        # Checking only numbers "in the answer AND near this citation's context"
        # is too heavy — take the conservative reading: "answer nums ∩ page nums
        # >= 1 with no answer-only number attributed to this page" cannot be
        # decided by a pure function, so the badge criterion = page numbers
        # intersect answer numbers -> green; no intersection -> warning (the
        # citation may be purely qualitative)
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


FIGURE_CROP_CAP = 2      # at most 2 figure locates per answer (~$0.025 each; caps cost/latency)
_CROP_PAD = 2.0          # expand 2% outward: better to over-crop than cut off axis labels


def _valid_crop(box) -> dict | None:
    """Deterministic bbox validation and expansion. Pure function.

    Rejects: missing/out-of-range/degenerate coordinates, area <8% (likely a
    mislocate) or >85% (effectively the whole page — falling back to the full
    page is more honest). On pass -> expand by _CROP_PAD and clamp to [0,100]."""
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
    """Three-way call for cited pages with visuals (deduped, capped at FIGURE_CROP_CAP):

    - page has a chart/table relevant to the question -> badge gets crop
      (frontend embeds the cropped region)
    - the page itself is one big figure (e.g. a keynote slide) -> badge gets
      show_page (embed the whole page)
    - plain text-extraction page -> mark nothing (frontend embeds no image —
      the citation link is enough)

    Relevant figure found but the bbox fails validation -> fall back to show_page
    (the figure really is on this page; the whole page beats nothing); call or
    parse failure -> no image (prefer silence over noise). Pure presentation
    layer, called only on the streaming UI path."""
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
    """Recency labels: whether the cited report's broker has a newer report on
    the same primary ticker."""
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
             pdf_name: str = "upload.pdf", emit=None,
             figure_crops: bool | None = None) -> dict:
    """One full conversation turn. When emit is given, sends progress events (for
    SSE): round / tool / tool_result / delta; without it (offline paths such as
    evaluate) behavior matches the old non-streaming version exactly."""
    streaming = emit is not None
    emit = emit or (lambda e: None)
    # non-streaming path (evaluate etc.): on_delta=None -> providers.chat does the old one-shot POST
    on_delta = (lambda s: emit({"type": "delta", "text": s})) if streaming else None
    t0 = time.time()
    usage_in = usage_out = usage_cached = calls = 0
    trace: list[dict] = []
    turn_pages: dict[tuple, Page] = _prior_pages(conversation.messages)  # follow-ups verify against earlier turns
    sent_images: set[tuple] = set()  # pages whose originals were already attached this turn (dedup)

    user_content: list[dict] = [{"type": "input_text", "text": text}]
    if image_b64:  # image goes straight into the user message (no description relay)
        user_content.append({"type": "input_image", "detail": "high",
                             "image_url": f"data:image/png;base64,{image_b64}"})
    if pdf_b64:  # PDF is passed directly via input_file; it is not indexed
        user_content.append({"type": "input_file", "filename": pdf_name,
                             "file_data": f"data:application/pdf;base64,{pdf_b64}"})
    user_msg = {"role": "user", "content": user_content}

    api_input = _history_to_api(conversation.messages) + [user_msg]
    # storage side: uploads are not persisted (size); store only a placeholder note
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

        # Reasoning models require each function_call to be sent back together with
        # its reasoning item, so within the turn this response's output items are
        # appended to the input verbatim.
        api_input.extend(res.get("output", []))
        for fc in fcalls:
            new_items.append({"type": "function_call", "call_id": fc["call_id"],
                              "name": fc["name"], "arguments": fc["arguments"]})
            args = json.loads(fc["arguments"] or "{}")
            emit({"type": "tool", "name": fc["name"], "args": args})
            try:
                payload = tools.dispatch(fc["name"], dict(args))
            except Exception as exc:  # feed tool errors back for self-correction; don't kill the turn
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
        # Hit the round cap -> force one tool-free best-effort answer (a give-up
        # message is a strictly worse output)
        api_input.append({"role": "user", "content": [{"type": "input_text", "text":
            "(System) Tool-round limit reached. Answer now from the content retrieved "
            "above; explicitly state what could not be retrieved. Do not fabricate. "
            "Do not call any more tools."}]})
        emit({"type": "round", "round": MAX_TOOL_ROUNDS + 1})
        res = providers.chat(api_input, instructions, on_delta=on_delta)  # no tools passed
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
    if (figure_crops if figure_crops is not None else streaming):  # default: UI path only; evaluate opts in per item
        ci, co, cc = _figure_crops(text, badges, emit)
        usage_in += ci
        usage_out += co
        calls += cc
    # Atomic append (review-confirmed lost-update fix): read-modify-write on the
    # snapshot loaded by the request thread lets concurrent turns (two open tabs /
    # an immediate re-ask after a disconnect) overwrite each other. Re-fetch under
    # a row lock, then append; updated_at is auto_now and only refreshes if it is
    # listed in update_fields.
    with transaction.atomic():
        locked = Conversation.objects.select_for_update().get(id=conversation.id)
        locked.messages = locked.messages + new_items
        locked.save(update_fields=["messages", "updated_at"])
    conversation.messages = locked.messages

    # Cache-hit input is estimated at 1/10 price (the typical discount of OpenAI's
    # automatic prefix caching; like $5/$30, an assumed rate until official pricing
    # is verified). Estimating at full price previously overstated multi-round turns.
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
