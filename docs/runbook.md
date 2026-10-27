# Runbook (D8)

The two most likely failure modes, then quick checks for the rest.

## 1. Model provider unavailable or rate limited

**Symptom**
- Every new run escalates with "model provider unavailable - degraded to human review".
- Grafana: breaker panel at 2 (open); `ecs_llm_calls_total{outcome="error"}` rising;
  escalations panel shows `provider_unavailable`.

**Confirm**
```bash
curl -s localhost:8000/v1/screenings/<run_id> | jq .escalation_reason
docker compose exec llm-gateway python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/admin/breaker').read().decode())"
docker compose logs --since 10m llm-gateway | grep -E "provider call failed|breaker"
```
Look at the error text: `HTTP 429` = rate limit, `HTTP 401` = bad key,
timeouts or connection errors = provider down or network.

**Recover**
- 429: wait. The breaker half-opens after `BREAKER_RESET_SECONDS` and closes on
  the first success. Lower load (`RATE_LIMIT_PER_MINUTE`), keep the run cache on.
- 401/403: fix `LLM_PROVIDER_API_KEY` in `.env`, then `docker compose up -d llm-gateway`.
- Provider down: set `LLM_FALLBACK_*` in `.env` to a second provider and
  `docker compose up -d llm-gateway`, or wait.
- Escalated runs are safe (no wrong answer was given). Resubmit them after recovery.

## 2. Index empty, stale or unreachable

**Symptom**
- Every run escalates with "sector is outside the indexed corpus" or
  "Schedule item … is not in the indexed corpus".
- Or tool steps in the trace show `ToolError` / connection errors to Chroma.
- Grafana: "Index chunks" = 0, or "Index age" unexpectedly old.

**Confirm**
```bash
curl -s localhost:8000/v1/index | jq '{chunks, clause_ids, built_at}'
docker compose ps chroma ingest
docker compose logs ingest | tail -20
```
`chunks: 0` means `corpus/clauses.yaml` still has placeholders, or the PDFs are
missing or have no text layer (ingest logs say which).

**Recover**
- Fill `corpus/clauses.yaml` / add PDFs, then `docker compose run --rm ingest`
  (only changed chunks are embedded).
- Chroma unhealthy: `docker compose restart chroma`, then `docker compose run --rm ingest`.
- Corrupt index: `docker compose run --rm ingest python -m ecs.ingestion.ingest --rebuild`.

## Other quick checks

| Symptom | Check | Fix |
| --- | --- | --- |
| Runs stay `queued` | `ecs_queue_depth` rising; `docker compose ps worker` | `docker compose up -d --scale worker=3`; check worker logs |
| Runs stuck `running` | Worker restarted mid-run (trace has partial steps) | Resubmit; the stuck record expires after `RUN_TTL_SECONDS` |
| All runs escalate "verifier agent unavailable" | `docker compose ps verifier` | `docker compose restart verifier` |
| 429 for one client | `ecs_rate_limited_total` | Expected; raise `RATE_LIMIT_PER_MINUTE` if legitimate |
| `docker compose up` fails at `.env` | `.env` missing | `cp .env.example .env` |
| Port already in use | Another program on 8000/3000/9090 | Change `API_PORT` etc. in `.env` |
