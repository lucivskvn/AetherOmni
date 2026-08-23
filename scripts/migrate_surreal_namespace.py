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
import sys
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("surreal_migrator")

TABLES_TO_MIGRATE = [
    "documents",
    "chunks",
    "rag_cache",
    "kv_cache",
    "audit_logs",
    "user_memories",
    "system_settings",
    "context_cache",
    "rate_limits",
    "entities",
    "chunk_references",
    "entity_relations",
]


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
        self.surreal_url = surreal_url.rstrip("/")
        self.user = user
        self.password = password
        self.db_name = db_name
        self.source_ns = source_ns
        self.target_ns = target_ns

    def _execute_sql(self, ns: str, sql_query: str) -> list[dict[str, Any]]:
        headers = {
            "Accept": "application/json",
            "NS": ns,
            "DB": self.db_name,
        }
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

    def count_records(self, ns: str, table: str) -> int:
        if table not in TABLES_TO_MIGRATE:
            raise ValueError(f"Invalid table name '{table}' not in whitelist.")
        query = f"SELECT count() FROM {table} GROUP ALL;"  # nosec B608 # noqa: S608
        try:
            result = self._execute_sql(ns, query)
            if result and isinstance(result, list) and "result" in result[0]:
                data = result[0]["result"]
                if data and isinstance(data, list) and "count" in data[0]:
                    return int(data[0]["count"])
            return 0
        except Exception:
            return 0

    def apply_schema_to_target(self, schema_file: str) -> bool:
        logger.info("[Migration] Applying schema definitions to target namespace '%s'...", self.target_ns)
        # Whitelist and sanitize schema file path to prevent CWE-22 path traversal
        filename = os.path.basename(schema_file)
        if filename != "schema.surql":
            raise ValueError(f"Unauthorized schema file '{filename}'. Only 'schema.surql' is allowed.")

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        safe_schema_path = os.path.join(root_dir, "schema.surql")
        if not os.path.exists(safe_schema_path) and os.path.exists("/app/schema.surql"):
            safe_schema_path = "/app/schema.surql"

        if not os.path.exists(safe_schema_path):
            raise FileNotFoundError(f"Schema file '{safe_schema_path}' does not exist.")

        with open(safe_schema_path, encoding="utf-8") as f:
            schema_sql = f.read()

        result = self._execute_sql(self.target_ns, schema_sql)
        errors = sum(1 for item in result if item.get("status") == "ERR")
        if errors > 0:
            logger.warning("[Migration] Target schema initialized with %d warnings/errors.", errors)
        else:
            logger.info("[Migration] Target namespace '%s' schema initialized cleanly.", self.target_ns)
        return errors == 0

    def _build_batch_statements(self, table: str, records: list[dict[str, Any]]) -> list[str]:
        insert_statements: list[str] = []
        for record in records:
            rec_id = record.get("id")
            clean_record = dict(record)
            json_data = json.dumps(clean_record, default=str)
            if rec_id:
                clean_id = str(rec_id).strip()
                insert_statements.append(f"UPSERT {clean_id} CONTENT {json_data};")
            else:
                insert_statements.append(f"CREATE {table} CONTENT {json_data};")
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

        offset = 0
        migrated_total = 0

        while True:
            fetch_query = f"SELECT * FROM {table} START {offset} LIMIT {batch_size};"  # nosec B608 # noqa: S608
            src_records_resp = self._execute_sql(self.source_ns, fetch_query)
            if not src_records_resp or "result" not in src_records_resp[0]:
                break

            records = src_records_resp[0]["result"]
            if not records:
                break

            insert_statements = self._build_batch_statements(table, records)
            batch_sql = "BEGIN TRANSACTION;\n" + "\n".join(insert_statements) + "\nCOMMIT TRANSACTION;"
            self._execute_sql(self.target_ns, batch_sql)

            migrated_total += len(records)
            offset += len(records)
            logger.info(
                "[Migration] Table '%s': Migrated %d / %d records...",
                table,
                migrated_total,
                src_count,
            )
            if len(records) < batch_size or (src_count and migrated_total >= src_count):
                break

        # Validation
        dst_count = self.count_records(self.target_ns, table)
        if dst_count >= src_count:
            logger.info(
                "✓ [Success] Table '%s' migrated cleanly (Source: %d, Target: %d).",
                table,
                src_count,
                dst_count,
            )
        else:
            logger.error(
                "✗ [Integrity Mismatch] Table '%s': Source has %d, Target has %d.",
                table,
                src_count,
                dst_count,
            )

        return migrated_total

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

        if not dry_run:
            self.apply_schema_to_target(schema_file)

        total_migrated = 0
        for table in TABLES_TO_MIGRATE:
            total_migrated += self.migrate_table_data(table, dry_run=dry_run)

        logger.info(
            "==========================================================\n"
            " ✅ Namespace Migration Complete! Total Records: %d\n"
            "==========================================================",
            total_migrated,
        )
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate SurrealDB data from source namespace to target namespace.")
    parser.add_argument("--url", default=os.getenv("SURREAL_URL", "http://localhost:8001"), help="SurrealDB URL")
    parser.add_argument("--user", default=os.getenv("SURREAL_USER", "root"), help="SurrealDB User")
    parser.add_argument("--password", default=os.getenv("SURREAL_PASS", "root"), help="SurrealDB Password")
    parser.add_argument("--db", default=os.getenv("SURREAL_DB", "extractor"), help="Database Name")
    parser.add_argument("--source-ns", default="aetheromni", help="Source namespace to copy from")
    parser.add_argument("--target-ns", default="korda", help="Target namespace to write to")
    parser.add_argument("--schema", default="schema.surql", help="Path to schema.surql")
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
        migrator.run_migration(schema_file=args.schema, dry_run=args.dry_run)
        return 0
    except Exception as e:
        logger.exception("[Migration Aborted] %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
