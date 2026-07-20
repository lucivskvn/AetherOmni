import unittest
from unittest.mock import MagicMock, patch

from scripts.update_docs import (
    _git,
    _replace_sentinel,
    compute_version,
    get_health_scores,
    get_major_minor,
    get_test_count,
    update_gcp_guide,
    update_readme,
    update_service_yamls,
)


class UpdateDocsTestCase(unittest.TestCase):

    @patch("scripts.update_docs.shutil.which", return_value=None)
    def test_git_no_executable(self, mock_which):
        self.assertEqual(_git("status"), "")

    @patch("scripts.update_docs.shutil.which", return_value="/usr/bin/git")
    @patch("scripts.update_docs.subprocess.run")
    def test_git_success(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(stdout="v1.0.0\n")
        self.assertEqual(_git("tag"), "v1.0.0")

    @patch("scripts.update_docs.shutil.which", return_value="/usr/bin/git")
    @patch("scripts.update_docs.subprocess.run", side_effect=OSError("Command failed"))
    def test_git_exception(self, mock_run, mock_which):
        self.assertEqual(_git("tag"), "")

    def test_get_major_minor(self):
        result = get_major_minor()
        self.assertTrue(isinstance(result, str))
        self.assertIn(".", result)

    @patch("scripts.update_docs._git")
    def test_compute_version(self, mock_git):
        mock_git.side_effect = lambda *args: {
            ("rev-list", "--count", "HEAD"): "100",
            ("rev-parse", "--short", "HEAD"): "abc1234",
            ("rev-parse", "HEAD"): "abc123456789",
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("status", "--porcelain"): "",
        }.get(args, "")
        info = compute_version()
        self.assertEqual(info["patch"], "100")
        self.assertEqual(info["sha"], "abc1234")
        self.assertFalse(info["dirty"])

    def test_get_test_count(self):
        count = get_test_count()
        self.assertTrue(isinstance(count, str))
        self.assertTrue(count.isdigit())

    def test_get_health_scores(self):
        scores = get_health_scores()
        self.assertIsInstance(scores, dict)

    def test_replace_sentinel(self):
        text = "<!-- auto:ver -->old<!-- /auto:ver -->"
        new_text, changed = _replace_sentinel(text, "ver", "new_content")
        self.assertTrue(changed)
        self.assertIn("new_content", new_text)

    @patch("scripts.update_docs.ROOT")
    def test_update_readme(self, mock_root):
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = (
            "<!-- auto:badges -->old badges<!-- /auto:badges -->\n"
            "Executes 100 comprehensive unit tests\n"
            "Current Objective/Mechanical Score: **90.0/100**\n"
            "Current Strict Code Health Score: **90.0/100**"
        )
        mock_root.__truediv__.return_value = mock_file

        info = {
            "release_ver": "v1.2.3+abc1234",
            "semver": "1.2.3",
            "badge_ver": "v1.2.3",
            "today": "2026-07-20",
            "sha": "abc1234",
        }
        changed = update_readme(info, "160", {"objective": "95.0", "strict": "98.0"})
        self.assertTrue(changed)

    @patch("scripts.update_docs.ROOT")
    def test_update_gcp_guide(self, mock_root):
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = "# Google Cloud Run Production Deployment Guide (Version 1.0.0)"
        mock_root.__truediv__.return_value = mock_file

        info = {"semver": "1.2.3"}
        changed = update_gcp_guide(info)
        self.assertTrue(changed)

    @patch("scripts.update_docs.ROOT")
    def test_update_service_yamls(self, mock_root):
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = '- name: RELEASE_VERSION\n  value: "old"'
        mock_root.__truediv__.return_value = mock_file

        info = {"release_ver": "v1.2.3+abc1234"}
        changed = update_service_yamls(info)
        self.assertTrue(changed)
