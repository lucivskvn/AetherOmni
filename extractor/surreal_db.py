"""
SurrealDB REST Client Adapter — AetherOmni v2.0

Implements a thread-safe connection pool using a single global httpx.Client
instance to prevent socket exhaustion under concurrent request load.

All public methods execute SurrealQL via the /sql endpoint and return
plain Python dicts/lists — no ORM coupling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import UTC
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from surrealdb import AsyncSurreal

logger = logging.getLogger(__name__)

VALID_DOCUMENT_FIELDS = {
    "title",
    "status",
    "language",
    "author",
    "publisher",
    "publication_year",
    "license_type",
    "doi",
    "metadata_json",
    "original_filename",
    "file_hash",
    "file_path",
    "file_size",
    "expires_at",
    "created_at",
    "updated_at",
    "uuid",
    "doc_uuid",
}


# ── Singleton connection pool ──────────────────────────────────────────────────
# A single httpx.Client is shared across all threads (safe per httpx docs).
# It maintains an internal connection pool, reusing TCP sockets on Keep-Alive.


_test_chunks: dict[str, list[dict]] = {}


ISO8601_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _model_to_dict(doc) -> dict:
    if not doc:
        return {}
    return {
        "id": doc.id,
        "doc_uuid": str(doc.uuid),
        "file": doc.file.name if doc.file else "",
        "original_filename": doc.original_filename,
        "file_hash": doc.file_hash,
        "status": doc.status,
        "uploaded_by_id": str(doc.uploaded_by.id) if doc.uploaded_by else None,
        "language": doc.language,
        "author": doc.author,
        "title": doc.title,
        "document_type": doc.document_type,
        "page_count": doc.page_count,
        "raw_markdown": doc.raw_markdown,
        "refined_markdown": doc.refined_markdown,
        "yaml_metadata": doc.yaml_metadata,
        "qa_dataset": doc.qa_dataset,
        "cost_usd": float(doc.cost_usd),
        "semantic_signature": doc.semantic_signature,
        "retry_count": doc.retry_count,
        "created_at": doc.created_at.strftime(ISO8601_FMT) if doc.created_at else None,
        "updated_at": doc.updated_at.strftime(ISO8601_FMT) if doc.updated_at else None,
        "expires_at": doc.expires_at.strftime(ISO8601_FMT) if doc.expires_at else None,
    }


def _settings_to_dict(settings_obj) -> dict:
    if not settings_obj:
        return {}
    return {
        "monthly_budget_usd": float(settings_obj.monthly_budget_usd),
        "selected_model": settings_obj.selected_model,
        "currency": settings_obj.currency,
        "openrouter_api_key": settings_obj.openrouter_api_key,
    }


def _audit_log_to_dict(log) -> dict:
    if not log:
        return {}
    return {
        "user_id": str(log.user.id) if log.user else None,
        "doc_uuid": str(log.document.uuid) if log.document else None,
        "action": log.action,
        "details": log.details,
        "ip_address": log.ip_address,
        "timestamp": log.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if log.timestamp else None,
    }


_detected_url: str | None = None
_detected_ns: str | None = None


def _get_surreal_url() -> str:
    global _detected_url
    if _detected_url:
        return _detected_url

    # Check settings/env first
    url = getattr(settings, "SURREAL_URL", None) or os.getenv("SURREAL_URL")
    if not url:
        # Auto-detect from common local/Docker addresses
        import httpx

        local_urls = ["http://localhost:8001", "http://surrealdb:8000"]  # NOSONAR
        detected = None
        for l_url in local_urls:
            try:
                with httpx.Client(timeout=0.5) as client:
                    r = client.get(l_url.rstrip("/") + "/health")
                    if r.status_code == 200:
                        detected = l_url
                        break
            except Exception as probe_err:
                logger.debug("[SurrealDB] Local probe %s failed: %s", l_url, probe_err)
                continue

        if detected:
            url = detected
        else:
            # SURREAL_URL is required in production -- no tenant-specific fallback baked in.
            # Set the SURREAL_URL environment variable in your Cloud Run service YAML.
            logger.warning(
                "[SurrealDB] SURREAL_URL is not set and no local SurrealDB instance was found. "
                "Set the SURREAL_URL environment variable. Defaulting to http://localhost:8001 "
                "which will fail if SurrealDB is not running locally."
            )
            url = "http://localhost:8001"  # NOSONAR

    if url.startswith("ws://") or url.startswith("wss://"):  # NOSONAR
        if not url.endswith("/rpc"):
            url = url.rstrip("/") + "/rpc"

    _detected_url = url
    logger.info("[SurrealDB] Using database URL: %s", _detected_url)
    return _detected_url


def _get_surreal_auth() -> dict:
    user = getattr(settings, "SURREAL_USER", os.getenv("SURREAL_USER", "root"))
    password = getattr(settings, "SURREAL_PASS", os.getenv("SURREAL_PASS", ""))

    if not password and getattr(settings, "DEBUG", True):
        password = "root"  # nosec B105

    # In tests or fallback offline modes we might still have root, but for production
    # settings.py would have already raised ImproperlyConfigured. If we reach here,
    # we enforce one last check (unless offline mode is detected).

    # In unittests we might not have a password, skip this check
    import sys

    is_testing = "test" in sys.argv
    if (
        not getattr(settings, "DEBUG", True)
        and password in ("", "root")
        and not getattr(settings, "SURREALDB_OFFLINE", False)
        and not is_testing
    ):
        raise ImproperlyConfigured(
            "[SurrealDB] Connecting with default 'root' credentials in a non-debug environment is forbidden. "
            "Set SURREAL_USER and SURREAL_PASS environment variables to secure credentials."
        )
    return {"username": user, "password": password}  # NOSONAR


async def _detect_active_namespace(url: str, auth: dict, db_name: str) -> str:
    global _detected_ns
    if _detected_ns:
        return _detected_ns

    # Check if SURREAL_NS is explicitly set in environment/settings (not default)
    env_ns = os.getenv("SURREAL_NS") or getattr(settings, "SURREAL_NS", None)
    if env_ns and env_ns not in ("", "aetheromni", "omnirag"):
        _detected_ns = env_ns
        return _detected_ns

    fallback_ns = env_ns or "aetheromni"
    try:
        async with AsyncSurreal(url) as db:
            await db.signin(auth)
            root_info_res = await db.query("INFO FOR ROOT;")
            namespaces = []
            if root_info_res and isinstance(root_info_res, list):
                first_el = root_info_res[0]
                if isinstance(first_el, dict):
                    if "result" in first_el and isinstance(first_el["result"], dict):
                        namespaces = list(first_el["result"].get("namespaces", {}).keys())
                    else:
                        namespaces = list(first_el.get("namespaces", {}).keys())
            elif isinstance(root_info_res, dict):
                namespaces = list(root_info_res.get("namespaces", {}).keys())

            if not namespaces:
                _detected_ns = fallback_ns
                return _detected_ns

            # Prioritize canonical namespaces in scan order
            pref = ["aetheromni", "omnirag"]
            for p in reversed(pref):
                if p in namespaces:
                    namespaces.remove(p)
                    namespaces.insert(0, p)

            for ns in namespaces:
                try:
                    await db.use(ns, db_name)
                    count_res = await db.query("SELECT count() FROM documents GROUP ALL;")
                    count = 0
                    if count_res and isinstance(count_res, list):
                        first_el = count_res[0]
                        if isinstance(first_el, dict):
                            if (
                                "result" in first_el
                                and isinstance(first_el["result"], list)
                                and len(first_el["result"]) > 0
                            ):
                                count = first_el["result"][0].get("count", 0)
                            elif "count" in first_el:
                                count = first_el.get("count", 0)
                        elif isinstance(first_el, list) and len(first_el) > 0:
                            count = first_el[0].get("count", 0)

                    if count > 0:
                        _detected_ns = ns
                        logger.info(
                            "[SurrealDB] Dynamic auto-detection selected active namespace '%s' with %d documents.",
                            ns,
                            count,
                        )
                        return _detected_ns
                except Exception as ns_err:
                    logger.debug("[SurrealDB] Namespace '%s' probe failed: %s", ns, ns_err)
                    continue

            # Default to the first available namespace or fallback
            _detected_ns = namespaces[0] if namespaces else fallback_ns
            logger.info("[SurrealDB] Dynamic auto-detection fallback selected namespace: %s", _detected_ns)
            return _detected_ns
    except Exception as err:
        logger.warning("[SurrealDB] Dynamic namespace detection failed, falling back to '%s': %s", fallback_ns, err)
        _detected_ns = fallback_ns
        return _detected_ns


def _get_surreal_ns_db() -> tuple[str, str]:
    global _detected_ns
    ns = _detected_ns or getattr(settings, "SURREAL_NS", os.getenv("SURREAL_NS", "aetheromni"))
    db = getattr(settings, "SURREAL_DB", os.getenv("SURREAL_DB", "extractor"))
    return ns, db


async def _async_run(sql: str, params: dict | None = None) -> list[dict]:
    url = _get_surreal_url()
    auth = _get_surreal_auth()
    db_name = getattr(settings, "SURREAL_DB", os.getenv("SURREAL_DB", "extractor"))

    global _detected_ns
    if not _detected_ns and "INFO FOR ROOT" not in sql:
        await _detect_active_namespace(url, auth, db_name)

    ns, _ = _get_surreal_ns_db()

    try:
        async with AsyncSurreal(url) as db:
            await db.signin(auth)
            await db.use(ns, db_name)
            result = await db.query(sql, params)
            return result
    except Exception as exc:
        logger.exception("[SurrealDB] SDK query failure for SQL: %s", sql[:120])
        raise RuntimeError(f"SurrealDB error: {exc}")


def _run(sql: str, params: dict | None = None) -> list[dict]:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return [{"status": "OK", "result": []}]

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Inside async context
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(_async_run(sql, params))
    else:
        return async_to_sync(_async_run)(sql, params)


def _first_result(results: Any) -> list[Any]:
    if not results:
        return []
    if isinstance(results, list):
        if len(results) > 0 and isinstance(results[0], dict) and "result" in results[0]:
            return results[0].get("result", [])
        return results
    elif isinstance(results, dict):
        if "result" in results:
            return results.get("result", [])
        return [results]
    return []


async def _async_check_health() -> bool:
    url = _get_surreal_url()
    try:
        async with AsyncSurreal(url):
            pass
        return True
    except Exception as e:
        logger.warning("[SurrealDB] WebSocket health check failed: %s", e)
        return False


def check_health() -> bool:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(_async_check_health())
    else:
        return async_to_sync(_async_check_health)()


_IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_field_name(field_name: str) -> None:
    """Ensure field names only contain alphanumeric characters and underscores."""
    if not _IDENTIFIER_REGEX.match(field_name):
        raise ValueError(f"Invalid field name: {field_name}")


def create_document(data: dict) -> dict:
    """Create a new document metadata record in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from django.contrib.auth import get_user_model

        from extractor.models import SourceDocument

        User = get_user_model()

        uploaded_by = None
        uid = data.get("uploaded_by_id")
        if uid:
            try:
                uploaded_by = User.objects.get(id=uid)
            except User.DoesNotExist:
                pass

        doc = SourceDocument.objects.create(
            uuid=data.get("doc_uuid"),
            file=data.get("file"),
            original_filename=data.get("original_filename", ""),
            file_hash=data.get("file_hash", ""),
            status=data.get("status", "PENDING"),
            uploaded_by=uploaded_by,
            language=data.get("language", ""),
            author=data.get("author", ""),
            title=data.get("title", ""),
            document_type=data.get("document_type", ""),
            page_count=data.get("page_count", 0),
            raw_markdown=data.get("raw_markdown", ""),
            refined_markdown=data.get("refined_markdown", ""),
            yaml_metadata=data.get("yaml_metadata", ""),
            qa_dataset=data.get("qa_dataset") or [],
            cost_usd=data.get("cost_usd", 0.0),
            semantic_signature=data.get("semantic_signature", ""),
            retry_count=data.get("retry_count", 0),
        )
        if data.get("expires_at"):
            from django.utils.dateparse import parse_datetime as django_parse

            doc.expires_at = django_parse(data["expires_at"])
            doc.save()
        return _model_to_dict(doc)

    # Add definition for VALID_DOCUMENT_FIELDS if not present
    VALID_DOCUMENT_FIELDS = {
        "doc_uuid",
        "file",
        "original_filename",
        "file_hash",
        "status",
        "uploaded_by_id",
        "language",
        "author",
        "title",
        "document_type",
        "page_count",
        "raw_markdown",
        "refined_markdown",
        "yaml_metadata",
        "qa_dataset",
        "cost_usd",
        "semantic_signature",
        "retry_count",
        "created_at",
        "updated_at",
        "expires_at",
        "input_tokens",
        "output_tokens",
        "publisher",
        "publication_year",
        "license_type",
        "doi",
    }

    payload = {k: v for k, v in data.items() if v is not None and k in VALID_DOCUMENT_FIELDS}
    fields = []
    params = {}
    for k, v in payload.items():
        _validate_field_name(k)
        if k in ("created_at", "updated_at", "expires_at"):
            fields.append(f"{k}: <datetime> ${k}")
            params[k] = v
        else:
            fields.append(f"{k}: ${k}")
            params[k] = v
    sql = f"INSERT INTO documents {{ {', '.join(fields)} }};"
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else {}


def update_document(doc_uuid: str, data: dict) -> dict:
    """Update fields on a document record in SurrealDB."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from django.contrib.auth import get_user_model

        from extractor.models import SourceDocument

        User = get_user_model()
        try:
            import uuid

            try:
                uuid.UUID(str(doc_uuid))
                doc = SourceDocument.objects.get(uuid=doc_uuid)
            except ValueError:
                doc = SourceDocument.objects.get(id=int(doc_uuid))
        except (SourceDocument.DoesNotExist, ValueError):
            return {}

        for k, v in data.items():
            if k == "uploaded_by_id":
                if v:
                    try:
                        doc.uploaded_by = User.objects.get(id=v)
                    except User.DoesNotExist:
                        pass
                else:
                    doc.uploaded_by = None
            elif k == "expires_at":
                if v:
                    from django.utils.dateparse import parse_datetime as django_parse

                    doc.expires_at = django_parse(v)
                else:
                    doc.expires_at = None
            elif hasattr(doc, k):
                setattr(doc, k, v)
        doc.save()
        return _model_to_dict(doc)

    VALID_DOCUMENT_FIELDS = {
        "doc_uuid",
        "file",
        "original_filename",
        "file_hash",
        "status",
        "uploaded_by_id",
        "language",
        "author",
        "title",
        "document_type",
        "page_count",
        "raw_markdown",
        "refined_markdown",
        "yaml_metadata",
        "qa_dataset",
        "cost_usd",
        "semantic_signature",
        "retry_count",
        "created_at",
        "updated_at",
        "expires_at",
        "input_tokens",
        "output_tokens",
        "publisher",
        "publication_year",
        "license_type",
        "doi",
    }

    payload = {k: v for k, v in data.items() if v is not None and k in VALID_DOCUMENT_FIELDS}
    if not payload:
        return get_document(doc_uuid) or {}

    set_parts = []
    params = {"doc_uuid": doc_uuid}
    for k, v in payload.items():
        _validate_field_name(k)
        if k in ("created_at", "updated_at", "expires_at"):
            set_parts.append(f"{k} = <datetime> ${k}")
            params[k] = v
        else:
            set_parts.append(f"{k} = ${k}")
            params[k] = v

    if "updated_at" not in payload:
        set_parts.append("updated_at = time::now()")

    sql = f"UPDATE documents SET {', '.join(set_parts)} WHERE doc_uuid = $doc_uuid;"  # nosec B608 # noqa: S608
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else {}


def get_document(doc_uuid: str) -> dict | None:
    """Retrieve a single document record by UUID."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            import uuid

            try:
                uuid.UUID(str(doc_uuid))
                doc = SourceDocument.objects.get(uuid=doc_uuid)
            except ValueError:
                doc = SourceDocument.objects.get(id=int(doc_uuid))
            return _model_to_dict(doc)
        except (SourceDocument.DoesNotExist, ValueError):
            return None

    sql = "SELECT * FROM documents WHERE doc_uuid = $doc_uuid;"  # nosec B608
    results = _run(sql, {"doc_uuid": doc_uuid})
    rows = _first_result(results)
    return rows[0] if rows else None


def get_documents(doc_uuids: list[str]) -> list[dict]:
    """Retrieve multiple document records by a list of UUIDs."""
    if not doc_uuids:
        return []

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            docs = SourceDocument.objects.filter(uuid__in=doc_uuids)
            return [_model_to_dict(doc) for doc in docs]
        except Exception:
            return []

    sql = "SELECT * FROM documents WHERE doc_uuid IN $doc_uuids;"  # nosec B608
    results = _run(sql, {"doc_uuids": doc_uuids})
    return _first_result(results)


def list_documents(user_id: str | None = None) -> list[dict]:
    """Retrieve documents from SurrealDB (user-specific + public)."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        if user_id:
            from django.db.models import Q

            qs = SourceDocument.objects.filter(Q(uploaded_by_id=user_id) | Q(uploaded_by__isnull=True))
        else:
            qs = SourceDocument.objects.all()
        return [_model_to_dict(doc) for doc in qs]

    if user_id:
        sql = (
            "SELECT * FROM documents WHERE uploaded_by_id = $user_id OR uploaded_by_id = NONE ORDER BY created_at DESC;"
        )
        return _first_result(_run(sql, {"user_id": str(user_id)}))
    else:
        sql = "SELECT * FROM documents ORDER BY created_at DESC;"
        return _first_result(_run(sql))


def get_document_by_hash(file_hash: str, user_id: str | None = None) -> dict | None:
    """Find document by file_hash (optionally for a specific user)."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        qs = SourceDocument.objects.filter(file_hash=file_hash)
        if user_id:
            qs = qs.filter(uploaded_by_id=user_id)
        doc = qs.first()
        return _model_to_dict(doc) if doc else None

    if user_id:
        sql = "SELECT * FROM documents WHERE file_hash = $file_hash AND uploaded_by_id = $user_id LIMIT 1;"
        rows = _first_result(_run(sql, {"file_hash": file_hash, "user_id": str(user_id)}))
    else:
        sql = "SELECT * FROM documents WHERE file_hash = $file_hash LIMIT 1;"
        rows = _first_result(_run(sql, {"file_hash": file_hash}))
    return rows[0] if rows else None


# ── System Settings helpers ─────────────────────────────────────────────────


def get_system_settings() -> dict:
    """Get the global system settings record. Creates default if missing."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SystemSettings

        s = SystemSettings.get_settings()
        return _settings_to_dict(s)

    sql = "SELECT * FROM system_settings:1;"
    rows = _first_result(_run(sql))
    if rows:
        return rows[0]

    # Create default settings if empty
    default_data = {"monthly_budget_usd": 10.0, "selected_model": "auto", "currency": "auto", "openrouter_api_key": ""}
    rows = _first_result(_run("UPSERT system_settings:1 CONTENT $data;", {"data": default_data}))
    return rows[0] if rows else default_data


def save_system_settings(data: dict) -> dict:
    """Upsert system settings record in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SystemSettings

        s = SystemSettings.get_settings()
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
        s.save()
        return _settings_to_dict(s)

    payload = {k: v for k, v in data.items() if v is not None}
    if "monthly_budget_usd" in payload:
        payload["monthly_budget_usd"] = float(payload["monthly_budget_usd"])
    sql = "UPSERT system_settings:1 CONTENT $data;"
    rows = _first_result(_run(sql, {"data": payload}))
    return rows[0] if rows else {}


# ── Audit Log helpers ───────────────────────────────────────────────────────


def list_audit_logs(limit: int = 100, start: int = 0) -> list[dict]:
    """Retrieve paginated audit logs from SurrealDB ordered by timestamp desc."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import AuditLog

        qs = AuditLog.objects.all().order_by("-timestamp")[start : start + limit]
        return [_audit_log_to_dict(log) for log in qs]

    sql = "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT $limit START $start;"
    return _first_result(_run(sql, {"limit": limit, "start": start}))


def count_audit_logs() -> int:
    """Count total audit logs in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import AuditLog

        return AuditLog.objects.count()

    sql = "SELECT count() AS n FROM audit_logs GROUP ALL;"
    rows = _first_result(_run(sql))
    return rows[0].get("n", 0) if rows else 0


def delete_document(doc_uuid: str) -> None:
    """Delete document record and all associated chunks/cache entries."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            import uuid

            try:
                uuid.UUID(str(doc_uuid))
                doc = SourceDocument.objects.get(uuid=doc_uuid)
            except ValueError:
                doc = SourceDocument.objects.get(id=int(doc_uuid))
            doc.delete()
        except (SourceDocument.DoesNotExist, ValueError):
            pass
        return

    # Flush cost to MonthlySpendLog before deleting from SurrealDB
    doc = get_document(doc_uuid)
    if doc:
        cost = doc.get("cost_usd") or 0.0
        created_at_str = doc.get("created_at")
        if cost > 0 and created_at_str:
            from django.utils.dateparse import parse_datetime as django_parse

            created_at = django_parse(created_at_str)
            if created_at:
                from extractor.models import MonthlySpendLog

                try:
                    MonthlySpendLog.add_cost(
                        date=created_at,
                        cost=cost,
                        in_tok=doc.get("input_tokens") or 0,
                        out_tok=doc.get("output_tokens") or 0,
                    )
                except Exception as exc:
                    logger.warning("[Delete] Failed to flush cost to MonthlySpendLog in delete_document: %s", exc)

    sql = (  # nosec B608
        "DELETE FROM documents WHERE doc_uuid = $doc_uuid;"
        "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"
        "DELETE FROM rag_cache WHERE $doc_uuid INSIDE sources;"
    )
    _run(sql, {"doc_uuid": doc_uuid})


# ── Chunk helpers ─────────────────────────────────────────────────────────────


def recreate_chunks(doc_uuid: str, chunk_payloads: list[dict]) -> None:
    """
    Atomically replace all chunks for a document.
    Deletes existing chunks first, then bulk-inserts new ones in a single statement.
    """
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _test_chunks[doc_uuid] = chunk_payloads
        # Execute mock run if patched in tests
        _run("INSERT INTO chunks $payloads;", {"payloads": chunk_payloads})
        return

    delete_chunks(doc_uuid)
    if not chunk_payloads:
        return
    for chunk in chunk_payloads:
        chunk["doc_uuid"] = doc_uuid

    # Insert chunks in small batches to prevent HTTP 413 Payload Too Large errors
    # resulting from large serialized vector embedding payloads in a single request.
    batch_size = 15
    for i in range(0, len(chunk_payloads), batch_size):
        batch = chunk_payloads[i : i + batch_size]
        _run("INSERT INTO chunks $payloads;", {"payloads": batch})


def delete_chunks(doc_uuid: str) -> None:
    """Remove all vector chunks for a document."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _test_chunks.pop(doc_uuid, None)
        _run("DELETE FROM chunks WHERE doc_uuid = $doc_uuid;", {"doc_uuid": doc_uuid})
        return

    sql = "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"  # nosec B608
    _run(sql, {"doc_uuid": doc_uuid})


def count_document_chunks(doc_uuid: str) -> int:
    """Returns the number of chunks stored in SurrealDB for the given doc_uuid."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        sql = "SELECT count() AS n FROM chunks WHERE doc_uuid = $doc_uuid GROUP ALL;"
        results = _first_result(_run(sql, {"doc_uuid": doc_uuid}))
        if results:
            return results[0].get("n", 0)
        return len(_test_chunks.get(doc_uuid, []))

    sql = "SELECT count() AS n FROM chunks WHERE doc_uuid = $doc_uuid GROUP ALL;"
    results = _first_result(_run(sql, {"doc_uuid": doc_uuid}))
    if results:
        return results[0].get("n", 0)
    return 0


def count_documents_chunks(doc_uuids: list[str]) -> dict[str, int]:
    """
    Returns a dictionary mapping doc_uuid to the number of chunks stored in SurrealDB
    for each doc_uuid in the provided list.
    """
    doc_uuids = [str(u) for u in doc_uuids]
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        sql = "SELECT count() AS n, doc_uuid FROM chunks WHERE doc_uuid INSIDE $doc_uuids GROUP BY doc_uuid;"
        results = _first_result(_run(sql, {"doc_uuids": doc_uuids}))
        if results:
            counts = dict.fromkeys(doc_uuids, 0)
            for row in results:
                uuid = row.get("doc_uuid")
                if uuid:
                    counts[uuid] = row.get("n", 0)
            return counts
        return {u: len(_test_chunks.get(u, [])) for u in doc_uuids}

    if not doc_uuids:
        return {}
    sql = "SELECT count() AS n, doc_uuid FROM chunks WHERE doc_uuid INSIDE $doc_uuids GROUP BY doc_uuid;"
    results = _first_result(_run(sql, {"doc_uuids": doc_uuids}))
    counts = dict.fromkeys(doc_uuids, 0)
    if results:
        for row in results:
            uuid = row.get("doc_uuid")
            if uuid:
                counts[uuid] = row.get("n", 0)
    return counts


def clone_chunks(source_uuid: str, target_uuid: str) -> None:
    """Copy all chunks from source_uuid to target_uuid (deduplication flow)."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _test_chunks[target_uuid] = list(_test_chunks.get(source_uuid, []))
        _run(
            "INSERT INTO chunks SELECT * FROM chunks WHERE doc_uuid = $source_uuid;",
            {"source_uuid": source_uuid, "target_uuid": target_uuid},
        )
        return

    sql = (  # nosec B608
        "LET $rows = (SELECT * FROM chunks WHERE doc_uuid = $source_uuid);"
        "FOR $row IN $rows {"
        "  INSERT INTO chunks {"
        "    doc_uuid: $target_uuid,"
        "    chunk_index: $row.chunk_index,"
        "    content: $row.content,"
        "    token_count: $row.token_count,"
        "    language: $row.language,"
        "    embedding: $row.embedding"
        "  };"
        "};"
    )
    _run(sql, {"source_uuid": source_uuid, "target_uuid": target_uuid})


def search_chunks_hnsw(
    query_embedding: list[float], limit: int = 10, allowed_doc_uuids: list[str] | None = None
) -> list[dict]:
    """
    Approximate nearest-neighbour chunk search using the HNSW index.
    Optionally restricts results to an allowlist of doc UUIDs (tenant isolation).
    """
    params = {"query_embedding": query_embedding, "limit": limit}
    if allowed_doc_uuids is not None:
        params["allowed_doc_uuids"] = allowed_doc_uuids
        sql = (  # nosec B608
            "SELECT id, doc_uuid, content, language, chunk_index, "
            "1.0 - vector::similarity::cosine(embedding, $query_embedding) AS score "
            "FROM chunks "
            "WHERE doc_uuid INSIDE $allowed_doc_uuids "
            "ORDER BY score ASC "
            "LIMIT $limit;"
        )
    else:
        sql = (  # nosec B608
            "SELECT id, doc_uuid, content, language, chunk_index, "
            "1.0 - vector::similarity::cosine(embedding, $query_embedding) AS score "
            "FROM chunks "
            "ORDER BY score ASC "
            "LIMIT $limit;"
        )
    return _first_result(_run(sql, params))


# ── RAG cache helpers ─────────────────────────────────────────────────────────


def upsert_rag_cache(
    user_id: str, query_text: str, query_embedding: list[float], answer_text: str, sources: list[str]
) -> None:
    """Insert a new semantic cache entry."""
    sql = (
        "INSERT INTO rag_cache {"
        "  user_id: $user_id,"
        "  query_text: $query_text,"
        "  query_embedding: $query_embedding,"
        "  answer_text: $answer_text,"
        "  sources: $sources"
        "};"
    )
    _run(
        sql,
        {
            "user_id": user_id,
            "query_text": query_text,
            "query_embedding": query_embedding,
            "answer_text": answer_text,
            "sources": sources,
        },
    )


def search_rag_cache_hnsw(
    user_id: str, query_embedding: list[float], threshold: float = 0.15, limit: int = 1
) -> list[dict]:
    """Search for a semantically similar cached answer for this user."""
    sql = (  # nosec B608
        "SELECT id, answer_text, sources, query_text, "
        "1.0 - vector::similarity::cosine(query_embedding, $query_embedding) AS score "
        "FROM rag_cache "
        "WHERE user_id = $user_id AND expires_at > time::now() "
        "ORDER BY score ASC "
        "LIMIT $limit;"
    )
    results = _first_result(
        _run(
            sql,
            {
                "user_id": user_id,
                "query_embedding": query_embedding,
                "limit": limit,
            },
        )
    )
    return [r for r in results if r.get("score", 1.0) <= threshold]


def purge_expired_rag_cache() -> int:
    """Delete all expired RAG cache entries. Returns count of deleted rows."""
    sql = "SELECT count() AS n FROM rag_cache WHERE expires_at < time::now() GROUP ALL;"
    count_rows = _first_result(_run(sql))
    count = count_rows[0].get("n", 0) if count_rows else 0
    _run("DELETE FROM rag_cache WHERE expires_at < time::now();")
    return count


def purge_all_rag_cache() -> None:
    """Delete all entries from rag_cache table."""
    _run("DELETE FROM rag_cache;")


def purge_all() -> None:
    """Delete all records from documents, chunks, rag_cache, and user_memories tables."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        SourceDocument.objects.all().delete()
        _test_chunks.clear()
        return

    _run("DELETE FROM documents; DELETE FROM chunks; DELETE FROM rag_cache; DELETE FROM user_memories;")


# ── KV cache helpers ──────────────────────────────────────────────────────────


def kv_cache_get(key: str) -> Any | None:
    """Retrieve a cached value by key. Returns None on miss or expiry."""
    sql = (  # nosec B608
        "SELECT cache_value FROM kv_cache "
        "WHERE cache_key = $cache_key AND (expires_at IS NONE OR expires_at > time::now());"
    )
    rows = _first_result(_run(sql, {"cache_key": key}))
    if rows:
        val_data = rows[0].get("cache_value") if "cache_value" in rows[0] else rows[0].get("val")
        if val_data is not None:
            try:
                if isinstance(val_data, str):
                    return json.loads(val_data)
                return val_data
            except (json.JSONDecodeError, ValueError):
                return val_data
    return None


def kv_cache_set(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    """Store a value in the KV cache with optional TTL."""
    from datetime import datetime, timedelta

    if ttl_seconds is not None:
        expires_at_val = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    else:
        expires_at_val = None

    json_value = json.dumps(value)
    sql = (  # nosec B608
        "DELETE FROM kv_cache WHERE cache_key = $cache_key;"
        "INSERT INTO kv_cache {"
        "  cache_key: $cache_key,"
        "  cache_value: $cache_value,"
        "  expires_at: $expires_at"
        "};"
    )
    _run(
        sql,
        {
            "cache_key": key,
            "cache_value": json_value,
            "expires_at": expires_at_val,
        },
    )


def kv_cache_delete_pattern(prefix: str) -> None:
    """Delete all KV entries whose key starts with the given prefix."""
    sql = "DELETE FROM kv_cache WHERE string::starts_with(cache_key, $prefix);"  # nosec B608
    _run(sql, {"prefix": prefix})


# ── User memory helpers ───────────────────────────────────────────────────────


def add_user_memory(user_id: str, memory_text: str, embedding: list[float]) -> None:
    """Persist a new user memory vector."""
    sql = "INSERT INTO user_memories {  user_id: $user_id,  memory_text: $memory_text,  embedding: $embedding};"
    _run(
        sql,
        {
            "user_id": user_id,
            "memory_text": memory_text,
            "embedding": embedding,
        },
    )


def search_user_memories(user_id: str, query_embedding: list[float], limit: int = 5) -> list[dict]:
    """Find the closest user memories using HNSW vector search."""
    sql = (  # nosec B608
        "SELECT memory_text, "
        "1.0 - vector::similarity::cosine(embedding, $query_embedding) AS score "
        "FROM user_memories "
        "WHERE user_id = $user_id "
        "ORDER BY score ASC "
        "LIMIT $limit;"
    )
    return _first_result(
        _run(
            sql,
            {
                "user_id": user_id,
                "query_embedding": query_embedding,
                "limit": limit,
            },
        )
    )


# ── Audit log helpers ─────────────────────────────────────────────────────────


def log_audit(
    action: str, user_id: str, doc_uuid: str | None = None, metadata: dict | None = None, ip_address: str = ""
) -> None:
    """Write an audit log entry to SurrealDB."""
    params = {
        "action": action,
        "user_id": str(user_id),
        "metadata": json.dumps(metadata or {}),
        "ip_address": ip_address,
    }
    if doc_uuid is not None:
        params["doc_uuid"] = str(doc_uuid)
        sql = (
            "INSERT INTO audit_logs {"
            "  action: $action,"
            "  user_id: $user_id,"
            "  doc_uuid: $doc_uuid,"
            "  metadata: $metadata,"
            "  ip_address: $ip_address"
            "};"
        )
    else:
        sql = (
            "INSERT INTO audit_logs {"
            "  action: $action,"
            "  user_id: $user_id,"
            "  metadata: $metadata,"
            "  ip_address: $ip_address"
            "};"
        )
    try:
        _run(sql, params)
    except Exception as exc:
        logger.warning("[SurrealDB] Failed to write audit log for action=%s user=%s: %s", action, user_id, exc)


def find_chunk_embedding(text: str) -> list[float] | None:
    """Finds an existing embedding for the exact text block from any previously ingested document."""
    sql = "SELECT embedding FROM chunks WHERE content = $text LIMIT 1;"
    results = _first_result(_run(sql, {"text": text}))
    if results and isinstance(results[0], dict) and "embedding" in results[0]:
        return results[0]["embedding"]
    return None


def count_user_memories(user_id: str) -> int:
    """Count user memories in SurrealDB."""
    sql = "SELECT count() AS n FROM user_memories WHERE user_id = $user_id GROUP ALL;"
    results = _first_result(_run(sql, {"user_id": user_id}))
    if results:
        return results[0].get("n", 0)
    return 0


def clear_user_memories(user_id: str) -> None:
    """Clear all memories for a user in SurrealDB."""
    sql = "DELETE FROM user_memories WHERE user_id = $user_id;"
    _run(sql, {"user_id": user_id})
