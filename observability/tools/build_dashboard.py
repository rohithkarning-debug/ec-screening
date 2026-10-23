"""Generates observability/grafana/dashboards/ecs.json (D5, A6, L5, L6).

    python observability/tools/build_dashboard.py

Edit the panel list here rather than the JSON by hand.
"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prom"}
panels, y = [], 0


def row(title):
    global y
    panels.append({"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "collapsed": False})
    y += 1


def ts(title, exprs, unit="short", w=8, h=8, x=0, legend=True):
    panels.append({
        "type": "timeseries", "title": title, "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"showLegend": legend, "displayMode": "list", "placement": "bottom"}},
        "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": l, "datasource": DS}
                    for i, (e, l) in enumerate(exprs)]})


def stat(title, expr, unit="short", w=4, x=0, h=4):
    panels.append({"type": "stat", "title": title, "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
                   "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
                   "targets": [{"refId": "A", "expr": expr, "datasource": DS}]})


def advance(h=8):
    global y
    y += h


row("Service level: complete runs (submission to result)")
stat("p95 run time (5m)", 'histogram_quantile(0.95, sum by (le) (rate(ecs_run_duration_seconds_bucket[5m])))', "s", x=0)
stat("p50 run time (5m)", 'histogram_quantile(0.50, sum by (le) (rate(ecs_run_duration_seconds_bucket[5m])))', "s", x=4)
stat("Runs / min", 'sum(rate(ecs_runs_total[5m])) * 60', "short", x=8)
stat("Run error rate", 'sum(rate(ecs_runs_total{status="failed"}[5m])) / clamp_min(sum(rate(ecs_runs_total[5m])), 1e-9)', "percentunit", x=12)
stat("Availability (targets up)", 'avg(up{job=~"api|worker|verifier|llm-gateway"})', "percentunit", x=16)
stat("Queue depth", 'ecs_queue_depth', "short", x=20)
advance(4)
ts("Run time p50 / p95 (SLO 60 s)", [
    ('histogram_quantile(0.50, sum by (le) (rate(ecs_run_duration_seconds_bucket[1m])))', "p50"),
    ('histogram_quantile(0.95, sum by (le) (rate(ecs_run_duration_seconds_bucket[1m])))', "p95"),
    ('vector(60)', "SLO")], "s", w=8, x=0)
ts("Throughput by route", [('sum by (route) (rate(ecs_runs_total[1m])) * 60', "{{route}}")], "short", w=8, x=8)
ts("API p50 / p95 latency and 5xx", [
    ('histogram_quantile(0.95, sum by (le) (rate(ecs_http_request_seconds_bucket[1m])))', "p95"),
    ('histogram_quantile(0.50, sum by (le) (rate(ecs_http_request_seconds_bucket[1m])))', "p50"),
    ('sum(rate(ecs_http_requests_total{status=~"5.."}[1m]))', "5xx/s")], "s", w=8, x=16)
advance()

row("Agents (A6)")
ts("Task success rate by agent", [
    ('sum by (agent) (rate(ecs_agent_tasks_total{outcome="success"}[5m])) / clamp_min(sum by (agent) (rate(ecs_agent_tasks_total[5m])), 1e-9)', "{{agent}}")],
   "percentunit", w=8, x=0)
ts("Steps and tool calls per run (avg)", [
    ('sum by (agent) (rate(ecs_agent_steps_per_run_sum[5m])) / clamp_min(sum by (agent) (rate(ecs_agent_steps_per_run_count[5m])), 1e-9)', "steps {{agent}}"),
    ('sum by (agent) (rate(ecs_agent_tool_calls_per_run_sum[5m])) / clamp_min(sum by (agent) (rate(ecs_agent_tool_calls_per_run_count[5m])), 1e-9)', "tool calls {{agent}}")],
   "short", w=8, x=8)
ts("Step latency p95 by agent and step", [
    ('histogram_quantile(0.95, sum by (le, agent, step) (rate(ecs_agent_step_seconds_bucket[5m])))', "{{agent}} {{step}}")],
   "s", w=8, x=16)
advance()
ts("Retry rate by agent and cause", [('sum by (agent, cause) (rate(ecs_agent_retries_total[5m])) * 60', "{{agent}} {{cause}}")],
   "short", w=8, x=0)
ts("Escalations by reason (per min)", [('sum by (reason) (rate(ecs_escalations_total[5m])) * 60', "{{reason}}")], "short", w=8, x=8)
ts("Container uptime", [('time() - process_start_time_seconds{job=~"api|worker|verifier|llm-gateway"}', "{{job}} {{instance}}")],
   "s", w=8, x=16)
advance()

row("Model provider, tokens and cost (A5, L5)")
ts("Circuit breaker (0 closed, 1 half-open, 2 open)", [('max by (provider) (ecs_breaker_state)', "{{provider}}")], "short", w=6, x=0)
ts("LLM calls by outcome", [('sum by (provider, outcome) (rate(ecs_llm_calls_total[1m])) * 60', "{{provider}} {{outcome}}")], "short", w=6, x=6)
ts("Tokens per min by step", [('sum by (agent, step, direction) (rate(ecs_llm_tokens_total[5m])) * 60', "{{agent}} {{step}} {{direction}}")],
   "short", w=6, x=12)
ts("Cost per 100 runs (USD)", [
    ('sum(rate(ecs_llm_cost_usd_total[15m])) / clamp_min(sum(rate(ecs_runs_total[15m])), 1e-9) * 100', "per 100 runs"),
    ('sum by (agent, step) (rate(ecs_llm_cost_usd_total[15m])) / clamp_min(scalar(sum(rate(ecs_runs_total[15m]))), 1e-9) * 100', "{{agent}} {{step}}")],
   "currencyUSD", w=6, x=18)
advance()

row("Quality, guardrails, feedback (L4, L6, D1)")
ts("Live quality (sampled runs, EWMA)", [('ecs_live_quality', "{{metric}}")], "percentunit", w=6, x=0)
ts("Verifier outcomes", [('sum by (outcome) (rate(ecs_verifications_total[5m])) * 60', "{{outcome}}")], "short", w=6, x=6)
ts("Guardrail blocks and quarantine", [
    ('sum by (check, stage) (rate(ecs_guardrail_blocks_total[5m])) * 60', "{{check}} {{stage}}"),
    ('sum by (reason) (rate(ecs_quarantined_total[5m])) * 60', "quarantine {{reason}}"),
    ('rate(ecs_rate_limited_total[5m]) * 60', "rate limited")], "short", w=6, x=12)
ts("Feedback", [('sum by (satisfactory) (ecs_feedback_total)', "satisfactory={{satisfactory}}")], "short", w=6, x=18)
advance()

row("Resources per container and index freshness (D5, D2)")
ts("CPU per container (cores)", [('sum by (name) (rate(container_cpu_usage_seconds_total{name=~".+"}[1m]))', "{{name}}")],
   "short", w=8, x=0)
ts("Memory per container", [('sum by (name) (container_memory_usage_bytes{name=~".+"})', "{{name}}")], "bytes", w=8, x=8)
stat("Index chunks", 'ecs_index_chunks', "short", w=4, x=16)
stat("Index age", 'time() - ecs_index_built_timestamp_seconds', "s", w=4, x=20)
advance(4)
stat("Ingest throughput (chunks/s)", 'ecs_ingest_chunks_per_second', "short", w=4, x=16)
stat("Run cache hit ratio", 'sum(rate(ecs_run_cache_total{result="hit"}[15m])) / clamp_min(sum(rate(ecs_run_cache_total[15m])), 1e-9)', "percentunit", w=4, x=20)
advance(4)

dash = {"title": "ECS - Environmental Clearance Pre-Screening", "uid": "ecs-main", "schemaVersion": 39, "version": 1,
        "refresh": "10s", "time": {"from": "now-30m", "to": "now"}, "tags": ["ecs"], "panels": panels,
        "templating": {"list": []}, "annotations": {"list": []}}
for i, p in enumerate(panels, start=1):
    p["id"] = i
out = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "ecs.json"
out.write_text(json.dumps(dash, indent=1) + "\n")
print(f"wrote {out} with {len(panels)} panels")
