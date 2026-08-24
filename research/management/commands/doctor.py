"""环境体检：clone 之后一条命令确认整个栈是否就绪（借鉴自社区实践，零 API 成本）。

python manage.py doctor [--probe]

逐项检查并给出修复指引；--probe 额外做一次最小 embedding 调用验证 API key
有效性（约 $0.0001，默认不做）。退出码：0 = 全部通过或仅警告，1 = 有失败项。
"""

import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "检查环境、数据库、索引、语料与资产是否就绪"

    def add_arguments(self, parser):
        parser.add_argument("--probe", action="store_true",
                            help="额外做一次最小 embedding 调用验证 API key（~$0.0001）")

    def handle(self, *args, **opts):
        self.failures = 0

        # ① 环境变量
        key = settings.OPENAI_API_KEY
        self.verify(bool(key), "OPENAI_API_KEY 已设置",
                   "复制 .env.example 为 .env 并填入 key，然后 export $(grep -v '^#' .env | xargs)")
        if key:
            self.verify(key.startswith("sk-"), "OPENAI_API_KEY 格式（sk-*）",
                       "key 看起来不像 OpenAI 格式，请核对")
        self.info(f"模型配置: {settings.OPENAI_VISION_MODEL} / "
                  f"{settings.OPENAI_EMBED_MODEL}({settings.EMBED_DIMENSIONS}d) / "
                  f"detail={settings.OPENAI_IMAGE_DETAIL}")

        # ② 数据库连通
        try:
            connection.ensure_connection()
            db = settings.DATABASES["default"]
            self.ok(f"Postgres 可连接（{db['HOST']}:{db['PORT']}/{db['NAME']}）")
        except Exception as exc:
            self.fail(f"Postgres 连接失败: {str(exc)[:120]}",
                      "docker compose up -d db；或本地 pg_ctl start（见 README）")
            return self.finish()  # 后续检查全依赖 DB

        # ③ pgvector 扩展
        with connection.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            row = cur.fetchone()
        self.verify(bool(row), f"pgvector 扩展（{row[0] if row else '缺失'}）",
                   "CREATE EXTENSION vector;（docker 镜像自带；本地 brew install pgvector）")

        # ④ 迁移是否打齐
        plan = MigrationExecutor(connection).migration_plan(
            MigrationExecutor(connection).loader.graph.leaf_nodes())
        self.verify(not plan, "迁移已全部应用",
                   f"有 {len(plan)} 个未应用：运行 make migrate")

        # ⑤ 数据与索引
        from research.models import Document, Page
        n_doc = Document.objects.count()
        n_done = Document.objects.filter(status=Document.Status.DONE).count()
        n_page = Page.objects.count()
        n_emb = Page.objects.exclude(embedding=None).count()
        if n_doc == 0:
            self.warn("知识库为空",
                      "make demo 加载 fixture（秒级、免费），或 make ingest 全量摄取（~1h/$23.5）")
        else:
            self.ok(f"知识库: {n_doc} 文档（{n_done} 完成）· {n_page} 页 · {n_emb} 向量")
            err = Document.objects.filter(status=Document.Status.ERROR).count()
            if err:
                self.warn(f"{err} 份文档处于 ERROR 状态",
                          "重跑 make ingest 会从断点续起补齐")
        with connection.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='research_page'")
            idx = {r[0] for r in cur.fetchall()}
        for name, label in [("page_embedding_hnsw", "HNSW 向量索引"),
                            ("page_search_gin", "GIN 全文索引")]:
            self.verify(name in idx, label, "运行 make migrate 重建索引")

        # ⑥ 语料与资产
        pdfs = list(settings.CORPUS_DIR.glob("*.pdf")) if settings.CORPUS_DIR.exists() else []
        self.verify(len(pdfs) > 0, f"语料目录（{len(pdfs)} 份 PDF）",
                   f"确认 PDF 在 {settings.CORPUS_DIR}")
        if n_page:
            missing_png = sum(
                1 for p in Page.objects.exclude(png_path="").only("png_path")
                if not (settings.PAGE_ASSET_DIR / p.png_path).exists())
            self.verify(missing_png == 0, "页面 PNG 资产齐全",
                       f"{missing_png} 页缺图：运行 make render（本地重渲，免费）")
        fixture = Path(settings.BASE_DIR) / "fixtures" / "corpus.json.gz"
        self.verify(fixture.exists(), "索引 fixture 存在（make demo 依赖）",
                   "fixture 缺失：跑完 ingest 后 dumpdata 重建（见 §6.4）")

        # ⑦ 可选：API key 有效性探测
        if opts["probe"]:
            try:
                from research import providers
                vec = providers.embed(["doctor probe"])[0]
                self.ok(f"API key 有效（embedding 返回 {len(vec)} 维）")
            except Exception as exc:
                self.fail(f"API 调用失败: {str(exc)[:120]}",
                          "核对 key 与账户余额：platform.openai.com/settings/organization/billing")
        else:
            self.info("跳过 API key 在线探测（加 --probe 验证，约 $0.0001）")

        return self.finish()

    # ---- 输出辅助 ----
    def ok(self, msg):
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def warn(self, msg, hint):
        self.stdout.write(self.style.WARNING(f"  ⚠ {msg}\n      ↳ {hint}"))

    def fail(self, msg, hint):
        self.failures += 1
        self.stdout.write(self.style.ERROR(f"  ✗ {msg}\n      ↳ {hint}"))

    def verify(self, cond, msg, hint):
        # 注意不能叫 check()——会覆盖 BaseCommand.check()（Django 系统检查钩子）
        self.ok(msg) if cond else self.fail(msg, hint)

    def info(self, msg):
        self.stdout.write(f"  · {msg}")

    def finish(self):
        if self.failures:
            self.stdout.write(self.style.ERROR(f"\n{self.failures} 项失败——按 ↳ 提示逐项修复"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\n环境就绪 ✓"))
