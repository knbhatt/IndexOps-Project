"""RAG tool: semantic search over the knowledge_base index with a BM25 fallback."""
from indexops.contract import KNOWLEDGE_BASE_INDEX, EMBEDDING_MODEL
from indexops.db import os_client

_model = None


def _embed(text: str) -> list[float] | None:
    """Embed with the same model used to build both indexes. Returns None if unavailable."""
    global _model
    try:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(EMBEDDING_MODEL)
        return _model.encode(text).tolist()
    except Exception:
        return None


def search_knowledge_base(query: str, top_k: int = 3) -> dict:
    """Retrieve the most relevant runbooks / incidents / contract docs for a query.

    Tries kNN vector search first; falls back to plain text match if the embedding
    model is unavailable or kNN returns nothing.
    """
    client = os_client()
    if not client.indices.exists(index=KNOWLEDGE_BASE_INDEX):
        return {"error": f"index '{KNOWLEDGE_BASE_INDEX}' does not exist; run data/index_knowledge_base.py"}

    hits, method = [], None
    vector = _embed(query)
    if vector is not None:
        resp = client.search(index=KNOWLEDGE_BASE_INDEX, body={
            "size": top_k,
            "_source": ["title", "content"],
            "query": {"knn": {"embedding": {"vector": vector, "k": top_k}}},
        })
        hits = resp["hits"]["hits"]
        method = "knn"

    if not hits:
        resp = client.search(index=KNOWLEDGE_BASE_INDEX, body={
            "size": top_k,
            "_source": ["title", "content"],
            "query": {"multi_match": {"query": query, "fields": ["title^2", "content"], "fuzziness": "AUTO"}},
        })
        hits = resp["hits"]["hits"]
        method = "bm25_fallback"

    return {
        "query": query,
        "method": method,
        "results": [{"title": h["_source"]["title"], "score": round(h["_score"], 4),
                     "content": h["_source"]["content"]} for h in hits],
    }
