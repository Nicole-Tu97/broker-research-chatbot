"""ASGI entry point.

Exposes the Django project as an ASGI `application` object - the interface an
async-capable web server (uvicorn, see docker-compose.yml) loads to serve the app.
ASGI rather than WSGI because the chat endpoint streams answers over SSE.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
