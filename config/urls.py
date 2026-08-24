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
