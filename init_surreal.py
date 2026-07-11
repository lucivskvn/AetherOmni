import logging
import os
import time

import httpx

DJANGO_DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in ("true", "1", "yes")
LOG_LEVEL = logging.INFO if DJANGO_DEBUG else logging.WARNING

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("init_surreal")

SURREAL_URL = os.getenv("SURREAL_URL", "http://surrealdb:8000")
SURREAL_USER = os.getenv("SURREAL_USER", "root")
SURREAL_PASS = os.getenv("SURREAL_PASS", "root")
SURREAL_NS = os.getenv("SURREAL_NS", "omnirag")
SURREAL_DB = os.getenv("SURREAL_DB", "extractor")


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
        client.post("/sql", content=f"DEFINE NAMESPACE {SURREAL_NS};".encode(), headers={"Accept": "application/json"})
        client.post(
            "/sql",
            content=f"DEFINE DATABASE {SURREAL_DB};".encode(),
            headers={"surreal-ns": SURREAL_NS, "Accept": "application/json"},
        )
    except Exception as exc:
        logger.warning("Namespace/database pre-definition warning: %s", exc)

    headers = {
        "NS": SURREAL_NS,
        "DB": SURREAL_DB,
        "surreal-ns": SURREAL_NS,
        "surreal-db": SURREAL_DB,
        "Accept": "application/json",
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
            logger.error("Statement %d failed: %s", idx, stmt.get("detail"))
            errors += 1

    if errors == 0:
        logger.info("SurrealDB schema initialized successfully!")
    else:
        logger.error("Schema applied with %d errors.", errors)


def init_django_admin():
    logger.info("Initializing Django administrative superuser...")
    try:
        import django

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
        django.setup()
        from django.conf import settings
        from django.contrib.auth.models import User

        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        # If Supabase is configured, register admin on Supabase directly
        if supabase_url and supabase_key:
            admin_email = "elang@fainko.co.id"
            admin_username = "elang"

            logger.info("Supabase is configured. Checking if 'elang' user already exists on Supabase Auth...")
            import json
            import urllib.request

            from extractor.utils import validate_url_scheme

            token_url = f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=password"
            headers = {"apikey": supabase_key, "Content-Type": "application/json"}
            payload = json.dumps({"email": admin_email, "password": "AdminPass123!"}).encode("utf-8")

            admin_exists = False
            try:
                validate_url_scheme(token_url)
                req = urllib.request.Request(token_url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5):
                    logger.info(
                        "Admin user '%s' already exists and authenticated successfully on Supabase Auth.", admin_email
                    )
                    admin_exists = True
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8")
                logger.info("Admin exists check response on token endpoint: HTTP %d - %s", e.code, body)
                if "email_not_confirmed" in body or "Email not confirmed" in body:
                    logger.info(
                        "Admin user '%s' already exists on Supabase Auth but has an unconfirmed email.", admin_email
                    )
                    admin_exists = True
            except Exception as e:
                logger.warning("Failed to check existing admin on Supabase: %s", e)

            if not admin_exists:
                logger.info(
                    "Admin user '%s' not authenticated. Attempting registration on Supabase Auth...", admin_username
                )
                app_url = getattr(settings, "APP_URL", "http://localhost:8000")
                import urllib.parse
                signup_url = f"{supabase_url.rstrip('/')}/auth/v1/signup?redirect_to={urllib.parse.quote(app_url.rstrip('/') + '/login')}"
                try:
                    validate_url_scheme(signup_url)
                    req = urllib.request.Request(signup_url, data=payload, headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=5):
                        logger.info("Admin user '%s' registered successfully on Supabase!", admin_email)
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8")
                    logger.info("Supabase registration status / response: HTTP %d - %s", e.code, body)
                except Exception as e:
                    logger.error("Failed to register admin on Supabase: %s", e)

            # Manual confirmation requested: bypass automatic PostgreSQL DB email confirmation updates
            # from django.db import connection
            # if connection.vendor == "postgresql":
            #     try:
            #         with connection.cursor() as cursor:
            #             cursor.execute(
            #                 "UPDATE auth.users SET email_confirmed_at = NOW() WHERE email = %s",
            #                 [admin_email],
            #             )
            #         logger.info("Admin email '%s' confirmed successfully in Supabase DB.", admin_email)
            #     except Exception as e:
            #         logger.warning("Could not confirm admin email in Supabase DB: %s", e)

            # Ensure local Django superuser stub exists with unusable password
            user, created = User.objects.get_or_create(
                username=admin_username,
                defaults={
                    "email": admin_email,
                    "is_active": True,
                    "is_superuser": True,
                    "is_staff": True,
                },
            )
            if created:
                user.set_unusable_password()
                user.save()
                logger.info("Local Django admin stub '%s' created with unusable password.", admin_username)
            else:
                user.set_unusable_password()
                user.is_superuser = True
                user.is_staff = True
                user.email = admin_email
                user.save()
                logger.info("Local Django admin stub '%s' updated to ensure unusable password.", admin_username)

            # Remove any existing local_admin user to enforce Supabase as the sole auth method
            deleted_count, _ = User.objects.filter(username="local_admin").delete()
            if deleted_count > 0:
                logger.info("Removed existing local fallback admin user 'local_admin'.")
        else:
            # Fallback to local SQLite db admin creation
            if not User.objects.filter(username="elang").exists():
                User.objects.create_superuser("elang", "elang@fainko.co.id", "AdminPass123!")
                logger.info("Default local Django superuser 'elang' created successfully!")
            else:
                logger.info("Local Django superuser 'elang' already exists.")

        # Clean any stray Q&A descriptions and headers from existing SourceDocuments
        import re

        from extractor.models import SourceDocument
        pattern = r'\n{1,4}(?:#{1,6}\s+|(?:\*{1,2}))(?:Curated\s+)?(?:SFT\s+)?(?:Q[&\s]*A|Question|Dataset|Training|Curated)[^\n]*(?:\*{1,2})?\s*\n(?:[^\n]*(?:Reasoning|downstream|training|NotebookLM)[^\n]*\n?)*.*$'
        cleaned_count = 0
        for doc in SourceDocument.objects.all():
            if doc.refined_markdown:
                cleaned = re.sub(pattern, '', doc.refined_markdown, flags=re.DOTALL | re.IGNORECASE).rstrip()
                if cleaned != doc.refined_markdown.rstrip():
                    doc.refined_markdown = cleaned
                    doc.save()
                    cleaned_count += 1
        if cleaned_count > 0:
            logger.info("Removed stray SFT Q&A headers/descriptions from %d existing documents.", cleaned_count)

        # Automatic SystemSettings model migration to upgrade legacy selected models
        try:
            from extractor.models import SystemSettings
            settings_obj = SystemSettings.get_settings()
            if settings_obj.selected_model in ["google/gemini-2.5-flash-lite", "google/gemini-3.1-flash-lite"]:
                settings_obj.selected_model = "google/gemini-3.1-flash-lite"
                settings_obj.save()
                logger.info("System settings migrated: updated legacy model to 'google/gemini-3.1-flash-lite'")
            elif settings_obj.selected_model in ["google/gemini-2.5-flash", "google/gemini-3.1-flash"]:
                settings_obj.selected_model = "google/gemini-3.5-flash"
                settings_obj.save()
                logger.info("System settings migrated: updated legacy model to 'google/gemini-3.5-flash'")
        except Exception as se:
            logger.warning("Failed to migrate SystemSettings model: %s", se)

    except Exception as exc:
        logger.error("Failed to initialize Django superuser: %s", exc)


def main():
    init_django_admin()
    # If SURREALDB_OFFLINE is set, skip initialization
    if os.getenv("SURREALDB_OFFLINE", "False").lower() in ("true", "1", "yes"):
        logger.info("SURREALDB_OFFLINE is True. Skipping initialization.")
        return

    client = httpx.Client(
        base_url=SURREAL_URL,
        auth=(SURREAL_USER, SURREAL_PASS),
        timeout=15.0,
    )

    if wait_for_surreal(client):
        apply_schema(client)
    else:
        logger.error("Failed to connect to SurrealDB. Initialization aborted.")


if __name__ == "__main__":
    main()
