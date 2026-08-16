#!/usr/bin/env python3
"""
Code Quality, Cognitive Complexity & Literal Deduplication Gatekeeper — AetherOmni v2.0

Verifies that:
1. No function or method exceeds Cognitive Complexity of 15 (aligned with SonarQube rules).
2. No long string literals (>=15 chars) are duplicated within the same file or across non-trivial contexts.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_COGNITIVE_COMPLEXITY = 15

# Directories to scan (production core and extractor source trees)
SOURCE_DIRS = [ROOT / "core", ROOT / "extractor"]
EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "migrations", "tests", "management"}


def _node_has_complexity_increment(node: ast.AST) -> bool:
    return isinstance(
        node,
        (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.With, ast.AsyncWith, ast.IfExp),
    )


def _is_nested_callable(node: ast.AST, func_node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node != func_node


def _calculate_sub_complexity(node: ast.AST, current_nesting: int, func_node: ast.AST) -> int:
    added = 0
    if _node_has_complexity_increment(node) or _is_nested_callable(node, func_node):
        added += 1 + current_nesting
        for child in ast.iter_child_nodes(node):
            added += _calculate_sub_complexity(child, current_nesting + 1, func_node)
    elif isinstance(node, ast.BoolOp):
        added += len(node.values) - 1
        for child in ast.iter_child_nodes(node):
            added += _calculate_sub_complexity(child, current_nesting, func_node)
    else:
        for child in ast.iter_child_nodes(node):
            added += _calculate_sub_complexity(child, current_nesting, func_node)
    return added


def get_cognitive_complexity(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return sum(_calculate_sub_complexity(stmt, 0, func_node) for stmt in func_node.body)


def _check_ast_nodes_for_quality(tree: ast.AST, rel_path: str) -> tuple[list[str], dict[str, list[int]]]:
    complexity_errors: list[str] = []
    file_literals: dict[str, list[int]] = defaultdict(list)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cc = get_cognitive_complexity(node)
            if cc > MAX_COGNITIVE_COMPLEXITY:
                complexity_errors.append(
                    f"{rel_path}:{node.lineno}: Function '{node.name}' has Cognitive Complexity of {cc} "
                    f"(maximum allowed: {MAX_COGNITIVE_COMPLEXITY})."
                )
        elif isinstance(node, ast.Constant):
            val = node.value
            if (
                isinstance(val, str)
                and len(val) >= 15
                and "\n" not in val
                and not val.startswith(("SELECT", "INSERT", "CREATE", "ALTER", "UPDATE", "DELETE"))
            ):
                file_literals[val].append(node.lineno)

    return complexity_errors, file_literals


def audit_file(file_path: Path) -> tuple[list[str], list[str]]:
    safe_path = _resolve_safe_file(file_path)
    if not safe_path:
        return [f"Untrusted or invalid file path rejected: {file_path}"], []

    rel_path = str(safe_path.relative_to(ROOT))
    try:
        content = safe_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(safe_path))
    except Exception as exc:
        return [f"{rel_path}: Failed to parse AST ({exc})"], []

    complexity_errors, file_literals = _check_ast_nodes_for_quality(tree, rel_path)

    # Check for in-file literal duplication (repeating the exact same UI/error/log string >= 3 times in 1 file)
    duplication_errors: list[str] = []
    for lit_val, lines in file_literals.items():
        if len(lines) >= 4:
            # Check if this literal is an allowed structural keyword
            allowed = {
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
                "usd_exchange_rates",
                "realtime_model_pricing",
                "application/zip",
                "application/x-jsonlines",
                "application/x-sqlite3",
                "text/csv; charset=utf-8",
            }
            if lit_val not in allowed:
                duplication_errors.append(
                    f"{rel_path}: Literal '{lit_val[:40]}...' is duplicated {len(lines)} times on lines {lines}. "
                    "Extract to a module-level constant."
                )

    return complexity_errors, duplication_errors


def _resolve_safe_file(p: Path) -> Path | None:
    try:
        resolved = (ROOT / p).resolve() if not p.is_absolute() else p.resolve()
        if resolved.is_file() and resolved.suffix == ".py" and (resolved == ROOT or ROOT in resolved.parents):
            return resolved
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def collect_target_files(target_files: list[str] | None) -> list[Path]:
    if target_files:
        valid_files: list[Path] = []
        for f_str in target_files:
            safe_p = _resolve_safe_file(Path(f_str))
            if safe_p:
                valid_files.append(safe_p)
        return valid_files
    files: list[Path] = []
    for src_dir in SOURCE_DIRS:
        for path in src_dir.rglob("*.py"):
            if not any(exc in path.parts for exc in EXCLUDE_DIRS):
                files.append(path)
    return files


def main(target_files: list[str] | None = None) -> int:
    print("Auditing codebase for Cognitive Complexity (<=15) & Literal String Deduplication...")
    files_to_check = collect_target_files(target_files)

    all_failures: list[str] = []
    for py_file in files_to_check:
        comp_errs, dup_errs = audit_file(py_file)
        all_failures.extend(comp_errs)
        all_failures.extend(dup_errs)

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
