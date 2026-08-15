import logging

from django.core.management.base import BaseCommand
from django.db.models import Q

from extractor.cloud_tasks import enqueue
from extractor.models import SourceDocument

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-queues completed documents that are missing publisher or doi metadata to force LLM backfill extraction."

    def handle(self, *args, **options):
        import datetime

        # Target documents created before the deployment date (e.g., July 16, 2026) to guarantee idempotency
        # and prevent perpetual re-queueing of genuinely missing DOI records.
        cutoff_date = datetime.datetime(2026, 7, 16, tzinfo=datetime.UTC)

        # Find completed legacy documents where publisher is Unknown/empty or doi is empty
        docs = SourceDocument.objects.filter(status="COMPLETED", created_at__lt=cutoff_date).filter(
            Q(publisher__in=["", "Unknown"]) | Q(doi="")
        )

        count = docs.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS("No documents require backfilling!"))
            return

        self.stdout.write(self.style.WARNING(f"Found {count} documents missing metadata. Re-queueing..."))

        doc_list = list(docs)
        # Optimization: Perform single batch update instead of individual doc.save() calls
        docs.update(status="PENDING", error_message="")

        for doc in doc_list:
            # Send to background worker queue
            enqueue("extractor.tasks.process_document_task", payload={"doc_uuid": str(doc.uuid)})
            self.stdout.write(f"Re-queued document: {doc.uuid} ({doc.title})")

        self.stdout.write(self.style.SUCCESS(f"Successfully re-queued {count} documents for metadata backfill."))
