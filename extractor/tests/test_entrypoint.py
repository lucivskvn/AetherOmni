from __future__ import annotations

import os
from unittest.mock import patch

from django.test import SimpleTestCase

from scripts import entrypoint


class EntrypointTest(SimpleTestCase):
    @patch.dict(os.environ, {"DJANGO_DEBUG": "False"}, clear=False)
    def test_production_gunicorn_command_uses_warning_log_level(self):
        self.assertEqual(
            entrypoint._gunicorn_command(),
            [
                "gunicorn",
                "--bind",
                ":8080",
                "--workers",
                "2",
                "--threads",
                "4",
                "--timeout",
                "120",
                "--log-level",
                "warning",
                "core.wsgi:application",
            ],
        )

    @patch.dict(os.environ, {"DJANGO_DEBUG": "True"}, clear=False)
    def test_debug_gunicorn_command_omits_warning_log_level(self):
        self.assertNotIn("--log-level", entrypoint._gunicorn_command())

    @patch.dict(os.environ, {"GUNICORN_WORKERS": "1"}, clear=False)
    def test_gunicorn_worker_count_is_configurable(self):
        command = entrypoint._gunicorn_command()
        self.assertEqual(command[command.index("--workers") + 1], "1")

    @patch.dict(os.environ, {"GUNICORN_WORKERS": "unbounded"}, clear=False)
    def test_invalid_gunicorn_worker_count_uses_safe_default(self):
        command = entrypoint._gunicorn_command()
        self.assertEqual(command[command.index("--workers") + 1], "2")

    @patch("scripts.entrypoint.subprocess.run")
    def test_migrations_use_an_argument_list(self, mock_run):
        entrypoint._run_migrations()

        mock_run.assert_called_once_with(
            [entrypoint.sys.executable, "manage.py", "migrate", "--noinput"], check=True, timeout=120
        )

    @patch("scripts.entrypoint.subprocess.Popen")
    def test_initialization_uses_an_argument_list(self, mock_popen):
        entrypoint._start_database_initialization()

        mock_popen.assert_called_once_with([entrypoint.sys.executable, "init_surreal.py"])

    @patch("scripts.entrypoint.os.execvp")
    @patch("scripts.entrypoint._start_database_initialization")
    @patch("scripts.entrypoint._run_migrations")
    def test_main_executes_gunicorn_after_bootstrap(self, mock_migrate, mock_initialize, mock_exec):
        with self.assertRaises(SystemExit):
            mock_exec.side_effect = SystemExit
            entrypoint.main()

        mock_migrate.assert_called_once_with()
        mock_initialize.assert_called_once_with()
        mock_exec.assert_called_once_with("gunicorn", entrypoint._gunicorn_command())
