import json
import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from extractor import view_common
from extractor.utils import (
    APPLICATION_JSON,
    get_client_ip,
)

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)

TEMPLATE_REGISTER = "extractor/register.html"
TEMPLATE_FORGOT_PASSWORD = "extractor/forgot_password.html"  # nosec B105


def _verify_turnstile_token(captcha_token: str) -> bool:
    """Verify a Turnstile token against Cloudflare siteverify (AUD-296).

    Returns True when verification succeeds or no secret is configured (the
    presence check already ran and GoTrue captcha remains the verifier).
    Fails closed on transport/parse errors so a forged token cannot pass.
    """
    secret = getattr(settings, "CF_TURNSTILE_SECRET_KEY", "")
    if not secret:
        return True
    import urllib.parse
    import urllib.request

    try:
        data = urllib.parse.urlencode({"secret": secret, "response": captcha_token}).encode()
        req = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 nosemgrep
            result = json.loads(resp.read().decode("utf-8"))
        return bool(result.get("success"))
    except Exception as exc:
        logger.warning("[Auth] Turnstile siteverify request failed: %s", exc)
        return False


def _turnstile_token_error(request) -> str | None:
    """Return a user-facing error when Turnstile is configured but the token is missing/invalid."""
    if not getattr(settings, "CF_TURNSTILE_SITE_KEY", ""):
        return None
    captcha_token = request.POST.get("cf-turnstile-response", "").strip()
    if not captcha_token:
        return "CAPTCHA verification is required. Please complete the security check and try again."
    if not _verify_turnstile_token(captcha_token):
        return "CAPTCHA verification failed. Please complete the security check and try again."
    return None


_email_validator = EmailValidator(allowlist=None)


def _validate_email_format(email: str) -> bool:
    """Validate email address supporting standard, multi-label, and IDNA domains."""
    if not email or "@" not in email or len(email) > 254:
        return False
    try:
        _email_validator(email)
        return True
    except ValidationError:
        return False


def _validate_registration_inputs(email, password, confirm_password, supabase_url, supabase_key):
    """Returns an error message string or None if input is valid."""

    if not email or not password:
        return "Email and Password are required."

    if password != confirm_password:
        return "Passwords do not match."

    if not supabase_url or not supabase_key:
        return "Supabase integration is not configured. Local registration is disabled."

    # Deny the configured admin identity and the literal admin@ prefix. The
    # Supabase project hostname is not a mail domain, so checking email
    # domains against it is dead logic (AUD-275).
    admin_emails = {e.strip().lower() for e in (getattr(settings, "ADMIN_EMAIL", "") or "").split(",") if e.strip()}
    email_lower = email.lower()
    if email_lower.startswith("admin@") or email_lower in admin_emails:
        return "Registration of administrative or system email addresses is not permitted."

    if len(password) < 8:
        return "Password must be at least 8 characters long."

    if not _validate_email_format(email):
        return "Invalid email format."

    return None


def _call_supabase_auth_endpoint(
    endpoint: str,
    redirect_path: str,
    request_body: dict[str, Any],
    supabase_url: str,
    supabase_key: str,
    app_url: str,
    captcha_token: str | None,
    failure_prefix: str,
) -> tuple[bool, str | None]:
    """POST a CAPTCHA-protected Supabase Auth request and normalize its errors."""
    import urllib.parse
    import urllib.request

    from extractor.utils import validate_url_scheme

    redirect_url = urllib.parse.quote(app_url.rstrip("/") + redirect_path)
    url = f"{supabase_url.rstrip('/')}/auth/v1/{endpoint}?redirect_to={redirect_url}"
    try:
        validate_url_scheme(url)
        headers = {
            "apikey": supabase_key,
            "Content-Type": "application/json",
            "User-Agent": f"AetherOmni-AuthClient/{getattr(settings, 'APP_VERSION', 'unknown')}",
        }
        if captcha_token:
            request_body["gotrue_meta_security"] = {"captcha_token": captcha_token}
        payload = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5):  # nosec B310 nosemgrep
            return True, None
    except urllib.error.HTTPError as e:
        body_bytes = e.read().decode("utf-8", errors="replace")
        try:
            err_data = json.loads(body_bytes)
            error_fields = ("msg", "message", "error_description", "error")
            err_msg = next((err_data.get(field) for field in error_fields if err_data.get(field)), body_bytes)
        except json.JSONDecodeError, KeyError, AttributeError:
            err_msg = "Authentication provider returned an invalid response."
        return False, f"{failure_prefix}: {str(err_msg)[:255]}"
    except ValueError as e:
        logger.warning("[Supabase Auth] %s rejected request URL: %s", endpoint, e)
        return False, f"{failure_prefix}: the authentication service rejected the request."
    except Exception as e:
        logger.warning("[Supabase Auth] %s network error: %s", endpoint, e)
        return False, "A network error occurred. Please try again later."


def _register_supabase_user(
    supabase_url: str,
    supabase_key: str,
    email: str,
    password: str,
    app_url: str,
    captcha_token: str | None = None,
) -> tuple[bool, str | None]:
    """Make the Supabase signup API call. Returns (success: bool, error_msg: str | None)."""
    return _call_supabase_auth_endpoint(
        "signup",
        "/login",
        {"email": email, "password": password},
        supabase_url,
        supabase_key,
        app_url,
        captcha_token,
        "Supabase Signup Failed",
    )


def _auth_post_rate_limited(request, scope: str, max_requests: int = 5) -> bool:
    """Throttle anonymous credential endpoints by source IP (fails closed in production)."""
    key = f"{scope}:{get_client_ip(request)}"
    return not view_common._enforce_distributed_rate_limit(key, key, max_requests=max_requests, window_seconds=60)


@require_http_methods(["GET", "POST"])
def register_view(request):
    """
    Handles new user signups via Supabase Auth.
    """
    if request.method == "POST":
        if _auth_post_rate_limited(request, "register"):
            messages.error(request, "Too many attempts. Please wait a minute and try again.")
            return render(request, TEMPLATE_REGISTER)
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        confirm_password = request.POST.get("confirm_password", "")

        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        error_msg = _validate_registration_inputs(email, password, confirm_password, supabase_url, supabase_key)
        if error_msg:
            messages.error(request, error_msg)
            return render(request, TEMPLATE_REGISTER)

        turnstile_error = _turnstile_token_error(request)
        if turnstile_error:
            messages.error(request, turnstile_error)
            return render(request, TEMPLATE_REGISTER)

        app_url = getattr(settings, "APP_URL", "http://localhost:8000")
        captcha_token = request.POST.get("cf-turnstile-response", "")
        success, error_msg = _register_supabase_user(
            supabase_url, supabase_key, email, password, app_url, captcha_token
        )
        if success:
            messages.success(request, "Registration successful! Please check your email for the activation link.")
            return redirect("login")
        else:
            messages.error(request, error_msg)

    return render(request, TEMPLATE_REGISTER)


def _send_supabase_recovery(
    email: str,
    supabase_url: str,
    supabase_key: str,
    app_url: str,
    captcha_token: str | None = None,
) -> tuple[bool, str | None]:
    return _call_supabase_auth_endpoint(
        "recover",
        "/reset-password-confirm",
        {"email": email},
        supabase_url,
        supabase_key,
        app_url,
        captcha_token,
        "Supabase Recovery Failed",
    )


@require_http_methods(["GET", "POST"])
def forgot_password_view(request):
    """
    Dispatches a password recovery email via Supabase Auth.
    """
    if request.method == "POST":
        if _auth_post_rate_limited(request, "password_recovery"):
            messages.error(request, "Too many attempts. Please wait a minute and try again.")
            return render(request, TEMPLATE_FORGOT_PASSWORD)
        email = request.POST.get("email", "").strip()
        if not email:
            messages.error(request, "Email is required.")
            return render(request, TEMPLATE_FORGOT_PASSWORD)

        # Validate email format
        if not _validate_email_format(email):
            messages.error(request, "Invalid email format.")
            return render(request, TEMPLATE_FORGOT_PASSWORD)

        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        if not supabase_url or not supabase_key:
            messages.error(request, "Supabase integration is not configured.")
            return render(request, TEMPLATE_FORGOT_PASSWORD)

        turnstile_error = _turnstile_token_error(request)
        if turnstile_error:
            messages.error(request, turnstile_error)
            return render(request, TEMPLATE_FORGOT_PASSWORD)

        app_url = getattr(settings, "APP_URL", "http://localhost:8000")
        captcha_token = request.POST.get("cf-turnstile-response", "")
        success, error_msg = _send_supabase_recovery(email, supabase_url, supabase_key, app_url, captcha_token)
        if success:
            messages.success(request, "Password recovery link has been sent! Please check your email inbox.")
            return redirect("login")
        # Collapse provider errors: distinguishable responses enable
        # account enumeration on the recover endpoint (AUD-276).
        logger.warning("[Auth] Account recovery failed for submitted address: %s", error_msg)
        messages.error(
            request,
            "If an account exists for that address, a recovery link has been sent. Please check your email inbox.",
        )

    return render(request, TEMPLATE_FORGOT_PASSWORD)


@require_http_methods(["GET", "HEAD"])
@never_cache
def reset_password_confirm_view(request):
    """
    Renders the password update confirmation view.
    Actual update is handled via client-side Javascript using the saved recovery token.
    """
    supabase_url = getattr(settings, "SUPABASE_URL", "")
    supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

    context = {
        "supabase_url": supabase_url,
        "supabase_key": supabase_key,
    }
    return render(request, "extractor/reset_password_confirm.html", context)


def _fetch_supabase_user_info(supabase_url: str, supabase_key: str, access_token: str):
    """Validate an access token against Supabase /auth/v1/user.

    Returns (user_info, None) on success or (None, JsonResponse) on failure.
    4xx rejections → 401 (bad/expired token); 429/5xx → 503 so provider
    outages are not misreported as credential failures (AUD-417 parity).
    Other failures propagate to the caller's 500 handler.
    """
    import urllib.error
    import urllib.request

    from extractor.utils import validate_url_scheme

    url = f"{supabase_url.rstrip('/')}/auth/v1/user"
    validate_url_scheme(url)
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": APPLICATION_JSON,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            logger.warning("[Auth] Session exchange hit provider outage: HTTP %s", exc.code)
            return None, JsonResponse({"error": "Authentication service unavailable."}, status=503)
        logger.info("[Auth] Session exchange rejected by provider: HTTP %s", exc.code)
        return None, JsonResponse({"error": "Authentication failed."}, status=401)


@method_decorator(csrf_protect, name="dispatch")
class SupabaseSessionExchangeView(View):
    """
    Exchanges a client-side Supabase access_token for an authenticated Django session.
    Validates the token against Supabase /auth/v1/user, synchronizes the user via _sync_supabase_user,
    and establishes the Django session with request.session["supabase_user_id"].
    """

    def _rate_limited(self, request) -> bool:
        """Throttle anonymous token exchanges by source IP, failing closed in production."""
        key = f"supabase_session_exchange:{get_client_ip(request)}"
        return not view_common._enforce_distributed_rate_limit(key, key, max_requests=10, window_seconds=60)

    def post(self, request):
        import urllib.request

        from django.contrib.auth import login

        from extractor.auth import _sync_supabase_user

        if self._rate_limited(request):
            return JsonResponse({"error": "Too many authentication attempts. Please wait and try again."}, status=429)

        body, parse_err = view_common._parse_json_dict_body(request)
        if parse_err is not None:
            return parse_err

        try:
            access_token = body.get("access_token", "").strip() if isinstance(body, dict) else ""
            if not access_token:
                return JsonResponse({"error": "Missing access token."}, status=400)

            supabase_url = getattr(settings, "SUPABASE_URL", "").strip()
            supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "").strip()
            if not supabase_url or not supabase_key:
                return JsonResponse({"error": "Supabase integration not configured."}, status=503)

            user_info, fetch_err = _fetch_supabase_user_info(supabase_url, supabase_key, access_token)
            if fetch_err is not None:
                return fetch_err

            if getattr(settings, "SUPABASE_CONFIRM_EMAIL_REQUIRED", False):
                email_confirmed = bool(user_info.get("email_confirmed_at") or user_info.get("confirmed_at"))
                if not email_confirmed:
                    return JsonResponse({"error": "Email address not verified."}, status=403)

            user = _sync_supabase_user(request, {"user": user_info}, user_info.get("email"))
            if user is None:
                # Deactivated Django account — _sync_supabase_user rejects it.
                return JsonResponse({"error": "Authentication failed."}, status=401)
            user.backend = "extractor.auth.SupabaseAuthBackend"
            login(request, user)
            return JsonResponse({"status": "ok", "user": user.username})
        except urllib.error.HTTPError as exc:
            logger.info("[Auth] Session exchange rejected by provider: HTTP %s", exc.code)
            return JsonResponse({"error": "Authentication failed."}, status=401)
        except Exception as exc:
            logger.exception("[Auth] Supabase session exchange failed: %s", exc)
            return JsonResponse({"error": "Authentication service unavailable."}, status=500)
