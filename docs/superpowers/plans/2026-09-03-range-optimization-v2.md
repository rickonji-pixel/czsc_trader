# Range Optimization V2 Implementation Plan

**Goal:** Re-evaluate the archived deployable range-weight universe against S001-v1 with the unified OPC-v1 evaluator and identify whether a freeze-worthy range platform exists.

**Architecture:** The experiment-specific runner converts immutable EX03 weight rows into complete S001 strategy payloads and a trial ledger. Trader owns factor generation, candidate backtests, execution simulation, range diagnostics, evaluation artifacts, and the final recommendation. Historical experiments remain unchanged.

## Task 1: Complete evaluator semantics for range research

- Add closed-trade range objectives to Trader candidate observations.
- Apply Profit Factor only to full/target windows.
- Verify focused evaluator and candidate-runner tests.

## Task 2: Preregister EX04

- Freeze the objective, cutoff, incumbent, execution hash, windows, OPC margins, and candidate sources.
- Add a deterministic manifest builder with identity, minimum-weight, trend-weight, execution-policy, and behavior-hash audits.
- Commit preregistration before generating performance observations.

## Task 3: Execute and review

- Build the complete candidate manifest and trial ledger.
- Run `czsc-trader strategy evaluate --experiment 0903_EX04`.
- Review the decision, four core metrics, range objective, Pareto profile, neighborhood, and stress evidence.
- Archive goal, design, execution, conclusion, review, manifest, and evaluator artifacts without changing earlier experiments.

## Task 4: Verify and hand off

- Run focused research/evaluator tests, archive validation, ruff, compile, and diff checks.
- Update the research handoff with evidence-bounded conclusions.
- Commit the completed experiment on `codex/range-optimization-v2`; do not merge or push without user approval.
