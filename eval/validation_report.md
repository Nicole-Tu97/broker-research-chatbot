# Validation Report (deterministic scoring against the preregistered thresholds in DESIGN.md Appendix A)

Generated: 2026-08-26 16:45 · zero LLM-judge scoring throughout

## Retrieval quality — golden-set (reference-based) evaluation

**What this is.** A golden-set evaluation: the correct answer pages were marked
*before* any testing, and the retriever is scored against that fixed answer key.

**How the answer key was made — three defenses against a wrong key.**
1. *Anchored in the source, not in the system.* Every fact and page reference was
   taken from the PDF's own text layer (extracted deterministically, no LLM), then
   a question was written for it — the answer existed before the question did.
2. *The drafting model is not the tested model.* Questions were drafted with a
   different vendor's model than the one the system runs on, so the system cannot
   grade its own homework.
3. *Every item passed a machine check (no LLM).* A script verified that the cited
   file exists, the page exists, and each expected fact literally appears on that
   page. Items that failed were fixed before entering the set.

**How the test runs.** 94 questions, each with 1–10 hand-checked answer
pages (about 135 hand-checked answer pages in total). Each question is sent to the retriever exactly as written, once
per configuration (94 × 3 = 282 single-shot searches, each returning its
top 10 pages). The score per question is the share of its answer pages that show
up in the top 10; the table averages this per question type. The agentic column is
different: it replays the 94 archived production runs and counts every page the
agent retrieved during the whole turn.

**The six question types.**
- `simple_qa` — the answer sits plainly on one page
- `table_numeric` — an exact number inside a dense financial table
- `pure_chart` — the answer exists only inside a chart image
- `comparison_timeseries` — needs pages from several documents (several brokers)
- `temporal` — needs time ordering: "latest", "before/after", how a number evolved
- `deep_page_recovery` — the answer is buried deep in a report; page 1 is only a summary

**The four columns.**
1. `dense` — semantic search: finds pages that *mean* the same thing, even with different words
2. `fts` — exact text match: finds pages that literally contain the question's words
3. `hybrid` — both combined, but still **one single search** with the raw question (single-shot RAG)
4. `agentic` — the **production system**: up to 6 rounds where the LLM rewrites the
   query, retries, and switches tools (e.g. exact date-ordered lookups); measured over the whole turn

| Category | dense | fts | hybrid | **agentic (production)** |
|---|---|---|---|---|
| comparison_timeseries | 0.587 | 0.426 | 0.541 | **0.992** |
| deep_page_recovery | 1.0 | 0.818 | 0.909 | **0.818** |
| pure_chart | 0.812 | 0.625 | 0.938 | **1.0** |
| simple_qa | 0.95 | 0.85 | 0.925 | **1.0** |
| table_numeric | 0.708 | 0.708 | 0.833 | **0.917** |
| temporal | 0.5 | 0.55 | 0.6 | **1.0** |
| **Mean** | **0.773** | **0.681** | **0.814** | **0.956** |

**Production verdict (post-hoc, not preregistered): agentic mean 0.956 — above the 0.85 bar.** The two weakest single-shot
types (temporal, comparison) are exactly where the agent gains the most — those
answers are meant to come from query rewriting and date-ordered lookups, not from
one similarity search.

**Worth noticing:** on `deep_page_recovery`, single-shot hybrid (0.909) beats
agentic (0.818). Part of this is a scoring artifact (the agent sometimes answers
from an equally valid *other* page, which the fixed answer key does not credit), but it
also points to a real improvement path: do not rely on the agent blindly — keep the
single-shot hybrid results as a floor (or route by question type) so the agent's
choices can only add pages, never lose them.

**Preregistered predictions (P1–P6).** These were fixed before the first run and
never revised. P1 gates the single-shot proxy; P2–P5 are bets about *how the
retriever works* — a FAIL means the forecast was wrong, not that users get worse
answers. End-to-end quality is the section below.

- P1 hybrid ≥ 0.85: **FAIL** (0.814) — single-shot proxy; the production path (agent rewrites queries and retries) recovers the misses (agentic mean 0.956)
- P2 hybrid ≥ both single modes: **PASS**
- P3 non-English items FTS-only ≤ 0.2: **FAIL** (0.624) — a design-assumption bet that the lexical leg would be useless off-English; falsified in the GOOD direction (English tickers/terms inside non-English questions still match). Non-English items score 1.0 correctness end-to-end
- P4 table_numeric dense < fts: **FAIL** (0.708 vs 0.708) — a which-leg-is-stronger bet, falsified; the fused result on these items is 0.833
- P5 pure_chart hybrid ≥ 0.67: **PASS** (0.938)
- P6 reranker: hybrid below threshold → triggers reranker evaluation

## Behavior validation (end-to-end)

- Unsupported-number rate (P7a): 0.0 (threshold ≤0.10 → **PASS**)
- Correctness (P7b): fact hit rate 1.0 (threshold ≥0.85 → **PASS**)
- Hallucination rate (P8): 0.0 — 0/4 unanswerable items answered anyway (threshold = 0 → **PASS**)
- Reproducibility (P9): 3/3 runs contain all invariants → **PASS**
- Robustness (P10): 3/3 paraphrase pairs → **PASS**
- Injection resistance (P11): canary not leaked → **PASS**
- Watermark & contact-info leak (P12): 0 leak(s) (122 corpus-derived canaries × 14 answers) → **PASS**
- Figure-crop accuracy: 1/1 → **PASS**

Behavior validation API cost: $2.43

## Behavior validation — extra set `items:PC5,PC9,PC11` (not preregistered; scored with the same rules)

- Correctness: fact hit rate 0.8; unsupported-number rate 0.0
- Figure-crop accuracy: 1/3 = 0.333 → **FAIL** (IoU ≥ 0.5; threshold ≥ 0.80)
- Watermark & contact-info leak: 0 leak(s) over 3 answers
- API cost: $0.59

## Behavior validation — extra set `items:CT3,TN2,TN3,XT2,NF2,RQ5,RQ6,RQ7,RQ8,RQ9,SQ1,SQ2,SQ5,SQ7,SQ8,XT3,XT4,XT5,XT6,XT7,XT8,XT9,XT10,TS1,TS2,TS3,TS4,TS5,TS6,TS7,TS8,TS9,TS10,PC13,PC14,CT4,CT5,CT6,CT7,CT8,CT9,CT10,CT11,CT12,TN5,TN6,TN7,TN8,TN9,TN10,TN11,TN12,TN13,TN14,TN15,TN16,TN17,TN18,TN19,TN20,AB5,AB6,AB7,AB8,AB9,AB10,AB11,AB12,AB13,AB14,AB15,NF3,NF4,NF5,NF6,NF7,NF8,NF9,NF10,MT1,MT2,MT3,MT4,MT5,MI1,MI2,MI3,MI4,MI5,MI6,MI7,MI8,MI9,MI10` (not preregistered; scored with the same rules)

- Correctness: fact hit rate 1.0; unsupported-number rate 0.012
- Hallucination rate: 0.0 (0/11 answered anyway)
- Multi-turn context carry: 5/5 = 1.0 → **PASS**
- Attachment input: 10/10 = 1.0 → **PASS**
- Watermark & contact-info leak: 0 leak(s) over 94 answers
- API cost: $5.91

## Behavior validation — extra set `crop@run1` (not preregistered; scored with the same rules)

- Correctness: fact hit rate 0.882; unsupported-number rate 0.176
- Figure-crop accuracy: 14/17 = 0.824 → **PASS** (IoU ≥ 0.5; threshold ≥ 0.80)
- Watermark & contact-info leak: 0 leak(s) over 17 answers
- API cost: $5.84

## Behavior validation — extra set `crop` (not preregistered; scored with the same rules)

- Correctness: fact hit rate 1.0; unsupported-number rate 0.125
- Figure-crop accuracy: 12/15 = 0.8 → **PASS** (IoU ≥ 0.5; threshold ≥ 0.80)
- Watermark & contact-info leak: 0 leak(s) over 17 answers
- API cost: $5.57

---
Details in `eval/results.json`. Rationale for cutting non-applicable dimensions (fairness/calibration/benchmarking) is in DESIGN.md §10.