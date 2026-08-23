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


def _read_git_head_sha(git_dir: str) -> str | None:
    head_file = os.path.join(git_dir, "HEAD")
    if not os.path.isfile(head_file):
        return None
    try:
        with open(head_file, encoding="utf-8") as f:
            ref = f.read().strip()
        if ref.startswith("ref: refs/"):
            clean_rel_path = ref[5:].replace("\\", "/").strip()
            # Prevent directory traversal in git ref
            if ".." in clean_rel_path.split("/"):
                return None
            ref_path = os.path.normpath(os.path.join(git_dir, clean_rel_path))
            real_git_dir = os.path.abspath(git_dir)
            real_ref_path = os.path.abspath(ref_path)
            if not real_ref_path.startswith(real_git_dir):
                return None
            if os.path.isfile(real_ref_path):
                with open(real_ref_path, encoding="utf-8") as f:
                    return f.read().strip()[:7]
            return None
        return ref[:7] if len(ref) >= 7 and "/" not in ref else None
    except OSError:
        return None


def _resolve_commit_sha() -> str:
    if env_sha := os.environ.get("COMMIT_SHA") or os.environ.get("SHORT_SHA") or os.environ.get("BUILD_SHA"):
        return env_sha[:7]

    git_dir = os.path.join(settings.BASE_DIR, ".git")
    return _read_git_head_sha(git_dir) or "unknown"


from typing import Any

from django.http import HttpRequest


def system_settings(request: HttpRequest) -> dict[str, Any]:
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
        "COMMIT_SHA": _resolve_commit_sha(),
        "CF_TURNSTILE_SITE_KEY": getattr(settings, "CF_TURNSTILE_SITE_KEY", ""),
    }
