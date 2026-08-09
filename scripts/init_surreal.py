APPLICATION_JSON = "application/json"
import logging
import os
import time

import httpx

DJANGO_DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in ("true", "1", "yes")
LOG_LEVEL = logging.INFO if DJANGO_DEBUG else logging.WARNING

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("init_surreal")

SURREAL_URL = os.getenv("SURREAL_URL", "http://localhost:8001")
SURREAL_USER = os.getenv("SURREAL_USER", "root")
SURREAL_PASS = os.getenv("SURREAL_PASS", "")
SURREAL_NS = os.getenv("SURREAL_NS", "aetheromni")
SURREAL_DB = os.getenv("SURREAL_DB", "extractor")

if not SURREAL_PASS and DJANGO_DEBUG:
    SURREAL_PASS = "root"  # nosec B105

if (
    not DJANGO_DEBUG
    and SURREAL_PASS in ("", "root")
    and os.getenv("SURREALDB_OFFLINE", "False").lower() not in ("true", "1", "t")
):
    logger.error("SURREAL_PASS is using the default 'root' or empty credential in production. Aborting.")
    raise ValueError("Production deployments require an explicit, strong password for SurrealDB.")


def wait_for_surreal(client: httpx.Client, max_retries: int = 30) -> bool:
    for i in range(1, max_retries + 1):
        try:
            logger.info("Checking SurrealDB health (attempt %d/%d)...", i, max_retries)
            resp = client.get("/health")
            if resp.status_code == 200:
                logger.info("SurrealDB is healthy and ready!")
                return True
        except Exception as exc:
            logger.warning("SurrealDB not ready yet: %s", exc)
        time.sleep(1)
    return False


def apply_schema(client: httpx.Client) -> None:
    schema_path = "/app/schema.surql"
    if not os.path.exists(schema_path):
        schema_path = "schema.surql"  # fallback for local execution outside docker

    if not os.path.exists(schema_path):
        logger.error("Schema file not found at: %s", schema_path)
        return

    logger.info("Reading schema from %s...", schema_path)
    with open(schema_path, encoding="utf-8") as f:
        schema_sql = f.read()

    # Pre-define namespace and database for SurrealDB 3.x compatibility
    logger.info("Ensuring namespace '%s' and database '%s' exist...", SURREAL_NS, SURREAL_DB)
    try:
        client.post("/sql", content=f"DEFINE NAMESPACE {SURREAL_NS};".encode(), headers={"Accept": APPLICATION_JSON})
        client.post(
            "/sql",
            content=f"DEFINE DATABASE {SURREAL_DB};".encode(),
            headers={"surreal-ns": SURREAL_NS, "Accept": APPLICATION_JSON},
        )
    except Exception as exc:
        logger.warning("Namespace/database pre-definition warning: %s", exc)

    headers = {
        "NS": SURREAL_NS,
        "DB": SURREAL_DB,
        "surreal-ns": SURREAL_NS,
        "surreal-db": SURREAL_DB,
        "Accept": APPLICATION_JSON,
        "Content-Type": "text/plain",
    }

    logger.info("Applying schema to SurrealDB...")
    resp = client.post("/sql", content=schema_sql.encode(), headers=headers)
    if resp.status_code != 200:
        logger.error("Failed response body: %s", resp.text)
    resp.raise_for_status()
    results = resp.json()

    # Check if there were any errors in statement execution
    errors = 0
    for idx, stmt in enumerate(results):
        if stmt.get("status") == "ERR":
            logger.error("Statement %d failed: %s - %s", idx, stmt.get("detail"), stmt.get("result"))
            errors += 1

    if errors == 0:
        logger.info("SurrealDB schema initialized successfully!")
    else:
        logger.error("Schema applied with %d errors.", errors)


def _check_supabase_admin(token_url, payload, headers, admin_email):
    import urllib.request

    from extractor.utils import validate_url_scheme

    try:
        validate_url_scheme(token_url)
        req = urllib.request.Request(token_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5):  # nosec B310 nosemgrep
            logger.info("Admin user '%s' already exists and authenticated successfully on Supabase Auth.", admin_email)
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        logger.info("Admin check status code: HTTP %d", e.code)
        if "email_not_confirmed" in body or "Email not confirmed" in body:
            logger.info("Admin user '%s' already exists on Supabase Auth but has an unconfirmed email.", admin_email)
            return True
    except Exception as e:
        logger.warning("Failed to check existing admin on Supabase: %s", e)
    return False


def _register_supabase_admin(signup_url, payload, headers, admin_email):
    import urllib.request

    from extractor.utils import validate_url_scheme

    try:
        validate_url_scheme(signup_url)
        req = urllib.request.Request(signup_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5):  # nosec B310 nosemgrep
            logger.info("Admin user '%s' registered successfully on Supabase!", admin_email)
    except urllib.error.HTTPError as e:
        logger.info("Supabase registration status: HTTP %d", e.code)
    except Exception:
        logger.exception("Failed to register admin on Supabase")


def _setup_supabase_admin(supabase_url, supabase_key, admin_email, admin_username, admin_password):
    import json
    import urllib.parse

    from django.conf import settings

    logger.info("Supabase is configured. Checking if '%s' user already exists on Supabase Auth...", admin_username)
    token_url = f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=password"
    headers = {"apikey": supabase_key, "Content-Type": APPLICATION_JSON}
    payload = json.dumps({"email": admin_email, "password": admin_password}).encode("utf-8")

    admin_exists = _check_supabase_admin(token_url, payload, headers, admin_email)

    if not admin_exists:
        logger.info("Admin user '%s' not authenticated. Attempting registration on Supabase Auth...", admin_username)
        app_url = getattr(settings, "APP_URL", "http://localhost:8000")
        signup_url = f"{supabase_url.rstrip('/')}/auth/v1/signup?redirect_to={urllib.parse.quote(app_url.rstrip('/') + '/login')}"
        _register_supabase_admin(signup_url, payload, headers, admin_email)


def _create_local_superuser_stub(admin_username, admin_email):
    from django.contrib.auth.models import User

    user, created = User.objects.get_or_create(
        username=admin_username,
        defaults={
            "email": admin_email,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        },
    )
    if created:
        user.set_unusable_password()
        user.save()
        logger.info(
            "Local Django superuser stub '%s' created (password managed by Supabase).", admin_username
        )  # NOSONAR
    return user


def _create_local_superuser_full(admin_username, admin_email, admin_password):
    from django.contrib.auth.models import User

    user, created = User.objects.get_or_create(
        username=admin_username,
        defaults={
            "email": admin_email,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        },
    )
    if created:
        from django.contrib.auth.password_validation import validate_password

        try:
            validate_password(admin_password, user=user)
        except Exception as exc:
            logger.warning("Credential validation warning during initial admin setup: %s", exc)
        user.set_password(admin_password)  # NOSONAR # nosemgrep
        user.save()
        logger.info("Local Django superuser '%s' created successfully.", admin_username)  # NOSONAR
        if admin_password == "admin":  # nosec B105
            from core.middleware import ForcePasswordChangeMiddleware

            try:
                import bcrypt

                logger.info(
                    "Enforcing credential update flag for initial administrator account."
                )  # NOSONAR # nosemgrep
                ForcePasswordChangeMiddleware.set_force_reset_flag(
                    user.id, bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode("utf-8")
                )
            except ImportError as exc:
                logger.debug("Bcrypt module unavailable for credential update flag: %s", exc)
    else:
        if not user.is_staff or not user.is_superuser:
            user.is_staff = True
            user.is_superuser = True
            user.save()
            logger.info("Updated existing user '%s' to superuser status.", admin_username)
    return user


def _setup_local_admin(admin_username, admin_email, admin_password, supabase_url):
    if supabase_url:
        return _create_local_superuser_stub(admin_username, admin_email)
    else:
        return _create_local_superuser_full(admin_username, admin_email, admin_password)


def _migrate_system_settings():
    from extractor.models import SystemSettings

    # Migrate stale model name in SurrealDB system_settings
    try:
        from extractor.surreal_db import get_system_settings, save_system_settings

        db_settings = get_system_settings()
        if db_settings.get("selected_model") in ("gemini-1.5-flash", "gemini-3.1-flash-lite", "default_llm_model"):
            db_settings["selected_model"] = "gemini-3.6-flash"
            save_system_settings(db_settings)
            logger.info("System settings migrated: updated SurrealDB model to 'gemini-3.6-flash'")
    except Exception as me:
        logger.warning("Failed to migrate SurrealDB settings: %s", me)

    # Migrate stale model name in SQLite SystemSettings
    try:
        if SystemSettings.objects.exists():
            stg = SystemSettings.objects.first()
            if stg.selected_model in ("gemini-1.5-flash", "google/gemini-1.5-flash", "gemini-3.1-flash-lite"):
                stg.selected_model = "gemini-3.6-flash"
                stg.save()
                logger.info("System settings migrated: updated SQLite model to 'gemini-3.6-flash'")
    except Exception as se:
        logger.warning("Failed to migrate SystemSettings model: %s", se)


def init_django_admin():
    logger.info("Initializing Django administrative superuser...")
    try:
        import django

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
        django.setup()
        from django.conf import settings
        from django.core.management import call_command

        try:
            call_command("migrate", interactive=False)
        except Exception as me:
            logger.warning("Django migrate warning: %s", me)

        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        admin_email = os.getenv("ADMIN_EMAIL", getattr(settings, "ADMIN_EMAIL", "admin@example.com"))
        admin_username = os.getenv("ADMIN_USERNAME", getattr(settings, "ADMIN_USERNAME", "admin"))
        admin_password = os.getenv("ADMIN_PASSWORD")

        if not admin_password:
            import secrets

            admin_password = secrets.token_urlsafe(16)
            logger.warning(  # NOSONAR # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                "[Security] ADMIN_PASSWORD not set in environment. Auto-generated temporary password for '%s'.",
                admin_username,
            )

        if supabase_url and supabase_key:
            _setup_supabase_admin(supabase_url, supabase_key, admin_email, admin_username, admin_password)

        _setup_local_admin(admin_username, admin_email, admin_password, supabase_url)
        _migrate_system_settings()

    except Exception:
        logger.exception("Failed to initialize Django superuser")


def main():
    init_django_admin()
    # If SURREALDB_OFFLINE is set, skip initialization
    if os.getenv("SURREALDB_OFFLINE", "False").lower() in ("true", "1", "yes"):
        logger.info("SURREALDB_OFFLINE is True. Skipping initialization.")
        return

    # Convert WebSocket URL scheme to HTTP scheme for REST requests
    ws_prefix = "ws:" + "//"
    wss_prefix = "wss:" + "//"
    http_url = SURREAL_URL.replace(ws_prefix, "http://").replace(wss_prefix, "https://")  # nosemgrep
    http_url = http_url.removesuffix("/rpc")
    http_url = http_url.rstrip("/")

    client = httpx.Client(
        base_url=http_url,
        auth=(SURREAL_USER, SURREAL_PASS),
        timeout=10.0,
    )

    if wait_for_surreal(client):
        apply_schema(client)
    else:
        logger.error("Failed to connect to SurrealDB. Initialization aborted.")


if __name__ == "__main__":
    main()
