"""Verifier threshold and amendment rules, stuck-run reaper, console endpoints."""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from ecs import retrieval, runstore
from ecs.agents import verifier
from ecs.contract import ScreeningRequest
from tests.conftest import valid_request


def _req(**over):
    return ScreeningRequest.model_validate(valid_request(**over)).normalized()


def _retrieved():
    out = {}
    for cid in ("3(b)", "GC"):
        for p in retrieval.get_clause(cid):
            out[p["passage_id"]] = p
    return out


def _ids(retrieved, clause):
    return [pid for pid, p in retrieved.items() if p.get("clause_id") == clause]


def _codes(result):
    return [i["code"] for i in result["issues"]]


# ---------------------------------------------------------------- threshold rule
def test_threshold_rule_catches_b_above_threshold(indexed):
    ret = _retrieved()
    draft = {"category": "B", "route": "category_b_state", "basis": "Row 3(b) places it in Category B.",
             "citations": _ids(ret, "3(b)") + _ids(ret, "GC")}
    out = verifier.verify("r", _req(capacity={"value": 2.0, "unit": "MTPA"}, protected_area_distance_km=40),
                          draft, ret)
    assert "threshold_mismatch" in _codes(out) and not out["ok"]


def test_threshold_rule_catches_a_below_threshold_without_modifier(indexed):
    ret = _retrieved()
    draft = {"category": "A", "route": "category_a_central", "basis": "Row 3(b).",
             "citations": _ids(ret, "3(b)")}
    out = verifier.verify("r", _req(protected_area_distance_km=40), draft, ret)
    assert "threshold_mismatch" in _codes(out)


def test_threshold_rule_allows_a_via_general_condition(indexed, monkeypatch):
    from ecs.llm import client as llm

    monkeypatch.setattr(llm, "chat", lambda *a, **k: llm.LLMResult(json.dumps({"supported": True}), 10, 5))
    ret = _retrieved()
    draft = {"category": "A", "route": "category_a_central", "conditions_applied": ["GC"],
             "basis": "SYNTHETIC ROW 3(b) cement below 1.0 million tonnes per annum is Category B. The "
                      "SYNTHETIC GENERAL CONDITION treats Category B within 10 km of a protected area as Category A.",
             "citations": _ids(ret, "3(b)") + _ids(ret, "GC")}
    out = verifier.verify("r", _req(), draft, ret)
    assert out["ok"], out["issues"]


def test_row_threshold_is_conservative():
    assert verifier._row_threshold("Category A >= 1.0 million tonnes; B < 1.0 million tonnes", "MTPA") == 1.0
    assert verifier._row_threshold(">= 500 MW (coal); >= 50 MW (pet coke)", "MW") is None  # ambiguous
    assert verifier._row_threshold("no numbers here", "MTPA") is None
    assert verifier._row_threshold("20,000 sq.m and 1,50,000 sq.m", "SQM") is None


# ---------------------------------------------------------------- amendment rule
def test_uncited_amendment_to_same_item_fails_verification(indexed):
    ret = _retrieved()
    ret["amend:3(b):x"] = {"passage_id": "amend:3(b):x", "clause_id": "3(b)", "type": "amendment",
                           "text": "In the Schedule, against item 3(b), the threshold shall be substituted."}
    draft = {"category": "B", "route": "category_b_state", "basis": "Row 3(b).",
             "citations": _ids(ret, "3(b)") + _ids(ret, "GC")}
    out = verifier.verify("r", _req(protected_area_distance_km=40), draft, ret)
    assert "amendment_not_considered" in _codes(out)
    assert "escalate" in out["suggested_strategy"]


def test_unrelated_amendment_is_ignored(indexed):
    ret = _retrieved()
    ret["amend:1(a):x"] = {"passage_id": "amend:1(a):x", "clause_id": "1(a)", "type": "amendment",
                           "text": "Against item 1(a) the following shall be substituted."}
    draft = {"category": "B", "route": "category_b_state", "basis": "SYNTHETIC ROW 3(b) cement Category B.",
             "citations": _ids(ret, "3(b)") + _ids(ret, "GC")}
    out = verifier.verify("r", _req(protected_area_distance_km=40), draft, ret)
    assert "amendment_not_considered" not in _codes(out)


# ---------------------------------------------------------------- reaper
def test_reaper_escalates_stuck_run(redis_client, monkeypatch):
    from ecs import worker

    monkeypatch.setenv("STUCK_RUN_SECONDS", "60")
    rec = runstore.create_run("run_stuck", {"sector": "cement"}, "c", "v2")
    rec["status"] = "running"
    runstore.save_run(rec)
    redis_client.zadd(runstore.ACTIVE_KEY, {"run_stuck": time.time() - 120})
    redis_client.hset(runstore.ACTIVE_OWNER_KEY, "run_stuck", "dead-worker")
    runstore.mark_active("run_fresh", "w2")  # just started: must be left alone
    assert worker.reap_stuck(redis_client) == ["run_stuck"]
    got = runstore.get_run("run_stuck")
    assert got["status"] == "failed" and got["result"]["route"] == "escalate_to_human"
    assert "worker stopped" in got["result"]["escalation_reason"]
    assert redis_client.zscore(runstore.ACTIVE_KEY, "run_fresh") is not None


def test_late_finish_does_not_overwrite_reaped_run(indexed, redis_client, monkeypatch):
    from ecs import worker
    from ecs.llm import client as llm

    def boom(*a, **k):
        # the run gets reaped while the "model call" is in flight
        rec = runstore.get_run("run_late")
        rec["result"] = {"route": "escalate_to_human", "escalation_code": "run_reaped"}
        rec["status"] = "failed"
        runstore.save_run(rec)
        raise llm.ProviderUnavailable("x")
    monkeypatch.setattr(llm, "chat", boom)
    runstore.create_run("run_late", _req(), "c", "v2")
    worker.process({"run_id": "run_late"})
    assert runstore.get_run("run_late")["result"]["escalation_code"] == "run_reaped"


def test_heartbeat(redis_client):
    from ecs import worker

    worker.heartbeat_once(redis_client)
    hb = json.loads(redis_client.get(worker.HEARTBEAT_PREFIX + worker.WORKER_ID))
    assert hb["worker_id"] == worker.WORKER_ID


# ---------------------------------------------------------------- console endpoints
@pytest.fixture
def api(redis_client):
    from ecs.api.main import app

    with TestClient(app) as c:
        yield c


def test_recent_runs_newest_first(api):
    for i in range(3):
        api.post("/v1/screenings", json=valid_request(capacity={"value": 0.1 + i / 10, "unit": "MTPA"}))
    runs = api.get("/v1/runs/recent?limit=2").json()["runs"]
    assert len(runs) == 2 and runs[0]["submitted_at"] >= runs[1]["submitted_at"]
    assert runs[0]["sector"] == "cement" and runs[0]["status"] == "queued"


def test_console_config(api, monkeypatch):
    monkeypatch.setenv("GRAFANA_PORT", "3333")
    d = api.get("/v1/console/config").json()
    assert d["links"]["grafana"].endswith(":3333/d/ecs-main")
    assert d["model_mode"] in ("stub", "real") and "v2" in d["prompt_versions"]


def test_system_status_reports_each_service(api, redis_client, chroma, monkeypatch):
    from ecs.api import console
    from ecs import worker

    def fake_get(url, timeout=2.0):
        req = httpx.Request("GET", url)
        if "gateway" in url:
            return httpx.Response(200, json={"status": "ok", "breakers": {"primary": {"state": "open"}}},
                                  request=req)
        return httpx.Response(503, json={}, request=req)
    monkeypatch.setattr(console.httpx, "get", fake_get)
    worker.heartbeat_once(redis_client)
    d = api.get("/v1/system/status").json()
    assert d["services"]["redis"]["ok"] and d["services"]["chroma"]["ok"]
    assert d["services"]["llm-gateway"]["ok"] and not d["services"]["verifier"]["ok"]
    assert d["breakers"]["primary"] == "open" and d["services"]["worker"]["count"] == 1


def test_admin_faults_guarded(api, monkeypatch):
    from ecs.api import console

    monkeypatch.setenv("DEMO_FAULTS", "0")
    assert api.post("/v1/admin/faults", json={"provider_down": True}).status_code == 403
    monkeypatch.setenv("DEMO_FAULTS", "1")
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    assert api.post("/v1/admin/faults", json={"provider_down": True}).status_code == 401
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(url=url, body=json)
        return httpx.Response(200, json=json, request=httpx.Request("POST", url))
    monkeypatch.setattr(console.httpx, "post", fake_post)
    r = api.post("/v1/admin/faults", json={"provider_down": True}, headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 200 and seen["body"] == {"provider_down": True}
    assert seen["url"].endswith("/admin/faults")


def test_results_endpoints_read_saved_files(api, tmp_path, monkeypatch):
    from ecs.api import console

    (tmp_path / "eval").mkdir()
    (tmp_path / "load").mkdir()
    (tmp_path / "eval" / "v2.json").write_text(json.dumps({
        "label": "v2", "prompt_version": "v2", "ran_at": 1, "summary": {"route_accuracy": 0.9},
        "rows": [{"id": "s01", "passed": True, "route": "category_a_central", "expected_route": "category_a_central"}]}))
    (tmp_path / "load" / "w1_u10_stats.csv").write_text(
        "Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,Min,Max,"
        "Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%\n"
        "RUN,full_run,120,0,5100,5000,1500,9000,0,0.8,0,5100,5600,5600,5600,6100,6600,6600,6600,6600,6600,6600\n")
    monkeypatch.setattr(console, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(console, "LOAD_DIR", tmp_path / "load")
    ev = api.get("/v1/results/eval").json()["results"][0]
    assert ev["label"] == "v2" and ev["scenarios"][0]["passed"]
    lt = api.get("/v1/results/loadtest").json()["results"][0]
    assert lt == {"run": "w1_u10", "requests": 120, "failures": 0, "rps": 0.8, "p50_s": 5.1, "p95_s": 6.6}


def test_run_cost_counts_each_model_call_once(indexed, redis_client, monkeypatch):
    from ecs import worker
    from ecs.agents import graph as g
    from ecs.llm import client as llm
    from tests.test_agents import FETCH_GC, FETCH_ROW, ScriptedLLM, final

    monkeypatch.setattr(llm, "chat", ScriptedLLM([FETCH_ROW, FETCH_GC, final("A", "category_a_central", ["3(b)", "GC"])]))
    monkeypatch.setattr(g, "_call_verifier",
                        lambda s: verifier.verify(s["run_id"], s["request"], s["draft"], s["retrieved"]))
    runstore.create_run("run_cost", _req(), "c", "v2")
    rec = worker.process({"run_id": "run_cost"})
    assert rec["result"]["route"] == "category_a_central"
    assert rec["totals"]["llm_calls"] == 4          # 3 screener steps + 1 verifier judge
    assert rec["totals"]["prompt_tokens"] == 3 * 500 + 100


def test_category_a_by_capacity_mentioning_gc_is_not_flagged(indexed, monkeypatch):
    """Regression: a correct A-by-capacity answer that mentions the GC only to say it does not
    apply was escalated (modifier_not_cited false positive). See docs/incident_log.md."""
    from ecs.llm import client as llm

    monkeypatch.setattr(llm, "chat", lambda *a, **k: llm.LLMResult(json.dumps({"supported": True}), 10, 5))
    ret = _retrieved()
    draft = {"category": "A", "route": "category_a_central", "conditions_applied": [],
             "basis": "SYNTHETIC ROW 3(b): 2.0 million tonnes per annum is at or above 1.0 million tonnes, "
                      "Category A. The general condition does not change this.",
             "citations": _ids(ret, "3(b)")}
    out = verifier.verify("r", _req(capacity={"value": 2.0, "unit": "MTPA"}, protected_area_distance_km=30),
                          draft, ret)
    assert out["ok"], out["issues"]


def test_gc_listed_as_applied_must_be_cited(indexed):
    ret = _retrieved()
    draft = {"category": "A", "route": "category_a_central", "conditions_applied": ["GC"],
             "basis": "Row 3(b) and the General Condition.", "citations": _ids(ret, "3(b)")}
    out = verifier.verify("r", _req(), draft, ret)
    assert "modifier_not_cited" in _codes(out)


def test_escalation_by_agent_lists_consulted_passages(indexed, monkeypatch):
    from ecs.agents import graph as g
    from ecs.llm import client as llm
    from tests.test_agents import FETCH_GC, FETCH_ROW, ScriptedLLM

    esc = {"action": "final", "answer": {"route": "escalate_to_human", "category": None, "citations": [],
                                         "conditions_applied": ["GC"], "basis": "Distance unknown.",
                                         "escalation_reason": "protected-area distance is unknown"}}
    monkeypatch.setattr(llm, "chat", ScriptedLLM([FETCH_ROW, FETCH_GC, esc]))
    monkeypatch.setattr(g, "_call_verifier",
                        lambda s: verifier.verify(s["run_id"], s["request"], s["draft"], s["retrieved"]))
    out = g.build_graph().invoke(g.initial_state("r", _req(protected_area_distance_km=None,
                                                         protected_area_name=None), "v2"))
    res = out["result"]
    assert res["route"] == "escalate_to_human" and res["conditions_applied"] == []
    assert {c["clause_id"] for c in res["citations"]} == {"3(b)", "GC"}


def test_verifier_prompt_v2_is_default():
    from ecs import prompts
    from ecs.config import settings

    assert settings.verifier_prompt_version == "v2"
    assert "does not apply" in prompts.load("verifier", "v2").system


def test_editing_a_prompt_invalidates_the_run_cache(redis_client, monkeypatch):
    from ecs import cache, prompts
    from ecs.api.main import app

    with TestClient(app) as c:
        captured = []
        real = cache.run_cache_key
        monkeypatch.setattr(cache, "run_cache_key", lambda req, pk, iv: captured.append(pk) or real(req, pk, iv))
        c.post("/v1/screenings", json=valid_request())
    assert prompts.load("screener", "v2").tag in captured[0] and "verifier@" in captured[0]


def test_blank_interstate_distance_is_not_unknown():
    from ecs import prompts

    text = prompts.load("screener", "v2").system
    assert "interstate_boundary_distance_km is optional" in text
    field = ScreeningRequest.model_json_schema()["properties"]["interstate_boundary_distance_km"]
    assert "Only protected_area_distance_km = null means unknown" in field["description"]


def test_worker_warms_up_embeddings(chroma):
    t = retrieval.warm_up()
    assert t >= 0


def test_result_marks_fallback_model_use(indexed, redis_client, monkeypatch):
    from ecs import worker
    from ecs.agents import graph as g
    from ecs.llm import client as llm
    from tests.test_agents import FETCH_GC, FETCH_ROW, ScriptedLLM, final

    scripted = ScriptedLLM([FETCH_ROW, FETCH_GC, final("A", "category_a_central", ["3(b)", "GC"])])

    def via_fallback(*a, **k):
        res = scripted(*a, **k)
        res.provider, res.degraded = "fallback", True
        return res
    monkeypatch.setattr(llm, "chat", via_fallback)
    monkeypatch.setattr(g, "_call_verifier",
                        lambda s: verifier.verify(s["run_id"], s["request"], s["draft"], s["retrieved"]))
    runstore.create_run("run_fb", _req(), "c", "v2")
    rec = worker.process({"run_id": "run_fb"})
    assert rec["result"]["fallback_model_used"] is True
    assert any((s.get("llm") or {}).get("degraded") for s in rec["steps"])


def test_cache_bypass_header_and_source_run(redis_client):
    from ecs import cache
    from ecs.api.main import app
    from ecs.queue import QUEUE_KEY

    with TestClient(app) as c:
        first = c.post("/v1/screenings", json=valid_request()).json()
        job = json.loads(redis_client.lpop(QUEUE_KEY))
        cache.put_run(redis_client, job["cache_key"], {"route": "category_b_state", "verified": True,
                                                       "source_run_id": first["run_id"]})
        hit = c.post("/v1/screenings", json=valid_request())
        assert hit.status_code == 200 and hit.json()["cache_hit"] is True
        assert c.get(f"/v1/screenings/{hit.json()['run_id']}/trace").json()["cached_from_run"] == first["run_id"]
        miss = c.post("/v1/screenings", json=valid_request(), headers={"X-Cache-Bypass": "1"})
        assert miss.status_code == 202 and redis_client.llen(QUEUE_KEY) == 1
