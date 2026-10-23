"""Redis list used as the job queue between the API and the agent workers (A4).

A plain list with BLPOP is enough here: jobs are small, ordering is FIFO, and
any number of worker replicas can block on the same list, which is what
`docker compose up --scale worker=3` relies on (A7).
"""
from __future__ import annotations

import json
from typing import Optional

from ecs.runstore import get_redis
from ecs.tracing import inject_headers

QUEUE_KEY = "queue:screening"


def enqueue(run_id: str, correlation_id: str, extra: Optional[dict] = None) -> None:
    job = {"run_id": run_id, "correlation_id": correlation_id,
           "trace": inject_headers({}), **(extra or {})}
    get_redis().rpush(QUEUE_KEY, json.dumps(job))


def dequeue(timeout_s: int = 5) -> Optional[dict]:
    item = get_redis().blpop([QUEUE_KEY], timeout=timeout_s)
    if not item:
        return None
    return json.loads(item[1])


def depth() -> int:
    return int(get_redis().llen(QUEUE_KEY))
