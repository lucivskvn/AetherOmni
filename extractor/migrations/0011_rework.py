"""
Migration 0011: Architecture Rework — Remove pgvector ORM Tables

Drops the DocumentChunk and RAGQueryCache tables that were previously
managed by Django's ORM + pgvector. These are replaced by SurrealDB's
`chunks` and `rag_cache` tables respectively.

Also removes the pgvector HNSW index extension that was created in 0003.

This migration is irreversible — apply only after the SurrealDB schema
has been initialised via:
    surreal import schema.surql

Dependencies: [('extractor', '0010_ragquerycache')]
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("extractor", "0010_ragquerycache"),
    ]

    operations = [
        # Drop DocumentChunk table (replaced by SurrealDB chunks table)
        migrations.DeleteModel(
            name="DocumentChunk",
        ),
        # Drop RAGQueryCache table (replaced by SurrealDB rag_cache table)
        migrations.DeleteModel(
            name="RAGQueryCache",
        ),
    ]
