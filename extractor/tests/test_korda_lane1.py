import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, TestCase, override_settings

from extractor import compiler, file_utils, llm_gateway, rag, task_handlers


class KordaLane1Tests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_build_gcs_prefix_path_ephemeral_and_latest(self):
        eph = file_utils.build_gcs_prefix_path("user-1", "legal-contract", "Contract v1.pdf", ephemeral=True)
        latest = file_utils.build_gcs_prefix_path("user-1", "legal-contract", "Contract v2.pdf", ephemeral=False)
        self.assertTrue(eph.startswith("raw/ephemeral/user-1/legal-contract/"))
        self.assertTrue(eph.endswith(".pdf"))
        self.assertEqual(latest, "raw/latest/user-1/legal-contract/contract-v2.pdf")

    def test_extract_anchors_from_gcs_uri_uses_part_from_uri_and_minimal_thinking(self):
        mock_part = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps(
            {
                "title": "Cloud Architecture",
                "anchors": [
                    {
                        "quote": "Scale to zero eliminates idle compute spend.",
                        "page_num": 4,
                        "section_heading": "2.1 Cost Model",
                        "start_sec": None,
                        "end_sec": None,
                        "figure_id": None,
                    }
                ],
            }
        )
        mock_response.usage_metadata = MagicMock(prompt_token_count=120, candidates_token_count=45)
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with (
            patch.object(llm_gateway, "_get_vertex_client", return_value=mock_client),
            patch.object(llm_gateway.types.Part, "from_uri", return_value=mock_part) as mock_from_uri,
        ):
            result = llm_gateway.extract_anchors_from_gcs_uri(
                "gs://proj-media-korda/raw/ephemeral/u1/doc/file.pdf",
                "application/pdf",
                dense_mode=False,
            )

        mock_from_uri.assert_called_once_with(
            file_uri="gs://proj-media-korda/raw/ephemeral/u1/doc/file.pdf",
            mime_type="application/pdf",
        )
        self.assertEqual(len(result["anchors"]), 1)
        self.assertEqual(result["anchors"][0]["page_num"], 4)
        self.assertEqual(result["anchors"][0]["section_heading"], "2.1 Cost Model")

    def test_strict_librarian_search_returns_verbatim_anchors_zero_llm(self):
        fake_anchors = [
            {
                "anchor_id": "anc-1",
                "source_id": "src-1",
                "doc_slug": "arch-spec",
                "quote": "Cloud Run minInstanceCount must be 0.",
                "page_num": 3,
                "section_heading": "3.1 Scaling",
                "bm25_rank": 1,
                "hnsw_rank": 2,
            }
        ]
        with (
            patch("extractor.surreal_db.search_anchors_hybrid", return_value=fake_anchors),
            patch("extractor.llm_gateway.generate_llm_content_unified") as mock_llm,
        ):
            res = rag.strict_librarian_search("minInstanceCount", owner_id="u1", query_embedding=[0.1] * 768)

        mock_llm.assert_not_called()
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["quote"], "Cloud Run minInstanceCount must be 0.")
        self.assertEqual(res["evidence"][0]["coord"], "p.3 | 3.1 Scaling")

    def test_markdown_compiler_emits_frontmatter_src_tags_and_splits_under_limit(self):
        crystals = [
            {
                "crystal_id": "cry-1",
                "doc_slug": "arch-spec",
                "title": "Serverless Scaling",
                "summary": "Cloud Run scales to zero when idle.",
                "status": "verified",
                "tags": ["cloud-run", "cost"],
                "anchors": [
                    {
                        "anchor_id": "anc-1",
                        "source_id": "src-1",
                        "quote": "Cloud Run minInstanceCount must be 0.",
                        "page_num": 3,
                        "section_heading": "3.1 Scaling",
                    }
                ],
            },
            {
                "crystal_id": "cry-2",
                "doc_slug": "arch-spec",
                "title": "Storage Lifecycle",
                "summary": "Ephemeral buckets disable soft delete.",
                "status": "verified",
                "tags": ["gcs"],
                "anchors": [
                    {
                        "anchor_id": "anc-2",
                        "source_id": "src-1",
                        "quote": "retentionDurationSeconds = 0.",
                        "page_num": 5,
                        "section_heading": "4.2 GCS",
                    }
                ],
            },
        ]
        bundles = compiler.compile_markdown_bundles(crystals, bundle_title="KORDA KB", max_words_per_bundle=40)
        self.assertGreaterEqual(len(bundles), 2)
        for bundle in bundles:
            self.assertTrue(bundle.startswith("---\n"))
            self.assertIn("[SRC: src-1 |", bundle)
            self.assertIn("> ", bundle)

    @override_settings(DEBUG=False)
    def test_eventarc_gcs_raw_view_ignores_non_raw_prefix_with_valid_oidc(self):
        payload = json.dumps({"bucket": "b", "name": "exports/out.md"}).encode("utf-8")
        request = self.factory.post("/internal/events/gcs-raw/", data=payload, content_type="application/json")
        view = task_handlers.EventarcGCSRawView()
        with patch.object(task_handlers, "_verify_oidc_token", return_value=True):
            resp = view.post(request)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["status"], "ignored")

    @override_settings(DEBUG=False, WORKER_URL="https://worker.example.run.app")
    def test_eventarc_gcs_raw_view_verifies_audience_and_digitalizes(self):
        payload = json.dumps(
            {
                "bucket": "korda-media",
                "name": "raw/ephemeral/user-1/arch-spec/doc.pdf",
                "contentType": "application/pdf",
            }
        ).encode("utf-8")
        request = self.factory.post("/internal/events/gcs-raw/", data=payload, content_type="application/json")
        view = task_handlers.EventarcGCSRawView()
        with (
            patch.object(task_handlers, "_verify_oidc_token", return_value=True) as mock_oidc,
            patch("extractor.surreal_db.upsert_source") as mock_upsert,
            patch(
                "extractor.llm_gateway.extract_anchors_from_gcs_uri",
                return_value={"title": "Arch Spec", "anchors": [{"quote": "Zero-copy URI."}]},
            ) as mock_extract,
            patch("extractor.surreal_db.create_anchors_with_provenance", return_value=1) as mock_create,
        ):
            resp = view.post(request)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["status"], "accepted")
        self.assertEqual(data["anchors_created"], 1)
        self.assertEqual(len(mock_oidc.call_args[0]), 2)
        mock_upsert.assert_called_once()
        mock_extract.assert_called_once()
        mock_create.assert_called_once()

    def test_eventarc_gcs_raw_view_unwraps_cloudevent_and_pubsub_envelopes(self):
        import base64
        import json

        from django.test import RequestFactory, override_settings

        from extractor import task_handlers

        rf = RequestFactory()
        inner = {
            "bucket": "test-bucket",
            "name": "raw/ephemeral/user1/slug1/doc.pdf",
            "contentType": "application/pdf",
        }
        ce_payload = {"type": "google.cloud.storage.object.v1.finalized", "data": inner}
        pubsub_payload = {"message": {"data": base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")}}

        with (
            override_settings(DEBUG=True, GS_BUCKET_NAME="test-bucket"),
            patch("extractor.llm_gateway.extract_anchors_from_gcs_uri", return_value={"anchors": []}),
            patch("extractor.surreal_db.upsert_source", return_value={"source_id": "user1:slug1"}),
        ):
            r1 = task_handlers.EventarcGCSRawView.as_view()(
                rf.post("/internal/events/gcs-raw/", data=json.dumps(ce_payload), content_type="application/json")
            )
            r2 = task_handlers.EventarcGCSRawView.as_view()(
                rf.post("/internal/events/gcs-raw/", data=json.dumps(pubsub_payload), content_type="application/json")
            )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(json.loads(r1.content)["status"], "accepted")
        self.assertEqual(json.loads(r2.content)["status"], "accepted")

    def test_surreal_check_error_and_last_non_null_result(self):
        from extractor import surreal_db

        with self.assertRaises(RuntimeError):
            surreal_db._check_surreal_error([{"status": "OK", "result": None}, {"status": "ERR", "detail": "boom"}])

        res = surreal_db._last_non_null_result(
            [
                {"status": "OK", "result": None},
                {"status": "OK", "result": {"live_spent": 1.5, "cap": 10.0}},
            ]
        )
        self.assertEqual(res, {"live_spent": 1.5, "cap": 10.0})

    def test_strict_librarian_and_crystal_and_compile_views_offline_roundtrip(self):
        import json
        from types import SimpleNamespace

        from django.test import RequestFactory, override_settings

        from extractor import surreal_db, views

        surreal_db._test_korda_sources.clear()
        surreal_db._test_korda_anchors.clear()
        surreal_db._test_korda_crystals.clear()

        rf = RequestFactory()
        user = SimpleNamespace(is_authenticated=True, id=42, username="u42")
        with override_settings(SURREALDB_OFFLINE=True):
            surreal_db.upsert_source(
                source_id="42:guide",
                doc_slug="guide",
                owner_id="42",
                title="Guide",
                mime_type="application/pdf",
                gcs_uri="gs://b/raw/latest/42/guide/g.pdf",
                sha256="abc",
            )
            surreal_db.create_anchors_with_provenance(
                source_id="42:guide",
                doc_slug="guide",
                owner_id="42",
                anchors=[{"anchor_id": "a1", "quote": "Zero-copy GCS URI extraction", "page_num": 2}],
            )
            with patch.object(views, "_enforce_distributed_rate_limit", return_value=True):
                req_lib = rf.post(
                    "/api/v1/librarian/",
                    data=json.dumps({"query": "Zero-copy GCS"}),
                    content_type="application/json",
                )
                req_lib.user = user
                lib_resp = views.StrictLibrarianAPIView.as_view()(req_lib)
                self.assertEqual(lib_resp.status_code, 200)
                self.assertGreaterEqual(len(json.loads(lib_resp.content)["evidence"]), 1)

                req_crys = rf.post(
                    "/api/v1/crystals/synthesize/",
                    data=json.dumps(
                        {
                            "title": "GCS Zero Copy",
                            "summary": "Avoids bytes copy.",
                            "doc_slug": "guide",
                            "anchor_ids": ["a1"],
                        }
                    ),
                    content_type="application/json",
                )
                req_crys.user = user
                crys_resp = views.CrystalSynthesizeAPIView.as_view()(req_crys)
                self.assertEqual(crys_resp.status_code, 200)

                req_comp = rf.get("/api/v1/compile-markdown/?download=1")
                req_comp.user = user
                comp_resp = views.MarkdownCompileAPIView.as_view()(req_comp)
                self.assertEqual(comp_resp.status_code, 200)
                self.assertEqual(comp_resp["X-Content-Type-Options"], "nosniff")
                self.assertIn("GCS Zero Copy", comp_resp.content.decode("utf-8"))

    def test_compiler_splits_oversized_single_crystal(self):
        from extractor.compiler import compile_markdown_bundles

        long_summary = " ".join(f"word{i}" for i in range(40))
        bundles = compile_markdown_bundles(
            [{"title": "Giant", "summary": long_summary, "doc_slug": "giant", "anchors": []}],
            max_words_per_bundle=15,
        )
        self.assertGreaterEqual(len(bundles), 3)

    def test_backup_surreal_rejects_insecure_url_and_empty_export(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with (
            patch.dict("os.environ", {"SURREAL_URL": "ws://insecure.example.com", "BACKUP_BUCKET_NAME": "b"}),
            self.assertRaises(CommandError),
        ):
            call_command("backup_surreal")

        mock_resp = MagicMock()
        mock_resp.iter_bytes.return_value = iter([b""])
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        mock_client = MagicMock()
        mock_client.stream.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with (
            patch("httpx.Client", return_value=mock_client),
            patch.dict(
                "os.environ",
                {
                    "SURREAL_URL": "wss://db.example.com",
                    "BACKUP_BUCKET_NAME": "b",
                    "SURREAL_USER": "u",
                    "SURREAL_PASS": "p",
                },
            ),
            self.assertRaises(CommandError),
        ):
            call_command("backup_surreal")

    def test_eventarc_digitalize_increments_version_and_records_spend(self):
        from django.test import override_settings

        from extractor import surreal_db, task_handlers

        surreal_db._test_korda_sources.clear()
        surreal_db._test_korda_anchors.clear()
        payload = {
            "bucket": "test-bucket",
            "name": "raw/latest/u99/spec/doc.pdf",
            "contentType": "application/pdf",
            "metadata": {"sha256": "sha-v2"},
        }
        with (
            override_settings(SURREALDB_OFFLINE=True),
            patch("extractor.llm_gateway.check_budget_and_api_limit", return_value=None),
            patch("extractor.task_handlers._record_korda_spend") as mock_spend,
            patch(
                "extractor.llm_gateway.extract_anchors_from_gcs_uri",
                return_value={
                    "anchors": [{"quote": "v1", "page_num": 1}],
                    "usage": {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.001},
                },
            ),
        ):
            r1 = task_handlers._digitalize_gcs_object(payload, "test-bucket", "raw/latest/u99/spec/doc.pdf")
            r2 = task_handlers._digitalize_gcs_object(payload, "test-bucket", "raw/latest/u99/spec/doc.pdf")
        self.assertEqual(r1["version"], 1)
        self.assertEqual(r2["version"], 2)
        self.assertEqual(mock_spend.call_count, 2)
