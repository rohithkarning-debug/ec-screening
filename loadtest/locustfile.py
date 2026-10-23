"""Load test (D6, A7).

Measures a COMPLETE run - submission to final result - not just the POST.
Each simulated user submits a valid request, polls until the run finishes,
and reports the end-to-end time as a custom request named `full_run`.
Capacities are randomised so the whole-run cache does not short-circuit
the test (set LOCUST_ALLOW_CACHE=1 to measure the cached path instead).

Run inside Compose (nothing runs outside the containers), with the gateway
pointed at the stub LLM:

    docker compose --profile loadtest run --rm locust \
        -f loadtest/locustfile.py --headless -u 10 -r 2 -t 3m \
        --host http://api:8000 --csv loadtest/results/w1_u10
"""
from __future__ import annotations

import os
import random
import time

from locust import HttpUser, between, events, task

POLL_S = 0.5
RUN_TIMEOUT_S = 300
ALLOW_CACHE = os.getenv("LOCUST_ALLOW_CACHE") == "1"


def _body() -> dict:
    cap = 0.5 if ALLOW_CACHE else round(random.uniform(0.1, 0.95), 3)
    return {
        "project_name": "Load test cement unit",
        "sector": "cement",
        "description": "Clinker grinding and cement manufacture for load testing.",
        "capacity": {"value": cap, "unit": "MTPA"},
        "location": {"state": "Karnataka", "district": "Kalaburagi"},
        "protected_area_distance_km": random.choice([15, 25, 40]),
        "protected_area_name": "Test protected area",
    }


class ScreeningUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def full_run(self):
        t0 = time.perf_counter()
        with self.client.post("/v1/screenings", json=_body(), name="POST /v1/screenings",
                              headers={"X-Client-Id": f"locust-{id(self) % 1000}"},
                              catch_response=True) as r:
            if r.status_code not in (200, 202):
                r.failure(f"submit {r.status_code}")
                return
            data = r.json()
        run_id, status = data["run_id"], data["status"]
        while status not in ("done", "failed"):
            if time.perf_counter() - t0 > RUN_TIMEOUT_S:
                self._report(t0, Exception("run timed out"))
                return
            time.sleep(POLL_S)
            with self.client.get(f"/v1/screenings/{run_id}", name="GET /v1/screenings/{id}",
                                 catch_response=True) as g:
                if g.status_code != 200:
                    g.failure(f"poll {g.status_code}")
                    continue
                status = g.json()["status"]
        self._report(t0, None if status == "done" else Exception("run failed"))

    def _report(self, t0: float, exc):
        events.request.fire(request_type="RUN", name="full_run", response_time=(time.perf_counter() - t0) * 1000,
                            response_length=0, exception=exc, context={})
