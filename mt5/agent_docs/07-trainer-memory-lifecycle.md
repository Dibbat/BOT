# 07 Trainer Memory Lifecycle

## 1. Purpose

`pt_mt5_trainer.py` builds and updates pattern memory used by thinker scoring.

Main entry point:
- `train_mt5(...)`

Outputs:
- `memories_<tf>.txt`
- `memory_weights_<tf>.txt`
- `trainer_status.json`

## 2. Training Data Path

1. Initialize MT5 with config auth.
2. Fetch candles over lookback window for selected symbol/timeframe.
3. Build feature streams:
- close-to-close percent changes
- high/open and low/open change series
- ATR-based normalization context

## 3. Pattern Construction

For each valid index window:
1. Build normalized window of fixed `pattern_length`.
2. Compute future move aggregates over `candles_to_predict`.
3. Apply quality filter (`min_move_pct`) to skip weak/noise patterns.
4. Encode pattern as string with embedded future high/low targets.

## 4. Update Strategy

- New pattern key:
  - append memory entry and initial recency-based weight.
- Existing pattern key:
  - increase weight with recency factor, capped for stability.

Recency handling:
- Uses half-life style exponential decay so newer bars influence updates more.

## 5. Current Short-Memory Policy

Intentional current behavior:
- Trainer does not auto-copy long memory into short memory files.
- If dedicated short files are absent, trainer logs informational note.

Reason:
- Auto-copying created mirrored long/short memories and caused equal-signal artifacts.

Operational implication:
- Short pattern scoring remains inactive until true short memory artifacts are trained/provided.

## 6. Status Metadata

Trainer writes `trainer_status.json` including:
- total patterns
- new and updated counts for run
- quality-skipped count
- candles used
- timeframe and timestamp

This file is used for visibility and troubleshooting progression across runs.

## 7. Agent Rules for Trainer Changes

1. Preserve deterministic pattern encoding compatibility with thinker parser.
2. Do not reintroduce automatic long-to-short artifact mirroring.
3. Keep quality gate explicit and configurable.
4. Keep status JSON backward-friendly and additive where possible.
