# Service level objective (D6)

**Declared before any load test was run.** This file is committed before the
first Locust run; the commit history shows the order. Do not edit the target
after seeing results. Record results in `loadtest/RESULTS.md`.

## Target

| Item | Value |
| --- | --- |
| Measured quantity | Complete run time: from `POST /v1/screenings` to the poll that returns `done` or `failed` (Locust request `full_run`) |
| Objective | **p95 ≤ 60 s** for a complete run |
| Load at which it must hold | 10 concurrent users, each submitting a new project every 1–3 s after the previous run finishes |
| LLM | Stub service, median 1.5 s per call (lognormal), same number of calls as the real agent path |
| Error budget | ≤ 1% of runs `failed` (escalations are valid outcomes, not errors) |

## Why 60 seconds

Section 1 of the brief: screening sits in front of a clearance process measured
in months, so nobody needs this answer in milliseconds. What matters is that
the person submitting a project can wait for the opinion in one sitting
instead of coming back later. About a minute is the longest wait that still
works as "submit and watch it finish" in the web client.

A typical run makes 4–6 model calls (3–4 screener steps plus the verifier),
about 6–9 s of model time with the stub. A verification retry roughly doubles
that. A 60 s p95 leaves room for one retry plus queueing at the declared
concurrency, but not for a backlog. If p95 rises above it, the queue is
growing faster than workers drain it, and that is the signal to scale.

## How it is measured

The stub LLM is switched in by the `docker-compose.loadtest.yml` overlay;
exact commands are in docs/demo_guide.md ("Load test"). Runs: 1 worker and
3 workers at 10 users, then 3 workers at 20, 40 and 80 users.

Then step users up (10, 20, 40, 80) to find the concurrency at which the
target fails, and name the component responsible (queue depth, worker CPU,
gateway, Chroma, Redis) from the Grafana dashboard.
