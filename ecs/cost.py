"""Token and cost accounting (L5).

Cost is computed at the time of the call from the token counts the provider
reports and the prices configured in the environment (USD per 1M tokens).
Free tiers cost 0 USD, so set LLM_PRICE_* to the provider's paid list price
to report a "shadow cost" - what the same traffic would cost on a paid plan.
"""
from __future__ import annotations

from ecs.config import settings


def estimate_tokens(text: str) -> int:
    """Rough fallback when a provider does not return usage (~4 chars/token)."""
    return max(1, len(text) // 4)


def call_cost(prompt_tokens: int, completion_tokens: int,
              price_in: float | None = None, price_out: float | None = None) -> float:
    pin = settings.price_input_per_1m if price_in is None else price_in
    pout = settings.price_output_per_1m if price_out is None else price_out
    return round(prompt_tokens * pin / 1e6 + completion_tokens * pout / 1e6, 8)


def summarize_run_costs(steps: list[dict]) -> dict:
    """Per-step and per-agent cost breakdown for one run, from its trace."""
    by_step: dict[str, dict] = {}
    for s in steps:
        if not s.get("llm"):
            continue
        key = f"{s.get('agent')}:{s.get('node')}"
        agg = by_step.setdefault(key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
        agg["calls"] += 1
        agg["prompt_tokens"] += s["llm"].get("prompt_tokens", 0)
        agg["completion_tokens"] += s["llm"].get("completion_tokens", 0)
        agg["cost_usd"] = round(agg["cost_usd"] + s["llm"].get("cost_usd", 0.0), 8)
    total = {k: sum(v[k] for v in by_step.values()) for k in ("calls", "prompt_tokens", "completion_tokens")}
    total["cost_usd"] = round(sum(v["cost_usd"] for v in by_step.values()), 8)
    return {"by_step": by_step, "total": total}
