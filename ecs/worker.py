"""Agent worker: takes jobs off the queue and runs the screener graph (A4, A7).

Each replica processes one run at a time, so throughput scales with
`docker compose up --scale worker=N`. Every graph update is written to the run
record, so a run in progress can be inspected, and a finished run can be
reconstructed in full from its ID (D5).

Liveness and recovery (A5):
- a background thread writes a heartbeat key every few seconds, so the
  console and the runbook can see which workers are alive and what they run;
- every run being processed sits in `runs:active` with its start time;
- one worker at a time (Redis lock) runs the reaper every 30 s: a run still
  active after STUCK_RUN_SECONDS (its worker crashed or hung) is closed as
  `failed` with an escalation, never left `running` forever and never
  returned as a confident answer.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time

from opentelemetry import context as otel_context
from prometheus_client import start_http_server

from ecs import cache, cost, live_quality, retrieval, runstore
from ecs.agents.graph import build_graph, initial_state
from ecs.config import settings
from ecs.logging_setup import correlation_id, log, setup_logging
from ecs.metrics import (AGENT_STEPS, AGENT_TASKS, AGENT_TOOL_CALLS, RUN_DURATION, RUNS, RUNS_IN_PROGRESS,
                         RUNS_REAPED)
from ecs.queue import dequeue
from ecs.tracing import extract_context, setup_tracing, span

logger = logging.getLogger("worker")
_running = True
_graph = None
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
HEARTBEAT_PREFIX = "worker:hb:"
HEARTBEAT_EVERY_S = 5
REAP_EVERY_S = 30
_current = {"run_id": None, "since": None, "done": 0, "started": time.time()}


def heartbeat_once(r) -> None:
    payload = {"worker_id": WORKER_ID, "run_id": _current["run_id"], "since": _current["since"],
               "runs_done": _current["done"], "started": _current["started"], "ts": time.time()}
    r.set(HEARTBEAT_PREFIX + WORKER_ID, json.dumps(payload), ex=HEARTBEAT_EVERY_S * 3)


def _beat() -> None:
    try:
        heartbeat_once(runstore.get_redis())
    except Exception:
        pass


def _heartbeat_loop() -> None:
    while _running:
        try:
            heartbeat_once(runstore.get_redis())
        except Exception:
            pass
        time.sleep(HEARTBEAT_EVERY_S)


def reap_stuck(r, now: float | None = None) -> list[str]:
    """Close runs left active past STUCK_RUN_SECONDS. Returns the run IDs closed."""
    now = now or time.time()
    limit = settings.stuck_run_seconds
    reaped = []
    for run_id in r.zrangebyscore(runstore.ACTIVE_KEY, 0, now - limit):
        owner = r.hget(runstore.ACTIVE_OWNER_KEY, run_id) or "unknown"
        alive = bool(r.exists(HEARTBEAT_PREFIX + owner))
        rec = runstore.get_run(run_id)
        runstore.mark_inactive(run_id)
        if rec is None or rec["status"] not in ("running", "queued"):
            continue
        why = "its worker stopped" if not alive else "it exceeded the time limit"
        rec["status"] = "failed"
        rec["finished_at"] = now
        rec["result"] = {"route": "escalate_to_human", "category": None, "verified": False, "citations": [],
                         "escalation_code": "run_reaped",
                         "escalation_reason": f"run did not complete: {why} (reaped after {limit:.0f}s); "
                                              "no determination was made"}
        runstore.save_run(rec)
        RUNS_REAPED.inc()
        RUNS.labels("escalate_to_human", "failed").inc()
        reaped.append(run_id)
        log(logger, logging.WARNING, "reaped stuck run", run_id=run_id, owner=owner, owner_alive=alive)
    return reaped


def maybe_reap(r) -> None:
    if r.set("reaper:lock", WORKER_ID, nx=True, ex=REAP_EVERY_S - 5):
        reap_stuck(r)


def _stop(*_):
    global _running
    _running = False


def _persist(run_id: str, state: dict) -> None:
    rec = runstore.get_run(run_id)
    if rec is None:
        return
    rec["steps"] = state.get("steps", [])
    rec["passages"] = state.get("retrieved", {})
    rec["verifications"] = state.get("verifications", [])
    runstore.save_run(rec)


def process(job: dict) -> dict:
    global _graph
    if _graph is None:
        _graph = build_graph()
    run_id = job["run_id"]
    correlation_id.set(job.get("correlation_id") or run_id)
    token = otel_context.attach(extract_context(job.get("trace") or {}))
    RUNS_IN_PROGRESS.inc()
    _current.update(run_id=run_id, since=time.time())
    _beat()
    try:
        runstore.mark_active(run_id, WORKER_ID)
        record = runstore.update_run(run_id, status="running", started_at=time.time())
        if record is None:
            log(logger, logging.WARNING, "run record missing; dropping job", run_id=run_id)
            return {}
        with span("run", run_id=run_id, prompt_version=record["prompt_version"]) as s:
            state = initial_state(run_id, record["request"], record["prompt_version"], job.get("demo_fault"))
            final = state
            try:
                for final in _graph.stream(state, stream_mode="values", config={"recursion_limit": 60}):
                    _persist(run_id, final)
                result = final.get("result") or {
                    "route": "escalate_to_human", "category": None, "verified": False,
                    "escalation_reason": "run ended without a result", "citations": []}
                status = "done"
            except Exception as e:  # never return a confident answer for an incomplete run
                log(logger, logging.ERROR, "run failed", error=repr(e)[:300])
                result = {"route": "escalate_to_human", "category": None, "verified": False, "citations": [],
                          "escalation_reason": f"internal error - run did not complete ({type(e).__name__})"}
                status = "failed"
            s.set_attribute("route", result.get("route") or "")
            s.set_attribute("verified", bool(result.get("verified")))

        rec = runstore.get_run(run_id)
        if (rec.get("result") or {}).get("escalation_code") == "run_reaped":
            log(logger, logging.WARNING, "run finished after being reaped; keeping the escalation", run_id=run_id)
            return rec
        rec["steps"] = final.get("steps", rec.get("steps", []))
        rec["passages"] = final.get("retrieved", rec.get("passages", {}))
        rec["verifications"] = final.get("verifications", rec.get("verifications", []))
        result["fallback_model_used"] = any((s.get("llm") or {}).get("degraded") for s in final.get("steps", []))
        rec["result"] = result
        rec["status"] = status
        rec["finished_at"] = time.time()
        costs = cost.summarize_run_costs(rec["steps"])  # verifier calls are already in its "verify" steps
        rec["cost"] = costs
        rec["totals"].update({
            "prompt_tokens": costs["total"]["prompt_tokens"], "completion_tokens": costs["total"]["completion_tokens"],
            "cost_usd": costs["total"]["cost_usd"], "llm_calls": costs["total"]["calls"],
            "tool_calls": final.get("tool_calls", 0), "steps": final.get("step_count", 0),
            "verify_attempts": len(rec["verifications"]),
        })
        runstore.save_run(rec)

        duration = rec["finished_at"] - rec["submitted_at"]
        RUN_DURATION.observe(duration)
        RUNS.labels(result.get("route") or "none", status).inc()
        AGENT_STEPS.labels("screener").observe(final.get("step_count", 0))
        AGENT_TOOL_CALLS.labels("screener").observe(final.get("tool_calls", 0))
        AGENT_TASKS.labels("screener", "success" if status == "done" and result.get("verified") else
                           ("escalated" if status == "done" else "failed")).inc()
        if job.get("cache_key"):
            cache.put_run(runstore.get_redis(), job["cache_key"], {**result, "source_run_id": run_id})
        live_quality.maybe_score(runstore.get_redis(), rec)
        log(logger, logging.INFO, "run finished", run_id=run_id, route=result.get("route"), status=status,
            verified=result.get("verified"), duration_s=round(duration, 2), cost_usd=costs["total"]["cost_usd"])
        return rec
    finally:
        runstore.mark_inactive(run_id)
        _current.update(run_id=None, since=None, done=_current["done"] + 1)
        _beat()
        RUNS_IN_PROGRESS.dec()
        otel_context.detach(token)


def main() -> None:
    setup_logging("worker")
    setup_tracing("worker")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    start_http_server(settings.metrics_port)
    settings.redis_url, settings.llm_gateway_url, settings.verifier_url  # fail fast on missing config
    warm_s = retrieval.warm_up()  # load the embedding model before taking jobs, not on the first search
    threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True).start()
    log(logger, logging.INFO, "worker ready", metrics_port=settings.metrics_port, worker_id=WORKER_ID,
        embedding_warm_up_s=warm_s)
    last_reap = 0.0
    while _running:
        if time.time() - last_reap >= REAP_EVERY_S:
            last_reap = time.time()
            try:
                maybe_reap(runstore.get_redis())
            except Exception as e:
                log(logger, logging.WARNING, "reaper failed", error=repr(e)[:200])
        try:
            job = dequeue(timeout_s=2)
        except Exception as e:
            log(logger, logging.ERROR, "queue unavailable", error=repr(e)[:200])
            time.sleep(2)
            continue
        if job:
            process(job)
    log(logger, logging.INFO, "worker stopped")


if __name__ == "__main__":
    main()
