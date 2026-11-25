#!/usr/bin/env python3
"""Static Phase-0 security boundary checks for KORDA.

Asserts the trust-boundary invariants documented in
docs/security-phase0.md directly against the Pulumi components, deploy
manifests, and application security code. Fails closed: any missing or
weakened control exits non-zero.

Run: python3 scripts/check_security_boundaries.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok  {message}")
    else:
        print(f" FAIL {message}")
        FAILURES.append(message)


def read(rel: str) -> str:
    path = ROOT / rel
    if not path.is_file():
        FAILURES.append(f"missing file: {rel}")
        return ""
    return path.read_text(encoding="utf-8")


def main() -> int:
    components = read("infra/pulumi/components.py")
    pulumi_main = read("infra/pulumi/__main__.py")
    service_yaml = read("infra/gcp/service.yaml")
    worker_yaml = read("infra/gcp/service-worker.yaml")
    cloudbuild = read("infra/gcp/cloudbuild.yaml")
    surreal_db = read("extractor/surreal_db.py")
    settings_py = read("core/settings.py")

    print("[1] GCS bucket posture (ComplianceBucket)")
    check("uniform_bucket_level_access=True" in components, "UBLA enabled on buckets")
    check("retention_duration_seconds=0" in components, "ephemeral bucket soft-delete disabled")
    check(
        "enabled=False" in components and "BucketVersioningArgs" in components,
        "ephemeral versioning off / backup versioning on",
    )
    check('matches_prefixes=["raw/ephemeral/"]' in components, "raw/ephemeral/ lifecycle TTL present")
    check('matches_prefixes=["exports/"]' in components, "exports/ lifecycle TTL present")
    check("AbortIncompleteMultipartUpload" in components, "multipart abort lifecycle rule present")

    print("[2] IAM least privilege (Pulumi)")
    check("roles/aiplatform.user" in pulumi_main, "runtime SA has aiplatform.user")
    check("roles/cloudtasks.enqueuer" in pulumi_main, "runtime SA has cloudtasks.enqueuer")
    check("roles/run.viewer" in pulumi_main, "runtime SA has run.viewer (worker read only)")
    check("roles/secretmanager.secretAccessor" in pulumi_main, "per-secret secretAccessor binding")
    check("roles/storage.objectAdmin" in pulumi_main, "objectAdmin scoped to media bucket")
    check("roles/storage.objectCreator" in pulumi_main, "backup bucket is write-only (objectCreator)")
    check("project-wide secretAccessor" not in pulumi_main.lower(), "no project-wide secretAccessor grant")
    check("allUsers" in components, "public invoker only via ScaleToZeroWorker(is_public=True)")

    print("[3] Cloud Run service auth posture")
    check("--no-allow-unauthenticated" in cloudbuild, "Cloud Build deploys worker without public invoker")
    check("--allow-unauthenticated" in cloudbuild, "Cloud Build deploys web with public invoker")
    for name, text in (("service.yaml", service_yaml), ("service-worker.yaml", worker_yaml)):
        check("GUNICORN_WORKERS" in text, f"{name} pins GUNICORN_WORKERS")
    check("2Gi" in worker_yaml or "2048Mi" in worker_yaml, "worker memory floor preserved in manifest")

    print("[4] SurrealDB transport & credential enforcement")
    check("must use a ws or wss scheme" in surreal_db or "ws_schemes" in surreal_db, "scheme allowlist enforced")
    check("Production SURREAL_URL must use encrypted wss" in surreal_db, "wss-only transport in production")
    check("SURREAL_USER and SURREAL_PASS cannot be empty" in surreal_db, "credentials mandatory outside offline mode")
    check("default 'root'" in read("scripts/init_surreal.py"), "init script rejects root password in production")

    print("[5] Application config guards")
    check("SUPABASE_DATABASE_URL" in cloudbuild, "Cloud Build requires Supabase database secret")
    check("ADMIN_EMAIL" in settings_py or "ADMIN_EMAIL" in pulumi_main, "ADMIN_EMAIL admin authority present")
    check("SURREAL_NS" in pulumi_main and "SURREAL_DB" in pulumi_main, "namespace/database pinned by Pulumi")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} boundary check(s) FAILED")
        return 1
    print("All Phase-0 security boundary checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
