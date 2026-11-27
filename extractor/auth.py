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

__all__ = [
    "SupabaseAuthBackend",
    "generate_unique_username",
]


def generate_unique_username(user_email: str) -> str:
    import hashlib
    import uuid

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

    for attempt in range(1, 100):
        candidate = f"{base_username}_{attempt}"[:150]
        conflicting_user = User.objects.filter(username=candidate).first()
        if not conflicting_user or conflicting_user.email == user_email:
            return candidate

    # Fallback to UUID-derived candidate if loop exhausted
    return f"{base_username[:130]}_{uuid.uuid4().hex[:8]}"


def _get_or_create_django_user(user_email: str, django_username: str) -> tuple[User, bool]:
    import uuid

    from django.db import IntegrityError, transaction

    # Deterministic binding: order by pk so duplicate-email rows always resolve
    # to the same account, and warn so operators can deduplicate (AUD-295).
    email_matches = User.objects.filter(email__iexact=user_email).order_by("pk")
    if email_matches.count() > 1:
        logger.warning(
            "[Auth] Multiple Django users share email %s; binding to pk=%s deterministically.",
            user_email,
            email_matches.values_list("pk", flat=True).first(),
        )
    user = email_matches.first()
    if user:
        return user, False

    # Each create runs in its own savepoint: a concurrent first-login for the
    # same email can trip the LOWER(email) unique index, and without a
    # savepoint the IntegrityError would poison the caller's atomic block so
    # the recovery lookup below raises TransactionManagementError instead
    # (AUD-418).
    candidates = [django_username, f"{django_username[:130]}_{uuid.uuid4().hex[:8]}"]
    for index, candidate in enumerate(candidates):
        try:
            with transaction.atomic():
                user = User.objects.create(
                    username=candidate,
                    email=user_email,
                    is_active=True,
                )
            return user, True
        except IntegrityError:
            existing = User.objects.filter(email__iexact=user_email).first()
            if existing:
                return existing, False
            if index == len(candidates) - 1:
                raise
    raise RuntimeError(f"Django user creation failed for {user_email}")


def _evaluate_admin_claim(user_email: str, user_info: dict) -> bool:
    from django.conf import settings

    admin_email = getattr(settings, "ADMIN_EMAIL", "").strip()
    # SEC-04: Only ADMIN_EMAIL server-side env var grants Django superuser.
    # app_metadata.is_admin is NOT trusted — any Supabase service-role holder
    # can set it on any user, granting immediate Django superuser on next login.
    # Log any attempted escalation via app_metadata for security monitoring.
    app_metadata = user_info.get("app_metadata", {})
    if app_metadata.get("is_admin") is True:
        logger.warning(
            "[Auth] app_metadata.is_admin=True claim detected for %s — ignored per SEC-04 policy.",
            user_email,
        )
    admin_emails = {e.strip().lower() for e in admin_email.split(",") if e.strip()}
    return bool(admin_emails) and user_email.lower() in admin_emails


def _attach_supabase_session(request: HttpRequest | None, user_info: dict) -> None:
    supabase_user_id = user_info.get("id", "")
    if request and supabase_user_id:
        try:
            request.session["supabase_user_id"] = supabase_user_id
        except Exception as sess_err:
            logger.debug("[Auth] Failed to set supabase_user_id on session: %s", sess_err)


def _sync_supabase_user(
    request: HttpRequest | None,
    resp_data: dict,
    username: str | None,
) -> User | None:
    """
    Retrieve or create a Django User from a successful Supabase auth response.
    Promotes to superuser/staff if the email matches the ADMIN_EMAIL.
    Stores the Supabase user_id in the session.
    """
    from django.db import transaction

    user_info = resp_data.get("user", {})
    user_email = str(user_info.get("email") or username or "").strip()
    if not user_email:
        raise ValueError("Supabase identity has no email address; account sync is impossible.")
    django_username = generate_unique_username(user_email)

    with transaction.atomic():
        user, created = _get_or_create_django_user(user_email, django_username)
        if not user.is_active:
            # Django-side deactivation is the operator ban path; Supabase is
            # only the IdP, so an inactive account must not get a session.
            logger.warning("[Auth] Login rejected for deactivated account: %s", user_email)
            return None
        is_promoted_admin = _evaluate_admin_claim(user_email, user_info)

        role_changed = user.is_superuser != is_promoted_admin or user.is_staff != is_promoted_admin
        if role_changed and user.is_superuser and not is_promoted_admin:
            # Explicit demotion path: ADMIN_EMAIL is the sole authority, so a
            # superuser absent from it loses privileges on next login (AUD-295).
            logger.warning("[Auth] Demoting superuser %s: email not in ADMIN_EMAIL.", user_email)
        user.is_superuser = is_promoted_admin
        user.is_staff = is_promoted_admin

        if created or role_changed:
            user.set_unusable_password()
            user.save()
            logger.info("[Auth] Django sync account created/updated from Supabase claims: %s", user_email)
        else:
            logger.info("[Auth] Supabase user session established: %s", user_email)

        _attach_supabase_session(request, user_info)

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
            # Local password auth is a development/offline escape hatch only.
            # A partially or fully unconfigured production deployment must
            # fail closed: falling back to ModelBackend would bypass the IdP
            # and leave Turnstile tokens unverified (they are only checked
            # for presence, never validated, on the local path).
            if getattr(settings, "DEBUG", False) or getattr(settings, "SURREALDB_OFFLINE", False):
                return _fallback_local_auth()
            logger.warning(
                "[Auth] Supabase is not fully configured; refusing local password authentication in production."
            )
            return None

        if not is_email:
            # A configured Supabase deployment accepts email identities only.
            # Allowing username/password auth here would bypass the IdP entirely.
            return None

        import re

        if not re.fullmatch(r"[^@\s]+@[^@\s.]+\.[^@\s]+", target_email):
            logger.warning("[Auth] Supabase auth rejected: '%s' is not a valid email format.", target_email)
            return None

        return self._do_supabase_auth(request, target_email, password, (supabase_url, supabase_key))

    def _do_supabase_auth(
        self,
        request: HttpRequest | None,
        target_email: str,
        password: str | None = None,
        config: tuple[str, str] | None = None,
    ) -> User | None:
        if not password:
            return None

        supabase_url, supabase_key = (
            config
            if config
            else (
                getattr(settings, "SUPABASE_URL", ""),
                getattr(settings, "SUPABASE_PUBLIC_KEY", ""),
            )
        )
        captcha_token = request.POST.get("cf-turnstile-response", "") if request else ""
        if not _supabase_captcha_allows(request, captcha_token):
            return None
        body: dict = {"email": target_email, "password": password}
        if captcha_token:
            body["gotrue_meta_security"] = {"captcha_token": captcha_token}

        resp_data = _request_supabase_token(request, supabase_url, supabase_key, body)
        if resp_data is None:
            return None
        return _sync_supabase_user(request, resp_data, target_email)


def _supabase_captcha_allows(request: HttpRequest | None, captcha_token: str) -> bool:
    """Enforce the Turnstile pre-dispatch check and optional siteverify (AUD-296)."""
    turnstile_required = bool(getattr(settings, "CF_TURNSTILE_SITE_KEY", ""))
    if turnstile_required and not captcha_token:
        logger.warning("[Auth] Supabase authentication rejected before dispatch: security check incomplete.")
        return False
    if captcha_token and getattr(settings, "CF_TURNSTILE_SECRET_KEY", ""):
        # Server-side siteverify when configured; GoTrue captcha remains
        # the verifier otherwise (AUD-296).
        from extractor.view_auth import _verify_turnstile_token

        if not _verify_turnstile_token(captcha_token):
            logger.warning("[Auth] Supabase authentication rejected: CAPTCHA verification failed.")
            return False
    return True


def _mark_auth_unavailable(request: HttpRequest | None) -> None:
    """Flag IdP outages so the login form shows 'unavailable', not 'bad credentials' (AUD-392)."""
    if request is not None:
        request._supabase_auth_unavailable = True


def _request_supabase_token(
    request: HttpRequest | None, supabase_url: str, supabase_key: str, body: dict
) -> dict | None:
    """POST the password grant to GoTrue; returns the token response or None."""
    url = f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=password"
    app_ver = getattr(settings, "APP_VERSION", "unknown")
    headers = {
        "apikey": supabase_key,
        "Content-Type": APPLICATION_JSON,
        "User-Agent": f"AetherOmni-AuthClient/{app_ver}",
    }
    payload = json.dumps(body).encode()
    try:
        from extractor.utils import validate_url_scheme

        validate_url_scheme(url)
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning("[Auth] Supabase authentication rejected: HTTP %s", e.code)
        if e.code == 429 or e.code >= 500:
            # Rate limiting and upstream outages are transient, not bad
            # credentials — surface them as unavailable (AUD-417).
            _mark_auth_unavailable(request)
        return None
    except Exception as e:
        logger.warning("[Auth] Supabase API network connectivity exception: %s", e)
        _mark_auth_unavailable(request)
        return None
