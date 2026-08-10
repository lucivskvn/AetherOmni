import logging
import os

from django.conf import settings

from extractor.models import SystemSettings

_log = logging.getLogger(__name__)


def _resolve_release_version() -> str:
    env_ver = os.environ.get("RELEASE_VERSION")
    if env_ver:
        return env_ver
    version_file = os.path.join(settings.BASE_DIR, "VERSION")
    if os.path.isfile(version_file):
        try:
            with open(version_file, encoding="utf-8") as f:
                return f.read().strip()
        except OSError as exc:  # nosec B110 - non-critical fallback
            _log.warning("Could not read VERSION file: %s", exc)
    return "1.5"


def system_settings(request):
    """
    Injects the single global SystemSettings instance dynamically into the template context.
    Provides easy access to dynamic configuration parameters like budget caps and source library URLs.
    """
    _ = request  # Intentional no-op to satisfy Django context processor signature and resolve SonarQube S1172
    return {
        "system_settings": SystemSettings.get_settings(),
        "SUPABASE_URL": getattr(settings, "SUPABASE_URL", ""),
        "SUPABASE_PUBLIC_KEY": getattr(settings, "SUPABASE_PUBLIC_KEY", ""),
        "RELEASE_VERSION": _resolve_release_version(),
        "CF_TURNSTILE_SITE_KEY": getattr(settings, "CF_TURNSTILE_SITE_KEY", ""),
    }
