import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from extractor import view_common
from extractor.context_processors import resolve_commit_sha, resolve_release_version

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)


def favicon_view(_request):
    """Serve a lightweight transparent 204 No Content for favicon.ico requests."""
    return HttpResponse(status=204, content_type="image/x-icon")


@require_http_methods(["GET", "HEAD"])
def release_metadata_view(_request):
    """Expose the release identity injected into this running Cloud Run revision."""
    return JsonResponse(
        {"release_version": resolve_release_version(), "commit_sha": resolve_commit_sha()},
        headers={"Cache-Control": "no-store"},
    )


def sentry_debug_view(request):
    """Verification endpoint for testing Sentry exception and trace ingestion during development."""
    user = getattr(request, "user", None)
    is_super = getattr(user, "is_superuser", False) if user else False
    if not settings.DEBUG and not is_super:
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("Sentry debug endpoint is restricted.")
    raise ZeroDivisionError("Sentry verification debug error: division by zero trigger.")
