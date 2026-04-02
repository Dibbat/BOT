#!/usr/bin/env python3
"""
MT5 Neural Trainer for PowerTrader  —  v2.0

Changes from v1:
  • Recency weighting     — recent candles count more than old ones
  • ATR-normalized scoring— pattern moves expressed as ATR multiples,
                            making patterns portable across market regimes
  • Duplicate merging     — increments weight on exact match instead of
                            growing the file unboundedly
  • Pattern quality gate  — discards patterns with near-zero predicted move
  • Multi-timeframe prep  — --timeframes flag trains all listed TFs in one run
  • Incremental mode      — --incremental skips if memory is fresh enough
  • Enhanced status JSON  — records per-timeframe stats for the dashboard

Usage:
  python mt5/pt_mt5_trainer.py --coin BTC --timeframes 1hour 4hour 1day \\
      --terminal-path "C:\\path\\to\\terminal64.exe" --lookback-days 90
"""

import argparse
import json
import os
import sys
import time
import traceback
import platform
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional

try:
    mt5 = __import__("MetaTrader5")
except ImportError:
    os_name = platform.system() or "Unknown"
    if os_name == "Windows":
        print("MetaTrader5 not installed. Run: pip install -r requirements.txt")
    else:
        print(f"MetaTrader5 not available on {os_name}. Must run on Windows with MT5.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    line = f"[{now()}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        safe = line.encode(enc, errors="replace").decode(enc, errors="replace")
        print(safe, flush=True)


def _default_config_path() -> str:
    """Pick a stable default config path regardless of process CWD."""
    script_dir = os.path.abspath(os.path.dirname(__file__))
    candidates = [
        os.path.join(script_dir, "mt5_config.json"),
        os.path.join(os.path.dirname(script_dir), "mt5_config.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


def _resolve_config_path(path: str) -> str:
    """Resolve relative config path against common launch roots."""
    raw = str(path or "").strip()
    if not raw:
        return _default_config_path()
    if os.path.isabs(raw):
        return raw

    script_dir = os.path.abspath(os.path.dirname(__file__))
    candidates = [
        os.path.abspath(raw),
        os.path.abspath(os.path.join(script_dir, raw)),
        os.path.abspath(os.path.join(os.path.dirname(script_dir), raw)),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return candidates[0]


# ---------------------------------------------------------------------------
# Timeframe helpers
# ---------------------------------------------------------------------------

_TF_ALIASES = {
    "m1": "1min",   "1m": "1min",   "1min": "1min",
    "m5": "5min",   "5m": "5min",   "5min": "5min",
    "m15": "15min", "15m": "15min", "15min": "15min",
    "m30": "30min", "30m": "30min", "30min": "30min",
    "h1": "1hour",  "1h": "1hour",  "1hour": "1hour",
    "h4": "4hour",  "4h": "4hour",  "4hour": "4hour",
    "d1": "1day",   "1d": "1day",   "1day": "1day",
    "w1": "1week",  "1w": "1week",  "1week": "1week",
}

_LABEL_TO_MT5 = {
    "1min":  "M1",
    "5min":  "M5",
    "15min": "M15",
    "30min": "M30",
    "1hour": "H1",
    "4hour": "H4",
    "1day":  "D1",
    "1week": "W1",
}

_MT5_TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
    "W1":  mt5.TIMEFRAME_W1,
}


def normalize_timeframe(name: str) -> str:
    raw = str(name).strip().lower()
    if raw not in _TF_ALIASES:
        raise ValueError(f"Unsupported timeframe: {name}. Use: {', '.join(_TF_ALIASES)}")
    return _TF_ALIASES[raw]


def tf_to_mt5_const(label: str) -> int:
    key = _LABEL_TO_MT5.get(label)
    if key is None:
        raise ValueError(f"Unknown label: {label}")
    return _MT5_TF_MAP[key]


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def load_memory_file(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return [p.strip() for p in content.split("~") if p.strip()] if content else []
    except Exception as e:
        log(f"[WARN] Failed to load memory {path}: {e}")
        return []


def save_memory_file(path: str, patterns: List[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("~".join(patterns))
    log(f"[SAVE] Memory: {path} ({len(patterns)} patterns)")


def load_weights_file(path: str) -> List[float]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        weights = []
        for tok in content.replace("[", "").replace("]", "").replace(",", "").split():
            try:
                weights.append(float(tok))
            except ValueError:
                pass
        return weights
    except Exception:
        return []


def save_weights_file(path: str, weights: List[float]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(" ".join(f"{w:.4f}" for w in weights))
    log(f"[SAVE] Weights: {path}")


def resolve_coin_dir(memory_dir: str, coin: str, base_dir: str) -> str:
    coin_key = str(coin).upper()
    if os.path.basename(memory_dir).upper() == coin_key:
        return memory_dir
    return os.path.join(memory_dir, coin_key)


def write_coin_training_state(
    coin_dir: str,
    coin: str,
    state: str,
    started_at: int,
    finished_at: Optional[int] = None,
    extra: Optional[Dict[str, object]] = None,
) -> None:
    payload: Dict[str, object] = {
        "coin": str(coin).upper(),
        "state": str(state).upper(),
        "started_at": int(started_at),
        "timestamp": int(time.time()),
    }
    if finished_at is not None:
        payload["finished_at"] = int(finished_at)
    if extra:
        payload.update(extra)

    try:
        os.makedirs(coin_dir, exist_ok=True)
        with open(os.path.join(coin_dir, "trainer_status.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log(f"[WARN] Could not write trainer status for {coin}: {e}")

    if str(state).upper() == "FINISHED":
        ts = int(finished_at if finished_at is not None else time.time())
        try:
            with open(os.path.join(coin_dir, "trainer_last_training_time.txt"), "w", encoding="utf-8") as f:
                f.write(str(ts))
        except Exception as e:
            log(f"[WARN] Could not write trainer timestamp for {coin}: {e}")


# ---------------------------------------------------------------------------
# ATR calculation helper
# ---------------------------------------------------------------------------

def compute_atr(highs: List[float], lows: List[float], closes: List[float],
                period: int = 14) -> List[float]:
    """
    Returns a list of ATR values (same length as closes, NaN for first entries).
    Uses Wilder's smoothed ATR.
    """
    n = len(closes)
    if n < 2:
        return [0.0] * n

    trs = [0.0]
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    atr: List[float] = [0.0] * n
    if n <= period:
        return atr

    # Seed: simple average for first window
    atr[period] = sum(trs[1:period + 1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period

    return atr


# ---------------------------------------------------------------------------
# Core training function
# ---------------------------------------------------------------------------

def train_mt5(
    coin: str,
    mt5_symbol: str,
    timeframe: str,
    terminal_path: str,
    lookback_days: int = 60,
    pattern_length: int = 12,
    candles_to_predict: int = 2,
    memory_dir: str = "",
    min_move_pct: float = 0.10,        # quality gate: discard patterns where predicted future
                                        # move (|avg_high|+|avg_low| in % units) < this value.
                                        # 0.20 = filter patterns predicting < 0.2% move (noise).
                                        # Root cause fix: old param was 'ATR multiples' but was
                                        # compared against raw % making the gate never fire (0 filtered).
    recency_half_life_days: int = 30,  # recent candles weighted higher
    incremental: bool = False,
    incremental_max_age_hours: int = 24,
    started_at: Optional[int] = None,
) -> Dict:
    """
    Train pattern memory on MT5 historical candles.

    Returns a dict with training statistics.
    """
    tf_label = normalize_timeframe(timeframe)
    log(f"[TRAIN] {coin} {tf_label} — lookback={lookback_days}d "
        f"pattern_len={pattern_length} predict={candles_to_predict}")


    start_ts = int(started_at if started_at is not None else time.time())

    # --- FIX: Use per-coin subdirectory for all memory files ---
    base_dir = os.path.abspath(os.path.dirname(__file__))
    memory_dir = os.path.abspath(memory_dir or base_dir)
    if os.path.commonpath([base_dir, memory_dir]) != base_dir:
        log(f"[WARN] memory_dir outside mt5 ignored: {memory_dir} -> {base_dir}")
        memory_dir = base_dir
    coin_dir = resolve_coin_dir(memory_dir, coin, base_dir)
    os.makedirs(coin_dir, exist_ok=True)

    write_coin_training_state(coin_dir, coin, "TRAINING", started_at=start_ts)

    mem_path     = os.path.join(coin_dir, f"memories_{tf_label}.txt")
    weights_path = os.path.join(coin_dir, f"memory_weights_{tf_label}.txt")
    mem_path_short     = os.path.join(coin_dir, f"memories_short_{tf_label}.txt")
    weights_path_short = os.path.join(coin_dir, f"memory_weights_short_{tf_label}.txt")

    # Incremental mode: skip only when BOTH long+short files are fresh and non-mirrored.
    force_rebuild_short = False
    if incremental and os.path.isfile(mem_path) and os.path.isfile(mem_path_short):
        age_hours_long = (time.time() - os.path.getmtime(mem_path)) / 3600.0
        age_hours_short = (time.time() - os.path.getmtime(mem_path_short)) / 3600.0
        long_short_mirrored = False
        try:
            if os.path.isfile(weights_path) and os.path.isfile(weights_path_short):
                with open(mem_path, "r", encoding="utf-8") as f1, open(mem_path_short, "r", encoding="utf-8") as f2:
                    mem_same = f1.read().strip() == f2.read().strip()
                with open(weights_path, "r", encoding="utf-8") as f1, open(weights_path_short, "r", encoding="utf-8") as f2:
                    w_same = f1.read().strip() == f2.read().strip()
                long_short_mirrored = bool(mem_same and w_same)
        except Exception:
            long_short_mirrored = False

        if age_hours_long < incremental_max_age_hours and age_hours_short < incremental_max_age_hours and not long_short_mirrored:
            age_hours = max(age_hours_long, age_hours_short)
            log(f"[TRAIN] Incremental skip: memory is {age_hours:.1f}h old "
                f"(limit={incremental_max_age_hours}h)")
            existing = load_memory_file(mem_path)
            finished_ts = int(time.time())
            write_coin_training_state(
                coin_dir,
                coin,
                "FINISHED",
                started_at=start_ts,
                finished_at=finished_ts,
                extra={"skipped": True, "reason": "incremental", "timeframe": tf_label},
            )
            return {"coin": coin, "timeframe": tf_label, "patterns": len(existing),
                    "skipped": True, "reason": "incremental"}
        if long_short_mirrored:
            log("[TRAIN] Incremental bypass: long/short memories are mirrored, rebuilding dedicated short memory")
            force_rebuild_short = True

    # Initialize MT5
    try:
        ok = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    except Exception as e:
        mt5_err = None
        try:
            mt5_err = mt5.last_error()
        except Exception:
            mt5_err = str(e)
        log(f"[ERROR] MT5 init failed: {e}")
        write_coin_training_state(
            coin_dir,
            coin,
            "FAILED",
            started_at=start_ts,
            extra={"error": "auth_failed", "mt5_error": str(mt5_err), "timeframe": tf_label},
        )
        try:
            mt5.shutdown()
        except Exception:
            pass
        return {
            "coin": coin,
            "timeframe": tf_label,
            "patterns": 0,
            "error": "auth_failed",
            "mt5_error": str(mt5_err),
        }

    # Fetch candles
    tf_const = tf_to_mt5_const(tf_label)
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    log(f"[TRAIN] Fetching {mt5_symbol} {tf_label} from {start_dt.date()} to {end_dt.date()}...")

    candles = mt5.copy_rates_range(mt5_symbol, tf_const, start_dt, end_dt)
    if candles is None or len(candles) == 0:
        log(f"[ERROR] No candles for {mt5_symbol}")
        write_coin_training_state(
            coin_dir,
            coin,
            "FAILED",
            started_at=start_ts,
            extra={"error": "no_candles", "timeframe": tf_label, "mt5_symbol": mt5_symbol},
        )
        try:
            mt5.shutdown()
        except Exception:
            pass
        return {"coin": coin, "timeframe": tf_label, "patterns": 0, "error": "no_candles"}

    log(f"[TRAIN] Fetched {len(candles)} candles")

    # Extract OHLC arrays
    def _f(c, k: str) -> float:
        try:
            return float(c[k])
        except Exception:
            try:
                return float(getattr(c, k))
            except Exception:
                return 0.0

    closes  = [_f(c, "close")  for c in candles]
    opens   = [_f(c, "open")   for c in candles]
    highs   = [_f(c, "high")   for c in candles]
    lows    = [_f(c, "low")    for c in candles]
    times   = [int(_f(c, "time")) for c in candles]

    n = len(closes)

    # ── ATR for normalization and quality gating ──
    atr_values = compute_atr(highs, lows, closes, period=14)

    # ── Price change series (% close-to-close) ──
    pct_changes:  List[float] = [0.0]   # index 0 = placeholder
    high_changes: List[float] = [0.0]
    low_changes:  List[float] = [0.0]

    for i in range(1, n):
        prev_close = closes[i - 1]
        if prev_close > 0:
            pct_changes.append((closes[i] - prev_close) / prev_close * 100.0)
            high_changes.append((highs[i]  - opens[i - 1]) / opens[i - 1] * 100.0 if opens[i - 1] > 0 else 0.0)
            low_changes.append( (lows[i]   - opens[i - 1]) / opens[i - 1] * 100.0 if opens[i - 1] > 0 else 0.0)
        else:
            pct_changes.append(0.0)
            high_changes.append(0.0)
            low_changes.append(0.0)

    log(f"[TRAIN] {len(pct_changes)-1} price change bars extracted")

    # ── Recency weights ──
    # Candle at index i gets weight exp(-(n-1-i) / half_life_in_candles)
    # so candles near end_dt have weight ≈ 1.0
    bars_per_day  = max(1, int(len(candles) / lookback_days))
    half_life_bars = recency_half_life_days * bars_per_day
    import math
    recency_w: List[float] = [
        math.exp(-(n - 1 - i) / max(1, half_life_bars))
        for i in range(n)
    ]

    # ── Load existing patterns ──
    memory_patterns = load_memory_file(mem_path)
    weights         = load_weights_file(weights_path)
    memory_patterns_short = load_memory_file(mem_path_short)
    weights_short         = load_weights_file(weights_path_short)

    # If short artifacts mirror long artifacts, clear short state and rebuild
    # from bearish-edge windows only.
    if not force_rebuild_short:
        try:
            if (
                os.path.isfile(mem_path)
                and os.path.isfile(mem_path_short)
                and os.path.isfile(weights_path)
                and os.path.isfile(weights_path_short)
            ):
                with open(mem_path, "r", encoding="utf-8") as f1, open(mem_path_short, "r", encoding="utf-8") as f2:
                    mem_same = f1.read().strip() == f2.read().strip()
                with open(weights_path, "r", encoding="utf-8") as f1, open(weights_path_short, "r", encoding="utf-8") as f2:
                    w_same = f1.read().strip() == f2.read().strip()
                if mem_same and w_same:
                    force_rebuild_short = True
                    log("[TRAIN] Detected mirrored long/short files on load, rebuilding dedicated short memory")
        except Exception:
            pass

    if force_rebuild_short:
        memory_patterns_short = []
        weights_short = []

    if len(weights) < len(memory_patterns):
        weights.extend([1.0] * (len(memory_patterns) - len(weights)))
    if len(weights_short) < len(memory_patterns_short):
        weights_short.extend([1.0] * (len(memory_patterns_short) - len(weights_short)))

    existing_key_to_idx: Dict[str, int] = {}
    for idx, p in enumerate(memory_patterns):
        key = str(p).split("{}", 1)[0]
        if key and key not in existing_key_to_idx:
            existing_key_to_idx[key] = idx

    existing_key_to_idx_short: Dict[str, int] = {}
    for idx, p in enumerate(memory_patterns_short):
        key = str(p).split("{}", 1)[0]
        if key and key not in existing_key_to_idx_short:
            existing_key_to_idx_short[key] = idx

    log(f"[TRAIN] Loaded long={len(memory_patterns)} short={len(memory_patterns_short)} patterns")

    # ── Extract new patterns ──
    new_count    = 0
    updated_count = 0
    new_count_short = 0
    updated_count_short = 0
    skipped_quality = 0

    MIN_CANDLES_REQUIRED = pattern_length + candles_to_predict + 1

    for i in range(pattern_length, n - candles_to_predict):
        # Sliding window of pct_changes
        window = pct_changes[i - pattern_length:i]
        if len(window) < pattern_length:
            continue

        # ATR at this bar for normalization (fallback to simple average)
        bar_atr = atr_values[i] if atr_values[i] > 0 else (
            sum(abs(c) for c in window) / len(window) or 1.0
        )

        # Normalize window by ATR so patterns are regime-independent
        window_norm = [round(v / bar_atr, 4) for v in window] if bar_atr > 0 else window

        # Future predicted move
        future_highs = high_changes[i: i + candles_to_predict]
        future_lows  = low_changes[i: i + candles_to_predict]
        avg_high = sum(future_highs) / len(future_highs) if future_highs else 0.0
        avg_low  = sum(future_lows)  / len(future_lows)  if future_lows  else 0.0

        # Quality gate: skip patterns with tiny predicted future moves (noise, not signal).
        expected_move_pct = abs(avg_high) + abs(avg_low)
        if expected_move_pct < min_move_pct:
            skipped_quality += 1
            continue

        pattern_str = " ".join(f"{v:.4f}" for v in window_norm)
        pattern_entry = f"{pattern_str}{{}}{avg_high:.4f}{{}}{avg_low:.4f}"

        idx_existing = existing_key_to_idx.get(pattern_str)
        rec_w = recency_w[i]  # recency multiplier

        if idx_existing is None:
            memory_patterns.append(pattern_entry)
            weights.append(rec_w)
            existing_key_to_idx[pattern_str] = len(memory_patterns) - 1
            new_count += 1
        else:
            if idx_existing < len(weights):
                # Boost weight by recency, cap at 10.0
                weights[idx_existing] = min(float(weights[idx_existing]) + 0.1 * rec_w, 10.0)
                updated_count += 1

        # Dedicated short memory: keep bearish-edge windows for short-side scoring.
        downside = abs(avg_low) if avg_low < 0 else 0.0
        upside_risk = avg_high if avg_high > 0 else 0.0
        short_edge = downside - upside_risk
        if short_edge >= min_move_pct:
            idx_existing_short = existing_key_to_idx_short.get(pattern_str)
            if idx_existing_short is None:
                memory_patterns_short.append(pattern_entry)
                weights_short.append(rec_w)
                existing_key_to_idx_short[pattern_str] = len(memory_patterns_short) - 1
                new_count_short += 1
            elif idx_existing_short < len(weights_short):
                weights_short[idx_existing_short] = min(float(weights_short[idx_existing_short]) + 0.1 * rec_w, 10.0)
                updated_count_short += 1

    log(f"[TRAIN] Patterns: {new_count} new, {updated_count} updated, "
        f"{skipped_quality} quality-filtered (min_move={min_move_pct:.2f}%)")
    log(f"[TRAIN] Short patterns: {new_count_short} new, {updated_count_short} updated")
    log(f"[TRAIN] Total patterns: long={len(memory_patterns)} short={len(memory_patterns_short)}")

    # ── Save ──
    save_memory_file(mem_path, memory_patterns)
    save_weights_file(weights_path, weights)
    save_memory_file(mem_path_short, memory_patterns_short)
    save_weights_file(weights_path_short, weights_short)

    # ── Status JSON ──
    status_path = os.path.join(coin_dir, "trainer_status.json")
    existing_status: Dict = {}
    if os.path.isfile(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                existing_status = json.load(f)
        except Exception:
            pass

    per_tf = existing_status.get("per_timeframe", {})
    per_tf[tf_label] = {
        "patterns_long":   len(memory_patterns),
        "patterns_short":  len(memory_patterns_short),
        "new_long":        new_count,
        "updated_long":    updated_count,
        "new_short":       new_count_short,
        "updated_short":   updated_count_short,
        "quality_skipped": skipped_quality,
        "candles_used":   n,
        "lookback_days":  lookback_days,
        "trained_at":     int(datetime.now(timezone.utc).timestamp()),
    }

    status = {
        "coin":             coin,
        "state":            "FINISHED",
        "timestamp":        int(datetime.now(timezone.utc).timestamp()),
        "patterns_saved_long":  len(memory_patterns),
        "patterns_saved_short": len(memory_patterns_short),
        "total_patterns_long":  len(memory_patterns),
        "total_patterns_short": len(memory_patterns_short),
        "lookback_days":    lookback_days,
        "timeframe":        tf_label,
        "per_timeframe":    per_tf,
    }

    os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)

    finished_ts = int(time.time())
    write_coin_training_state(
        coin_dir,
        coin,
        "FINISHED",
        started_at=start_ts,
        finished_at=finished_ts,
        extra={"timeframe": tf_label, "patterns_saved_long": len(memory_patterns), "patterns_saved_short": len(memory_patterns_short)},
    )

    log(f"[TRAIN] Done! {len(memory_patterns)} patterns saved -> {mem_path}")
    log(f"[TRAIN] Status -> {status_path}")

    try:
        mt5.shutdown()
    except Exception:
        pass

    return {
        "coin":            coin,
        "timeframe":       tf_label,
        "patterns_long":   len(memory_patterns),
        "patterns_short":  len(memory_patterns_short),
        "new_long":        new_count,
        "updated_long":    updated_count,
        "new_short":       new_count_short,
        "updated_short":   updated_count_short,
        "quality_skipped": skipped_quality,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:

    parser = argparse.ArgumentParser(description="PowerTrader MT5 Trainer v2")
    parser.add_argument("--coin", help="Coin symbol, e.g. BTC")
    parser.add_argument("--all-coins", action="store_true", help="Train all enabled coins from config")
    parser.add_argument("--config", default=_default_config_path(), help="Path to config file for all-coins mode")
    parser.add_argument("--mt5-symbol", default=None, help="MT5 symbol (auto-resolved if omitted)")
    parser.add_argument("--timeframe", default="1hour", help="Single timeframe (legacy flag, use --timeframes for multi)")
    parser.add_argument("--timeframes", nargs="+", default=None, help="One or more timeframes to train: 1hour 4hour 1day")
    parser.add_argument("--terminal-path", default="", help="Path to terminal64.exe")
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--pattern-length", type=int, default=12)
    parser.add_argument("--candles-predict", type=int, default=2, help="Candles ahead to predict for TP/SL scoring")
    parser.add_argument("--memory-dir", default="", help="Folder for memories/weights (default: BOT parent dir)")
    parser.add_argument("--min-move", type=float, default=0.10, help="Min predicted move (%% units) to keep pattern. Default 0.10 = 0.10%%")
    parser.add_argument("--recency-half-life", type=int, default=30, help="Recency half-life in days (higher = weight history more)")
    parser.add_argument("--incremental", action="store_true", help="Skip if memory file is fresh enough")
    parser.add_argument("--incremental-max-age", type=int, default=24, help="Hours before incremental training re-runs")
    args = parser.parse_args()
    args.config = _resolve_config_path(args.config)

    # Helper to load enabled coins from config
    def load_coin_config(config_path: str) -> Tuple[List[str], Dict[str, str], Dict[str, object]]:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            coins: List[str] = []
            symbol_map: Dict[str, str] = {}
            # Preferred schema: symbols=[{bot_symbol, enable_long, enable_short, ...}]
            for row in (cfg.get("symbols") or []):
                if not isinstance(row, dict):
                    continue
                coin = str(row.get("bot_symbol", "")).strip().upper()
                if not coin:
                    continue
                mt5_sym = str(row.get("mt5_symbol", "")).strip()
                if mt5_sym:
                    symbol_map[coin] = mt5_sym
                if bool(row.get("enable_long", True)) or bool(row.get("enable_short", True)):
                    coins.append(coin)

            # Backward compatibility: legacy coins={BTC:{...}, ...}
            if not coins:
                for coin, cdata in (cfg.get("coins", {}) or {}).items():
                    if not isinstance(cdata, dict):
                        continue
                    if bool(cdata.get("enable_long", False)) or bool(cdata.get("enable_short", False)):
                        coin_key = str(coin).upper()
                        coins.append(coin_key)
                        mt5_sym = str(cdata.get("mt5_symbol", "")).strip()
                        if mt5_sym:
                            symbol_map[coin_key] = mt5_sym
            return coins, symbol_map, cfg
        except Exception as e:
            log(f"[ERROR] Failed to load config: {e}")
            return [], {}, {}

    # Default symbol map
    default_sym_map = {
        "BTC": "BTCUSD", "ETH": "ETHUSD", "XRP": "XRPUSD",
        "DOGE": "DOGUSD", "BNB": "BNBUSD",
    }


    # --- AUTO ALL: If neither --coin nor --all-coins is specified, default to all enabled coins ---
    config_coins, config_symbol_map, cfg = load_coin_config(args.config)
    coins: List[str] = []
    if args.all_coins:
        coins = config_coins
        if not coins:
            log("[ERROR] No enabled coins found in config.")
            return 1
    elif args.coin:
        coins = [args.coin.upper()]
    else:
        # No coin or all-coins specified: run auto-all
        coins = config_coins
        if not coins:
            log("[ERROR] No enabled coins found in config (auto-all mode).")
            return 1

    # Resolve timeframe list
    if args.timeframes:
        timeframes = args.timeframes
    elif args.timeframe:
        timeframes = [args.timeframe]
    else:
        # If no timeframe specified, try to get all timeframes from config or use defaults
        try:
            # Try to get timeframes from config (if present)
            timeframes = cfg.get("timeframes", ["1hour", "4hour", "1day"])
        except Exception:
            timeframes = ["1hour", "4hour", "1day"]

    normalized_tfs: List[str] = []
    for tf in timeframes:
        try:
            n_tf = normalize_timeframe(tf)
            if n_tf not in normalized_tfs:
                normalized_tfs.append(n_tf)
        except Exception as e:
            log(f"[WARN] Skipping unsupported timeframe '{tf}': {e}")
    if not normalized_tfs:
        log("[ERROR] No valid timeframes to train.")
        return 1

    errors = 0
    run_started_at = int(time.time())
    for coin in coins:
        coin_dir = resolve_coin_dir(os.path.abspath(args.memory_dir or os.path.dirname(os.path.abspath(__file__))), coin, os.path.abspath(os.path.dirname(__file__)))
        write_coin_training_state(coin_dir, coin, "TRAINING", started_at=run_started_at)
        mt5_symbol = args.mt5_symbol or config_symbol_map.get(coin) or default_sym_map.get(coin, f"{coin}USD")
        log(f"[INFO] {coin} -> MT5 symbol: {mt5_symbol}")
        coin_errors = 0
        for tf in normalized_tfs:
            try:
                result = train_mt5(
                    coin=coin,
                    mt5_symbol=mt5_symbol,
                    timeframe=tf,
                    terminal_path=args.terminal_path,
                    lookback_days=args.lookback_days,
                    pattern_length=args.pattern_length,
                    candles_to_predict=args.candles_predict,
                    memory_dir=args.memory_dir,
                    min_move_pct=float(args.min_move),
                    recency_half_life_days=args.recency_half_life,
                    incremental=args.incremental,
                    incremental_max_age_hours=args.incremental_max_age,
                    started_at=run_started_at,
                )
                log(f"[RESULT] {coin} {tf}: {result}")
            except Exception as e:
                log(f"[ERROR] {coin} {tf}: {e}")
                traceback.print_exc()
                errors += 1
                coin_errors += 1
                write_coin_training_state(
                    coin_dir,
                    coin,
                    "FAILED",
                    started_at=run_started_at,
                    extra={"timeframe": tf, "error": str(e)},
                )

        if coin_errors == 0:
            write_coin_training_state(coin_dir, coin, "FINISHED", started_at=run_started_at, finished_at=int(time.time()))

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
