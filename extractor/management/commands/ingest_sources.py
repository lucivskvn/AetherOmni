import os
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from extractor import cloud_tasks
from extractor.models import SourceDocument
from extractor.tasks import process_document_task
from extractor.utils import calculate_file_sha256, check_budget_and_api_limit


class Command(BaseCommand):
    help = "Scan the data/samples/ booklet directory and ingest all booklets into the SurrealDB knowledge base."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sources-dir",
            type=str,
            default=os.path.join(settings.BASE_DIR, "data", "samples"),
            help="Directory containing the booklet files to ingest.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run the extraction pipeline synchronously in the terminal with live logs instead of queuing asynchronously.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force re-ingestion of files even if they already exist in the database with the same hash.",
        )

    def handle(self, *args, **options):
        sources_dir = options["sources_dir"]
        sync_mode = options["sync"]
        force_mode = options["force"]

        self.stdout.write(self.style.SUCCESS("=== Starting Booklet Ingestion Process ==="))
        self.stdout.write(f"Sources Directory: {sources_dir}")
        self.stdout.write(f"Processing Mode: {'SYNCHRONOUS (Live logs)' if sync_mode else 'ASYNCHRONOUS (Queue)'}")
        self.stdout.write(f"Force Re-ingest: {force_mode}\n")

        if not os.path.exists(sources_dir):
            raise CommandError(f"Sources directory does not exist: {sources_dir}")

        files_to_ingest = self._find_files(sources_dir)
        if not files_to_ingest:
            self.stdout.write(self.style.WARNING("No supported booklet documents found in the data/samples directory."))
            return

        self.stdout.write(self.style.SUCCESS(f"Found {len(files_to_ingest)} candidate booklet(s) to process.\n"))

        for idx, file_path in enumerate(files_to_ingest, 1):
            filename = os.path.basename(file_path)
            self.stdout.write(f"[{idx}/{len(files_to_ingest)}] Analyzing: {filename}")

            try:
                file_hash = calculate_file_sha256(file_path, safe_base_dir=sources_dir)
                self.stdout.write(f"   - SHA-256 Hash: {file_hash}")

                existing_doc = SourceDocument.objects.filter(file_hash=file_hash, status="COMPLETED").first()
                exact_name_match = SourceDocument.objects.filter(
                    file_hash=file_hash, original_filename=filename
                ).exists()

                if existing_doc and exact_name_match and not force_mode:
                    self.stdout.write(
                        self.style.WARNING("   - [SKIPPED] Already fully ingested and indexed in the knowledge base.")
                    )
                    continue

                if existing_doc and not exact_name_match and not force_mode:
                    self._handle_duplicate_document(filename, file_hash, existing_doc)
                    continue

                self.stdout.write("   - Status: Preparing fresh ingestion...")

                try:
                    check_budget_and_api_limit()
                except Exception as budget_err:
                    self.stdout.write(
                        self.style.ERROR(f"   - [HALTED] Monthly budget limit reached. Cannot proceed: {budget_err}\n")
                    )
                    continue

                doc = self._create_pending_document(file_path, filename, file_hash, force_mode)
                self._process_document(doc, sync_mode)

            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"   - [ERROR] Failed to ingest {filename}: {exc!s}\n"))

        self.stdout.write(self.style.SUCCESS("=== Booklet Ingestion Process Completed ==="))

    def _find_files(self, sources_dir):
        """Scan for supported document files."""
        supported_extensions = [".pdf", ".jpg", ".jpeg", ".png", ".csv", ".txt"]
        files_to_ingest = []
        for root, _, files in os.walk(sources_dir):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in supported_extensions:
                    files_to_ingest.append(os.path.join(root, file))
        return files_to_ingest

    def _handle_duplicate_document(self, filename, file_hash, existing_doc):
        """
        Deduplication match — clone SurrealDB chunks from existing doc to new record.
        Uses surreal_db.clone_chunks to clone chunks atomically.
        """
        from extractor import surreal_db

        self.stdout.write(
            self.style.SUCCESS(
                f"   - [INSTANT CACHE] Found identical file hash under name '{existing_doc.original_filename}'."
            )
        )
        with transaction.atomic():
            doc = SourceDocument.objects.create(
                file=existing_doc.file,
                original_filename=filename,
                file_hash=file_hash,
                status="COMPLETED",
                language=existing_doc.language,
                author=existing_doc.author,
                title=f"{existing_doc.title} (Duplicate Source)",
                document_type=existing_doc.document_type,
                page_count=existing_doc.page_count,
                raw_markdown=existing_doc.raw_markdown,
                refined_markdown=existing_doc.refined_markdown,
                yaml_metadata=existing_doc.yaml_metadata,
                qa_dataset=existing_doc.qa_dataset,
                cost_usd=Decimal("0.00"),
                semantic_signature=existing_doc.semantic_signature,
                expires_at=timezone.now() + timedelta(days=int(getattr(settings, "DATA_RETENTION_DAYS", 30))),
            )

        # Clone SurrealDB chunks outside transaction (DB has its own atomicity)
        try:
            surreal_db.clone_chunks(str(existing_doc.uuid), str(doc.uuid))
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"   - [WARNING] SurrealDB chunk clone failed: {exc}"))

        self.stdout.write(self.style.SUCCESS("   - [SUCCESS] Duplicated reference created in DB at $0.00 cost.\n"))

    def _create_pending_document(self, file_path, filename, file_hash, force_mode):
        """Create document record and handle force mode deletions."""

        with open(file_path, "rb") as f:
            django_file = File(f, name=filename)

            with transaction.atomic():
                if force_mode:
                    for old_doc in SourceDocument.objects.filter(file_hash=file_hash):
                        try:
                            old_doc.file.delete(save=False)
                        except Exception as exc:
                            self.stdout.write(self.style.WARNING(f"   - [WARNING] Failed to delete old file: {exc}"))
                        old_doc.delete()

                return SourceDocument.objects.create(
                    file=django_file, original_filename=filename, file_hash=file_hash, status="PENDING"
                )

    def _process_document(self, doc, sync_mode):
        """Process document synchronously or dispatch via Cloud Tasks."""

        if sync_mode:
            self.stdout.write("   - Running extraction pipeline synchronously...")
            process_document_task({"document_id": doc.id})
            doc.refresh_from_db()
            if doc.status == "COMPLETED":
                self.stdout.write(
                    self.style.SUCCESS(
                        f"   - [SUCCESS] Processed! Title: '{doc.title}', Author: '{doc.author}', "
                        f"Language: '{doc.language}', Cost: ${doc.cost_usd:.4f} USD\n"
                    )
                )
            else:
                self.stdout.write(self.style.ERROR(f"   - [FAILED] Error: {doc.error_message or 'Unknown error'}\n"))
        else:
            self.stdout.write("   - Queueing asynchronous background task...")
            transaction.on_commit(lambda d_id=doc.id: cloud_tasks.enqueue("process_document", {"document_id": d_id}))
            self.stdout.write(self.style.SUCCESS("   - [QUEUED] Task dispatched to Cloud Tasks.\n"))
