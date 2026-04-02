# 04 Risk and Safety Controls

## 1. Portfolio Risk Cap

Function:
- `check_portfolio_risk(config)`

Behavior:
- Computes approximate open risk from entry-to-SL distance across current positions.
- Blocks new entries when total open risk percent >= `max_portfolio_risk_pct`.
- Existing position management and closes still continue.

## 2. Daily Loss Limit

Functions:
- `check_daily_loss_limit(config)`
- `emergency_flatten_all(config)`

Behavior:
- Maintains day-start equity baseline in UTC day boundaries.
- If intraday equity drop reaches configured negative limit (`daily_loss_limit_pct`), triggers emergency flatten.
- Loop then pauses before next check.

## 3. Signal Staleness Guard

Behavior:
- If signal files are older than `signal_stale_seconds`, bridge logs stale state.
- Existing position management still runs.
- Entry and signal-driven close logic is skipped in stale mode to avoid acting on old signals.

## 4. Reverse Entry Gap Control

Functions:
- `_record_reverse_entry_gap(...)`
- `_reverse_entry_gap_allowed(...)`

Purpose:
- Prevent immediate side-flips after closing opposite side.

Gate dimensions:
1. Time cooldown (`opposite_trade_gap_seconds`)
2. Minimum favorable price move from reference close (`opposite_trade_gap_pct`)

If either condition is unmet, reverse entry is blocked and logged.

## 5. No-Hedge Enforcement

Behavior:
- If a long exists and short signal wants open-level entry, short entry is blocked.
- If a short exists and long signal wants open-level entry, long entry is blocked.

This runs after opposite-close attempt, so legitimate side transitions can still happen through close-first behavior.

## 6. Duplicate Position Guard

Function:
- `_enforce_max_one_per_side(...)`

Behavior:
- Detects >1 position on the same side for a symbol.
- Keeps one, closes extras, and cleans state trackers.
- Prevents accumulation from transient execution duplication.

## 7. Position Protection Stack

Per-position mechanisms:
1. Break-even stop:
- moves SL to entry once profit reaches trigger threshold.

2. Partial TP:
- closes configured fraction when partial threshold is hit.

3. Trailing SL ratchet:
- updates only in favorable direction to avoid oscillation-induced loosening.

## 8. Golden-Ratio DCA Spacing

State:
- `_dca_entry_prices` per symbol

Rule:
- Each additional DCA requires deeper price movement based on `dca_step1_pct * PHI^(level-1)`.

Purpose:
- Prevent repeated rapid-fire averaging entries with minimal price movement.

## 9. Safe Failure Modes

Implemented patterns:
- MT5 reconnect with backoff.
- Per-symbol exception capture in main loop.
- Non-fatal status writer errors.
- Explicit warnings for unsafe config/path situations (for example, `signals_root` outside allowed base).
