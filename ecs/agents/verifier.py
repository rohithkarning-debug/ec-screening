"""Verifier agent (A2, A3).

A separate agent with its own service, prompt, tools and LangGraph. It never
makes a determination; it decides whether the screener's draft may be
returned. Its graph:

    resolve_citations -> rule_checks -> llm_judge -> decide

resolve_citations: every cited passage ID must be in the run's retrieved set,
    and the verifier re-reads each one from the index itself (its own tool),
    so the citation is checked against the corpus, not against the screener's
    copy.
rule_checks: deterministic checks that encode what a correct screening must
    contain - a cited Schedule row, a capacity on the right side of that row's
    threshold, a checked location modifier when the draft says Category B,
    every retrieved amendment to the same item considered, and escalation
    when a decisive distance is unknown.
llm_judge: the model reads the cited passages and the draft and says whether
    the passages support it. Skipped when the rules have already failed.
decide: pass, or fail with issue codes and a different strategy for the
    screener's retry.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from ecs import prompts, retrieval
from ecs.config import settings
from ecs.guardrails import ALLOWED_ROUTES, ROUTE_FOR_CATEGORY, wrap_user_text
from ecs.llm import client as llm
from ecs.logging_setup import agent_name, log
from ecs.metrics import AGENT_STEP_LATENCY, AGENT_TASKS, VERIFICATIONS

logger = logging.getLogger("verifier")
KM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:km\b|kilomet)", re.I)

STRATEGY = {
    "citation_not_retrieved": "Cite only passage_ids shown in OBSERVATION blocks. Use get_clause to fetch the "
                              "exact Schedule row and re-cite it.",
    "citation_not_in_index": "One cited passage does not exist in the corpus. Fetch provisions with get_clause.",
    "citation_text_mismatch": "Cited text does not match the corpus. Re-fetch with get_clause and cite again.",
    "schedule_row_not_cited": "Fetch the Schedule row with get_clause(schedule_item) and cite it.",
    "modifier_not_checked": "The draft says Category B but no modifying provision was checked. Call "
                            "get_clause with 'GC' and search_corpus with type 'modifier' using the "
                            "project's distances, then apply what you find.",
    "modifier_applies": "A retrieved modifying provision (the General Condition) applies at this distance: "
                        "re-read it, apply it to the Schedule category, and cite both provisions.",
    "modifier_not_cited": "The draft lists a modifying provision as applied but does not cite it. Cite it, or "
                          "remove it from conditions_applied if it did not change the category.",
    "no_citations": "Every determination needs citations. Retrieve and cite the Schedule row.",
    "invalid_route": "Use one of the four allowed routes, consistent with the category.",
    "threshold_mismatch": "The category does not match the cited Schedule row's threshold for this capacity. "
                          "Re-read the row's Category A and B columns and compare the request's capacity; "
                          "only a cited modifying provision can move a project above its Schedule category.",
    "amendment_not_considered": "An amendment to this Schedule item or to the General Condition was retrieved "
                                "but not considered. Read it: if it changes the rule, apply it and cite it; if it "
                                "conflicts with the Schedule text and you cannot tell which prevails, escalate.",
    "judge_unsupported": "The cited passages do not support the draft. Search again with a different query "
                         "(search_corpus with type 'amendment' or 'modifier') before answering.",
}
ESCALATE_CODES = {"proximity_unknown", "verifier_llm_unavailable"}


class VState(TypedDict, total=False):
    run_id: str
    request: dict
    draft: dict
    retrieved: dict
    cited: list
    issues: list
    judge: Optional[dict]
    llm_usage: list
    steps: list
    ok: bool
    suggested_strategy: Optional[str]
    must_escalate: bool


def _issue(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def _timed(step):
    def deco(fn):
        def wrapper(state: VState) -> VState:
            t0 = time.perf_counter()
            out = fn(state)
            dt = time.perf_counter() - t0
            AGENT_STEP_LATENCY.labels("verifier", step).observe(dt)
            steps = list(state.get("steps", [])) + [{"agent": "verifier", "node": step,
                                                     "latency_ms": round(dt * 1000, 1),
                                                     "issues_so_far": len(out.get("issues", state.get("issues", [])))}]
            out["steps"] = steps
            return out
        return wrapper
    return deco


@_timed("resolve_citations")
def resolve_citations(state: VState) -> VState:
    draft, retrieved = state["draft"], state["retrieved"]
    issues = list(state.get("issues", []))
    cited_ids = [c for c in (draft.get("citations") or []) if isinstance(c, str)]
    not_retrieved = [c for c in cited_ids if c not in retrieved]
    if not_retrieved:
        issues.append(_issue("citation_not_retrieved", f"cited but never retrieved: {not_retrieved}"))
    in_run = [c for c in cited_ids if c in retrieved]
    fresh = {p["passage_id"]: p for p in retrieval.get_passages(in_run)} if in_run else {}
    cited = []
    for cid in in_run:
        p = fresh.get(cid)
        if p is None:
            issues.append(_issue("citation_not_in_index", f"{cid} is not in the index"))
        elif p["text"].strip() != (retrieved[cid].get("text") or "").strip():
            issues.append(_issue("citation_text_mismatch", f"{cid} text differs from the corpus"))
        else:
            cited.append(p)
    return {"issues": issues, "cited": cited}


def _is_modifier(p: dict) -> bool:
    return p.get("type") == "modifier" or (p.get("clause_id") or "").upper() == "GC"


UNIT_PATTERNS = {
    "MTPA": re.compile(r"(\d+(?:\.\d+)?)\s*(?:mtpa\b|million\s*(?:tonnes?|tons?|t)\b)", re.I),
    "MW": re.compile(r"(\d+(?:\.\d+)?)\s*mw\b", re.I),
    "SQM": re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:sq\.?\s*m\b|sqm\b|square\s*met)", re.I),
    "HA": re.compile(r"(\d+(?:\.\d+)?)\s*(?:ha\b|hectares?\b)", re.I),
}


def _row_threshold(row_text: str, unit: str) -> float | None:
    """The single threshold a Schedule row states for this unit, or None if absent or ambiguous.

    Conservative on purpose: a row with several thresholds for the unit (e.g. by fuel type) is
    left to the LLM judge rather than guessed at.
    """
    pat = UNIT_PATTERNS.get(unit)
    if not pat:
        return None
    values = {float(m.group(1).replace(",", "")) for m in pat.finditer(row_text)}
    return values.pop() if len(values) == 1 else None


def _measure(req: dict) -> tuple[float, str] | None:
    cap = req.get("capacity") or {}
    if cap.get("value") is not None and cap.get("unit") in UNIT_PATTERNS:
        return float(cap["value"]), cap["unit"]
    if req.get("area_ha") is not None:
        return float(req["area_ha"]), "HA"
    return None


@_timed("rule_checks")
def rule_checks(state: VState) -> VState:
    draft, req, retrieved = state["draft"], state["request"], state["retrieved"]
    issues = list(state.get("issues", []))
    cited = state.get("cited", [])
    route, cat = draft.get("route"), draft.get("category")

    if route not in ALLOWED_ROUTES or (route != "escalate_to_human" and ROUTE_FOR_CATEGORY.get(cat) != route):
        issues.append(_issue("invalid_route", f"route={route!r} category={cat!r}"))
        return {"issues": issues}
    if route == "escalate_to_human":
        return {"issues": issues}  # escalating is always permitted
    if not draft.get("citations"):
        issues.append(_issue("no_citations", "determination has no citations"))
        return {"issues": issues}

    item = (req.get("schedule_item") or "").lower()
    if route in ("category_a_central", "category_b_state") and item:
        if not any((p.get("clause_id") or "").lower() == item or p.get("type") == "schedule_row" for p in cited):
            issues.append(_issue("schedule_row_not_cited", f"Schedule item {item} not cited"))

    # Capacity against the cited row's threshold (A at or above, B below).
    # A draft relies on the General Condition only if it lists it as applied. Mentioning it in the
    # basis to say it does NOT apply (e.g. a project already in Category A) is not relying on it.
    relies_on_modifier = any((c or "").upper() == "GC" for c in draft.get("conditions_applied") or [])
    row = next((p for p in cited if (p.get("clause_id") or "").lower() == item), None)
    measure = _measure(req)
    if row and measure and cat in ("A", "B"):
        value, unit = measure
        limit = _row_threshold(row["text"], unit)
        if limit is not None:
            if cat == "B" and value >= limit:
                issues.append(_issue("threshold_mismatch", f"{value:g} {unit} is at or above the row's {limit:g} "
                                                           f"{unit} threshold, which the row places in Category A"))
            elif cat == "A" and value < limit and not (relies_on_modifier and any(_is_modifier(p) for p in cited)):
                issues.append(_issue("threshold_mismatch", f"{value:g} {unit} is below the row's {limit:g} {unit} "
                                                           "threshold and no modifying provision is cited"))

    # Every retrieved amendment to this item, or to the General Condition, must be considered (F7).
    cited_ids = {p["passage_id"] for p in cited}
    loc_matters = any(req.get(k) is not None for k in ("protected_area_distance_km",
                                                         "interstate_boundary_distance_km"))
    ignored = [p for p in retrieved.values() if p.get("type") == "amendment" and p["passage_id"] not in cited_ids
               and (((p.get("clause_id") or "").lower() == item and item)
                    or (loc_matters and "general condition" in (p.get("text") or "").lower()))]
    if ignored:
        issues.append(_issue("amendment_not_considered",
                             "retrieved but not cited: " + ", ".join(p["passage_id"] for p in ignored[:3])))

    distances = {"protected area": req.get("protected_area_distance_km"),
                 "inter-state boundary": req.get("interstate_boundary_distance_km")}
    modifiers = [p for p in retrieved.values() if _is_modifier(p)]
    if cat == "B":
        if req.get("protected_area_distance_km") is None:
            issues.append(_issue("proximity_unknown",
                                 "Category B depends on location, but the protected-area distance is unknown"))
        if not modifiers:
            issues.append(_issue("modifier_not_checked", "no modifying provision was retrieved"))
        else:
            for m in modifiers:
                km = KM_RE.search(m["text"])
                if not km:
                    continue
                limit = float(km.group(1))
                near = {k: v for k, v in distances.items() if v is not None and v <= limit}
                if near:
                    issues.append(_issue("modifier_applies",
                                         f"{m.get('clause_id') or m['passage_id']} sets {limit:g} km; project is "
                                         + ", ".join(f"{v:g} km from the {k}" for k, v in near.items())))
                    break
    if cat == "A":
        if relies_on_modifier and not any(_is_modifier(p) for p in cited):
            issues.append(_issue("modifier_not_cited", "the General Condition is listed as applied but not cited"))
    return {"issues": issues}


def _route_after_rules(state: VState) -> str:
    if state.get("issues") or state["draft"].get("route") == "escalate_to_human":
        return "decide"
    return "llm_judge"


@_timed("llm_judge")
def llm_judge(state: VState) -> VState:
    prompt = prompts.load("verifier", settings.verifier_prompt_version)
    facts = {k: v for k, v in state["request"].items() if k != "description"}
    facts["project_name"] = wrap_user_text(str(facts.get("project_name", "")))
    passages = "\n\n".join(f"[{p['passage_id']}] ({p.get('clause_id') or p.get('type')})\n{p['text'][:1500]}"
                           for p in state.get("cited", []))
    draft = {k: state["draft"].get(k) for k in ("category", "route", "basis", "conditions_applied")}
    msgs = prompt.render(facts_json=json.dumps(facts, indent=1), draft_json=json.dumps(draft, indent=1),
                         passages=passages or "(none)")
    issues = list(state.get("issues", []))
    usage = list(state.get("llm_usage", []))
    try:
        res = llm.chat(msgs, agent="verifier", step="verifier.judge", run_id=state.get("run_id"), max_tokens=300)
        usage.append({**res.usage(), "prompt": prompt.tag})
        verdict = llm.parse_json(res.content)
        if verdict.get("supported") is not True:
            issues.append(_issue("judge_unsupported", str(verdict.get("reason") or "not supported")[:300]))
        return {"judge": verdict, "issues": issues, "llm_usage": usage}
    except llm.ProviderUnavailable as e:
        issues.append(_issue("verifier_llm_unavailable", str(e)[:200]))
    except llm.BadModelOutput as e:
        issues.append(_issue("judge_unsupported", f"judge output unreadable: {e}"))
    return {"judge": None, "issues": issues, "llm_usage": usage}


@_timed("decide")
def decide(state: VState) -> VState:
    issues = state.get("issues", [])
    codes = [i["code"] for i in issues]
    ok = not issues
    hints = [STRATEGY[c] for c in dict.fromkeys(codes) if c in STRATEGY]
    return {"ok": ok, "must_escalate": any(c in ESCALATE_CODES for c in codes),
            "suggested_strategy": " ".join(hints) or None}


def build_graph():
    g = StateGraph(VState)
    g.add_node("resolve_citations", resolve_citations)
    g.add_node("rule_checks", rule_checks)
    g.add_node("llm_judge", llm_judge)
    g.add_node("decide", decide)
    g.add_edge(START, "resolve_citations")
    g.add_edge("resolve_citations", "rule_checks")
    g.add_conditional_edges("rule_checks", _route_after_rules, {"llm_judge": "llm_judge", "decide": "decide"})
    g.add_edge("llm_judge", "decide")
    g.add_edge("decide", END)
    return g.compile()


_graph = None


def verify(run_id: str, request: dict, draft: dict, retrieved: dict) -> dict:
    global _graph
    if _graph is None:
        _graph = build_graph()
    agent_name.set("verifier")
    t0 = time.perf_counter()
    out = _graph.invoke({"run_id": run_id, "request": request, "draft": draft, "retrieved": retrieved,
                         "issues": [], "steps": [], "llm_usage": []})
    outcome = "pass" if out["ok"] else ("escalate" if out["must_escalate"] else "fail")
    VERIFICATIONS.labels(outcome).inc()
    AGENT_TASKS.labels("verifier", "success").inc()
    result = {"ok": out["ok"], "outcome": outcome, "issues": out.get("issues", []),
              "suggested_strategy": out.get("suggested_strategy"), "must_escalate": out.get("must_escalate", False),
              "judge": out.get("judge"), "llm_usage": out.get("llm_usage", []), "steps": out.get("steps", []),
              "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    log(logger, logging.INFO, "verification done", outcome=outcome, issues=[i["code"] for i in result["issues"]])
    return result
