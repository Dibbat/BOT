# 01 Runtime Architecture

## 1. Components

The MT5 stack is composed of three operational layers:

1. Thinker (`pt_mt5_thinker.py`)
- Produces per-coin signal files and confidence values.
- Optionally exports fresh historical signal CSVs from MT5 candles.
- Writes readiness state for orchestration/dashboard.

2. Bridge/Trader (`pt_mt5_bridge.py`)
- Reads long/short signal levels and optional profit-margin files.
- Reconciles open positions against signal state every poll cycle.
- Executes entries/exits and risk controls through MT5.

3. Trainer (`pt_mt5_trainer.py`)
- Learns pattern memories and weights from MT5 candle history.
- Maintains long-memory artifacts and status metadata.
- Does not auto-create short memory by copying long memory.

## 2. Data and File Flow

1. Trainer artifacts (per coin, per timeframe):
- `memories_<tf>.txt`
- `memory_weights_<tf>.txt`
- optional short counterparts if genuinely trained:
  - `memories_short_<tf>.txt`
  - `memory_weights_short_<tf>.txt`

2. Thinker export output:
- `signal_history/<COIN>_signals.csv` with columns: `time,long_sig,short_sig`

3. Thinker runtime signal outputs consumed by bridge:
- `long_dca_signal.txt`
- `short_dca_signal.txt`
- `futures_long_profit_margin.txt`
- `futures_short_profit_margin.txt`
- `signal_confidence.txt` (auxiliary visibility)

4. Bridge status output:
- trader status JSON in hub data path

## 3. Runtime Loop Timing

- Thinker:
  - Poll loop cadence from thinker env settings.
  - Export refresh cadence from thinker export interval.
- Bridge:
  - Reconcile loop cadence from `poll_seconds` in config.

## 4. High-Level Decision Sequence

1. Thinker computes `long_sig` and `short_sig` from pattern matching.
2. Bridge reads those levels and current MT5 positions.
3. Bridge always runs position management first (BE/partial/trailing).
4. Bridge applies opposite-close and no-hedge protections.
5. Bridge evaluates entry eligibility (risk caps, stale guard, reverse gap, DCA spacing, thresholds).
6. Bridge submits at most one entry per side evaluation path and logs every action.

## 5. Safety Philosophy in Current Code

- Signal ambiguity avoidance:
  - If both directions are strong and tied, skip new entries for that cycle.
- No structural long/short mirroring:
  - Short scorer uses downside-vs-upside edge, not long scorer reuse.
  - Mirrored short files are detected and ignored in export path.
- Anti-churn controls:
  - Reverse-entry cooldown and price-gap gate.
  - Optional opposite-close behavior and no-hedge blocking.
