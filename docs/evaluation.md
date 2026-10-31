# Evaluation (L2, L3)

## The set

`eval/scenarios.jsonl`: 23 scenarios. Each has a request, the expected route
and the clause IDs that support it. The set includes the section 4.1 case
(`s01`), its contrasts (`s02` 15 km, `s03` unknown), boundary distances
(9.5 / 10 / 10.5 km), the inter-state boundary limb, large projects that are A
regardless of location, an out-of-corpus sector, an amendment-conflict case,
and a prompt injection.

**Before Evaluation 2, confirm every expected route against your corpus.**
Thresholds and item numbers in the set follow the 2006 Schedule as commonly
cited; your downloaded text, and the amendments you include, are the authority.

## Running it

```bash
# real model configured in .env; run inside the api container so it uses the Compose network
docker compose exec api python -m eval.run_eval --api http://localhost:8000 --prompt-version v1 --label v1
docker compose exec api python -m eval.run_eval --api http://localhost:8000 --prompt-version v2 --label v2
docker compose exec api python -m eval.compare eval/results/v1.json eval/results/v2.json
```

Results are written to `eval/results/` in the repository folder (it is mounted into the API container) and appear in the web console's Quality tab.

Each scenario goes through the public API, so validation, guardrails, both
agents and verification are all exercised. The run cache is bypassed by
making each project name unique.

## Metrics (implemented in `ecs/quality.py`)

| Metric | How it is computed |
| --- | --- |
| Groundedness | Share of sentences in the answer's basis whose content words overlap ≥ 50% with some cited passage. Escalations score 1. |
| Context relevance | Share of the scenario's expected supporting clauses (e.g. 3(b), GC) among the passages retrieved during the run. |
| Faithfulness | 1 if every citation resolves to retrieved text, the verifier passed (or the run escalated), and the route equals the expected route; else 0. |
| Hallucination rate | Share of runs that cite a passage never retrieved, or make a basis sentence with < 20% overlap with every cited passage. |
| Route accuracy | Share of scenarios whose route (or guardrail block) matches the expectation. |

These are transparent lexical measures, chosen over an LLM-as-judge library to
stay free, deterministic and explainable at the viva. Their limit: overlap is
not entailment. A paraphrased but correct basis can score lower than it should.

## The deliberate change (L3) and prompt comparison (L2)

The planned change is **screener prompt v1 → v2**: v2 adds the explicit
modifier-search procedure. Expected effect: higher route accuracy and context
relevance on location-dependent scenarios (s01, s06, s08, s09, s13, s14, s23).

Record the results here after running:

| Metric | v1 (before) | v2 (after) | Change |
| --- | --- | --- | --- |
| Groundedness | | | |
| Context relevance | | | |
| Faithfulness | | | |
| Hallucination rate | | | |
| Route accuracy | | | |
| Section 4.1 passed | | | |

Which variant was better, on which metric, and by how much:
