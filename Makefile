PY ?= .venv/bin/python

venv:          ## Create local virtualenv (Python 3.13)
	python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt

up:            ## Start database and web (Docker)
	docker compose up -d

migrate:       ## Create tables
	$(PY) manage.py migrate

test:          ## Run tests (pure-function tests need no API key)
	$(PY) manage.py test research -v 2

doctor:        ## Environment checkup: one command to confirm the whole stack is ready (zero API cost)
	$(PY) manage.py doctor

ingest:        ## Full ingest (~1 hour / ~$$23.5, requires OPENAI_API_KEY)
	$(PY) manage.py ingest --resume

render:        ## Only re-render page PNGs (use after loaddata, zero API calls)
	$(PY) manage.py ingest --render-only

demo:          ## Shortest path after clone: database + migrate + fixture + re-render PNGs + serve
	@test -f fixtures/corpus.json.gz || \
		(echo "fixture ships with the delivery package (contains licensed content, kept out of the public repo) — or build your own with make ingest"; exit 1)
	@if command -v docker >/dev/null 2>&1; then docker compose up -d db; \
	else echo "docker not found — assuming a local Postgres on 127.0.0.1:5432 (README: Local development without Docker)"; fi
	$(PY) manage.py migrate
	$(PY) manage.py loaddata fixtures/corpus.json.gz
	$(PY) manage.py ingest --render-only
	$(PY) manage.py runserver

.PHONY: venv up migrate test doctor ingest render demo
