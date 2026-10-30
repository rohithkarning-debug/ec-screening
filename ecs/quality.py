"""Quality metrics shared by the offline evaluation (L3) and live scoring (L6).

Definitions (also in docs/evaluation.md):

groundedness      Share of sentences in the answer's `basis` whose content words
                  overlap at least 50% with the text of some cited passage.
                  Escalations score 1.0 (they make no claim).
context_relevance Share of the scenario's expected supporting clauses (e.g.
                  "3(b)", "GC") present among the passages retrieved during the
                  run. Needs a reference, so offline only.
faithfulness      1 if every citation resolves to retrieved text AND the
                  verifier's independent check passed AND the route matches the
                  route implied by the expected supporting passages; else 0.
                  Offline only (needs the expected route).
hallucination     1 if the answer cites any passage that was not retrieved, or
                  any basis sentence has under 20% overlap with all cited
                  passages (an unsupported claim); else 0. The reported
                  hallucination rate is the mean over runs.
"""
from __future__ import annotations

import re

STOP = set("""a an the of to in on for and or is are be by with as at from that this it its which
was were will shall may can not no any all if than then into under over per such these those
project projects category""".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", (text or "").lower()) if w not in STOP and len(w) > 1}


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+", (text or "").strip()) if len(_words(s)) >= 3]


def sentence_support(sentence: str, passages: list[str]) -> float:
    sw = _words(sentence)
    if not sw:
        return 1.0
    return max((len(sw & _words(p)) / len(sw) for p in passages), default=0.0)


def groundedness(result: dict) -> float:
    if result.get("route") == "escalate_to_human":
        return 1.0
    texts = [c.get("text", "") for c in result.get("citations", [])]
    sents = _sentences(result.get("basis", ""))
    if not sents:
        return 0.0 if not texts else 1.0
    return round(sum(sentence_support(s, texts) >= 0.5 for s in sents) / len(sents), 4)


def hallucinated(result: dict, retrieved_ids: set[str], cited_ids: list[str]) -> int:
    if result.get("route") == "escalate_to_human":
        return 0
    if any(c not in retrieved_ids for c in cited_ids):
        return 1
    texts = [c.get("text", "") for c in result.get("citations", [])]
    if any(sentence_support(s, texts) < 0.2 for s in _sentences(result.get("basis", ""))):
        return 1
    return 0


def context_relevance(retrieved: dict, expected_clauses: list[str]) -> float:
    if not expected_clauses:
        return 1.0
    have = {(p.get("clause_id") or "").lower() for p in retrieved.values()}
    return round(sum(c.lower() in have for c in expected_clauses) / len(expected_clauses), 4)


def faithfulness(result: dict, retrieved_ids: set[str], cited_ids: list[str], verified_ok: bool,
                 expected_route: str) -> int:
    resolves = all(c in retrieved_ids for c in cited_ids)
    return int(resolves and verified_ok and result.get("route") == expected_route)


def score_record(record: dict) -> dict:
    """Reference-free scores for one completed run record (used live)."""
    result = record.get("result") or {}
    retrieved_ids = set((record.get("passages") or {}).keys())
    cited = []
    for s in record.get("steps", []):
        if s.get("action") == "final" and s.get("draft"):
            cited = [c for c in s["draft"].get("citations") or [] if isinstance(c, str)]
    return {"groundedness": groundedness(result), "hallucination": hallucinated(result, retrieved_ids, cited)}
