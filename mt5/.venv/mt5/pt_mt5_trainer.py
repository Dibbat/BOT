#!/usr/bin/env python3
"""
MT5-Native Neural Trainer for PowerTrader

Trains pattern recognition on MT5 historical candles without KuCoin dependency.
Generates memory files for signal exporter and live trading.

Usage:
  python mt5/pt_mt5_trainer.py --coin BTC --timeframe 1hour --terminal-path "C:\\path\\to\\terminal64.exe"
"""

import argparse
import json
import os
import sys
import platform
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple

try:
    mt5 = __import__("MetaTrader5")
except ImportError:
    os_name = platform.system() or "Unknown"
    if os_name == "Windows":
        print("MetaTrader5 package is not installed in this Python environment.")
        print("Run: pip install -r requirements.txt")
    else:
        print(f"MetaTrader5 package is not available on {os_name}.")
        print("This trainer must run on Windows with MetaTrader 5 installed.")
    sys.exit(1)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    print(f"[{now()}] {msg}")


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
    """Convert timeframe name to MT5 constant."""
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
    """Normalize user timeframe input into canonical memory filename label."""
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
    """Map canonical timeframe label to MT5 key used by timeframe_from_name."""
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
    """Load memory patterns."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            patterns = content.split("~")
            return [p.strip() for p in patterns if p.strip()]
    except Exception as e:
        log(f"[WARN] Failed to load memory: {e}")
        return []


def load_weights_file(path: str) -> List[float]:
    """Load weights."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            weights = []
            for token in content.replace("[", "").replace("]", "").replace(",", "").split():
                try:
                    weights.append(float(token))
                except ValueError:
                    pass
            return weights
    except Exception:
        return []


def save_memory_file(path: str, patterns: List[str]) -> None:
    """Save memory patterns."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("~".join(patterns))
    log(f"[SAVE] Memory: {path}")


def save_weights_file(path: str, weights: List[float]) -> None:
    """Save weights."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(" ".join(str(w) for w in weights))
    log(f"[SAVE] Weights: {path}")


def train_mt5(
    coin: str,
    mt5_symbol: str,
    timeframe: str,
    terminal_path: str,
    lookback_days: int = 60,
    pattern_length: int = 12,
    candles_to_predict: int = 2,
) -> None:
    """
    Train neural patterns on MT5 historical candles.
    Replicates pt_trainer.py logic without KuCoin dependency.
    """
    timeframe_label = normalize_timeframe_label(timeframe)
    log(f"[TRAIN] Starting {coin} {timeframe_label} training on MT5...")
    
    # Initialize MT5
    try:
        if terminal_path:
            ok = mt5.initialize(path=terminal_path)
        else:
            ok = mt5.initialize()
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    except Exception as e:
        log(f"[WARN] MT5 init error (may be okay if already running): {e}")
    
    # Fetch historical candles
    tf_const = timeframe_from_name(timeframe_label_to_mt5_key(timeframe_label))
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    
    log(f"[TRAIN] Fetching {mt5_symbol} {timeframe_label} from MT5 ({lookback_days} days)...")
    candles = mt5.copy_rates_range(mt5_symbol, tf_const, start_dt, end_dt)
    
    if candles is None or len(candles) == 0:
        log(f"[ERROR] No candles fetched for {mt5_symbol}")
        return
    
    log(f"[TRAIN] Got {len(candles)} candles, starting pattern extraction...")
    
    # Extract price changes (% differences)
    closes = [rate_value(c, "close", 0.0) for c in candles]
    opens = [rate_value(c, "open", 0.0) for c in candles]
    highs = [rate_value(c, "high", 0.0) for c in candles]
    lows = [rate_value(c, "low", 0.0) for c in candles]
    
    price_changes = []
    high_changes = []
    low_changes = []
    
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            change = ((closes[i] - closes[i-1]) / closes[i-1]) * 100.0
            price_changes.append(change)
            
            high_change = ((highs[i] - opens[i-1]) / opens[i-1]) * 100.0 if opens[i-1] > 0 else 0.0
            high_changes.append(high_change)
            
            low_change = ((lows[i] - opens[i-1]) / opens[i-1]) * 100.0 if opens[i-1] > 0 else 0.0
            low_changes.append(low_change)
    
    log(f"[TRAIN] Extracted {len(price_changes)} price changes")
    
    # Load existing memory and weights
    mem_path = os.path.join(os.path.dirname(__file__), "..", f"memories_{timeframe_label}.txt")
    weights_path = os.path.join(os.path.dirname(__file__), "..", f"memory_weights_{timeframe_label}.txt")
    
    memory_patterns = load_memory_file(mem_path)
    weights = load_weights_file(weights_path)
    
    log(f"[TRAIN] Loaded {len(memory_patterns)} existing patterns, {len(weights)} weights")
    
    # Extract new patterns from candles
    for i in range(pattern_length, len(price_changes) - candles_to_predict):
        # Current pattern (last N price changes)
        pattern = price_changes[i-pattern_length:i]
        pattern_str = " ".join(f"{p:.4f}" for p in pattern)
        
        # Future high and low predictions
        future_highs = high_changes[i:i+candles_to_predict]
        future_lows = low_changes[i:i+candles_to_predict]
        
        avg_high = sum(future_highs) / len(future_highs) if future_highs else 0.0
        avg_low = sum(future_lows) / len(future_lows) if future_lows else 0.0
        
        # Create pattern entry
        pattern_entry = f"{pattern_str}{{}}{avg_high:.4f}{{}}{avg_low:.4f}"
        
        # Check if pattern already exists
        exists = any(pattern_entry.split("{}")[0] == p.split("{}")[0] for p in memory_patterns)
        
        if not exists:
            memory_patterns.append(pattern_entry)
            weights.append(1.0)
    
    log(f"[TRAIN] Extracted {len(memory_patterns)} total patterns")
    
    # Save results
    save_memory_file(mem_path, memory_patterns)
    save_weights_file(weights_path, weights)
    
    # Save training status
    status_path = os.path.join(os.path.dirname(__file__), "..", "trainer_status.json")
    status = {
        "coin": coin,
        "state": "FINISHED",
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "patterns": len(memory_patterns),
        "lookback_days": lookback_days,
        "timeframe": timeframe_label,
    }
    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status, f)
    
    log(f"[TRAIN] Training complete! {len(memory_patterns)} patterns saved.")
    log(f"[TRAIN] Status written to {status_path}")
    
    try:
        mt5.shutdown()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="MT5-Native Neural Trainer for PowerTrader")
    parser.add_argument("--coin", required=True, help="Coin symbol (BTC, ETH, etc.)")
    parser.add_argument("--mt5-symbol", default=None, help="MT5 symbol (auto-resolved if not provided)")
    parser.add_argument("--timeframe", default="1hour", help="Timeframe (5min, 15min, 1hour, 4hour, 1day, etc.)")
    parser.add_argument("--terminal-path", default="", help="Path to MT5 terminal64.exe")
    parser.add_argument("--lookback-days", type=int, default=60, help="Historical data lookback (days)")
    parser.add_argument("--pattern-length", type=int, default=12, help="Pattern length (candles)")
    
    args = parser.parse_args()
    
    # Auto-resolve MT5 symbol if not provided
    mt5_symbol = args.mt5_symbol
    if not mt5_symbol:
        symbol_map = {
            "BTC": "BTCUSD",
            "ETH": "ETHUSD",
            "XRP": "XRPUSD",
            "DOGE": "DOGUSD",
            "BNB": "BNBUSD",
        }
        mt5_symbol = symbol_map.get(args.coin.upper(), f"{args.coin}USD")
        log(f"[INFO] Auto-resolved {args.coin} -> {mt5_symbol}")
    
    try:
        train_mt5(
            coin=args.coin,
            mt5_symbol=mt5_symbol,
            timeframe=args.timeframe,
            terminal_path=args.terminal_path,
            lookback_days=args.lookback_days,
            pattern_length=args.pattern_length,
        )
        return 0
    except Exception as e:
        log(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
