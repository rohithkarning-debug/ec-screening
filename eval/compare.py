"""Compare two evaluation runs (L2 prompt variants, L3 before/after a change).

    python -m eval.compare eval/results/before.json eval/results/after.json

Prints a table of every metric before, after and the change, and which
scenarios flipped between pass and fail.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LOWER_IS_BETTER = {"hallucination_rate", "mean_cost_usd", "mean_duration_s"}


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    a, b = (json.loads(Path(p).read_text()) for p in argv)
    print(f"| Metric | {a['label']} | {b['label']} | Change | Better |")
    print("| --- | --- | --- | --- | --- |")
    for k, va in a["summary"].items():
        vb = b["summary"].get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and not isinstance(va, bool):
            d = round(vb - va, 4)
            better = (d < 0) if k in LOWER_IS_BETTER else (d > 0)
            print(f"| {k} | {va} | {vb} | {d:+} | {'yes' if better else ('same' if d == 0 else 'no')} |")
        else:
            print(f"| {k} | {va} | {vb} | | |")
    ra = {r["id"]: r["passed"] for r in a["rows"]}
    flips = [(r["id"], ra.get(r["id"]), r["passed"]) for r in b["rows"] if ra.get(r["id"]) != r["passed"]]
    if flips:
        print("\nScenarios that changed:")
        for i, x, y in flips:
            print(f"- {i}: {'pass' if x else 'fail'} -> {'pass' if y else 'fail'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
