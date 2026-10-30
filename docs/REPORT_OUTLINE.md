# Report outline (due one week before the presentation)

Keep it brief. Each section lists where its content comes from.

1. **Problem and scope**: brief sections 1, 2, 13. Screening opinion only; three routes plus escalation.
2. **Agent design**: topology, tools, verification, loop limits. Source: `docs/agent_design.md`.
3. **Architecture diagram with deployment boundaries marked**: `docs/architecture.md` (diagram and boundary table).
4. **Design decisions and what was rejected**: rejected alternatives in `docs/architecture.md` and `docs/chunking.md`; the stub-vs-real model split; lexical metrics vs LLM-as-judge (`docs/evaluation.md`).
5. **Evaluation results before and after the change**: the v1 → v2 table in `docs/evaluation.md`, from `eval/results/*.json`.
6. **p95 target, measured results, bottleneck, cost per 100 runs**: `loadtest/SLO.md` (target, declared first), `loadtest/RESULTS.md`, `docs/observability.md`.
7. **What did not work**: be specific. Candidates: cases the real model still gets wrong, lexical-metric limits, the toxicity lexicon, PDF table extraction (why clauses are pasted by hand), anything from `docs/incident_log.md`.

Requirements status (prioritisation is assessed): list each of F1–F8,
A1–A8, L1–L6 and D1–D8 as done, partial or dropped, with one line on why.
