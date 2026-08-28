# Validation Report

Scoring is deterministic throughout — no LLM grades another LLM.

## The test set

- **124 questions, each with a fixed answer key** (`eval/golden_set.json`): the pages
  that hold the answer, the facts the answer must state, a forbidden text pattern for
  questions that must be declined, and — for figure questions — a hand-drawn figure box.
- **The answer keys were drafted by an LLM from the source pages, then checked:** every
  expected fact was verified by code to appear on the expected page, and the keys were
  spot-checked by hand. The chatbot under test never saw them.
- **One set feeds both tests.** The retrieval test uses the page keys
  (94 questions have them); the behavior test uses the fact, pattern and box keys
  on every question.
- **Grading is rule-based against these fixed keys** (rules below), so the same set
  re-scores any future version of the system the same way.

## How answers are graded — preset rules, never an LLM judging an LLM

Every golden-set question was written with its grading rule attached, fixed before any
testing. There are three kinds of rule:

1. **Must contain** — exact facts the answer has to state (e.g. `$240`, `+20%`, a
   date). Graded by searching the answer text for those strings, with number formats
   normalized first (thousands separators, scale words, multiplication signs).
2. **Must NOT contain** — a text pattern that would betray a made-up answer. E.g. a
   question about a year the library does not cover forbids any 2–3 digit dollar
   figure: stating one means the system invented a price target instead of declining.
3. **Box match** — for figure questions, the hand-drawn rectangle on the source page
   that the returned crop must overlap (IoU ≥ 0.5).

One universal rule applies on top, to every answer: each number a citation carries
must actually appear on the cited page's text (the ✓/⚠ badge check). The chatbot's
answers are compared against these preset rules by a plain script — string search,
number comparison, rectangle overlap. No LLM grades another LLM's output anywhere,
so every score is exactly reproducible.

## Retrieval quality — golden-set (reference-based) evaluation

**What this is.** A golden-set evaluation: the correct answer pages were marked
*before* any testing, and the retriever is scored against that fixed answer key.

**How the test runs.** 94 questions, each with 1–10 hand-checked answer
pages (about 135 in total). Each question is sent to the retriever exactly as written,
once per configuration (each returning
its top 10 pages — a slightly wider window than the live tool's default of 8, which
the agent can raise). The score per question is the share of its answer pages that
show up in the top 10; the table averages this per question type.

**The six question types.**
- `simple_qa` — the answer sits plainly on one page
- `table_numeric` — an exact number inside a dense financial table
- `pure_chart` — the answer exists only inside a chart image
- `cross_broker_comparison` — needs pages from several documents (several brokers)
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
| cross_broker_comparison (13) | 0.587 | 0.426 | 0.541 | **0.992** |
| deep_page_recovery (11) | 1.0 | 0.818 | 0.909 | **0.818** |
| pure_chart (16) | 0.812 | 0.625 | 0.938 | **1.0** |
| simple_qa (20) | 0.95 | 0.85 | 0.925 | **1.0** |
| table_numeric (24) | 0.708 | 0.708 | 0.833 | **0.917** |
| temporal (10) | 0.5 | 0.55 | 0.6 | **1.0** |
| non-English questions (35 of 94) | 0.782 | 0.624 | 0.779 | **0.969** |
| **Mean (94 questions)** | **0.773** | **0.681** | **0.814** | **0.956** |

*The non-English row is not a seventh type — it is a slice of the same 94
questions (its items also appear in the type rows above, and the Mean row does
not count them twice). It answers one question: does retrieval hold up when the
question is not in English?*

**Acceptance bar — judged on the production (agentic) column.** One bar:
the overall mean must reach 0.90. (Set after the first results were known, so
marked post-hoc; from here on it is the bar every future run must clear.)

- Overall mean ≥ 0.90: **PASS** (0.956)

**One weak spot to note.** On `deep_page_recovery`, single-shot hybrid (0.909) beats
the agent (0.818). **The cause is the agent's cost-saving default: it reads each
report's first page and stops as soon as that looks sufficient.** On deep-page
questions the first page is only a summary, so the agent never fetches the deeper page
with the exact figure — while single-shot hybrid, which never stops early, does. (A few
misses are the agent answering correctly from another valid page the fixed answer key
does not credit.) **Fix: keep the single-shot hybrid results as a floor** — the agent's
early stop can then only add pages, never lose them (hybrid alone scores 0.909 here) —
and the stopping threshold itself can be tuned.

How the retriever's design evolved — including the approaches that failed and
what fixed them — is told in DESIGN.md §7 (what I tried that didn't work).

## Behavior validation (end-to-end)

**What is being tested.** The full **production system** — the same agentic
pipeline the chat page runs (up to 6 rounds of tool calls), called live, one
final answer per question.

**What was asked.** All 124 golden-set questions, across 9 types:

- `abstention` — 15 questions
- `attachment_input` — 10 questions
- `cross_broker_comparison` — 13 questions
- `deep_page_recovery` — 11 questions
- `multi_turn` — 5 questions
- `pure_chart` — 16 questions
- `simple_qa` — 20 questions
- `table_numeric` — 24 questions
- `temporal` — 10 questions
- `prompt-injection probe` — 1 planted document (outside the golden set): hidden
  instructions plus a canary word, tested on both untrusted surfaces (ingested PDF,
  user upload)

Some questions were deliberately asked more than once (one question three times,
for reproducibility; the figure questions twice, to measure run-to-run variance) —
148 live calls in total. The metrics below score each question's most recent
answer once; per-run raw numbers are archived in `eval/results.json`.

- **Correctness (P7b)** — share of the answer key's expected facts the answer actually
  states, over the 103 questions carrying 189 expected facts: 1.0
  (threshold ≥0.85 → **PASS**)
- **Unsupported-number rate (P7a)** — share of checked citations whose numbers do *not*
  appear on the page they cite, over 204 checked citations (5 citations naming a
  page that cannot be re-checked — e.g. answered from conversation memory — are
  excluded): 0.02 (threshold ≤0.10 → **PASS**)
- **Hallucination rate (P8)** — 15 deliberately unanswerable questions (a year or a
  broker the library does not cover); the system must decline. "Answered anyway" is
  detected mechanically, by a per-question forbidden text pattern — e.g. for a question
  about 2023 targets, any dollar figure in the answer counts as answering; a decline
  contains none: 0/15 answered anyway (threshold = 0 → **PASS**)
- **Multi-turn context carry** — follow-up questions must keep citing the right pages
  from earlier turns (5 multi-turn conversations): 5/5 → **PASS**
- **Attachment input** — a chart screenshot or PDF attached to the question must be
  matched to the right report and page (10 questions): 10/10 → **PASS**
- **Figure-crop accuracy** — when the answer embeds a figure, the crop must overlap the
  hand-annotated figure box with IoU ≥ 0.5 (IoU = overlap area of the two boxes ÷ their
  combined area; 0 = no overlap, 1 = exact match, so ≥ 0.5 means at least half overlap);
  the 17 figure questions were asked in 2 separate runs:
  26/32 scoreable = 0.812 (run 1: 14/17; run 2: 12/15; a question whose
  annotated page is not cited is not scoreable) (threshold ≥0.80 → **PASS**).
  An early 3-question spot-check scored 1/3 and triggered the locator fix;
  it measured the pre-fix locator and is archived, not pooled.
- **Reproducibility (P9)** — the same question asked 3 separate times; every run must
  contain all the key numbers: 3/3 → **PASS**
- **Robustness (P10)** — the same question asked in two different wordings (3 pairs);
  both answers must agree on the key facts: 3/3 → **PASS**
- **Injection resistance (P11)** — hidden instructions planted in untrusted input carry a
  secret canary word; the canary must never surface in an answer: not leaked → **PASS**
- **Watermark & contact-info leak (P12)** — 122 client-identifying strings harvested
  from the PDFs (distribution watermarks, e-mail addresses); none may appear in any
  answer: 0 leak(s) across all 145 archived answers → **PASS**

Behavior validation total API cost: $20.34

---
Details in `eval/results.json`. Not evaluated, by design: fairness/bias (single-domain corpus, no user population), calibration (answers are cited facts, not probabilities), and public leaderboards (they do not measure this corpus).