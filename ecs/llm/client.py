"""Client used by the agents to reach the LLM gateway (A5).

- timeout on every call (connect and read);
- bounded retry with exponential backoff and full jitter on timeouts,
  connection errors and 5xx responses;
- no retry when the gateway says the provider is unavailable (breaker open):
  retrying would only wait, so we degrade immediately;
- raises ProviderUnavailable when all attempts fail; the graph turns that into
  an escalation, so an incomplete run is never returned as a confident answer.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field

import httpx

from ecs.config import settings
from ecs.logging_setup import CORRELATION_HEADER, correlation_id, log
from ecs.metrics import AGENT_RETRIES
from ecs.tracing import inject_headers

logger = logging.getLogger("llm.client")


class ProviderUnavailable(RuntimeError):
    pass


class BadModelOutput(ValueError):
    pass


@dataclass
class LLMResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    provider: str = ""
    degraded: bool = False
    latency_ms: float = 0.0
    attempts: int = 1
    errors: list = field(default_factory=list)

    def usage(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "cost_usd": self.cost_usd, "provider": self.provider, "degraded": self.degraded,
                "latency_ms": self.latency_ms, "attempts": self.attempts}


def backoff_s(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """Exponential backoff with full jitter: U(0, min(cap, base * 2^attempt))."""
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def chat(messages: list[dict], agent: str, step: str, run_id: str | None = None,
         max_tokens: int = 800, sleep=time.sleep) -> LLMResult:
    url = settings.llm_gateway_url.rstrip("/") + "/v1/chat"
    body = {"messages": messages, "agent": agent, "step": step, "run_id": run_id, "max_tokens": max_tokens}
    timeout = httpx.Timeout(settings.llm_timeout_s + 5, connect=3.0)
    errors = []
    attempts = settings.llm_max_attempts
    for attempt in range(attempts):
        headers = inject_headers({CORRELATION_HEADER: correlation_id.get()})
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=timeout)
            if r.status_code == 503:
                detail = r.json().get("detail", {})
                if isinstance(detail, dict) and detail.get("error") == "provider_unavailable":
                    raise ProviderUnavailable(detail.get("reason", "provider unavailable"))
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"gateway {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            d = r.json()
            return LLMResult(content=d["content"], prompt_tokens=d.get("prompt_tokens", 0),
                             completion_tokens=d.get("completion_tokens", 0), cost_usd=d.get("cost_usd", 0.0),
                             provider=d.get("provider", ""), degraded=d.get("degraded", False),
                             latency_ms=d.get("latency_ms", 0.0), attempts=attempt + 1, errors=errors)
        except ProviderUnavailable:
            AGENT_RETRIES.labels(agent, "provider_unavailable").inc()
            raise
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
            errors.append(f"{type(e).__name__}: {e}"[:200])
            if attempt + 1 >= attempts:
                break
            AGENT_RETRIES.labels(agent, "llm_transient").inc()
            wait = backoff_s(attempt)
            log(logger, logging.WARNING, "llm call failed, retrying", agent=agent, step=step,
                attempt=attempt + 1, wait_s=round(wait, 2), error=errors[-1])
            sleep(wait)
    raise ProviderUnavailable(f"gave up after {attempts} attempts: {errors[-1] if errors else ''}")


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_json(content: str) -> dict:
    """Parse the model's JSON reply, tolerating code fences and leading prose."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(text)
        if not m:
            raise BadModelOutput("no JSON object in model output")
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise BadModelOutput(f"invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise BadModelOutput("model output is not a JSON object")
    return obj
