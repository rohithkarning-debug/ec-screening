# Environmental Clearance Pre-Screening Service

Mini Project 13, UE24AM342AA5 (ML System Design and AgentOps), PES University.

A multi-agent service that takes an Indian project description, validates it,
finds the applicable Schedule row of the EIA Notification, 2006, checks
whether the General Condition or an amendment modifies it, and routes the
project with citations to one of:

- **Category A**: central ministry
- **Category B**: state authority
- **no clearance required**
- **escalate to a human**: when proximity is unknown, amendments conflict, or the sector is outside the corpus

> The output is a screening opinion, never an authoritative determination.
> An accredited consultant decides.

## Run it (from a clean clone)

You need Docker with Compose v2 and about 4 GB of free RAM.

```bash
git clone <this repo> && cd ec-screening
cp .env.example .env
docker compose up --build
```

This starts the following services; nothing runs outside the containers:

- API
- worker
- verifier
- LLM gateway
- stub LLM
- Chroma
- Redis
- ingestion
- Prometheus
- Grafana
- Jaeger
- cAdvisor

Open:

| What | URL |
| --- | --- |
| Web client | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Grafana dashboard | http://localhost:3000 (anonymous view; admin password is in `.env`) |
| Jaeger traces | http://localhost:16686 |
| Prometheus | http://localhost:9090 |

If a port is taken, change `API_PORT`, `GRAFANA_PORT` and the other port variables in `.env`.

### Two things to do before it gives real answers

1. **Corpus.** As cloned, `corpus/clauses.yaml` holds placeholders, so the
   index is empty and every request escalates. Follow `corpus/README.md`:
   download the EIA Notification, 2006 and chosen amendments, paste the
   Schedule rows and the General Condition verbatim, then run
   `docker compose run --rm ingest`.
2. **Model.** `.env` points the gateway at the bundled **stub**, which follows a
   fixed script and cannot reason. It is only for load tests. For real runs,
   set these in `.env` to any OpenAI-compatible provider (a free tier works):
   - `LLM_PROVIDER_BASE_URL`
   - `LLM_PROVIDER_API_KEY`
   - `LLM_MODEL`

   Then run `docker compose up -d llm-gateway`. Keys stay in `.env`, which git ignores.

## Use it

```bash
curl -s -X POST localhost:8000/v1/screenings -H 'Content-Type: application/json' -d '{
  "project_name": "Example cement grinding unit", "sector": "cement",
  "description": "Standalone cement grinding unit.",
  "capacity": {"value": 0.5, "unit": "MTPA"},
  "location": {"state": "Karnataka", "district": "Kalaburagi"},
  "protected_area_distance_km": 2, "protected_area_name": "Example National Park"}'
# -> 202 {"run_id": "run_...", "status_url": "/v1/screenings/run_..."}
curl -s localhost:8000/v1/screenings/<run_id>         # poll until status is done
curl -s localhost:8000/v1/screenings/<run_id>/trace   # full reconstruction of the run
```

The full contract is in `docs/data_contract.md` and served at `GET /v1/contract`.

## Demo (Evaluation 2)

`scripts/demo.sh` has one command per item:

| # | Item | Command |
| --- | --- | --- |
| 1 | Clean start | `git clone … && cp .env.example .env && docker compose up --build` |
| 2 | Valid request with citations | `./scripts/demo.sh valid` |
| 3 | One run traced end to end | `./scripts/demo.sh trace <run_id>`, then Jaeger, tag `run_id` |
| 4 | Malformed request quarantined, no run | `./scripts/demo.sh malformed` |
| 5 | Unanswerable case escalates | `./scripts/demo.sh unknown` or `./scripts/demo.sh outside` |
| 6 | Self-verification catches a bad answer | `./scripts/demo.sh badcite` (needs `DEMO_FAULTS=1`) |
| 7 | Prompt injection | `./scripts/demo.sh injection` |
| 8 | Circuit breaker and degradation | `./scripts/demo.sh breaker`, then `./scripts/demo.sh restore` |
| 9 | Locust vs target, scaling | See "Load test" below and `loadtest/RESULTS.md` |
| 10 | Section 4.1 case | `./scripts/demo.sh case41` (real model required) |

## Scale the agent workers

```bash
docker compose up -d --scale worker=3
```

## Load test

The p95 target is declared in `loadtest/SLO.md` before testing. The gateway
must point at the stub (the default `.env`).

```bash
mkdir -p loadtest/results
docker compose --profile loadtest run --rm locust -f loadtest/locustfile.py --headless \
  -u 10 -r 2 -t 3m --host http://api:8000 --csv loadtest/results/w1_u10
docker compose up -d --scale worker=3
docker compose --profile loadtest run --rm locust -f loadtest/locustfile.py --headless \
  -u 10 -r 2 -t 3m --host http://api:8000 --csv loadtest/results/w3_u10
```

Read the `full_run` row (submission to final result) and fill in `loadtest/RESULTS.md`.
For load tests, set `RATE_LIMIT_PER_MINUTE` high in `.env`.

## Evaluation

With a real model configured:

```bash
docker compose exec api python -m eval.run_eval --api http://localhost:8000 --prompt-version v1 --label v1
docker compose exec api python -m eval.run_eval --api http://localhost:8000 --prompt-version v2 --label v2
docker compose exec api python -m eval.compare eval/results/v1.json eval/results/v2.json
```

See `docs/evaluation.md` for how each metric is computed.

## Tests

The tests need no Docker, no network and no model:

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Repository map

| Path | What |
| --- | --- |
| `ecs/api/` | FastAPI app: contract enforcement, quarantine, rate limiting, feedback |
| `ecs/agents/` | Screener graph, tools, verifier agent |
| `ecs/llm/` | Gateway (breaker, fallback, cost), client (timeouts, retries), stub |
| `ecs/ingestion/` | Corpus pipeline and chunking |
| `ecs/` (top level) | Config, contract, retrieval, cache, guardrails, metrics, tracing, logging, worker |
| `prompts/` | Versioned prompts |
| `corpus/` | Manifest, verified clauses, PDFs (not committed) |
| `eval/` | Evaluation set, runner, comparison |
| `loadtest/` | Locust file, SLO, results |
| `observability/` | Prometheus, Grafana provisioning and dashboard |
| `docs/` | Architecture, contract, agent design, chunking, evaluation, observability, runbook, incident log, report outline |
| `web/` | Web client |

## Team

| Member | SRN | Owns |
| --- | --- | --- |
| Rohith Karning | PES1UG24AM229 | Agents, verification, reliability, guardrails, evaluation (A1, A2, A3, A5, L3, L4) |
| P Manas Siddhardha | PES1UG24AM186 | API, data contract, Compose, tracing, load test, scaling (A4, D1, D3, D5, D6, A7) |
| Rohanth Ganesha C | PES1UG24AM227 | Ingestion, chunking, retrieval, prompts, caching, rate limits, feedback (D2, L1, L2, D7, L6) |
| Raghav Budur | PES1UG23AM225 | Metrics, cost tracking, LLM gateway, web client, config, runbook (A6, L5, A8, D4, D8) |
