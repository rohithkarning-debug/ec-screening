"""HTTP service hosting the verifier agent (A2).

Runs in its own container, so it fails, scales and restarts independently of
the screener workers. The hand-off is one call:

    POST /verify {run_id, request, draft, retrieved} -> {ok, outcome, issues, suggested_strategy, ...}

If this service is down or times out, the worker escalates the run; an
unverified answer is never returned (A3).
"""
from __future__ import annotations

import contextlib

from fastapi import FastAPI, Request
from fastapi.responses import Response
from opentelemetry import context as otel_context
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from ecs.agents import verifier
from ecs.logging_setup import CORRELATION_HEADER, correlation_id, setup_logging
from ecs.tracing import extract_context, setup_tracing, span


@contextlib.asynccontextmanager
async def lifespan(_app):
    setup_logging("verifier")
    setup_tracing("verifier")
    yield


app = FastAPI(title="ECS verifier agent", version="1.0.0", lifespan=lifespan)


class VerifyRequest(BaseModel):
    run_id: str
    request: dict
    draft: dict
    retrieved: dict


@app.post("/verify")
def verify(body: VerifyRequest, request: Request):
    correlation_id.set(request.headers.get(CORRELATION_HEADER, body.run_id))
    token = otel_context.attach(extract_context(dict(request.headers)))
    try:
        with span("agent.verifier", run_id=body.run_id) as s:
            out = verifier.verify(body.run_id, body.request, body.draft, body.retrieved)
            s.set_attribute("outcome", out["outcome"])
            s.set_attribute("issues", ",".join(i["code"] for i in out["issues"]))
            return out
    finally:
        otel_context.detach(token)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
