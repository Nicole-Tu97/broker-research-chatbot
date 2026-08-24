"""最小化 Django 配置。

不做的事与理由（见 DESIGN.md §10）：无 auth/admin/sessions（与考察点无关），
无前端框架（模板足够）。.env 由 shell 或 docker compose 注入，不引入 dotenv 依赖：
本地跑 `export $(grep -v '^#' .env | xargs)`，容器里 compose env_file 处理。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]  # 内部 demo；无公网暴露面

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "research",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
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

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

# ---- 项目自有配置（外部调用集中在 research/providers.py）----

CORPUS_DIR = Path(os.environ.get("CORPUS_DIR", BASE_DIR / "case_study"))
PAGE_ASSET_DIR = Path(os.environ.get("PAGE_ASSET_DIR", BASE_DIR / "page_assets"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-5.6-sol")
OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-large")
OPENAI_IMAGE_DETAIL = os.environ.get("OPENAI_IMAGE_DETAIL", "original")
EMBED_DIMENSIONS = 1024
