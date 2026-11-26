"""
KORDA SurrealDB Knowledge-Graph Adapter

Entity upsert, chunk-to-entity relations, and 2-hop graph queries split
out of ``surreal_db.py``. Infrastructure resolves through
``extractor.surreal_db`` at call time to preserve mock.patch semantics.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

# ── Knowledge Graph RAG & Graph-Relational Methods ───────────────────────────


def upsert_entity(
    name: str,
    tenant_id: str,
    category: str = "CONCEPT",
    description: str = "",
    embedding: list[float] | None = None,
) -> dict[str, Any] | None:
    from extractor import surreal_db

    """
    Upserts an entity node in SurrealDB with optional HNSW embedding vector.
    """
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return {"name": name, "tenant_id": tenant_id, "category": category, "description": description}

    sql = """
    UPSERT entities:[ $name, $tenant_id ] SET
        name = $name,
        tenant_id = $tenant_id,
        category = $category,
        description = $description,
        embedding = $embedding,
        created_at = time::now();
    """
    params = {
        "name": name,
        "tenant_id": str(tenant_id),
        "category": category,
        "description": description,
        "embedding": embedding,
    }
    rows = surreal_db._first_result(surreal_db._run(sql, params))
    return rows[0] if rows and isinstance(rows, list) else None


def relate_chunk_to_entity(
    chunk_id: str,
    entity_name: str,
    tenant_id: str,
    relevance_score: float = 1.0,
) -> None:
    from extractor import surreal_db

    """
    Creates a directed graph relation edge from a text chunk to a knowledge entity.
    """
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return

    sql = """
    RELATE $chunk_id->chunk_references->(SELECT id FROM entities WHERE name = $entity_name AND tenant_id = $tenant_id LIMIT 1)
    SET relevance_score = $relevance_score, extracted_at = time::now();
    """
    params = {
        "chunk_id": chunk_id,
        "entity_name": entity_name,
        "tenant_id": str(tenant_id),
        "relevance_score": float(relevance_score),
    }
    surreal_db._check_surreal_error(surreal_db._run(sql, params))


def query_knowledge_graph(
    tenant_id: str,
    entity_name: str,
) -> dict[str, Any]:
    from extractor import surreal_db

    """
    Executes a 2-hop Graph Relational query returning connected documents, chunks, and related concepts.
    """
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return {"entity": entity_name, "connected_documents": [], "related_concepts": []}

    sql = """
    SELECT 
        name,
        category,
        description,
        <-chunk_references<-chunks.doc_uuid AS connected_documents,
        ->entity_relations->entities.name AS related_concepts
    FROM entities
    WHERE tenant_id = $tenant_id 
      AND name = $entity_name
    LIMIT 1;
    """
    rows = surreal_db._first_result(surreal_db._run(sql, {"tenant_id": str(tenant_id), "entity_name": entity_name}))
    if rows and isinstance(rows, list) and len(rows) > 0:
        return rows[0]
    return {"entity": entity_name, "connected_documents": [], "related_concepts": []}
