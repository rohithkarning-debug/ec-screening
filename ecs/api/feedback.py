"""Feedback endpoint and live-quality series (L6).

POST /v1/screenings/{run_id}/feedback  {"satisfactory": bool, "reason": str}
GET  /v1/feedback/summary              aggregates: counts, share satisfactory, by route, recent reasons
GET  /v1/quality/live                  the sampled live-run quality series
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ecs import live_quality, runstore
from ecs.guardrails import detect_injection, toxicity_score
from ecs.metrics import FEEDBACK

router = APIRouter()
FEEDBACK_KEY = "feedback:all"


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    satisfactory: bool
    reason: str = Field(..., min_length=3, max_length=1000)


@router.post("/v1/screenings/{run_id}/feedback", status_code=201)
def submit_feedback(run_id: str, fb: Feedback):
    rec = runstore.get_run(run_id)
    if rec is None:
        raise HTTPException(404, "unknown run_id")
    if rec["status"] not in ("done", "failed"):
        raise HTTPException(409, "run has not finished")
    flagged = bool(detect_injection(fb.reason)) or toxicity_score(fb.reason) >= 2
    entry = {"run_id": run_id, "ts": time.time(), "satisfactory": fb.satisfactory, "reason": fb.reason,
             "route": (rec.get("result") or {}).get("route"), "prompt_version": rec.get("prompt_version"),
             "flagged": flagged}
    r = runstore.get_redis()
    r.rpush(FEEDBACK_KEY, json.dumps(entry))
    r.hset(f"feedback:run:{run_id}", mapping={"satisfactory": int(fb.satisfactory), "reason": fb.reason})
    FEEDBACK.labels(str(fb.satisfactory).lower()).inc()
    return {"stored": True, "run_id": run_id}


@router.get("/v1/feedback/summary")
def feedback_summary():
    items = [json.loads(x) for x in runstore.get_redis().lrange(FEEDBACK_KEY, 0, -1)]
    total = len(items)
    good = sum(1 for i in items if i["satisfactory"])
    by_route: dict[str, dict] = {}
    by_prompt: dict[str, dict] = {}
    for i in items:
        for key, bucket in ((i.get("route") or "unknown", by_route), (i.get("prompt_version") or "?", by_prompt)):
            b = bucket.setdefault(key, {"total": 0, "satisfactory": 0})
            b["total"] += 1
            b["satisfactory"] += int(i["satisfactory"])
    return {"total": total, "satisfactory": good,
            "share_satisfactory": round(good / total, 3) if total else None,
            "by_route": by_route, "by_prompt_version": by_prompt,
            "recent_unsatisfactory": [{"run_id": i["run_id"], "reason": i["reason"]}
                                      for i in items if not i["satisfactory"]][-10:]}


@router.get("/v1/quality/live")
def live_series(limit: int = 200):
    return {"points": live_quality.series(runstore.get_redis(), min(max(limit, 1), 2000))}
