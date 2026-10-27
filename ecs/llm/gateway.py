"""LLM gateway service: the one interface to the model provider (A5, L5, section 12).

Workers never talk to a provider directly. They call this service, which:
- forwards to an OpenAI-compatible chat-completions endpoint configured only by
  environment (LLM_PROVIDER_BASE_URL, LLM_PROVIDER_API_KEY, LLM_MODEL), so the
  provider changes by configuration;
- enforces a per-call timeout and a circuit breaker on the provider;
- degrades along a defined path: primary provider -> fallback provider (if
  configured). A failed call while a breaker is closed returns 502
  `provider_error` (the client retries with backoff); when every provider's
  breaker is open it returns 503 `provider_unavailable` at once, which the
  worker turns into an escalation, never a confident answer;
- records prompt/completion tokens and cost at the time of the call, labelled
  by agent and step.

Demo controls (only when DEMO_FAULTS=1): POST /admin/faults {"provider_down": true}
makes every primary call fail, so the breaker can be shown opening live.
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from ecs import cost
from ecs.config import settings
from ecs.llm.breaker import STATE_VALUE, CircuitBreaker, CircuitOpenError
from ecs.logging_setup import CORRELATION_HEADER, correlation_id, log, setup_logging
from ecs.metrics import (BREAKER_OPENED, BREAKER_STATE, LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_TOKENS)
from ecs.tracing import extract_context, setup_tracing, span

logger = logging.getLogger("gateway")


@contextlib.asynccontextmanager
async def lifespan(_app):
    setup_logging("llm-gateway")
    setup_tracing("llm-gateway")
    breaker("primary")
    settings.llm_provider_base_url, settings.llm_model  # fail fast on missing config
    yield


app = FastAPI(title="ECS LLM gateway", version="1.0.0", lifespan=lifespan)
FAULTS = {"provider_down": False}


def _on_breaker_change(name, old, new):
    BREAKER_STATE.labels(name).set(STATE_VALUE[new])
    if new == "open":
        BREAKER_OPENED.labels(name).inc()
    log(logger, logging.WARNING, "circuit breaker state change", provider=name, old=old, new=new)


breakers: dict[str, CircuitBreaker] = {}


def breaker(name: str) -> CircuitBreaker:
    if name not in breakers:
        breakers[name] = CircuitBreaker(name, settings.breaker_fail_threshold, settings.breaker_reset_s,
                                        on_change=_on_breaker_change)
        BREAKER_STATE.labels(name).set(0)
    return breakers[name]


class ChatRequest(BaseModel):
    messages: list[dict]
    agent: str = Field("unknown", max_length=40)
    step: str = Field("unknown", max_length=60)
    run_id: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = Field(800, ge=1, le=4000)
    json_mode: bool = True


class ProviderError(RuntimeError):
    pass


def _providers() -> list[dict]:
    out = [{"name": "primary", "base_url": settings.llm_provider_base_url,
            "key": settings.llm_provider_api_key, "model": settings.llm_model}]
    if settings.llm_fallback_base_url and settings.llm_fallback_model:
        out.append({"name": "fallback", "base_url": settings.llm_fallback_base_url,
                    "key": settings.llm_fallback_api_key, "model": settings.llm_fallback_model})
    return out


async def _call_provider(p: dict, req: ChatRequest) -> dict:
    if p["name"] == "primary" and FAULTS["provider_down"]:
        raise ProviderError("simulated outage (DEMO_FAULTS)")
    headers = {"Content-Type": "application/json"}
    if p["key"]:
        headers["Authorization"] = f"Bearer {p['key']}"
    body = {"model": p["model"], "messages": req.messages, "temperature": req.temperature,
            "max_tokens": req.max_tokens}
    if req.json_mode:
        body["response_format"] = {"type": "json_object"}
    timeout = httpx.Timeout(settings.llm_timeout_s, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(p["base_url"].rstrip("/") + "/chat/completions", json=body, headers=headers)
    if r.status_code == 400 and req.json_mode and "response_format" in r.text:
        # Some providers reject json_object mode; retry once without it.
        body.pop("response_format", None)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(p["base_url"].rstrip("/") + "/chat/completions", json=body, headers=headers)
    if r.status_code >= 400:
        raise ProviderError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    content = data["choices"][0]["message"].get("content") or ""
    usage = data.get("usage") or {}
    pt = usage.get("prompt_tokens") or cost.estimate_tokens(" ".join(m.get("content", "") for m in req.messages))
    ct = usage.get("completion_tokens") or cost.estimate_tokens(content)
    return {"content": content, "prompt_tokens": int(pt), "completion_tokens": int(ct)}


@app.post("/v1/chat")
async def chat(req: ChatRequest, request: Request):
    correlation_id.set(request.headers.get(CORRELATION_HEADER, req.run_id or "-"))
    ctx = extract_context(dict(request.headers))
    last_error = "no provider configured"
    attempted = False
    from opentelemetry import context as otel_context

    token = otel_context.attach(ctx)
    try:
        for p in _providers():
            br = breaker(p["name"])
            try:
                br.before_call()
            except CircuitOpenError as e:
                LLM_CALLS.labels(p["name"], "breaker_open").inc()
                last_error = str(e)
                continue
            t0 = time.perf_counter()
            attempted = True
            with span("llm.call", provider=p["name"], agent=req.agent, step=req.step, run_id=req.run_id) as s:
                try:
                    out = await _call_provider(p, req)
                except (httpx.HTTPError, ProviderError, KeyError, ValueError) as e:
                    br.record_failure()
                    LLM_CALLS.labels(p["name"], "error").inc()
                    last_error = f"{p['name']}: {type(e).__name__}: {e}"[:300]
                    s.set_attribute("error", last_error)
                    log(logger, logging.WARNING, "provider call failed", provider=p["name"], error=last_error,
                        breaker=br.state)
                    continue
                br.record_success()
                latency = time.perf_counter() - t0
                usd = cost.call_cost(out["prompt_tokens"], out["completion_tokens"])
                LLM_CALLS.labels(p["name"], "ok").inc()
                LLM_LATENCY.labels(p["name"]).observe(latency)
                LLM_TOKENS.labels("prompt", req.agent, req.step).inc(out["prompt_tokens"])
                LLM_TOKENS.labels("completion", req.agent, req.step).inc(out["completion_tokens"])
                LLM_COST.labels(req.agent, req.step).inc(usd)
                s.set_attribute("prompt_tokens", out["prompt_tokens"])
                s.set_attribute("completion_tokens", out["completion_tokens"])
                s.set_attribute("cost_usd", usd)
                log(logger, logging.INFO, "provider call ok", provider=p["name"], agent=req.agent, step=req.step,
                    latency_ms=round(latency * 1000), prompt_tokens=out["prompt_tokens"],
                    completion_tokens=out["completion_tokens"], cost_usd=usd)
                return {**out, "provider": p["name"], "degraded": p["name"] != "primary",
                        "cost_usd": usd, "latency_ms": round(latency * 1000, 1)}
    finally:
        otel_context.detach(token)
    states = {k: b.state for k, b in breakers.items()}
    if attempted:
        # A provider was tried and failed while its breaker was still closed: transient, retryable.
        raise HTTPException(status_code=502, detail={"error": "provider_error", "reason": last_error,
                                                     "breakers": states})
    # Every provider is behind an open breaker: degrade now, do not retry.
    raise HTTPException(status_code=503, detail={"error": "provider_unavailable", "reason": last_error,
                                                 "breakers": states})


@app.get("/health")
def health():
    return {"status": "ok", "breakers": {k: b.snapshot() for k, b in breakers.items()} or {"primary": "closed"}}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/admin/breaker")
def breaker_status():
    breaker("primary")
    return {k: b.snapshot() for k, b in breakers.items()}


@app.post("/admin/faults")
def set_faults(body: dict):
    if not settings.demo_faults:
        return JSONResponse({"error": "fault injection disabled (set DEMO_FAULTS=1)"}, status_code=403)
    FAULTS["provider_down"] = bool(body.get("provider_down", False))
    log(logger, logging.WARNING, "fault injection changed", **FAULTS)
    return FAULTS
