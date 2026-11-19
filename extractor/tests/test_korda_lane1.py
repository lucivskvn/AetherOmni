import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from extractor import compiler, file_utils, llm_gateway, rag, task_handlers


class KordaLane1Tests(SimpleTestCase):
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
        bundles = compiler.compile_markdown_bundles(crystals, bundle_title="KORDA KB", max_words_per_bundle=12)
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
