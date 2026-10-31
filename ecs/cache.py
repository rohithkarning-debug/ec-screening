"""Caching (D7).

Two layers, both in Redis so every worker replica shares them:

1. Whole-run cache. Key = hash(normalised request + content hashes of the
   screener and verifier prompts + index fingerprint). Screening the same project twice against the same corpus and
   prompt should give the same opinion, so a hit returns the stored, verified
   result without any model call. Only verified results are cached; an
   escalation caused by a provider outage is never cached. The index
   fingerprint changes on re-ingestion, so a corpus update invalidates
   everything automatically.
2. Retrieval cache. Key = hash(query + k + type filter + index fingerprint).
   Saves the embedding and the vector search on repeated agent queries.

Embeddings are additionally memoised in-process (LRU) because the same query
strings recur across runs ("General Condition protected area").
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Optional

from ecs.config import settings
from ecs.metrics import RUN_CACHE

RETRIEVAL_TTL_S = 6 * 3600


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def run_cache_key(normalized_request: dict, prompt_version: str, index_version: str) -> str:
    req = {k: v for k, v in normalized_request.items() if k not in ("project_name",)}
    return "cache:run:" + _h({"r": req, "p": prompt_version, "i": index_version})


def get_run(redis_client, key: str) -> Optional[dict]:
    if not settings.run_cache_enabled:
        return None
    raw = redis_client.get(key)
    RUN_CACHE.labels("hit" if raw else "miss").inc()
    return json.loads(raw) if raw else None


def put_run(redis_client, key: str, result: dict) -> bool:
    if not settings.run_cache_enabled or not result.get("verified"):
        return False
    redis_client.set(key, json.dumps(result), ex=settings.run_cache_ttl_s)
    return True


def cached_retrieval(redis_client, index_version: str, kind: str, args: dict,
                     fn: Callable[[], list]) -> tuple[list, bool]:
    key = f"cache:retr:{kind}:" + _h({"a": args, "i": index_version})
    raw = redis_client.get(key)
    if raw:
        return json.loads(raw), True
    value = fn()
    redis_client.set(key, json.dumps(value), ex=RETRIEVAL_TTL_S)
    return value, False
