import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Locally nothing needs setting. On Render, set DJANGO_SECRET_KEY in the dashboard.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = ["*"]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

CSRF_TRUSTED_ORIGINS = [
    "https://*.railway.app",
    "https://*.up.railway.app",
    "http://*.railway.app",
    "http://*.up.railway.app",
    "https://*.vercel.app",
    "https://*.now.sh",
    "https://*.render.com",
    "https://*.onrender.com",
    "http://localhost",
    "http://127.0.0.1",
]

# Add Railway environment domains if present
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
if RAILWAY_PUBLIC_DOMAIN:
    CSRF_TRUSTED_ORIGINS.append(f"https://{RAILWAY_PUBLIC_DOMAIN}")
    CSRF_TRUSTED_ORIGINS.append(f"http://{RAILWAY_PUBLIC_DOMAIN}")

VERCEL_URL = os.environ.get("VERCEL_URL")
if VERCEL_URL:
    url = f"https://{VERCEL_URL}" if not VERCEL_URL.startswith("http") else VERCEL_URL
    if url not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(url)

env_origins = os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
for o in env_origins:
    o = o.strip()
    if o and o not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(o)

# Server-side lock on real submission. The Settings switch alone is not enough: real submission
# also needs KOBO_ALLOW_SUBMIT=1 in the environment. Not set = the app can never submit.
# Locally (DEBUG on) the Settings switch is enough.
SUBMIT_ENV_UNLOCKED = DEBUG or os.environ.get("KOBO_ALLOW_SUBMIT") == "1"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "filler",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "kobo_web.middleware.DynamicCsrfOriginMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "kobo_web.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "kobo_web.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 30},  # the background filler writes while pages read
    }
}

MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000  # review page posts one input per question
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024  # uploaded sheets are stored in the database
