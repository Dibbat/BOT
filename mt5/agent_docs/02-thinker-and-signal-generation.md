# 02 Thinker and Signal Generation

## 1. Core Purpose

`pt_mt5_thinker.py` converts recent market history into discrete signal levels for each coin:
- Long signal integer scale (0..7)
- Short signal integer scale (0..7)
- Confidence metadata and direction labels for logs/dashboard

It can run in cosine-only mode or blend with ML where available.

## 2. Pattern Matching Path

Main export path function:
- `export_historical_signals(...)`

Core mechanics:
1. Load long memory patterns and weights.
2. Load threshold and short-memory artifacts.
3. Build rolling change window from candles.
4. Match current window to stored patterns (`match_patterns`).
5. Convert predicted move edges into signal levels.

### 2.1 Edge-to-Level Mapping

Long side (`generate_signal`):
- Computes edge as: `high_pred - downside_risk`
- If edge <= 0 then signal is 0
- Higher edge maps to levels 1..7

Short side (`generate_short_signal`):
- Computes edge as: `downside - upside_risk`
- If edge <= 0 then signal is 0
- Higher edge maps to levels 1..7

This directional split is intentional and prevents symmetric long/short outputs from identical math.

## 3. Anti-Mirroring Guard

During export, thinker compares long memory file content with short memory file content.

If identical:
- Logs warning that short memory mirrors long memory
- Disables short-pattern scoring for that export cycle
- Forces short to rely on non-pattern path (effectively 0 if no short model input)

Reason:
- Mirrored long/short pattern files cause chronic equal signals and neutral lock behavior.

## 4. Export and Readiness

The exporter writes:
- `signal_history/<COIN>_signals.csv`

It retries failed export attempts and classifies MT5 auth errors separately.

Thinker also maintains runner readiness and staleness logic:
- If signal CSVs are stale beyond configured limits, readiness is degraded.

## 5. ML Integration (Optional)

Thinker supports ML blend with fallback:
- Blend weight from env (`PT_MT5_THINKER_ML_BLEND`)
- Hot reload by model file mtime
- If model/dependencies unavailable, thinker continues with cosine-only flow

Operational implication:
- Signals remain available even if ML scorer fails.

## 6. Expected Healthy Log Signatures

Healthy examples:
- `BTC: L5(...) S0(...) dir=LONG`
- `ETH: L2(...) S0(...) dir=LONG`
- neutral assets show `L0/S0 dir=NEUTRAL`

Unhealthy examples to investigate:
- persistent `Lx/Sx` equality across many cycles with no market regime reason
- repeated warnings about missing long memory (critical)
- repeated MT5 authorization failures in exporter

## 7. Agent Rules for Thinker Changes

When modifying thinker logic:
1. Preserve deterministic integer output range (0..7).
2. Keep long and short scoring formulas direction-specific.
3. Never reintroduce auto-mirroring assumptions between long and short memory artifacts.
4. Keep exporter failure handling non-fatal for runtime continuity.
