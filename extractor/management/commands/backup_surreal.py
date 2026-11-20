"""Off-host gzip-compressed SurrealDB backup Cloud Run Job command (stored in encrypted GCS)."""

from __future__ import annotations

import gzip
import os
import tempfile
from datetime import UTC, datetime

import httpx
from django.core.management.base import BaseCommand, CommandError
from google.cloud import storage  # type: ignore[attr-defined]


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

        with tempfile.NamedTemporaryFile(suffix=".surql.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            total_bytes = 0
            with (
                httpx.stream(
                    "GET",
                    f"{http_url}/export",
                    auth=(surreal_user, surreal_pass) if surreal_user else None,
                    headers={"surreal-ns": surreal_ns, "surreal-db": surreal_db},
                    timeout=300.0,
                ) as resp,
                gzip.open(tmp_path, "wb") as gz_out,
            ):
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=65536):
                    total_bytes += len(chunk)
                    gz_out.write(chunk)
            if total_bytes == 0:
                raise CommandError("SurrealDB /export returned an empty snapshot; aborting backup upload.")

            client = storage.Client()
            blob = client.bucket(backup_bucket).blob(blob_name)
            blob.upload_from_filename(tmp_path, content_type="application/gzip", timeout=300)
            self.stdout.write(f"Uploaded SurrealDB backup to gs://{backup_bucket}/{blob_name}")
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f"SurrealDB backup failed: {exc}") from exc
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
