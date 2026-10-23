"""Public REST API (F1, F2, F8, A4, D1, D7).

Runs take tens of seconds (several model calls plus verification), so the API
never holds a connection open for one. POST returns 202 with a run ID at
once; the client polls GET /v1/screenings/{run_id}. Polling was chosen over
callbacks because the web client and Locust both run where an inbound
callback URL is awkward, and polling is idempotent and easy to retry. A Redis
queue sits between submission and execution.

Order of checks at the boundary, cheapest first, and none of them start a run:
rate limit -> JSON parse -> data contract -> input guardrails -> run cache.
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from ecs import cache, guardrails, live_quality, prompts, retrieval, runstore
from ecs.api import feedback, quarantine, ratelimit
from ecs.config import settings
from ecs.contract import CONTRACT_VERSION, ScreeningRequest, field_errors
from ecs.cost import summarize_run_costs
from ecs.logging_setup import CORRELATION_HEADER, correlation_id, log, setup_logging
from ecs.metrics import (HTTP_LATENCY, HTTP_REQUESTS, INDEX_BUILT_AT, INDEX_CHUNKS, INDEX_DOCS,
                         INGEST_THROUGHPUT, QUEUE_DEPTH, RUNS, SUBMISSIONS)
from ecs.queue import depth, enqueue
from ecs.tracing import setup_tracing, span

logger = logging.getLogger("api")
WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@contextlib.asynccontextmanager
async def lifespan(_app):
    setup_logging("api")
    setup_tracing("api")
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(_app, excluded_urls="health,metrics")
    except Exception:
        pass
    settings.redis_url  # fail fast on missing config
    yield


app = FastAPI(title="Environmental Clearance Pre-Screening Service", version="1.0.0", lifespan=lifespan,
              description="Screening opinion only. Not an authoritative determination; an accredited "
                          "consultant decides.")
app.include_router(feedback.router)


@app.middleware("http")
async def correlation_and_metrics(request: Request, call_next):
    cid = request.headers.get(CORRELATION_HEADER) or "req_" + uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    t0 = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", "static")
    HTTP_REQUESTS.labels("api", path, request.method, str(response.status_code)).inc()
    HTTP_LATENCY.labels("api", path).observe(time.perf_counter() - t0)
    response.headers[CORRELATION_HEADER] = correlation_id.get()
    return response


def _reject(status: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=body)


@app.post("/v1/screenings", status_code=202)
async def submit(request: Request, client_id: str = Depends(ratelimit.enforce)):
    raw = (await request.body()).decode("utf-8", errors="replace")
    cid = correlation_id.get()
    with span("api.submit", client_id=client_id) as s:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            qid = quarantine.quarantine(raw, [{"field": "(body)", "error": f"invalid JSON: {e.msg}"}],
                                        "malformed_json", client_id, cid)
            SUBMISSIONS.labels("quarantined").inc()
            return _reject(400, {"error": "malformed_json", "quarantine_id": qid, "run_started": False})
        try:
            req = ScreeningRequest.model_validate(body)
        except ValidationError as e:
            errs = field_errors(e)
            qid = quarantine.quarantine(raw, errs, "contract_violation", client_id, cid)
            SUBMISSIONS.labels("quarantined").inc()
            s.set_attribute("quarantined", True)
            return _reject(422, {"error": "contract_violation", "contract_version": CONTRACT_VERSION,
                                 "errors": errs, "quarantine_id": qid, "run_started": False})
        normalized = req.normalized()
        chk = guardrails.check_input(normalized)
        if not chk.ok:
            errs = [{"field": chk.field, "error": f"{chk.check}: {chk.detail}"}]
            qid = quarantine.quarantine(raw, errs, f"guardrail_{chk.check}", client_id, cid)
            SUBMISSIONS.labels("guardrail_blocked").inc()
            return _reject(400, {"error": "guardrail_blocked", "check": chk.check, "field": chk.field,
                                 "quarantine_id": qid, "run_started": False})

        pv = request.headers.get("X-Prompt-Version") or settings.prompt_version
        if pv not in prompts.available("screener"):
            raise HTTPException(400, f"unknown prompt version {pv!r}; available {prompts.available('screener')}")

        r = runstore.get_redis()
        run_id = runstore.new_run_id()
        correlation_id.set(run_id)
        s.set_attribute("run_id", run_id)
        key = cache.run_cache_key(normalized, pv, retrieval.index_version(r))
        cached = cache.get_run(r, key)
        record = runstore.create_run(run_id, normalized, client_id, pv)
        if cached:
            record.update(status="done", started_at=time.time(), finished_at=time.time(),
                          result={**cached, "cache_hit": True}, cached_from=key)
            runstore.save_run(record)
            RUNS.labels(cached.get("route") or "none", "done").inc()
            SUBMISSIONS.labels("cache_hit").inc()
            log(logger, logging.INFO, "run served from cache", run_id=run_id)
            return JSONResponse(status_code=200, content=runstore.public_result(record))

        demo_fault = request.headers.get("X-Demo-Fault") if settings.demo_faults else None
        enqueue(run_id, run_id, {"cache_key": key, "demo_fault": demo_fault})
        SUBMISSIONS.labels("queued").inc()
        log(logger, logging.INFO, "run queued", run_id=run_id, prompt_version=pv, client_id=client_id)
    return {"run_id": run_id, "status": "queued", "status_url": f"/v1/screenings/{run_id}",
            "trace_url": f"/v1/screenings/{run_id}/trace", "prompt_version": pv}


@app.get("/v1/screenings/{run_id}")
def get_result(run_id: str):
    rec = runstore.get_run(run_id)
    if rec is None:
        raise HTTPException(404, "unknown run_id")
    return runstore.public_result(rec)


@app.get("/v1/screenings/{run_id}/trace")
def get_trace(run_id: str):
    """Everything needed to reconstruct the run (D5)."""
    rec = runstore.get_run(run_id)
    if rec is None:
        raise HTTPException(404, "unknown run_id")
    rec.setdefault("cost", summarize_run_costs(rec.get("steps", [])))
    rec["jaeger_hint"] = f"Search Jaeger for tag run_id={run_id}"
    return rec


@app.get("/v1/contract")
def contract():
    return {"version": CONTRACT_VERSION, "request_schema": ScreeningRequest.model_json_schema()}


@app.get("/v1/quarantine")
def list_quarantine(limit: int = 50):
    return {"count": quarantine.count(), "items": quarantine.recent(min(max(limit, 1), 500))}


@app.get("/v1/index")
def index_info():
    return retrieval.index_meta(runstore.get_redis())


@app.get("/v1/prompts")
def prompt_versions():
    return {"screener": prompts.available("screener"), "default": settings.prompt_version}


@app.get("/health")
def health():
    checks = {}
    try:
        runstore.get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {type(e).__name__}"
    meta = retrieval.index_meta(runstore.get_redis()) if checks["redis"] == "ok" else {}
    checks["index_chunks"] = meta.get("chunks", 0)
    ok = checks["redis"] == "ok"
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "degraded", **checks})


@app.get("/metrics")
def metrics():
    try:
        r = runstore.get_redis()
        QUEUE_DEPTH.set(depth())
        meta = retrieval.index_meta(r)
        INDEX_CHUNKS.set(meta.get("chunks", 0))
        INDEX_DOCS.set(len(meta.get("documents", [])))
        INDEX_BUILT_AT.set(meta.get("built_at", 0))
        INGEST_THROUGHPUT.set(meta.get("chunks_per_second", 0))
        live_quality.export_gauges(r)
    except Exception:
        pass
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
