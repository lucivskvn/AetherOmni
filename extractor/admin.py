from django.contrib import admin

from extractor.models import AuditLog, SourceDocument, SystemSettings


@admin.register(SourceDocument)
class SourceDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "language", "author", "document_type", "status", "cost_usd", "created_at")
    list_filter = ("status", "language", "document_type", "created_at")
    search_fields = ("title", "original_filename", "author", "file_hash", "semantic_signature")
    readonly_fields = ("uuid", "file_hash", "retry_count", "created_at", "updated_at", "expires_at")
    fieldsets = (
        (
            "General Information",
            {"fields": ("uuid", "file", "original_filename", "file_hash", "status", "error_message")},
        ),
        (
            "Taxonomy & Curation Metadata",
            {"fields": ("title", "author", "language", "document_type", "page_count", "semantic_signature")},
        ),
        (
            "LLM Outputs & Costs",
            {
                "fields": (
                    "raw_markdown",
                    "refined_markdown",
                    "yaml_metadata",
                    "qa_dataset",
                    "input_tokens",
                    "output_tokens",
                    "cost_usd",
                    "retry_count",
                )
            },
        ),
        ("GDPR & Lifecycle Auditing", {"fields": ("created_at", "updated_at", "expires_at")}),
    )


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ("id", "monthly_budget_usd", "selected_model")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "document", "ip_address")
    list_filter = ("action", "created_at", "user")
    search_fields = ("details", "ip_address", "document__original_filename", "user__username")
    readonly_fields = ("created_at", "user", "action", "document", "ip_address", "details")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
