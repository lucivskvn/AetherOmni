import logging
import logging.config
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Ensure all libraries look up /tmp as home (Cloud Run write sandbox)
os.environ["HOME"] = "/tmp"  # NOSONAR

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file
load_dotenv(os.path.join(BASE_DIR, ".env"))


TESTING = "test" in sys.argv
SURREALDB_OFFLINE = TESTING


# ── Core Security ──────────────────────────────────────────────────────────────

DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() in ("true", "1", "t")

# ── Logging Configuration ─────────────────────────────────────────────────────
LOGGING_LEVEL = "INFO" if DEBUG else "WARNING"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "level": LOGGING_LEVEL,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOGGING_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOGGING_LEVEL,
            "propagate": False,
        },
        "urllib3": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "httpx": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "google": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

logging.config.dictConfig(LOGGING)
logger = logging.getLogger(__name__)


_raw_secret = os.getenv("DJANGO_SECRET_KEY", "")
if not _raw_secret:
    if not DEBUG:
        # Gap F-11: fail-closed in production if secret key is absent
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY environment variable is not set. Production deployments require an explicit secret key."
        )
    _raw_secret = "django-insecure-local-dev-key-do-not-use-in-prod"  # nosec B106

SECRET_KEY = _raw_secret

# ALLOWED_HOSTS configuration
django_allowed_hosts = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,*.run.app")
ALLOWED_HOSTS = [host.strip() for host in django_allowed_hosts.split(",") if host.strip()]
if DEBUG and "*" not in ALLOWED_HOSTS:
    # Ensure local development tunnels/proxies (e.g. ngrok, cloudflare) work seamlessly
    ALLOWED_HOSTS.append("*")

# ── Application Definition ─────────────────────────────────────────────────────

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party Apps
    "storages",
    # Custom Project Apps
    "extractor",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # GZIP Compression Middleware (Compresses outputs by up to 80% dynamically)
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "core.middleware.DynamicCsrfTrustedOriginsMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.ForcePasswordChangeMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "extractor.context_processors.system_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"


# ── Database Configuration ─────────────────────────────────────────────────────
# All document/vector/KV data lives in SurrealDB.
# Django users, sessions, and settings live in SQLite locally, or PostgreSQL in production.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

if not DEBUG and os.getenv("DATABASE_URL") and "test" not in sys.argv:
    from urllib.parse import urlparse

    parsed = urlparse(os.getenv("DATABASE_URL"))
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username,
            "PASSWORD": parsed.password,
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
            "OPTIONS": {
                "sslmode": "require",
                "connect_timeout": 10,
            },
        }
    }

AUTHENTICATION_BACKENDS = [
    "extractor.auth.SupabaseAuthBackend",
]

# ── Password Validation ────────────────────────────────────────────────────────

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── Internationalisation ───────────────────────────────────────────────────────

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ── Static Files ───────────────────────────────────────────────────────────────

STATIC_URL = "static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static_root")
STATICFILES_DIRS = [os.path.join(BASE_DIR, "static")]
os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)


# ── File Storage (GCS) ────────────────────────────────────────────────────────

GS_BUCKET_NAME = os.getenv("GS_BUCKET_NAME")

if GS_BUCKET_NAME:
    DEFAULT_STORAGE_BACKEND = "storages.backends.gcloud.GoogleCloudStorage"
else:
    DEFAULT_STORAGE_BACKEND = "django.core.files.storage.FileSystemStorage"
    # Gap I-7: warn operators that local storage is ephemeral on Cloud Run
    if not DEBUG:
        logger.warning(
            "[Storage] GS_BUCKET_NAME is not set in production. "
            "Files will be stored on the local filesystem and will be LOST on container restart. "
            "Configure a GCS bucket for persistent file storage."
        )

STORAGES = {
    "default": {"BACKEND": DEFAULT_STORAGE_BACKEND},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

if GS_BUCKET_NAME:
    GS_DEFAULT_ACL = None  # Use Bucket Uniform Level Access control (private)
    GS_QUERYSTRING_AUTH = True
    GS_EXPIRATION = 900  # 15-minute signed URLs
    MEDIA_URL = "/media/"
    MEDIA_ROOT = os.path.join(BASE_DIR, "media")
    logger.info(f"[Storage] Using Google Cloud Storage private bucket: {GS_BUCKET_NAME}")
else:
    MEDIA_URL = "/media/"
    MEDIA_ROOT = os.path.join(BASE_DIR, "media")
    logger.info("[Storage] Cloud Storage not configured. Falling back to local file storage.")


# ── Caching (LocMemCache — KV data lives in SurrealDB) ────────────────────────

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "omnirag-locmem",
    }
}


# ── SurrealDB Configuration ────────────────────────────────────────────────────

SURREAL_URL = os.getenv("SURREAL_URL", "http://localhost:8001")
SURREAL_NS = os.getenv("SURREAL_NS", "omnirag")
SURREAL_DB = os.getenv("SURREAL_DB", "extractor")
SURREAL_USER = os.getenv("SURREAL_USER", "root")
SURREAL_PASS = os.getenv("SURREAL_PASS", "root")


# ── Google Cloud Tasks Configuration ──────────────────────────────────────────

CLOUD_TASKS_QUEUE = os.getenv("CLOUD_TASKS_QUEUE") or os.getenv("GCP_QUEUE_NAME") or "omnirag-tasks"
# APP_URL is the fully-qualified URL of this Cloud Run service (used for task callbacks)
APP_URL = os.getenv("APP_URL", "http://localhost:8080")
# WORKER_URL is the fully-qualified URL of the data-extractor-worker service
WORKER_URL = os.getenv("WORKER_URL", "")


# ── Supabase Realtime ─────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_PUBLIC_KEY = os.getenv("SUPABASE_PUBLIC_KEY", "")


# ── Cloud Run / Upload RAM Safeguards ─────────────────────────────────────────

FILE_UPLOAD_MAX_MEMORY_SIZE = 10485760  # 10 MB — larger files stream to /tmp
DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760


# ── OWASP Security Headers ────────────────────────────────────────────────────

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"

# Trust the X-Forwarded-Proto header for SSL detection behind Cloud Run / proxies
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# ── CSRF Trusted Origins ───────────────────────────────────────────────────────
# Static origins come from env var + ALLOWED_HOSTS derivation.
# Dynamic per-tenant origins are loaded once at startup by DynamicCsrfTrustedOriginsMiddleware.

CSRF_TRUSTED_ORIGINS = []

# 1. Add custom origins defined in environment variable
csrf_origins = os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "")
if csrf_origins:
    CSRF_TRUSTED_ORIGINS.extend([o.strip() for o in csrf_origins.split(",") if o.strip()])

# 2. Gap F-10: auto-append APP_URL so initial login is never blocked by CSRF
_app_url = APP_URL.rstrip("/")
if _app_url and _app_url not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_app_url)

# 3. Derive trusted origins automatically from ALLOWED_HOSTS
for host in ALLOWED_HOSTS:
    host_clean = host.strip()
    if host_clean and host_clean != "*":
        if host_clean.startswith("*.") or not host_clean.startswith("."):
            CSRF_TRUSTED_ORIGINS.append(f"https://{host_clean}")
            CSRF_TRUSTED_ORIGINS.append(f"http://{host_clean}")
        else:
            CSRF_TRUSTED_ORIGINS.append(f"https://*{host_clean}")
            CSRF_TRUSTED_ORIGINS.append(f"http://*{host_clean}")

# 4. In DEBUG mode, trust common local tunnel services
# Unconditionally trust localhost, loopback, and common local tunnels to support gcloud Run proxy
local_and_tunnel_origins = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost",
    "http://127.0.0.1",
    "https://localhost:8080",
    "https://127.0.0.1:8080",
    "https://localhost",
    "https://127.0.0.1",
    "https://*.ngrok-free.app",
    "http://*.ngrok-free.app",
    "https://*.trycloudflare.com",
    "http://*.trycloudflare.com",
    "https://*.localtunnel.me",
    "http://*.localtunnel.me",
    "https://*.gitpod.io",
    "https://*.github.dev",
]
for origin in local_and_tunnel_origins:
    if origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(origin)

# In production, enforce SSL and secure cookies (fully configurable via environment variables)
if not DEBUG:
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "True").lower() == "true"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "True").lower() == "true"
    CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "True").lower() == "true"

    if not CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append("https://*.run.app")

    logger.info(f"[Security] Production OWASP enforcement active. CSRF Trusted Origins: {CSRF_TRUSTED_ORIGINS}")
else:
    logger.info(f"[Security] Development security active. CSRF Trusted Origins: {CSRF_TRUSTED_ORIGINS}")


# ── Operational Settings ───────────────────────────────────────────────────────

DATA_RETENTION_DAYS = int(os.getenv("DATA_RETENTION_DAYS", "30"))
MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "10.00"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# Google Cloud Platform (GCP) and Vertex AI Settings
GCP_PROJECT = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
GCP_REGION = os.getenv("GCP_REGION") or os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1"
VERTEX_API_KEY = os.getenv("VERTEX_API_KEY")


# ── Authentication Gateway ────────────────────────────────────────────────────

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"


# ── Primary Key Field ─────────────────────────────────────────────────────────

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
