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


def _generate_unique_username(user_email: str) -> str:
    import hashlib

    base_username = user_email.split("@")[0]

    candidate = base_username[:150]
    conflicting_user = User.objects.filter(username=candidate).first()
    if not conflicting_user or conflicting_user.email == user_email:
        return candidate

    email_hash = hashlib.sha256(user_email.encode("utf-8")).hexdigest()[:8]
    candidate = f"{base_username}_{email_hash}"[:150]
    conflicting_user = User.objects.filter(username=candidate).first()
    if not conflicting_user or conflicting_user.email == user_email:
        return candidate

    attempt = 1
    while True:
        candidate = f"{base_username}_{attempt}"[:150]
        conflicting_user = User.objects.filter(username=candidate).first()
        if not conflicting_user or conflicting_user.email == user_email:
            return candidate
        attempt += 1


def _sync_supabase_user(
    request: HttpRequest | None,
    resp_data: dict,
    username: str | None,
) -> User:
    """
    Retrieve or create a Django User from a successful Supabase auth response.
    Promotes to superuser/staff if the email matches the ADMIN_EMAIL.
    Stores the Supabase user_id in the session.
    """
    from django.conf import settings

    user_info = resp_data.get("user", {})
    user_email = user_info.get("email", username)

    django_username = _generate_unique_username(user_email)

    # Retrieve or instantiate standard Django User account
    user, created = User.objects.get_or_create(
        email=user_email, defaults={"username": django_username, "is_active": True}
    )

    admin_email = getattr(settings, "ADMIN_EMAIL", "").strip()
    is_promoted_admin = bool(admin_email) and user_email.lower() == admin_email.lower()

    # 1. Supabase App Metadata Role Syncing
    app_metadata = user_info.get("app_metadata", {})
    if app_metadata.get("is_admin") is True:
        is_promoted_admin = True

    # Supabase is authoritative for privileges on every successful authentication.
    # A removed admin claim must demote the corresponding Django account immediately.
    role_changed = user.is_superuser != is_promoted_admin or user.is_staff != is_promoted_admin
    user.is_superuser = is_promoted_admin
    user.is_staff = is_promoted_admin

    if created or role_changed:
        user.set_unusable_password()
        user.save()
        logger.info("[Auth] Django sync account created/updated from Supabase claims: %s", user_email)
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
    and establishing local authenticated sessions. Local Django authentication is used
    only when Supabase is explicitly unconfigured (for offline development).
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

        target_email = (username or "").strip()
        is_email = "@" in target_email

        def _fallback_local_auth():
            if is_email:
                user = User.objects.filter(email=target_email).first()
                if user:
                    return super(SupabaseAuthBackend, self).authenticate(
                        request, username=user.username, password=password, **kwargs
                    )
            return super(SupabaseAuthBackend, self).authenticate(request, username, password, **kwargs)

        if not supabase_url or not supabase_key:
            return _fallback_local_auth()

        if not is_email:
            local_user = _fallback_local_auth()
            if local_user:
                logger.info(f"[Auth] Local user authenticated: {username}")
                return local_user
            return None

        import re

        if not re.fullmatch(r"[^@\s]+@[^@\s.]+\.[^@\s]+", target_email):
            logger.warning("[Auth] Supabase auth rejected: '%s' is not a valid email format.", target_email)
            return None

        return self._do_supabase_auth(request, target_email, password, supabase_url, supabase_key)

    def _do_supabase_auth(self, request, target_email, password, supabase_url, supabase_key):
        url = f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=password"
        headers = {
            "apikey": supabase_key,
            "Content-Type": APPLICATION_JSON,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        body: dict = {"email": target_email, "password": password}
        captcha_token = request.POST.get("cf-turnstile-response", "") if request else ""
        turnstile_required = bool(getattr(settings, "CF_TURNSTILE_SITE_KEY", ""))
        if turnstile_required and not captcha_token:
            logger.warning("[Auth] Supabase authentication rejected before dispatch: security check incomplete.")
            return None
        if captcha_token:
            body["gotrue_meta_security"] = {"captcha_token": captcha_token}
        payload = json.dumps(body).encode("utf-8")

        try:
            from extractor.utils import validate_url_scheme

            validate_url_scheme(url)
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
                resp_data = json.loads(response.read().decode("utf-8"))
                return _sync_supabase_user(request, resp_data, target_email)
        except urllib.error.HTTPError as e:
            logger.warning("[Auth] Supabase authentication rejected: HTTP %s", e.code)
            if turnstile_required:
                return None
        except Exception as e:
            logger.exception(f"[Auth] Supabase API network connectivity exception: {e}")
            if turnstile_required:
                return None

        # Do not fall back to a local password after a configured Supabase
        # authentication attempt. Otherwise Supabase account disablement, password
        # resets, and MFA policy can be bypassed by stale Django credentials.
        return None
