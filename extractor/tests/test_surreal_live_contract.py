"""Live SurrealDB contract tests for production SurrealQL branches.

These tests exercise the real WebSocket RPC path against an ephemeral
in-memory SurrealDB instance, covering tenant fencing, transactions, and
conditional updates that the offline in-memory mirror cannot verify.

Opt-in: set ``SURREAL_LIVE_TEST=1`` and ensure the ``surreal`` CLI is on
PATH. The suite starts its own throwaway server on a random localhost port
and applies ``schema.surql``; it never touches a remote database.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess  # nosec B404
import time
import unittest
import uuid
from pathlib import Path

import httpx
from django.test import SimpleTestCase, override_settings

from extractor import surreal_db as sdb

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schema.surql"
LIVE_TEST_ENABLED = os.getenv("SURREAL_LIVE_TEST") == "1" and shutil.which("surreal") is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(http_url: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"surreal start exited early with code {proc.returncode}")
        try:
            if httpx.get(f"{http_url}/health", timeout=1.0).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for ephemeral SurrealDB to become healthy")


@unittest.skipUnless(LIVE_TEST_ENABLED, "Set SURREAL_LIVE_TEST=1 with the surreal CLI on PATH")
class SurrealLiveContractTests(SimpleTestCase):
    """Contract tests against a real SurrealDB engine (memory backend)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._port = _free_port()
        cls._http_url = f"http://127.0.0.1:{cls._port}"
        cls._proc = subprocess.Popen(  # nosec B603
            [
                "surreal",
                "start",
                "--log",
                "error",
                "--bind",
                f"127.0.0.1:{cls._port}",
                "--user",
                "root",
                "--pass",
                "root",
                "memory",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_health(cls._http_url, cls._proc)
        # surreal import requires `OPTION IMPORT;` (v3); apply the schema via
        # the /sql endpoint instead so DEFINE EVENT/FIELD side effects run.
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        response = httpx.post(
            f"{cls._http_url}/sql",
            content=schema_sql,
            headers={"surreal-ns": "korda", "surreal-db": "extractor"},
            auth=("root", "root"),
            timeout=60.0,
        )
        response.raise_for_status()
        cls._settings_override = override_settings(
            SURREALDB_OFFLINE=False,
            SURREAL_URL=f"ws://127.0.0.1:{cls._port}",
            SURREAL_USER="root",
            SURREAL_PASS="root",
            SURREAL_NS="korda",
            SURREAL_DB="extractor",
            DEBUG=True,
        )
        cls._settings_override.enable()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._settings_override.disable()
        sdb._shutdown_rpc_loop()
        cls._proc.terminate()
        try:
            cls._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls._proc.kill()
        super().tearDownClass()

    def setUp(self) -> None:
        sdb._detected_url = None
        sdb._detected_ns = None
        self.owner_a = f"owner-a-{uuid.uuid4().hex[:8]}"
        self.owner_b = f"owner-b-{uuid.uuid4().hex[:8]}"

    # ── helpers ──────────────────────────────────────────────────────────

    def _insert(self, table: str, fields: dict) -> None:
        assignments = ", ".join(f"{k}: ${k}" for k in fields)
        sql = f"CREATE {table} CONTENT {{ {assignments} }};"
        sdb._check_surreal_error(sdb._run(sql, fields))

    def _create_source(self, source_id: str, owner_id: str) -> None:
        self._insert(
            "source",
            {
                "source_id": source_id,
                "doc_slug": f"doc-{source_id}",
                "owner_id": owner_id,
                "sha256": f"sha-{source_id}",
            },
        )

    def _create_anchor(self, anchor_id: str, source_id: str, owner_id: str) -> None:
        self._insert(
            "anchor",
            {
                "anchor_id": anchor_id,
                "source_id": source_id,
                "doc_slug": f"doc-{source_id}",
                "owner_id": owner_id,
                "quote": f"verbatim quote for {anchor_id}",
            },
        )

    # ── tests ────────────────────────────────────────────────────────────

    def test_rate_limit_atomic_denies_after_quota(self) -> None:
        key = f"rl-{uuid.uuid4().hex}"
        self.assertTrue(sdb.check_rate_limit_atomic(key, max_requests=2, window_seconds=60))
        self.assertTrue(sdb.check_rate_limit_atomic(key, max_requests=2, window_seconds=60))
        self.assertFalse(sdb.check_rate_limit_atomic(key, max_requests=2, window_seconds=60))

    def test_upsert_source_rejects_cross_owner_write(self) -> None:
        source_id = f"src-{uuid.uuid4().hex[:8]}"
        sdb.upsert_source(
            source_id=source_id,
            doc_slug="doc-1",
            owner_id=self.owner_a,
            title="Doc",
            mime_type="application/pdf",
            gcs_uri="gs://bucket/doc.pdf",
            sha256="abc123",
        )
        with self.assertRaises(RuntimeError):
            sdb.upsert_source(
                source_id=source_id,
                doc_slug="doc-1",
                owner_id=self.owner_b,
                title="Hijack",
                mime_type="application/pdf",
                gcs_uri="gs://bucket/evil.pdf",
                sha256="def456",
            )

    def test_tombstone_source_owner_fencing(self) -> None:
        source_id = f"src-{uuid.uuid4().hex[:8]}"
        self._create_source(source_id, self.owner_a)
        self.assertFalse(sdb.tombstone_source(source_id, self.owner_b))
        self.assertTrue(sdb.tombstone_source(source_id, self.owner_a))
        rows = sdb._first_result(
            sdb._run("SELECT gcs_status, gcs_uri FROM source WHERE source_id = $sid;", {"sid": source_id})
        )
        self.assertEqual(rows[0]["gcs_status"], "purged")
        self.assertEqual(rows[0]["gcs_uri"], "")

    def test_crystal_provenance_verified_requires_owned_anchor(self) -> None:
        source_id = f"src-{uuid.uuid4().hex[:8]}"
        owned_anchor = f"anc-{uuid.uuid4().hex[:8]}"
        foreign_anchor = f"anc-{uuid.uuid4().hex[:8]}"
        self._create_source(source_id, self.owner_a)
        self._create_anchor(owned_anchor, source_id, self.owner_a)
        self._create_anchor(foreign_anchor, source_id, self.owner_b)

        # Verified status with no owned anchors must degrade to draft.
        draft_crystal = sdb.create_crystal_with_provenance(
            crystal_id=f"cry-{uuid.uuid4().hex[:8]}",
            owner_id=self.owner_a,
            doc_slug="doc-1",
            title="Unanchored",
            summary="No anchors",
            anchor_ids=[],
            status="verified",
        )
        self.assertEqual(draft_crystal["status"], "draft")

        # Owned + foreign anchors: only the owned one may be linked.
        crystal_id = f"cry-{uuid.uuid4().hex[:8]}"
        crystal = sdb.create_crystal_with_provenance(
            crystal_id=crystal_id,
            owner_id=self.owner_a,
            doc_slug="doc-1",
            title="Anchored",
            summary="Has anchors",
            anchor_ids=[owned_anchor, foreign_anchor],
            status="verified",
        )
        self.assertEqual(crystal["status"], "verified")
        edges = sdb._first_result(
            sdb._run(
                "SELECT out AS target FROM grounded_in WHERE in = type::record('crystal', $cid);",
                {"cid": crystal_id},
            )
        )
        linked_ids = [
            (raw.split("⟨")[-1].rstrip("⟩") if "⟨" in (raw := str(e["target"])) else raw.rsplit(":", 1)[-1])
            for e in edges
        ]
        self.assertEqual(linked_ids, [owned_anchor])

    def test_update_attempt_rating_owner_fencing(self) -> None:
        item_id = f"item-{uuid.uuid4().hex[:8]}"
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        attempt_id = f"att-{uuid.uuid4().hex[:8]}"
        self._insert(
            "exam_item",
            {
                "item_id": item_id,
                "owner_id": self.owner_a,
                "stem": "2+2?",
                "correct_answer": "4",
            },
        )
        self._insert(
            "attempt",
            {
                "attempt_id": attempt_id,
                "session_id": session_id,
                "item_id": item_id,
                "owner_id": self.owner_a,
                "is_correct": True,
                "rating": 3,
            },
        )
        self.assertFalse(
            sdb.update_attempt_rating(session_id=session_id, item_id=item_id, owner_id=self.owner_b, rating=4)
        )
        self.assertTrue(
            sdb.update_attempt_rating(session_id=session_id, item_id=item_id, owner_id=self.owner_a, rating=4)
        )
        rows = sdb._first_result(sdb._run("SELECT rating FROM attempt WHERE attempt_id = $aid;", {"aid": attempt_id}))
        self.assertEqual(rows[0]["rating"], 4)

    def test_get_anchors_by_ids_tenant_isolation(self) -> None:
        source_id = f"src-{uuid.uuid4().hex[:8]}"
        anchor_id = f"anc-{uuid.uuid4().hex[:8]}"
        self._create_source(source_id, self.owner_a)
        self._create_anchor(anchor_id, source_id, self.owner_a)
        self.assertEqual(sdb.get_anchors_by_ids([anchor_id], self.owner_b), {})
        result = sdb.get_anchors_by_ids([anchor_id], self.owner_a)
        self.assertIn(anchor_id, result)
        self.assertEqual(result[anchor_id]["gcs_status"], "active")


if __name__ == "__main__":
    unittest.main()
