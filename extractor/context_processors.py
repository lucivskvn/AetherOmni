import logging
import os

from django.conf import settings

from extractor.models import SystemSettings

_log = logging.getLogger(__name__)


def _read_sonar_version(file_path: str) -> str | None:
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("sonar.projectVersion="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val
    except OSError as exc:
        _log.warning("Could not read sonar-project.properties: %s", exc)
    return None


def _read_file_version(file_path: str) -> str | None:
    try:
        with open(file_path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        _log.warning("Could not read VERSION file: %s", exc)
    return None


def _resolve_release_version() -> str:
    if env_ver := os.environ.get("RELEASE_VERSION"):
        return env_ver

    sonar_val = _read_sonar_version(os.path.join(settings.BASE_DIR, "sonar-project.properties"))
    if sonar_val:
        return sonar_val

    file_val = _read_file_version(os.path.join(settings.BASE_DIR, "VERSION"))
    if file_val:
        return file_val

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
