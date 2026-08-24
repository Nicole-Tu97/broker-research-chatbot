"""视图：聊天页 + SSE 流式对话 API + 页面资产。

对话 API 返回 text/event-stream：进度事件（round/tool/tool_result）+ 答案增量
（delta）+ 终帧（final，携带与旧 JSON 响应完全相同的 payload）。流式不改变
总耗时，改变的是"第一个字出现"的时间。
早期校验错误（空消息、不支持的文件类型）仍返回 JSON——前端按 content-type 区分。"""

import base64
import json
import queue
import threading
import uuid

from django.conf import settings
from django.db import connections
from django.http import (FileResponse, Http404, HttpResponse, JsonResponse,
                         StreamingHttpResponse)
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import chat as chat_mod
from .models import Conversation, Document, Page


class _ClientGone(Exception):
    """客户端已断开——用于从 emit 回调中止工作线程里的 run_turn。"""


def index(request):
    return render(request, "research/chat.html",
                  {"boundary": chat_mod.corpus_boundary()})


@csrf_exempt  # 内部 demo，无认证面（DESIGN.md §10）
@require_POST
def chat_api(request):
    text = (request.POST.get("message") or "").strip()
    if not text:
        return JsonResponse({"error": "message must not be empty"}, status=400)

    conv_id = request.POST.get("conversation_id")
    conv = None
    if conv_id:
        try:
            conv = Conversation.objects.filter(id=uuid.UUID(conv_id)).first()
        except ValueError:
            pass
    if conv is None:
        conv = Conversation.objects.create()

    image_b64 = pdf_b64 = None
    pdf_name = "upload.pdf"
    up = request.FILES.get("file")
    if up:
        blob = up.read()
        if up.content_type == "application/pdf" or up.name.lower().endswith(".pdf"):
            pdf_b64, pdf_name = base64.b64encode(blob).decode(), up.name
        elif (up.content_type or "").startswith("image/"):
            image_b64 = base64.b64encode(blob).decode()
        else:  # 其他格式显式优雅拒绝
            return JsonResponse({"error": f"Unsupported type {up.content_type or up.name} "
                                          "— text, images, and PDF are accepted"}, status=415)

    q: queue.Queue = queue.Queue()
    dead = threading.Event()  # 客户端断开的信号（审查确认的泄漏修复）

    def emit(e):
        # 客户端已断开 → 在下一个事件点中止 run_turn：不再烧 API 调用、
        # 不落 conversation.save()（半途的轮不该进历史）
        if dead.is_set():
            raise _ClientGone()
        q.put(e)

    def work():
        try:
            result = chat_mod.run_turn(conv, text, image_b64=image_b64,
                                       pdf_b64=pdf_b64, pdf_name=pdf_name,
                                       emit=emit)
            result["conversation_id"] = str(conv.id)
            q.put({"type": "final", "payload": result})
        except _ClientGone:
            pass  # 没有观众了，安静收场
        except chat_mod.providers.ProviderError as exc:
            q.put({"type": "error", "error": f"Upstream call failed: {exc}"})
        except Exception as exc:  # 工作线程的异常必须显式送达前端，不能静默吞掉
            q.put({"type": "error", "error": str(exc)[:300]})
        finally:
            connections.close_all()  # 线程私有 DB 连接，用完即关（同 ingest workers）
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        # 断开时 WSGI 关闭生成器 → GeneratorExit 走 finally → 通知工作线程。
        # 检测粒度是"下一次 yield"，工作线程在其后的第一个 emit 点退出——
        # 模型思考中的长静默段无法立刻中止，这是同步 WSGI 下的已知边界。
        try:
            while True:
                e = q.get()
                if e is None:
                    break
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
        finally:
            dead.set()

    resp = StreamingHttpResponse(gen(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # 反向代理（如 nginx）不得缓冲
    return resp


def page_image(request, document_id: int, page_number: int):
    """页面图。可选 ?crop=x0,y0,x1,y1（页面宽高的百分比,0-100）——
    从 PDF 按 clip 重渲该区域（图形裁剪),坐标非法时回退整页而非报错。"""
    page = (Page.objects.filter(document_id=document_id, page_number=page_number)
            .select_related("document").first())
    if not page or not page.png_path:
        raise Http404
    crop = request.GET.get("crop")
    if crop:
        try:
            x0, y0, x1, y1 = (float(v) for v in crop.split(","))
            if not (0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100):
                raise ValueError
            import pymupdf
            pdf = pymupdf.open(str(settings.CORPUS_DIR / page.document.filename))
            pg = pdf[page_number - 1]
            r = pg.rect
            clip = pymupdf.Rect(r.width * x0 / 100, r.height * y0 / 100,
                                r.width * x1 / 100, r.height * y1 / 100)
            pix = pg.get_pixmap(dpi=120, clip=clip)
            return HttpResponse(pix.tobytes("png"), content_type="image/png")
        except Exception:
            pass  # 裁剪失败 → 整页兜底
    f = settings.PAGE_ASSET_DIR / page.png_path
    if not f.exists():
        raise Http404
    return FileResponse(open(f, "rb"), content_type="image/png")


def document_pdf(request, document_id: int):
    doc = Document.objects.filter(id=document_id).first()
    if not doc:
        raise Http404
    f = settings.CORPUS_DIR / doc.filename
    if not f.exists():
        raise Http404
    return FileResponse(open(f, "rb"), content_type="application/pdf")
