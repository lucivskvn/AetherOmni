from unittest.mock import patch

from django.test import TestCase

from scripts import check_suppressions


class CheckSuppressionsTestCase(TestCase):
    """Tests for scripts/check_suppressions.py static analysis suppression validator."""

    @patch("scripts.check_suppressions.changed_lines")
    def test_valid_suppressions_pass(self, mock_changed):
        mock_changed.return_value = [
            ("file.py", 10, f"url = 'http://test'  #{'nosec'} B310"),
            (
                "file.py",
                11,
                f"res = requests.get(url)  #{'nosemgrep'}: python.lang.security.audit.req -- trusted internal endpoint",
            ),
            ("file.py", 12, f"val = eval(expr)  #{'NOSONAR'} python:S1523 -- sandboxed execution"),
        ]

        result = check_suppressions.main()
        self.assertEqual(result, 0)

    @patch("scripts.check_suppressions.changed_lines")
    def test_invalid_suppressions_fail(self, mock_changed):
        mock_changed.return_value = [
            ("file.py", 10, f"url = 'http://test'  #{'nosec'}"),
            (
                "file.py",
                11,
                f"res = requests.get(url)  #{'nosemgrep'}",
            ),
            ("file.py", 12, f"val = eval(expr)  #{'NOSONAR'}"),
        ]

        result = check_suppressions.main()
        self.assertEqual(result, 1)

    def test_parse_diff_added_lines(self):
        sample_diff = (
            "diff --git a/test.py b/test.py\n"
            "--- a/test.py\n"
            "+++ b/test.py\n"
            "@@ -5,3 +5,4 @@\n"
            " unchanged line\n"
            "+added line 1\n"
            "+added line 2\n"
            "-removed line\n"
        )
        lines = check_suppressions._parse_diff_added_lines(sample_diff)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], ("test.py", 6, "added line 1"))
        self.assertEqual(lines[1], ("test.py", 7, "added line 2"))

    @patch("scripts.check_suppressions.subprocess.run")
    def test_read_untracked_files(self, mock_subproc):
        from unittest.mock import MagicMock

        mock_subproc.return_value = MagicMock(stdout="README.md\n")
        lines = check_suppressions._read_untracked_files()
        self.assertIsInstance(lines, list)

    @patch("scripts.check_suppressions.GIT", None)
    def test_changed_lines_requires_git(self):
        with self.assertRaises(RuntimeError):
            check_suppressions.changed_lines()
