"""Vector store access through LangChain + Chroma (L1).

Embeddings run locally: all-MiniLM-L6-v2, through the ONNX build that ships
with Chroma (no torch, small image, no API cost). The `hash` backend is a
deterministic stand-in used only by the test suite.

Every chunk carries metadata: doc_id, clause_id, type (schedule_row,
modifier, amendment, paragraph), page and content hash. `type=modifier`
is what lets the agent ask for "provisions that modify a Schedule row"
directly, which a plain similarity search on the project type would miss.
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import re
import threading
from typing import Optional

import chromadb
from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings

from ecs.config import settings


class MiniLMEmbeddings(Embeddings):
    """all-MiniLM-L6-v2 (384 dimensions) via ONNX Runtime."""

    def __init__(self):
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        self._fn = ONNXMiniLM_L6_V2()
        self._query_cache = functools.lru_cache(maxsize=2048)(self._embed_one)  # D7 embedding cache

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._fn(texts)]

    def _embed_one(self, text: str) -> tuple:
        return tuple(self.embed_documents([text])[0])

    def embed_query(self, text: str) -> list[float]:
        return list(self._query_cache(text))


class HashEmbeddings(Embeddings):
    """Deterministic bag-of-words hashing embedding. Tests only."""

    dim = 384

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


_lock = threading.Lock()
_store: Optional[Chroma] = None
_client = None


def make_embeddings() -> Embeddings:
    backend = settings.embedding_backend
    if backend == "hash":
        return HashEmbeddings()
    if backend == "minilm":
        return MiniLMEmbeddings()
    raise ValueError(f"Unknown EMBEDDING_BACKEND {backend!r}")


def chroma_client():
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    return _client


def set_client(client) -> None:  # tests
    global _client, _store
    _client, _store = client, None


def vector_store() -> Chroma:
    global _store
    with _lock:
        if _store is None:
            _store = Chroma(client=chroma_client(), collection_name=settings.chroma_collection,
                            embedding_function=make_embeddings(),
                            collection_metadata={"hnsw:space": "cosine"})
        return _store


def _passage(doc_id: str, text: str, meta: dict, score: Optional[float] = None) -> dict:
    return {
        "passage_id": doc_id,
        "clause_id": meta.get("clause_id") or None,
        "type": meta.get("type"),
        "document": meta.get("doc_title") or meta.get("doc_id"),
        "doc_id": meta.get("doc_id"),
        "page": int(meta["page"]) if meta.get("page") not in (None, "", -1) else None,
        "text": text,
        "score": None if score is None else round(float(score), 4),
    }


def search(query: str, k: int = 5, type_filter: Optional[str] = None) -> list[dict]:
    """Semantic search. `type_filter` restricts to one chunk type, e.g. 'modifier'."""
    where = {"type": type_filter} if type_filter else None
    hits = vector_store().similarity_search_with_score(query, k=k, filter=where)
    return [_passage(d.id, d.page_content, d.metadata, s) for d, s in hits]


def get_clause(clause_id: str) -> list[dict]:
    """Exact lookup of a clause by identifier, e.g. '3(b)' or 'GC'."""
    coll = chroma_client().get_collection(settings.chroma_collection)
    res = coll.get(where={"clause_id": clause_id}, include=["documents", "metadatas"])
    return [_passage(i, d, m) for i, d, m in zip(res["ids"], res["documents"], res["metadatas"])]


def get_passages(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    coll = chroma_client().get_collection(settings.chroma_collection)
    res = coll.get(ids=list(ids), include=["documents", "metadatas"])
    return [_passage(i, d, m) for i, d, m in zip(res["ids"], res["documents"], res["metadatas"])]


def clause_exists(clause_id: str) -> bool:
    try:
        return bool(get_clause(clause_id))
    except Exception:
        return False


INDEX_META_KEY = "index:meta"


def index_meta(redis_client) -> dict:
    raw = redis_client.get(INDEX_META_KEY)
    return json.loads(raw) if raw else {}


def index_version(redis_client) -> str:
    """Changes whenever the index content changes; part of every cache key."""
    return index_meta(redis_client).get("fingerprint", "none")
