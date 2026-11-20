"""Structured Markdown Compiler for KORDA Lane 1 (Knowledge Refinery)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml

DEFAULT_MAX_WORDS_PER_BUNDLE = 450_000
FIELD_SECTION_HEADING = "section_heading"


def format_anchor_coord(anchor: dict[str, Any]) -> str:
    """Format deterministic anchor coordinates into a concise citation string."""
    parts: list[str] = []
    if anchor.get("page_num") is not None:
        parts.append(f"p.{anchor['page_num']}")
    if anchor.get(FIELD_SECTION_HEADING):
        parts.append(str(anchor[FIELD_SECTION_HEADING]).strip())
    if anchor.get("start_sec") is not None:
        end_sec = anchor.get("end_sec")
        if end_sec is not None:
            parts.append(f"{anchor['start_sec']:.1f}s-{end_sec:.1f}s")
        else:
            parts.append(f"{anchor['start_sec']:.1f}s")
    if anchor.get("figure_id"):
        parts.append(f"fig:{anchor['figure_id']}")
    return " | ".join(parts) if parts else "loc:unindexed"


def _render_crystal_section(crystal: dict[str, Any]) -> str:
    title = str(crystal.get("title") or "Untitled Concept").strip()
    summary = str(crystal.get("summary") or "").strip()
    tags = [str(t).strip() for t in (crystal.get("tags") or []) if str(t).strip()]
    anchors = crystal.get("anchors") or []

    lines: list[str] = [f"## {title}", ""]
    if tags:
        lines.append(f"*Tags:* {', '.join(tags)}")
        lines.append("")

    if summary:
        src_tags = " ".join(f"[SRC: {a.get('source_id', 'unknown')} | {format_anchor_coord(a)}]" for a in anchors[:3])
        lines.append(f"{summary} {src_tags}".strip())
        lines.append("")

    if anchors:
        lines.append("### Verbatim Evidence")
        lines.append("")
        for anchor in anchors:
            coord = format_anchor_coord(anchor)
            src_id = anchor.get("source_id", "unknown")
            quote = str(anchor.get("quote") or "").strip().replace("\n", "\n> ")
            lines.append(f"- `[SRC: {src_id} | {coord}]`")
            lines.append(f"  > {quote}")
        lines.append("")

    return "\n".join(lines)


def _render_bundle_header(bundle_title: str, part_index: int, doc_slugs: list[str]) -> str:
    frontmatter = {
        "title": f"{bundle_title} (Part {part_index})" if part_index > 1 else bundle_title,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema": "korda-kb-v1",
        "sources": sorted(set(doc_slugs)),
    }
    yaml_block = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    heading = frontmatter["title"]
    return f"---\n{yaml_block}\n---\n\n# {heading}\n\n"


def _split_oversized_section(section_md: str, max_words: int) -> list[str]:
    """Split a single section exceeding max_words into word-bounded chunks."""
    words = section_md.split()
    return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]


def _flush_bundle(
    bundles: list[str],
    bundle_title: str,
    sections: list[str],
    slugs: list[str],
) -> None:
    if not sections:
        return
    header = _render_bundle_header(bundle_title, len(bundles) + 1, slugs)
    bundles.append(header + "\n".join(sections))


def _append_oversized_crystal(
    bundles: list[str],
    bundle_title: str,
    section_md: str,
    max_words: int,
    slug: str,
) -> None:
    slug_list = [slug] if slug else []
    for part_md in _split_oversized_section(section_md, max_words):
        _flush_bundle(bundles, bundle_title, [part_md], slug_list)


def compile_markdown_bundles(
    crystals: list[dict[str, Any]],
    *,
    bundle_title: str = "KORDA Knowledge Bundle",
    max_words_per_bundle: int = DEFAULT_MAX_WORDS_PER_BUNDLE,
) -> list[str]:
    """Compile verified crystals into NotebookLM-safe Markdown bundles (<500k words each)."""
    if not crystals:
        return []

    max_words = max(1, int(max_words_per_bundle))
    bundles: list[str] = []
    current_sections: list[str] = []
    current_slugs: list[str] = []
    current_words = 0

    for crystal in crystals:
        section_md = _render_crystal_section(crystal)
        section_words = len(section_md.split())
        slug = str(crystal.get("doc_slug") or "")

        if section_words > max_words:
            _flush_bundle(bundles, bundle_title, current_sections, current_slugs)
            current_sections, current_slugs, current_words = [], [], 0
            _append_oversized_crystal(bundles, bundle_title, section_md, max_words, slug)
            continue

        if current_sections and (current_words + section_words > max_words):
            _flush_bundle(bundles, bundle_title, current_sections, current_slugs)
            current_sections, current_slugs, current_words = [], [], 0

        current_sections.append(section_md)
        if slug:
            current_slugs.append(slug)
        current_words += section_words

    _flush_bundle(bundles, bundle_title, current_sections, current_slugs)
    return bundles
