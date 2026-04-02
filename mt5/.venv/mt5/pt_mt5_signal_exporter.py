#!/usr/bin/env python3
"""
Historical Signal Exporter for MT5 Backtesting

Generates per-bar long/short signal CSV files from trained memory patterns
using MT5 historical rates.
"""

import argparse
import csv
import json
import os
import platform
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

try:
    mt5 = __import__("MetaTrader5")
except ImportError:
    os_name = platform.system() or "Unknown"
    if os_name == "Windows":
        print("MetaTrader5 package is not installed in this Python environment.")
        print("Run: pip install -r requirements.txt")
    else:
        print(f"MetaTrader5 package is not available on {os_name}.")
        print("This exporter must run on Windows with MetaTrader 5 installed.")
    sys.exit(1)


def parse_dt(text: str) -> datetime:
    """Parse common datetime formats to UTC datetime."""
    s = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Invalid datetime: {text}")


def rate_value(rate, field: str, default: float = 0.0) -> float:
    """Safely extract field from MT5 rate object."""
    try:
        return float(rate[field])
    except Exception:
        pass
    try:
        return float(getattr(rate, field))
    except Exception:
        return float(default)


def timeframe_from_name(name: str) -> int:
    key = str(name).strip().upper()
    mapping = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
    }
    if key not in mapping:
        raise ValueError(f"Unsupported timeframe: {name}")
    return mapping[key]


def normalize_timeframe_label(name: str) -> str:
    """Normalize timeframe input to canonical memory label."""
    raw = str(name).strip().lower()
    aliases = {
        "m1": "1min",
        "1m": "1min",
        "1min": "1min",
        "m5": "5min",
        "5m": "5min",
        "5min": "5min",
        "m15": "15min",
        "15m": "15min",
        "15min": "15min",
        "m30": "30min",
        "30m": "30min",
        "30min": "30min",
        "h1": "1hour",
        "1h": "1hour",
        "1hour": "1hour",
        "h4": "4hour",
        "4h": "4hour",
        "4hour": "4hour",
        "d1": "1day",
        "1d": "1day",
        "1day": "1day",
        "w1": "1week",
        "1w": "1week",
        "1week": "1week",
    }
    if raw not in aliases:
        raise ValueError(f"Unsupported timeframe: {name}")
    return aliases[raw]


def timeframe_label_to_mt5_key(label: str) -> str:
    mapping = {
        "1min": "M1",
        "5min": "M5",
        "15min": "M15",
        "30min": "M30",
        "1hour": "H1",
        "4hour": "H4",
        "1day": "D1",
        "1week": "W1",
    }
    if label not in mapping:
        raise ValueError(f"Unsupported timeframe label: {label}")
    return mapping[label]


def load_memory_file(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            return [p.strip() for p in content.split("~") if p.strip()]
    except Exception as e:
        print(f"[WARN] Failed to load memory file {path}: {e}")
        return []


def load_weights_file(path: str) -> List[float]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            out = []
            for token in content.replace("[", "").replace("]", "").replace(",", "").split():
                try:
                    out.append(float(token))
                except ValueError:
                    pass
            return out
    except Exception as e:
        print(f"[WARN] Failed to load weights file {path}: {e}")
        return []


def load_threshold(path: str, default: float = 10.0) -> float:
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return float(f.read().strip() or default)
    except Exception:
        return default


def load_mt5_config(path: str) -> dict:
    """Load optional MT5 config JSON used for terminal init credentials/path."""
    if not path:
        return {}
    cfg_path = os.path.abspath(path)
    if not os.path.isfile(cfg_path):
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def initialize_mt5_with_config(config: dict) -> None:
    """Initialize MT5 with best-effort config params, fallback to default init."""
    kwargs = {}

    terminal_path = str(config.get("terminal_path", "") or "").strip()
    if terminal_path:
        kwargs["path"] = terminal_path

    login = config.get("login")
    if login is not None and str(login).strip() != "":
        try:
            kwargs["login"] = int(login)
        except Exception:
            pass

    password = str(config.get("password", "") or "").strip()
    if password:
        kwargs["password"] = password

    server = str(config.get("server", "") or "").strip()
    if server:
        kwargs["server"] = server

    ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
    if not ok:
        err = mt5.last_error()
        raise RuntimeError(f"MT5 initialize failed: {err}")


def safe_file_token(value: str, default: str = "COIN") -> str:
    """Return a filesystem-safe token for filenames."""
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return token or default


def candles_to_price_changes(candles: List) -> List[float]:
    """Convert candle window to percent-change pattern used by trainer memory."""
    closes = []
    for c in candles:
        close_val = rate_value(c, "close", 0.0)
        if close_val > 0:
            closes.append(close_val)

    if len(closes) < 2:
        return []

    changes = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        cur = closes[i]
        if prev > 0:
            changes.append(((cur - prev) / prev) * 100.0)
    return changes


def match_patterns(
    current_changes: List[float],
    memory_patterns: List[str],
    weights: List[float],
    threshold: float,
    top_k: int = 25,
    max_match_diff: float = 2.0,
) -> Tuple[float, float]:
    """Match current pattern against memory patterns and return weighted predictions."""
    if not current_changes or not memory_patterns or not weights:
        return 0.0, 0.0

    candidates = []
    effective_threshold = min(float(threshold), float(max_match_diff))

    for idx, mem_pattern in enumerate(memory_patterns):
        try:
            parts = mem_pattern.split("{}")
            if len(parts) < 3:
                continue

            pattern_vals = []
            for p in parts[0].strip().split():
                try:
                    pattern_vals.append(float(p))
                except ValueError:
                    pass

            if len(pattern_vals) != len(current_changes):
                continue

            diffs = [abs(current_changes[i] - pattern_vals[i]) for i in range(len(pattern_vals))]
            if not diffs:
                continue

            avg_diff = sum(diffs) / len(diffs)
            if avg_diff > effective_threshold:
                continue

            base_w = weights[idx] if idx < len(weights) else 1.0
            high_val = float(parts[1].strip())
            low_val = float(parts[2].strip())
            candidates.append((avg_diff, base_w, high_val, low_val))
        except Exception:
            continue

    if not candidates:
        return 0.0, 0.0

    # Keep the nearest matches to prevent global averaging that flattens signals.
    candidates.sort(key=lambda x: x[0])
    selected = candidates[: max(1, int(top_k))]

    matched_weights = []
    high_preds = []
    low_preds = []
    for avg_diff, base_w, high_val, low_val in selected:
        # Distance-aware weighting favors closer patterns while still using memory confidence.
        dist_w = 1.0 / (1.0 + avg_diff)
        w = max(0.0, float(base_w)) * dist_w
        if w <= 0:
            continue
        matched_weights.append(w)
        high_preds.append(high_val * w)
        low_preds.append(low_val * w)

    if not matched_weights:
        return 0.0, 0.0

    total_w = sum(matched_weights)
    if total_w <= 0:
        return 0.0, 0.0

    high_pred = sum(high_preds) / total_w if high_preds else 0.0
    low_pred = sum(low_preds) / total_w if low_preds else 0.0
    return high_pred, low_pred


def generate_signal(high_pred: float, low_pred: float) -> int:
    """Map risk-adjusted prediction edge to signal strength 0..7."""
    if high_pred <= 0:
        return 0

    downside_risk = abs(low_pred) if low_pred < 0 else 0.0
    edge = high_pred - downside_risk
    if edge <= 0:
        return 0

    if edge >= 1.20:
        return 7
    if edge >= 0.80:
        return 6
    if edge >= 0.55:
        return 5
    if edge >= 0.35:
        return 4
    if edge >= 0.20:
        return 3
    if edge >= 0.10:
        return 2
    return 1


def export_historical_signals(
    coin: str,
    mt5_symbol: str,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
    memory_dir: str,
    output_dir: str,
    pattern_length: int = 12,
    top_k: int = 25,
    max_match_diff: float = 2.0,
    mt5_config: dict = None,
) -> str:
    output_dir = os.path.normpath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    timeframe_label = normalize_timeframe_label(timeframe)

    memory_path = os.path.join(memory_dir, f"memories_{timeframe_label}.txt")
    weights_path = os.path.join(memory_dir, f"memory_weights_{timeframe_label}.txt")
    threshold_path = os.path.join(memory_dir, f"neural_perfect_threshold_{timeframe_label}.txt")

    memory_patterns = load_memory_file(memory_path)
    weights = load_weights_file(weights_path)
    threshold = load_threshold(threshold_path)

    print(f"[EXPORT] Loaded {len(memory_patterns)} patterns, {len(weights)} weights, threshold={threshold:.2f}")

    initialize_mt5_with_config(mt5_config or {})

    tf_const = timeframe_from_name(timeframe_label_to_mt5_key(timeframe_label))

    print(f"[EXPORT] Fetching {mt5_symbol} {timeframe_label} from MT5...")
    candles = mt5.copy_rates_range(mt5_symbol, tf_const, start_dt, end_dt)
    if candles is None or len(candles) == 0:
        raise RuntimeError(f"No candles fetched for {mt5_symbol}")

    print(f"[EXPORT] Got {len(candles)} candles")

    signals = []
    for i, candle in enumerate(candles):
        ts = int(rate_value(candle, "time", 0.0))
        if ts <= 0:
            continue

        start_idx = max(0, i - pattern_length)
        window = candles[start_idx : i + 1]
        current_changes = candles_to_price_changes(window)
        if len(current_changes) < pattern_length:
            pad = current_changes[0] if current_changes else 0.0
            current_changes = [pad] * (pattern_length - len(current_changes)) + current_changes

        high_pred, low_pred = match_patterns(
            current_changes,
            memory_patterns,
            weights,
            threshold,
            top_k=top_k,
            max_match_diff=max_match_diff,
        )
        long_sig = generate_signal(high_pred, low_pred)
        short_sig = 0

        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        signals.append(
            {
                "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "long_sig": long_sig,
                "short_sig": short_sig,
            }
        )

    coin_file = safe_file_token(coin.upper())
    output_path = os.path.normpath(os.path.join(output_dir, f"{coin_file}_signals.csv"))
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["time", "long_sig", "short_sig"])
        writer.writeheader()
        writer.writerows(signals)

    print(f"[EXPORT] Wrote {len(signals)} signals to {output_path}")

    try:
        mt5.shutdown()
    except Exception:
        pass

    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export historical neural signals for MT5 backtesting")
    parser.add_argument("--coin", required=True, help="Coin symbol (BTC, ETH, etc.)")
    parser.add_argument("--mt5-symbol", required=True, help="MT5 symbol (BTCUSD, ETHUSD, etc.)")
    parser.add_argument("--start", default=None, help="Start date (YYYY-MM-DD, default: 60 days ago)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD, default: now)")
    parser.add_argument("--timeframe", default="1hour", help="Timeframe (5min, 15min, 1hour, 4hour, 1day, etc.)")
    parser.add_argument("--memory-dir", default=".", help="Directory containing memory/weights files")
    parser.add_argument("--output-dir", default="mt5/signal_history", help="Output directory for signal CSVs")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "mt5_config.json"),
        help="Path to MT5 JSON config (terminal_path/login/password/server)",
    )
    parser.add_argument("--pattern-length", type=int, default=12, help="Number of changes in pattern")
    parser.add_argument("--lookback-days", type=int, default=60, help="Days to look back if --start not provided")
    parser.add_argument("--top-k", type=int, default=25, help="Use top-K nearest memory matches (default: 25)")
    parser.add_argument(
        "--max-match-diff",
        type=float,
        default=2.0,
        help="Maximum average pattern diff (pct points) allowed for a match (default: 2.0)",
    )

    args = parser.parse_args()

    try:
        mt5_config = load_mt5_config(args.config)
        end_dt = parse_dt(args.end) if args.end else datetime.now(timezone.utc)
        start_dt = parse_dt(args.start) if args.start else (end_dt - timedelta(days=args.lookback_days))

        export_historical_signals(
            coin=args.coin,
            mt5_symbol=args.mt5_symbol,
            timeframe=args.timeframe,
            start_dt=start_dt,
            end_dt=end_dt,
            memory_dir=args.memory_dir,
            output_dir=args.output_dir,
            pattern_length=args.pattern_length,
            top_k=args.top_k,
            max_match_diff=args.max_match_diff,
            mt5_config=mt5_config,
        )
        return 0
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
