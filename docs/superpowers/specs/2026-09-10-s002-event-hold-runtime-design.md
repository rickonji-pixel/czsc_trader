# S002 event-hold runtime and multi-symbol PTE design

## Goal

Make the frozen S002 mechanism executable through the same TDR advice/backtest core and PTE
account workflow as S001, without changing the immutable `S002-C001` research definition.

## Frozen semantic boundary

- Strategy kind: `czsc_event_hold`.
- Signal: daily `bar_triple_V230506`, primary state `三连跌`.
- Entry: only a fresh transition into `三连跌` while flat.
- Position: full target position for five trading sessions.
- Re-entry: ignore signals while holding; require a later fresh transition after exit.
- Risk filters: none.
- Decision: after a completed close; execution: next trading session.

The research candidate remains immutable. A Strategy Manager release wraps that behavioral
definition with the complete project-owned execution specification and preserves the candidate
hash as provenance.

## Shared TDR runtime

`strategy_runtime` becomes the single dispatch point used by advice and backtest:

- Existing S001 kinds keep the current factor-frame and baseline executor.
- `czsc_event_hold` evaluates only its declared CZSC signal and runs the audited event-hold state
  machine.
- Both paths return the same target-position, strategy-score, event and optional-regime contract.

The event-hold score is an observable indicator: `1` when the declared signal state is present,
otherwise `0`. It is diagnostic and has no independent threshold-selection role.

## Execution rule review

S002 reuses the one reviewed project execution policy:

- entry: previous unadjusted close, DAY limit order, floor to tick;
- buy fill: opening price at/below limit, otherwise first intraday strict cross; exact touch is
  conservatively unfilled;
- exit: marketable DAY limit order and opening-price fill;
- capital: full available virtual-account cash for one entry cycle;
- fee: 5 bp per side;
- lot: 100 shares.

The formal audit must replay S002 with this rule. Research metrics based on unconditional next-open
entry are evidence about the mechanism, not deployment-performance claims.

## PTE ownership model

- One Futu channel may carry many virtual accounts.
- Each virtual account owns exactly one strategy release, one symbol, one asset type and one Futu
  channel binding.
- Advice is requested with the account's symbol and asset type.
- Daily publication iterates over the distinct instruments owned by active accounts.
- Futu order placement derives the broker code from each durable account intent.
- Reconciliation compares broker and logical positions per owned symbol; any non-owned broker
  position remains a blocking safety error.

The legacy CLI `--symbol` and `--asset` remain bootstrap defaults for the original S001 account.
They no longer define the whole PTE process.

## Acceptance

1. Candidate signal replay matches the frozen S002 state machine on the research dataset.
2. Advice and backtest produce identical target transitions for the same release and data cutoff.
3. Formal execution replay produces an auditable comparison with the research next-open result.
4. Existing S001 functional behavior remains unchanged.
5. PTE can publish data, generate decisions, place orders and reconcile positions for two accounts
   with different symbols in one process.
6. Existing PTE databases migrate in place by adding a default `asset_type=etf` account field.

