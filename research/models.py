"""三张表（ARCHITECTURE.md §6.0）。

- Page 为检索原子：markdown 是检索语料，raw_text 是数字校验与调和的依据。
- search_vector 是数据库生成列——转录写入即索引，无应用侧同步代码。
- Conversation.messages 存整个消息列表；图像只存引用 (document_id, page_number,
  png_path)，构造请求时按当轮 rehydrate（§6.0，DECISION-LOG §七.7）。
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
    # mentioned-tickers 语义（§6.0）：提及即标，主 ticker（文件名解析）排首位
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
    page_number = models.IntegerField()  # 1-indexed，引用锚点
    raw_text = models.TextField(blank=True, default="")
    # NULL = 尚未转录；"" = 已转录且合法为空（免责声明页被 prompt 忽略，§8.1.1.3）。
    # 这是 TextField 用 null 的教科书反例场景——恰恰需要三态。
    markdown = models.TextField(null=True, blank=True, default=None)
    has_visual = models.BooleanField(default=False)
    png_path = models.TextField(blank=True, default="")
    embedding = VectorField(dimensions=1024, null=True, blank=True)
    search_vector = models.GeneratedField(
        expression=SearchVector("markdown", config="english"),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    numeric_flags = models.JSONField(null=True, blank=True)  # 可疑数字列表（§4.3）

    @property
    def png_abspath(self):
        """png_path 只存 basename（fixture 可跨机器迁移，§6.4）；读取侧在此拼接。"""
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
