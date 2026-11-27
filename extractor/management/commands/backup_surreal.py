"""Off-host gzip-compressed SurrealDB backup Cloud Run Job command (stored in encrypted GCS)."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
from datetime import UTC, datetime

import httpx
from django.core.management.base import BaseCommand, CommandError
from google.cloud import storage  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Export SurrealDB namespace/database snapshot and upload to BACKUP_BUCKET_NAME."

    def handle(self, *args, **options):
        backup_bucket = os.getenv("BACKUP_BUCKET_NAME", "").strip()
        surreal_url = os.getenv("SURREAL_URL", "").strip()
        surreal_user = os.getenv("SURREAL_USER", "").strip()
        surreal_pass = os.getenv("SURREAL_PASS", "").strip()
        surreal_ns = os.getenv("SURREAL_NS", "korda").strip()
        surreal_db = os.getenv("SURREAL_DB", "extractor").strip()

        if not backup_bucket or not surreal_url:
            raise CommandError("BACKUP_BUCKET_NAME and SURREAL_URL are required.")

        normalized_url = surreal_url.rstrip("/").removesuffix("/rpc")
        if normalized_url.startswith("wss://"):
            http_url = "https://" + normalized_url.removeprefix("wss://")
        elif normalized_url.startswith("https://"):
            http_url = normalized_url
        else:
            raise CommandError("SURREAL_URL must use wss:// or https:// for off-host backup exports.")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        blob_name = f"backups/{surreal_ns}/{surreal_db}/{stamp}.surql.gz"
        # staging/ is the only prefix where the runtime identity holds
        # get/delete/update (IAM-conditioned); backups/ stays write-only.
        staging_name = f"staging/{surreal_ns}/{surreal_db}/{stamp}.surql.gz"

        client = storage.Client()
        bucket = client.bucket(backup_bucket)
        # Stream to a staging object first: if the export fails mid-stream the
        # final backup name never holds a truncated snapshot (AUD-359). On
        # success the staging object is server-side copied to the final name.
        staging_blob = bucket.blob(staging_name)
        try:
            total_bytes, sha256_hex = self._stream_export(
                http_url, surreal_user, surreal_pass, surreal_ns, surreal_db, staging_blob
            )
        except CommandError:
            staging_blob.delete()
            raise
        except Exception as exc:
            try:
                staging_blob.delete()
            except Exception:
                logger.warning("[Backup] Failed to delete staging object %s", staging_blob.name)
            raise CommandError(f"SurrealDB backup failed: {exc}") from exc

        # Record integrity metadata on the staging object BEFORE promotion:
        # objects.copy carries metadata to the destination, and the runtime
        # identity has no storage.objects.update on backups/ (write-only).
        staging_blob.metadata = {
            "surreal_ns": surreal_ns,
            "surreal_db": surreal_db,
            "exported_at": stamp,
            "uncompressed_bytes": str(total_bytes),
            "uncompressed_sha256": sha256_hex,
        }
        staging_blob.patch()

        try:
            bucket.copy_blob(staging_blob, bucket, blob_name)
        finally:
            try:
                staging_blob.delete()
            except Exception:
                logger.warning("[Backup] Failed to delete staging object %s", staging_blob.name)
        self.stdout.write(
            f"Uploaded SurrealDB backup to gs://{backup_bucket}/{blob_name} "
            f"({total_bytes} uncompressed bytes, sha256:{sha256_hex[:12]}…)"
        )

    def _stream_export(
        self, http_url: str, surreal_user: str, surreal_pass: str, surreal_ns: str, surreal_db: str, staging_blob
    ) -> tuple[int, str]:
        """Stream the gzip-compressed /export response into the staging blob."""
        # Stream gzip -> GCS directly: Cloud Run /tmp is memory-backed, so
        # spooling a large export through a temp file can OOM the 1Gi job.
        total_bytes = 0
        digest = hashlib.sha256()
        with (
            httpx.stream(
                "GET",
                f"{http_url}/export",
                auth=(surreal_user, surreal_pass) if surreal_user else None,
                headers={"surreal-ns": surreal_ns, "surreal-db": surreal_db},
                timeout=300.0,
            ) as resp,
            staging_blob.open("wb", content_type="application/gzip", timeout=300) as raw_out,
            gzip.GzipFile(fileobj=raw_out, mode="wb") as gz_out,
        ):
            resp.raise_for_status()
            for chunk in resp.iter_bytes(chunk_size=65536):
                total_bytes += len(chunk)
                digest.update(chunk)
                gz_out.write(chunk)
        if total_bytes == 0:
            raise CommandError("SurrealDB /export returned an empty snapshot; aborting backup upload.")
        return total_bytes, digest.hexdigest()
