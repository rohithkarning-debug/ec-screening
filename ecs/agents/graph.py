r"""Screener agent graph (A1, A2, A3, A5, F3-F7).

    START -> precheck -> screener <-> tools
                           |
                           v
                         verify --(pass)--> finalize -> END
                           |  \--(fail, retries left)--> screener (with the verifier's strategy)
                           \----(fail / must escalate)--> escalate -> END

The screener LLM chooses each action: search_corpus, get_clause,
get_request_field, or final. Retrieval is therefore a tool it calls, not a
fixed stage. The loop is bounded by steps, tool calls and wall-clock time; a
run that hits a bound escalates.

Hand-off to the verifier agent is an HTTP call to its own service. If it
fails mid-run (down, timeout, error) the run escalates: nothing unverified
is returned.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from ecs import guardrails, prompts, retrieval
from ecs.agents.tools import TOOLS, ToolError
from ecs.config import settings
from ecs.llm import client as llm
from ecs.logging_setup import CORRELATION_HEADER, agent_name, correlation_id, log
from ecs.metrics import AGENT_RETRIES, AGENT_STEP_LATENCY, ESCALATIONS, TOOL_CALLS
from ecs.tracing import inject_headers, span

logger = logging.getLogger("screener")
OBS_TEXT_CHARS = 900


class RunState(TypedDict, total=False):
    run_id: str
    request: dict
    prompt_version: str
    started: float
    steps: list
    observations: list
    retrieved: dict
    step_count: int
    tool_calls: int
    parse_failures: int
    pending: Optional[dict]
    draft: Optional[dict]
    verifications: list
    attempt: int
    strategy_hint: Optional[str]
    next: str
    escalation_reason: Optional[str]
    escalation_code: Optional[str]
    result: Optional[dict]
    demo_fault: Optional[str]


def _step(state: RunState, **fields) -> list:
    rec = {"n": len(state.get("steps", [])) + 1, "t": round(time.time() - state["started"], 3), **fields}
    return list(state.get("steps", [])) + [rec]


def _escalate(state: RunState, reason: str, code: str) -> RunState:
    return {"next": "escalate", "escalation_reason": reason, "escalation_code": code,
            "steps": _step(state, agent="screener", node="escalate_decision", reason=reason, code=code)}


# ---------------------------------------------------------------- precheck
def precheck(state: RunState) -> RunState:
    agent_name.set("screener")
    req = state["request"]
    chk = guardrails.check_input(req)
    if not chk.ok:
        return _escalate(state, f"input blocked by guardrail ({chk.check} in {chk.field})", "guardrail")
    item = req.get("schedule_item")
    if not item:
        return _escalate(state, "sector is outside the indexed corpus", "sector_outside_corpus")
    if not retrieval.clause_exists(item):
        return _escalate(state, f"Schedule item {item} is not in the indexed corpus", "sector_outside_corpus")
    return {"next": "screener", "steps": _step(state, agent="screener", node="precheck", ok=True)}


# ---------------------------------------------------------------- screener (LLM)
def _render_observations(state: RunState) -> str:
    blocks = []
    for i, o in enumerate(state.get("observations", []), start=1):
        head = f"OBSERVATION {i} ({o['tool']} {json.dumps(o['args'])}):"
        if o.get("error"):
            blocks.append(f"{head}\nTOOL ERROR: {o['error']}")
        elif o["tool"] == "get_request_field":
            blocks.append(f"{head}\nvalue: {json.dumps(o.get('value'))}")
        elif not o.get("passage_ids"):
            blocks.append(f"{head}\n(no passages found)")
        else:
            lines = [head]
            for pid in o["passage_ids"]:
                p = state["retrieved"][pid]
                lines.append(f"- passage_id: {pid} | clause_id: {p.get('clause_id') or '-'} | type: {p.get('type')}"
                             f" | page: {p.get('page')}\n  {p['text'][:OBS_TEXT_CHARS]}")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(no observations yet)"


def screener(state: RunState) -> RunState:
    agent_name.set("screener")
    elapsed = time.time() - state["started"]
    if state.get("step_count", 0) >= settings.max_steps:
        return _escalate(state, f"step limit reached ({settings.max_steps})", "loop_limit")
    if state.get("tool_calls", 0) >= settings.max_tool_calls:
        return _escalate(state, f"tool-call limit reached ({settings.max_tool_calls})", "loop_limit")
    if elapsed >= settings.max_run_seconds:
        return _escalate(state, f"time limit reached ({settings.max_run_seconds:.0f}s)", "loop_limit")

    prompt = prompts.load("screener", state["prompt_version"])
    hint = f"STRATEGY HINT FROM VERIFIER: {state['strategy_hint']}" if state.get("strategy_hint") else ""
    if state.get("parse_failures"):
        hint += "\nYour previous reply was not one valid JSON object. Reply with exactly one JSON object."
    msgs = prompt.render(
        request_json=json.dumps(guardrails.request_view(state["request"]), indent=1),
        strategy_hint=hint, observations=_render_observations(state),
        steps_left=settings.max_steps - state.get("step_count", 0),
        tool_calls_left=settings.max_tool_calls - state.get("tool_calls", 0))
    n = state.get("step_count", 0) + 1
    t0 = time.perf_counter()
    with span("agent.screener.step", run_id=state["run_id"], step=n, prompt=prompt.tag) as s:
        try:
            res = llm.chat(msgs, agent="screener", step="screener.reason", run_id=state["run_id"])
        except llm.ProviderUnavailable as e:
            return _escalate(state, f"model provider unavailable - degraded to human review ({e})"[:300],
                             "provider_unavailable")
        dt = time.perf_counter() - t0
        AGENT_STEP_LATENCY.labels("screener", "reason").observe(dt)
        base = dict(agent="screener", node="reason", step=n, prompt_version=prompt.tag,
                    latency_ms=round(dt * 1000, 1), llm=res.usage())
        try:
            action = llm.parse_json(res.content)
        except llm.BadModelOutput as e:
            AGENT_RETRIES.labels("screener", "bad_output").inc()
            pf = state.get("parse_failures", 0) + 1
            steps = _step(state, **base, error=str(e), raw=res.content[:300])
            if pf > 1:
                return {"steps": steps, "step_count": n, "parse_failures": pf, "next": "escalate",
                        "escalation_code": "bad_model_output", "escalation_reason": "model output unreadable twice"}
            return {"steps": steps, "step_count": n, "parse_failures": pf, "next": "screener"}
        name = action.get("action")
        s.set_attribute("action", str(name))
        if name == "final":
            draft = action.get("answer") or {}
            if state.get("demo_fault") == "bad_citation" and state.get("attempt", 0) == 0:
                draft = {**draft, "citations": list(draft.get("citations") or []) + ["fabricated:not-retrieved"]}
                base["demo_fault"] = "bad_citation injected"
            return {"steps": _step(state, **base, action="final", thought=action.get("thought"), draft=draft),
                    "step_count": n, "draft": draft, "next": "verify", "parse_failures": 0}
        if name in TOOLS:
            return {"steps": _step(state, **base, action=name, args=action.get("args") or {},
                                   thought=action.get("thought")),
                    "step_count": n, "pending": {"tool": name, "args": action.get("args") or {}},
                    "next": "tools", "parse_failures": 0}
        AGENT_RETRIES.labels("screener", "bad_output").inc()
        pf = state.get("parse_failures", 0) + 1
        steps = _step(state, **base, error=f"unknown action {name!r}")
        return {"steps": steps, "step_count": n, "parse_failures": pf,
                "next": "escalate" if pf > 1 else "screener",
                "escalation_code": "bad_model_output",
                "escalation_reason": "model chose invalid actions twice" if pf > 1 else None}


# ---------------------------------------------------------------- tools
def tools(state: RunState) -> RunState:
    call = state["pending"]
    tool, args = call["tool"], call["args"] if isinstance(call["args"], dict) else {}
    t0 = time.perf_counter()
    obs = {"tool": tool, "args": args}
    retrieved = dict(state.get("retrieved", {}))
    with span(f"tool.{tool}", run_id=state["run_id"], args=json.dumps(args)[:200]) as s:
        try:
            out = TOOLS[tool](args, state["request"])
            ids = []
            for p in out.get("passages", []):
                retrieved[p["passage_id"]] = p
                ids.append(p["passage_id"])
            obs.update(query=out.get("query"), passage_ids=ids, value=out.get("value"),
                       cache_hit=out.get("cache_hit"))
            TOOL_CALLS.labels(tool, "ok").inc()
            s.set_attribute("passages", ",".join(ids))
        except (ToolError, Exception) as e:  # tool failures become observations, not crashes
            obs["error"] = f"{type(e).__name__}: {e}"[:300]
            TOOL_CALLS.labels(tool, "error").inc()
            s.set_attribute("error", obs["error"])
    dt = time.perf_counter() - t0
    AGENT_STEP_LATENCY.labels("screener", f"tool.{tool}").observe(dt)
    return {"retrieved": retrieved, "observations": list(state.get("observations", [])) + [obs],
            "tool_calls": state.get("tool_calls", 0) + 1, "pending": None, "next": "screener",
            "steps": _step(state, agent="screener", node="tool", tool=tool, args=args, query=obs.get("query"),
                           passage_ids=obs.get("passage_ids", []), cache_hit=obs.get("cache_hit"),
                           error=obs.get("error"), latency_ms=round(dt * 1000, 1))}


# ---------------------------------------------------------------- verify (hand-off)
def _call_verifier(state: RunState) -> dict:
    body = {"run_id": state["run_id"], "request": state["request"], "draft": state["draft"],
            "retrieved": state.get("retrieved", {})}
    last = None
    for attempt in range(2):
        try:
            r = httpx.post(settings.verifier_url.rstrip("/") + "/verify", json=body,
                           headers=inject_headers({CORRELATION_HEADER: correlation_id.get()}),
                           timeout=httpx.Timeout(settings.verifier_timeout_s, connect=3.0))
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            last = e
            AGENT_RETRIES.labels("screener", "verifier_call").inc()
            time.sleep(llm.backoff_s(attempt))
    raise RuntimeError(f"verifier unavailable: {last}")


def verify(state: RunState) -> RunState:
    t0 = time.perf_counter()
    with span("handoff.verifier", run_id=state["run_id"], attempt=state.get("attempt", 0)) as s:
        try:
            v = _call_verifier(state)
        except RuntimeError as e:
            return _escalate(state, f"verifier agent unavailable - answer not verified ({e})"[:300],
                             "verifier_unavailable")
        s.set_attribute("outcome", v["outcome"])
    dt = time.perf_counter() - t0
    verifications = list(state.get("verifications", [])) + [{**v, "attempt": state.get("attempt", 0)}]
    steps = _step(state, agent="verifier", node="verify", outcome=v["outcome"],
                  issues=[i["code"] for i in v["issues"]], latency_ms=round(dt * 1000, 1),
                  llm=(v["llm_usage"][0] if v.get("llm_usage") else None))
    base = {"verifications": verifications, "steps": steps}
    if v["ok"]:
        return {**base, "next": "finalize"}
    detail = "; ".join(f"{i['code']}: {i['detail']}" for i in v["issues"])[:400]
    if v.get("must_escalate"):
        return {**base, "next": "escalate", "escalation_code": "verification_escalate",
                "escalation_reason": f"verification requires escalation ({detail})"}
    if state.get("attempt", 0) < settings.max_verify_retries:
        AGENT_RETRIES.labels("screener", "verification_failed").inc()
        return {**base, "next": "screener", "attempt": state.get("attempt", 0) + 1,
                "strategy_hint": v.get("suggested_strategy"), "draft": None}
    return {**base, "next": "escalate", "escalation_code": "verification_failed",
            "escalation_reason": f"answer could not be verified ({detail})"}


# ---------------------------------------------------------------- terminal nodes
def _citations(state: RunState, ids: list) -> list:
    out = []
    for pid in ids:
        p = state.get("retrieved", {}).get(pid)
        if p:
            out.append({k: p.get(k) for k in ("passage_id", "clause_id", "document", "page", "text")})
    return out


def finalize(state: RunState) -> RunState:
    answer, notes = guardrails.filter_output(state["draft"])
    result = {
        "route": answer["route"], "category": answer.get("category"), "basis": answer.get("basis"),
        "citations": _citations(state, [c for c in answer.get("citations") or [] if isinstance(c, str)]),
        "conditions_applied": answer.get("conditions_applied") or [],
        "escalation_reason": answer.get("escalation_reason") if answer["route"] == "escalate_to_human" else None,
        "verified": True, "output_filter_notes": notes,
    }
    return {"result": result, "next": "end",
            "steps": _step(state, agent="screener", node="finalize", route=result["route"], filter_notes=notes)}


def escalate(state: RunState) -> RunState:
    reason = state.get("escalation_reason") or "escalated"
    code = state.get("escalation_code") or "other"
    ESCALATIONS.labels(code).inc()
    consulted = list(state.get("retrieved", {}).keys())[:6]
    result = {"route": "escalate_to_human", "category": None,
              "basis": "The system did not return a determination. A human reviewer should assess this project.",
              "citations": _citations(state, consulted), "conditions_applied": [],
              "escalation_reason": reason, "escalation_code": code, "verified": False}
    log(logger, logging.INFO, "run escalated", reason=reason)
    return {"result": result, "next": "end", "steps": _step(state, agent="screener", node="escalate", reason=reason)}


def _next(state: RunState) -> str:
    return state.get("next", "escalate")


def build_graph():
    g = StateGraph(RunState)
    for name, fn in [("precheck", precheck), ("screener", screener), ("tools", tools), ("verify", verify),
                     ("finalize", finalize), ("escalate", escalate)]:
        g.add_node(name, fn)
    g.add_edge(START, "precheck")
    g.add_conditional_edges("precheck", _next, {"screener": "screener", "escalate": "escalate"})
    g.add_conditional_edges("screener", _next, {"screener": "screener", "tools": "tools", "verify": "verify",
                                                "escalate": "escalate"})
    g.add_edge("tools", "screener")
    g.add_conditional_edges("verify", _next, {"finalize": "finalize", "screener": "screener",
                                              "escalate": "escalate"})
    g.add_edge("finalize", END)
    g.add_edge("escalate", END)
    return g.compile()


def initial_state(run_id: str, request: dict, prompt_version: str, demo_fault: Optional[str] = None) -> RunState:
    return {"run_id": run_id, "request": request, "prompt_version": prompt_version, "started": time.time(),
            "steps": [], "observations": [], "retrieved": {}, "step_count": 0, "tool_calls": 0,
            "parse_failures": 0, "pending": None, "draft": None, "verifications": [], "attempt": 0,
            "strategy_hint": None, "escalation_reason": None, "escalation_code": None, "result": None, "demo_fault": demo_fault}
