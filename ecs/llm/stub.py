"""Stub LLM provider for load testing (D6).

An OpenAI-compatible /v1/chat/completions endpoint that returns fixed,
well-formed responses after a realistic delay, so Locust measures this system
rather than a free tier's rate limits. Point the gateway at it with
LLM_PROVIDER_BASE_URL=http://llm-stub:8090/v1.

It is NOT a reasoning model: it follows a fixed script (search the sector's
Schedule row, fetch the General Condition, then answer Category B citing what
it saw). Never use it for evaluation or for the section 4.1 demo.

Delay: lognormal around STUB_DELAY_MS (median), clipped to [0.3x, 4x].
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="ECS stub LLM")
_rng = random.Random(42)


def _delay_s() -> float:
    median = float(os.getenv("STUB_DELAY_MS", "1500")) / 1000.0
    d = median * math.exp(_rng.gauss(0, 0.35))
    return min(max(d, 0.3 * median), 4 * median)


def _screener_reply(user: str) -> dict:
    n_obs = user.count("OBSERVATION ")
    item = re.search(r'"schedule_item":\s*"([^"]+)"', user)
    sector = re.search(r'"sector":\s*"([^"]+)"', user)
    if n_obs == 0:
        if item:
            return {"thought": "fetch schedule row", "action": "get_clause", "args": {"clause_id": item.group(1)}}
        return {"thought": "search", "action": "search_corpus",
                "args": {"query": f"{sector.group(1) if sector else 'project'} schedule threshold"}}
    if n_obs == 1:
        return {"thought": "check modifiers", "action": "get_clause", "args": {"clause_id": "GC"}}
    ids = re.findall(r'passage_id:\s*(\S+)', user)
    return {"thought": "answer", "action": "final", "answer": {
        "category": "B", "route": "category_b_state",
        "basis": "Stub answer for load testing. The Schedule row places the project in Category B.",
        "citations": ids[:2], "conditions_applied": [], "escalation_reason": None}}


@app.post("/v1/chat/completions")
async def completions(body: dict):
    if _rng.random() < float(os.getenv("STUB_FAIL_RATE", "0")):
        return JSONResponse({"error": "stub failure"}, status_code=503)
    await asyncio.sleep(_delay_s())
    msgs = body.get("messages", [])
    system = next((m["content"] for m in msgs if m.get("role") == "system"), "")
    user = "\n".join(m.get("content", "") for m in msgs if m.get("role") == "user")
    if "ROLE: VERIFIER" in system:
        reply = {"supported": True, "unsupported_claims": [], "reason": "stub verifier"}
    else:
        reply = _screener_reply(user)
    content = json.dumps(reply)
    pt = sum(len(m.get("content", "")) for m in msgs) // 4
    return {"id": "stub", "object": "chat.completion", "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": len(content) // 4,
                      "total_tokens": pt + len(content) // 4}}


@app.get("/health")
def health():
    return {"status": "ok", "stub": True}
