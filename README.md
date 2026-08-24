# Broker Research Chatbot

Django chatbot over broker research PDFs — hybrid retrieval (pgvector + Postgres FTS)
with page-level citations, deterministic grounding badges, and recency labels.

> **Start here:** [`DESIGN.md`](DESIGN.md) (design & trade-offs, English) ·
> [`DECISION-LOG.md`](DECISION-LOG.md) (every failed path and reversal) ·
> [`eval/validation_report.md`](eval/validation_report.md) (pre-registered, deterministic scoring) ·
> demo script: `ARCHITECTURE.md` §13 (Chinese, personal notes)

All six behavior dimensions pass machine scoring (final strict round): grounding 1.0,
expected facts 1.0, abstention 4/4 (incl. a user-caught scope-substitution case),
reproducibility 3/3, robustness 3/3, prompt-injection canary 0 leaks on both
untrusted-input surfaces, client-watermark PII 0 leaks.

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
make test                             # 36 tests; deterministic core needs no API key
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
