"""Environment checkup: one command after clone to confirm the whole stack is ready
(borrowed from community practice, zero API cost).

python manage.py doctor [--probe]

Runs each check and prints fix hints; --probe additionally makes one minimal
embedding call to verify the API key works (~$0.0001, off by default).
Exit codes: 0 = all passed or warnings only, 1 = at least one failure.
"""

import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "Check that the environment, database, indexes, corpus and assets are ready"

    def add_arguments(self, parser):
        parser.add_argument("--probe", action="store_true",
                            help="Also make one minimal embedding call to verify the API key (~$0.0001)")

    def handle(self, *args, **opts):
        self.failures = 0

        # 1) Environment variables
        key = settings.OPENAI_API_KEY
        self.verify(bool(key), "OPENAI_API_KEY is set",
                   "Copy .env.example to .env, fill in the key, then export $(grep -v '^#' .env | xargs)")
        if key:
            self.verify(key.startswith("sk-"), "OPENAI_API_KEY format (sk-*)",
                       "Key does not look like OpenAI format; please double-check")
        self.info(f"Model config: {settings.OPENAI_VISION_MODEL} / "
                  f"{settings.OPENAI_EMBED_MODEL}({settings.EMBED_DIMENSIONS}d) / "
                  f"detail={settings.OPENAI_IMAGE_DETAIL}")

        # 2) Database connectivity
        try:
            connection.ensure_connection()
            db = settings.DATABASES["default"]
            self.ok(f"Postgres reachable ({db['HOST']}:{db['PORT']}/{db['NAME']})")
        except Exception as exc:
            self.fail(f"Postgres connection failed: {str(exc)[:120]}",
                      "docker compose up -d db; or local pg_ctl start (see README)")
            return self.finish()  # all remaining checks depend on the DB

        # 3) pgvector extension
        with connection.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            row = cur.fetchone()
        self.verify(bool(row), f"pgvector extension ({row[0] if row else 'missing'})",
                   "CREATE EXTENSION vector; (bundled in the docker image; locally brew install pgvector)")

        # 4) Migrations fully applied
        plan = MigrationExecutor(connection).migration_plan(
            MigrationExecutor(connection).loader.graph.leaf_nodes())
        self.verify(not plan, "All migrations applied",
                   f"{len(plan)} unapplied: run make migrate")

        # 5) Data and indexes
        from research.models import Document, Page
        n_doc = Document.objects.count()
        n_done = Document.objects.filter(status=Document.Status.DONE).count()
        n_page = Page.objects.count()
        n_emb = Page.objects.exclude(embedding=None).count()
        if n_doc == 0:
            self.warn("Knowledge base is empty",
                      "make demo loads the fixture (seconds, free); or make ingest for full ingestion (~1h/$23.5)")
        else:
            self.ok(f"Knowledge base: {n_doc} documents ({n_done} done) · {n_page} pages · {n_emb} vectors")
            err = Document.objects.filter(status=Document.Status.ERROR).count()
            if err:
                self.warn(f"{err} documents in ERROR state",
                          "Re-running make ingest resumes from the checkpoint and fills the gaps")
        with connection.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='research_page'")
            idx = {r[0] for r in cur.fetchall()}
        for name, label in [("page_embedding_hnsw", "HNSW vector index"),
                            ("page_search_gin", "GIN full-text index")]:
            self.verify(name in idx, label, "Run make migrate to rebuild the index")

        # 6) Corpus and assets
        pdfs = list(settings.CORPUS_DIR.glob("*.pdf")) if settings.CORPUS_DIR.exists() else []
        self.verify(len(pdfs) > 0, f"Corpus directory ({len(pdfs)} PDFs)",
                   f"Make sure the PDFs are in {settings.CORPUS_DIR}")
        if n_page:
            missing_png = sum(
                1 for p in Page.objects.exclude(png_path="").only("png_path")
                if not (settings.PAGE_ASSET_DIR / p.png_path).exists())
            self.verify(missing_png == 0, "Page PNG assets complete",
                       f"{missing_png} pages missing images: run make render (local re-render, free)")
        fixture = Path(settings.BASE_DIR) / "fixtures" / "corpus.json.gz"
        self.verify(fixture.exists(), "Index fixture present (required by make demo)",
                   "Fixture missing: rebuild with dumpdata after running ingest")

        # 7) Optional: API key liveness probe
        if opts["probe"]:
            try:
                from research import providers
                vec = providers.embed(["doctor probe"])[0]
                self.ok(f"API key valid (embedding returned {len(vec)} dimensions)")
            except Exception as exc:
                self.fail(f"API call failed: {str(exc)[:120]}",
                          "Check the key and account balance: platform.openai.com/settings/organization/billing")
        else:
            self.info("Skipped online API key probe (add --probe to verify, ~$0.0001)")

        return self.finish()

    # ---- Output helpers ----
    def ok(self, msg):
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def warn(self, msg, hint):
        self.stdout.write(self.style.WARNING(f"  ⚠ {msg}\n      ↳ {hint}"))

    def fail(self, msg, hint):
        self.failures += 1
        self.stdout.write(self.style.ERROR(f"  ✗ {msg}\n      ↳ {hint}"))

    def verify(self, cond, msg, hint):
        # Must not be named check() — that would override BaseCommand.check()
        # (Django's system-check hook)
        self.ok(msg) if cond else self.fail(msg, hint)

    def info(self, msg):
        self.stdout.write(f"  · {msg}")

    def finish(self):
        if self.failures:
            self.stdout.write(self.style.ERROR(
                f"\n{self.failures} check(s) failed — fix each one following the ↳ hints"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\nEnvironment ready ✓"))
