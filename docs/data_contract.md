# Data contract (D1, F1, F2)

Version **1.0.0**. Machine-readable schema: `contracts/screening_request.schema.json`,
also served at `GET /v1/contract`. Source of truth: `ecs/contract.py`.

## `POST /v1/screenings`

| Field | Type | Required | Rule |
| --- | --- | --- | --- |
| `project_name` | string | yes | 3–200 chars, no control characters |
| `sector` | enum | yes | `mining_minerals`, `thermal_power`, `cement`, `petroleum_refining`, `building_construction`, `township`, `other` |
| `description` | string | yes | 1–2000 chars; prompt-injection and toxicity checked |
| `capacity.value` | number | for cement, thermal power, building | > 0, ≤ 1,000,000 |
| `capacity.unit` | enum | with value | `MTPA`, `MW`, `SQM`, `TPA`; must be valid for the sector |
| `area_ha` | number | for mining, township | > 0, ≤ 100,000 |
| `location.state` | enum | yes | One of 28 states and 8 union territories |
| `location.district` | string | yes | 2–100 chars |
| `protected_area_distance_km` | number or null | yes (key must be present) | 0–500; `null` = unknown, which escalates when the category depends on it |
| `protected_area_name` | string | when a distance is given | 2–200 chars |
| `interstate_boundary_distance_km` | number or null | no | 0–2000; `null` = no inter-state or international boundary nearby (not "unknown") |

Unknown fields are rejected (`extra = forbid`).

**Cross-field rules**: unit must match sector; capacity or area required by
sector; protected-area name required with a distance.

## Responses

| Case | Status | Body |
| --- | --- | --- |
| Accepted | 202 | `{run_id, status: "queued", status_url, trace_url, prompt_version}` |
| Served from cache | 200 | the finished result, `cache_hit: true` |
| Invalid JSON | 400 | `{error: "malformed_json", quarantine_id, run_started: false}` |
| Contract violation | 422 | `{error: "contract_violation", errors: [{field, error, type}], quarantine_id, run_started: false}` |
| Guardrail | 400 | `{error: "guardrail_blocked", check, field, quarantine_id, run_started: false}` |
| Rate limited | 429 | `Retry-After` header |

`GET /v1/screenings/{run_id}` returns `status` (`queued`, `running`, `done`,
`failed`) and, when finished: `route` (`category_a_central`,
`category_b_state`, `no_clearance_required`, `escalate_to_human`),
`category`, `basis`, `citations[{passage_id, clause_id, document, page, text}]`,
`conditions_applied`, `escalation_reason`, `verified`, `prompt_version`,
`disclaimer`.

Console endpoints (read-only unless noted): `GET /v1/console/config`,
`GET /v1/system/status`, `GET /v1/runs/recent`, `GET /v1/results/eval`,
`GET /v1/results/loadtest`, and `POST /v1/admin/faults {provider_down: bool}`
(needs `DEMO_FAULTS=1`, and header `X-Admin-Token` when `ADMIN_TOKEN` is set).

Other interfaces: `POST /v1/screenings/{id}/feedback {satisfactory: bool,
reason: 3–1000 chars}`; the LLM gateway's `POST /v1/chat`; the verifier's
`POST /verify`. Their request models are in `ecs/api/feedback.py`,
`ecs/llm/gateway.py` and `ecs/verifier_service.py`.

## Quarantine

Rejected requests are stored in Redis (`GET /v1/quarantine`) with the reason,
field errors, client ID, correlation ID and the raw body (first 4 KB), and
counted in `ecs_quarantined_total{reason}`.
