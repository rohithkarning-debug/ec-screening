"""Batch evaluation from the command line (L3, L2).

    python -m eval.run_eval --api http://localhost:8000 --prompt-version v2 --label after
    python -m eval.run_eval --prompt-version v1 --label before
    python -m eval.compare eval/results/before.json eval/results/after.json

Each scenario is submitted through the public API (so the evaluation runs the
real system, including validation, guardrails, both agents and the cache is
bypassed), the run trace is fetched, and four metrics are computed with the
definitions in ecs/quality.py and docs/evaluation.md:

    groundedness, context relevance, faithfulness, hallucination rate
plus route accuracy. Results go to eval/results/<label>.json and .md.

Run against a real model, never the stub. Before the demo, confirm every
expected route and supporting clause against your corpus.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ecs import quality  # noqa: E402

HERE = Path(__file__).resolve().parent


def run_one(client: httpx.Client, s: dict, prompt_version: str, timeout_s: float) -> dict:
    headers = {"X-Prompt-Version": prompt_version, "X-Client-Id": "eval-cli"}
    body = dict(s["request"])
    body["project_name"] = f"{body['project_name']} [{s['id']} {time.time():.0f}]"  # avoid run cache
    r = client.post("/v1/screenings", json=body, headers=headers)
    if s["expected_route"] == "blocked":
        blocked = r.status_code == 400 and r.json().get("error") == "guardrail_blocked"
        return {"id": s["id"], "kind": "guardrail", "passed": blocked, "status_code": r.status_code}
    if r.status_code not in (200, 202):
        return {"id": s["id"], "kind": "error", "passed": False, "status_code": r.status_code, "body": r.text[:300]}
    run_id = r.json()["run_id"]
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        d = client.get(f"/v1/screenings/{run_id}").json()
        if d["status"] in ("done", "failed"):
            break
        time.sleep(1.0)
    trace = client.get(f"/v1/screenings/{run_id}/trace").json()
    result = trace.get("result") or {}
    retrieved = trace.get("passages") or {}
    cited = []
    for st in trace.get("steps", []):
        if st.get("action") == "final" and st.get("draft"):
            cited = [c for c in st["draft"].get("citations") or [] if isinstance(c, str)]
    verified_ok = bool(trace.get("verifications")) and trace["verifications"][-1]["ok"]
    route = result.get("route")
    m = {
        "groundedness": quality.groundedness(result),
        "context_relevance": quality.context_relevance(retrieved, s["supporting_clauses"]),
        "faithfulness": quality.faithfulness(result, set(retrieved), cited, verified_ok or route == "escalate_to_human",
                                             s["expected_route"]),
        "hallucination": quality.hallucinated(result, set(retrieved), cited),
    }
    cited_clauses = sorted({(c.get("clause_id") or "") for c in result.get("citations", [])} - {""})
    return {"id": s["id"], "kind": "run", "run_id": run_id, "expected_route": s["expected_route"], "route": route,
            "passed": route == s["expected_route"], "metrics": m, "cited_clauses": cited_clauses,
            "expected_clauses": s["supporting_clauses"], "escalation_reason": result.get("escalation_reason"),
            "verify_attempts": len(trace.get("verifications", [])),
            "cost_usd": (trace.get("totals") or {}).get("cost_usd"),
            "duration_s": round((trace.get("finished_at") or time.time()) - trace.get("submitted_at", time.time()), 2)}


def summarize(rows: list[dict]) -> dict:
    runs = [r for r in rows if r["kind"] == "run"]
    agg = {k: round(statistics.mean(r["metrics"][k] for r in runs), 4) if runs else None
           for k in ("groundedness", "context_relevance", "faithfulness", "hallucination")}
    agg["hallucination_rate"] = agg.pop("hallucination")
    agg["route_accuracy"] = round(sum(r["passed"] for r in rows) / len(rows), 4) if rows else None
    agg["case_4_1_passed"] = next((r["passed"] for r in rows if r["id"].startswith("s01")), None)
    costs = [r["cost_usd"] for r in runs if r.get("cost_usd") is not None]
    agg["mean_cost_usd"] = round(statistics.mean(costs), 6) if costs else None
    durs = [r["duration_s"] for r in runs]
    agg["mean_duration_s"] = round(statistics.mean(durs), 2) if durs else None
    return agg


def to_markdown(label: str, prompt_version: str, summary: dict, rows: list[dict]) -> str:
    lines = [f"# Evaluation: {label} (prompt {prompt_version})", "", "| Metric | Value |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]
    lines += ["", "| Scenario | Expected | Got | Pass | Cited | Grounded | Ctx rel | Faithful | Halluc |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        m = r.get("metrics") or {}
        lines.append(f"| {r['id']} | {r.get('expected_route', 'blocked')} | {r.get('route', r.get('status_code'))} | "
                     f"{'yes' if r['passed'] else 'NO'} | {','.join(r.get('cited_clauses', []))} | "
                     f"{m.get('groundedness', '')} | {m.get('context_relevance', '')} | {m.get('faithfulness', '')} | "
                     f"{m.get('hallucination', '')} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run the fixed evaluation set against the API.")
    p.add_argument("--api", default=os.getenv("EVAL_API_URL", "http://localhost:8000"))
    p.add_argument("--scenarios", default=str(HERE / "scenarios.jsonl"))
    p.add_argument("--prompt-version", default="v2")
    p.add_argument("--label", default=None)
    p.add_argument("--only", nargs="*", help="Scenario IDs to run")
    p.add_argument("--timeout", type=float, default=180.0)
    a = p.parse_args(argv)
    label = a.label or f"prompt-{a.prompt_version}"
    scenarios = [json.loads(line) for line in Path(a.scenarios).read_text().splitlines() if line.strip()]
    if a.only:
        scenarios = [s for s in scenarios if s["id"] in a.only]
    rows = []
    with httpx.Client(base_url=a.api, timeout=30) as client:
        for s in scenarios:
            row = run_one(client, s, a.prompt_version, a.timeout)
            rows.append(row)
            print(f"{row['id']:<28} {'PASS' if row['passed'] else 'FAIL'}  got={row.get('route', row.get('status_code'))}",
                  flush=True)
    summary = summarize(rows)
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    payload = {"label": label, "prompt_version": a.prompt_version, "ran_at": time.time(), "summary": summary,
               "rows": rows}
    (out_dir / f"{label}.json").write_text(json.dumps(payload, indent=2))
    (out_dir / f"{label}.md").write_text(to_markdown(label, a.prompt_version, summary, rows))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
