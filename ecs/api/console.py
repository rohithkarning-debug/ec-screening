"""Read-only endpoints (plus one guarded admin action) behind the web console (A8, F8).

GET  /v1/console/config     links to Grafana / Jaeger / Prometheus, demo flags, prompt versions, model mode
GET  /v1/system/status      every service's health, breaker state, workers alive, queue, index, active runs
GET  /v1/runs/recent        newest runs, one line each
GET  /v1/results/eval       saved evaluation summaries (eval/results/*.json)
GET  /v1/results/loadtest   saved Locust results, `full_run` row of each loadtest/results/*_stats.csv
POST /v1/admin/faults       simulate a provider outage through the LLM gateway (demo item 8)

The admin action needs DEMO_FAULTS=1, and X-Admin-Token when ADMIN_TOKEN is
set. Nothing here exposes a provider URL, key or prompt text.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from ecs import prompts, retrieval, runstore
from ecs.api import quarantine
from ecs.config import settings
from ecs.queue import depth

router = APIRouter()
ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "results"
LOAD_DIR = ROOT / "loadtest" / "results"
HEARTBEAT_PREFIX = "worker:hb:"


def _get_json(url: str, timeout: float = 2.0) -> tuple[bool, dict]:
    try:
        r = httpx.get(url, timeout=timeout)
        return r.status_code < 400, (r.json() if r.headers.get("content-type", "").startswith("application/json")
                                     else {})
    except Exception as e:
        return False, {"error": type(e).__name__}


@router.get("/v1/console/config")
def console_config(request: Request):
    host = request.url.hostname or "localhost"
    link = lambda var, default, path="": f"http://{host}:{settings.public_port(var, default)}{path}"  # noqa: E731
    model = (settings.llm_model_or_none or "").strip()
    return {
        "links": {"grafana": link("GRAFANA_PORT", 3000, "/d/ecs-main"),
                  "jaeger": link("JAEGER_UI_PORT", 16686, "/search?service=worker"),
                  "prometheus": link("PROMETHEUS_PORT", 9090, "/targets"),
                  "api_docs": "/docs", "metrics": "/metrics"},
        "demo_faults": settings.demo_faults,
        "admin_token_required": bool(settings.admin_token),
        "prompt_versions": prompts.available("screener"),
        "default_prompt_version": settings.prompt_version,
        "model_mode": "stub" if model in ("", "stub") else "real",
    }


@router.get("/v1/system/status")
def system_status():
    r = runstore.get_redis()
    services = {}
    try:
        r.ping()
        services["redis"] = {"ok": True}
    except Exception as e:
        services["redis"] = {"ok": False, "detail": type(e).__name__}
    try:
        retrieval.chroma_client().heartbeat()
        services["chroma"] = {"ok": True}
    except Exception as e:
        services["chroma"] = {"ok": False, "detail": type(e).__name__}
    ok, gw = _get_json(settings.llm_gateway_url.rstrip("/") + "/health")
    services["llm-gateway"] = {"ok": ok}
    ok, _ = _get_json(settings.verifier_url.rstrip("/") + "/health")
    services["verifier"] = {"ok": ok}

    breakers = {k: (v.get("state") if isinstance(v, dict) else v) for k, v in (gw.get("breakers") or {}).items()}
    workers = []
    for key in r.scan_iter(HEARTBEAT_PREFIX + "*"):
        raw = r.get(key)
        if raw:
            hb = json.loads(raw)
            workers.append({"worker_id": hb.get("worker_id"), "run_id": hb.get("run_id"),
                            "busy_for_s": round(time.time() - hb["since"], 1) if hb.get("since") else None,
                            "runs_done": hb.get("runs_done"), "uptime_s": round(time.time() - hb.get("started", 0))})
    services["worker"] = {"ok": bool(workers), "count": len(workers)}
    meta = retrieval.index_meta(r)
    return {
        "ts": time.time(),
        "services": services,
        "breakers": breakers or {"primary": "unknown"},
        "workers": sorted(workers, key=lambda w: w["worker_id"] or ""),
        "queue_depth": depth(),
        "active_runs": int(r.zcard(runstore.ACTIVE_KEY)),
        "quarantined": quarantine.count(),
        "index": {"chunks": meta.get("chunks", 0), "clause_ids": meta.get("clause_ids", []),
                  "built_at": meta.get("built_at"), "documents": len(meta.get("documents", [])),
                  "embedding_backend": meta.get("embedding_backend")},
        "demo_faults": settings.demo_faults,
        "outage_simulated": bool((gw.get("faults") or {}).get("provider_down")),
    }


@router.get("/v1/runs/recent")
def recent_runs(limit: int = 20):
    return {"runs": runstore.recent(min(max(limit, 1), 100))}


@router.get("/v1/results/eval")
def eval_results():
    out = []
    for f in sorted(EVAL_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text())
            out.append({"file": f.name, "label": d.get("label"), "prompt_version": d.get("prompt_version"),
                        "ran_at": d.get("ran_at"), "summary": d.get("summary"),
                        "scenarios": [{"id": r.get("id"), "passed": r.get("passed"), "route": r.get("route"),
                                       "expected": r.get("expected_route")} for r in d.get("rows", [])]})
        except Exception:
            continue
    return {"results": out}


@router.get("/v1/results/loadtest")
def loadtest_results():
    out = []
    for f in sorted(LOAD_DIR.glob("*_stats.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with f.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("Name") == "full_run":
                        out.append({"run": f.name.replace("_stats.csv", ""), "requests": int(row["Request Count"]),
                                    "failures": int(row["Failure Count"]),
                                    "rps": round(float(row["Requests/s"]), 3),
                                    "p50_s": round(float(row["50%"]) / 1000, 2),
                                    "p95_s": round(float(row["95%"]) / 1000, 2)})
        except Exception:
            continue
    return {"slo_p95_s": 60, "results": out}


@router.post("/v1/admin/faults")
def set_faults(body: dict, x_admin_token: str | None = Header(default=None)):
    if not settings.demo_faults:
        raise HTTPException(403, "fault injection is disabled (set DEMO_FAULTS=1)")
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(401, "missing or wrong X-Admin-Token")
    want = {"provider_down": bool(body.get("provider_down", False))}
    try:
        r = httpx.post(settings.llm_gateway_url.rstrip("/") + "/admin/faults", json=want, timeout=3)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"gateway did not accept the change: {type(e).__name__}")
