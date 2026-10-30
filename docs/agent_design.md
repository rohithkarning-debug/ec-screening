# Agent design (A1, A2, A3, A5, L4)

## Topology

Two agents, each a LangGraph graph, in separate containers.

**Screener** (`ecs/agents/graph.py`, runs in `worker`):

```
precheck -> screener <-> tools
               |
             verify --pass--> finalize
               |---fail, retry left--> screener (with the verifier's strategy)
               |---fail / must escalate--> escalate
```

**Verifier** (`ecs/agents/verifier.py`, runs in `verifier`):

```
resolve_citations -> rule_checks -> llm_judge -> decide
                          \--- (rules failed) ---> decide
```

**Hand-off**: the screener posts `{request, draft, retrieved passages}` to the
verifier's `/verify` and gets `{ok, issues[], suggested_strategy,
must_escalate}`.

## How it follows the required approach (brief section 5)

| Step | Where |
| --- | --- |
| 1. Retrieve the provision that appears to apply | Screener calls `get_clause(schedule_item)` or `search_corpus` |
| 2. Decide whether another provision modifies it, and search | Prompt v2 procedure; verifier rule `modifier_not_checked` forces it if skipped |
| 3. Search again with a query derived from what it read | Screener builds the query from the row and the request distances |
| 4. Verify the passages support the answer | Verifier: citations resolve, rules, LLM judge |
| 5. Retry with a different strategy, or escalate | `suggested_strategy` feeds the retry; second failure escalates |

## Tools

| Tool | Arguments (validated) | Returns |
| --- | --- | --- |
| `search_corpus` | `query` 3–300 chars, `k` 1–8, `type` ∈ schedule_row, modifier, amendment, paragraph | passages with IDs and metadata |
| `get_clause` | `clause_id` matching `GC` or `N(x)` | the clause's passages |
| `get_request_field` | one of the request's field names | the value |

Every tool has a timeout (`TOOL_TIMEOUT_SECONDS`). A tool error becomes an
observation for the model, not a crash.

## Loop limits

`AGENT_MAX_STEPS` (8), `AGENT_MAX_TOOL_CALLS` (10) and
`AGENT_MAX_RUN_SECONDS` (90) are checked before every screener step. Hitting
one escalates the run with the reason.

## Verification rules

| Code | Fails when | Strategy given to the retry |
| --- | --- | --- |
| `citation_not_retrieved` | A cited ID was never returned by a tool | Cite only observed passages |
| `citation_not_in_index` / `citation_text_mismatch` | Citation does not match the corpus | Re-fetch with `get_clause` |
| `schedule_row_not_cited` | A or B without the sector's Schedule row | Fetch and cite the row |
| `modifier_not_checked` | Draft says B, no modifier retrieved | Fetch GC, search modifiers |
| `modifier_applies` | Draft says B, a retrieved modifier's km limit covers a request distance | Apply the GC, cite both |
| `modifier_not_cited` | Basis relies on the GC without citing it | Cite it |
| `proximity_unknown` | Draft says B but the protected-area distance is unknown | **Escalate** |
| `judge_unsupported` | The LLM judge finds the passages do not support the draft | Search amendments and modifiers |

Escalations always pass verification: not answering is always permitted.

## Failure modes

| Failure | Behaviour |
| --- | --- |
| Provider errors | Gateway returns 502; client retries with exponential backoff and jitter (3 attempts) |
| Provider down | Breaker opens after `BREAKER_FAIL_THRESHOLD` failures; fallback provider if configured; else 503, run escalates "provider unavailable" |
| Verifier down / timeout | Two attempts, then the run escalates "verifier unavailable"; nothing unverified is returned |
| Model returns invalid JSON | One corrective retry, then escalation |
| Worker crashes mid-run | The record stays `running` with its partial trace; see runbook |
| Any exception in the graph | Run marked `failed`, result is an escalation |

## Guardrails (L4)

- **Injection**: pattern detection on every free-text field, at the API (block
  and quarantine) and again in `precheck`. Free text reaches the model only
  inside `<<<USER_TEXT … USER_TEXT>>>`, and both prompts say it is data. Tool
  arguments are validated, so injected text cannot change what a tool does.
- **Output filter**: only the four routes; route must match the category; any
  claim to grant or refuse clearance is removed; the disclaimer is always
  attached.
- **Toxicity**: lexicon screen on input (block) and on the generated basis
  (escalate). Known limit: no context, English only.

## Prompts (L2)

`prompts/screener_v1.yaml` (baseline) and `screener_v2.yaml` (explicit modifier
procedure and escalation rules). The version, with a content hash, is stored on
every run and step. Compare them with the evaluation CLI (docs/evaluation.md).
