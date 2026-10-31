"""Prometheus metrics for every service (A6, F8, D5, L5).

All metric names start with `ecs_`. Per-agent metrics carry an `agent`
label (`screener` or `verifier`) so the dashboard can show task success rate,
steps and tool calls per run, step latency and retry rate per agent.
Container uptime comes from `process_start_time_seconds`, which the client
library exports for every process.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

RUN_BUCKETS = (1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300)
STEP_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60)
COUNT_BUCKETS = (0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20)

# --- API ----------------------------------------------------------------------
HTTP_REQUESTS = Counter("ecs_http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
HTTP_LATENCY = Histogram("ecs_http_request_seconds", "HTTP request latency", ["service", "route"],
                         buckets=STEP_BUCKETS)
SUBMISSIONS = Counter("ecs_submissions_total", "Screening submissions by outcome", ["outcome"])
QUARANTINED = Counter("ecs_quarantined_total", "Requests rejected at the boundary", ["reason"])
RATE_LIMITED = Counter("ecs_rate_limited_total", "Requests rejected by the rate limiter")
QUEUE_DEPTH = Gauge("ecs_queue_depth", "Jobs waiting in the queue")

# --- runs (end to end, submission -> final result) ------------------------------
RUNS = Counter("ecs_runs_total", "Completed runs by route and status", ["route", "status"])
RUN_DURATION = Histogram("ecs_run_duration_seconds", "Run time from submission to final result",
                         buckets=RUN_BUCKETS)
RUN_CACHE = Counter("ecs_run_cache_total", "Whole-run cache lookups", ["result"])
RUNS_IN_PROGRESS = Gauge("ecs_runs_in_progress", "Runs currently executing in this worker")
RUNS_REAPED = Counter("ecs_runs_reaped_total", "Runs left 'running' by a dead or hung worker and escalated")

# --- per agent (A6) -------------------------------------------------------------
AGENT_TASKS = Counter("ecs_agent_tasks_total", "Agent tasks by outcome", ["agent", "outcome"])
AGENT_STEPS = Histogram("ecs_agent_steps_per_run", "Agent steps per run", ["agent"], buckets=COUNT_BUCKETS)
AGENT_TOOL_CALLS = Histogram("ecs_agent_tool_calls_per_run", "Tool calls per run", ["agent"],
                             buckets=COUNT_BUCKETS)
AGENT_STEP_LATENCY = Histogram("ecs_agent_step_seconds", "Latency per agent step", ["agent", "step"],
                               buckets=STEP_BUCKETS)
AGENT_RETRIES = Counter("ecs_agent_retries_total", "Retries by agent and cause", ["agent", "cause"])
TOOL_CALLS = Counter("ecs_tool_calls_total", "Tool calls by tool and outcome", ["tool", "outcome"])
ESCALATIONS = Counter("ecs_escalations_total", "Escalations by reason", ["reason"])
VERIFICATIONS = Counter("ecs_verifications_total", "Verifier outcomes", ["outcome"])

# --- LLM, tokens and cost (L5) ---------------------------------------------------
LLM_CALLS = Counter("ecs_llm_calls_total", "LLM calls by provider and outcome", ["provider", "outcome"])
LLM_LATENCY = Histogram("ecs_llm_call_seconds", "Provider call latency", ["provider"], buckets=STEP_BUCKETS)
LLM_TOKENS = Counter("ecs_llm_tokens_total", "Tokens by direction, agent and step", ["direction", "agent", "step"])
LLM_COST = Counter("ecs_llm_cost_usd_total", "Cost in USD by agent and step", ["agent", "step"])
BREAKER_STATE = Gauge("ecs_breaker_state", "Circuit breaker state (0 closed, 1 half-open, 2 open)", ["provider"])
BREAKER_OPENED = Counter("ecs_breaker_opened_total", "Times the breaker opened", ["provider"])

# --- guardrails (L4) --------------------------------------------------------------
GUARDRAIL_BLOCKS = Counter("ecs_guardrail_blocks_total", "Guardrail interventions", ["check", "stage"])

# --- quality and feedback (L6) ----------------------------------------------------
FEEDBACK = Counter("ecs_feedback_total", "Caller feedback", ["satisfactory"])
LIVE_QUALITY = Gauge("ecs_live_quality", "Rolling live-run quality score", ["metric"])
LIVE_SCORED = Counter("ecs_live_scored_total", "Live runs scored automatically")

# --- index freshness (D2) ---------------------------------------------------------
INDEX_CHUNKS = Gauge("ecs_index_chunks", "Chunks in the vector index")
INDEX_BUILT_AT = Gauge("ecs_index_built_timestamp_seconds", "When the index was last built")
INDEX_DOCS = Gauge("ecs_index_documents", "Documents in the vector index")
INGEST_THROUGHPUT = Gauge("ecs_ingest_chunks_per_second", "Throughput of the last ingestion run")
