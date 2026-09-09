#!/usr/bin/env python3
"""Validate and, with explicit approval, normalize the SurrealDB settings record.

The command is dry-run by default. It never prints values from the settings
record, because a retired credential field may still contain sensitive data.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx


def _endpoint(url: str) -> str:
    normalized = url.strip().removesuffix("/rpc").rstrip("/")
    if normalized.startswith("wss://"):
        normalized = "https://" + normalized.removeprefix("wss://")
    elif not normalized.startswith("https://"):
        raise ValueError("SURREAL_URL must use wss:// or https://.")
    return f"{normalized}/sql"


def _run_sql(endpoint: str, namespace: str, database: str, query: str) -> list[dict]:
    username = os.environ.get("SURREAL_USER", "")
    password = os.environ.get("SURREAL_PASS", "")
    response = httpx.post(
        endpoint,
        content=query,
        headers={"Accept": "application/json", "NS": namespace, "DB": database},
        auth=(username, password) if username and password else None,
        timeout=30.0,
    )
    response.raise_for_status()
    result = response.json()
    if any(item.get("status") == "ERR" for item in result):
        raise RuntimeError("SurrealDB rejected the settings normalization query.")
    return result


def _settings_record(result: list[dict]) -> dict:
    rows = result[0].get("result", []) if result else []
    return rows[0] if rows else {}


def _read_normalization_status(endpoint: str, namespace: str, database: str) -> dict:
    """Read only type and presence status; never retrieve a retired credential value."""
    return _settings_record(
        _run_sql(
            endpoint,
            namespace,
            database,
            "SELECT csrf_trusted_origins, openrouter_api_key IS NOT NONE AS has_retired_field FROM system_settings:1;",
        )
    )


def _normalization_needed(record: dict) -> tuple[bool, bool]:
    if not record:
        raise RuntimeError("system_settings:1 is missing; initialize it before normalization.")
    has_retired_field = record.get("has_retired_field")
    if not isinstance(has_retired_field, bool):
        raise RuntimeError("Unable to determine whether the retired field is present.")
    return not isinstance(record.get("csrf_trusted_origins"), str), has_retired_field


def _normalization_error(stage: str) -> int:
    print(f"ERROR: Settings normalization {stage}; no field values were displayed.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the normalization after review.")
    parser.add_argument("--namespace", default=os.environ.get("SURREAL_NAMESPACE", "korda"))
    parser.add_argument("--database", default=os.environ.get("SURREAL_DATABASE", "extractor"))
    args = parser.parse_args()

    surreal_url = os.environ.get("SURREAL_URL", "").strip()
    if not surreal_url:
        print("ERROR: SURREAL_URL must be configured.", file=sys.stderr)
        return 2

    endpoint = _endpoint(surreal_url)
    try:
        needs_csrf_normalization, needs_key_purge = _normalization_needed(
            _read_normalization_status(endpoint, args.namespace, args.database)
        )
    except httpx.HTTPError, RuntimeError, ValueError:
        return _normalization_error("preflight could not be completed")
    if not args.apply:
        print(
            "Settings record is readable. "
            f"CSRF normalization required: {needs_csrf_normalization}; "
            f"retired-field purge required: {needs_key_purge}. Dry run only."
        )
        return 0

    statements = ["BEGIN TRANSACTION"]
    if needs_csrf_normalization:
        statements.append('UPDATE system_settings:1 SET csrf_trusted_origins = ""')
    if needs_key_purge:
        statements.append("UPDATE system_settings:1 UNSET openrouter_api_key")
    statements.append("COMMIT TRANSACTION")
    try:
        _run_sql(
            endpoint,
            args.namespace,
            args.database,
            "; ".join(statements) + ";",
        )
        still_needs_csrf_normalization, still_needs_key_purge = _normalization_needed(
            _read_normalization_status(endpoint, args.namespace, args.database)
        )
    except httpx.HTTPError, RuntimeError, ValueError:
        return _normalization_error("apply could not be verified")
    if still_needs_csrf_normalization or still_needs_key_purge:
        print("ERROR: Settings normalization postcondition failed; no field values were displayed.", file=sys.stderr)
        return 1
    print("Settings record normalized and verified; no field values were displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
