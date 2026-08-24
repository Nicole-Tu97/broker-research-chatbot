"""Three tables.

- Page is the retrieval atom: markdown is the retrieval corpus, raw_text is
  the basis for numeric checks and reconciliation.
- search_vector is a database-generated column — transcriptions are indexed on
  write, with no application-side sync code.
- Conversation.messages stores the whole message list; images are stored as
  references only (document_id, page_number, png_path) and rehydrated per turn
  when building the request.
"""

import uuid

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from pgvector.django import HnswIndex, VectorField


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        RENDERED = "rendered"
        TRANSCRIBED = "transcribed"
        DONE = "done"
        ERROR = "error"

    filename = models.TextField(unique=True)
    content_hash = models.CharField(max_length=64, unique=True)
    broker = models.TextField(blank=True, default="")
    published_date = models.DateField(null=True, blank=True)
    # mentioned-tickers semantics: any mention is tagged; the primary ticker
    # (parsed from the filename) comes first
    tickers = ArrayField(models.TextField(), default=list, blank=True)
    ticker_pages = models.JSONField(default=dict, blank=True)  # {"NVDA": [1, 7, 12]}
    title = models.TextField(blank=True, default="")
    page_count = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    error = models.TextField(blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["broker", "published_date"])]

    def __str__(self):
        return f"{self.broker} {self.published_date} {self.title[:40]}"


class Page(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="pages")
    page_number = models.IntegerField()  # 1-indexed, citation anchor
    raw_text = models.TextField(blank=True, default="")
    # NULL = not yet transcribed; "" = transcribed and legitimately empty
    # (disclaimer pages the prompt is told to ignore). This is the textbook
    # counterexample to "never use null on a TextField" — three states are
    # exactly what we need.
    markdown = models.TextField(null=True, blank=True, default=None)
    has_visual = models.BooleanField(default=False)
    png_path = models.TextField(blank=True, default="")
    embedding = VectorField(dimensions=1024, null=True, blank=True)
    search_vector = models.GeneratedField(
        expression=SearchVector("markdown", config="english"),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    numeric_flags = models.JSONField(null=True, blank=True)  # suspect-number list (ingest-time check output)

    @property
    def png_abspath(self):
        """png_path stores only the basename (fixtures stay portable across
        machines); the read side joins the full path here."""
        from django.conf import settings

        return settings.PAGE_ASSET_DIR / self.png_path if self.png_path else None

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document", "page_number"], name="uniq_doc_page"),
        ]
        indexes = [
            HnswIndex(
                name="page_embedding_hnsw",
                fields=["embedding"],
                opclasses=["vector_cosine_ops"],
            ),
            GinIndex(fields=["search_vector"], name="page_search_gin"),
        ]

    def __str__(self):
        return f"{self.document_id} p{self.page_number}"


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    messages = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
