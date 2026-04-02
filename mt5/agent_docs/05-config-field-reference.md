# 05 Config Field Reference

This document describes practical meaning of key fields in `mt5_config.json` as used by bridge/thinker/trainer.

## 1. Connection and Runtime

- `login`, `password`, `server`
  - MT5 auth credentials.
- `terminal_path`
  - Optional MT5 terminal launch path for warmup/auth workflows.
- `signals_root`
  - Root folder for signal files.
  - Bridge sanitizes to stay within MT5 base path.
- `trade_enabled`
  - `true` live orders, `false` dry-run behavior.
- `poll_seconds`
  - Bridge reconcile interval.
- `deviation_points`
  - MT5 max slippage points for order fills.

## 2. Signal Thresholding

- `open_threshold`
  - Minimum signal level required to open/scale side.
- `close_threshold`
  - Level below which side is closed by signal fade path.
- `trade_start_level`
  - Legacy/backtest compatibility field; bridge entry logic keys off open/close thresholds.

## 3. Entry and Scaling

- `max_scale_ins`
  - Maximum per-side position count target from signal-level mapping.
- `close_on_opposite_signal`
  - Enables opposite-close behavior before reverse-side entries.
- `opposite_trade_gap_seconds`
  - Reverse-entry cooldown time after closing opposite side.
- `opposite_trade_gap_pct`
  - Reverse-entry minimum price move requirement.
- `dca_step1_pct`
  - First DCA spacing gap; subsequent gaps grow by golden ratio.

## 4. SL/TP and Position Management

- `sl_pct`, `tp_pct`
  - Base percentage stop and target.
- `use_atr_sl_tp`, `atr_sl_mult`, `atr_tp_mult`, `atr_period`
  - Optional ATR-driven SL/TP override path.
- `trailing_sl_enabled`
  - Enables trailing stop management.
- `trailing_sl_trigger_pct`
  - Profit threshold to activate trailing.
- `trailing_sl_distance_pct`
  - Distance maintained by trailing stop.
- `breakeven_enabled`
  - Enables break-even stop movement.
- `breakeven_trigger_pct`
  - Profit threshold for BE action.
- `partial_tp_enabled`
  - Enables partial take profit.
- `partial_tp_pct`
  - Profit threshold for partial close.
- `partial_tp_close_fraction`
  - Portion of position to close when partial TP fires.

## 5. Portfolio and Session Guards

- `risk_per_trade_pct`
  - Dynamic lot sizing risk budget per trade (when dynamic sizing active).
- `max_portfolio_risk_pct`
  - Blocks new entries when aggregate open risk exceeds this cap.
- `daily_loss_limit_pct`
  - If intraday equity drawdown reaches this negative percent, emergency flatten occurs.
- `signal_stale_seconds`
  - Signal freshness limit used by bridge stale guard.

## 6. Thinker/Strategy Assistance Fields

- `use_profit_margin_tp`
  - Enables thinker-supplied profit-margin close checks.
- `pm_start_pct_no_dca`, `pm_start_pct_with_dca`, `trailing_gap_pct`
  - Profit-margin strategy parameters for thinker/trailing coordination.

## 7. Per-Symbol Blocks (`symbols[]`)

Each symbol object may override global defaults:

Required practical fields:
- `bot_symbol`
- `mt5_symbol`
- `lot`
- `magic`
- `enable_long`
- `enable_short`

Common overrides:
- `sl_pct`, `tp_pct`
- `breakeven_trigger_pct`
- `partial_tp_pct`, `partial_tp_close_fraction`
- `trailing_sl_trigger_pct`, `trailing_sl_distance_pct`
- `dca_step1_pct`

## 8. Current Operational Notes in This Repo

- `max_scale_ins` is currently set to 1 in checked-in config, so bridge effectively keeps one position per side target for symbols.
- DOGE is intentionally disabled via `enable_long=false` and `enable_short=false` in current config.
- Some comments in config are strategy notes and safety explanations; they are not parsed as logic directly.
