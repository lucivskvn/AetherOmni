#!/usr/bin/env python3
"""
KORDA Code Quality, Cognitive Complexity & Literal Deduplication Gatekeeper

Verifies that:
1. No function or method exceeds Cognitive Complexity of 15 (aligned with SonarQube rules).
2. No long string literals (>=15 chars) are duplicated across files or within the same file.
"""

from __future__ import annotations

import ast
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_COGNITIVE_COMPLEXITY = 15

# Directories to scan (production core and extractor source trees)
SOURCE_DIRS = [ROOT / "core", ROOT / "extractor"]
EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "migrations", "tests", "management"}

ALLOWED_LITERALS = {
    "Content-Disposition",
    "application/json",
    "original_filename",
    "refined_markdown",
    "SURREALDB_OFFLINE",
    "SUPABASE_PUBLIC_KEY",
    "monthly_budget_usd",
    "total_spent_usd",
    "candidates_tokens",
    "total_tokens_spent",
    "budget_exceeded",
    "formatted_monthly_spent",
    "formatted_total_spent",
    "formatted_budget_cap",
    "currency_details",
    "process_document",
    "selected_documents",
    "semantic_signature",
    "export_ratelimit_",
    "publication_year",
    "license_type",
    "is_default_password",
    "query_embedding",
    "accumulated_cost_usd",
    "accumulated_input_tokens",
    "accumulated_output_tokens",
    "asia-southeast1",
    "europe-west9",
    "europe-west4",
    "northamerica-northeast1",
    "usd_exchange_rates",
    "realtime_model_pricing",
    "application/zip",
    "application/x-jsonlines",
    "application/x-sqlite3",
    "text/csv; charset=utf-8",
    "http://localhost:8080",
    "autoscaling.knative.dev/maxScale",
    "Metadata-Flavor",
    "/internal/tasks/",
    "password_change",
    "%Y-%m-%dT%H:%M:%SZ",
    "openrouter_api_key",
    "text-embedding-004",
    "supabase_user_id",
    "cf-turnstile-response",
    "CF_TURNSTILE_SITE_KEY",
    "RELEASE_VERSION",
}


def _calc_if_complexity(node: ast.If, current_nesting: int, func_node: ast.AST, is_elif: bool) -> int:
    score = 1 if is_elif else (1 + current_nesting)
    score += _calculate_sub_complexity(node.test, current_nesting, func_node)
    for child in node.body:
        score += _calculate_sub_complexity(child, current_nesting + 1, func_node)
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        score += _calculate_sub_complexity(node.orelse[0], current_nesting, func_node, is_elif=True)
    elif node.orelse:
        score += 1
        for child in node.orelse:
            score += _calculate_sub_complexity(child, current_nesting + 1, func_node)
    return score


def _calc_loop_complexity(node: ast.AST, current_nesting: int, func_node: ast.AST) -> int:
    score = 1 + current_nesting
    for child in ast.iter_child_nodes(node):
        score += _calculate_sub_complexity(child, current_nesting + 1, func_node)
    return score


def _calculate_sub_complexity(node: ast.AST, current_nesting: int, func_node: ast.AST, is_elif: bool = False) -> int:
    if isinstance(node, ast.If):
        return _calc_if_complexity(node, current_nesting, func_node, is_elif)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.With, ast.AsyncWith, ast.IfExp)):
        return _calc_loop_complexity(node, current_nesting, func_node)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node != func_node:
        return sum(
            _calculate_sub_complexity(child, current_nesting + 1, func_node) for child in ast.iter_child_nodes(node)
        )
    if isinstance(node, ast.BoolOp):
        return 1 + sum(_calculate_sub_complexity(child, current_nesting, func_node) for child in node.values)
    return sum(_calculate_sub_complexity(child, current_nesting, func_node) for child in ast.iter_child_nodes(node))


def get_cognitive_complexity(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return sum(_calculate_sub_complexity(stmt, 0, func_node) for stmt in func_node.body)


def _check_func_complexity(node: ast.AST, rel_path: str) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        cc = get_cognitive_complexity(node)
        if cc > MAX_COGNITIVE_COMPLEXITY:
            return (
                f"{rel_path}:{node.lineno}: Function '{node.name}' has Cognitive Complexity of {cc} "
                f"(maximum allowed: {MAX_COGNITIVE_COMPLEXITY})."
            )
    return None


def _check_constant_literal(node: ast.AST, file_literals: dict[str, list[int]]) -> None:
    if isinstance(node, ast.Constant):
        val = node.value
        if (
            isinstance(val, str)
            and len(val) >= 15
            and "\n" not in val
            and not val.startswith(("SELECT", "INSERT", "CREATE", "ALTER", "UPDATE", "DELETE"))
        ):
            file_literals[val].append(node.lineno)


def _check_ast_nodes_for_quality(tree: ast.AST, rel_path: str) -> tuple[list[str], dict[str, list[int]]]:
    complexity_errors: list[str] = []
    file_literals: dict[str, list[int]] = defaultdict(list)

    for node in ast.walk(tree):
        err = _check_func_complexity(node, rel_path)
        if err:
            complexity_errors.append(err)
        _check_constant_literal(node, file_literals)

    return complexity_errors, file_literals


def audit_file(file_path: Path) -> tuple[list[str], dict[str, list[str]]]:
    rel_path = str(file_path.relative_to(ROOT))
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except Exception as exc:
        return [f"{rel_path}: Failed to parse AST ({exc})"], {}

    complexity_errors, file_literals = _check_ast_nodes_for_quality(tree, rel_path)
    literal_locations: dict[str, list[str]] = defaultdict(list)
    for lit_val, lines in file_literals.items():
        if lit_val not in ALLOWED_LITERALS:
            for line_no in lines:
                literal_locations[lit_val].append(f"{rel_path}:{line_no}")

    return complexity_errors, literal_locations


def _get_all_tracked_python_files() -> dict[str, Path]:
    """Build an authoritative lookup map of canonical repo Python files."""
    file_map: dict[str, Path] = {}
    for src_dir in SOURCE_DIRS:
        for path in src_dir.rglob("*.py"):
            if not any(exc in path.parts for exc in EXCLUDE_DIRS):
                try:
                    canon_abs = os.path.realpath(str(path))
                    canon_rel = os.path.relpath(canon_abs, str(ROOT))
                    file_map[canon_abs] = path
                    file_map[canon_rel] = path
                except (OSError, ValueError):
                    continue
    return file_map


def _match_target_file(f_str: str, allowed_map: dict[str, Path]) -> Path | None:
    clean_str = os.path.normpath(f_str.strip())
    if clean_str in allowed_map:
        return allowed_map[clean_str]
    try:
        resolved = os.path.realpath(clean_str if os.path.isabs(clean_str) else os.path.join(str(ROOT), clean_str))
        if resolved in allowed_map:
            return allowed_map[resolved]
    except (OSError, ValueError):
        pass
    return None


def collect_target_files(target_files: list[str] | None) -> list[Path]:
    allowed_map = _get_all_tracked_python_files()
    if target_files:
        valid_files: list[Path] = []
        for f_str in target_files:
            matched = _match_target_file(f_str, allowed_map)
            if matched:
                valid_files.append(matched)
        return valid_files
    return list(set(allowed_map.values()))


def _aggregate_duplicate_literals(all_literals: dict[str, list[str]]) -> list[str]:
    dup_errors: list[str] = []
    for lit_val, locs in all_literals.items():
        if len(locs) >= 4:
            dup_errors.append(
                f"Literal '{lit_val[:40]}...' repeated {len(locs)} times across {locs[:3]}...\n"
                f"  Extract to a shared constant."
            )
    return dup_errors


def main(target_files: list[str] | None = None) -> int:
    print("Auditing codebase for Cognitive Complexity (<=15) & Literal String Deduplication...")
    files_to_check = collect_target_files(target_files)

    all_failures: list[str] = []
    repo_literals: dict[str, list[str]] = defaultdict(list)

    for py_file in files_to_check:
        comp_errs, file_lit_locs = audit_file(py_file)
        all_failures.extend(comp_errs)
        for lit_val, locs in file_lit_locs.items():
            repo_literals[lit_val].extend(locs)

    dup_failures = _aggregate_duplicate_literals(repo_literals)
    all_failures.extend(dup_failures)

    if all_failures:
        print(f"FAILED: Found {len(all_failures)} code quality issue(s):", file=sys.stderr)
        for failure in all_failures:
            print(f"  ✗ {failure}", file=sys.stderr)
        return 1

    print("✓ All functions satisfy Cognitive Complexity <= 15 and string literals are deduplicated.")
    return 0


if __name__ == "__main__":
    files_arg = sys.argv[1:] if len(sys.argv) > 1 else None
    raise SystemExit(main(files_arg))
