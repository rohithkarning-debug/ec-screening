# Observability and cost (D5, A6, L5, F8)

| Tool | URL (default ports) | What it shows |
| --- | --- | --- |
| Grafana | http://localhost:3000 (dashboard "ECS") | p50/p95 run time vs SLO, throughput, error rate, availability, per-agent metrics, breaker, tokens and cost, live quality, resources per container, index freshness |
| Prometheus | http://localhost:9090 | Raw metrics; every service scraped, workers found by DNS |
| Jaeger | http://localhost:16686 | One trace per run across api → worker → tools → gateway → verifier |
| Run trace API | `GET /v1/screenings/{id}/trace` | The full reconstruction |
| Web console | http://localhost:8000 → Runs and traces, System | The same trace as a timeline; live service health, breaker, workers |

## Reconstructing a run from its ID

`GET /v1/screenings/{run_id}/trace` returns:

- the validated request and prompt version;
- every step: agent, node, action, tool arguments, query, passage IDs returned,
  cache hit, latency, tokens, cost, errors;
- every passage retrieved, with its text;
- every verification attempt with issue codes and the judge's verdict;
- cost by step and in total, and the final result.

In Jaeger, search service `worker` with tag `run_id=<id>`. Logs are JSON with
`correlation_id` = run ID, so you can also filter with
`docker compose logs worker | grep <run_id>`.

## Per-agent metrics (A6)

| Panel | Metric |
| --- | --- |
| Task success rate | `ecs_agent_tasks_total{agent, outcome}` |
| Steps per run | `ecs_agent_steps_per_run{agent}` |
| Tool calls per run | `ecs_agent_tool_calls_per_run{agent}` |
| Latency end to end | `ecs_run_duration_seconds` |
| Latency per step | `ecs_agent_step_seconds{agent, step}` |
| Retry rate | `ecs_agent_retries_total{agent, cause}` |
| Container uptime | `time() - process_start_time_seconds` |
| Runs reaped after a worker died or hung | `ecs_runs_reaped_total` |

## Cost (L5)

Tokens come from the provider's `usage` field (estimated at 4 characters per
token if missing). Cost is computed at call time from
`LLM_PRICE_INPUT_PER_1M_USD` and `LLM_PRICE_OUTPUT_PER_1M_USD`. Each call is
labelled with the agent and step, so cost is attributed both per step
(`ecs_llm_cost_usd_total{agent, step}`) and per run (trace `cost`). On a free
tier, set the paid list price to report a shadow cost.

**Cost per 100 runs** = sum of `totals.cost_usd` over a batch ÷ runs × 100
(the Grafana panel shows the same from metrics). **Largest single driver**: the
`screener:reason` step, because each screener step resends the growing
observation history; it is typically 4 to 6 calls per run against one
verifier call.
