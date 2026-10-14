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

# The container's own disk is wiped on every redeploy/restart. Put the database on a persistent
# disk instead: SQLITE_PATH wins; otherwise Railway's volume (RAILWAY_VOLUME_MOUNT_PATH is set
# automatically when a volume is attached); otherwise the project folder (local use).
_db_dir = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH") or (Path(_db_dir) / "db.sqlite3" if _db_dir else BASE_DIR / "db.sqlite3"))
SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": SQLITE_PATH,
        "OPTIONS": {"timeout": 30},  # the background filler writes while pages read
    }
}

# Oracle (e.g. Oracle Cloud Autonomous Database, free tier) when ORACLE_DSN is set; SQLite otherwise.
#   ORACLE_DSN       connect string from the DB's "Database connection" page, e.g. "(description=...)"
#   ORACLE_USER / ORACLE_PASSWORD
#   ORACLE_WALLET_PEM + ORACLE_WALLET_PASSWORD   only for mutual TLS: the text of ewallet.pem from
#                    the downloaded wallet zip, and the password chosen when downloading it
# python-oracledb runs in "thin" mode, so no Oracle client needs installing.
if os.environ.get("ORACLE_DSN"):
    _oracle_opts = {}
    if os.environ.get("ORACLE_WALLET_PEM"):
        _wallet = Path(os.environ.get("ORACLE_WALLET_DIR", "/tmp/oracle_wallet"))
        _wallet.mkdir(parents=True, exist_ok=True)
        (_wallet / "ewallet.pem").write_text(os.environ["ORACLE_WALLET_PEM"].replace("\\n", "\n"))
        _oracle_opts = {"config_dir": str(_wallet), "wallet_location": str(_wallet),
                        "wallet_password": os.environ.get("ORACLE_WALLET_PASSWORD", "")}
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.oracle",
        "NAME": os.environ["ORACLE_DSN"],  # full DSN, so HOST/PORT stay empty
        "USER": os.environ.get("ORACLE_USER", ""),
        "PASSWORD": os.environ.get("ORACLE_PASSWORD", ""),
        "OPTIONS": _oracle_opts,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,  # Autonomous DB drops idle sessions
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
