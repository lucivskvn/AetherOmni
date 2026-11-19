"""Off-host encrypted/compressed SurrealDB backup Cloud Run Job command."""

from __future__ import annotations

import gzip
import os
from datetime import UTC, datetime

import httpx
from django.core.management.base import BaseCommand
from google.cloud import storage


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
            self.stderr.write("BACKUP_BUCKET_NAME and SURREAL_URL are required.")
            return

        http_url = surreal_url.replace("wss://", "https://").replace("ws://", "http://").removesuffix("/rpc")
        resp = httpx.get(
            f"{http_url}/export",
            auth=(surreal_user, surreal_pass) if surreal_user else None,
            headers={"surreal-ns": surreal_ns, "surreal-db": surreal_db},
            timeout=120.0,
        )
        resp.raise_for_status()

        compressed = gzip.compress(resp.content)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        blob_name = f"backups/{surreal_ns}/{surreal_db}/{stamp}.surql.gz"
        client = storage.Client()
        blob = client.bucket(backup_bucket).blob(blob_name)
        blob.upload_from_string(compressed, content_type="application/gzip")
        self.stdout.write(f"Uploaded SurrealDB backup to gs://{backup_bucket}/{blob_name}")
