"""Quarantine of rejected requests (D1, F2).

Every request rejected at the boundary - schema, type, range, category,
cross-field or guardrail - is stored with its field-level errors and the raw
body (truncated), and counted. No run ID is created for it, so no agent runs.
"""
from __future__ import annotations

import json
import time
import uuid

from ecs.metrics import QUARANTINED
from ecs.runstore import get_redis

QUARANTINE_KEY = "quarantine"
MAX_KEEP = 10_000


def quarantine(raw_body: str, errors: list[dict], reason: str, client_id: str, correlation: str) -> str:
    qid = "q_" + uuid.uuid4().hex[:12]
    entry = {"quarantine_id": qid, "ts": time.time(), "reason": reason, "client_id": client_id,
             "correlation_id": correlation, "errors": errors, "body": raw_body[:4000]}
    r = get_redis()
    r.lpush(QUARANTINE_KEY, json.dumps(entry))
    r.ltrim(QUARANTINE_KEY, 0, MAX_KEEP - 1)
    QUARANTINED.labels(reason).inc()
    return qid


def recent(limit: int = 50) -> list[dict]:
    return [json.loads(x) for x in get_redis().lrange(QUARANTINE_KEY, 0, limit - 1)]


def count() -> int:
    return int(get_redis().llen(QUARANTINE_KEY))
