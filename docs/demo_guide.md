# Demo guide (Evaluation 2)

Commands are for Windows PowerShell in the repository folder. The console is at
`http://localhost:<API_PORT>` (8000 by default). Each section is one demo item.

## Before the demo

```powershell
docker compose up -d --build
docker compose ps                 # everything running; ingest "Exited (0)"
```

- Console rail: every service green, breaker closed, chunks indexed (not "index empty").
- The real model is configured in `.env` (the rail does not say "Running on the stub model").
- Leave about 30 s between model-backed runs: free tiers allow few requests per minute.

## 1. Clean start
`git clone`, `Copy-Item .env.example .env` (then add the model key), `docker compose up --build`.

## 2–7, 10. Screening cases
Screen a project tab, one button each:

| Item | Button | What to point at |
| --- | --- | --- |
| 10 | Section 4.1 case | Category A; citations 3(b) and General Condition; ring diagram |
| 2 | Same project at 15 km | Category B, verified; citations resolve to passage text |
| 5 | Distance not known | No determination made; reason; passages consulted |
| 5 | Sector outside the corpus | Referred without any model call |
| 7 | Prompt injection | Blocked, HTTP 400, no agent run started |
| 4 | Malformed request | HTTP 422, field errors, quarantined; then the Rejected requests tab |
| 6 | Fabricated citation | Open the full trace: attempt 1 fails `citation_not_retrieved`, attempt 2 passes |
| 3 | Any result → Open the full trace | Steps, tools, queries, passages, prompt version, latency, tokens; Open in Jaeger |

## 8. Circuit breaker and degradation path

The path is: primary provider → fallback provider (if configured) → refer to a
human. A run that cannot reach any model is never answered with a guess.

**Part A: breaker opens, fallback keeps serving** (fallback configured in `.env`)
1. Screen tab → Run options → tick **Skip the cache** (a cached answer makes no model
   call, so the outage would never be hit). System tab → Simulate provider outage.
   A red "Simulated outage is on" tag appears.
2. Screen → Same project at 15 km. The result still comes back, tagged
   "Served by the fallback model"; the trace marks each such call "fallback model".
3. System tab: breaker **Open** (red in the rail). Each primary failure counted;
   after `BREAKER_FAIL_THRESHOLD` (5) the gateway stops calling the primary.
4. Grafana → "Circuit breaker" panel jumps to 2; "LLM calls by outcome" shows
   primary errors, then primary breaker_open, while fallback ok rises.
5. System → End outage. After `BREAKER_RESET_SECONDS` (30) the breaker goes
   **Half open**, lets one trial call through, and closes on success.

**Part B: no model available → refer to a human** (optional, shows the last step)
1. In `.env`, blank `LLM_FALLBACK_BASE_URL`, `LLM_FALLBACK_API_KEY`, `LLM_FALLBACK_MODEL`;
   `docker compose up -d`.
2. Simulate provider outage; screen a project twice. Run 1: retries, then
   "model provider unavailable - degraded to human review". Run 2: fails at once
   with "breaker primary open", no call to the provider.
3. End outage, restore the three lines, `docker compose up -d`.

## 9. Load test against the declared target

Target (declared first, `loadtest/SLO.md`): p95 ≤ 60 s for a complete run at 10 users.
The overlay switches the gateway to the stub LLM and lifts the rate limit;
`.env` is not changed.

```powershell
$LT = "-f", "docker-compose.yml", "-f", "docker-compose.loadtest.yml"
docker compose @LT up -d
# the index needs a corpus; while corpus/clauses.yaml is still a template, load the test fixture:
docker compose run --rm -v "${PWD}/tests/fixtures/corpus:/app/fixture:ro" ingest python -m ecs.ingestion.ingest --corpus /app/fixture

# 1 worker, 10 users, 2 minutes
docker compose @LT --profile loadtest run --rm locust -f loadtest/locustfile.py --headless -u 10 -r 2 -t 2m --host http://api:8000 --csv loadtest/results/w1_u10
# 3 workers, same load
docker compose @LT up -d --scale worker=3
docker compose @LT --profile loadtest run --rm locust -f loadtest/locustfile.py --headless -u 10 -r 2 -t 2m --host http://api:8000 --csv loadtest/results/w3_u10
# find where the target fails
docker compose @LT --profile loadtest run --rm locust -f loadtest/locustfile.py --headless -u 20 -r 4 -t 2m --host http://api:8000 --csv loadtest/results/w3_u20
docker compose @LT --profile loadtest run --rm locust -f loadtest/locustfile.py --headless -u 40 -r 8 -t 2m --host http://api:8000 --csv loadtest/results/w3_u40

# back to normal (real model, 1 worker; ingest resets the index to corpus/)
docker compose up -d --scale worker=1
```

During each run, watch Grafana: run time p95 against the 60 s line, queue depth,
CPU per container, workers. Afterwards the console's Quality tab lists every run
with p50, p95 and Met/Missed. Copy the numbers into `loadtest/RESULTS.md` and name
the bottleneck (usually the agent workers: queue depth grows while CPU stays low,
because each worker runs one run at a time and waits on the model).
