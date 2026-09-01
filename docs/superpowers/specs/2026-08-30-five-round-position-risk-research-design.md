# 588080 Five-Round Position-Risk Research Design

## 1. Objective

The program has one portfolio objective: preserve the fee-after return of the
registered `baseline_20260826` strategy while strictly reducing maximum
drawdown for `588080.SH` through causal position management.

The champion remains the sole entry and exit authority. An overlay may only
reduce an existing champion position to `0.50` or `0.75`, restore it to `1.00`,
or follow the champion to zero. It may not create exposure, use leverage,
change champion factors or thresholds, lock profits from portfolio equity,
react to benchmark performance, or use calendar-specific overrides.

## 2. Evidence Boundary

- 2020 is warm-up only.
- 2021-2023 is the discovery segment. Rounds 1-4 may access no later data.
- 2024-2025 is a program-locked validation segment. Only round 5 may access it,
  and only for candidates frozen by rounds 2-4.
- 2026 through 2026-08-28 is a final historical check. It may be loaded only
  after a round-5 candidate passes the 2024-2025 gate and is frozen and hashed.
- All these dates have been observed elsewhere in the project. The internal
  boundary prevents feedback within this program but does not recreate an
  independent holdout. New independent forward evidence still begins after
  2026-08-28.

All executions use next-trading-day open, one-way fee `0.0005`, initial cash
`1,000,000`, no daily rebalancing, and exact champion identity from
`configs/rule_baselines/registry.json`.

## 3. Shared Measurement

Every challenge compares independently funded champion and challenger paths.
The primary eligibility rule is exact and unchanged across rounds:

1. challenger fee-after cumulative return is not below champion return; and
2. challenger maximum drawdown is numerically greater than champion maximum
   drawdown (strictly less severe).

Annual return deltas, median return delta, exposure, Sharpe ratio, number of
position changes, and family diagnostics are recorded but cannot relax the
primary rule. Eligible candidates are ranked by maximum-drawdown improvement,
worst annual return delta, full-period return delta, number of transitions,
then candidate ID.

## 4. Five Effective Rounds

### Round 1 — Holding-hazard attribution (`0830_EX03`)

Status type: diagnostic `COMPLETE`; no challenger and no PASS claim.

On champion-held days in 2021-2023, causally compute three preregistered state
families and their forward 5- and 10-session open-to-open return and maximum
adverse excursion:

- trend damage: `L=60`, `D=0.10`, plus price below the 20-day mean;
- negative-return persistence: at least 7 negative returns in 10 sessions plus
  price below the 20-day mean;
- intraday distribution pressure: close location at most `0.25`, down-bar
  volume share at least `0.70`, plus price below the 20-day mean.

The round measures event count, conditional mean/median, yearly sign, loss-tail
rate, overlap, and concentration. Forward horizon H is measured from the next
open after signal T to open T+H; adverse excursion uses intervening daily lows
relative to the next open. It does not choose thresholds for later rounds; all
later grids are fixed in this document before the diagnostic runs.

### Round 2 — Trend-damage overlay (`0830_EX04`)

Pressure begins while the champion is long when:

- close is below its completed 20-session simple moving average; and
- close is at least `D` below the prior completed `L`-session high.

Grid: `L={20,60,120}`, `D={0.06,0.10}`, `P={0.50,0.75}`: 12 candidates.
Pressure persists until close recovers to the completed 20-session mean or the
champion exits. The target change executes at the next open.

The family is `PASS` and freezes one discovery winner only if at least one
candidate satisfies the common 2021-2023 eligibility rule. Otherwise it is
`FAIL`. Multiple eligible candidates use the common ranking rule.

### Round 3 — Negative-return-persistence overlay (`0830_EX05`)

Pressure begins while the champion is long when close is below the completed
20-session mean and the completed-return count condition is one of
`(K,N)={(5,4),(10,7),(20,13)}`: at least `N` negative returns in the last `K`.
Grid adds `P={0.50,0.75}`: 6 candidates. Pressure ends after close reaches the
completed 5-session mean and the latest two completed returns are positive, or
when the champion exits.

The family applies the same discovery PASS/FAIL, ranking, and freeze behavior
as round 2.

### Round 4 — Intraday-distribution overlay (`0830_EX06`)

Daily features are built from completed 30-minute bars. Pressure begins while
the champion is long and close is below the completed 20-session mean when:

- daily close location `(close-low)/(high-low)` is at most
  `C={0.25,0.35}`; and
- down-bar volume share is at least `V={0.60,0.70}`.

Grid adds `P={0.50,0.75}`: 8 candidates. Zero-range days have neutral close
location; zero-volume days cannot trigger. Pressure ends after two consecutive
non-pressure sessions with close at or above the completed 5-session mean, or
when the champion exits.

The family applies the same discovery PASS/FAIL, ranking, and freeze behavior
as rounds 2-3.

### Round 5 — Locked validation and final historical check (`0830_EX07`)

Only the frozen, hashed family winners from rounds 2-4 are candidates. No new
threshold, combination, union, intersection, or parameter is allowed. If no
family has an eligible discovery winner, round 5 is `FAIL` without accessing
2024-2025 or 2026.

Otherwise all family winners are evaluated once on 2024-2025 and independently
on 2024 and 2025. The common eligibility and ranking rules select at most one
candidate. If none qualifies, round 5 is `FAIL` and 2026 is not accessed. If one
qualifies, it is frozen and hashed before loading 2026FULL. Final PASS requires
the same return-nondecline and strict drawdown-improvement conditions on
2026FULL; 2026Q1, 2026H1, and 2026M1-M8 are diagnostics only.

## 5. Causality and Accounting

Risk features on signal date T use no data after T. Rolling highs and means are
based on completed observations. Each `Entry`, `Reduce`, `Increase`, and `Exit`
event records feature values, family parameters, prior/target position, signal
date, and next-open execution date. Every order maps to one event. Vectorized
results and the independent cash/share ledger must agree daily.

The overlay changes shares only when its target state changes. A constant target
does not rebalance daily. Champion target zero always forces challenger target
zero.

## 6. Adaptation, Status, and Quota

Candidate spaces and gates are fixed before round 1. Later implementation may
respond to technical defects but not to performance by changing a completed
round's protocol. A pure technical `ERROR` is archived truthfully; an exact
protocol retry does not consume another effective-round allowance. `PASS`,
`FAIL`, and diagnostic `COMPLETE` each consume one of the five rounds.

Each round is preregistered and locally committed before execution. Results are
locally committed before the next round so the research chain is portable and
auditable. No branch, commit, or result is pushed to a remote. The active
baseline registry is never modified automatically.

## 7. Stopping Rules

- Do not widen a failed family grid.
- Do not reuse round-5 validation or 2026 results to revise any candidate.
- Do not manufacture a 2026 result when the prior gate fails.
- Stop after five effective rounds regardless of outcome and report the exact
  evidence, including negative results and any technical retries.
