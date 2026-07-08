# Generated manually to apply high-performance pgvector HNSW indexing on PostgreSQL
from django.db import migrations


def create_hnsw_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        print("[Migration] PostgreSQL database detected. Creating high-performance pgvector HNSW index...")
        with schema_editor.connection.cursor() as cursor:
            # Ensure pgvector extension is initialized
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            # Create the Hierarchical Navigable Small World (HNSW) index
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS extractor_documentchunk_embedding_hnsw_idx 
                ON extractor_documentchunk 
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
            """)
            print("[Migration] HNSW index created successfully on 'extractor_documentchunk' (embedding).")
    else:
        print("[Migration] Non-PostgreSQL database detected. Skipping HNSW vector index creation.")


def drop_hnsw_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        print("[Migration] Removing HNSW vector index...")
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("DROP INDEX IF EXISTS extractor_documentchunk_embedding_hnsw_idx;")


class Migration(migrations.Migration):
    dependencies = [
        ("extractor", "0002_alter_documentchunk_unique_together"),
    ]

    operations = [
        migrations.RunPython(create_hnsw_index, reverse_code=drop_hnsw_index),
    ]
