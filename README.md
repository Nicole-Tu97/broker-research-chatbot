# Broker Research Chatbot

Django chatbot over broker research PDFs — hybrid retrieval (pgvector + Postgres FTS)
with page-level citations, deterministic grounding badges, and recency labels.

> **Start here:** [`DESIGN.md`](DESIGN.md) (design & trade-offs, and what I tried
> that didn't work) · [`eval/validation_report.md`](eval/validation_report.md)
> (pre-registered, deterministic scoring).

## What it does

Ask questions about a library of broker research reports (here: 30 PDFs, 423 pages, June–September 2025 coverage of NVDA and AI infrastructure) and get answers an analyst can verify:

- **Plain-language questions, in multiple languages** — comparative ("compare UBS's and Barclays's price targets"), temporal ("how did Citi's target evolve"), exact numbers from dense tables, facts that exist only inside charts.
- **Cited and verified** — every answer cites its source as `[Broker, date, p.N]`, the link opens the original PDF at that page, and every number is re-checked against that page's own text: a ✓ badge means it appears there, ⚠ means it does not.
- **Relevant charts appear inline** — when a cited page's chart or table supports the answer, that figure is cropped from the original page and shown next to the text; text-only pages get a link, never a screenshot.
- **Knows what it doesn't know** — brokers, tickers, or years outside the library get an explicit boundary statement instead of a guess; citations to reports superseded by a newer note from the same broker are flagged.
- **Guards sensitive content, resists manipulation** — distribution watermarks and contact details from the source PDFs never surface in answers, and instructions hidden inside documents or attachments are ignored, not obeyed (both validated: 0 leaks, 0 injections — see the validation report).
- **Image and PDF input** — attach a chart screenshot to find which report and page it comes from, or a PDF to relate it to the library.
- **A visible process** — the answer streams with its tool calls, pages read, and per-answer cost and latency.

Example — the brief's own question:

> **Q:** Compare the change in price target for NVDA between UBS's research and Barclays's research over the past two years.
>
> **A:** *Coverage note: the library covers 12 June–29 September 2025, not the full two years.* Then a table — Barclays $170 → $200 (17 Jun) → $240 (25 Sep), UBS $175 (8 Jul), each row cited `[Barclays, 2025-09-25, p.1] ✓` — and a short synthesis. Nothing is averaged across brokers.

## How well it works

Validated on a 124-item golden set (9 question types × 4 cross-cutting tags), scored
deterministically: correctness 189/189 expected facts; hallucination 0/15 on unanswerable
questions; prompt-injection canary 0 leaks on both untrusted surfaces; watermark/contact-info
leaks 0 across 145 answers; multi-turn 5/5; attachment input 10/10; figure-crop accuracy
0.812 pooled over two runs (0.824 and 0.80). Retrieval is judged on the
production loop (the agent rewrites queries, retries, and picks tools): recall@10 reaches
0.956 on 94 golden-set items, clearing the 0.90 acceptance bar; single-shot hybrid
retrieval alone scores 0.814 on the same items. Full detail in
[`eval/validation_report.md`](eval/validation_report.md) and DESIGN.md Appendix A.

## Deliverables map (per the case-study brief)

| The brief asks for | Where it lives |
|---|---|
| Django app with a functional chatbot interface | `research/` app — chat UI at `/` (SSE streaming, page-level citations, inline figures) |
| Data pipeline: how PDFs are processed and indexed | `research/management/commands/ingest.py` (discover → render → transcribe → validate → index) + transcription benchmark in `bench/` |
| Documentation: choices, trade-offs, what didn't work, deliberate omissions | [`DESIGN.md`](DESIGN.md) — incl. §10 deliberately-not-built, §11 known limits, Appendix A with two falsified predictions kept as-is |
| Retrieval quality evidence | `manage.py evaluate` → [`eval/validation_report.md`](eval/validation_report.md) (retrieval ablation + 6 behavior dimensions) |
| References back to the original page/figure | every answer cites `[broker, date, p.N]`, links into the PDF at that page, and embeds the located figure |
| Instructions to run locally | this README (Quick start · Full ingestion · Local development) |

## Requirements

| What | Version / note |
|---|---|
| macOS or Linux | Windows works via WSL2 (`make` and the commands below assume a Unix shell) |
| Python **3.13** | `brew install python@3.13` / `apt install python3.13` — `make venv` fails without it |
| Docker Desktop (running) | only used for the Postgres 17 + pgvector container; alternatively see "Local development without Docker" below |
| An **OpenAI API key of your own** | create one at <https://platform.openai.com/api-keys>; the account needs a small credit balance. Typical spend: a chat question $0.05–0.5, the key-check probe ~$0.0001, a full re-ingestion of the corpus ~$23.5. Chat and ingestion call the API; everything else (tests, doctor, page rendering) is free and offline |

## Quick start (~5 minutes, no ingestion cost)

This is the path for the **submission package** (it contains `fixtures/corpus.json.gz`
and the 30 PDFs in `case_study/`). Cloned the public repo instead? Skip to
"Building the index yourself" below.

```bash
# 1. Python env (creates .venv and installs requirements)
make venv

# 2. Your API key — the ONLY secret, lives only in .env (git-ignored)
cp .env.example .env
#    → edit .env: OPENAI_API_KEY=sk-your-own-key
export $(grep -v '^#' .env | xargs)

# 3. Database + index + page images + server (Docker must be running)
make demo
```

What `make demo` does, in order: starts the Postgres container → applies migrations →
loads the prebuilt index (453 objects: 30 documents and 423 page
transcriptions) → re-renders page PNGs locally from the PDFs (~1–2 min, free) → starts the
server. Leave it running and open **http://127.0.0.1:8000** — you should see the chat
page with the corpus summary ("30 reports covering 2025-06-12 to 2025-09-29 ...") in
the header and suggested questions to click. Ctrl+C stops the server; `make demo`
starts it again (already-done steps are skipped).

To double-check the stack at any point:

```bash
make doctor                                    # environment checkup, zero API cost → "Environment ready ✓"
.venv/bin/python manage.py doctor --probe      # + one ~$0.0001 live call proving YOUR key works
```

> **Why the public repo has no fixture/PDFs:** the source PDFs are licensed broker
> research and the 2.6 MB index fixture embeds their transcribed content, so neither
> is committed publicly. Both are included in the private submission package.

## Building the index yourself (~1 h / ~$23.5, or your own PDFs)

If you don't have the submission package: put research PDFs into `case_study/`
(any PDFs work), then:

```bash
make venv && cp .env.example .env   # add your key, then:
export $(grep -v '^#' .env | xargs)
docker compose up -d db
make migrate ingest                 # renders, transcribes, validates, indexes every page
.venv/bin/python manage.py runserver
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `make venv` → `python3.13: command not found` | install Python 3.13 (see Requirements) |
| `make demo` → `Cannot connect to the Docker daemon` | start Docker Desktop first |
| Postgres port conflict (`5432 already in use`) | stop the local Postgres, or run without Docker (section below) |
| Chat answers fail with `Upstream call failed` | key missing/invalid or no credit — run `manage.py doctor --probe`; every check prints a `↳ fix` hint |
| Page images/thumbnails missing | `make render` (re-renders locally from the PDFs, free) |
| Header says the knowledge base is empty | the index isn't loaded — run `make demo` (fixture path) or `make ingest` |

`make doctor` diagnoses all of the above in one shot and prints a fix hint per failure.

## Adding more documents

The corpus is a directory of PDFs — expanding it is an operation, not a code change:

1. Drop new PDFs into `case_study/` (any research PDFs; filenames like
   `YYYYMMDD - Broker - TICKER - Title.pdf` get their metadata parsed, others fall
   back to first-page content).
2. Run `make ingest`. Ingestion is **idempotent**: documents are identified by content
   hash, finished ones are skipped, interrupted ones resume — so re-running only pays
   for what is new (~$0.055/page).
3. Nothing else to update: the corpus boundary in the system prompt, broker lists, and
   ticker page hints are computed from the database at request time.

Scaling beyond thousands of documents (batch ingestion, index partitioning, when a
pre-extracted facts table starts to pay) is mapped out in DESIGN.md §9.

## Configuration — API key and models

All runtime configuration is environment variables (read in `config/settings.py`);
nothing is hardcoded and no key is ever committed.

- **Use your own OpenAI key:** `cp .env.example .env`, set `OPENAI_API_KEY=sk-...`,
  then `export $(grep -v '^#' .env | xargs)`. To change the key later, edit `.env`
  and re-export (or restart the server) — that is the only place it lives.
- **Verify it works:** `make doctor` (free stack check) or
  `.venv/bin/python manage.py doctor --probe` (one ~$0.0001 live API call).
- **Swap models:** `OPENAI_VISION_MODEL` (chat + transcription) is drop-in.
  `OPENAI_EMBED_MODEL` / embedding dimensions are **not** drop-in — vectors from a
  different embedding model are incompatible, so changing them means re-running
  `make ingest`.
- Every external call lives in one file: `research/providers.py`.

## Tests & evaluation

```bash
make test                             # full test suite; the deterministic core needs no API key
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
page_assets/         rendered page PNGs — created by `make demo`/`make render`, git-ignored
fixtures/            index fixture — in the submission package; git-ignored in the public repo
case_study/          the source PDFs — in the submission package; put your own PDFs here
.env.example         template for the one secret (OPENAI_API_KEY → your .env)
```

## Local development without Docker

Requires Python 3.13 and PostgreSQL 17 with pgvector:

```bash
brew install postgresql@17 pgvector
LC_ALL=en_US.UTF-8 /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /opt/homebrew/var/postgresql@17 start
createdb research && psql research -c "CREATE EXTENSION vector"
make venv migrate test
```

## License

MIT — see [LICENSE](LICENSE).
