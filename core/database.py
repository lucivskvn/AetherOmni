"""Database configuration helpers for durable production persistence."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured


def database_config_from_url(database_url: str) -> dict[str, object]:
    """Build a Django PostgreSQL configuration from a validated connection URI."""
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must use a PostgreSQL URI.")
    if not parsed.hostname or not parsed.path or parsed.path == "/":
        raise ImproperlyConfigured("DATABASE_URL must include a host and database name.")

    raw_options = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
    allowed_option_keys = {
        "sslmode",
        "connect_timeout",
        "application_name",
        "channel_binding",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "target_session_attrs",
    }
    options = {k: v for k, v in raw_options.items() if k in allowed_option_keys}
    sslmode = options.get("sslmode", raw_options.get("sslmode", "require")).lower()
    if sslmode in {"disable", "allow", "prefer"}:
        raise ImproperlyConfigured("DATABASE_URL must require TLS (sslmode=require or stronger).")
    options["sslmode"] = sslmode
    port_val = parsed.port or 5432
    if not (1 <= port_val <= 65535):
        raise ImproperlyConfigured("DATABASE_URL port must be between 1 and 65535.")

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname,
        "PORT": str(port_val),
        "OPTIONS": options,
        # Supavisor transaction pooling cannot safely retain Django connections.
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "DISABLE_SERVER_SIDE_CURSORS": True,
    }
