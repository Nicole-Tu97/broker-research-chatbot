"""Ingestion pipeline: discover → render → transcribe → index.

python manage.py ingest [--resume] [--limit N] [--file PATH] [--workers 8]
                        [--dry-run] [--render-only]

Design notes:
- Synchronous + bounded concurrency (no Celery/Batch).
- Document.status state machine + --resume give idempotency; content_hash dedupes.
  DONE is written only when every page is complete (png + markdown + embedding) —
  any page failure lands on ERROR with counts recorded, and --resume re-enters to
  backfill (review fix).
- A single failed page does not abort the whole document (the corpus contains
  corrupt XObjects and pages with empty text layers).
- Render DPI is computed per page (render table + per-page physical-size
  normalization; keynote p3 is 53.3×30″).
- --render-only is for make demo: after loaddata, re-render PNGs locally from
  DB records with zero API calls.
"""

import concurrent.futures
import hashlib
import sys
from pathlib import Path

import pymupdf
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections

from research import providers
from research.metadata import date_from_text, parse_filename
from research.models import Document, Page
from research.numeric import suspect_numbers
from research.tickers import extract_ticker_pages

# Render table (tiers set by baseline measurement). Each class defines a baseline
# DPI and baseline page width; actual DPI is normalized to each page's own width
# (constant target pixel width within a class).
REPORT_DPI = 150                      # letter/A4/landscape reports; 100 trips the digit veto on dense tables
SLIDE_TARGET_PX = 2080                # 40×22.5″ large-type decks → ~52 DPI equivalent
KEYNOTE_TARGET_PX = 2880              # keynote → 72 DPI equivalent (150 hallucinates instead)
SLIDE_MIN_WIDTH_IN = 20               # pages wider than 20″ are treated as slides
EMBED_CHAR_LIMIT = 12_000             # see embedding-step comment (calibrated by measurement)


def page_dpi(page_width_in: float, is_keynote: bool) -> int:
    """Report class is fixed at 150 (font sizes are physical pt); slide class is
    normalized by page width to a target pixel width, so keynote p3 (53.3″) and
    the remaining pages (40″) render to the same pixel budget."""
    if page_width_in < SLIDE_MIN_WIDTH_IN:
        return REPORT_DPI
    target = KEYNOTE_TARGET_PX if is_keynote else SLIDE_TARGET_PX
    return max(1, round(target / page_width_in))


class Command(BaseCommand):
    help = "Ingest PDFs under case_study/: render, transcribe, numeric checks, index"

    def add_arguments(self, parser):
        parser.add_argument("--resume", action="store_true", help="Skip documents with status=done")
        parser.add_argument("--limit", type=int, help="Process only the first N documents (sorted by filename)")
        parser.add_argument("--file", type=Path, help="Ingest a single PDF (for adding files live in the demo)")
        parser.add_argument("--workers", type=int, default=8, help="Transcription concurrency")
        parser.add_argument("--dry-run", action="store_true",
                            help="Discovery and rendering only; no external API calls")
        parser.add_argument("--render-only", action="store_true",
                            help="Only re-render missing PNGs (use after make demo's loaddata)")

    def handle(self, *args, **opts):
        pdfs = ([opts["file"]] if opts["file"]
                else sorted(settings.CORPUS_DIR.glob("*.pdf")))
        if opts["limit"]:
            pdfs = pdfs[: opts["limit"]]
        if not pdfs:
            self.stderr.write("No PDFs found")
            sys.exit(2)

        settings.PAGE_ASSET_DIR.mkdir(parents=True, exist_ok=True)
        pymupdf.TOOLS.mupdf_display_errors(False)

        for pdf in pdfs:
            try:
                self.ingest_one(pdf, opts)
            except Exception as exc:  # one failed document does not abort the batch
                self.stderr.write(self.style.ERROR(f"[FAIL] {pdf.name}: {exc}"))
                Document.objects.filter(filename=pdf.name).update(
                    status=Document.Status.ERROR, error=str(exc)[:500])

    # ---- Single-document flow ----

    def ingest_one(self, pdf: Path, opts):
        content_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()

        # Same filename, new content → clear pages and re-ingest (never flip old row to ERROR)
        doc = Document.objects.filter(filename=pdf.name).first()
        if doc and doc.content_hash != content_hash:
            self.stdout.write(f"[REDO] {pdf.name}: content changed, re-ingesting")
            doc.pages.all().delete()
            doc.content_hash = content_hash
            doc.status = Document.Status.PENDING
        elif doc is None:
            # content_hash dedupe: same content, different filename → no-op (demo script Q4)
            dup = Document.objects.filter(content_hash=content_hash).first()
            if dup:
                self.stdout.write(
                    f"[SKIP] {pdf.name}: same content as {dup.filename} (dedupe no-op)")
                return
            meta = parse_filename(pdf.stem)
            doc = Document(
                filename=pdf.name, content_hash=content_hash,
                broker=meta.broker, published_date=meta.published_date,
                title=meta.title, tickers=[meta.ticker] if meta.ticker else [],
            )
        if (opts["resume"] and not opts["render_only"]
                and doc.status == Document.Status.DONE):
            self.stdout.write(f"[SKIP] {pdf.name}: already done")
            return
        doc.save()

        # 1+2. Render + text layer (DPI normalized per page)
        is_keynote = "keynote" in pdf.name.lower()
        render_failures = 0
        with pymupdf.open(pdf) as fitz_doc:
            doc.page_count = len(fitz_doc)
            for i, fpage in enumerate(fitz_doc, start=1):
                page, _ = Page.objects.get_or_create(document=doc, page_number=i)
                png_name = f"{doc.id}_{i}.png"  # store basename only; keeps fixtures portable
                png_file = settings.PAGE_ASSET_DIR / png_name
                if page.png_path and png_file.exists():
                    continue  # render idempotency: skip if the file exists
                try:
                    dpi = page_dpi(fpage.rect.width / 72, is_keynote)
                    fpage.get_pixmap(dpi=dpi).save(png_file)
                    page.png_path = png_name
                    page.raw_text = fpage.get_text().strip()
                    page.save()
                except Exception as exc:  # per-page fault tolerance
                    render_failures += 1
                    self.stderr.write(f"  [page {i}] render failed: {exc}")

        # Metadata fallback from page-1 content (filename first, content check as backup)
        p1 = doc.pages.filter(page_number=1).first()
        if p1 and p1.raw_text:
            content_date = date_from_text(p1.raw_text)
            if content_date and doc.published_date is None:
                doc.published_date = content_date
                self.stdout.write(f"  [meta] date backfilled from page 1: {content_date}")
            elif content_date and doc.published_date and content_date != doc.published_date:
                self.stderr.write(
                    f"  [meta] date mismatch: filename {doc.published_date} vs page 1 {content_date}"
                    " (keeping filename value; logged)")

        doc.status = Document.Status.RENDERED
        doc.save()
        self.stdout.write(f"[RENDER] {pdf.name}: {doc.page_count} pages")
        if opts["dry_run"] or opts["render_only"]:
            return

        # 3. Transcribe (sync + bounded concurrency; workers close thread-local DB connections)
        todo = list(doc.pages.exclude(png_path="").filter(markdown__isnull=True))
        transcribe_failures = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=opts["workers"]) as pool:
            futs = {pool.submit(self._transcribe, p): p for p in todo}
            for fut in concurrent.futures.as_completed(futs):
                p = futs[fut]
                try:
                    fut.result()
                except Exception as exc:
                    transcribe_failures += 1
                    self.stderr.write(f"  [page {p.page_number}] transcription failed: {exc}")
        doc.status = Document.Status.TRANSCRIBED
        doc.save()

        # 4. Index: numeric checks → ticker extraction → embeddings
        pages = list(doc.pages.order_by("page_number"))
        for p in pages:
            flags = suspect_numbers(p.markdown or "", p.raw_text)
            p.numeric_flags = flags or None
        Page.objects.bulk_update(pages, ["numeric_flags"])

        hits = extract_ticker_pages([f"{p.raw_text}\n{p.markdown or ''}" for p in pages])
        primary = doc.tickers[:1]
        doc.ticker_pages = hits
        doc.tickers = primary + sorted(t for t in hits if t not in primary)

        # Truncation cap: the model's hard limit is 8,192 tokens. The **measured**
        # ratio on the densest financial pages is 1.76 chars/token (not English
        # prose's ~4:1 — digits and symbols each take a token), so 8,192 × 1.76
        # ≈ 14.4k chars is the theoretical cap; 12,000 leaves ~15% headroom.
        # The initial 6,000 was overly conservative (clipped 7/423 pages);
        # 30,000 blew straight past the limit and errored — both mistakes used
        # the wrong ratio; the third value was measured.
        to_embed = [p for p in pages if p.markdown and p.embedding is None]
        for batch_start in range(0, len(to_embed), 64):
            batch = to_embed[batch_start: batch_start + 64]
            vecs = providers.embed([p.markdown[:EMBED_CHAR_LIMIT] for p in batch])
            for p, v in zip(batch, vecs):
                p.embedding = v
            Page.objects.bulk_update(batch, ["embedding"])

        # DONE gate: all pages complete, else ERROR + counts; --resume re-enters to backfill
        incomplete = sum(
            1 for p in doc.pages.all()
            if not p.png_path or p.markdown is None
            or (p.markdown and p.embedding is None))  # "" = valid empty transcription (disclaimer pages)
        if render_failures or transcribe_failures or incomplete:
            doc.status = Document.Status.ERROR
            doc.error = (f"{incomplete} pages incomplete"
                         f" (render failures: {render_failures}, "
                         f"transcription failures: {transcribe_failures}); "
                         "re-run ingest to backfill")
            doc.save()
            self.stderr.write(self.style.WARNING(f"[PART] {pdf.name}: {doc.error}"))
            return

        doc.status = Document.Status.DONE
        doc.error = ""
        doc.save()
        n_flags = sum(1 for p in pages if p.numeric_flags)
        self.stdout.write(self.style.SUCCESS(
            f"[DONE] {pdf.name}: {len(pages)} pages, tickers={doc.tickers}, "
            f"suspect-number pages={n_flags}"))

    @staticmethod
    def _transcribe(page: Page):
        try:
            png = (settings.PAGE_ASSET_DIR / page.png_path).read_bytes()
            markdown, has_visual, _usage = providers.transcribe_page(png, page.raw_text)
            page.markdown = markdown
            page.has_visual = has_visual
            page.save()
        finally:
            connections.close_all()  # never leave thread-local connections to the GC (review fix)
