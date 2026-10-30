"""Automatic scoring of a sample of live runs (L6).

A fraction (LIVE_SCORE_SAMPLE_RATE) of completed runs is scored with the
reference-free metrics from ecs.quality. Each score is appended to a Redis
time series (`quality:series`, newest last, capped) and an exponentially
weighted moving average is exported as `ecs_live_quality{metric=...}`, which
Grafana charts over time.
"""
from __future__ import annotations

import json
import random
import time

from ecs import quality
from ecs.config import settings
from ecs.metrics import LIVE_QUALITY, LIVE_SCORED

SERIES_KEY = "quality:series"
EWMA_KEY = "quality:ewma"
ALPHA = 0.2
MAX_POINTS = 5000


def maybe_score(redis_client, record: dict, rng=random.random) -> dict | None:
    if rng() >= settings.live_score_sample_rate:
        return None
    scores = quality.score_record(record)
    point = {"ts": time.time(), "run_id": record["run_id"], **scores}
    redis_client.rpush(SERIES_KEY, json.dumps(point))
    redis_client.ltrim(SERIES_KEY, -MAX_POINTS, -1)
    ewma = json.loads(redis_client.get(EWMA_KEY) or "{}")
    for k, v in scores.items():
        ewma[k] = round(v if k not in ewma else ALPHA * v + (1 - ALPHA) * ewma[k], 4)
        LIVE_QUALITY.labels(k).set(ewma[k])
    redis_client.set(EWMA_KEY, json.dumps(ewma))
    LIVE_SCORED.inc()
    return point


def series(redis_client, limit: int = 200) -> list[dict]:
    return [json.loads(x) for x in redis_client.lrange(SERIES_KEY, -limit, -1)]


def export_gauges(redis_client) -> None:
    """Refresh gauges from Redis (the API exports them; workers may restart)."""
    for k, v in json.loads(redis_client.get(EWMA_KEY) or "{}").items():
        LIVE_QUALITY.labels(k).set(v)
