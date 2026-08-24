"""Views: chat page + SSE streaming chat API + page assets.

The chat API returns text/event-stream: progress events (round/tool/
tool_result) + answer increments (delta) + a final frame (final, carrying the
exact same payload as the old JSON response). Streaming does not change total
latency — it changes when the first character appears.
Early validation errors (empty message, unsupported file type) still return
JSON — the frontend distinguishes by content-type."""

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
    """Client has disconnected — used to abort run_turn in the worker thread
    from the emit callback."""


def index(request):
    return render(request, "research/chat.html",
                  {"boundary": chat_mod.corpus_boundary()})


@csrf_exempt  # internal demo, no auth surface (DESIGN.md §10)
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
        else:  # explicitly and gracefully reject other formats
            return JsonResponse({"error": f"Unsupported type {up.content_type or up.name} "
                                          "— text, images, and PDF are accepted"}, status=415)

    q: queue.Queue = queue.Queue()
    dead = threading.Event()  # client-disconnect signal (leak fix confirmed in review)

    def emit(e):
        # Client gone -> abort run_turn at the next event point: burn no more
        # API calls, skip conversation.save() (a half-finished turn must not
        # enter history)
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
            pass  # nobody is watching, wind down quietly
        except chat_mod.providers.ProviderError as exc:
            q.put({"type": "error", "error": f"Upstream call failed: {exc}"})
        except Exception as exc:  # worker-thread exceptions must reach the frontend, never be swallowed
            q.put({"type": "error", "error": str(exc)[:300]})
        finally:
            connections.close_all()  # thread-local DB connections, closed when done (same as ingest workers)
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        # On disconnect WSGI closes the generator -> GeneratorExit hits
        # finally -> signals the worker thread. Detection granularity is "the
        # next yield"; the worker exits at its first emit after that — a long
        # silent stretch while the model is thinking cannot be aborted
        # immediately, a known limit of synchronous WSGI.
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
    resp["X-Accel-Buffering"] = "no"  # reverse proxies (e.g. nginx) must not buffer
    return resp


def page_image(request, document_id: int, page_number: int):
    """Page image. Optional ?crop=x0,y0,x1,y1 (percent of page width/height,
    0-100) — re-renders that region from the PDF via clip (graphical crop);
    invalid coordinates fall back to the full page instead of erroring."""
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
            pass  # crop failed -> fall back to the full page
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
