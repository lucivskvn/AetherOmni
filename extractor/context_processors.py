import os

from django.conf import settings

from extractor.models import SystemSettings


def _read_file_version(file_path: str) -> str | None:
    try:
        with open(file_path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _resolve_release_version() -> str:
    if env_ver := os.environ.get("RELEASE_VERSION"):
        return env_ver.lstrip("v")

    file_val = _read_file_version(os.path.join(settings.BASE_DIR, "VERSION"))
    if file_val:
        return file_val if file_val.count(".") == 2 else f"{file_val}.0"

    return "0.0.0"


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
