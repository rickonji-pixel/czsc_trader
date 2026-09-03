# Strategy Evaluator

The strategy_evaluator package is the deterministic, broker-neutral evaluation domain used by
CZSC Trader. It owns OPC evaluation contracts, provisional-champion audits, and decisions. It
does not load market data, run backtests, mutate strategy lifecycle state, or connect to paper
trading.

Its public pure-function workflow is `validate_protocol` → `screen_candidates` →
`rank_candidates` → `finalize_evaluation` → `render_summary`. OPC-v1 compares net CAGR,
maximum drawdown, Calmar ratio, and Profit Factor over the worst registered decision window.
Experiment-specific margins may only tighten the defaults. Repository I/O and candidate execution
belong to CZSC Trader.

OPC-v3 keeps the deterministic OPC-v2 screen and ranking, then runs one complete audit inside SE:
execution/reproducibility/ledger checks, fee and slippage stress, CSCV/PBO, Deflated Sharpe,
paired stationary bootstrap, and a real-candidate parameter-neighborhood check. Statistical
findings are reported as `FAVORABLE`, `MIXED`, or `WEAK`; they do not create a numerical automatic
veto. Trader supplies immutable factual evidence and executes the scenarios requested by SE.
