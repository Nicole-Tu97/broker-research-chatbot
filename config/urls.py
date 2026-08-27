"""URL routing.

Maps every path the app serves to its view: the chat page, the SSE chat API, page
images (full or cropped) for citations and figure cards, the original PDF for
click-through, and a health check.
"""
from django.http import JsonResponse
from django.urls import path

from research import views


def health(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", views.index),
    path("api/chat", views.chat_api),
    path("page-image/<int:document_id>/<int:page_number>", views.page_image),
    path("pdf/<int:document_id>", views.document_pdf),
    path("health/", health),
]
