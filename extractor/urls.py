from django.contrib.auth import views as auth_views
from django.urls import path

from extractor import task_handlers, views
from extractor.forms import TurnstileAuthenticationForm

urlpatterns = [
    path("favicon.ico", views.favicon_view, name="favicon"),
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="extractor/login.html", authentication_form=TurnstileAuthenticationForm
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
    path("register/", views.register_view, name="register"),
    path("forgot-password/", views.forgot_password_view, name="forgot_password"),
    path("reset-password-confirm/", views.reset_password_confirm_view, name="reset_password_confirm"),
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("upload/", views.UploadView.as_view(), name="upload_document"),
    path("audit-logs/", views.AuditLogListView.as_view(), name="audit_logs"),
    path("document/<uuid:doc_uuid>/", views.DocumentDetailView.as_view(), name="document_detail"),
    path("document/<uuid:doc_uuid>/save/", views.DocumentSaveView.as_view(), name="save_document"),
    path("document/<uuid:doc_uuid>/delete/", views.DocumentDeleteView.as_view(), name="delete_document"),
    path("document/<uuid:doc_uuid>/retry/", views.DocumentRetryView.as_view(), name="retry_document"),
    path("purge-all/", views.DocumentPurgeAllView.as_view(), name="purge_all_documents"),
    path("rag-search/", views.DocumentRAGSearchView.as_view(), name="rag_search"),
    path("export/", views.ExportZipView.as_view(), name="export_zip"),
    path("export/sft-jsonl/", views.ExportSftJsonlView.as_view(), name="export_sft_jsonl"),
    path("document/<uuid:doc_uuid>/sft-preview/", views.SFTDatasetPreviewView.as_view(), name="sft_dataset_preview"),
    path("bulk-action/", views.BulkDocumentActionView.as_view(), name="bulk_action"),
    path("save-settings/", views.SaveSettingsView.as_view(), name="save_settings"),
    path("api/documents/status/", views.DocumentStatusAPIView.as_view(), name="document_status_api"),
    path("deployment-controller/", views.DeploymentControllerView.as_view(), name="deployment_controller"),
    path(
        "password-change/",
        auth_views.PasswordChangeView.as_view(
            template_name="extractor/password_change.html", success_url="/password-change/done/"
        ),
        name="password_change",
    ),
    path(
        "password-change/done/",
        auth_views.PasswordChangeDoneView.as_view(template_name="extractor/password_change_done.html"),
        name="password_change_done",
    ),
    # Cloud Tasks internal webhook receiver (CSRF-exempt, OIDC-verified)
    path("internal/tasks/<str:task_name>/", task_handlers.CloudTaskHandlerView.as_view(), name="cloud_task_handler"),
    # Client-side Supabase OAuth session exchange endpoint
    path(
        "api/auth/supabase-session/",
        views.SupabaseSessionExchangeView.as_view(),
        name="supabase_session_exchange",
    ),
]
