# Load test results (D6, A7)

Fill this in after running the tests in `SLO.md`. Copy numbers from the Locust
CSV files in `loadtest/results/` (row `full_run`) and the Grafana dashboard.

**Target (declared in SLO.md before testing):** p95 ≤ 60 s at 10 users.

## Results

| Workers | Users | RPS (runs/s) | p50 (s) | p95 (s) | Target met? | Failed runs |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 10 | | | | | |
| 3 | 10 | | | | | |
| 3 | 20 | | | | | |
| 3 | 40 | | | | | |

## Where the target fails

- Concurrency at which p95 first exceeds 60 s:
- Component responsible (evidence from the dashboard):
- What `--scale worker=3` changed, and why:

## Caching effect (D7)

| Path | p95 (s) | Cost per run (USD) |
| --- | --- | --- |
| Cache off (`RUN_CACHE_ENABLED=0`) | | |
| Cache on, repeated projects (`LOCUST_ALLOW_CACHE=1`) | | |

## Cost per 100 runs (L5)

From `GET /v1/screenings/{id}/trace` totals or the `ecs_llm_cost_usd_total` metric:

- Cost per 100 runs:
- Largest single cost driver (agent and step):
