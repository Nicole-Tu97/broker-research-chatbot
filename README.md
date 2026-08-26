# Broker Research Chatbot

Django chatbot over broker research PDFs — hybrid retrieval (pgvector + Postgres FTS)
with page-level citations, deterministic grounding badges, and recency labels.

> **Start here:** [`DESIGN.md`](DESIGN.md) (design & trade-offs, and what we tried
> that didn't work) · [`eval/validation_report.md`](eval/validation_report.md)
> (pre-registered, deterministic scoring).

## What it does

Ask questions about a library of broker research reports (here: 30 PDFs, 423 pages, June–September 2025 coverage of NVDA and AI infrastructure) and get answers an analyst can verify:

- **Plain-language questions, English or Chinese** — comparative ("compare UBS's and Barclays's price targets"), temporal ("how did Citi's target evolve"), exact numbers from dense tables, facts that exist only inside charts.
- **Every answer cites the source page** — `[Broker, date, p.N]` links open the original PDF at that page.
- **Numbers are verified, not trusted** — each citation carries a badge: ✓ the answer's numbers appear on the cited page, ⚠ they do not. No LLM judges another LLM anywhere.
- **Relevant charts appear inline** — when a cited page's chart or table supports the answer, that figure is cropped from the original page and shown next to the text; text-only pages get a link, never a screenshot.
- **Knows what it doesn't know** — brokers, tickers, or years outside the library get an explicit boundary statement instead of a guess; citations to reports superseded by a newer note from the same broker are flagged.
- **Image and PDF input** — attach a chart screenshot to find which report and page it comes from, or a PDF to relate it to the library.
- **A visible process** — the answer streams with its tool calls, pages read, and per-answer cost and latency.

Example — the brief's own question:

> **Q:** Compare the change in price target for NVDA between UBS's research and Barclays's research over the past two years.
>
> **A:** *Coverage note: the library covers 12 June–29 September 2025, not the full two years.* Then a table — Barclays $170 → $200 (17 Jun) → $240 (25 Sep), UBS $175 (8 Jul), each row cited `[Barclays, 2025-09-25, p.1] ✓` — and a short synthesis. Nothing is averaged across brokers.

## How well it works

Validated on a 124-item golden set (9 question types × 4 cross-cutting tags), scored
deterministically: correctness 1.0 on the preregistered core and 136/136 facts on the
expanded set; hallucination 0/15 on unanswerable questions; prompt-injection canary 0 leaks
on both untrusted surfaces; watermark/contact-info leaks 0; multi-turn 5/5; attachment
input 10/10; figure-crop accuracy 0.80–0.82 across two runs. Single-shot hybrid retrieval
recall@10 is 0.814 on 94 items — below its preregistered 0.85 threshold and reported as
such (the agent's own query rewriting recovers the misses: agentic recall 0.9). Full
detail in [`eval/validation_report.md`](eval/validation_report.md) and DESIGN.md Appendix A.

## Deliverables map (per the case-study brief)

| The brief asks for | Where it lives |
|---|---|
| Django app with a functional chatbot interface | `research/` app — chat UI at `/` (SSE streaming, page-level citations, inline figures) |
| Data pipeline: how PDFs are processed and indexed | `research/management/commands/ingest.py` (discover → render → transcribe → validate → index) + transcription benchmark in `bench/` |
| Documentation: choices, trade-offs, what didn't work, deliberate omissions | [`DESIGN.md`](DESIGN.md) — incl. §10 deliberately-not-built, §11 known limits, Appendix A with two falsified predictions kept as-is |
| Retrieval quality evidence | `manage.py evaluate` → [`eval/validation_report.md`](eval/validation_report.md) (retrieval ablation + 6 behavior dimensions) |
| References back to the original page/figure | every answer cites `[broker, date, p.N]`, links into the PDF at that page, and embeds the located figure |
| Instructions to run locally | this README (Quick start · Full ingestion · Local development) |

## Quick start (fixture path — no ingestion cost, ~3 minutes)

```bash
make venv                     # local Python 3.13 virtualenv
cp .env.example .env          # add your OpenAI API key
export $(grep -v '^#' .env | xargs)
make demo                     # db + migrate + load index fixture + re-render PNGs + server
make doctor                   # verify the whole stack is ready (zero API cost)
```

Open http://127.0.0.1:8000 .

> **Corpus & fixture note:** the source PDFs are licensed broker research, and the
> 2.6 MB index fixture embeds their transcribed content — so neither is committed
> here. Both ship in the private submission package. With the fixture in `fixtures/`
> and the PDFs in `case_study/`, `make demo` gives the full experience (fixture →
> instant index; PDFs → page images via `make render`, local and free). Without
> them, point the pipeline at any research PDFs of your own and rebuild with
> `make ingest`.

## Full ingestion path (rebuilds the index from PDFs, ~1 h / ~$23.5)

```bash
make venv && cp .env.example .env   # add your key, then:
export $(grep -v '^#' .env | xargs)
docker compose up -d db
make migrate ingest
```

## Tests & evaluation

```bash
make test                             # 42 tests; deterministic core needs no API key
.venv/bin/python manage.py evaluate   # retrieval ablation + 6-dimension behavior validation
```

Evaluation is pre-registered: see `DESIGN.md` Appendix A for all 12 predictions
vs. outcomes — including the two the data went on to falsify.

## Layout

```
config/              Django project (settings / urls / asgi)
research/            the app
  providers.py       ALL external API calls (OpenAI Responses API, zero SDK deps)
  tools.py           the two retrieval tools + their schemas (same file, no drift)
  chat.py            function-calling loop, grounding badges, recency labels
  tickers.py         deterministic ticker alias extraction
  numeric.py         transcription numeric validation (normalized multiset diff)
  management/commands/ingest.py     PDF → render → transcribe → validate → index
  management/commands/evaluate.py   ablation + behavior validation → report
bench/               transcription benchmark (20 pages × DPI tiers, ground truth)
eval/                golden set, results, validation report
fixtures/            committed index fixture (make demo)
case_study/          the source PDFs
```

## Local development without Docker

Requires Python 3.13 and PostgreSQL 17 with pgvector:

```bash
brew install postgresql@17 pgvector
LC_ALL=en_US.UTF-8 /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /opt/homebrew/var/postgresql@17 start
createdb research && psql research -c "CREATE EXTENSION vector"
make venv migrate test
```
