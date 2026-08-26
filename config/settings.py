"""Minimal Django configuration.

What we skip and why (see DESIGN.md §10): no auth/admin/sessions (irrelevant to
the assessment), no frontend framework (templates suffice). .env is injected by
the shell or docker compose; no dotenv dependency: locally run
`export $(grep -v '^#' .env | xargs)`, in containers compose env_file handles it.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]  # internal demo; no public exposure

INSTALLED_APPS = [
    "research",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "research"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "postgres"),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TIME_ZONE = "UTC"

# ---- Project-specific settings (external calls centralized in research/providers.py) ----

CORPUS_DIR = Path(os.environ.get("CORPUS_DIR", BASE_DIR / "case_study"))
PAGE_ASSET_DIR = Path(os.environ.get("PAGE_ASSET_DIR", BASE_DIR / "page_assets"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-5.6-sol")
OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-large")
OPENAI_IMAGE_DETAIL = os.environ.get("OPENAI_IMAGE_DETAIL", "original")
EMBED_DIMENSIONS = 1024
