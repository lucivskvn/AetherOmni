import logging
import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models
from django.db.models import JSONField

logger = logging.getLogger(__name__)


class SafeVectorField(models.Field):
    """Legacy field stub to prevent Django migration import errors."""

    def __init__(self, *args, **kwargs):
        kwargs.pop("dimensions", 768)
        super().__init__(*args, **kwargs)

    def db_type(self, connection):
        return "TEXT"


class SourceDocument(models.Model):
    """
    Primary document record stored in SQLite.
    Vector chunks, semantic cache, and user memories live in SurrealDB.
    """

    STATUS_CHOICES = [
        ("PENDING", "Queued"),
        ("EXTRACTING", "OCR & Layout Analysis"),
        ("REFINING", "Semantic Curation"),
        ("EMBEDDING", "Vector Indexing"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
    ]

    # Secure identifier for URL routing to prevent IDOR / enumeration attacks.
    # BUG-04: unique=True added to match SurrealDB's idx_documents_uuid UNIQUE.
    # Migration 0021 backfills any pre-existing NULL uuid rows before this constraint applies.
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True, unique=True)

    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_documents"
    )

    # File storage
    file = models.FileField(upload_to="documents/%Y/%m/%d/", max_length=500)
    original_filename = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64, db_index=True)  # SHA-256 content address

    # Metadata extracted or analyzed
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    error_message = models.TextField(blank=True, default="")

    # Curated taxonomy fields for Digital Preservation exports
    language = models.CharField(max_length=50, blank=True, default="Unknown")
    author = models.CharField(max_length=255, blank=True, default="Unknown")
    title = models.CharField(max_length=255, blank=True, default="Untitled")
    document_type = models.CharField(max_length=50, blank=True, default="PDF")
    page_count = models.IntegerField(default=0)

    # Legal and Provenance Tracking
    publisher = models.CharField(max_length=255, blank=True, default="Unknown")
    publication_year = models.CharField(max_length=4, blank=True, default="")
    license_type = models.CharField(max_length=100, blank=True, default="Unknown")
    doi = models.CharField(max_length=255, blank=True, default="")

    # Processing outputs
    raw_markdown = models.TextField(blank=True)  # Stage 1 OCR output
    refined_markdown = models.TextField(blank=True)  # Stage 2 editorial output (editor edits this)
    yaml_metadata = models.TextField(blank=True)  # Extracted YAML metadata block
    qa_dataset = JSONField(default=list, blank=True)  # Extracted Q&A dataset pairs for SFT

    # Operational metrics & budget auditing
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0.0)

    # Deduplication & cross-lingual translation linkages
    semantic_signature = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # Operational metrics & budget auditing — BUG-17: PositiveIntegerField
    # enforces retry_count >= 0 at the DB level, preventing negative values
    # that could bypass the retry_count >= 3 limit guard.
    retry_count = models.PositiveIntegerField(default=0)

    # Lifespans and GDPR auditing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)  # GDPR auto-cleanup target

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.language})"

    @property
    def is_expired(self):
        from django.utils import timezone

        if self.expires_at:
            exp = self.expires_at
            if timezone.is_naive(exp):
                exp = timezone.make_aware(exp, timezone.get_current_timezone())
            return timezone.now() > exp
        return False


class SystemSettings(models.Model):
    CURRENCY_CHOICES = [
        ("auto", "Detected by Browser Locale"),
        ("USD", "USD ($)"),
        ("IDR", "IDR (Rp)"),
        ("SAR", "SAR (SR)"),
    ]
    monthly_budget_usd = models.DecimalField(max_digits=10, decimal_places=2, default=10.00)
    selected_model = models.CharField(max_length=100, default="auto")
    currency = models.CharField(max_length=10, choices=CURRENCY_CHOICES, default="auto")
    csrf_trusted_origins = models.TextField(
        blank=True,
        default="",
        help_text="Comma-separated list of custom domains/origins to trust for CSRF (e.g. https://my-custom-domain.com).",
    )
    openrouter_api_key = models.CharField(max_length=255, blank=True, default="")

    @property
    def openrouter_api_key_masked(self) -> str:
        """Returns a masked placeholder if the key is configured, preventing plain-text leaks to clients."""
        return "••••••••••••••••" if self.openrouter_api_key else ""

    @classmethod
    def get_settings(cls):
        from django.conf import settings

        if not getattr(settings, "SURREALDB_OFFLINE", False):
            from decimal import Decimal

            from extractor import surreal_db

            try:
                raw = surreal_db.get_system_settings()

                class SurrealSettings:
                    def __init__(self, raw_data):
                        self.monthly_budget_usd = Decimal(str(raw_data.get("monthly_budget_usd", 10.0)))
                        self.selected_model = raw_data.get("selected_model", "auto")
                        self.currency = raw_data.get("currency", "auto")
                        self.csrf_trusted_origins = raw_data.get("csrf_trusted_origins", "")
                        self.openrouter_api_key = raw_data.get("openrouter_api_key", "")

                    @property
                    def openrouter_api_key_masked(self) -> str:
                        return "••••••••••••••••" if self.openrouter_api_key else ""

                    def __str__(self):
                        return f"SystemSettings(Budget=${self.monthly_budget_usd}, Model={self.selected_model})"

                return SurrealSettings(raw)
            except Exception as e:
                logger.warning("[SystemSettings] Failed to fetch settings from SurrealDB: %s", e)

        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return f"SystemSettings(Budget=${self.monthly_budget_usd}, Model={self.selected_model})"


class AuditAction:
    """Shared string constants for AuditLog.action to eliminate magic strings."""

    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    UPLOAD = "UPLOAD"
    UPLOAD_CACHED = "UPLOAD_CACHED"
    EXTRACTION_START = "EXTRACTION_START"
    EXTRACTION_COMPLETED = "EXTRACTION_COMPLETED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    DELETE = "DELETE"
    PURGE_ALL = "PURGE_ALL"
    DOCUMENT_EDITED = "DOCUMENT_EDITED"
    DOCUMENT_REQUEUED = "DOCUMENT_REQUEUED"
    SYSTEM_CONTROL = "SYSTEM_CONTROL"
    EXPORT = "EXPORT"


class AuditLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    action = models.CharField(max_length=50, db_index=True)  # Use AuditAction constants for all action values
    document = models.ForeignKey(
        SourceDocument, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    details = models.TextField(blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    # SEC-06 fix: expose `timestamp` as a property alias for `created_at` so that
    # _audit_log_to_dict() serialising key "timestamp" does not cause AttributeError
    # in consumers. The underlying DB column remains `created_at`.
    @property
    def timestamp(self):
        return self.created_at

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.pop("force_update_allowed", False):
            raise PermissionError("AuditLog records are immutable append-only ledgers and cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not kwargs.pop("force_delete_allowed", False):
            raise PermissionError("AuditLog records are immutable append-only ledgers and cannot be deleted.")
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.created_at} - {self.user} - {self.action}"


class UserMemory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memories")
    memory_text = models.TextField()
    embedding = models.JSONField()  # 768-dim float vector
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} - {self.memory_text[:30]}"


class MonthlySpendLog(models.Model):
    """
    Persistent accumulator for AI compute spend, keyed by calendar month.
    Survives SourceDocument deletions – cost_usd is flushed here via pre_delete
    signal *before* each document row is removed from the database.
    """

    year = models.SmallIntegerField(db_index=True)
    month = models.SmallIntegerField()  # 1-12
    accumulated_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    accumulated_input_tokens = models.BigIntegerField(default=0)
    accumulated_output_tokens = models.BigIntegerField(default=0)

    class Meta:
        unique_together = (("year", "month"),)
        ordering = ["-year", "-month"]

    @classmethod
    def _add_cost_surreal(cls, year: int, month: int, cost: Decimal, in_tok: int, out_tok: int) -> bool:
        import json
        from decimal import Decimal as D

        from extractor import surreal_db

        key = f"monthly_spend_log:{year}:{month}"
        try:
            existing = surreal_db.kv_cache_get(key)
            if isinstance(existing, dict):
                data = existing
            elif isinstance(existing, str):
                try:
                    data = json.loads(existing)
                except Exception:
                    data = {"accumulated_cost_usd": 0.0, "accumulated_input_tokens": 0, "accumulated_output_tokens": 0}
            else:
                data = {"accumulated_cost_usd": 0.0, "accumulated_input_tokens": 0, "accumulated_output_tokens": 0}

            accumulated_cost = D(str(data.get("accumulated_cost_usd", 0.0))) + D(str(cost))
            accumulated_in = int(data.get("accumulated_input_tokens", 0)) + in_tok
            accumulated_out = int(data.get("accumulated_output_tokens", 0)) + out_tok

            new_data = {
                "accumulated_cost_usd": float(accumulated_cost),
                "accumulated_input_tokens": accumulated_in,
                "accumulated_output_tokens": accumulated_out,
            }
            surreal_db.kv_cache_set(key, new_data)
            return True
        except Exception as exc:
            logger.warning("[SurrealDB] Failed to add cost to KV cache monthly log: %s", exc)
            return False

    @classmethod
    def _add_cost_django(cls, year: int, month: int, cost: Decimal, in_tok: int, out_tok: int) -> bool:
        from decimal import Decimal as D

        from django.db import IntegrityError

        try:
            try:
                cls.objects.get_or_create(year=year, month=month)
            except IntegrityError:
                pass

            updated = cls.objects.filter(year=year, month=month).update(
                accumulated_cost_usd=models.F("accumulated_cost_usd") + D(str(cost)),
                accumulated_input_tokens=models.F("accumulated_input_tokens") + in_tok,
                accumulated_output_tokens=models.F("accumulated_output_tokens") + out_tok,
            )
            return updated == 1
        except Exception as exc:  # pragma: no cover
            logger.warning("[MonthlyLog] add_cost skipped: %s", exc)
            return False

    @classmethod
    def add_cost(cls, date, cost: Decimal, in_tok: int = 0, out_tok: int = 0) -> bool:
        """Thread-safe upsert: add cost to the specified year/month bucket."""
        from django.conf import settings

        year, month = date.year, date.month
        if not getattr(settings, "SURREALDB_OFFLINE", False):
            return cls._add_cost_surreal(year, month, cost, in_tok, out_tok)
        return cls._add_cost_django(year, month, cost, in_tok, out_tok)

    @classmethod
    def total_for_month(cls, year: int, month: int) -> Decimal:
        """Return the total spend for the given calendar month."""
        from decimal import Decimal as D

        from django.conf import settings

        if not getattr(settings, "SURREALDB_OFFLINE", False):
            import json

            from extractor import surreal_db

            key = f"monthly_spend_log:{year}:{month}"
            try:
                existing = surreal_db.kv_cache_get(key)
                if existing:
                    data = json.loads(existing) if isinstance(existing, str) else existing
                    return D(str(data.get("accumulated_cost_usd", 0.0)))
            except Exception as exc:
                logger.warning("[SurrealDB] Failed to read cost from KV cache monthly log: %s", exc)
            return D("0.0")

        try:
            row = cls.objects.filter(year=year, month=month).first()
            return row.accumulated_cost_usd if row else D("0.0")
        except Exception:  # pragma: no cover — handles pre-migration state
            return D("0.0")


# ── Signal Receivers for Auth Event Auditing ──────────────────────────────────
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver


@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    ip = None
    if request:
        from extractor.utils import get_client_ip

        ip = get_client_ip(request)
    from extractor.utils import AuditEvent, log_audit_event

    log_audit_event(
        AuditEvent(
            action=AuditAction.LOGIN,
            user=user,
            actor_id=getattr(request, "session", {}).get("supabase_user_id") if request else None,
            details=f"User '{user.username}' authenticated successfully.",
            ip_address=ip,
        )
    )


@receiver(user_logged_out)
def log_user_logout(sender, request, user, **kwargs):
    if user:
        ip = None
        if request:
            from extractor.utils import get_client_ip

            ip = get_client_ip(request)
        from extractor.utils import AuditEvent, log_audit_event

        log_audit_event(
            AuditEvent(
                action=AuditAction.LOGOUT,
                user=user,
                actor_id=getattr(request, "session", {}).get("supabase_user_id") if request else None,
                details=f"User '{user.username}' logged out.",
                ip_address=ip,
            )
        )


# ── Signal Receivers for SurrealDB RAG Cache Invalidation ────────────────────
from django.db.models.signals import post_delete, post_save, pre_delete


def purge_rag_cache():
    """
    Purges all cached query/answer pairs from the SurrealDB rag_cache table
    and wipes exact-match KV cache entries with the 'rag_search_cache:' prefix.
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return
    try:
        from extractor import surreal_db

        surreal_db.kv_cache_delete_pattern("rag_search_cache:")
        surreal_db.purge_all_rag_cache()
    except Exception as exc:
        logger.warning("[Cache] Failed to purge SurrealDB RAG cache: %s", exc)


@receiver(pre_delete, sender=SourceDocument)
def flush_cost_to_monthly_log(sender, instance, **kwargs):
    """
    Before a SourceDocument row is removed, persist its cost_usd into the
    MonthlySpendLog for the month in which the document was *created*.
    This ensures the Monthly AI Compute Spend metric is never reset by deletions.
    """
    if instance.cost_usd and instance.cost_usd > 0:
        ts = instance.created_at
        try:
            MonthlySpendLog.add_cost(
                date=ts,
                cost=instance.cost_usd,
                in_tok=instance.input_tokens or 0,
                out_tok=instance.output_tokens or 0,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("[MonthlyLog] Failed to flush cost before delete: %s", exc)


@receiver(post_save, sender=SourceDocument)
def invalidate_rag_cache_on_save(sender, instance, **kwargs):
    if instance.status == "COMPLETED":
        purge_rag_cache()


@receiver(post_delete, sender=SourceDocument)
def invalidate_rag_cache_on_delete(sender, instance, **kwargs):
    from django.conf import settings

    if not getattr(settings, "SURREALDB_OFFLINE", False):
        try:
            from extractor import surreal_db

            surreal_db.delete_document(str(instance.uuid))
        except Exception as exc:
            logger.warning("[Cleanup] Failed to delete SurrealDB document: %s", exc)
    purge_rag_cache()
