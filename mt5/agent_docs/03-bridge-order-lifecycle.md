# 03 Bridge Order Lifecycle

## 1. Core Function

Bridge reconciliation is centered in:
- `reconcile_symbol(config, sym_cfg, skip_close=False)`

Every poll cycle, for each configured symbol, it performs:
1. Read signals and staleness state.
2. Read open positions split by long/short.
3. Apply duplicate-position cleanup.
4. Run risk management on existing positions.
5. Evaluate closes.
6. Evaluate new entries.

## 2. Signal Inputs

Bridge reads per coin:
- long signal level
- short signal level
- long profit margin target
- short profit margin target
- stale flag

Stale behavior:
- Existing position management still runs.
- New signal-driven open/close actions are skipped when stale path is active.

## 3. Pre-Entry Management

Before opening anything, bridge runs:
- Break-even manager
- Partial TP manager
- Trailing SL manager

This keeps open trades maintained independently from immediate entry decisions.

## 4. Opposite-Close and No-Hedge Flow

Sequence is intentional:
1. Try opposite-side close when enabled and signal supports reversal.
2. Then enforce hard no-hedge guard.

Effect:
- Existing opposite positions are closed before considering fresh opposite entries.
- If opposite position remains, new hedge entry is blocked.

## 5. Entry Eligibility Gates

A side can open only if all required gates pass:

1. Signal strength gate:
- Uses `open_threshold` logic.

2. Portfolio risk gate:
- `check_portfolio_risk(...)` must allow new entries.

3. Reverse-entry cooldown/price gap gate:
- `_reverse_entry_gap_allowed(...)` must pass.

4. Ambiguity gate:
- If long and short are both strong and tied, skip entries.
- If both strong but one stronger, dominant side is preferred.

5. Symbol direction enable gate:
- `enable_long` / `enable_short` must be true.

6. Scale target gate:
- Current side position count must be below target count computed from signal level and `max_scale_ins`.

7. Golden DCA spacing gate:
- `_golden_dca_allowed(...)` enforces minimum price movement from prior entry.

## 6. Entry Submission

When all gates pass:
- Bridge sends market order with side-specific comment:
  - `pt-long:<coin>` or `pt-short:<coin>`
- On success:
  - logs ticket/price/sl/tp
  - records DCA entry price state
  - appends trade history record

## 7. Close Paths

Bridge can close from three major paths:

1. Opposite-signal close path
- Triggered when opposite direction reaches open-level conditions and hold-time conditions are satisfied.

2. Signal-fade close path
- If `long_sig < close_threshold`, close longs.
- If `short_sig < close_threshold`, close shorts.

3. Profit-margin TP path
- `evaluate_take_profit(...)` can close based on thinker-supplied margin targets when enabled.

## 8. Main Loop Behavior

`run_loop(...)` also enforces:
- Daily loss guard check before symbol reconcile
- MT5 reconnect attempts on disconnect
- Per-symbol exception isolation (one symbol failure does not kill whole loop)
- Trader status writing after reconcile pass

## 9. Agent Rules for Bridge Changes

1. Preserve action ordering: manage -> close -> open.
2. Do not place no-hedge checks before opposite-close attempts.
3. Preserve stale-signal safety behavior.
4. Keep per-cycle duplicate cleanup and one-step idempotence.
5. Keep all entry gates explicit in logs to aid diagnostics.
