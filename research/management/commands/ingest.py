"""摄取管线：发现 → 渲染 → 转录 → 索引。

python manage.py ingest [--resume] [--limit N] [--file PATH] [--workers 8]
                        [--dry-run] [--render-only]

设计要点：
- 同步 + 有界并发（无 Celery/Batch）。
- Document.status 状态机 + --resume 提供幂等；content_hash 去重。
  DONE 只在全部页完整（png + markdown + embedding）时才写入——任何页失败
  则落 ERROR 并记录数量，--resume 会重入补齐（评审修正）。
- 单页失败不中断整份文档（语料含损坏 XObject 与空文本层页）。
- 渲染 DPI 按页计算（渲染表 + 每页物理尺寸归一；keynote p3 是 53.3×30″）。
- --render-only 供 make demo 使用：loaddata 后按库内记录本地重渲 PNG，零 API 调用。
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

# 渲染表（基准实测定档）。类别给出基准 DPI 与基准页宽，
# 实际 DPI 按每页自身宽度归一（同类内目标像素宽恒定）。
REPORT_DPI = 150                      # letter/A4/横版研报；100 在密集表上有数字 veto
SLIDE_TARGET_PX = 2080                # 40×22.5″ 大字 deck → 52 DPI 等效
KEYNOTE_TARGET_PX = 2880              # keynote → 72 DPI 等效（150 反而幻觉）
SLIDE_MIN_WIDTH_IN = 20               # 页宽超过 20″ 视为幻灯片类
EMBED_CHAR_LIMIT = 12_000             # 见 embedding 步骤注释（实测校准）


def page_dpi(page_width_in: float, is_keynote: bool) -> int:
    """研报类固定 150（字号口径是物理 pt）；幻灯片类按页宽归一到目标像素宽，
    使 keynote p3（53.3″）与其余页（40″）渲染到同一像素预算。"""
    if page_width_in < SLIDE_MIN_WIDTH_IN:
        return REPORT_DPI
    target = KEYNOTE_TARGET_PX if is_keynote else SLIDE_TARGET_PX
    return max(1, round(target / page_width_in))


class Command(BaseCommand):
    help = "摄取 case_study/ 下的 PDF：渲染、转录、数字校验、索引"

    def add_arguments(self, parser):
        parser.add_argument("--resume", action="store_true", help="跳过 status=done 的文档")
        parser.add_argument("--limit", type=int, help="只处理前 N 份文档（按文件名排序）")
        parser.add_argument("--file", type=Path, help="只摄取单个 PDF（demo 现场新增用）")
        parser.add_argument("--workers", type=int, default=8, help="转录并发数")
        parser.add_argument("--dry-run", action="store_true",
                            help="只做发现与渲染，不调用任何外部 API")
        parser.add_argument("--render-only", action="store_true",
                            help="只补渲缺失的 PNG（make demo 的 loaddata 之后用）")

    def handle(self, *args, **opts):
        pdfs = ([opts["file"]] if opts["file"]
                else sorted(settings.CORPUS_DIR.glob("*.pdf")))
        if opts["limit"]:
            pdfs = pdfs[: opts["limit"]]
        if not pdfs:
            self.stderr.write("没有找到 PDF")
            sys.exit(2)

        settings.PAGE_ASSET_DIR.mkdir(parents=True, exist_ok=True)
        pymupdf.TOOLS.mupdf_display_errors(False)

        for pdf in pdfs:
            try:
                self.ingest_one(pdf, opts)
            except Exception as exc:  # 单文档失败不中断整批
                self.stderr.write(self.style.ERROR(f"[FAIL] {pdf.name}: {exc}"))
                Document.objects.filter(filename=pdf.name).update(
                    status=Document.Status.ERROR, error=str(exc)[:500])

    # ---- 单文档流程 ----

    def ingest_one(self, pdf: Path, opts):
        content_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()

        # 同名文件内容变化 → 清页重摄（不得把旧行翻成 ERROR）
        doc = Document.objects.filter(filename=pdf.name).first()
        if doc and doc.content_hash != content_hash:
            self.stdout.write(f"[REDO] {pdf.name}: 内容已变化，重摄取")
            doc.pages.all().delete()
            doc.content_hash = content_hash
            doc.status = Document.Status.PENDING
        elif doc is None:
            # content_hash 去重：同内容不同文件名 → no-op（demo 剧本第 4 问）
            dup = Document.objects.filter(content_hash=content_hash).first()
            if dup:
                self.stdout.write(
                    f"[SKIP] {pdf.name}: 内容与 {dup.filename} 相同（去重 no-op）")
                return
            meta = parse_filename(pdf.stem)
            doc = Document(
                filename=pdf.name, content_hash=content_hash,
                broker=meta.broker, published_date=meta.published_date,
                title=meta.title, tickers=[meta.ticker] if meta.ticker else [],
            )
        if (opts["resume"] and not opts["render_only"]
                and doc.status == Document.Status.DONE):
            self.stdout.write(f"[SKIP] {pdf.name}: 已完成")
            return
        doc.save()

        # 1+2. 渲染 + 文本层（DPI 按页归一）
        is_keynote = "keynote" in pdf.name.lower()
        render_failures = 0
        with pymupdf.open(pdf) as fitz_doc:
            doc.page_count = len(fitz_doc)
            for i, fpage in enumerate(fitz_doc, start=1):
                page, _ = Page.objects.get_or_create(document=doc, page_number=i)
                png_name = f"{doc.id}_{i}.png"  # 只存 basename，fixture 可迁移
                png_file = settings.PAGE_ASSET_DIR / png_name
                if page.png_path and png_file.exists():
                    continue  # 渲染幂等：文件在即跳过
                try:
                    dpi = page_dpi(fpage.rect.width / 72, is_keynote)
                    fpage.get_pixmap(dpi=dpi).save(png_file)
                    page.png_path = png_name
                    page.raw_text = fpage.get_text().strip()
                    page.save()
                except Exception as exc:  # 页级容错
                    render_failures += 1
                    self.stderr.write(f"  [page {i}] 渲染失败: {exc}")

        # 首页内容侧元数据兜底（文件名为主，内容校验兜底）
        p1 = doc.pages.filter(page_number=1).first()
        if p1 and p1.raw_text:
            content_date = date_from_text(p1.raw_text)
            if content_date and doc.published_date is None:
                doc.published_date = content_date
                self.stdout.write(f"  [meta] 首页内容补齐日期: {content_date}")
            elif content_date and doc.published_date and content_date != doc.published_date:
                self.stderr.write(
                    f"  [meta] 日期不一致: 文件名 {doc.published_date} vs 首页 {content_date}"
                    "（保留文件名值，已记录）")

        doc.status = Document.Status.RENDERED
        doc.save()
        self.stdout.write(f"[RENDER] {pdf.name}: {doc.page_count} 页")
        if opts["dry_run"] or opts["render_only"]:
            return

        # 3. 转录（同步 + 有界并发；worker 内关闭线程私有 DB 连接）
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
                    self.stderr.write(f"  [page {p.page_number}] 转录失败: {exc}")
        doc.status = Document.Status.TRANSCRIBED
        doc.save()

        # 4. 索引：数字校验 → ticker 提取 → embedding
        pages = list(doc.pages.order_by("page_number"))
        for p in pages:
            flags = suspect_numbers(p.markdown or "", p.raw_text)
            p.numeric_flags = flags or None
        Page.objects.bulk_update(pages, ["numeric_flags"])

        hits = extract_ticker_pages([f"{p.raw_text}\n{p.markdown or ''}" for p in pages])
        primary = doc.tickers[:1]
        doc.ticker_pages = hits
        doc.tickers = primary + sorted(t for t in hits if t not in primary)

        # 截断上限：模型硬约束 8,192 token。**实测**最密财务页的比率是
        # 1.76 char/token（不是英文散文的 ~4:1——数字符号各占一个 token），
        # 故 8,192 × 1.76 ≈ 14.4k 字符是理论上限，取 12,000 留 ~15% 余量。
        # 初版 6,000 过度保守（误伤 7/423 页）；改 30,000 则直接超限报错——
        # 两次都错在用错换算比率，第三次是量出来的。
        to_embed = [p for p in pages if p.markdown and p.embedding is None]
        for batch_start in range(0, len(to_embed), 64):
            batch = to_embed[batch_start: batch_start + 64]
            vecs = providers.embed([p.markdown[:EMBED_CHAR_LIMIT] for p in batch])
            for p, v in zip(batch, vecs):
                p.embedding = v
            Page.objects.bulk_update(batch, ["embedding"])

        # DONE 门槛：全部页完整才算完成，否则 ERROR + 计数，--resume 可重入补齐
        incomplete = sum(
            1 for p in doc.pages.all()
            if not p.png_path or p.markdown is None
            or (p.markdown and p.embedding is None))  # "" = 合法空转录（免责声明页）
        if render_failures or transcribe_failures or incomplete:
            doc.status = Document.Status.ERROR
            doc.error = (f"{incomplete} 页未完成"
                         f"（渲染失败 {render_failures}，转录失败 {transcribe_failures}）；"
                         "重跑 ingest 可补齐")
            doc.save()
            self.stderr.write(self.style.WARNING(f"[PART] {pdf.name}: {doc.error}"))
            return

        doc.status = Document.Status.DONE
        doc.error = ""
        doc.save()
        n_flags = sum(1 for p in pages if p.numeric_flags)
        self.stdout.write(self.style.SUCCESS(
            f"[DONE] {pdf.name}: {len(pages)} 页, tickers={doc.tickers}, "
            f"可疑数字页={n_flags}"))

    @staticmethod
    def _transcribe(page: Page):
        try:
            png = (settings.PAGE_ASSET_DIR / page.png_path).read_bytes()
            markdown, has_visual, _usage = providers.transcribe_page(png, page.raw_text)
            page.markdown = markdown
            page.has_visual = has_visual
            page.save()
        finally:
            connections.close_all()  # 线程私有连接不留给 GC（评审修正）
