"""Per-client rate limiting (D7).

Fixed one-minute window per client in Redis (INCR + EXPIRE), shared by every
API replica. The client is identified by the X-Client-Id header, falling back
to the remote address. A rejected request gets 429 with Retry-After and costs
nothing: no run is created and no model is called. This is a cost control -
one client looping against a multi-step agent could otherwise spend the
provider budget in minutes.
"""
from __future__ import annotations

import time

from fastapi import HTTPException, Request

from ecs.config import settings
from ecs.metrics import RATE_LIMITED
from ecs.runstore import get_redis


def client_id(request: Request) -> str:
    cid = (request.headers.get("X-Client-Id") or "").strip()[:64]
    if cid:
        return cid
    return request.client.host if request.client else "unknown"


def enforce(request: Request) -> str:
    cid = client_id(request)
    limit = settings.rate_limit_per_minute
    window = int(time.time() // 60)
    key = f"rl:{cid}:{window}"
    r = get_redis()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 70)
    count = pipe.execute()[0]
    if count > limit:
        RATE_LIMITED.inc()
        retry = 60 - int(time.time() % 60)
        raise HTTPException(status_code=429, detail={"error": "rate_limited", "limit_per_minute": limit},
                            headers={"Retry-After": str(retry)})
    return cid
