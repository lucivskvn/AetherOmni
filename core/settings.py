import logging
import logging.config
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Configure specific temporary directories for libraries that need to write to the filesystem
# in serverless environments (like Cloud Run) where only /tmp is writable.
os.environ["HF_HOME"] = "/tmp/huggingface"  # nosec B108 # NOSONAR
os.environ["XDG_CACHE_HOME"] = "/tmp/xdg_cache"  # nosec B108 # NOSONAR
os.environ["MPLCONFIGDIR"] = "/tmp/matplotlib"  # nosec B108 # NOSONAR

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file
load_dotenv(os.path.join(BASE_DIR, ".env"))


TESTING = "test" in sys.argv
SURREALDB_OFFLINE = TESTING or os.getenv("SURREALDB_OFFLINE", "False").lower() in ("true", "1", "t")


# ── Core Security ──────────────────────────────────────────────────────────────

DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in ("true", "1", "t")

# ── Logging Configuration ─────────────────────────────────────────────────────
if TESTING:
    LOGGING_LEVEL = "ERROR"
elif DEBUG:
    LOGGING_LEVEL = "INFO"
else:
    LOGGING_LEVEL = "WARNING"

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
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR" if TESTING else LOGGING_LEVEL,
            "propagate": False,
        },
        "extractor": {
            "handlers": ["console"],
            "level": "ERROR" if TESTING else LOGGING_LEVEL,
            "propagate": False,
        },
        "init_surreal": {
            "handlers": ["console"],
            "level": "ERROR" if TESTING else LOGGING_LEVEL,
            "propagate": False,
        },
        "urllib3": {
            "handlers": ["console"],
            "level": "ERROR" if TESTING else "WARNING",
            "propagate": False,
        },
        "httpx": {
            "handlers": ["console"],
            "level": "ERROR" if TESTING else "WARNING",
            "propagate": False,
        },
        "google": {
            "handlers": ["console"],
            "level": "ERROR" if TESTING else "WARNING",
            "propagate": False,
        },
    },
}

logging.config.dictConfig(LOGGING)
logger = logging.getLogger(__name__)


_raw_secret = os.getenv("DJANGO_SECRET_KEY", "")
if not _raw_secret:
    if not DEBUG:
        # Fail-closed in production if secret key is absent
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY environment variable is not set. Production deployments require an explicit secret key."
        )
    import secrets

    _raw_secret = secrets.token_urlsafe(50)

SECRET_KEY = _raw_secret

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()

# ALLOWED_HOSTS configuration
django_allowed_hosts = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,.run.app")
ALLOWED_HOSTS = []
for host in django_allowed_hosts.split(","):
    host_clean = host.strip()
    if host_clean:
        if host_clean.startswith("*."):
            host_clean = host_clean[1:]  # *.run.app -> .run.app
        ALLOWED_HOSTS.append(host_clean)

custom_domain = os.getenv("CUSTOM_DOMAIN")
if custom_domain and custom_domain not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(custom_domain)

app_url = os.getenv("APP_URL")
if app_url:
    from urllib.parse import urlparse

    parsed_host = urlparse(app_url).netloc
    if parsed_host and parsed_host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(parsed_host)

extra_hosts_env = os.getenv("EXTRA_ALLOWED_HOSTS", ".cloudflareaccess.com,.fainko.my.id,.fainko.id")
for extra_host in [h.strip() for h in extra_hosts_env.split(",") if h.strip()]:
    if extra_host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(extra_host)

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
# All document/vector/KV data lives in SurrealDB. Django relational state uses
# Supabase PostgreSQL in production and SQLite only for explicit offline/test use.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL and not SURREALDB_OFFLINE:
    from core.database import database_config_from_url

    DATABASES = {"default": database_config_from_url(DATABASE_URL)}
else:
    if os.getenv("K_SERVICE") and not SURREALDB_OFFLINE:
        logger.info("[Database] DATABASE_URL not configured on Cloud Run; utilizing local SQLite storage.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
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

GS_BUCKET_NAME = os.getenv("GS_BUCKET_NAME", "").strip() or None

import sys

if "test" in sys.argv or "test_coverage" in sys.argv:
    GS_BUCKET_NAME = None

if GS_BUCKET_NAME:
    DEFAULT_STORAGE_BACKEND = "storages.backends.gcloud.GoogleCloudStorage"
else:
    DEFAULT_STORAGE_BACKEND = "django.core.files.storage.FileSystemStorage"
    # Warn operators that local storage is ephemeral on Cloud Run
    if not DEBUG:
        logger.warning(
            "[Storage] GS_BUCKET_NAME is not set in production. "
            "Files will be stored on the local filesystem and will be LOST on container restart. "
            "Configure a GCS bucket for persistent file storage."
        )

staticfiles_backend = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if ("test" in sys.argv or "test_coverage" in sys.argv or os.getenv("TESTING") == "true")
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)

STORAGES = {
    "default": {"BACKEND": DEFAULT_STORAGE_BACKEND},
    "staticfiles": {"BACKEND": staticfiles_backend},
}

# 1-year immutable caching for fingerprinted static assets (Brotli/Gzip compressed)
WHITENOISE_MAX_AGE = 31536000 if not DEBUG else 0
WHITENOISE_IMMUTABLE_FILE_TEST = "whitenoise.storage.CompressedManifestStaticFilesStorage.immutable_file_test"

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
        "LOCATION": "korda-locmem",
    }
}


# ── SurrealDB Configuration ────────────────────────────────────────────────────

SURREAL_URL = os.getenv("SURREAL_URL", "http://localhost:8001")
SURREAL_NS = os.getenv("SURREAL_NS", "korda")
SURREAL_DB = os.getenv("SURREAL_DB", "extractor")
SURREAL_USER = os.getenv("SURREAL_USER", "")
SURREAL_PASS = os.getenv("SURREAL_PASS", "")

if (
    not DEBUG
    and (not SURREAL_PASS or SURREAL_PASS == "root" or not SURREAL_USER or SURREAL_USER == "root")  # nosec B105
    and not SURREALDB_OFFLINE
    and "collectstatic" not in sys.argv
):
    raise ImproperlyConfigured(
        "[Security] SURREAL_USER and SURREAL_PASS must be explicitly configured in production. "
        "Default or empty credentials are strictly forbidden."
    )


# ── Google Cloud Tasks Configuration ──────────────────────────────────────────

# Priority: CLOUD_TASKS_QUEUE > GCP_QUEUE_NAME > canonical fallback
# IMPORTANT: must match the actual queue name created in GCP Cloud Tasks.
# The deployed service.yaml sets GCP_QUEUE_NAME="extractor-tasks".
CLOUD_TASKS_QUEUE = os.getenv("CLOUD_TASKS_QUEUE") or os.getenv("GCP_QUEUE_NAME") or "extractor-tasks"
# APP_URL is the fully-qualified URL of this Cloud Run service (used for task callbacks)
APP_URL = os.getenv("APP_URL", "http://localhost:8080")
# WORKER_URL is the fully-qualified URL of the korda-worker service
WORKER_URL = os.getenv("WORKER_URL", "")
CLOUD_TASKS_SERVICE_ACCOUNT = os.getenv("CLOUD_TASKS_SERVICE_ACCOUNT", "")


# ── Supabase Realtime ─────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_PUBLIC_KEY = os.getenv("SUPABASE_PUBLIC_KEY", "")

# Cloudflare Turnstile — site key is public (safe to render in templates)
CF_TURNSTILE_SITE_KEY = os.getenv("CF_TURNSTILE_SITE_KEY", "")

# Run periodic database maintenance only on the single-worker maintenance service.
ENABLE_PERIODIC_MAINTENANCE = os.getenv("ENABLE_PERIODIC_MAINTENANCE", "False").lower() in ("true", "1", "yes")


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

# 2. Auto-append APP_URL so initial login is never blocked by CSRF
_app_url = APP_URL.rstrip("/")
if _app_url and _app_url not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_app_url)

# 3. Derive trusted origins automatically from ALLOWED_HOSTS (Enforce HTTPS)
for host in ALLOWED_HOSTS:
    host_clean = host.strip()
    if host_clean and host_clean != "*":
        if host_clean.startswith("*.") or not host_clean.startswith("."):
            CSRF_TRUSTED_ORIGINS.append(f"https://{host_clean}")
        else:
            CSRF_TRUSTED_ORIGINS.append(f"https://*{host_clean}")

# 4. Trust localhost and loopback — always needed for gcloud Run proxy / health checks
local_origins = [
    "http://localhost:8080",  # NOSONAR python:S5332 -- Local development origin allowlist
    "http://127.0.0.1:8080",  # NOSONAR python:S5332 -- Local development origin allowlist
    "http://localhost",  # NOSONAR python:S5332 -- Local development origin allowlist
    "http://127.0.0.1",  # NOSONAR python:S5332 -- Local development origin allowlist
    "https://localhost:8080",
    "https://127.0.0.1:8080",
    "https://localhost",
    "https://127.0.0.1",
]
for origin in local_origins:
    if origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(origin)

# 5. In DEBUG mode only: trust common local tunnel services (ngrok, Cloudflare Tunnel, etc.)
#    These are NOT added in production to limit the CSRF attack surface.
if DEBUG:
    tunnel_origins = [
        "https://*.ngrok-free.app",
        "http://*.ngrok-free.app",  # NOSONAR python:S5332 -- Local tunnel origin allowlist in DEBUG mode
        "https://*.trycloudflare.com",
        "http://*.trycloudflare.com",  # NOSONAR python:S5332 -- Local tunnel origin allowlist in DEBUG mode
        "https://*.localtunnel.me",
        "http://*.localtunnel.me",  # NOSONAR python:S5332 -- Local tunnel origin allowlist in DEBUG mode
        "https://*.gitpod.io",
        "https://*.github.dev",
    ]
    for origin in tunnel_origins:
        if origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(origin)

# Enable Proxy SSL & Forwarded Host headers for Cloudflare / Cloud Run / Reverse Proxies
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# In production, enforce SSL and secure cookies (fully configurable via environment variables)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

if not DEBUG:
    SECURE_SSL_REDIRECT = False if TESTING else os.getenv("SECURE_SSL_REDIRECT", "True").lower() == "true"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "True").lower() == "true"
    CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "True").lower() == "true"
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000")) if not TESTING else 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "True").lower() == "true"
    SECURE_HSTS_PRELOAD = os.getenv("SECURE_HSTS_PRELOAD", "True").lower() == "true"

    if not CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append("https://*.run.app")

    # Cloudflare Access & Extra trusted origins dynamically sourced
    cf_access_origins_env = os.getenv("EXTRA_CSRF_ORIGINS", "https://*.cloudflareaccess.com")
    for o in [x.strip() for x in cf_access_origins_env.split(",") if x.strip()]:
        if o not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(o)

    if custom_domain:
        origin_https = f"https://{custom_domain}"
        if origin_https not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(origin_https)

    if app_url and app_url.startswith("http"):
        from urllib.parse import urlparse

        u = urlparse(app_url)
        origin_app = f"{u.scheme}://{u.netloc}"
        if origin_app not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(origin_app)

    logger.info("[Security] Production OWASP enforcement active.")
else:
    logger.info("[Security] Development security active.")


# ── Operational Settings ───────────────────────────────────────────────────────

DATA_RETENTION_DAYS = max(1, int(os.getenv("DATA_RETENTION_DAYS", "30")))
MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "10.00"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not DEBUG and not GEMINI_API_KEY and not os.getenv("GOOGLE_CLOUD_PROJECT") and not os.getenv("GCP_PROJECT"):
    logger.warning(
        "[LLM] Neither GEMINI_API_KEY nor GCP_PROJECT is set. "
        "LLM calls will fail unless Vertex AI ADC credentials are available."
    )

# Google Cloud Platform (GCP) and Vertex AI Settings
GCP_PROJECT = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
# Canonical default: asia-southeast1 (Singapore) — matches cloudbuild.yaml, Pulumi, and deployment.py
GCP_REGION = os.getenv("GCP_REGION") or os.getenv("GOOGLE_CLOUD_LOCATION") or "asia-southeast1"
VERTEX_API_KEY = os.getenv("VERTEX_API_KEY")


# ── Authentication Gateway ────────────────────────────────────────────────────

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"


# ── Primary Key Field ─────────────────────────────────────────────────────────

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Observability & Monitoring (Sentry) ───────────────────────────────────────

SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk
        from django.core.exceptions import ImproperlyConfigured
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        release_ver = os.getenv("RELEASE_VERSION", "").strip()
        if not release_ver:
            if not DEBUG:
                raise ImproperlyConfigured(
                    "RELEASE_VERSION must be set when SENTRY_DSN is configured in production environments."
                )
            release_ver = "0.0.0"

        send_pii = os.getenv("SENTRY_SEND_DEFAULT_PII", "false").lower() in ("true", "1", "t")
        traces_rate = min(1.0, max(0.0, float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0" if DEBUG else "0.1"))))
        profile_session_rate = min(1.0, max(0.0, float(os.getenv("SENTRY_PROFILE_SESSION_SAMPLE_RATE", "0.0"))))

        logging_integration = LoggingIntegration(
            level=logging.INFO,
            event_level=logging.ERROR,
        )

        sentry_kwargs = {
            "dsn": SENTRY_DSN,
            "integrations": [DjangoIntegration(), logging_integration],
            "traces_sample_rate": traces_rate,
            "send_default_pii": send_pii,
            "release": f"korda@{release_ver}",
            "environment": "production" if not DEBUG else "development",
        }
        if profile_session_rate > 0.0:
            sentry_kwargs["profile_session_sample_rate"] = profile_session_rate
            sentry_kwargs["profile_lifecycle"] = os.getenv("SENTRY_PROFILE_LIFECYCLE", "trace")

        sentry_sdk.init(**sentry_kwargs)  # type: ignore[arg-type]
        logger.info("[Observability] Sentry release tracking active (release=%s).", release_ver)
    except ImportError:
        logger.debug("[Observability] sentry-sdk not installed; skipping Sentry initialization.")
    except ImproperlyConfigured:
        raise
    except Exception as exc:
        logger.warning("[Observability] Failed to initialize Sentry: %s", exc)
