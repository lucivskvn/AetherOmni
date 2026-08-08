# Copyright (c) 2026 AetherOmni Contributors.
#
# This file is part of AetherOmni.
#
# AetherOmni is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# AetherOmni is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with AetherOmni.  If not, see <https://www.gnu.org/licenses/>.


import logging
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

    if is_loopback_referer:
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


class DynamicCsrfTrustedOriginsMiddleware:
    """
    Dynamically registers the request's origin and referer in CSRF_TRUSTED_ORIGINS
    during local development or proxy access to allow seamless access behind tunnels
    and custom proxy domains without manual configuration.
    """

    _base_origins = None
    _db_origins_loaded: bool = False
    _last_query_time: float = 0.0
    _cached_db_origins: tuple[str, ...] = ()

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_loopback(self, origin_str: str) -> bool:
        if not origin_str:
            return False
        origin_str = origin_str.strip().lower()
        if origin_str in ("localhost", "127.0.0.1", "::1"):
            return True
        try:
            url = origin_str if "://" in origin_str else f"http://{origin_str}"  # NOSONAR
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname
            if not host:
                return False
            # Strip IPv6 brackets if present (e.g. [::1])
            host = host.strip("[]")
            return host in ("localhost", "127.0.0.1", "::1")
        except Exception:
            return False

    def __call__(self, request):
        # 1. Capture base static origins from settings if not already cached
        if DynamicCsrfTrustedOriginsMiddleware._base_origins is None:
            DynamicCsrfTrustedOriginsMiddleware._base_origins = list(settings.CSRF_TRUSTED_ORIGINS)

        # 2. Reset the trusted origins to the base configuration to prevent stale whitelist buildup
        settings.CSRF_TRUSTED_ORIGINS = list(DynamicCsrfTrustedOriginsMiddleware._base_origins)

        # 3. Trust the host origin (derived from the current request Host header)
        # only in DEBUG mode to support local tunnels/dev servers.
        # Enforcing static/configured origins in production prevents CSRF bypass.
        if settings.DEBUG:
            self._trust_host_origin(request)

        # 4. Trust headers if DEBUG is enabled, or if they point to a local loopback interface
        self._trust_header_origins_if_safe(request)

        # 5. Load and append current database-configured origins
        self._trust_database_origins()

        return self.get_response(request)

    def _trust_database_origins(self):
        """Load CSRF trusted origins from the database with eventual consistency (60s cache).

        Queries database at most once every 60 seconds per process.
        Origins are merged into the shared settings list.
        """
        import time

        now = time.time()
        if not DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded or (
            now - DynamicCsrfTrustedOriginsMiddleware._last_query_time >= 60
        ):
            try:
                from extractor.models import SystemSettings

                settings_obj = SystemSettings.get_settings()
                db_origins = settings_obj.csrf_trusted_origins
                DynamicCsrfTrustedOriginsMiddleware._cached_db_origins = _parse_db_origins(db_origins)
                DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = True
                DynamicCsrfTrustedOriginsMiddleware._last_query_time = now
            except (AttributeError, RuntimeError, ValueError) as db_err:
                logger.debug("[Middleware] Could not load CSRF trusted origins from DB: %s", db_err)

        for origin in DynamicCsrfTrustedOriginsMiddleware._cached_db_origins:
            if origin not in settings.CSRF_TRUSTED_ORIGINS:
                settings.CSRF_TRUSTED_ORIGINS.append(origin)

    def _trust_host_origin(self, request):
        host = request.get_host()
        for scheme in ("http", "https"):
            host_origin = f"{scheme}://{host}"
            if host_origin not in settings.CSRF_TRUSTED_ORIGINS:
                settings.CSRF_TRUSTED_ORIGINS.append(host_origin)

    def _trust_origin_if_safe(self, origin):
        if origin and (settings.DEBUG or self._is_loopback(origin)) and origin not in settings.CSRF_TRUSTED_ORIGINS:
            settings.CSRF_TRUSTED_ORIGINS.append(origin)

    def _trust_header_origins_if_safe(self, request):
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
        allowed_names = ["password_change", "password_change_done", "logout"]
        allowed_paths = []
        from django.urls import NoReverseMatch, reverse

        for name in allowed_names:
            try:
                allowed_paths.append(reverse(name))
            except NoReverseMatch as rev_err:
                logger.debug("[Middleware] Could not resolve URL name '%s': %s", name, rev_err)
        return (
            any(path == p for p in allowed_paths) or path.startswith(("/static/", "/media/")) or "favicon.ico" in path
        )

    def __call__(self, request):
        if not request.user.is_authenticated:
            return self.get_response(request)

        path = request.path
        if not self._is_allowed_path(path):
            if "is_default_password" not in request.session:
                request.session["is_default_password"] = request.user.check_password("admin")

            if request.session.get("is_default_password"):
                from django.shortcuts import redirect

                return redirect("password_change")

        try:
            from django.urls import NoReverseMatch, reverse

            if path == reverse("password_change_done"):
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
