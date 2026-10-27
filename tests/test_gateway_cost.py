"""LLM gateway, token and cost accounting, per-step attribution (L5, A5, A6)."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from ecs import cost


def test_call_cost():
    assert cost.call_cost(1_000_000, 0, 0.5, 1.0) == 0.5
    assert cost.call_cost(1000, 1000, 0.59, 0.79) == pytest.approx(0.00138)


def test_summarize_run_costs_by_step():
    steps = [
        {"agent": "screener", "node": "reason", "llm": {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": 0.01}},
        {"agent": "screener", "node": "tool"},
        {"agent": "screener", "node": "reason", "llm": {"prompt_tokens": 200, "completion_tokens": 20, "cost_usd": 0.02}},
        {"agent": "verifier", "node": "judge", "llm": {"prompt_tokens": 50, "completion_tokens": 5, "cost_usd": 0.005}},
    ]
    s = cost.summarize_run_costs(steps)
    assert s["by_step"]["screener:reason"]["calls"] == 2
    assert s["total"]["cost_usd"] == pytest.approx(0.035)
    assert s["total"]["prompt_tokens"] == 350


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("BREAKER_FAIL_THRESHOLD", "2")
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_1M_USD", "1")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_1M_USD", "2")
    monkeypatch.setenv("DEMO_FAULTS", "1")
    from ecs.llm import gateway as gw

    gw.breakers.clear()
    gw.FAULTS["provider_down"] = False
    with TestClient(gw.app) as c:
        yield c, gw


def _ok_provider(monkeypatch, gw):
    async def fake(p, req):
        return {"content": '{"ok": true}', "prompt_tokens": 1000, "completion_tokens": 500}
    monkeypatch.setattr(gw, "_call_provider", fake)


def test_gateway_records_cost(gateway, monkeypatch):
    c, gw = gateway
    _ok_provider(monkeypatch, gw)
    r = c.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}], "agent": "screener", "step": "reason"})
    assert r.status_code == 200
    assert r.json()["cost_usd"] == pytest.approx(0.002)
    assert 'ecs_llm_cost_usd_total{agent="screener",step="reason"}' in c.get("/metrics").text


def test_gateway_breaker_opens_and_degrades(gateway):
    c, gw = gateway
    c.post("/admin/faults", json={"provider_down": True})
    body = {"messages": [{"role": "user", "content": "hi"}], "agent": "screener", "step": "reason"}
    assert c.post("/v1/chat", json=body).status_code == 502   # retryable
    assert c.post("/v1/chat", json=body).status_code == 502
    r = c.post("/v1/chat", json=body)                          # breaker now open
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_unavailable"
    assert c.get("/admin/breaker").json()["primary"]["state"] == "open"


def test_gateway_uses_fallback_when_primary_open(gateway, monkeypatch):
    c, gw = gateway
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "http://fallback.test/v1")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "fallback-model")

    async def fake(p, req):
        if p["name"] == "primary":
            raise gw.ProviderError("down")
        return {"content": "{}", "prompt_tokens": 10, "completion_tokens": 5}
    monkeypatch.setattr(gw, "_call_provider", fake)
    r = c.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["provider"] == "fallback" and r.json()["degraded"] is True


def test_fault_injection_requires_flag(gateway, monkeypatch):
    c, gw = gateway
    monkeypatch.setenv("DEMO_FAULTS", "0")
    assert c.post("/admin/faults", json={"provider_down": True}).status_code == 403


def test_metrics_module_names():
    from ecs import metrics

    names = {m._name for m in vars(metrics).values() if hasattr(m, "_name")}
    for required in ("ecs_agent_tasks", "ecs_agent_steps_per_run", "ecs_agent_tool_calls_per_run",
                     "ecs_agent_step_seconds", "ecs_agent_retries", "ecs_run_duration_seconds"):
        assert required in names
