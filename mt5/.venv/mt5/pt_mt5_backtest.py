import argparse
import csv
import json
import os
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


try:
    mt5 = __import__("MetaTrader5")
except ImportError:
    os_name = platform.system() or "Unknown"
    if os_name == "Windows":
        print("MetaTrader5 package is not installed in this Python environment.")
        print("Run: pip install -r requirements.txt")
    else:
        print(f"MetaTrader5 package is not available on {os_name}.")
        print("This backtester must run on Windows with MetaTrader 5 installed.")
    sys.exit(1)


@dataclass
class SymbolConfig:
    bot_symbol: str
    mt5_symbol: str
    lot: float
    magic: int
    enable_long: bool
    enable_short: bool


@dataclass
class Position:
    side: str
    volume: float
    entry_price: float


@dataclass
class BarSignal:
    long_sig: int
    short_sig: int


@dataclass
class BacktestResult:
    symbol: str
    bars: int
    trades_opened: int
    trades_closed: int
    realized_pnl: float
    floating_pnl: float
    dca_events: int
    trailing_sell_events: int
    wins: int
    losses: int
    forced_stop_events: int
    max_drawdown_pct: float


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    print(f"[{now()}] {msg}")


def parse_float_file(path: str, default: float = 0.0) -> float:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return float((f.read() or "").strip())
    except Exception:
        return default


def parse_int_file(path: str, default: int = 0) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(float((f.read() or "").strip()))
    except Exception:
        return default


def resolve_coin_folder(signals_root: str, bot_symbol: str) -> str:
    sym = str(bot_symbol).strip().upper()
    if sym == "BTC":
        return signals_root
    candidate = os.path.join(signals_root, sym)
    if os.path.isdir(candidate):
        return candidate
    return signals_root


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    config_dir = os.path.dirname(os.path.abspath(path))
    required = ["login", "password", "server", "symbols"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError(f"Missing config keys: {', '.join(missing)}")

    if not isinstance(cfg["symbols"], list) or not cfg["symbols"]:
        raise ValueError("'symbols' must be a non-empty list")

    default_signals_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    raw_signals_root = str(cfg.get("signals_root", "")).strip()
    if not raw_signals_root:
        cfg["signals_root"] = default_signals_root
    elif os.path.isabs(raw_signals_root):
        cfg["signals_root"] = raw_signals_root
    else:
        cfg["signals_root"] = os.path.abspath(os.path.join(config_dir, raw_signals_root))

    raw_terminal_path = str(cfg.get("terminal_path", "")).strip()
    if raw_terminal_path:
        cfg["terminal_path"] = (
            raw_terminal_path
            if os.path.isabs(raw_terminal_path)
            else os.path.abspath(os.path.join(config_dir, raw_terminal_path))
        )

    cfg.setdefault("open_threshold", 3)
    cfg.setdefault("trade_start_level", 3)
    cfg.setdefault("close_threshold", 2)
    cfg.setdefault("max_scale_ins", 5)
    cfg.setdefault("close_on_opposite_signal", True)
    cfg.setdefault("use_profit_margin_tp", True)
    cfg.setdefault("start_allocation_pct", 0.005)
    cfg.setdefault("dca_multiplier", 2.0)
    cfg.setdefault("dca_levels", [-2.5, -5.0, -10.0, -20.0, -30.0, -40.0, -50.0])
    cfg.setdefault("max_dca_buys_per_24h", 2)
    cfg.setdefault("pm_start_pct_no_dca", 5.0)
    cfg.setdefault("pm_start_pct_with_dca", 2.5)
    cfg.setdefault("trailing_gap_pct", 0.5)
    cfg.setdefault("max_dca_per_trade", 6)
    cfg.setdefault("per_coin_stop_loss_pct", -35.0)
    cfg.setdefault("start_balance_per_symbol", 10000.0)
    cfg.setdefault("max_notional_leverage", 1.0)
    cfg.setdefault("force_close_drawdown_pct", 25.0)
    cfg.setdefault("terminal_path", "")

    parsed_symbols: List[SymbolConfig] = []
    base_magic = int(cfg.get("base_magic", 880000))

    for idx, raw in enumerate(cfg["symbols"]):
        if isinstance(raw, dict):
            bot_symbol = str(raw.get("bot_symbol", "")).strip().upper()
            mt5_symbol = str(raw.get("mt5_symbol", "")).strip()
            if not bot_symbol or not mt5_symbol:
                raise ValueError("Each symbol object must include bot_symbol and mt5_symbol")
            lot = float(raw.get("lot", cfg.get("default_lot", 0.01)))
            enable_long = bool(raw.get("enable_long", True))
            enable_short = bool(raw.get("enable_short", True))
            magic = int(raw.get("magic", base_magic + idx))
        elif isinstance(raw, str):
            text = raw.strip()
            if not text:
                continue
            if ":" in text:
                bot_symbol, mt5_symbol = [p.strip() for p in text.split(":", 1)]
            else:
                bot_symbol, mt5_symbol = text, text
            lot = float(cfg.get("default_lot", 0.01))
            enable_long = True
            enable_short = True
            magic = base_magic + idx
        else:
            raise ValueError("'symbols' entries must be strings or objects")

        if lot <= 0:
            raise ValueError(f"lot must be > 0 for symbol {bot_symbol}")

        parsed_symbols.append(
            SymbolConfig(
                bot_symbol=bot_symbol.upper(),
                mt5_symbol=mt5_symbol,
                lot=lot,
                magic=magic,
                enable_long=enable_long,
                enable_short=enable_short,
            )
        )

    if not parsed_symbols:
        raise ValueError("No valid symbols configured")

    cfg["_parsed_symbols"] = parsed_symbols
    return cfg


def initialize_mt5(config: Dict[str, Any]) -> None:
    terminal_path = config.get("terminal_path")
    if terminal_path:
        ok = mt5.initialize(path=terminal_path)
    else:
        ok = mt5.initialize()
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    authorized = mt5.login(
        int(config["login"]),
        password=str(config["password"]),
        server=str(config["server"]),
    )
    if not authorized:
        raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")


def _symbol_candidates() -> List[str]:
    symbols = mt5.symbols_get()
    if not symbols:
        return []
    return [str(s.name) for s in symbols]


def _resolve_mt5_symbol(requested_symbol: str, available: List[str]) -> Optional[str]:
    requested = str(requested_symbol or "").strip()
    if not requested:
        return None

    requested_upper = requested.upper()

    for s in available:
        if s.upper() == requested_upper:
            return s

    starts_with = [s for s in available if s.upper().startswith(requested_upper)]
    if starts_with:
        return sorted(starts_with, key=len)[0]

    contains = [s for s in available if requested_upper in s.upper()]
    if contains:
        return sorted(contains, key=len)[0]

    return None


def resolve_symbols(symbols: List[SymbolConfig]) -> List[SymbolConfig]:
    available = _symbol_candidates()
    resolved: List[SymbolConfig] = []

    for sym_cfg in symbols:
        if not sym_cfg.enable_long and not sym_cfg.enable_short:
            log(f"[SKIP] Disabled in config: {sym_cfg.bot_symbol}")
            continue

        matched = _resolve_mt5_symbol(sym_cfg.mt5_symbol, available)
        if not matched:
            log(f"[SKIP] Symbol not found: {sym_cfg.mt5_symbol} (bot={sym_cfg.bot_symbol})")
            continue

        if matched != sym_cfg.mt5_symbol:
            log(f"[MAP] {sym_cfg.bot_symbol}: {sym_cfg.mt5_symbol} -> {matched}")
            sym_cfg.mt5_symbol = matched

        if mt5.symbol_info(sym_cfg.mt5_symbol) is None:
            log(f"[SKIP] symbol_info unavailable: {sym_cfg.mt5_symbol}")
            continue

        resolved.append(sym_cfg)

    return resolved


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
    }
    if key not in mapping:
        raise ValueError(f"Unsupported timeframe: {name}")
    return mapping[key]


def parse_dt(text: str) -> datetime:
    s = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Invalid datetime: {text}")


def load_signal_series_csv(path: str) -> Dict[int, BarSignal]:
    data: Dict[int, BarSignal] = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}

        time_col = cols.get("time") or cols.get("timestamp") or cols.get("datetime")
        long_col = cols.get("long_sig") or cols.get("long")
        short_col = cols.get("short_sig") or cols.get("short")

        if not time_col or not long_col or not short_col:
            raise ValueError(
                f"CSV {path} must include columns: time/timestamp/datetime, long_sig, short_sig"
            )

        for row in reader:
            t = str(row.get(time_col, "")).strip()
            if not t:
                continue
            ts = int(parse_dt(t).timestamp())
            ls = int(float(str(row.get(long_col, "0") or "0")))
            ss = int(float(str(row.get(short_col, "0") or "0")))
            data[ts] = BarSignal(long_sig=ls, short_sig=ss)

    return data


def save_signal_series_csv(path: str, data: Dict[int, BarSignal]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "long_sig", "short_sig"])
        for ts in sorted(data.keys()):
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            v = data[ts]
            writer.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), int(v.long_sig), int(v.short_sig)])


def signal_series_all_zero(data: Dict[int, BarSignal]) -> bool:
    if not data:
        return True
    for v in data.values():
        if int(v.long_sig) != 0 or int(v.short_sig) != 0:
            return False
    return True


def seed_validation_signal_series(timestamps: List[int]) -> Dict[int, BarSignal]:
    out: Dict[int, BarSignal] = {}
    for i, ts in enumerate(sorted(timestamps)):
        phase = i % 120
        if phase < 8:
            out[ts] = BarSignal(long_sig=3, short_sig=0)
        elif phase < 16:
            out[ts] = BarSignal(long_sig=4, short_sig=0)
        elif phase < 24:
            out[ts] = BarSignal(long_sig=5, short_sig=0)
        else:
            out[ts] = BarSignal(long_sig=0, short_sig=0)
    return out


def rate_value(rate: Any, field: str, default: float = 0.0) -> float:
    # MT5 rates are often numpy structured rows (numpy.void), which do not expose attributes.
    try:
        return float(rate[field])
    except Exception:
        pass

    try:
        return float(getattr(rate, field))
    except Exception:
        return float(default)


def ensure_signal_csv(
    path: str,
    rates: List[Any],
    default_long_sig: int,
    default_short_sig: int,
) -> bool:
    if os.path.isfile(path):
        return False

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "long_sig", "short_sig"])
        for r in rates:
            ts = int(rate_value(r, "time", 0.0))
            if ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            writer.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), default_long_sig, default_short_sig])

    return True


def read_current_signal_defaults(signals_root: str, bot_symbol: str) -> Tuple[int, int, float, float]:
    folder = resolve_coin_folder(signals_root, bot_symbol)
    long_sig = parse_int_file(os.path.join(folder, "long_dca_signal.txt"), 0)
    short_sig = parse_int_file(os.path.join(folder, "short_dca_signal.txt"), 0)
    long_pm = parse_float_file(os.path.join(folder, "futures_long_profit_margin.txt"), 0.25)
    short_pm = parse_float_file(os.path.join(folder, "futures_short_profit_margin.txt"), 0.25)
    return long_sig, short_sig, max(0.0, long_pm), max(0.0, short_pm)


def weighted_entry_price(positions: List[Position]) -> float:
    total = 0.0
    weighted = 0.0
    for p in positions:
        total += p.volume
        weighted += p.entry_price * p.volume
    if total <= 0:
        return 0.0
    return weighted / total


def total_notional_usd(positions: List[Position], price: float, contract_size: float) -> float:
    if price <= 0 or contract_size <= 0:
        return 0.0
    return sum((p.volume * contract_size * price) for p in positions)


def make_bid_ask(mid_price: float, spread_bps: float) -> Tuple[float, float]:
    if mid_price <= 0:
        return 0.0, 0.0
    half = max(0.0, float(spread_bps)) / 20000.0
    bid = mid_price * (1.0 - half)
    ask = mid_price * (1.0 + half)
    return bid, ask


def close_all(
    positions: List[Position],
    close_price: float,
    contract_size: float,
    side: str,
) -> Tuple[List[Position], float, int]:
    kept: List[Position] = []
    realized = 0.0
    closed_count = 0

    for p in positions:
        if p.side != side:
            kept.append(p)
            continue

        if side == "long":
            pnl = (close_price - p.entry_price) * p.volume * contract_size
        else:
            pnl = (p.entry_price - close_price) * p.volume * contract_size

        realized += pnl
        closed_count += 1

    return kept, realized, closed_count


def run_backtest_for_symbol(
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
    rates: List[Any],
    spread_bps: float,
    signal_csv: Optional[str],
    auto_fix_signals: bool,
    start_balance_per_symbol: float,
) -> BacktestResult:
    trade_start_level = max(1, min(int(config.get("trade_start_level", 3)), 7))
    dca_multiplier = max(0.0, float(config.get("dca_multiplier", 2.0)))
    max_dca_buys_per_24h = max(0, int(config.get("max_dca_buys_per_24h", 2)))
    pm_start_pct_no_dca = max(0.0, float(config.get("pm_start_pct_no_dca", 5.0)))
    pm_start_pct_with_dca = max(0.0, float(config.get("pm_start_pct_with_dca", 2.5)))
    trailing_gap_pct = max(0.0, float(config.get("trailing_gap_pct", 0.5)))
    max_dca_per_trade = max(0, int(config.get("max_dca_per_trade", 6)))
    per_coin_stop_loss_pct = float(config.get("per_coin_stop_loss_pct", -35.0))
    max_notional_leverage = max(0.0, float(config.get("max_notional_leverage", 1.0)))
    force_close_drawdown_pct = max(0.0, float(config.get("force_close_drawdown_pct", 25.0)))

    dca_levels_raw = config.get("dca_levels", [-2.5, -5.0, -10.0, -20.0, -30.0, -40.0, -50.0])
    dca_levels: List[float] = []
    if isinstance(dca_levels_raw, list):
        for v in dca_levels_raw:
            try:
                dca_levels.append(float(v))
            except Exception:
                pass
    if not dca_levels:
        dca_levels = [-2.5, -5.0, -10.0, -20.0, -30.0, -40.0, -50.0]

    default_long_sig, default_short_sig, long_pm, short_pm = read_current_signal_defaults(
        str(config["signals_root"]), sym_cfg.bot_symbol
    )

    signal_series: Dict[int, BarSignal] = {}
    if signal_csv and os.path.isfile(signal_csv):
        signal_series = load_signal_series_csv(signal_csv)
        log(f"[INFO] Loaded signal history for {sym_cfg.bot_symbol}: {signal_csv}")
    elif signal_csv:
        log(f"[WARN] Signal CSV not found for {sym_cfg.bot_symbol}: {signal_csv}")

    if auto_fix_signals and signal_series_all_zero(signal_series):
        ts_list = [int(rate_value(r, "time", 0.0)) for r in rates]
        ts_list = [x for x in ts_list if x > 0]
        if ts_list:
            signal_series = seed_validation_signal_series(ts_list)
            if signal_csv:
                save_signal_series_csv(signal_csv, signal_series)
                log(f"[AUTO] Repaired all-zero signals with validation pattern: {signal_csv}")

    if signal_series:
        long_vals = [v.long_sig for v in signal_series.values()]
        short_vals = [v.short_sig for v in signal_series.values()]
        long_max = max(long_vals) if long_vals else 0
        short_max = max(short_vals) if short_vals else 0
        long_ge_open = sum(1 for v in long_vals if v >= trade_start_level)
        short_eq_zero = sum(1 for v in short_vals if v == 0)
        log(
            f"[SIG] {sym_cfg.bot_symbol} rows={len(signal_series)} "
            f"L(max={long_max}, >=open={long_ge_open}) "
            f"S(max={short_max}, ==0={short_eq_zero}) trade_start_level={trade_start_level}"
        )
    else:
        log(
            f"[SIG] {sym_cfg.bot_symbol} using static txt signals "
            f"L/S={default_long_sig}/{default_short_sig} trade_start_level={trade_start_level}"
        )

    info = mt5.symbol_info(sym_cfg.mt5_symbol)
    contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)

    positions: List[Position] = []  # Long-only for trader-style simulation.
    trades_opened = 0
    trades_closed = 0
    realized_pnl = 0.0
    dca_events = 0
    trailing_sell_events = 0
    dca_buys_ts: List[int] = []
    dca_triggered_stages: List[int] = []
    wins = 0
    losses = 0
    forced_stop_events = 0
    max_drawdown_pct = 0.0
    equity_peak = float(start_balance_per_symbol)

    trail_active = False
    trail_line = 0.0
    trail_peak = 0.0
    trail_was_above = False

    for r in rates:
        mid = rate_value(r, "close", 0.0)
        bid, ask = make_bid_ask(mid, spread_bps)
        if bid <= 0 or ask <= 0:
            continue

        ts = int(rate_value(r, "time", 0.0))
        bar_sig = signal_series.get(ts)
        if bar_sig:
            long_sig, short_sig = bar_sig.long_sig, bar_sig.short_sig
        else:
            long_sig, short_sig = default_long_sig, default_short_sig

        longs = [p for p in positions if p.side == "long"]

        # Trader-style start: long signal >= start level AND short signal == 0.
        if not longs and sym_cfg.enable_long and long_sig >= trade_start_level and short_sig == 0:
            candidate = Position(side="long", volume=sym_cfg.lot, entry_price=ask)
            projected_notional = total_notional_usd([candidate], ask, contract_size)
            max_allowed_notional = float(start_balance_per_symbol) * max_notional_leverage
            if projected_notional <= max_allowed_notional:
                positions.append(candidate)
                trades_opened += 1
                dca_triggered_stages = []
                dca_buys_ts = []
                trail_active = False
                trail_line = 0.0
                trail_peak = 0.0
                trail_was_above = False
                longs = [p for p in positions if p.side == "long"]
            else:
                longs = []

        if not longs:
            continue

        avg_open = weighted_entry_price(longs)
        if avg_open <= 0:
            continue

        gain_loss_pct_buy = ((ask - avg_open) / avg_open) * 100.0

        # Safety stop: force close if per-coin drawdown breaches hard stop.
        if gain_loss_pct_buy <= per_coin_stop_loss_pct:
            positions, pnl, closed = close_all(positions, bid, contract_size, "long")
            realized_pnl += pnl
            trades_closed += closed
            forced_stop_events += 1
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            dca_triggered_stages = []
            dca_buys_ts = []
            trail_active = False
            trail_line = 0.0
            trail_peak = 0.0
            trail_was_above = False
            continue

        # Safety stop: force close if equity drawdown breaches configured cap.
        floating_now = sum((bid - p.entry_price) * p.volume * contract_size for p in positions if p.side == "long")
        equity_now = float(start_balance_per_symbol) + realized_pnl + floating_now
        if equity_now > equity_peak:
            equity_peak = equity_now
        if equity_peak > 0:
            dd_pct_now = ((equity_peak - equity_now) / equity_peak) * 100.0
            if dd_pct_now > max_drawdown_pct:
                max_drawdown_pct = dd_pct_now
            if dd_pct_now >= force_close_drawdown_pct:
                positions, pnl, closed = close_all(positions, bid, contract_size, "long")
                realized_pnl += pnl
                trades_closed += closed
                forced_stop_events += 1
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1
                dca_triggered_stages = []
                dca_buys_ts = []
                trail_active = False
                trail_line = 0.0
                trail_peak = 0.0
                trail_was_above = False
                continue

        # Trader-style DCA stage logic.
        current_stage = len(dca_triggered_stages)
        hard_level = dca_levels[current_stage] if current_stage < len(dca_levels) else dca_levels[-1]
        hard_hit = gain_loss_pct_buy <= hard_level

        neural_dca_max = max(0, 7 - trade_start_level)
        neural_hit = False
        if current_stage < neural_dca_max:
            neural_needed = trade_start_level + 1 + current_stage
            neural_hit = (gain_loss_pct_buy < 0.0) and (long_sig >= neural_needed)

        # rolling 24h DCA cap
        cutoff = ts - (24 * 60 * 60)
        dca_buys_ts = [x for x in dca_buys_ts if x >= cutoff]

        dca_trade_cap_ok = current_stage < max_dca_per_trade
        if (hard_hit or neural_hit) and (len(dca_buys_ts) < max_dca_buys_per_24h) and dca_trade_cap_ok:
            position_value_usd = total_notional_usd(longs, ask, contract_size)
            dca_amount_usd = position_value_usd * dca_multiplier
            if dca_amount_usd > 0 and ask > 0 and contract_size > 0:
                dca_volume = dca_amount_usd / (ask * contract_size)
                candidate = Position(side="long", volume=dca_volume, entry_price=ask)
                projected_notional = total_notional_usd(longs + [candidate], ask, contract_size)
                max_allowed_notional = float(start_balance_per_symbol) * max_notional_leverage
                if projected_notional <= max_allowed_notional:
                    positions.append(candidate)
                    trades_opened += 1
                    dca_events += 1
                    dca_triggered_stages.append(current_stage)
                    dca_buys_ts.append(ts)
                    # Reset trailing state on DCA like trader resets trailing for fresh avg basis.
                    trail_active = False
                    trail_line = 0.0
                    trail_peak = 0.0
                    trail_was_above = False

        # Trader-style trailing PM sell logic (uses sell-side/bid price).
        longs = [p for p in positions if p.side == "long"]
        if not longs:
            continue

        avg_open = weighted_entry_price(longs)
        if avg_open <= 0:
            continue

        pm_start_pct = pm_start_pct_no_dca if len(dca_triggered_stages) == 0 else pm_start_pct_with_dca
        base_pm_line = avg_open * (1.0 + (pm_start_pct / 100.0))
        gap = trailing_gap_pct / 100.0

        if not trail_active:
            trail_line = base_pm_line
        else:
            trail_line = max(trail_line, base_pm_line)

        above_now = bid >= trail_line
        if (not trail_active) and above_now:
            trail_active = True
            trail_peak = bid

        if trail_active:
            if bid > trail_peak:
                trail_peak = bid

            new_line = max(base_pm_line, trail_peak * (1.0 - gap))
            if new_line > trail_line:
                trail_line = new_line

            if trail_was_above and (bid < trail_line):
                positions, pnl, closed = close_all(positions, bid, contract_size, "long")
                realized_pnl += pnl
                trades_closed += closed
                trailing_sell_events += 1
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1
                dca_triggered_stages = []
                dca_buys_ts = []
                trail_active = False
                trail_line = 0.0
                trail_peak = 0.0
                trail_was_above = False
                continue

        trail_was_above = above_now

        # Track max drawdown using per-symbol equity curve.
        floating_now = sum((bid - p.entry_price) * p.volume * contract_size for p in positions if p.side == "long")
        equity_now = float(start_balance_per_symbol) + realized_pnl + floating_now
        if equity_now > equity_peak:
            equity_peak = equity_now
        if equity_peak > 0:
            dd_pct = ((equity_peak - equity_now) / equity_peak) * 100.0
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct

    last_price = rate_value(rates[-1], "close", 0.0) if rates else 0.0
    last_bid, last_ask = make_bid_ask(last_price, spread_bps)

    floating_pnl = 0.0
    for p in positions:
        floating_pnl += (last_bid - p.entry_price) * p.volume * contract_size

    return BacktestResult(
        symbol=sym_cfg.bot_symbol,
        bars=len(rates),
        trades_opened=trades_opened,
        trades_closed=trades_closed,
        realized_pnl=realized_pnl,
        floating_pnl=floating_pnl,
        dca_events=dca_events,
        trailing_sell_events=trailing_sell_events,
        wins=wins,
        losses=losses,
        forced_stop_events=forced_stop_events,
        max_drawdown_pct=max_drawdown_pct,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PowerTrader MT5 backtester (same bridge logic)")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "mt5_config.json"),
        help="Path to MT5 JSON config file",
    )
    parser.add_argument("--start", required=True, help="UTC start, e.g. 2026-01-01 or 2026-01-01 00:00")
    parser.add_argument("--end", required=True, help="UTC end, e.g. 2026-03-01 or 2026-03-01 00:00")
    parser.add_argument("--timeframe", default="H1", help="M1,M5,M15,M30,H1,H4,D1")
    parser.add_argument(
        "--spread-bps",
        type=float,
        default=2.0,
        help="Assumed full spread in bps around close price (default: 2.0)",
    )
    parser.add_argument(
        "--signals-dir",
        default="",
        help=(
            "Optional folder containing per-symbol CSV signal history named "
            "<BOT_SYMBOL>_signals.csv with columns: time,long_sig,short_sig"
        ),
    )
    parser.add_argument(
        "--auto-fix-signals",
        action="store_true",
        help="If signal CSVs are all zeros, auto-seed validation signal pattern and continue",
    )
    parser.add_argument(
        "--confirm-logic",
        action="store_true",
        help="Print PASS/FAIL confirmation checks for trader-style logic paths",
    )
    parser.add_argument(
        "--min-win-rate",
        type=float,
        default=40.0,
        help="Minimum win rate percent required for GO decision (default: 40)",
    )
    parser.add_argument(
        "--start-balance-per-symbol",
        type=float,
        default=10000.0,
        help="Virtual starting balance per symbol used for drawdown tracking (default: 10000)",
    )
    parser.add_argument(
        "--max-drawdown-pct",
        type=float,
        default=25.0,
        help="Maximum allowed per-symbol drawdown percent for GO decision (default: 25)",
    )
    parser.add_argument(
        "--max-forced-stops",
        type=int,
        default=0,
        help="Maximum allowed forced stop events across symbols for GO decision (default: 0)",
    )
    parser.add_argument(
        "--max-notional-leverage",
        type=float,
        default=1.0,
        help=(
            "Maximum per-symbol notional exposure as a multiple of start balance "
            "(default: 1.0, i.e. <= 1x)"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = os.path.abspath(args.config)
    if not os.path.isfile(config_path):
        log(f"Config file not found: {config_path}")
        return 1

    try:
        config = load_config(config_path)
        start_dt = parse_dt(args.start)
        end_dt = parse_dt(args.end)
        if end_dt <= start_dt:
            raise ValueError("--end must be after --start")

        tf = timeframe_from_name(args.timeframe)
        signals_dir = ""
        if args.signals_dir:
            signals_dir = os.path.abspath(args.signals_dir)
            os.makedirs(signals_dir, exist_ok=True)

        initialize_mt5(config)
        parsed_symbols: List[SymbolConfig] = resolve_symbols(config["_parsed_symbols"])
        if not parsed_symbols:
            raise RuntimeError("No active symbols to backtest")

        log(f"Using config: {config_path}")
        log(f"Backtest range: {start_dt} -> {end_dt} ({args.timeframe})")
        log(f"Symbols: {', '.join([s.bot_symbol for s in parsed_symbols])}")

        total_realized = 0.0
        total_floating = 0.0
        total_opened = 0
        total_dca = 0
        total_trailing_sells = 0
        config["max_notional_leverage"] = float(args.max_notional_leverage)
        config["force_close_drawdown_pct"] = float(args.max_drawdown_pct)
        total_wins = 0
        total_losses = 0
        total_forced_stops = 0
        worst_drawdown_pct = 0.0

        # Apply CLI safety overrides into config for a single run.
        config["start_balance_per_symbol"] = float(args.start_balance_per_symbol)
        config["max_dca_per_trade"] = int(config.get("max_dca_per_trade", 6))
        config["per_coin_stop_loss_pct"] = float(config.get("per_coin_stop_loss_pct", -35.0))

        for sym_cfg in parsed_symbols:
            rates = mt5.copy_rates_range(sym_cfg.mt5_symbol, tf, start_dt, end_dt)
            if rates is None or len(rates) == 0:
                log(f"[SKIP] No rates for {sym_cfg.bot_symbol}/{sym_cfg.mt5_symbol}")
                continue

            signal_csv = ""
            if signals_dir:
                signal_csv = os.path.join(signals_dir, f"{sym_cfg.bot_symbol}_signals.csv")
                default_long_sig, default_short_sig, _, _ = read_current_signal_defaults(
                    str(config["signals_root"]), sym_cfg.bot_symbol
                )
                if ensure_signal_csv(
                    path=signal_csv,
                    rates=list(rates),
                    default_long_sig=default_long_sig,
                    default_short_sig=default_short_sig,
                ):
                    log(f"[AUTO] Created signal CSV: {signal_csv}")

            result = run_backtest_for_symbol(
                config=config,
                sym_cfg=sym_cfg,
                rates=list(rates),
                spread_bps=float(args.spread_bps),
                signal_csv=signal_csv,
                auto_fix_signals=bool(args.auto_fix_signals),
                start_balance_per_symbol=float(config["start_balance_per_symbol"]),
            )

            total_realized += result.realized_pnl
            total_floating += result.floating_pnl
            total_opened += int(result.trades_opened)
            total_dca += int(result.dca_events)
            total_trailing_sells += int(result.trailing_sell_events)
            total_wins += int(result.wins)
            total_losses += int(result.losses)
            total_forced_stops += int(result.forced_stop_events)
            if float(result.max_drawdown_pct) > worst_drawdown_pct:
                worst_drawdown_pct = float(result.max_drawdown_pct)

            log(
                f"[RESULT] {result.symbol} bars={result.bars} opened={result.trades_opened} "
                f"dca={result.dca_events} trail_sells={result.trailing_sell_events} "
                f"wins={result.wins} losses={result.losses} "
                f"forced_stops={result.forced_stop_events} dd={result.max_drawdown_pct:.2f}% "
                f"closed={result.trades_closed} realized={result.realized_pnl:.4f} "
                f"floating={result.floating_pnl:.4f} total={(result.realized_pnl + result.floating_pnl):.4f}"
            )

        log(
            f"[TOTAL] realized={total_realized:.4f} floating={total_floating:.4f} "
            f"total={(total_realized + total_floating):.4f}"
        )

        closed_events = total_wins + total_losses
        win_rate = (100.0 * total_wins / closed_events) if closed_events > 0 else 0.0
        log(
            f"[STATS] closed_events={closed_events} wins={total_wins} losses={total_losses} "
            f"win_rate={win_rate:.2f}% forced_stops={total_forced_stops} "
            f"worst_drawdown={worst_drawdown_pct:.2f}%"
        )

        entry_ok = total_opened > 0
        dca_ok = total_dca > 0
        trail_ok = total_trailing_sells > 0
        pnl_ok = (total_realized + total_floating) >= 0.0
        win_ok = win_rate >= float(args.min_win_rate)
        dd_ok = worst_drawdown_pct <= float(args.max_drawdown_pct)
        stop_ok = total_forced_stops <= int(args.max_forced_stops)

        decision = "GO" if all([entry_ok, dca_ok, trail_ok, pnl_ok, win_ok, dd_ok, stop_ok]) else "NO-GO"
        reasons = []
        if not entry_ok:
            reasons.append("no entries")
        if not dca_ok:
            reasons.append("no dca events")
        if not trail_ok:
            reasons.append("no trailing sells")
        if not pnl_ok:
            reasons.append("negative total pnl")
        if not win_ok:
            reasons.append(f"win rate below {float(args.min_win_rate):.2f}%")
        if not dd_ok:
            reasons.append(f"drawdown above {float(args.max_drawdown_pct):.2f}%")
        if not stop_ok:
            reasons.append(f"forced stops above {int(args.max_forced_stops)}")

        if reasons:
            log(f"[AUTO-DECISION] {decision} ({'; '.join(reasons)})")
        else:
            log(f"[AUTO-DECISION] {decision}")

        if args.confirm_logic:
            checks = {
                "entry_path": total_opened > 0,
                "dca_path": total_dca > 0,
                "trailing_sell_path": total_trailing_sells > 0,
            }
            for k, ok in checks.items():
                log(f"[CHECK] {k}: {'PASS' if ok else 'FAIL'}")
            if all(checks.values()):
                log("[CHECK] trader-style logic confirmation: PASS")
            else:
                log("[CHECK] trader-style logic confirmation: PARTIAL/FAIL")
        return 0
    except Exception as e:
        log(f"[ERROR] {e}")
        return 1
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
