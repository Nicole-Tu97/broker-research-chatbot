# Design — Broker Research Chatbot

> English deliverable. Evaluation evidence: `eval/validation_report.md` + Appendix A below.

## 1. The problem, taken seriously

The exemplar question — *"Compare the change in price target for NVDA between UBS's
research and Barclays's research over the past two years"* — has four properties, and
each one defeats naive vector RAG:

| Property | Why naive top-k fails |
|---|---|
| Comparative (two brokers) | Similarity doesn't guarantee covering both sides |
| Temporal ("change over two years") | Embeddings carry no date ordering |
| Aggregative | The answer lives in no single chunk |
| Exact numbers | Price targets sit in sidebars, tables, and **chart pixels** |

So the goal is not "retrieve relevant passages" — it is *reliably answering this class
of question, with verifiable provenance*.

## 2. One-sentence architecture

**Every page becomes "text describing everything on that page," stored in one table;
an LLM loop with two tools queries it; every number in every answer is
deterministically checked back against its cited page.**

Everything else is implementation detail. Stack: Django (ASGI) + Postgres 17 +
pgvector, single LLM vendor (OpenAI Responses API — the only API shape whose
`function_call_output` carries images, verified live). Two Docker services. Three
tables. Two tools. No queue, no reranker, no fact table, no framework.

## 3. Measure first: what the corpus actually is

We measured all 30 PDFs page-by-page before designing (and re-measured after an
adversarial review caught four errors in our own first-round numbers):

- **423 pages.** Filename page counts are systematically wrong (12→6, 18→8, 40→32) —
  metadata must be content-verified, never trusted from names.
- **The GTC keynote breaks text-only pipelines**: 23 of 70 pages have a completely
  empty text layer, 58 are near-empty; a load-bearing `$100T` figure exists only as
  pixels. Multimodal ingestion is a hard requirement, not a nice-to-have.
- **Chart numbers in broker reports live in the text layer** (one UBS page has 417
  numeric tokens) — enabling deterministic transcription validation on 350/423 pages.
- **Six physical size classes**, three documents mix sizes internally (one keynote
  page is 53.3×30″) — render DPI must be computed per page.
- **Ticker surface forms**: literal "NVDA" appears in only 21/30 documents; the
  NVIDIA decks and multi-industry reports say only "NVIDIA". Uppercase "AI" (a real
  ticker) appears in 29/30. Extraction therefore uses a curated alias dictionary —
  symbols case-sensitive, company names case-insensitive — never bare symbol matching.
- **Corpus window is 3.5 months (2025-06-12 → 09-29), not two years; UBS has 1 report.**
  The honest answer to the exemplar question *declares this boundary* — injected into
  the system prompt as fact, so "knowing what it doesn't know" is default behavior.

## 4. Five decisions that shape the system

**4.1 Page = atomic unit.** No sub-page chunking: it shatters tables, orphans charts,
and breaks page-level citations. Dilution risk on dense pages is compensated by the
full-text leg, and page images ride along as ground truth.

**4.2 One multimodal call per page.** Page image *and* native text layer go into a
single transcription call; the prompt pins prose to the text layer (numbers must keep
their original surface forms) and uses vision for structure and image-only content.
A benchmarked prompt rule ("cell count must match header count; blank cells stay
blank — never fill with 0") exists because the benchmark caught exactly that failure.

**4.3 Numeric guarantees come from validation, not from the model.** A normalized
multiset diff flags transcription numbers absent from the text layer (~20 lines, zero
API calls, 350/423 pages). Its blind spots (same-value collisions, zero-count-neutral
shifts) are documented, measured, and compensated by the next decision. The same
check runs at answer time: every citation gets a **grounding badge** — numbers in the
answer are checked against the cited page. Deterministic end to end; there is no
LLM-judge anywhere in this system.

**4.4 Original assets surface twice: in model context and in the answer.** Pages with
visuals return their original image inside the tool result (Responses API), for both
tools — so a transcription omission is recoverable at query time. On the UI side, one
isolated vision call per cited visual page (capped at 2) makes a three-way call:
a question-relevant chart/table exists → its bounding box is located and the server
re-renders just that region from the PDF (PyMuPDF clip — no new dependency, no new
storage) as an inline card; the page as a whole IS the visual (a chart slide) → the
full page embeds; the page's contribution is textual → NO image at all — the citation
link suffices (embedding cover pages of text reports is noise, not evidence). The risk
that made us reject cropping twice — a bad box silently losing axis labels or footnotes
— is contained deterministically: coordinates are validated (out of range, degenerate,
<8% or >85% of the page are all rejected), padded by 2%; located-but-invalid boxes fall
back to the full page, and the click-through always opens the original PDF at the cited
page. The locator call runs only on the interactive path,
never during evaluation, and never touches the frozen prompts. Cross-turn, tool traffic
(including images) is never replayed; it persists only as references for audit and UI.

**4.5 No pre-extracted facts table.** What comparative/temporal questions need is
*exact metadata filtering*, which SQL already does perfectly:
`WHERE broker = ? AND ? = ANY(tickers) ORDER BY published_date`. A `list_reports`
tool returning full first-page transcriptions preserves the *why* behind each rating
change that a `pt=240` row would destroy. Two verified caveats are engineered in:
ticker extraction is the alias dictionary above, and for the 2/21 reports whose price
target is not on page 1 (Wells Fargo p.3, BofA p.9) the tool description carries an
explicit recovery path — which the agent demonstrably follows.

## 5. Retrieval

`search_pages`: vector (pgvector cosine) and full-text (`ts_rank_cd`) legs, top-50
each, fused with RRF (k=10), per-leg ranks logged into a visible retrieval trace.
`list_reports`: pure metadata SQL. Routing lives in tool descriptions; there is no
planner layer — the function-calling loop is the planner, and it is demonstrably
capable of multi-step recovery (filtered follow-up searches, page-hint navigation).

Cross-lingual behavior (measured): non-English questions work end-to-end because the
model writes English search queries and the embedding space is cross-lingual; the
English-config FTS leg contributes only on English keyword queries — by design.

## 6. Evaluation: pre-registered, deterministic, and honest

Twelve predictions with fixed thresholds were registered **before** `manage.py
evaluate` first ran (Appendix A, verbatim, with outcomes). Scoring is value-based and deterministic throughout —
methodology reused from my prior open-source project
[llm-validation-harness](https://github.com/Nicole-Tu97/llm-validation-harness).

**Transcription benchmark** (20 hardest pages × multiple DPI tiers, 60 runs,
human-authored ground truth double-checked by an independent reviewer): production
tiers were *measured into* the design — reports at 150 DPI (100 DPI produces numeric
vetoes on dense tables), keynote at 72 DPI (52 passes; **150 DPI hallucinated** —
more resolution is not monotonically safer). Watermark leakage 0/60. One residual
failure mode (an all-zero row misaligned in the two densest pages) is documented as a
known bound rather than tuned away on n=2.

**Retrieval ablation** (recall@10, raw questions as queries — a conservative proxy,
declared in advance): hybrid 0.804 vs FTS-only 0.196 on the original 17 retrieval
items. After the golden set grew to 109 items (94 with expected pages), the same
ablation re-ran: hybrid 0.761, dense-only 0.773, FTS-only 0.094 — the larger sample
corrected the small-sample optimism by ~4 points and surfaced a signal the n=17 run
could not: with websearch (AND) semantics the lexical leg died on long natural-language
questions and its noise votes *slightly hurt* fusion. Root-caused and fixed: the lexical
leg now uses OR semantics ranked by `ts_rank_cd` — FTS-only 0.094 → 0.681, hybrid
**0.814** (> dense 0.773; pure_chart 0.81 → 0.94, table 0.71 → 0.83). The preregistered
core behavior round was re-run after the change: all six dimensions still pass, at a
third of the previous cost ($2.43 vs $7.17) because the agent now lands on the right
page in fewer rounds. Agentic recall on the production path: 0.9 strict on the 10
end-to-end items, 10/10 once an equally valid newer source is credited. Two
original predictions were falsified, and we kept the receipts: FTS dies on
natural-language sentences (websearch AND-semantics) long before term precision can
matter — its value is on model-written keyword queries. The reranker decision closed with data: every miss
was candidate absence (recall = 0 in *all* configs), which reranking cannot fix; the
fixes belonged in query formulation, were made, and were verified end-to-end.

**Behavior validation** (end-to-end, all six dimensions PASS in the final strict
round): grounding badge rate 1.0, expected-fact hit 1.0; abstention 4/4 — including
a scope-substitution case caught by a real user (asked about 2023, the bot volunteered
2025 data; fixed with an overlap-based boundary rule, and the first fix's regression
was itself caught by this suite); reproducibility 3/3; robustness 3/3 across paraphrase and
language; **prompt-injection resistance verified on both untrusted-input surfaces**
(a planted PDF in ingestion and a user-uploaded PDF — the embedded canary never
leaked); client-watermark PII leakage 0/13 answers.

The evaluation also caught and fixed three real defects (give-up on tool-round
exhaustion, vocabulary mismatch on sparse slide pages, answer substitution from an
adjacent source) — and one defect in the scorer itself (unit-scale blindness:
"US$131.651 billion" vs "131,651"), rescored transparently against stored answers.

## 7. Chat experience

Table-first answers for comparative/numeric questions with per-row citations;
cross-broker numbers never blended; clickable citations with page thumbnails opening
the original PDF at the cited page; grounding badges; **recency labels** ("superseded
by this broker's 2025-09-25 report") via one deterministic SQL check per citation —
the most expensive mistake an analyst can make, prevented for free; a live
cost/latency footer under every answer; a retrieval-trace panel with per-leg ranks.
Inputs: text, images (answered *and* reverse-located to their source page), PDFs
(native `input_file`, compared against the corpus), graceful decline otherwise.

## 8. Reproducibility

`make demo`: fresh clone → db + migrate + load the committed 2.6 MB index fixture
(transcriptions, embeddings, metadata; tsvectors regenerate as DB-generated columns)
→ re-render page PNGs locally from the PDFs (deterministic, free) → chat, in ~3
minutes with only an API key. Full re-ingestion (`make ingest`) is ~1 hour / ~$23.5.
Deterministic-core tests run with no API key at all.

## 9. Scaling to thousands of documents

Measured migration points, not hand-waving: Celery + Batch API for ingestion (Batch
was *removed* from the current design after measuring that 423 pages of base64 PNG
exceed its 200 MB input-file cap — the $12 saving lost to a second code path);
a rating facts table only once `list_reports` matches exceed ~50 reports; per-language
tsvector configs when non-English corpora arrive; pg_search/BM25 then Elasticsearch
for lexical scale; object storage for page assets (`png_path` is already relative).
Unit economics: ~$0.055/page ingested, $0.05–0.9/question, both measured.

## 10. What we deliberately did not build

No LangChain/LlamaIndex (two tools and one table don't need a framework). No
pre-extracted facts table, no reranker (both decisions closed *with data*). No
sub-page chunking, no bbox cropping, no Celery/Redis, no Batch API at this scale, no
prompt-caching engineering (system prompt is ~5% of spend), no LLM-as-judge anywhere
(deterministic validation instead — "who validates the validator" terminates), no
frontend framework, no auth/multi-tenancy. Each omission is argued, and several were
*reversals of our own earlier designs* — documented throughout this document with
their triggers and lessons, per the brief's request for what we tried and what
didn't work.

## 11. Known limits (stated, not hidden)

Corpus window is 3.5 months — the system says so rather than extrapolating. The
numeric validator cannot see same-value collisions or zero-count-neutral column
shifts (compensated by original-image feedback and grounding badges). The golden set
is 124 items (9 answer-location types × 4 cross-cutting tags); end-to-end behavior
scoring has run on all of them (14 preregistered core, 17 figure-crop, 94 new —
correctness 136/136, unsupported numbers 2/163 citations, hallucination 0/11, multi-turn 5/5,
attachment input 10/10, figure-crop accuracy 0.80–0.82 across two runs — the locator is stochastic, ±2 items run to run). Three scorer blind spots surfaced
only at this scale and were fixed with tests: locale-specific numeric scale words and the
multiplication sign in answers; page numbers inside citation labels counted as numeric
claims; and citations to pages retrieved in an earlier turn (or named from an attached
image) being marked unverifiable — the last one was also a product defect (follow-up
answers showed a warning badge) and is fixed in the chat loop. Pricing for the chat model is assumed
($5/$30 per 1M) pending the official price page. The 52-DPI tier for the quarterly
decks rests on a dominance argument (the harder keynote passes at 52), not direct
sampling.

## Appendix A — Pre-registered predictions vs. outcomes

Registered before the first evaluation run; thresholds were fixed in advance and
results were
never used to revise a prediction. Two predictions were falsified — kept as-is.

| # | Prediction (threshold) | Outcome |
|---|---|---|
| P1 | Hybrid mean recall@10 ≥ 0.85 | **FAIL** (0.804) — every miss was candidate absence, recoverable by the agentic layer (verified per item) |
| P2 | hybrid ≥ each single leg | PASS |
| P3 | FTS-only ≈ 0 on non-English questions (≤ 0.2) | PASS (0.167) |
| P4 | dense < fts on exact-number table questions | **FALSIFIED** (0.75 vs 0.0) — websearch AND-semantics kills FTS on full sentences before term precision can matter |
| P5 | pure-chart hybrid ≥ 2 of 3 | PASS (0.667) |
| P6 | reranker built only if hybrid misses threshold | Evaluated → **not built**: misses were recall-zero cases, which reranking cannot fix |
| P7 | citation grounding ≥ 0.90; fact hit ≥ 0.85 | PASS (1.0 / 1.0, final confirmation round) |
| P8 | abstention: no fabricated figures/ratings | PASS (4/4, incl. a user-caught scope-substitution case added as AB4) |
| P9 | reproducibility 3/3 on invariant facts | PASS |
| P10 | robustness ≥ 2/3 paraphrase pairs | PASS (3/3, incl. cross-language) |
| P11 | injection canary never leaks | PASS (both surfaces: ingested PDF and user upload) |
| P12 | client-watermark PII never leaks | PASS (0/14 answers) |

Pre-declared caveats that mattered: the ablation feeds raw question sentences to
`search_pages` — a conservative proxy for the production path, where the model
writes its own queries; the original n=17 retrieval set had limited statistical power.

**Re-evaluation after expansion (n=94 retrieval items, same thresholds, not
re-registered):** under the original AND-semantics FTS leg — P1 FAIL (0.761), **P2
falsified** (dense 0.773 > hybrid 0.761), P3 PASS (0.195), P4 falsified (0.708 vs
0.125), P5 PASS (0.812). After switching the lexical leg to OR semantics — P1 FAIL
(0.814), P2 PASS, **P3 falsified** (0.624: non-English questions still contain English
tokens such as NVDA/UBS that OR matching finds), P4 dense = fts (0.708), P5 PASS
(0.938). The original outcomes above are kept as the preregistered record; this
paragraph is the honest update.
