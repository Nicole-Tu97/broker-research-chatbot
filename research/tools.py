"""两个检索工具（ARCHITECTURE.md §6.2）。schema 与实现同文件，避免漂移。

- search_pages：向量（pgvector cosine）与全文（ts_rank_cd）各取 top-50，
  RRF（rrf_k=10）融合取前 k。每路独立排名进检索追踪，供 eval 归因（§8.2）。
- list_reports：纯元数据 SQL。返回首页转录 + 各 ticker 命中页码；
  description 写明恢复路径（2/21 份报告 PT 不在首页，§3）。
- 原图回传是 chat 层的职责：工具返回 png_path 与 has_visual，
  由循环组装 function_call_output 的 image content（§4.4）。
"""

from datetime import date

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, Q
from pgvector.django import CosineDistance

from . import providers
from .models import Document, Page

LEG_DEPTH = 50   # 每路候选深度（§6.2：融合前每路 ≥50）
RRF_K = 10       # 小常数：单路强命中不被稀释（§6.2）


def _doc_filters(tickers=None, brokers=None, date_from=None, date_to=None) -> Q:
    q = Q()
    if tickers:
        q &= Q(document__tickers__overlap=[t.upper() for t in tickers])
    if brokers:
        broker_q = Q()
        for b in brokers:
            broker_q |= Q(document__broker__icontains=b)
        q &= broker_q
    if date_from:
        q &= Q(document__published_date__gte=date_from)
    if date_to:
        q &= Q(document__published_date__lte=date_to)
    return q


def _undated_warning(tickers=None, brokers=None) -> str | None:
    """日期过滤的盲区提示（§十七）：published_date=None 的文档（公司自家 deck，
    页面上无任何可解析日期）会被任何日期过滤静默排除。此提示让 agent 能在
    一次重试内恢复，而不是在 0 results 里反复换措辞。确定性 SQL，零 API。"""
    q = Q(status=Document.Status.DONE, published_date=None)
    if tickers:
        q &= Q(tickers__overlap=[t.upper() for t in tickers])
    if brokers:
        broker_q = Q()
        for b in brokers:
            broker_q |= Q(broker__icontains=b)
        q &= broker_q
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
        out["suspect_numbers"] = page.numeric_flags  # §4.3 的工具侧消费者
    if extra:
        out.update(extra)
    return out


def search_pages(query: str, tickers=None, brokers=None,
                 date_from=None, date_to=None, k: int = 8,
                 mode: str = "hybrid") -> dict:
    """混合检索。返回 {results: [...], trace: {...}}。

    mode 仅供 §8.2 消融评估（dense/fts/hybrid），不暴露给模型（不在 TOOL_SCHEMAS）。"""
    base = Page.objects.exclude(markdown=None).exclude(markdown="").filter(
        _doc_filters(tickers, brokers, date_from, date_to)
    ).select_related("document")

    # 向量一路
    vec_ids = []
    if mode in ("hybrid", "dense"):
        qvec = providers.embed([query])[0]
        vec_ids = list(
            base.exclude(embedding=None)
            .order_by(CosineDistance("embedding", qvec))
            .values_list("id", flat=True)[:LEG_DEPTH])

    # 全文一路（websearch 语法：容忍分析师的自然措辞）
    fts_ids = []
    if mode in ("hybrid", "fts"):
        sq = SearchQuery(query, config="english", search_type="websearch")
        fts_ids = list(
            base.filter(search_vector=sq)
            .annotate(rank=SearchRank(F("search_vector"), sq, normalization=1))
            .order_by("-rank")
            .values_list("id", flat=True)[:LEG_DEPTH])

    # RRF 融合
    scores: dict[int, float] = {}
    for leg in (vec_ids, fts_ids):
        for rank, pid in enumerate(leg, start=1):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
    top = sorted(scores, key=scores.get, reverse=True)[:k]

    pages = {p.id: p for p in Page.objects.filter(id__in=top).select_related("document")}
    vec_rank = {pid: i for i, pid in enumerate(vec_ids, start=1)}
    fts_rank = {pid: i for i, pid in enumerate(fts_ids, start=1)}
    results = [
        _page_payload(pages[pid], {"rrf_score": round(scores[pid], 4)})
        for pid in top if pid in pages
    ]
    # 检索追踪（§6.3）：每路独立排名，miss 可归因到具体一路（§8.2）
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
    """元数据 SQL：匹配报告按日期排序，各带首页转录与 ticker 命中页码。"""
    docs = Document.objects.filter(status=Document.Status.DONE)
    inner = _doc_filters(tickers, brokers, date_from, date_to)
    # _doc_filters 生成的是 Page 侧前缀，剥掉 document__ 前缀复用于 Document 查询
    docs = docs.filter(_strip_document_prefix(inner)).order_by("published_date")

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


def _strip_document_prefix(q: Q) -> Q:
    new = Q()
    new.connector = q.connector
    new.negated = q.negated
    for child in q.children:
        if isinstance(child, Q):
            new.children.append(_strip_document_prefix(child))
        else:
            key, val = child
            new.children.append((key.replace("document__", "", 1), val))
    return new


# ---- Responses API 工具 schema（与上方签名同源维护）----

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
    """chat 循环的工具分发入口。日期串转 date，未知工具名显式报错。"""
    for key in ("date_from", "date_to"):
        if args.get(key):
            args[key] = date.fromisoformat(args[key])
    if name == "search_pages":
        return search_pages(**args)
    if name == "list_reports":
        return list_reports(**args)
    raise ValueError(f"未知工具: {name}")
