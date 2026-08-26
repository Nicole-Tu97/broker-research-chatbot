"""The two retrieval tools. Schemas live in the same file as the
implementations to avoid drift.

- search_pages: vector (pgvector cosine) and full-text (ts_rank_cd) legs each
  take top-50, fused via RRF (rrf_k=10), returning the top k. Each leg's
  independent rank goes into the retrieval trace for eval attribution.
- list_reports: pure metadata SQL. Returns the first-page transcription plus
  the pages where each ticker is mentioned; the description spells out the
  recovery path (2 of 21 reports keep the PT off page 1).
- Returning the original image is the chat layer's job: tools return png_path
  and has_visual, and the loop assembles the function_call_output image content.
"""

from datetime import date

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, Q
from pgvector.django import CosineDistance

from . import providers
from .models import Document, Page

LEG_DEPTH = 50   # candidate depth per leg (>=50 per leg before fusion)
RRF_K = 10       # small constant: a strong single-leg hit is not diluted
# Fusion knobs (defaults = shipped behavior; the retrieval ablation measures alternatives):
#   FTS_SEARCH_TYPE: "websearch" (AND semantics — every term must match; dies on long
#     natural-language questions) | "or" (any term may match, ranked by ts_rank_cd)
#   FTS_MAX_QUERY_WORDS: skip the lexical leg for queries longer than this (None = never skip)
#   FTS_WEIGHT: multiplier on the lexical leg's RRF votes
FTS_SEARCH_TYPE = "or"  # measured on 94 items: OR 0.814 vs AND 0.761 hybrid recall@10 (dense-only 0.773)
FTS_MAX_QUERY_WORDS = None
FTS_WEIGHT = 1.0


def _doc_filters(tickers=None, brokers=None, date_from=None, date_to=None,
                 prefix: str = "document__") -> Q:
    """Ticker / broker / date filters. prefix="document__" targets Page queries,
    prefix="" targets Document queries — one builder for both sides."""
    q = Q()
    if tickers:
        q &= Q(**{f"{prefix}tickers__overlap": [t.upper() for t in tickers]})
    if brokers:
        broker_q = Q()
        for b in brokers:
            broker_q |= Q(**{f"{prefix}broker__icontains": b})
        q &= broker_q
    if date_from:
        q &= Q(**{f"{prefix}published_date__gte": date_from})
    if date_to:
        q &= Q(**{f"{prefix}published_date__lte": date_to})
    return q


def _undated_warning(tickers=None, brokers=None) -> str | None:
    """Blind-spot warning for date filters: documents with published_date=None
    (company-authored decks with no parseable date anywhere on the page) are
    silently excluded by any date filter. This warning lets the agent recover
    within one retry instead of rewording endlessly against 0 results.
    Deterministic SQL, zero API calls."""
    q = Q(status=Document.Status.DONE, published_date=None)
    q &= _doc_filters(tickers, brokers, prefix="")
    n = Document.objects.filter(q).count()
    if not n:
        return None
    return (f"{n} document(s) match your non-date filters but have NO published_date "
            "(company-authored decks carry no printed date anywhere) and are therefore "
            "EXCLUDED by date_from/date_to. If the document you want may be one of "
            "these (e.g. NVIDIA's own quarterly presentations or keynotes), retry "
            "WITHOUT date filters.")


def _page_payload(page: Page, extra: dict | None = None) -> dict:
    d = page.document
    out = {
        "document_id": d.id,
        "broker": d.broker,
        "published_date": str(d.published_date) if d.published_date else None,
        "title": d.title,
        "page_number": page.page_number,
        "citation": f"{d.broker}, {d.published_date or 'n.d.'}, p.{page.page_number}",
        "markdown": page.markdown or "",
        "has_visual": page.has_visual,
        "png_path": page.png_path,
    }
    if page.numeric_flags:
        out["suspect_numbers"] = page.numeric_flags  # tool-side consumer of ingest-time numeric checks
    if extra:
        out.update(extra)
    return out


def search_pages(query: str, tickers=None, brokers=None,
                 date_from=None, date_to=None, k: int = 8,
                 mode: str = "hybrid") -> dict:
    """Hybrid retrieval. Returns {results: [...], trace: {...}}.

    mode is for ablation evals only (dense/fts/hybrid) and is not exposed to
    the model (absent from TOOL_SCHEMAS)."""
    base = Page.objects.exclude(markdown=None).exclude(markdown="").filter(
        _doc_filters(tickers, brokers, date_from, date_to)
    ).select_related("document")

    # Vector leg
    vec_ids = []
    if mode in ("hybrid", "dense"):
        qvec = providers.embed([query])[0]
        vec_ids = list(
            base.exclude(embedding=None)
            .order_by(CosineDistance("embedding", qvec))
            .values_list("id", flat=True)[:LEG_DEPTH])

    # Full-text leg (websearch syntax: tolerates analysts' natural phrasing)
    fts_ids = []
    use_fts = mode in ("hybrid", "fts") and (
        FTS_MAX_QUERY_WORDS is None or len(query.split()) <= FTS_MAX_QUERY_WORDS)
    if use_fts:
        if FTS_SEARCH_TYPE == "or":
            import re as _re
            terms = [t.replace("'", "''") for t in _re.findall(r"[A-Za-z0-9][A-Za-z0-9.%$-]*", query) if len(t) > 1]
            sq = SearchQuery(" | ".join(f"'{t}'" for t in terms) or query, config="english", search_type="raw")
        else:
            sq = SearchQuery(query, config="english", search_type="websearch")
        fts_ids = list(
            base.filter(search_vector=sq)
            .annotate(rank=SearchRank(F("search_vector"), sq, normalization=1))
            .order_by("-rank")
            .values_list("id", flat=True)[:LEG_DEPTH])

    # RRF fusion
    scores: dict[int, float] = {}
    for leg, w in ((vec_ids, 1.0), (fts_ids, FTS_WEIGHT)):
        for rank, pid in enumerate(leg, start=1):
            scores[pid] = scores.get(pid, 0.0) + w / (RRF_K + rank)
    top = sorted(scores, key=scores.get, reverse=True)[:k]

    pages = {p.id: p for p in Page.objects.filter(id__in=top).select_related("document")}
    vec_rank = {pid: i for i, pid in enumerate(vec_ids, start=1)}
    fts_rank = {pid: i for i, pid in enumerate(fts_ids, start=1)}
    results = [
        _page_payload(pages[pid], {"rrf_score": round(scores[pid], 4)})
        for pid in top if pid in pages
    ]
    # Retrieval trace: independent per-leg ranks, so a miss is attributable to a specific leg
    trace = {
        "query": query,
        "filters": {"tickers": tickers, "brokers": brokers,
                    "date_from": str(date_from) if date_from else None,
                    "date_to": str(date_to) if date_to else None},
        "legs": {"vector": len(vec_ids), "fts": len(fts_ids)},
        "per_result_ranks": [
            {"page_id": pid, "vector": vec_rank.get(pid), "fts": fts_rank.get(pid)}
            for pid in top
        ],
    }
    out = {"results": results, "trace": trace}
    if date_from or date_to:
        w = _undated_warning(tickers, brokers)
        if w:
            out["warning"] = w
    return out


def list_reports(tickers=None, brokers=None, date_from=None, date_to=None) -> dict:
    """Metadata SQL: matching reports sorted by date, each with its first-page
    transcription and the pages where each ticker is mentioned."""
    docs = Document.objects.filter(status=Document.Status.DONE)
    docs = docs.filter(_doc_filters(tickers, brokers, date_from, date_to, prefix="")).order_by("published_date")

    reports = []
    for d in docs:
        p1 = d.pages.filter(page_number=1).first()
        hits = {t: d.ticker_pages.get(t, []) for t in (tickers or []) if d.ticker_pages.get(t)}
        reports.append({
            "document_id": d.id,
            "broker": d.broker,
            "published_date": str(d.published_date) if d.published_date else None,
            "title": d.title,
            "page_count": d.page_count,
            "tickers": d.tickers,
            "ticker_hit_pages": hits or d.ticker_pages,
            "first_page": _page_payload(p1) if p1 else None,
        })
    out = {"reports": reports, "count": len(reports)}
    if date_from or date_to:
        w = _undated_warning(tickers, brokers)
        if w:
            out["warning"] = w
    return out



# ---- Responses API tool schemas (maintained alongside the signatures above) ----

TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "search_pages",
        "description": (
            "Hybrid search (semantic vector + full-text) over the whole knowledge "
            "base; returns the most relevant page transcriptions, with the original "
            "page image attached when a hit contains visuals. Filters NARROW results "
            "— do not apply them when unsure (over-filtering loses recall). Good "
            "for: specific arguments, chart data, thematic discussions. Query "
            "wording: use words likely printed ON the target page, not your analyst "
            "vocabulary — slide/keynote pages carry minimal text (slogans, proper "
            "nouns, figures); terms like 'market size/TAM' do not exist on them. On "
            "a miss, retry with RADICALLY different wording (parallel differentiated "
            "queries are fine), not synonym tweaks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text, phrased as the report itself would phrase it"},
                "tickers": {"type": "array", "items": {"type": "string"},
                            "description": "e.g. [\"NVDA\"], optional"},
                "brokers": {"type": "array", "items": {"type": "string"},
                            "description": "e.g. [\"Barclays\", \"UBS\"], substring match, optional"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD, optional"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD, optional"},
                "k": {"type": "integer", "description": "pages to return, default 8"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "list_reports",
        "description": (
            "Exact metadata listing of matching reports (broker / ticker / date "
            "range), sorted by date, each with its first-page transcription and the "
            "pages where the ticker is mentioned. PREFER this tool for comparative, "
            "temporal, and rating/price-target questions — it is guaranteed to "
            "return ALL matching reports, independent of similarity. Note: a few "
            "reports keep the rating/PT off page 1 (e.g. Wells Fargo on p3) — when "
            "page 1 lacks the value, follow up with search_pages filtered by "
            "broker/date, or search the pages listed in ticker_hit_pages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tickers": {"type": "array", "items": {"type": "string"}},
                "brokers": {"type": "array", "items": {"type": "string"}},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
    },
]


def dispatch(name: str, args: dict) -> dict:
    """Tool-dispatch entry point for the chat loop. Converts date strings to
    date objects; raises explicitly on unknown tool names."""
    for key in ("date_from", "date_to"):
        if args.get(key):
            args[key] = date.fromisoformat(args[key])
    if name == "search_pages":
        return search_pages(**args)
    if name == "list_reports":
        return list_reports(**args)
    raise ValueError(f"Unknown tool: {name}")
