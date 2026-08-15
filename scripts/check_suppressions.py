"""Reject newly introduced static-analysis suppressions without a rule identifier."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

# Safe: fixed Git-only subprocess calls below are audited individually.
ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")
RULES = (
    (
        re.compile(r"#\s*nosec\b", re.IGNORECASE),
        re.compile(r"#\s*nosec\s+B\d{3}(?:\s+B\d{3})*\b", re.IGNORECASE),
        "nosec B123",
    ),
    (
        re.compile(r"#\s*nosemgrep\b", re.IGNORECASE),
        re.compile(r"#\s*nosemgrep:\s*[^\s]+\s+--\s+\S+", re.IGNORECASE),
        "nosemgrep: rule-id -- reason",
    ),
    (re.compile(r"#\s*NOSONAR\b"), re.compile(r"#\s*NOSONAR\s+[^\s]+\s+--\s+\S+"), "NOSONAR rule-id -- reason"),
)


def _parse_diff_added_lines(diff: str) -> list[tuple[str, int, str]]:
    path = ""
    line_number = 0
    result: list[tuple[str, int, str]] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            line_number = int(match.group(1)) if match else 0
        elif line.startswith("+") and not line.startswith("+++"):
            result.append((path, line_number, line[1:]))
            line_number += 1
        elif not line.startswith("-"):
            line_number += 1
    return result


def _read_untracked_files() -> list[tuple[str, int, str]]:
    if GIT is None:
        return []
    untracked = subprocess.run(  # nosec B603
        [GIT, "ls-files", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout.splitlines()
    result: list[tuple[str, int, str]] = []
    for path in untracked:
        file_path = ROOT / path
        if file_path.is_file():
            result.extend(
                (path, index, line) for index, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1)
            )
    return result


def changed_lines() -> list[tuple[str, int, str]]:
    """Return added lines from tracked changes and all lines from untracked files."""
    if GIT is None:
        raise RuntimeError("Git is required to validate suppressions.")
    # Safe: GIT resolves the local executable and every argument is constant.
    diff = subprocess.run(  # nosec B603
        [GIT, "diff", "--unified=0", "HEAD"], cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout
    return _parse_diff_added_lines(diff) + _read_untracked_files()


def main() -> int:
    violations: list[str] = []
    for path, line_number, line in changed_lines():
        for marker, valid, example in RULES:
            if marker.search(line) and not valid.search(line):
                violations.append(f"{path}:{line_number}: suppression must use `{example}`")

    if violations:
        print(
            "New static-analysis suppressions require a precise rule ID; Semgrep and SonarQube suppressions also require a reason:",
            file=sys.stderr,
        )
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
