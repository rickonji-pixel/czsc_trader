# MA5/MA20 Backtest Comparison Design

## Objective

Extend every ordinary backtest so the requested window is evaluated by three
comparable strategies:

1. the registry-selected active baseline;
2. BuyHold;
3. a full-position/cash MA5/MA20 strategy.

The report presents one row per strategy and compares maximum drawdown, Calmar
ratio, win/loss ratio, return, and Sharpe ratio. Each window also receives a
separate interactive daily MA chart.

## Strategy semantics

The MA strategy uses simple moving averages of daily close:

- `MA5 = close.rolling(5, min_periods=5).mean()`;
- `MA20 = close.rolling(20, min_periods=20).mean()`;
- target position is `1.0` when `MA5 > MA20`, otherwise `0.0`;
- warmup rows before MA20 exists remain at `0.0`;
- a decision made after session `t` closes executes at session `t+1` open;
- the first session of an evaluation window uses the already-known target from
  the prior trading session;
- position size is always either 100% or 0%;
- initial cash and per-side fee equal the active baseline backtest request.

This is a persistent target-state strategy. A crossover changes the target, but
the position remains active while `MA5 > MA20` rather than only on the crossover
date.

## Shared execution and metrics

All three strategies use `run_backtest` so execution prices, fees, portfolio
accounting, Sharpe calculation, and the independent-equity reconciliation remain
identical.

For every strategy and every evaluation window, publish:

- `max_drawdown`: minimum equity divided by its prior running maximum minus one;
- `calmar`: annualized return divided by the absolute maximum drawdown;
- `win_loss_ratio`: mean positive closed-trade return divided by the absolute
  mean negative closed-trade return;
- `return`: terminal equity divided by initial cash minus one;
- `sharpe`: the existing vectorbt daily Sharpe ratio.

Calmar is `null` when maximum drawdown is zero or the annualization cannot be
calculated. Win/loss ratio uses only complete Buy-Sell pairs and is `null` unless
at least one profitable and one losing closed trade exist. BuyHold therefore
normally reports `null`; Markdown renders every null metric as `N/A`.

The active baseline's final open trade and the MA strategy's final open trade are
not synthetically liquidated for win/loss ratio. Return and equity still include
the marked-to-market final close, matching the existing backtest convention.

## Result schema

Replace the current flat difference-oriented window metrics with one coherent
three-strategy structure:

```json
{
  "windows": {
    "2026FULL": {
      "start": "2026-01-05",
      "end": "2026-09-01",
      "strategies": {
        "active_baseline": {
          "max_drawdown": -0.12,
          "calmar": 0.0,
          "win_loss_ratio": null,
          "return": 0.0,
          "sharpe": 0.0
        },
        "buyhold": {},
        "ma5_ma20": {}
      }
    }
  }
}
```

The same schema is written to `metrics.json`, returned by the Python runner, and
exposed by the CLI. Old difference fields are removed rather than duplicated.

## Artifacts

Keep existing active-baseline artifacts and add auditable MA artifacts for each
window:

- `ma_signals.csv`: date, close, MA5, MA20, and target position over the causal
  data span;
- `ma_orders<suffix>.csv`: MA orders with signal and execution dates;
- `ma_equity<suffix>.csv`: MA equity and target/execution positions;
- `ma_chart<suffix>.html`: independent interactive MA daily chart.

The existing suffix convention remains unchanged: a direct start/end run named
`full` uses no suffix, while configured names such as `2026Q1` use `_2026Q1`.

The manifest records the MA strategy parameters, both chart names, and the new
metrics schema. Baseline audit artifacts remain intact. MA causality is verified
by its deterministic target construction and order signal/execution dates.

## Report

`report.md` contains one subsection per window and exactly this table shape:

| 策略 | 最大回撤 | 卡玛比率 | 盈亏比 | 收益率 | 夏普率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 活动基线 | value | value | value or N/A | value | value |
| BuyHold | value | value | N/A | value | value |
| MA5/MA20 | value | value | value or N/A | value | value |

Percent formatting applies to maximum drawdown and return. Ratios use three
decimal places. The report's chart section links both the existing baseline chart
and the MA chart for every window.

## MA daily chart

The MA chart is a separate single-panel Plotly HTML file containing:

- daily candlesticks;
- MA5 and MA20 lines;
- MA Buy and MA Sell execution markers;
- one unified hover panel showing OHLC, MA5, and MA20 for the selected date;
- non-trading-date range breaks, including statutory holidays;
- a title containing symbol, window name, and `MA5/MA20`.

It does not duplicate CZSC pens, divergence markers, factor signals, or the factor
subplot. The current active-baseline chart is unchanged.

## Components

- `src/czsc_trader/moving_average.py`: pure MA signal/target construction.
- `src/czsc_trader/strategy_metrics.py`: generic closed-trade ledger and the five
  comparison metrics.
- `src/czsc_trader/ma_charting.py`: MA-only Plotly chart construction and writing.
- `src/czsc_trader/backtest_runner.py`: orchestrates the three strategies, result
  schema, artifacts, report, and manifest.
- `tests/test_cli_e2e.py`: retains one network-free end-to-end test and asserts the
  new public contract and artifacts.
- `README.md` and `docs/RESEARCH_HANDOFF.md`: document the comparison strategy,
  metric definitions, and additional chart.

## Failure handling

The existing atomic output-directory behavior remains. Any invalid price series,
missing prior trading session, non-finite target, failed independent-equity
reconciliation, or chart/report write failure creates `failure.json` and prevents
the run from being reported as PASS.

## Acceptance criteria

- MA5/MA20 uses only same-day and earlier closes and executes one trading session
  later at the open.
- Active baseline, BuyHold, and MA use identical cash, fees, dates, and accounting.
- Every window contains exactly three strategy metrics with the five requested
  fields.
- Undefined win/loss ratio and Calmar serialize as JSON null and render as `N/A`.
- `report.md` contains exactly three strategy rows per window.
- Every window produces a separate MA chart plus MA signal, order, and equity CSVs.
- The MA chart shows candles, MA5, MA20, execution markers, unified hover, and no
  holiday gaps.
- Existing active-baseline chart and audit remain valid.
- The single end-to-end test passes without network access.
