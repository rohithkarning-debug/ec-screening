"""Run records in Redis (A4, D5).

A run record holds everything needed to reconstruct a run from its ID: the
validated request, status, every step (tool call, query, passage IDs, prompt
version, latency, tokens, cost), the passages retrieved, the verification
outcome and the final result.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import redis

from ecs.config import settings

_client: Optional[redis.Redis] = None
RECENT_KEY = "runs:recent"      # newest first, capped
ACTIVE_KEY = "runs:active"      # sorted set: run_id -> started_at
ACTIVE_OWNER_KEY = "runs:active:owner"
RECENT_MAX = 500


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                       socket_timeout=5, socket_connect_timeout=3)
    return _client


def set_redis(client: redis.Redis) -> None:  # tests
    global _client
    _client = client


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:16]


def _key(run_id: str) -> str:
    return f"run:{run_id}"


def create_run(run_id: str, request: dict, client_id: str, prompt_version: str) -> dict:
    record = {
        "run_id": run_id,
        "status": "queued",
        "client_id": client_id,
        "request": request,
        "prompt_version": prompt_version,
        "submitted_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "steps": [],
        "passages": {},
        "verifications": [],
        "result": None,
        "totals": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                   "llm_calls": 0, "tool_calls": 0, "steps": 0, "retries": 0},
    }
    save_run(record)
    r = get_redis()
    r.lpush(RECENT_KEY, run_id)
    r.ltrim(RECENT_KEY, 0, RECENT_MAX - 1)
    return record


def save_run(record: dict) -> None:
    get_redis().set(_key(record["run_id"]), json.dumps(record, default=str), ex=settings.run_ttl_s)


def get_run(run_id: str) -> Optional[dict]:
    raw = get_redis().get(_key(run_id))
    return json.loads(raw) if raw else None


def update_run(run_id: str, **fields: Any) -> Optional[dict]:
    record = get_run(run_id)
    if record is None:
        return None
    record.update(fields)
    save_run(record)
    return record


def public_result(record: dict) -> dict:
    """The caller-facing view of a run (no internal trace)."""
    result = record.get("result") or {}
    return {
        "run_id": record["run_id"],
        "status": record["status"],
        "prompt_version": record.get("prompt_version"),
        "submitted_at": record.get("submitted_at"),
        "finished_at": record.get("finished_at"),
        **result,
    }


def mark_active(run_id: str, worker_id: str) -> None:
    r = get_redis()
    r.zadd(ACTIVE_KEY, {run_id: time.time()})
    r.hset(ACTIVE_OWNER_KEY, run_id, worker_id)


def mark_inactive(run_id: str) -> None:
    r = get_redis()
    r.zrem(ACTIVE_KEY, run_id)
    r.hdel(ACTIVE_OWNER_KEY, run_id)


def summary(record: dict) -> dict:
    """One line per run for the console's recent-runs list."""
    result = record.get("result") or {}
    req = record.get("request") or {}
    return {"run_id": record["run_id"], "status": record["status"], "route": result.get("route"),
            "category": result.get("category"), "verified": result.get("verified"),
            "cache_hit": bool(result.get("cache_hit")), "sector": req.get("sector"),
            "project_name": req.get("project_name"),
            "protected_area_distance_km": req.get("protected_area_distance_km"),
            "prompt_version": record.get("prompt_version"), "submitted_at": record.get("submitted_at"),
            "finished_at": record.get("finished_at"),
            "escalation_reason": result.get("escalation_reason"),
            "cost_usd": (record.get("totals") or {}).get("cost_usd")}


def recent(limit: int = 20) -> list[dict]:
    out = []
    for run_id in get_redis().lrange(RECENT_KEY, 0, max(limit, 1) * 2):
        rec = get_run(run_id)
        if rec:
            out.append(summary(rec))
        if len(out) >= limit:
            break
    return out
