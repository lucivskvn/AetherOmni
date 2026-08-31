import functools
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


def _resolve_ref_from_packed_refs(git_dir: str, target_ref: str) -> str | None:
    packed_refs_file = os.path.join(git_dir, "packed-refs")
    if not os.path.isfile(packed_refs_file):
        return None
    try:
        with open(packed_refs_file, encoding="utf-8") as pf:
            for line in pf:
                line = line.strip()
                if line and not line.startswith(("#", "^")) and " " in line:
                    sha_part, ref_part = line.split(" ", 1)
                    if ref_part.strip() == target_ref:
                        return sha_part[:7]
    except OSError:
        return None
    return None


def _resolve_ref_from_loose_file(git_dir: str, rel_path: str) -> str | None:
    clean_rel_path = rel_path.replace("\\", "/").strip()
    if ".." in clean_rel_path.split("/"):
        return None
    ref_path = os.path.normpath(os.path.join(git_dir, clean_rel_path))
    real_git_dir = os.path.abspath(git_dir)
    real_ref_path = os.path.abspath(ref_path)
    if not real_ref_path.startswith(real_git_dir) or not os.path.isfile(real_ref_path):
        return None
    try:
        with open(real_ref_path, encoding="utf-8") as f:
            return f.read().strip()[:7]
    except OSError:
        return None


def _read_git_head_sha(git_dir: str) -> str | None:
    head_file = os.path.join(git_dir, "HEAD")
    if not os.path.isfile(head_file):
        return None
    try:
        with open(head_file, encoding="utf-8") as f:
            ref = f.read().strip()
    except OSError:
        return None

    if not ref.startswith("ref: refs/"):
        return ref[:7] if len(ref) >= 7 and "/" not in ref else None

    clean_target = ref[5:].strip()
    return _resolve_ref_from_loose_file(git_dir, clean_target) or _resolve_ref_from_packed_refs(git_dir, clean_target)


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
