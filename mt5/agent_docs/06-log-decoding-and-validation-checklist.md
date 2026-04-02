# 06 Log Decoding and Validation Checklist

Use this when validating whether trade logic is healthy after changes.

## 1. Runner Log Interpretation

Typical line shape:
- `<COIN>: Lx(...) Sy(...) conf=... dir=... age=...`

Interpretation:
- `Lx` and `Sy` are integer decision levels.
- `dir` is derived from comparative edge confidence.
- `age` indicates how old the current signal snapshot is.

Healthy signs:
- Signals vary by coin/regime.
- No chronic tied long/short levels unless market truly flat and model supports that.

Concern signs:
- Repeated tied levels across multiple active coins for long periods.
- Repeated exporter auth failures.

## 2. Trader Log Interpretation

Typical line shape:
- `COIN/SYMBOL sig(L/S)=a/b pos(L/S)=c/d pm(L/S)=...`

Interpretation:
- `sig(L/S)` expected side strength.
- `pos(L/S)` current open positions managed by bot magic.
- `pm(L/S)` thinker-provided profit margin targets.

Healthy signs:
- Entry only when signal reaches threshold and gates pass.
- Position counts remain bounded and consistent with `max_scale_ins`.
- No immediate opposite flip after close when reverse-gap controls are configured.

## 3. Buy-Path Validation Checklist

1. Threshold alignment
- Confirm `open_threshold` in config.
- Confirm buy signal at or above threshold in logs.

2. Entry correctness
- Verify `[OK] BUY ...` appears once when expected.
- Verify subsequent cycles show `pos(L/S)=1/0` (or expected count) without duplicate burst.

3. No unintended hedge
- Ensure no immediate opposite sell entry for same symbol unless intended by strategy and gates.

4. Tie handling
- If both sides strong and equal, expect `[AMBIG] ... skipping new entries`.

## 4. Close-Path Validation Checklist

1. Signal fade close
- For a long, when long signal drops below `close_threshold`, expect close path logs.

2. Opposite close path
- If enabled and opposite signal is strong, expect opposite side closure before reverse entry.

3. Profit-margin close
- If `use_profit_margin_tp=true`, verify PM-based close logs when conditions are met.

## 5. Risk Guard Validation Checklist

1. Portfolio cap
- Simulate high open-risk state and confirm new entries are blocked.

2. Daily loss guard
- Verify emergency flatten triggers at configured drawdown boundary.

3. Stale signals
- Make signal inputs stale and confirm no new entries occur.

4. Reverse-entry gap
- After close, verify reverse entries respect cooldown and min gap.

## 6. Fast Triage Map

If issue is "equal long/short everywhere":
- Check short memory mirroring warnings in thinker export logs.
- Confirm short scoring path is direction-aware.

If issue is "duplicate entries":
- Check duplicate guard logs.
- Verify cycle timing and position re-query before open.

If issue is "unexpected no-trade":
- Check stale signal flag, risk cap, direction enable flags, threshold values, and reverse-gap logs.

## 7. Agent Handoff Standard

When handing off to another agent, include:
1. Exact timestamp window reviewed.
2. Signal and position lines for affected symbol.
3. Config keys relevant to decision.
4. Which gate blocked or allowed action.
5. Whether behavior is expected or regression.
