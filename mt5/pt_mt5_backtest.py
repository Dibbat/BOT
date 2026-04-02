#!/usr/bin/env python3
"""
PowerTrader MT5 Backtester  --  v1.0

Self-contained backtest engine.  Does NOT need a live MT5 connection --
it replays the pattern-memory signal model against MT5 historical candles
(or a CSV candle file if MT5 is unavailable).

Signal model:
  - Loads memories_{tf}.txt + memory_weights_{tf}.txt from the coin memory dir
  - Scans every candle window: counts matching patterns (long vs short)
  - Long signal >= open_threshold  => open LONG
  - Short signal >= open_threshold => open SHORT
  - TP / SL / trailing SL applied per candle close
  - Partial TP optionally fires at partial_tp_pct

Output JSON schema (written to --output-json):
{
  "summary": {
    "coin", "timeframe", "lookback_days",
    "total_trades", "win_trades", "loss_trades",
    "win_rate_pct", "total_pnl_pct", "total_pnl_usd",
    "max_drawdown_pct", "profit_factor", "sharpe_ratio",
    "avg_win_pct", "avg_loss_pct",
    "initial_balance", "final_balance",
    "start_date", "end_date"
  },
  "equity_curve": [{"ts": int, "equity": float}, ...],
  "trades": [
    {
      "entry_ts", "exit_ts",
      "side",            -- "long" | "short"
      "entry",           -- entry price
      "exit",            -- exit price
      "pnl_pct",
      "pnl_usd",
      "reason"           -- "tp" | "sl" | "trailing_sl" | "partial_tp" | "signal_close" | "end_of_data"
    }, ...
  ]
}

Usage:
  python mt5/pt_mt5_backtest.py --coin BTC --timeframe 1hour --lookback-days 90 \\
      --output-json logs/bt_BTC_1hour_xxx.json
"""

import argparse
import json
import math
import os
import platform
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    line = f"[{now_str()}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        safe = line.encode(enc, errors="replace").decode(enc, errors="replace")
        print(safe, flush=True)


# ---------------------------------------------------------------------------
# Timeframe helpers
# ---------------------------------------------------------------------------

_TF_ALIASES = {
    "m1": "1min",  "1m": "1min",  "1min": "1min",
    "m5": "5min",  "5m": "5min",  "5min": "5min",
    "m15":"15min", "15m":"15min", "15min":"15min",
    "m30":"30min", "30m":"30min", "30min":"30min",
    "h1":"1hour",  "1h":"1hour",  "1hour":"1hour",
    "h4":"4hour",  "4h":"4hour",  "4hour":"4hour",
    "d1":"1day",   "1d":"1day",   "1day":"1day",
    "w1":"1week",  "1w":"1week",  "1week":"1week",
}

_LABEL_TO_MT5_KEY = {
    "1min":"M1","5min":"M5","15min":"M15","30min":"M30",
    "1hour":"H1","4hour":"H4","1day":"D1","1week":"W1",
}


def normalize_tf(name: str) -> str:
    raw = str(name).strip().lower()
    if raw not in _TF_ALIASES:
        raise ValueError(f"Unsupported timeframe: {name}")
    return _TF_ALIASES[raw]


# ---------------------------------------------------------------------------
# Memory file helpers
# ---------------------------------------------------------------------------

def load_memory(mem_path: str) -> List[str]:
    if not os.path.isfile(mem_path):
        return []
    try:
        with open(mem_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return [p.strip() for p in content.split("~") if p.strip()]
    except Exception:
        return []


def load_weights(weights_path: str, n: int) -> List[float]:
    if not os.path.isfile(weights_path):
        return [1.0] * n
    try:
        with open(weights_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        ws = []
        for tok in content.replace("[","").replace("]","").replace(",","").split():
            try:
                ws.append(float(tok))
            except ValueError:
                pass
        if len(ws) < n:
            ws.extend([1.0] * (n - len(ws)))
        return ws[:n]
    except Exception:
        return [1.0] * n


def _parse_pattern(entry: str) -> Tuple[List[float], float, float]:
    """
    Parse a memory entry: "v1 v2 ... vN{}avg_high{}avg_low"
    Returns (feature_list, avg_high, avg_low).
    """
    try:
        parts = entry.split("{}")
        features = [float(x) for x in parts[0].strip().split()]
        avg_high = float(parts[1]) if len(parts) > 1 else 0.0
        avg_low  = float(parts[2]) if len(parts) > 2 else 0.0
        return features, avg_high, avg_low
    except Exception:
        return [], 0.0, 0.0


def _pattern_similarity(a: List[float], b: List[float]) -> float:
    """
    Cosine similarity between two equal-length float vectors.
    Returns 0..1.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    if na < 1e-10 or nb < 1e-10:
        return 0.0
    return max(0.0, dot / (na * nb))


def score_window(
    window: List[float],
    patterns: List[Tuple[List[float], float, float]],
    weights: List[float],
    sim_threshold: float = 0.92,
) -> Tuple[int, int]:
    """
    Compare `window` against all stored patterns.
    Returns (long_score, short_score) = count of matching patterns
    weighted by their weight, that predict a positive or negative move.
    """
    long_score  = 0.0
    short_score = 0.0

    for (feats, avg_high, avg_low), w in zip(patterns, weights):
        if len(feats) != len(window):
            continue
        sim = _pattern_similarity(window, feats)
        if sim < sim_threshold:
            continue
        if avg_high > 0:
            long_score  += w * sim
        if avg_low < 0:
            short_score += w * sim

    # Normalize to integer-like signal levels (0..8)
    ls = min(8, int(long_score))
    ss = min(8, int(short_score))
    return ls, ss


# ---------------------------------------------------------------------------
# MT5 candle fetch (optional)
# ---------------------------------------------------------------------------

def fetch_candles_mt5(
    mt5_symbol: str,
    tf_label: str,
    lookback_days: int,
    config_path: str = "",
) -> List[Dict]:
    try:
        mt5 = __import__("MetaTrader5")
    except ImportError:
        log("[WARN] MetaTrader5 not available -- no candle data")
        return []

    cfg: Dict = {}
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass

    terminal = str(cfg.get("terminal_path", "") or "").strip()
    try:
        ok = mt5.initialize(path=terminal) if terminal else mt5.initialize()
        if not ok:
            log(f"[WARN] MT5 init failed: {mt5.last_error()}")
            return []
    except Exception as e:
        log(f"[WARN] MT5 init exception: {e}")
        return []

    login = cfg.get("login")
    pw    = cfg.get("password")
    srv   = cfg.get("server")
    if login and pw and srv:
        try:
            mt5.login(int(login), password=str(pw), server=str(srv))
        except Exception:
            pass

    tf_key  = _LABEL_TO_MT5_KEY.get(tf_label, "H1")
    tf_attr = f"TIMEFRAME_{tf_key}"
    try:
        tf_const = getattr(mt5, tf_attr, mt5.TIMEFRAME_H1)
    except Exception:
        tf_const = mt5.TIMEFRAME_H1

    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    log(f"[BT] Fetching {mt5_symbol} {tf_label} from MT5 ({lookback_days}d)...")
    try:
        mt5.symbol_select(mt5_symbol, True)
    except Exception:
        pass

    try:
        rates = mt5.copy_rates_range(mt5_symbol, tf_const, start_dt, end_dt)
    except Exception as e:
        log(f"[WARN] copy_rates_range failed: {e}")
        rates = None

    if rates is None or len(rates) == 0:
        log(f"[WARN] No candles returned for {mt5_symbol}")
        try:
            mt5.shutdown()
        except Exception:
            pass
        return []

    candles = []
    for r in rates:
        try:
            candles.append({
                "ts":    int(r["time"]),
                "open":  float(r["open"]),
                "high":  float(r["high"]),
                "low":   float(r["low"]),
                "close": float(r["close"]),
                "vol":   float(r.get("tick_volume", 0)),
            })
        except Exception:
            continue

    log(f"[BT] Fetched {len(candles)} candles")
    try:
        mt5.shutdown()
    except Exception:
        pass
    return candles


def fetch_candles_csv(csv_path: str) -> List[Dict]:
    """
    Fallback: read candles from a CSV.
    Expected columns: timestamp,open,high,low,close  (or ts,open,high,low,close)
    """
    import csv as csv_mod
    candles = []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                try:
                    ts_raw = row.get("timestamp") or row.get("ts") or row.get("time") or "0"
                    ts = int(float(ts_raw))
                    candles.append({
                        "ts":    ts,
                        "open":  float(row.get("open", 0)),
                        "high":  float(row.get("high", 0)),
                        "low":   float(row.get("low", 0)),
                        "close": float(row.get("close", 0)),
                        "vol":   float(row.get("volume", row.get("vol", 0))),
                    })
                except Exception:
                    continue
    except Exception as e:
        log(f"[WARN] Could not read CSV {csv_path}: {e}")
    return candles


# ---------------------------------------------------------------------------
# Core backtester
# ---------------------------------------------------------------------------

def run_backtest(
    coin: str,
    tf_label: str,
    candles: List[Dict],
    memory_dir: str,
    open_threshold: int = 3,
    close_threshold: int = 2,
    sl_pct: float = 2.0,
    tp_pct: float = 3.0,
    partial_tp_pct: float = 1.8,
    partial_tp_fraction: float = 0.5,
    trailing_sl_trigger_pct: float = 1.5,
    trailing_sl_distance_pct: float = 0.8,
    breakeven_trigger_pct: float = 1.0,
    initial_balance: float = 10000.0,
    lot_fraction: float = 0.10,         # fraction of balance per trade
    pattern_length: int = 12,
    sim_threshold: float = 0.92,
    enable_long: bool = True,
    enable_short: bool = True,
) -> Dict:
    """
    Replay the neural pattern model over historical candles and record every trade.
    Returns the full result dict ready for JSON serialization.
    """

    # Load memory
    mem_path     = os.path.join(memory_dir, f"memories_{tf_label}.txt")
    weights_path = os.path.join(memory_dir, f"memory_weights_{tf_label}.txt")
    raw_patterns = load_memory(mem_path)

    if not raw_patterns:
        log(f"[WARN] No memory file found at {mem_path}. Signals will always be 0.")

    parsed = [_parse_pattern(p) for p in raw_patterns]
    weights = load_weights(weights_path, len(parsed))

    log(f"[BT] Loaded {len(parsed)} patterns from {mem_path}")
    log(f"[BT] Candles: {len(candles)}  |  Pattern length: {pattern_length}")
    log(f"[BT] Params: open>={open_threshold} SL={sl_pct}% TP={tp_pct}% "
        f"TrailTrig={trailing_sl_trigger_pct}% TrailDist={trailing_sl_distance_pct}%")

    # Pre-compute % change series for scoring
    closes = [float(c["close"]) for c in candles]
    n = len(closes)

    if n < pattern_length + 2:
        log("[ERROR] Not enough candles for backtesting")
        return _empty_result(coin, tf_label)

    pct_changes = [0.0]
    for i in range(1, n):
        if closes[i - 1] > 0:
            pct_changes.append((closes[i] - closes[i - 1]) / closes[i - 1] * 100.0)
        else:
            pct_changes.append(0.0)

    # ATR for normalization (same as trainer)
    from statistics import mean
    atr_values = _compute_atr_bt(candles)

    # State
    balance    = float(initial_balance)
    equity     = balance
    in_trade   = False
    side       = ""          # "long" | "short"
    entry_price = 0.0
    entry_ts   = 0
    position_size = 0.0      # USD notional
    sl_price   = 0.0
    tp_price   = 0.0
    trail_active = False
    peak_pnl_pct = 0.0
    be_done    = False
    partial_done = False
    partial_fraction = partial_tp_fraction

    trades: List[Dict] = []
    equity_curve: List[Dict] = []
    peak_equity = balance
    max_drawdown = 0.0

    def _close_trade(exit_price: float, exit_ts: int, reason: str, fraction: float = 1.0) -> float:
        nonlocal balance, in_trade, side, entry_price, sl_price, tp_price
        nonlocal trail_active, peak_pnl_pct, be_done, partial_done, position_size

        ep = float(exit_price)
        if side == "long":
            pnl_pct = (ep - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
        else:
            pnl_pct = (entry_price - ep) / entry_price * 100.0 if entry_price > 0 else 0.0

        pnl_usd = position_size * fraction * pnl_pct / 100.0
        balance += pnl_usd

        trades.append({
            "entry_ts":  entry_ts,
            "exit_ts":   exit_ts,
            "side":      side,
            "entry":     round(entry_price, 6),
            "exit":      round(ep, 6),
            "pnl_pct":   round(pnl_pct, 4),
            "pnl_usd":   round(pnl_usd, 4),
            "reason":    reason,
        })

        if fraction >= 1.0:
            in_trade     = False
            side         = ""
            entry_price  = 0.0
            sl_price     = 0.0
            tp_price     = 0.0
            trail_active = False
            peak_pnl_pct = 0.0
            be_done      = False
            partial_done = False
            position_size = 0.0

        return pnl_usd

    for i in range(pattern_length, n):
        c       = candles[i]
        close   = float(c["close"])
        high    = float(c["high"])
        low     = float(c["low"])
        ts      = int(c["ts"])

        # ---- Signal scoring ----
        window = pct_changes[i - pattern_length:i]
        bar_atr = atr_values[i] if i < len(atr_values) and atr_values[i] > 0 else 1.0
        # Normalize by ATR (same as trainer)
        window_norm = [round(v / bar_atr, 4) for v in window] if bar_atr > 0 else window

        long_sig, short_sig = score_window(window_norm, parsed, weights, sim_threshold)

        # ---- Manage open position ----
        if in_trade:
            cur_price = close

            # P&L %
            if side == "long":
                pnl_pct = (cur_price - entry_price) / entry_price * 100.0
            else:
                pnl_pct = (entry_price - cur_price) / entry_price * 100.0

            # Break-even
            if not be_done and pnl_pct >= breakeven_trigger_pct:
                if side == "long":
                    sl_price = max(sl_price, entry_price * 1.0001)
                else:
                    sl_price = min(sl_price, entry_price * 0.9999) if sl_price > 0 else entry_price * 0.9999
                be_done = True

            # Partial TP
            if not partial_done and pnl_pct >= partial_tp_pct and partial_tp_fraction > 0:
                _close_trade(cur_price, ts, "partial_tp", fraction=partial_fraction)
                partial_done = True
                # Position size reduced
                position_size *= (1.0 - partial_fraction)

            # Trailing SL
            if pnl_pct >= trailing_sl_trigger_pct:
                trail_active = True
            if trail_active:
                if pnl_pct > peak_pnl_pct:
                    peak_pnl_pct = pnl_pct
                if side == "long":
                    trail_sl = cur_price * (1.0 - trailing_sl_distance_pct / 100.0)
                    if trail_sl > sl_price:
                        sl_price = trail_sl
                else:
                    trail_sl = cur_price * (1.0 + trailing_sl_distance_pct / 100.0)
                    if sl_price <= 0 or trail_sl < sl_price:
                        sl_price = trail_sl

            # Check SL (use low for longs, high for shorts -- intrabar simulation)
            sl_hit = False
            tp_hit = False
            if side == "long":
                if sl_price > 0 and low <= sl_price:
                    sl_hit = True
                if tp_price > 0 and high >= tp_price:
                    tp_hit = True
            else:
                if sl_price > 0 and high >= sl_price:
                    sl_hit = True
                if tp_price > 0 and low <= tp_price:
                    tp_hit = True

            # TP fires first (optimistic assumption)
            if tp_hit:
                _close_trade(tp_price, ts, "tp")
            elif sl_hit:
                _close_trade(sl_price, ts, "sl")
            elif not in_trade:
                pass  # already closed above
            else:
                # Signal close on opposite
                if side == "long" and short_sig >= open_threshold:
                    _close_trade(close, ts, "signal_close")
                elif side == "short" and long_sig >= open_threshold:
                    _close_trade(close, ts, "signal_close")
                # Fade close
                elif side == "long" and long_sig < close_threshold:
                    _close_trade(close, ts, "signal_fade")
                elif side == "short" and short_sig < close_threshold:
                    _close_trade(close, ts, "signal_fade")

        # ---- Open new position ----
        if not in_trade and balance > 0:
            if enable_long and long_sig >= open_threshold and short_sig < open_threshold:
                in_trade      = True
                side          = "long"
                entry_price   = close
                entry_ts      = ts
                position_size = balance * lot_fraction
                sl_price      = entry_price * (1.0 - sl_pct / 100.0)
                tp_price      = entry_price * (1.0 + tp_pct / 100.0)
                trail_active  = False
                peak_pnl_pct  = 0.0
                be_done       = False
                partial_done  = False

            elif enable_short and short_sig >= open_threshold and long_sig < open_threshold:
                in_trade      = True
                side          = "short"
                entry_price   = close
                entry_ts      = ts
                position_size = balance * lot_fraction
                sl_price      = entry_price * (1.0 + sl_pct / 100.0)
                tp_price      = entry_price * (1.0 - tp_pct / 100.0)
                trail_active  = False
                peak_pnl_pct  = 0.0
                be_done       = False
                partial_done  = False

        # ---- Equity curve ----
        if in_trade:
            cur = float(close)
            if side == "long":
                unrealized_pct = (cur - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            else:
                unrealized_pct = (entry_price - cur) / entry_price * 100.0 if entry_price > 0 else 0.0
            equity = balance + position_size * unrealized_pct / 100.0
        else:
            equity = balance

        equity_curve.append({"ts": ts, "equity": round(equity, 4)})

        # Max drawdown tracking
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

    # Close any open position at end of data
    if in_trade and candles:
        last = candles[-1]
        _close_trade(float(last["close"]), int(last["ts"]), "end_of_data")

    # --- Summary ---
    win_trades  = [t for t in trades if t["pnl_usd"] > 0]
    loss_trades = [t for t in trades if t["pnl_usd"] <= 0]
    total       = len(trades)
    wins        = len(win_trades)

    win_rate  = (wins / total * 100.0) if total > 0 else 0.0
    gross_win = sum(t["pnl_usd"] for t in win_trades)
    gross_loss= abs(sum(t["pnl_usd"] for t in loss_trades))
    pf        = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    total_pnl_usd = balance - initial_balance
    total_pnl_pct = total_pnl_usd / initial_balance * 100.0 if initial_balance > 0 else 0.0

    avg_win  = (gross_win  / wins              ) if wins  > 0 else 0.0
    avg_loss = (gross_loss / len(loss_trades)  ) if loss_trades else 0.0

    # Sharpe (simplified: mean / std of per-trade pnl_pct)
    pnl_pcts = [t["pnl_pct"] for t in trades]
    sharpe = 0.0
    if len(pnl_pcts) > 1:
        mu = sum(pnl_pcts) / len(pnl_pcts)
        var = sum((x - mu) ** 2 for x in pnl_pcts) / len(pnl_pcts)
        std = math.sqrt(var)
        sharpe = mu / std if std > 0 else 0.0

    start_date = datetime.utcfromtimestamp(candles[0]["ts"]).strftime("%Y-%m-%d")  if candles else "-"
    end_date   = datetime.utcfromtimestamp(candles[-1]["ts"]).strftime("%Y-%m-%d") if candles else "-"

    summary = {
        "coin":             coin,
        "timeframe":        tf_label,
        "start_date":       start_date,
        "end_date":         end_date,
        "total_trades":     total,
        "win_trades":       wins,
        "loss_trades":      len(loss_trades),
        "win_rate_pct":     round(win_rate, 2),
        "total_pnl_pct":    round(total_pnl_pct, 4),
        "total_pnl_usd":    round(total_pnl_usd, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "profit_factor":    round(pf, 4),
        "sharpe_ratio":     round(sharpe, 4),
        "avg_win_usd":      round(avg_win, 4),
        "avg_loss_usd":     round(avg_loss, 4),
        "initial_balance":  round(initial_balance, 2),
        "final_balance":    round(balance, 4),
    }

    log(f"[BT] DONE  trades={total} WR={win_rate:.1f}% "
        f"PnL={total_pnl_pct:.2f}% DD={max_drawdown:.2f}% PF={pf:.3f}")

    return {"summary": summary, "equity_curve": equity_curve, "trades": trades}


def _compute_atr_bt(candles: List[Dict], period: int = 14) -> List[float]:
    n = len(candles)
    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]
    closes = [float(c["close"]) for c in candles]
    trs = [0.0]
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        trs.append(tr)
    atr = [0.0] * n
    if n > period:
        atr[period] = sum(trs[1:period + 1]) / period
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
    return atr


def _empty_result(coin: str, tf: str) -> Dict:
    return {
        "summary": {
            "coin": coin, "timeframe": tf,
            "total_trades": 0, "win_rate_pct": 0.0,
            "total_pnl_pct": 0.0, "total_pnl_usd": 0.0,
            "max_drawdown_pct": 0.0, "profit_factor": 0.0,
            "initial_balance": 10000.0, "final_balance": 10000.0,
        },
        "equity_curve": [],
        "trades": [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="PowerTrader MT5 Backtester")
    parser.add_argument("--coin",           required=True)
    parser.add_argument("--mt5-symbol",     default=None)
    parser.add_argument("--timeframe",      default="1hour")
    parser.add_argument("--lookback-days",  type=int,   default=90)
    parser.add_argument("--memory-dir",     default="")
    parser.add_argument("--output-json",    default="")
    parser.add_argument("--config",         default="")
    parser.add_argument("--candle-csv",     default="",  help="Optional CSV file of candles (bypass MT5)")
    # FIX: raised to 5 to match new mt5_config.json open_threshold=5
    parser.add_argument("--open-threshold", type=int,   default=5)
    parser.add_argument("--close-threshold",type=int,   default=2)
    parser.add_argument("--sl-pct",         type=float, default=2.0)
    parser.add_argument("--tp-pct",         type=float, default=3.0)
    parser.add_argument("--partial-tp-pct", type=float, default=1.8)
    parser.add_argument("--partial-tp-frac",type=float, default=0.5)
    parser.add_argument("--trail-trigger",  type=float, default=1.5)
    parser.add_argument("--trail-distance", type=float, default=0.8)
    parser.add_argument("--breakeven",      type=float, default=1.0)
    parser.add_argument("--balance",        type=float, default=10000.0)
    parser.add_argument("--lot-fraction",   type=float, default=0.10,
                        help="Fraction of balance per trade (0.1 = 10%)")
    parser.add_argument("--pattern-length", type=int,   default=12)
    parser.add_argument("--sim-threshold",  type=float, default=0.92)
    # FIX: Python 3.14 raises ValueError for store_true with default=True.
    # Use store_false on --no-long / --no-short only; derive enable from those.
    parser.add_argument("--no-long",   action="store_true", default=False,
                        help="Disable long trades")
    parser.add_argument("--no-short",  action="store_true", default=False,
                        help="Disable short trades")
    # Custom date range (alternative to lookback-days)
    parser.add_argument("--start",          default="",  help="Start date YYYY-MM-DD")
    parser.add_argument("--end",            default="",  help="End date YYYY-MM-DD")
    args = parser.parse_args()

    tf_label = normalize_tf(args.timeframe)
    coin     = args.coin.strip().upper()

    # Resolve MT5 symbol
    sym_map = {
        "BTC":"BTCUSD","ETH":"ETHUSD","XRP":"XRPUSD",
        "DOGE":"DOGUSD","BNB":"BNBUSD",
    }
    mt5_symbol = args.mt5_symbol or sym_map.get(coin, f"{coin}USD")

    # Resolve memory dir
    base_dir = os.path.abspath(os.path.dirname(__file__))
    memory_dir = os.path.abspath(args.memory_dir) if args.memory_dir else base_dir
    if os.path.commonpath([base_dir, memory_dir]) != base_dir:
        log(f"[WARN] memory_dir outside mt5 ignored: {memory_dir} -> {base_dir}")
        memory_dir = base_dir

    # Lookback / date range
    lookback_days = args.lookback_days
    if args.start and args.end:
        try:
            from datetime import datetime as _dt
            d0 = _dt.strptime(args.start, "%Y-%m-%d")
            d1 = _dt.strptime(args.end, "%Y-%m-%d")
            lookback_days = max(1, (d1 - d0).days)
        except Exception:
            pass

    log(f"[BT] coin={coin}  symbol={mt5_symbol}  tf={tf_label}  "
        f"lookback={lookback_days}d  memory={memory_dir}")

    # Fetch candles
    if args.candle_csv and os.path.isfile(args.candle_csv):
        candles = fetch_candles_csv(args.candle_csv)
    else:
        config_path = args.config
        if not config_path:
            # Auto-locate config
            base = os.path.dirname(os.path.abspath(__file__))
            guesses = [
                os.path.join(base, "mt5_config.json"),
            ]
            for g in guesses:
                if os.path.isfile(g):
                    config_path = g
                    break
        candles = fetch_candles_mt5(mt5_symbol, tf_label, lookback_days, config_path)

    if not candles:
        log("[ERROR] No candles available. "
            "Make sure MT5 is running or supply --candle-csv.")
        # Write empty result so hub doesn't crash
        if args.output_json:
            os.makedirs(os.path.dirname(args.output_json) or ".", exist_ok=True)
            with open(args.output_json, "w", encoding="utf-8") as f:
                json.dump(_empty_result(coin, tf_label), f, indent=2)
        return 1

    # Filter by date range if start/end given
    if args.start:
        try:
            from datetime import datetime as _dt
            t0 = _dt.strptime(args.start, "%Y-%m-%d").timestamp()
            candles = [c for c in candles if c["ts"] >= t0]
        except Exception:
            pass
    if args.end:
        try:
            from datetime import datetime as _dt
            t1 = _dt.strptime(args.end, "%Y-%m-%d").timestamp() + 86400
            candles = [c for c in candles if c["ts"] <= t1]
        except Exception:
            pass

    result = run_backtest(
        coin=coin,
        tf_label=tf_label,
        candles=candles,
        memory_dir=memory_dir,
        open_threshold=args.open_threshold,
        close_threshold=args.close_threshold,
        sl_pct=args.sl_pct,
        tp_pct=args.tp_pct,
        partial_tp_pct=args.partial_tp_pct,
        partial_tp_fraction=args.partial_tp_frac,
        trailing_sl_trigger_pct=args.trail_trigger,
        trailing_sl_distance_pct=args.trail_distance,
        breakeven_trigger_pct=args.breakeven,
        initial_balance=args.balance,
        lot_fraction=args.lot_fraction,
        pattern_length=args.pattern_length,
        sim_threshold=args.sim_threshold,
        enable_long=not args.no_long,
        enable_short=not args.no_short,
    )

    if args.output_json:
        out_path = os.path.abspath(args.output_json)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        log(f"[BT] Results -> {out_path}")
    else:
        print(json.dumps(result["summary"], indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
