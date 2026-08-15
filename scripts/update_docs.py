#!/usr/bin/env python3
"""
AetherOmni — Automatic Documentation & Version Updater
=======================================================
Computes the canonical application version from live git metadata on every
invocation — no hardcoded numbers, no manual version bumps for patches.

VERSION SCHEME
--------------
  Full version : MAJOR.MINOR.PATCH
  MAJOR.MINOR  : human-controlled; stored in VERSION file (e.g. "1.2")
  PATCH        : Git commit count, so every analyzed commit has a distinct version
  Release tag  : stable semver only; no SHA suffix because Docker/Cloud Run tags
                 reject '+' and we want a predictable deployment image tag.

  Examples:
    1.2.107          — stable release tag
    1.2.107-dirty    — local uncommitted changes

WHAT IS UPDATED
---------------
  README.md              — version badge, last-updated badge, commit SHA badge,
                           test count, desloppify scores
  docs/gcp_deployment_guide.md — guide version heading
  infra/gcp/service.yaml           — RELEASE_VERSION env var
  infra/gcp/service-worker.yaml    — RELEASE_VERSION env var

HOW IT IS TRIGGERED
-------------------
  1. Locally: python scripts/update_docs.py
  2. After run_checks.sh passes (step 7)
  3. GitHub Actions: manual workflow dispatch only (.github/workflows/update_docs.yml)
     — this workflow no longer auto-commits to main to prevent branch churn.
  4. CI and Cloud Build compute RELEASE_VERSION from VERSION + commit count and
     pass it to SonarQube and Cloud Run without rewriting tracked files.

Usage:
    python scripts/update_docs.py [--ci] [--dry-run]
    python scripts/update_docs.py --print-version
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()


# ── Version computation ────────────────────────────────────────────────────────


import shutil


def _git(*args: str) -> str:
    """Run a git command and return stripped stdout, or '' on failure."""
    git_bin = shutil.which("git")
    if not git_bin:
        return ""
    try:
        result = subprocess.run(  # nosec B603
            [git_bin, *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def get_major_minor() -> str:
    """
    Read MAJOR.MINOR from the VERSION file.
    This is the only part that requires a human decision (API breaks, big features).
    """
    vf = ROOT / "VERSION"
    if vf.exists():
        raw = vf.read_text(encoding="utf-8").strip()
        # Accept "1.2", "1.2.0", "v1.2" — normalize to MAJOR.MINOR
        parts = raw.lstrip("v").split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
        return raw.lstrip("v")
    return "0.1"


def get_last_release_tag() -> str:
    """Return the newest semantic release tag in the repo, if one exists."""
    tag = _git("describe", "--tags", "--abbrev=0")
    if tag:
        return tag.strip()

    tags = _git("tag", "--sort=-v:refname")
    if tags:
        for line in tags.splitlines():
            candidate = line.strip()
            if re.fullmatch(r"v?\d+\.\d+\.\d+", candidate):
                return candidate
    return ""


def get_commit_count() -> str:
    """Return a monotonically increasing PATCH number for the current commit."""
    env_count = os.getenv("BUILD_NUMBER") or os.getenv("COMMIT_COUNT")
    if env_count and env_count.isdigit():
        return env_count
    return _git("rev-list", "--count", "HEAD") or "0"


def get_short_sha() -> str:
    env_sha = os.getenv("SHORT_SHA") or os.getenv("COMMIT_SHA", "")[:7]
    if env_sha:
        return env_sha
    return _git("rev-parse", "--short", "HEAD") or "unknown"


def get_full_sha() -> str:
    env_sha = os.getenv("COMMIT_SHA")
    if env_sha:
        return env_sha
    return _git("rev-parse", "HEAD") or "unknown"


def get_branch() -> str:
    env_branch = os.getenv("BRANCH_NAME") or os.getenv("GIT_BRANCH")
    if env_branch:
        return env_branch
    return _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"


def is_dirty() -> bool:
    if os.getenv("CI") or os.getenv("CLOUD_BUILD"):
        return False
    return bool(_git("status", "--porcelain"))


def compute_version() -> dict:
    """
    Compute all version artefacts from live git state.

    Returns a dict with:
      major_minor   : "1.2"
      patch         : "107"
      semver        : "1.2.107"          — clean semantic version
      release_ver   : "v1.2.107"         — stable deployment version
      badge_ver     : "v1.2.107"         — clean badge
      sha           : "08e6f35"
      branch        : "current"
      today         : "2026-07-19"
      dirty         : False
    """
    major_minor = get_major_minor()
    patch = get_commit_count()
    sha = get_short_sha()
    branch = get_branch()
    dirty = is_dirty()
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    semver = f"{major_minor}.{patch}"
    dirty_suffix = "-dirty" if dirty else ""
    release_ver = f"v{semver}{dirty_suffix}"
    badge_ver = f"v{semver}"

    return {
        "major_minor": major_minor,
        "patch": patch,
        "semver": semver,
        "release_ver": release_ver,
        "badge_ver": badge_ver,
        "sha": sha,
        "full_sha": get_full_sha(),
        "branch": branch,
        "today": today,
        "dirty": dirty,
    }


# ── Metadata helpers ───────────────────────────────────────────────────────────


def get_test_count() -> str:
    """Read cached test count written by run_checks.sh."""
    cache = ROOT / ".test_count"
    if cache.exists():
        val = cache.read_text(encoding="utf-8").strip()
        if val.isdigit():
            return val
    return ""


def get_health_scores() -> dict[str, str]:
    """Read or compute live desloppify scan scores dynamically."""
    query_file = ROOT / ".desloppify" / "query.json"
    score_file = ROOT / ".desloppify" / "score_cache.json"

    # If query.json is missing, trigger a fast local desloppify scan
    if not query_file.exists() and not score_file.exists():
        try:
            import subprocess  # nosec B404

            subprocess.run(  # nosec B607 B603
                ["desloppify", "scan"],
                cwd=ROOT,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            print(f"[WARN] Dynamic desloppify scan execution warning: {exc}")

    if query_file.exists():
        try:
            import json

            data = json.loads(query_file.read_text(encoding="utf-8"))
            if "objective_score" in data:
                return {
                    "objective": str(round(float(data.get("objective_score", 0)), 1)),
                    "strict": str(round(float(data.get("strict_score", 0)), 1)),
                    "overall": str(round(float(data.get("overall_score", 0)), 1)),
                }
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    if score_file.exists():
        try:
            import json

            data = json.loads(score_file.read_text(encoding="utf-8"))
            return {
                "objective": str(round(float(data.get("objective", 0)), 1)),
                "strict": str(round(float(data.get("strict", 0)), 1)),
            }
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    return {}


# ── Section rewriter ───────────────────────────────────────────────────────────


def _replace_sentinel(text: str, key: str, new_content: str) -> tuple[str, bool]:
    """Replace content between <!-- auto:KEY --> … <!-- /auto:KEY --> sentinels."""
    pattern = re.compile(
        rf"(<!-- auto:{re.escape(key)} -->)(.*?)(<!-- /auto:{re.escape(key)} -->)",
        re.DOTALL,
    )
    replacement = rf"\g<1>\n{new_content}\n\g<3>"
    new_text, count = pattern.subn(replacement, text)
    return new_text, count > 0


# ── README update ──────────────────────────────────────────────────────────────


def update_readme(v: dict, test_count: str, scores: dict) -> bool:
    """Patch README.md. Returns True if the file changed."""
    readme = ROOT / "README.md"
    if not readme.exists():
        print("[WARN] README.md not found, skipping.")
        return False

    original = readme.read_text(encoding="utf-8")
    text = original

    # Badge block — use sentinel if present, else inline patch
    badge_block = (
        f"[![Version](https://img.shields.io/badge/version-{v['badge_ver']}-blue.svg)]"
        f"(https://github.com/lucivskvn/AetherOmni)\n"
        f"[![Last Updated](https://img.shields.io/badge/last%20updated-"
        f"{v['today'].replace('-', '--')}-green.svg)](#)\n"
        f"[![Commit](https://img.shields.io/badge/commit-{v['sha']}-lightgrey.svg)](#)"
    )

    text, replaced = _replace_sentinel(text, "badges", badge_block)
    if not replaced:
        text = re.sub(
            r"\[!\[Version\]\(https://img\.shields\.io/badge/version-[^\)]+\)\]\([^\)]*\)",
            f"[![Version](https://img.shields.io/badge/version-{v['badge_ver']}-blue.svg)]"
            f"(https://github.com/lucivskvn/AetherOmni)",
            text,
        )
        text = re.sub(
            r"\[!\[Last Updated\]\(https://img\.shields\.io/badge/last%20updated-[^\)]+\)\]\([^\)]*\)",
            f"[![Last Updated](https://img.shields.io/badge/last%20updated-"
            f"{v['today'].replace('-', '--')}-green.svg)](#)",
            text,
        )
        text = re.sub(
            r"\[!\[Commit\]\(https://img\.shields\.io/badge/commit-[^\)]+\)\]\([^\)]*\)",
            f"[![Commit](https://img.shields.io/badge/commit-{v['sha']}-lightgrey.svg)](#)",
            text,
        )

    # Test count
    if test_count:
        text = re.sub(
            r"(Executes\s+)\d+(\s+comprehensive unit tests)",
            rf"\g<1>{test_count}\g<2>",
            text,
        )

    # Desloppify scores — badge is now the dynamic scorecard.png image; no shields.io URL to patch.
    # The scorecard.png is regenerated by `desloppify scan` and committed separately.
    # We still update any inline score text references if they exist in the README.
    if scores.get("objective"):
        text = re.sub(
            r"(Current Objective/Mechanical Score:\s+\*\*)[\\d.]+?(/100\*\*)",
            rf"\g<1>{scores['objective']}\g<2>",
            text,
        )
    if scores.get("strict"):
        text = re.sub(
            r"(Current Strict Code Health Score:\s+\*\*)[\\d.]+?(/100\*\*)",
            rf"\g<1>{scores['strict']}\g<2>",
            text,
        )

    if text == original:
        print("[INFO] README.md — no changes needed.")
        return False

    readme.write_text(text, encoding="utf-8")
    print(f"[OK]   README.md — {v['badge_ver']} | {v['today']} | sha:{v['sha']}")
    return True


# ── GCP deployment guide update ────────────────────────────────────────────────


def update_gcp_guide(v: dict) -> bool:
    """Keep the deployment guide free of generated version churn."""
    _ = v
    return False


# ── service.yaml / service-worker.yaml update ─────────────────────────────────


def update_service_yamls(_v: dict) -> bool:
    """
    Validate that service YAMLs contain the dynamic ${RELEASE_VERSION} placeholder.

    RELEASE_VERSION is intentionally NOT written here — it is computed at deploy
    time by cloudbuild.yaml (Step 0) from the VERSION file + git commit count +
    $SHORT_SHA, then injected via --set-env-vars.  This keeps the service YAML
    files as clean templates (like ${GCP_REGION}, ${GCP_PROJECT_ID}) with no
    hardcoded build metadata that would create churn on every local run.

    See: infra/gcp/cloudbuild.yaml — Step 0 (compute-version)
    """
    placeholder = "${RELEASE_VERSION}"
    for fname in ("infra/gcp/service.yaml", "infra/gcp/service-worker.yaml"):
        f = ROOT / fname
        if not f.exists():
            continue
        content = f.read_text(encoding="utf-8")
        if placeholder in content:
            print(f"[INFO] {fname} — RELEASE_VERSION uses dynamic placeholder (resolved by CloudBuild)")
        else:
            # Placeholder was replaced — restore it so the file stays a clean template
            fixed = re.sub(
                r'(- name: RELEASE_VERSION\s+value:\s+")[^"]*(")',
                rf"\g<1>{placeholder}\g<2>",
                content,
                flags=re.MULTILINE,
            )
            if fixed != content:
                f.write_text(fixed, encoding="utf-8")
                print(f"[OK]   {fname} — restored {placeholder} dynamic placeholder")
            else:
                print(f"[WARN] {fname} — RELEASE_VERSION entry not found; manual check needed")
    return False  # service YAMLs are never "changed" by this updater


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="AetherOmni auto documentation updater.")
    parser.add_argument("--ci", action="store_true", help="CI mode: compact output.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing.")
    parser.add_argument("--print-version", action="store_true", help="Print semver only (machine-readable) and exit.")
    parser.add_argument("--print-release", action="store_true", help="Print stable release_ver (v1.2.107) and exit.")
    args = parser.parse_args()

    v = compute_version()

    if args.print_version:
        print(v["semver"])
        return 0

    if args.print_release:
        print(v["release_ver"])
        return 0

    print("AetherOmni Doc Updater")
    print(f"  Semver        : {v['semver']}")
    print(f"  Release ver   : {v['release_ver']}")
    print(f"  Branch        : {v['branch']}")
    print(f"  Commit        : {v['sha']} (total: {v['patch']})")
    print(f"  Date          : {v['today']}")
    print(f"  Dirty         : {v['dirty']}")

    test_count = get_test_count()
    scores = get_health_scores()
    if test_count:
        print(f"  Test count    : {test_count}")
    if scores:
        print(f"  Health scores : {scores}")
    print()

    if args.dry_run:
        print("[DRY RUN] No files will be written.")
        return 0

    changed = []
    if update_readme(v, test_count, scores):
        changed.append("README.md")
    if update_service_yamls(v):
        changed.extend(["service.yaml", "service-worker.yaml"])
    if args.ci:
        print(f"\nChanged: {', '.join(changed) if changed else 'none'}")

    return 0 if changed or not args.ci else 1


if __name__ == "__main__":
    sys.exit(main())
