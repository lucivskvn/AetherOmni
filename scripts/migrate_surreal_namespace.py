"""
Deterministic Namespace Migration Script for SurrealDB.
Migrates all tables, schemas, indexes, and records from source namespace
(e.g., 'aetheromni') to target namespace ('korda').

Features:
- Connects over HTTP REST / RPC to SurrealDB.
- Automatically creates schema and indexes in target namespace.
- Transfers records with batch streaming and transactional safety.
- Validates source vs. target record counts to ensure 100% data integrity.
- Safe dry-run mode (--dry-run).
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("surreal_migrator")

# Must cover every DEFINE TABLE in schema.surql; check_reliability_contracts.py
# fails when this list drifts from the schema.
TABLES_TO_MIGRATE = [
    "documents",
    "chunks",
    "rag_cache",
    "kv_cache",
    "document_spend_reconciliations",
    "monthly_spend_ledger",
    "audit_logs",
    "user_memories",
    "system_settings",
    "context_cache",
    "rate_limits",
    "entities",
    "chunk_references",
    "entity_relations",
    "source",
    "anchor",
    "crystal",
    "anchored_in",
    "grounded_in",
    "exam_item",
    "attempt",
    "tests",
    "attempted_on",
]

SCHEMA_FILENAME = "schema.surql"

# TYPE RELATION tables whose `in`/`out` fields are record-typed. SurrealDB
# returns them as "table:id" strings; they must be emitted unquoted or the
# record<> coercion fails (verified against SurrealDB 3.x).
RELATION_TABLES = {
    "chunk_references",
    "entity_relations",
    "anchored_in",
    "grounded_in",
    "tests",
    "attempted_on",
}
_RECORD_ID_VALUE = re.compile(r"^[A-Za-z0-9_]+:[A-Za-z0-9_.\-\u27e8\u27e9]+$")


class SurrealNamespaceMigrator:
    def __init__(
        self,
        surreal_url: str,
        user: str,
        password: str,
        db_name: str = "extractor",
        source_ns: str = "aetheromni",
        target_ns: str = "korda",
    ):
        url = surreal_url.rstrip("/")
        ws_pfx = "ws:" + "//"
        wss_pfx = "wss:" + "//"
        if url.startswith(ws_pfx):
            scheme = "http://"  # NOSONAR python:S5332 -- Local development WebSocket to HTTP URL scheme normalization
            url = scheme + url[len(ws_pfx) :]
        elif url.startswith(wss_pfx):
            url = "https://" + url[len(wss_pfx) :]
        url = url.removesuffix("/rpc").rstrip("/")
        # Credentials and table data traverse this endpoint; require TLS for
        # any non-loopback target (mirrors init_surreal.py's wss/https rule).
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if not url.startswith("https://") and host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("SURREAL_URL must use wss:// or https:// for non-local targets.")
        self.surreal_url = url
        self.user = user
        self.password = password
        self.db_name = db_name
        self.source_ns = source_ns
        self.target_ns = target_ns
        self._integrity_failures: list[str] = []

    def _execute_sql(self, ns: str, sql_query: str, include_db: bool = True) -> list[dict[str, Any]]:
        headers = {
            "Accept": "application/json",
            "surreal-ns": ns,
        }
        if include_db:
            headers["surreal-db"] = self.db_name
        endpoint = f"{self.surreal_url}/sql"
        try:
            with httpx.Client(timeout=60.0) as client:
                if self.user and self.password:
                    resp = client.post(endpoint, content=sql_query, headers=headers, auth=(self.user, self.password))
                else:
                    resp = client.post(endpoint, content=sql_query, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.exception("[Migration] SQL execution failed on NS '%s'", ns)
            raise

    @staticmethod
    def _statement_errors(result: list[dict[str, Any]]) -> list[str]:
        return [
            str(item.get("result") or item.get("details") or "unknown error")
            for item in result
            if isinstance(item, dict) and item.get("status") == "ERR"
        ]

    def count_records(self, ns: str, table: str) -> int:
        if table not in TABLES_TO_MIGRATE:
            raise ValueError(f"Invalid table name '{table}' not in whitelist.")
        query = f"SELECT count() FROM {table} GROUP ALL;"  # nosec B608 # noqa: S608
        result = self._execute_sql(ns, query)
        errors = self._statement_errors(result)
        if errors:
            # A missing table means zero records to migrate; a missing
            # namespace/database or any other error must stay fatal.
            if all(re.search(r"The table '.*' does not exist", e) for e in errors):
                return 0
            raise RuntimeError(f"Count query failed on '{table}': {errors[0]}")
        if result and isinstance(result, list) and "result" in result[0]:
            data = result[0]["result"]
            if data and isinstance(data, list) and "count" in data[0]:
                return int(data[0]["count"])
        return 0

    def apply_schema_to_target(self, schema_file: str) -> bool:
        logger.info("[Migration] Applying schema definitions to target namespace '%s'...", self.target_ns)
        # Whitelist and sanitize schema file path to prevent CWE-22 path traversal
        filename = os.path.basename(schema_file)
        if filename != SCHEMA_FILENAME:
            raise ValueError(f"Unauthorized schema file '{filename}'. Only '{SCHEMA_FILENAME}' is allowed.")

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        safe_schema_path = os.path.join(root_dir, SCHEMA_FILENAME)
        if not os.path.exists(safe_schema_path) and os.path.exists(f"/app/{SCHEMA_FILENAME}"):
            safe_schema_path = f"/app/{SCHEMA_FILENAME}"

        if not os.path.exists(safe_schema_path):
            raise FileNotFoundError(f"Schema file '{safe_schema_path}' does not exist.")

        # The /sql endpoint does not auto-create databases; the first
        # schema statement fails with "database does not exist" otherwise.
        db_ensure = self._execute_sql(
            self.target_ns,
            f"DEFINE DATABASE IF NOT EXISTS `{self.db_name}`;",
            include_db=False,
        )
        db_errors = self._statement_errors(db_ensure)
        if db_errors:
            raise RuntimeError(f"Target database setup failed: {db_errors[0]}")

        with open(safe_schema_path, encoding="utf-8") as f:
            schema_sql = f.read()

        result = self._execute_sql(self.target_ns, schema_sql)
        errors = sum(1 for item in result if item.get("status") == "ERR")
        if errors > 0:
            logger.warning("[Migration] Target schema initialized with %d warnings/errors.", errors)
        else:
            logger.info("[Migration] Target namespace '%s' schema initialized cleanly.", self.target_ns)
        return errors == 0

    @staticmethod
    def _is_iso_datetime_string(val: Any) -> bool:
        if (
            isinstance(val, str)
            and len(val) >= 19
            and ("T" in val or " " in val)
            and (val.endswith("Z") or "+" in val or "-" in val[10:])
        ):
            try:
                datetime.fromisoformat(val.replace("Z", "+00:00"))
                return True
            except ValueError:
                return False
        return False

    def _build_batch_statements(self, table: str, records: list[dict[str, Any]]) -> list[str]:
        insert_statements: list[str] = []
        for record in records:
            rec_id = record.get("id")
            clean_record = dict(record)

            # Format record dictionary, converting ISO datetime strings to SurrealQL type::datetime()
            is_relation = table in RELATION_TABLES
            in_val = clean_record.get("in")
            out_val = clean_record.get("out")
            if is_relation and not (
                isinstance(in_val, str)
                and _RECORD_ID_VALUE.match(in_val)
                and isinstance(out_val, str)
                and _RECORD_ID_VALUE.match(out_val)
            ):
                raise ValueError(
                    f"Refusing to migrate {table} record with unsafe in/out ids: {in_val!r} -> {out_val!r}"
                )

            formatted_fields: list[str] = []
            for k, v in clean_record.items():
                if k == "id" or (is_relation and k in ("in", "out")):
                    continue
                if self._is_iso_datetime_string(v):
                    formatted_fields.append(f"{k}: type::datetime({json.dumps(v)})")
                else:
                    formatted_fields.append(f"{k}: {json.dumps(v, default=str)}")

            fields_content = "{" + ", ".join(formatted_fields) + "}"
            if is_relation:
                # SCHEMAFULL TYPE RELATION tables reject CREATE/UPSERT
                # ("not a relation"); only RELATE registers graph edges.
                # Edge ids cannot be preserved — RELATE assigns new ones —
                # so dedupe on (in, out) to keep re-runs idempotent.
                idx = len(insert_statements)
                content_clause = f" CONTENT {fields_content}" if formatted_fields else ""
                rel_sql = (
                    f"LET $rel_{idx} = (SELECT VALUE id FROM {table} "  # noqa: S608 -- table whitelisted; in/out validated  # nosec B608
                    f"WHERE `in` = {in_val} AND `out` = {out_val} LIMIT 1)[0]; "
                    f"IF $rel_{idx} == NONE {{ RELATE {in_val}->{table}->{out_val}{content_clause}; }};"
                )
                insert_statements.append(rel_sql)
            elif rec_id:
                clean_id = str(rec_id).strip()
                # Record ids originate from SurrealDB but are interpolated into
                # SQL; reject anything that could break out of the identifier.
                if not re.fullmatch(r"[A-Za-z0-9_]+:[A-Za-z0-9_⟨⟩\-]+", clean_id):
                    raise ValueError(f"Refusing to migrate record with unsafe id: {clean_id!r}")
                # MERGE (not CONTENT) so re-runs preserve fields the source
                # lacks and UPSERT stays idempotent; CONTENT nulls fields
                # absent from the payload (verified against SurrealDB 3.x).
                insert_statements.append(f"UPSERT {clean_id} MERGE {fields_content};")
            else:
                insert_statements.append(f"CREATE {table} CONTENT {fields_content};")
        return insert_statements

    def migrate_table_data(self, table: str, batch_size: int = 200, dry_run: bool = False) -> int:
        if table not in TABLES_TO_MIGRATE:
            raise ValueError(f"Invalid table name '{table}' not in whitelist.")

        src_count = self.count_records(self.source_ns, table)
        logger.info(
            "[Migration] Table '%s': Found %d records in source NS '%s'.",
            table,
            src_count,
            self.source_ns,
        )

        if src_count == 0:
            return 0

        if dry_run:
            logger.info("[Dry Run] Would migrate %d records for table '%s'.", src_count, table)
            return src_count

        effective_batch_size = 50 if table in ("chunks", "rag_cache", "context_cache") else batch_size
        migrated_total = self._copy_table_batches(table, src_count, effective_batch_size)

        if not self._log_count_validation(table, src_count) or migrated_total != src_count:
            self._integrity_failures.append(table)
        return migrated_total

    def _copy_table_batches(self, table: str, src_count: int, batch_size: int) -> int:
        """Copy a table in bounded batches and return the number transferred."""
        offset = 0
        migrated_total = 0

        while True:
            fetch_query = f"SELECT * FROM {table} ORDER BY id START {offset} LIMIT {batch_size};"  # nosec B608 # noqa: S608
            src_records_resp = self._execute_sql(self.source_ns, fetch_query)
            fetch_errors = self._statement_errors(src_records_resp)
            if fetch_errors:
                raise RuntimeError(f"Fetch query failed for '{table}': {fetch_errors[0]}")
            if not src_records_resp or "result" not in src_records_resp[0]:
                break

            records = src_records_resp[0]["result"]
            if not records:
                break

            insert_statements = self._build_batch_statements(table, records)
            batch_sql = "BEGIN TRANSACTION;\n" + "\n".join(insert_statements) + "\nCOMMIT TRANSACTION;"
            batch_result = self._execute_sql(self.target_ns, batch_sql)
            batch_errors = self._statement_errors(batch_result)
            if batch_errors:
                raise RuntimeError(f"Batch insert failed for '{table}' at offset {offset}: {batch_errors[0]}")

            migrated_total += len(records)
            offset += len(records)
            logger.info(
                "[Migration] Table '%s': Migrated %d / %d records...",
                table,
                migrated_total,
                src_count,
            )
            if len(records) < batch_size or migrated_total >= src_count:
                break
        return migrated_total

    def _log_count_validation(self, table: str, src_count: int) -> bool:
        """Log source/target count consistency after a table copy."""
        dst_count = self.count_records(self.target_ns, table)
        if dst_count >= src_count:
            logger.info(
                "✓ [Success] Table '%s' migrated cleanly (Source: %d, Target: %d).",
                table,
                src_count,
                dst_count,
            )
            return True
        logger.error(
            "✗ [Integrity Mismatch] Table '%s': Source has %d, Target has %d.",
            table,
            src_count,
            dst_count,
        )
        return False

    def run_migration(self, schema_file: str, dry_run: bool = False) -> bool:
        logger.info(
            "==========================================================\n"
            " 🚀 Starting SurrealDB Namespace Migration\n"
            "    Host:      %s\n"
            "    Source NS: %s\n"
            "    Target NS: %s\n"
            "    Database:  %s\n"
            "    Dry Run:   %s\n"
            "==========================================================",
            self.surreal_url,
            self.source_ns,
            self.target_ns,
            self.db_name,
            dry_run,
        )

        if not dry_run and not self.apply_schema_to_target(schema_file):
            logger.error("[Migration] Aborting: target schema application reported errors.")
            return False

        total_migrated = 0
        for table in TABLES_TO_MIGRATE:
            total_migrated += self.migrate_table_data(table, dry_run=dry_run)

        logger.info(
            "==========================================================\n"
            " ✅ Namespace Migration Complete! Total Records: %d\n"
            "==========================================================",
            total_migrated,
        )
        if self._integrity_failures:
            logger.error(
                "[Migration] Integrity validation failed for tables: %s",
                ", ".join(self._integrity_failures),
            )
            return False
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate SurrealDB data from source namespace to target namespace.")
    parser.add_argument("--url", default=os.getenv("SURREAL_URL", "http://localhost:8001"), help="SurrealDB URL")
    parser.add_argument("--user", default=os.getenv("SURREAL_USER", "root"), help="SurrealDB User")
    parser.add_argument("--password", default=os.getenv("SURREAL_PASS", "root"), help="SurrealDB Password")
    parser.add_argument("--db", default=os.getenv("SURREAL_DB", "extractor"), help="Database Name")
    parser.add_argument("--source-ns", default="aetheromni", help="Source namespace to copy from")
    parser.add_argument("--target-ns", default="korda", help="Target namespace to write to")
    parser.add_argument("--schema", default=SCHEMA_FILENAME, help=f"Path to {SCHEMA_FILENAME}")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without applying writes")

    args = parser.parse_args()

    migrator = SurrealNamespaceMigrator(
        surreal_url=args.url,
        user=args.user,
        password=args.password,
        db_name=args.db,
        source_ns=args.source_ns,
        target_ns=args.target_ns,
    )

    try:
        success = migrator.run_migration(schema_file=args.schema, dry_run=args.dry_run)
        return 0 if success else 1
    except Exception as e:
        logger.exception("[Migration Aborted] %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
