import json
import logging
import urllib.request
from typing import Any

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
from django.http import HttpRequest

from extractor.utils import APPLICATION_JSON

logger = logging.getLogger(__name__)


def _resolve_target_email(username: str, supabase_url: str) -> tuple[str, bool]:
    """
    Map a non-email username to a full email address.
    Returns (target_email, is_admin_check). Returns ("", False) if no mapping exists.
    """
    from urllib.parse import urlparse

    from django.conf import settings

    admin_username = getattr(settings, "ADMIN_USERNAME", "admin")
    admin_email = getattr(settings, "ADMIN_EMAIL", "admin@example.com")

    if username == admin_username:
        if admin_username == "admin":
            parsed = urlparse(supabase_url)
            domain = parsed.netloc if parsed.netloc else "example.com"
            return f"admin@{domain}", True
        else:
            return admin_email, True
    return "", False


def _sync_supabase_user(
    request: HttpRequest | None,
    resp_data: dict,
    supabase_url: str,
    username: str | None,
    is_admin_check: bool,
) -> User:
    """
    Retrieve or create a Django User from a successful Supabase auth response.
    Promotes to superuser/staff if the email matches the Supabase admin address.
    Stores the Supabase user_id in the session.
    """
    import hashlib
    from urllib.parse import urlparse

    from django.conf import settings

    user_info = resp_data.get("user", {})
    user_email = user_info.get("email", username)

    # Generate clean local Django username (prefix of email)
    base_username = user_email.split("@")[0]
    django_username = base_username

    # Ensure username is unique to prevent collisions with other domains sharing the same prefix
    suffix = ""
    attempt = 0
    while True:
        candidate = f"{base_username}{suffix}"[:150]
        conflicting_user = User.objects.filter(username=candidate).first()
        if not conflicting_user or conflicting_user.email == user_email:
            django_username = candidate
            break
        if attempt == 0:
            email_hash = hashlib.sha256(user_email.encode("utf-8")).hexdigest()[:8]
            suffix = f"_{email_hash}"
        else:
            suffix = f"_{attempt}"
        attempt += 1

    # Retrieve or instantiate standard Django User account
    user, created = User.objects.get_or_create(
        email=user_email, defaults={"username": django_username, "is_active": True}
    )

    parsed_url = urlparse(supabase_url)
    domain = parsed_url.netloc if parsed_url.netloc else "example.com"
    expected_admin_email = f"admin@{domain}"
    admin_email = getattr(settings, "ADMIN_EMAIL", "admin@example.com")

    is_promoted_admin = (is_admin_check and user_email.lower() == expected_admin_email.lower()) or (
        user_email.lower() == admin_email.lower()
    )
    if is_promoted_admin:
        user.is_superuser = True
        user.is_staff = True

    if created or is_promoted_admin:
        user.set_unusable_password()
        user.save()
        logger.info("[Auth] Django sync account created/updated for Supabase admin/user: %s", user_email)
    else:
        logger.info("[Auth] Supabase user session established: %s", user_email)

    # Store Supabase user_id on the request session for audit log linkage
    supabase_user_id = user_info.get("id", "")
    if request and supabase_user_id:
        request.session["supabase_user_id"] = supabase_user_id

    return user


class SupabaseAuthBackend(ModelBackend):
    """
    Custom server-side authentication backend that authenticates credentials against
    Supabase GoTrue Auth service, dynamically creating standard Django User accounts
    and establishing local authenticated sessions, while maintaining seamless fallback
    to local Django superusers / SQLite auth databases when offline or unconfigured.
    """

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> User | None:
        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        # 1. If Supabase is unconfigured, fall back immediately to local Django Database auth
        if not supabase_url or not supabase_key:
            return super().authenticate(request, username, password, **kwargs)

        # 2. Supabase Auth requires email addresses for authentication.
        # If the input is 'admin', map it to 'admin@<supabase_domain>' to authenticate via Supabase.
        # Otherwise, if it's another non-email username, fall back to local Django DB.
        target_email = (username or "").strip()
        is_admin_check = False
        if "@" not in target_email:
            target_email, is_admin_check = _resolve_target_email(target_email, supabase_url)
            if not target_email:
                local_user = super().authenticate(request, username, password, **kwargs)
                if local_user:
                    logger.info(f"[Auth] Local user authenticated: {username}")
                    return local_user
                return None

        # 3. Securely dispatch HTTPS POST to Supabase GoTrue Auth REST endpoint
        # Validate email format to prevent auth bypass via crafted usernames
        import re

        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", target_email):
            logger.warning("[Auth] Supabase auth rejected: '%s' is not a valid email format.", target_email)
            return None

        url = f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=password"
        headers = {
            "apikey": supabase_key,
            "Content-Type": APPLICATION_JSON,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        payload = json.dumps({"email": target_email, "password": password}).encode("utf-8")

        try:
            from extractor.utils import validate_url_scheme

            validate_url_scheme(url)
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
                resp_data = json.loads(response.read().decode("utf-8"))
                return _sync_supabase_user(request, resp_data, supabase_url, username, is_admin_check)

        except urllib.error.HTTPError as e:
            # Handle standard login credential mismatches (400 Bad Request) from Supabase GoTrue
            error_body = e.read().decode("utf-8")
            logger.warning(f"[Auth] Supabase authentication rejected: HTTP {e.code} - {error_body}")
            # Fall back to local Django DB
            return super().authenticate(request, username, password, **kwargs)
        except Exception as e:
            # Handle connection timeouts, DNS resolve issues, or unconfigured network sockets
            logger.exception(f"[Auth] Supabase API network connectivity exception: {e}")
            # Fall back to local Django DB
            return super().authenticate(request, username, password, **kwargs)
