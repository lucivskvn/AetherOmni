# Copyright (c) 2026 KORDA Contributors.
#
# This file is part of KORDA.
#
# KORDA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# KORDA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with KORDA.  If not, see <https://www.gnu.org/licenses/>.


import logging
import re
import urllib.parse

from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware
from django.utils.functional import cached_property

logger = logging.getLogger(__name__)

# Patch CsrfViewMiddleware to make its cached properties dynamic.
# This is necessary because settings.CSRF_TRUSTED_ORIGINS can be modified dynamically
# by DynamicCsrfTrustedOriginsMiddleware, but CsrfViewMiddleware caches
# 'csrf_trusted_origins_hosts', 'allowed_origins_exact', and 'allowed_origin_subdomains'
# on the first request, ignoring any future dynamic updates.
for prop_name in ["csrf_trusted_origins_hosts", "allowed_origins_exact", "allowed_origin_subdomains"]:
    prop = getattr(CsrfViewMiddleware, prop_name, None)
    if prop and isinstance(prop, cached_property):
        setattr(CsrfViewMiddleware, prop_name, property(prop.func))


# Monkeypatch CsrfViewMiddleware.process_view to allow insecure HTTP referrers
# for local loopback connections (e.g. localhost:8080) when running behind SSL proxies.
# Django's CsrfViewMiddleware enforces a strict referer.scheme == "https" check
# for secure requests, causing 403 errors when local developers access the app over HTTP.
_orig_process_view = CsrfViewMiddleware.process_view


def _patched_process_view(self, request, callback, callback_args, callback_kwargs):
    referer = request.META.get("HTTP_REFERER")
    is_loopback_referer = False
    if referer:
        try:
            parsed = urllib.parse.urlsplit(referer)
            host = parsed.hostname
            if host:
                host = host.strip("[]").lower()
                if host in ("localhost", "127.0.0.1", "::1"):
                    is_loopback_referer = True
        except (ValueError, TypeError, AttributeError) as val_err:
            logger.debug("[CSRF Middleware] Could not split referer URL '%s': %s", referer, val_err)
            is_loopback_referer = False

    # Only bypass the HTTPS referer check for loopback referers in DEBUG mode.
    # In production the referer scheme check must stay enforced: the bypass
    # exists for local development behind SSL-terminating proxies.
    if settings.DEBUG and is_loopback_referer:
        orig_is_secure = request.is_secure
        request.is_secure = lambda: False
        try:
            return _orig_process_view(self, request, callback, callback_args, callback_kwargs)
        finally:
            request.is_secure = orig_is_secure
    else:
        return _orig_process_view(self, request, callback, callback_args, callback_kwargs)


CsrfViewMiddleware.process_view = _patched_process_view


def _parse_db_origins(db_origins):
    if not db_origins:
        return ()
    parsed = []
    for origin in db_origins.split(","):
        clean_origin = origin.strip()
        if clean_origin:
            parsed.append(clean_origin)
    return tuple(parsed)


def _is_loopback_origin(origin_str: str) -> bool:
    if not origin_str:
        return False
    clean = origin_str.strip().lower()
    if clean in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        url = clean if "://" in clean else f"https://{clean}"
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").strip("[]").lower()
        return host in ("localhost", "127.0.0.1", "::1")
    except Exception:
        return False


def _extract_host_from_origin(origin: str) -> str | None:
    try:
        url = origin if "://" in origin else f"https://{origin}"
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc or parsed.path
        if host and "[" in host and "]" in host:
            host = host.split("]", 1)[0].lstrip("[")
        elif host and ":" in host:
            host = host.split(":", 1)[0]
        if host and host.startswith("*."):
            # Django ALLOWED_HOSTS uses leading dot (.example.com) for wildcard subdomains
            host = host[1:]
        return host or None
    except Exception:
        logger.debug("[Middleware] Could not parse host from origin")
        return None


# A leading dot is valid: wildcard DB origins ("https://*.example.com") are
# normalized to ".example.com" by _extract_host_from_origin, which is exactly
# the ALLOWED_HOSTS wildcard form (AUD-426).
_VALID_HOST_RE = re.compile(
    r"^(\*\.)?\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$|^\[?[0-9a-fA-F:]+\]?$"
)


def _is_safe_db_host(host: str) -> bool:
    """Reject wildcard/malformed hosts derived from DB-stored CSRF origins."""
    if not host or host == "*" or ".." in host or "/" in host or "\\" in host:
        return False
    return bool(_VALID_HOST_RE.match(host.lower()))


def _load_db_origins_tuple() -> tuple[str, ...]:
    try:
        from extractor.models import SystemSettings

        settings_obj = SystemSettings.get_settings()
        return _parse_db_origins(settings_obj.csrf_trusted_origins)
    except (AttributeError, RuntimeError, ValueError) as db_err:
        logger.debug("[Middleware] Could not load CSRF trusted origins from DB: %s", db_err)
        return ()


class DynamicCsrfTrustedOriginsMiddleware:
    """
    Dynamically registers the request's origin and referer in CSRF_TRUSTED_ORIGINS
    during local development or proxy access to allow seamless access behind tunnels
    and custom proxy domains without manual configuration.
    """

    _base_origins = None
    _base_allowed_hosts = None
    _db_allowed_hosts: set = set()
    _db_origins_loaded: bool = False
    _last_query_time: float = 0.0
    _cached_db_origins: tuple[str, ...] = ()

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_loopback(self, origin_str: str) -> bool:
        return _is_loopback_origin(origin_str)

    def __call__(self, request):
        # 1. Capture base static origins from settings if not already cached
        if DynamicCsrfTrustedOriginsMiddleware._base_origins is None:
            DynamicCsrfTrustedOriginsMiddleware._base_origins = list(settings.CSRF_TRUSTED_ORIGINS)
        if DynamicCsrfTrustedOriginsMiddleware._base_allowed_hosts is None:
            DynamicCsrfTrustedOriginsMiddleware._base_allowed_hosts = list(settings.ALLOWED_HOSTS)

        # 2. Reset the trusted origins to the base configuration to prevent stale whitelist buildup
        settings.CSRF_TRUSTED_ORIGINS = list(DynamicCsrfTrustedOriginsMiddleware._base_origins)

        # 3. Load and append current database-configured origins BEFORE any
        # get_host() call: host validation must already see DB-derived hosts,
        # and the 60s cache makes reloads cheap.
        self._trust_database_origins()

        # 4. Trust the host origin (derived from the current request Host header)
        # only in DEBUG mode to support local tunnels/dev servers.
        # Enforcing static/configured origins in production prevents CSRF bypass.
        if settings.DEBUG:
            self._trust_host_origin(request)

        # 5. Trust headers if DEBUG is enabled, or if they point to a local loopback interface
        self._trust_header_origins_if_safe(request)

        return self.get_response(request)

    def _trust_database_origins(self):
        """Load CSRF trusted origins and sync allowed hosts from the database with eventual consistency (60s cache)."""
        import time

        now = time.time()
        should_reload = not DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded or (
            now - DynamicCsrfTrustedOriginsMiddleware._last_query_time >= 60
        )
        if should_reload:
            DynamicCsrfTrustedOriginsMiddleware._cached_db_origins = _load_db_origins_tuple()
            DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = True
            DynamicCsrfTrustedOriginsMiddleware._last_query_time = now

        db_hosts = set()
        for origin in DynamicCsrfTrustedOriginsMiddleware._cached_db_origins:
            if origin not in settings.CSRF_TRUSTED_ORIGINS:
                settings.CSRF_TRUSTED_ORIGINS.append(origin)
            origin_host = _extract_host_from_origin(origin)
            if origin_host and _is_safe_db_host(origin_host):
                db_hosts.add(origin_host)
            elif origin_host:
                logger.warning("[Middleware] Ignoring unsafe DB-derived host: %r", origin_host)

        # Reconcile ALLOWED_HOSTS: drop hosts previously appended from DB
        # origins that are no longer configured, without touching static or
        # test-overridden entries. Previously this list only ever grew, so a
        # removed origin stayed a valid Host header target until restart.
        base_hosts = DynamicCsrfTrustedOriginsMiddleware._base_allowed_hosts or []
        previous_db_hosts = DynamicCsrfTrustedOriginsMiddleware._db_allowed_hosts
        settings.ALLOWED_HOSTS = [
            host
            for host in settings.ALLOWED_HOSTS
            if host not in previous_db_hosts or host in base_hosts or host in db_hosts
        ]
        for host in db_hosts:
            if host not in settings.ALLOWED_HOSTS:
                settings.ALLOWED_HOSTS.append(host)
        DynamicCsrfTrustedOriginsMiddleware._db_allowed_hosts = db_hosts

    def _trust_host_origin(self, request):
        host = request.get_host()
        for scheme in ("http", "https"):
            host_origin = f"{scheme}://{host}"
            if host_origin not in settings.CSRF_TRUSTED_ORIGINS:
                settings.CSRF_TRUSTED_ORIGINS.append(host_origin)

    def _trust_origin_if_safe(self, origin):
        if origin and (settings.DEBUG or self._is_loopback(origin)) and origin not in settings.CSRF_TRUSTED_ORIGINS:
            settings.CSRF_TRUSTED_ORIGINS.append(origin)

    def _request_host_is_loopback(self, request) -> bool:
        try:
            return _is_loopback_origin(request.get_host())
        except Exception:
            return False

    def _trust_header_origins_if_safe(self, request):
        # Header-derived origins are trusted only in DEBUG mode or when the
        # request itself targets a loopback host. Otherwise remote clients
        # could grow CSRF_TRUSTED_ORIGINS with arbitrary loopback origin
        # strings (unbounded per-process list mutation).
        if not settings.DEBUG and not self._request_host_is_loopback(request):
            return
        # Origin header
        origin = request.META.get("HTTP_ORIGIN")
        self._trust_origin_if_safe(origin)

        # Referer header
        referer = request.META.get("HTTP_REFERER")
        if referer:
            try:
                parsed = urllib.parse.urlparse(referer)
                if parsed.scheme and parsed.netloc:
                    ref_origin = f"{parsed.scheme}://{parsed.netloc}"
                    self._trust_origin_if_safe(ref_origin)
            except (ValueError, AttributeError) as parse_err:
                logger.debug("[Middleware] Could not parse referer header for CSRF trust: %s", parse_err)


class ForcePasswordChangeMiddleware:
    """
    Forces any logged-in user with the default password 'admin' to change their password
    before they can access any other page (except password change, password change done, logout, and static files).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_allowed_path(self, path):
        from extractor.utils import ROUTE_PW_CHANGE_DONE

        allowed_names = ["password_change", ROUTE_PW_CHANGE_DONE, "logout", "release_metadata"]
        allowed_paths = []
        from django.urls import NoReverseMatch, reverse

        for name in allowed_names:
            try:
                allowed_paths.append(reverse(name))
            except NoReverseMatch as rev_err:
                logger.debug("[Middleware] Could not resolve URL name '%s': %s", name, rev_err)
        return (
            any(path == p for p in allowed_paths)
            or path.startswith(("/static/", "/media/", "/internal/tasks/"))
            or path in ("/healthz", "/release/")
            or "favicon.ico" in path
        )

    def __call__(self, request):
        if not getattr(request, "session", None) or not request.user.is_authenticated:
            return self.get_response(request)

        path = request.path
        if not self._is_allowed_path(path):
            if "is_default_password" not in request.session:
                request.session["is_default_password"] = request.user.check_password("admin")

            if request.session.get("is_default_password"):
                from extractor.utils import HEADER_X_REQUESTED_WITH, VALUE_XML_HTTP_REQUEST

                if (
                    request.headers.get(HEADER_X_REQUESTED_WITH) == VALUE_XML_HTTP_REQUEST
                    or request.headers.get("accept", "").startswith("application/json")
                    or path.startswith("/api/")
                ):
                    from django.http import JsonResponse

                    return JsonResponse({"error": "Default password change required."}, status=403)
                from django.shortcuts import redirect

                return redirect("password_change")

        try:
            from django.urls import NoReverseMatch, reverse

            from extractor.utils import ROUTE_PW_CHANGE_DONE

            if path == reverse(ROUTE_PW_CHANGE_DONE):
                # Re-verify the password to prevent bypassing the change
                # by resetting back to the default 'admin' password.
                still_default = request.user.check_password("admin")
                request.session["is_default_password"] = still_default
                if still_default:
                    # User reset their password back to 'admin' — redirect again!
                    return redirect("password_change")
        except (NoReverseMatch, AttributeError) as rev_err:
            logger.debug("[Middleware] Could not resolve identity reset finished URL: %s", rev_err)

        return self.get_response(request)
