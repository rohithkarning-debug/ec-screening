"""Guardrails (L4).

1. Prompt-injection defence on every free-text field that reaches a model:
   - detection: pattern rules for instruction-override, role-play, tool and
     system-prompt manipulation and determination forcing;
   - containment: free text is always wrapped in <<<USER_TEXT ... USER_TEXT>>>
     delimiters and the prompts tell the model it is data;
   - the agent's tool arguments are validated, so injected text cannot widen
     what a tool does.
2. Toxicity detection on input and output: a lexicon screen. It is cheap and
   transparent; its limits (no context, English only) are documented in the
   report. Swap `toxicity_score` for a classifier model if resources allow.
3. Output filtering: the system may only issue one of four routes, must not
   claim to grant or refuse clearance, and must carry the disclaimer.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ecs.metrics import GUARDRAIL_BLOCKS

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
    r"disregard\s+(all\s+|the\s+)?(previous|prior|above|system)",
    r"forget\s+(everything|all|your)\s+(instructions|rules|previous)",
    r"\byou\s+are\s+now\b",
    r"\bact\s+as\s+(an?\s+)?(different|new|unrestricted)",
    r"\b(system|developer)\s*(prompt|message|instructions?)\s*[:=]",
    r"^\s*(system|assistant)\s*:",
    r"\bnew\s+instructions?\s*:",
    r"\b(return|output|answer|respond\s+with|classify\s+(it|this)\s+as)\s+(only\s+)?[\"']?category\s*[ab]\b",
    r"\b(set|force|make)\s+(the\s+)?(category|route|determination)\b",
    r"\b(call|invoke|run)\s+(the\s+)?(tool|function)\b",
    r"\"action\"\s*:",
    r"<\s*/?\s*(system|instructions?|tool)\s*>",
    r"USER_TEXT\s*>>>|<<<\s*USER_TEXT",
    r"\bjailbreak\b|\bdo\s+anything\s+now\b",
    r"\bprint\s+(your|the)\s+(system\s+)?prompt\b",
    r"\bclearance\s+(is\s+)?(granted|not\s+required)\b.*\b(say|state|write)\b",
]
_INJ = [re.compile(p, re.I | re.M) for p in INJECTION_PATTERNS]

# Short lexicon of abusive terms. Matching is on whole words after normalisation.
TOXIC_TERMS = {
    "idiot", "idiots", "stupid", "moron", "morons", "scum", "bastard", "bastards", "bitch",
    "shit", "fuck", "fucking", "asshole", "kill", "die", "hate", "dumb", "retard", "retarded",
    "imbecile", "loser", "worthless", "garbage",
}
TOXIC_THRESHOLD = 2  # number of distinct abusive terms that triggers a block

FREE_TEXT_FIELDS = ("project_name", "description", "protected_area_name")
ALLOWED_ROUTES = {"no_clearance_required", "category_b_state", "category_a_central", "escalate_to_human"}
ROUTE_FOR_CATEGORY = {"A": "category_a_central", "B": "category_b_state", "none": "no_clearance_required"}
FORBIDDEN_OUTPUT = [
    re.compile(r"\b(clearance|approval|permission)\s+(is\s+|has\s+been\s+)?(hereby\s+)?(granted|approved|issued|refused|denied)\b", re.I),
    re.compile(r"\byou\s+(may|can)\s+(start|begin|commence)\s+(construction|work|operations?)\b", re.I),
    re.compile(r"\bthis\s+(is\s+)?(a\s+)?(final|binding|authoritative)\s+(decision|determination)\b", re.I),
]


@dataclass
class Check:
    ok: bool
    check: str = ""
    field: str = ""
    detail: str = ""


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[​-‏⁠﻿]", "", text)  # strip zero-width characters


def detect_injection(text: str) -> str | None:
    t = _norm(text)
    for p in _INJ:
        m = p.search(t)
        if m:
            return m.group(0)[:80]
    return None


def toxicity_score(text: str) -> int:
    words = set(re.findall(r"[a-z]+", _norm(text).lower()))
    return len(words & TOXIC_TERMS)


def check_input(request: dict) -> Check:
    for f in FREE_TEXT_FIELDS:
        val = request.get(f)
        if not val:
            continue
        hit = detect_injection(val)
        if hit:
            GUARDRAIL_BLOCKS.labels("prompt_injection", "input").inc()
            return Check(False, "prompt_injection", f, f"matched: {hit!r}")
        if toxicity_score(val) >= TOXIC_THRESHOLD:
            GUARDRAIL_BLOCKS.labels("toxicity", "input").inc()
            return Check(False, "toxicity", f, "abusive language")
    return Check(True)


def wrap_user_text(text: str) -> str:
    safe = (text or "").replace("<<<", "‹‹‹").replace(">>>", "›››")
    return f"<<<USER_TEXT\n{safe}\nUSER_TEXT>>>"


def request_view(request: dict) -> dict:
    """The request as the model sees it: free text delimited, nothing else changed."""
    view = dict(request)
    for f in FREE_TEXT_FIELDS:
        if view.get(f):
            view[f] = wrap_user_text(view[f])
    return view


def filter_output(answer: dict) -> tuple[dict, list[str]]:
    """Force the answer into the set of determinations the system may issue."""
    notes: list[str] = []
    out = dict(answer)
    route = out.get("route")
    cat = out.get("category")
    if route not in ALLOWED_ROUTES:
        notes.append(f"route {route!r} not permitted")
        route = "escalate_to_human"
    if route != "escalate_to_human" and ROUTE_FOR_CATEGORY.get(cat) != route:
        notes.append(f"category {cat!r} inconsistent with route {route!r}")
        route = "escalate_to_human"
    basis = out.get("basis") or ""
    for p in FORBIDDEN_OUTPUT:
        if p.search(basis):
            notes.append("basis claimed an authoritative decision")
            basis = p.sub("[removed: the system cannot grant or refuse clearance]", basis)
    if toxicity_score(basis) >= TOXIC_THRESHOLD:
        notes.append("toxic output")
        basis = "[removed by output filter]"
        route = "escalate_to_human"
    if notes:
        GUARDRAIL_BLOCKS.labels("output_filter", "output").inc()
    out["route"] = route
    out["basis"] = basis
    if route == "escalate_to_human":
        out["category"] = None
        out.setdefault("escalation_reason", None)
        if not out.get("escalation_reason"):
            out["escalation_reason"] = "; ".join(notes) or "escalated"
    return out, notes
