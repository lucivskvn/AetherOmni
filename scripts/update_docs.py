#!/usr/bin/env python3
"""
AetherOmni — Automatic Documentation Updater
=============================================
Reads live metadata from the repository and rewrites dynamic sections in
README.md and gcp_deployment_guide.md so they are never out of date.

Sections managed automatically (wrapped in <!-- auto:* --> sentinels):
  - version badge + last-updated badge
  - deployment RELEASE_VERSION stamp
  - test count (parsed from Django test runner output if available)

Run locally:
    python scripts/update_docs.py

Run in CI (GitHub Actions):
    python scripts/update_docs.py --ci
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _git(*args: str) -> str:
    """Run a git command and return stripped stdout, or '' on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_version() -> str:
    """Return semver from VERSION file."""
    vf = ROOT / "VERSION"
    if vf.exists():
        return vf.read_text(encoding="utf-8").strip()
    return "0.0.0"


def get_commit_sha() -> str:
    return _git("rev-parse", "--short", "HEAD") or "unknown"


def get_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD") or "current"


def get_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def get_release_stamp() -> str:
    """Produces a YYYYMMDDHHmmSS stamp used as RELEASE_VERSION in service.yaml."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def get_commit_count() -> str:
    return _git("rev-list", "--count", "HEAD") or "?"


def get_test_count() -> str:
    """
    Try to parse the number of Django unit tests from the last test output.
    Falls back to reading the existing README value if tests are not run.
    """
    # Look for a cached test-count file written by run_checks.sh
    cache = ROOT / ".test_count"
    if cache.exists():
        val = cache.read_text(encoding="utf-8").strip()
        if val.isdigit():
            return val
    return ""  # leave unchanged when unknown


def get_health_scores() -> dict[str, str]:
    """
    Parse last desloppify scan output from .desloppify/score_cache.json if present.
    """
    score_file = ROOT / ".desloppify" / "score_cache.json"
    if score_file.exists():
        try:
            import json
            data = json.loads(score_file.read_text(encoding="utf-8"))
            return {
                "objective": str(round(data.get("objective", 0), 1)),
                "strict": str(round(data.get("strict", 0), 1)),
            }
        except Exception:
            pass
    return {}


# ── Section rewriter ───────────────────────────────────────────────────────────

def _replace_sentinel(text: str, key: str, new_content: str) -> tuple[str, bool]:
    """
    Replace content between <!-- auto:KEY --> and <!-- /auto:KEY --> sentinels.
    Returns (updated_text, changed_bool).
    """
    pattern = re.compile(
        rf"(<!-- auto:{re.escape(key)} -->)(.*?)(<!-- /auto:{re.escape(key)} -->)",
        re.DOTALL,
    )
    replacement = rf"\g<1>\n{new_content}\n\g<3>"
    new_text, count = pattern.subn(replacement, text)
    return new_text, count > 0


# ── README update ──────────────────────────────────────────────────────────────

def update_readme(version: str, today: str, sha: str, test_count: str, scores: dict) -> bool:
    """Update README.md dynamic sections. Returns True if the file was modified."""
    readme = ROOT / "README.md"
    if not readme.exists():
        print("[WARN] README.md not found, skipping.")
        return False

    original = readme.read_text(encoding="utf-8")
    text = original

    # 1. Version + last-updated badges block
    badge_block = (
        f'[![Version](https://img.shields.io/badge/version-v{version}-blue.svg)]'
        f'(https://github.com/lucivskvn/AetherOmni)\n'
        f'[![Last Updated](https://img.shields.io/badge/last%20updated-'
        f'{today.replace("-", "--")}-green.svg)](#)\n'
        f'[![Commit](https://img.shields.io/badge/commit-{sha}-lightgrey.svg)](#)'
    )

    # Replace sentinel block if present; otherwise replace the two existing badge lines
    text, replaced = _replace_sentinel(text, "badges", badge_block)
    if not replaced:
        # Patch in-place badges (no sentinel)
        text = re.sub(
            r'\[!\[Version\]\(https://img\.shields\.io/badge/version-[^\)]+\)\]\([^\)]*\)',
            f'[![Version](https://img.shields.io/badge/version-v{version}-blue.svg)]'
            f'(https://github.com/lucivskvn/AetherOmni)',
            text,
        )
        text = re.sub(
            r'\[!\[Last Updated\]\(https://img\.shields\.io/badge/last%20updated-[^\)]+\)\]\([^\)]*\)',
            f'[![Last Updated](https://img.shields.io/badge/last%20updated-'
            f'{today.replace("-", "--")}-green.svg)](#)',
            text,
        )
        # Add commit badge after last-updated if not already there
        if "Commit" not in text:
            text = re.sub(
                r'(\[!\[Last Updated\].*?\]\(#\))',
                r'\1\n'
                f'[![Commit](https://img.shields.io/badge/commit-{sha}-lightgrey.svg)](#)',
                text,
            )

    # 2. Test count in QA gates section
    if test_count:
        text = re.sub(
            r'(Executes\s+)\d+(\s+comprehensive unit tests)',
            rf'\g<1>{test_count}\g<2>',
            text,
        )

    # 3. Desloppify scores
    if scores.get("objective"):
        text = re.sub(
            r'(Current Objective/Mechanical Score:\s+\*\*)\d+\.?\d*(/100\*\*)',
            rf'\g<1>{scores["objective"]}\g<2>',
            text,
        )
    if scores.get("strict"):
        text = re.sub(
            r'(Current Strict Code Health Score:\s+\*\*)\d+\.?\d*(/100\*\*)',
            rf'\g<1>{scores["strict"]}\g<2>',
            text,
        )

    if text == original:
        print("[INFO] README.md — no changes needed.")
        return False

    readme.write_text(text, encoding="utf-8")
    print(f"[OK]   README.md — updated (v{version}, {today}, sha:{sha})")
    return True


# ── GCP deployment guide update ────────────────────────────────────────────────

def update_gcp_guide(version: str, today: str, stamp: str) -> bool:
    """Update gcp_deployment_guide.md header block. Returns True if modified."""
    guide = ROOT / "gcp_deployment_guide.md"
    if not guide.exists():
        print("[INFO] gcp_deployment_guide.md not found, skipping.")
        return False

    original = guide.read_text(encoding="utf-8")
    text = original

    # Update the guide version number in the title
    text = re.sub(
        r'(# Google Cloud Run Production Deployment Guide \(Version )\S+(\))',
        rf'\g<1>{version}\g<2>',
        text,
    )

    # Update any hardcoded RELEASE_VERSION stamps in the guide body
    text = re.sub(
        r'(RELEASE_VERSION["\s:=]+)["\']?\d{8}-\d{6}["\']?',
        rf'\g<1>"{stamp}"',
        text,
    )

    if text == original:
        print("[INFO] gcp_deployment_guide.md — no changes needed.")
        return False

    guide.write_text(text, encoding="utf-8")
    print(f"[OK]   gcp_deployment_guide.md — updated (v{version}, stamp:{stamp})")
    return True


# ── service.yaml / service-worker.yaml update ─────────────────────────────────

def update_service_yamls(stamp: str) -> bool:
    """Patch RELEASE_VERSION in service.yaml and service-worker.yaml."""
    changed = False
    for fname in ("service.yaml", "service-worker.yaml"):
        f = ROOT / fname
        if not f.exists():
            continue
        original = f.read_text(encoding="utf-8")
        text = re.sub(
            r'(- name: RELEASE_VERSION\s+value:\s+")[^"]*(")',
            rf'\g<1>{stamp}\g<2>',
            original,
        )
        if text != original:
            f.write_text(text, encoding="utf-8")
            print(f"[OK]   {fname} — RELEASE_VERSION updated to {stamp}")
            changed = True
        else:
            print(f"[INFO] {fname} — RELEASE_VERSION unchanged (already {stamp}?)")
    return changed


# ── cloudbuild.yaml — ensure BUILD_ID-based RELEASE_VERSION ───────────────────

def ensure_cloudbuild_dynamic_version() -> bool:
    """
    Verify cloudbuild.yaml passes RELEASE_VERSION as a dynamic substitution
    from the build timestamp rather than a hardcoded value.
    Returns True if the file was changed.
    """
    cb = ROOT / "cloudbuild.yaml"
    if not cb.exists():
        return False
    text = cb.read_text(encoding="utf-8")
    if "RELEASE_VERSION" in text:
        # Already managed — no changes needed
        return False
    return False  # non-destructive; changes are managed separately


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-update AetherOmni documentation.")
    parser.add_argument("--ci", action="store_true", help="Running in CI — skip interactive output.")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing files.")
    args = parser.parse_args()

    version = get_version()
    today = get_today()
    sha = get_commit_sha()
    stamp = get_release_stamp()
    test_count = get_test_count()
    scores = get_health_scores()

    print(f"AetherOmni Doc Updater — v{version} | {today} | sha:{sha}")
    print(f"  Release stamp : {stamp}")
    print(f"  Test count    : {test_count or '(unknown, keeping existing)'}")
    print(f"  Health scores : {scores or '(not available)'}")
    print()

    if args.dry_run:
        print("[DRY RUN] No files will be written.")
        return 0

    changed = []
    if update_readme(version, today, sha, test_count, scores):
        changed.append("README.md")
    if update_gcp_guide(version, today, stamp):
        changed.append("gcp_deployment_guide.md")
    if update_service_yamls(stamp):
        changed.extend(["service.yaml", "service-worker.yaml"])

    if args.ci and changed:
        print(f"\nChanged files: {', '.join(changed)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
