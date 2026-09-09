#!/usr/bin/env python3
"""Fail-closed checks for reliability controls derived from review incidents."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(relative_path: str) -> str:
    path = ROOT / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"Required reliability-control file is missing: {relative_path}")
    return path.read_text(encoding="utf-8")


def _require(errors: list[str], source: str, required: tuple[str, ...], message: str) -> None:
    if not all(value in source for value in required):
        errors.append(message)


def main() -> int:
    """Verify task, tenant, auth, export, and deployment controls before remote checks."""
    errors: list[str] = []
    try:
        settings = _read("core/settings.py")
        surreal = _read("extractor/surreal_db.py")
        exports = _read("extractor/file_utils.py")
        views = _read("extractor/views.py")
        dashboard = _read("static/js/main.js")
        login = _read("extractor/templates/extractor/login.html")
        recovery = _read("extractor/templates/extractor/reset_password_confirm.html")
        base_template = _read("extractor/templates/extractor/base.html")
        auth = _read("extractor/auth.py")
        models = _read("extractor/models.py")
        service_manifest = _read("infra/gcp/service.yaml")
        worker_manifest = _read("infra/gcp/service-worker.yaml")
        cloudbuild = _read("infra/gcp/cloudbuild.yaml")
        pulumi = _read("infra/pulumi/__main__.py")
        settings_normalizer = _read("scripts/normalize_system_settings.py")
        surreal_schema = _read("schema.surql")
    except OSError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    _require(errors, settings, ("SURREAL_EXECUTOR_WORKERS", "max(1,"), "Executor worker count must be positive.")
    _require(
        errors,
        surreal,
        ("def claim_document_for_reembedding", "if task_name:", "cancel_requested != true"),
        "Re-embedding claims must fence named tasks and reject cancellations.",
    )
    _require(
        errors,
        views,
        ("def _abort_inflight_processing", "cancel_document_task(doc_uuid,"),
        "Bulk document deletion must invoke task cancellation for in-flight documents.",
    )
    tasks_source = _read("extractor/tasks.py")
    _require(
        errors,
        tasks_source,
        ("def _reap_single_stale_doc", "CANCEL_REQUESTED: False"),
        "Task reaper auto-retry must clear CANCEL_REQUESTED to avoid recovery latch deadlock.",
    )
    _require(
        errors,
        exports,
        ('replace("\\r", "\\\\r")', 'replace("\\n", "\\\\n")'),
        "YAML export values must escape carriage returns and line feeds.",
    )
    _require(
        errors,
        exports,
        ("_validate_xlsx_archive", "_XLSX_MAX_ROWS", "_XLSX_MAX_MARKDOWN_BYTES", "_bounded_excel_row"),
        "XLSX parsing must enforce archive, row, cell, and rendered-output budgets.",
    )
    _require(
        errors,
        dashboard,
        ("visibilitychange", "document.visibilityState", "newVisibleDocument && !globalThis._reloadTriggered"),
        "Dashboard polling must pause while hidden and guard reloads.",
    )
    _require(
        errors,
        dashboard,
        ("executeRagStream", "'/api/v1/stream-query/'", "ragAnswer.textContent += event.token", "'/rag-search/'"),
        "Dashboard RAG must stream text-safe SSE tokens and retain the JSON fallback.",
    )
    if "sessionStorage" in login or "sessionStorage" in recovery:
        errors.append("Recovery credentials must not be persisted in web storage.")
    dedup_lookup = views.partition("def _find_existing_doc_by_hash")[2].partition("def _get_dedup_field")[0]
    if not dedup_lookup:
        errors.append("Tenant deduplication source anchors are missing.")
    if (
        "status = 'COMPLETED' LIMIT 1" in dedup_lookup
        or "SourceDocument.objects.filter(file_hash=file_hash" in dedup_lookup
    ):
        errors.append("Private document deduplication must not fall back to a global hash lookup.")
    _require(
        errors,
        surreal,
        ("Unexpected SurrealDB transaction result; denying request.", "return False"),
        "Unexpected production rate-limit results must deny the request.",
    )
    _require(
        errors,
        views,
        ("[ExportRateLimit] SurrealDB rate limit fallback error", "return False"),
        "Export rate-limit backend failures must deny the request.",
    )
    _require(
        errors,
        views,
        (
            "[RAGRateLimit] SurrealDB rate limit fallback error",
            "[RAGStreamRateLimit] SurrealDB rate limit fallback error",
            "[SFTPreviewRateLimit] SurrealDB rate limit fallback error",
        ),
        "RAG and SFT rate-limit backend failures must deny the request.",
    )
    if "openrouter_api_key" in base_template:
        errors.append("The web console must not render an OpenRouter credential input.")
    if "openrouter_api_key" in models or "openrouter_api_key" in surreal_schema:
        errors.append("Retired OpenRouter credentials must not remain in application persistence schemas.")
    _require(
        errors,
        views,
        ("gotrue_meta_security", "SUPABASE_CONFIRM_EMAIL_REQUIRED", "email_confirmed_at"),
        "Supabase Auth must forward CAPTCHA and enforce configured email confirmation.",
    )
    _require(
        errors,
        auth,
        ("ADMIN_EMAIL", "app_metadata"),
        "Supabase app metadata must not be the authority for administrator access.",
    )
    spend_delete_signal = models.partition("def flush_cost_to_monthly_log")[2].partition(
        "@receiver(post_save, sender=SourceDocument)"
    )[0]
    _require(
        errors,
        spend_delete_signal,
        ('getattr(settings, "SURREALDB_OFFLINE", False)', "return"),
        "Online document deletion must not double-count spend through the Django mirror signal.",
    )
    _require(
        errors,
        service_manifest,
        ("memory: 1024Mi",),
        "Web Cloud Run manifest must retain the post-incident memory floor.",
    )
    _require(
        errors,
        worker_manifest,
        ("memory: 1024Mi",),
        "Worker Cloud Run manifest must retain the post-incident memory floor.",
    )
    _require(
        errors,
        pulumi,
        (
            '"memory": "1Gi"',
            '"memory": "2Gi"',
            "korda-worker-cloud-tasks-invoker",
            "roles/run.invoker",
            "roles/run.viewer",
        ),
        "Pulumi must retain memory floors, Cloud Tasks worker invocation, and deployment-controller read access.",
    )
    _require(
        errors,
        cloudbuild,
        ("--memory=1Gi", "--memory=2Gi", "--no-allow-unauthenticated", "DATABASE_URL=SUPABASE_DATABASE_URL:latest"),
        "Cloud Build must retain memory floors, a private worker, and a required Supabase database secret.",
    )
    _require(
        errors,
        views,
        ("class SaveSettingsView", "return self.request.user.is_superuser", "or not parsed.netloc"),
        "Global settings must be superuser-only and accept only strict HTTPS origins.",
    )
    _require(
        errors,
        settings_normalizer,
        ('parser.add_argument("--apply"', "Dry run only", "has_retired_field", "postcondition failed"),
        "System-settings normalization must remain dry-run by default and verify retired-field removal explicitly.",
    )

    if errors:
        print("FAILED: Reliability contract violation(s):", file=sys.stderr)
        for error in errors:
            print(f"  ✗ {error}", file=sys.stderr)
        return 1
    print("✓ Reliability contracts for task delivery, recovery, exports, and polling are enforced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
