"""Tests for the value-safe persisted-settings normalization utility."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "normalize_system_settings.py"
SPEC = importlib.util.spec_from_file_location("normalize_system_settings", SCRIPT_PATH)
assert SPEC and SPEC.loader
normalizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalizer)


class SystemSettingsNormalizerTests(SimpleTestCase):
    @patch.dict(os.environ, {"SURREAL_URL": "wss://surreal.example.test/rpc"}, clear=True)
    @patch.object(normalizer, "_run_sql", side_effect=httpx.ConnectError("unreachable"))
    def test_preflight_network_failure_is_value_safe(self, _run_sql):
        with patch.object(sys, "argv", ["normalize_system_settings.py"]), patch("builtins.print") as output:
            self.assertEqual(normalizer.main(), 1)

        self.assertEqual(
            output.call_args.args[0],
            "ERROR: Settings normalization preflight could not be completed; no field values were displayed.",
        )

    @patch.dict(os.environ, {"SURREAL_URL": "wss://surreal.example.test/rpc"}, clear=True)
    @patch.object(normalizer, "_run_sql")
    def test_apply_verifies_postconditions_without_printing_values(self, run_sql):
        run_sql.side_effect = [
            [{"result": [{"csrf_trusted_origins": None, "has_retired_field": True}]}],
            [{"result": []}],
            [{"result": [{"csrf_trusted_origins": "", "has_retired_field": False}]}],
        ]

        with patch.object(sys, "argv", ["normalize_system_settings.py", "--apply"]), patch("builtins.print") as output:
            self.assertEqual(normalizer.main(), 0)

        self.assertEqual(run_sql.call_count, 3)
        self.assertIn("BEGIN TRANSACTION", run_sql.call_args_list[1].args[3])
        self.assertIn("UNSET openrouter_api_key", run_sql.call_args_list[1].args[3])
        self.assertEqual(
            output.call_args.args[0], "Settings record normalized and verified; no field values were displayed."
        )

    @patch.dict(os.environ, {"SURREAL_URL": "wss://surreal.example.test/rpc"}, clear=True)
    @patch.object(normalizer, "_run_sql")
    def test_apply_fails_when_postconditions_remain_unmet(self, run_sql):
        run_sql.side_effect = [
            [{"result": [{"csrf_trusted_origins": "", "has_retired_field": True}]}],
            [{"result": []}],
            [{"result": [{"csrf_trusted_origins": "", "has_retired_field": True}]}],
        ]

        with patch.object(sys, "argv", ["normalize_system_settings.py", "--apply"]), patch("builtins.print") as output:
            self.assertEqual(normalizer.main(), 1)

        self.assertEqual(
            output.call_args.args[0],
            "ERROR: Settings normalization postcondition failed; no field values were displayed.",
        )
