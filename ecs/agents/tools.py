"""The screener agent's tools (A1).

Retrieval is a tool the agent chooses to call, not a fixed stage. Every tool:
- validates its arguments (injected text cannot widen what a tool does),
- runs under a timeout,
- returns passages that are recorded in the run's retrieved set; only those
  passages may later be cited (A3).
"""
from __future__ import annotations

import concurrent.futures
import re
from typing import Any, Callable

from ecs import cache, retrieval
from ecs.config import settings
from ecs.runstore import get_redis

ALLOWED_TYPES = {None, "schedule_row", "modifier", "amendment", "paragraph"}
CLAUSE_ID_RE = re.compile(r"^(GC|\d{1,2}\([a-z]\))$", re.I)
REQUEST_FIELDS = {
    "project_name", "sector", "schedule_item", "description", "capacity", "area_ha", "location",
    "protected_area_distance_km", "protected_area_name", "interstate_boundary_distance_km",
}

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


class ToolError(Exception):
    pass


def _with_timeout(fn: Callable[[], Any]) -> Any:
    fut = _pool.submit(fn)
    try:
        return fut.result(timeout=settings.tool_timeout_s)
    except concurrent.futures.TimeoutError as e:
        raise ToolError(f"tool timed out after {settings.tool_timeout_s}s") from e


def search_corpus(args: dict, request: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not (3 <= len(query) <= 300):
        raise ToolError("query must be 3-300 characters")
    k = int(args.get("k") or 5)
    k = max(1, min(k, 8))
    ctype = args.get("type") or None
    if ctype not in ALLOWED_TYPES:
        raise ToolError(f"type must be one of {sorted(t for t in ALLOWED_TYPES if t)}")
    r = get_redis()
    iv = retrieval.index_version(r)
    passages, hit = cache.cached_retrieval(
        r, iv, "search", {"q": query, "k": k, "t": ctype},
        lambda: _with_timeout(lambda: retrieval.search(query, k=k, type_filter=ctype)))
    return {"query": query, "passages": passages, "cache_hit": hit}


def get_clause(args: dict, request: dict) -> dict:
    cid = str(args.get("clause_id") or "").strip().replace(" ", "")
    if not CLAUSE_ID_RE.match(cid):
        raise ToolError("clause_id must look like '3(b)' or 'GC'")
    cid = "GC" if cid.upper() == "GC" else cid.lower()
    r = get_redis()
    iv = retrieval.index_version(r)
    passages, hit = cache.cached_retrieval(
        r, iv, "clause", {"c": cid}, lambda: _with_timeout(lambda: retrieval.get_clause(cid)))
    return {"query": f"clause_id={cid}", "passages": passages, "cache_hit": hit}


def get_request_field(args: dict, request: dict) -> dict:
    name = str(args.get("name") or "")
    if name not in REQUEST_FIELDS:
        raise ToolError(f"unknown field; allowed: {sorted(REQUEST_FIELDS)}")
    return {"query": f"field={name}", "value": request.get(name), "passages": []}


TOOLS: dict[str, Callable[[dict, dict], dict]] = {
    "search_corpus": search_corpus,
    "get_clause": get_clause,
    "get_request_field": get_request_field,
}
