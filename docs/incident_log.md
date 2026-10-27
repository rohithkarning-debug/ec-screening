# Incident log (D8)

Record real failures as they happen, newest first. One genuine entry is
required. Write it the day it happens; it is hard to reconstruct later.

| Date | What happened | Impact | Detection | Root cause | Fix | Follow-up |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-23 | During integration testing with the provider forced down, the circuit breaker never opened: after 3 runs it still showed `closed` with 2 consecutive failures | Every run during an outage still called the dead provider once, and each run escalated at once on a single transient error, with no retry | Breaker demo (gateway `/admin/breaker` showed `closed` after repeated failures) | Gateway returned 503 `provider_unavailable` after any failed call; the client treats that as final and does not retry, so the breaker saw one failure per run | Gateway now returns 502 `provider_error` (retryable) when a provider call fails with the breaker closed, and 503 only when every breaker is open. Retest: run 1 retried 3 times and opened the breaker; run 2 failed fast with "breaker open" | Covered by `test_gateway_breaker_opens_and_degrades` and `test_client_retries_transient_then_gives_up` |
| 2026-10-23 | API failed to start in local testing: `[Errno 98] address already in use` on port 8000; worker metrics port also taken | Service unavailable until moved | Startup log | Another process on the development machine already used 8000 and 9101 | Host ports made configurable in `.env` (`API_PORT`, `GRAFANA_PORT`, …) | Runbook "Port already in use" row |
| | *(add your own entries below as you develop and test)* | | | | | |
