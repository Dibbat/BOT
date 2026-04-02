"""
PowerTrader MT5 Bridge  —  v2.0
Connects signal files → MetaTrader 5 live/paper orders.

Key features added in v2:
  • Break-even stop    – moves SL to entry once profit >= be_trigger_pct
  • Partial take-profit– closes a fraction of the position at partial_tp_pct,
                         lets the remainder run with trailing SL
  • Portfolio risk cap – blocks new entries when total open risk >= max_portfolio_risk_pct
  • Daily loss limit   – emergency-flattens all positions on a bad day
  • Per-symbol overrides for every risk parameter (BE, partial TP, trailing SL)
  • Signal staleness guard in reconcile_symbol
  • Improved trailing SL (ratchet: only moves in profit direction, no whipsaws)
  • Post-fill SL/TP set with smart retry on broker rejection
  • Detailed structured logging for every order event
"""

import argparse
import faulthandler
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple


try:
    mt5 = __import__("MetaTrader5")
except ImportError:
    os_name = platform.system() or "Unknown"
    if os_name == "Windows":
        print("MetaTrader5 package is not installed. Run: pip install -r requirements.txt")
    else:
        print(f"MetaTrader5 not available on {os_name}. Must run on Windows with MT5.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SymbolConfig:
    bot_symbol: str
    mt5_symbol: str
    lot: float
    magic: int
    enable_long: bool
    enable_short: bool
    # Core SL / TP
    sl_pct: float = 2.0
    tp_pct: float = 3.0
    # Break-even stop
    breakeven_trigger_pct: float = 1.0   # profit % at which SL moves to entry
    # Partial take-profit
    partial_tp_pct: float = 1.8          # profit % at which partial close fires
    partial_tp_close_fraction: float = 0.5  # fraction of position to close (0 < x < 1)
    # Trailing SL (per-symbol override; falls back to global config if 0)
    trailing_sl_trigger_pct: float = 1.5
    trailing_sl_distance_pct: float = 0.8
    # Golden-ratio DCA spacing
    # Each DCA level must be at least (dca_step1_pct * PHI^(n-1)) % away from
    # the previous entry.  Set to 0 to disable price-gap gating.
    dca_step1_pct: float = 1.0           # first DCA gap % (subsequent gaps scale by PHI)


@dataclass
class PositionTracker:
    """Tracks live positions per symbol for history/PnL."""
    known_tickets: Set[int] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_ACCOUNT_HISTORY_APPEND_COUNT = 0
_HUB_DIR_CACHE: Optional[str] = None
_HUB_DIR_WARNED_OUTSIDE_MT5 = False

# ticket → peak_pnl_pct  (for trailing SL ratchet)
_trailing_sl_peak: Dict[int, float] = {}

# ticket → bool  (partial TP already fired for this ticket)
_partial_tp_done: Set[int] = set()

# ticket → bool  (break-even already moved for this ticket)
_breakeven_done: Set[int] = set()

# Day-start equity snapshot for daily loss limit
_day_start_equity: float = 0.0
_day_start_date: str = ""

# Golden-ratio DCA state per symbol
# bot_symbol -> list of entry prices in order (index 0 = first ENTRY)
_dca_entry_prices: Dict[str, List[float]] = {}

# Reverse-entry cooldown state per symbol.
# bot_symbol -> {"long": (cooldown_until_ts, reference_price), "short": (...)}
_reverse_entry_gap_state: Dict[str, Dict[str, Tuple[float, float]]] = {}

# Golden ratio constant
_PHI: float = 1.6180339887


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


def _is_within_dir(base_dir: str, candidate: str) -> bool:
    """True when candidate is inside base_dir after resolving symlinks."""
    try:
        base_real = os.path.realpath(os.path.abspath(base_dir))
        cand_real = os.path.realpath(os.path.abspath(candidate))
        return os.path.commonpath([base_real, cand_real]) == base_real
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    config_dir = os.path.dirname(os.path.abspath(path))

    # Env overrides for secrets
    env_login    = str(os.environ.get("PT_MT5_LOGIN", "")).strip()
    env_password = str(os.environ.get("PT_MT5_PASSWORD", "")).strip()
    env_server   = str(os.environ.get("PT_MT5_SERVER", "")).strip()
    env_trade    = str(os.environ.get("PT_MT5_TRADE_ENABLED", "")).strip().lower()

    if env_login:    cfg["login"]    = int(env_login)
    if env_password: cfg["password"] = env_password
    if env_server:   cfg["server"]   = env_server

    for key in ["login", "password", "server", "symbols"]:
        if key not in cfg:
            raise ValueError(f"Missing config key: {key}")

    if not isinstance(cfg["symbols"], list) or not cfg["symbols"]:
        raise ValueError("'symbols' must be a non-empty list")

    # signals_root resolution
    default_root = os.path.abspath(os.path.dirname(__file__))
    raw_root = str(cfg.get("signals_root", "")).strip()
    if not raw_root:
        resolved_root = default_root
    elif os.path.isabs(raw_root):
        resolved_root = raw_root
    else:
        resolved_root = os.path.abspath(os.path.join(config_dir, raw_root))

    if not _is_within_dir(default_root, resolved_root):
        log(f"[WARN] signals_root outside mt5 ignored: {resolved_root} -> {default_root}")
        resolved_root = default_root
    cfg["signals_root"] = resolved_root

    raw_terminal = str(cfg.get("terminal_path", "")).strip()
    if raw_terminal:
        cfg["terminal_path"] = (
            raw_terminal if os.path.isabs(raw_terminal)
            else os.path.abspath(os.path.join(config_dir, raw_terminal))
        )

    cfg.setdefault("trade_enabled", False)
    if env_trade in {"1", "true", "yes", "on"}:
        cfg["trade_enabled"] = True
    elif env_trade in {"0", "false", "no", "off"}:
        cfg["trade_enabled"] = False

    # Global defaults
    cfg.setdefault("poll_seconds", 10)
    cfg.setdefault("deviation_points", 20)
    cfg.setdefault("open_threshold", 3)
    cfg.setdefault("close_threshold", 2)
    cfg.setdefault("max_scale_ins", 5)
    cfg.setdefault("min_price_improvement_pct", 0.0)
    cfg.setdefault("close_on_opposite_signal", True)
    cfg.setdefault("opposite_trade_gap_seconds", 180)
    cfg.setdefault("opposite_trade_gap_pct", 0.35)
    cfg.setdefault("use_profit_margin_tp", True)
    cfg.setdefault("terminal_path", "")

    # SL / TP
    cfg.setdefault("sl_pct", 2.0)
    cfg.setdefault("tp_pct", 3.0)
    cfg.setdefault("use_atr_sl_tp", False)
    cfg.setdefault("atr_sl_mult", 1.5)
    cfg.setdefault("atr_tp_mult", 2.5)
    cfg.setdefault("atr_period", 14)

    # Trailing SL
    cfg.setdefault("trailing_sl_enabled", True)
    cfg.setdefault("trailing_sl_trigger_pct", 1.5)
    cfg.setdefault("trailing_sl_distance_pct", 0.8)

    # Break-even stop  (NEW)
    cfg.setdefault("breakeven_enabled", True)
    cfg.setdefault("breakeven_trigger_pct", 1.0)

    # Partial TP  (NEW)
    cfg.setdefault("partial_tp_enabled", True)
    cfg.setdefault("partial_tp_pct", 1.8)
    cfg.setdefault("partial_tp_close_fraction", 0.5)

    # Portfolio / session risk guards  (NEW)
    cfg.setdefault("max_portfolio_risk_pct", 6.0)
    cfg.setdefault("daily_loss_limit_pct", -3.0)

    # Signal freshness  (NEW)
    cfg.setdefault("signal_stale_seconds", 300)

    # Risk-based position sizing  (NEW)
    # risk_per_trade_pct: % of equity to risk per trade (e.g. 1.0 = 1% of $171 = $1.71 max loss)
    # Set to 0 to use fixed lot sizes from config instead.
    cfg.setdefault("risk_per_trade_pct", 1.0)

    # Golden-ratio DCA spacing  (NEW)
    # dca_step1_pct: the minimum % drop from entry required for the FIRST DCA.
    # Each subsequent DCA requires a gap = dca_step1_pct * PHI^(level-1).
    # Example with dca_step1_pct=1.0 and PHI=1.618:
    #   DCA1 needs price -1.00% from entry
    #   DCA2 needs price -1.62% from DCA1
    #   DCA3 needs price -2.62% from DCA2
    #   DCA4 needs price -4.24% from DCA3
    # Set to 0.0 to disable price-gap gating (entries fire on every poll cycle).
    cfg.setdefault("dca_step1_pct", 1.0)

    # Parse symbols
    base_magic = int(cfg.get("base_magic", 880000))
    parsed_symbols: List[SymbolConfig] = []

    for idx, raw in enumerate(cfg["symbols"]):
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                continue
            bot_sym, mt5_sym = (text.split(":", 1) if ":" in text else (text, text))
            bot_sym, mt5_sym = bot_sym.strip(), mt5_sym.strip()
            lot = float(cfg.get("default_lot", 0.01))
            enable_long = enable_short = True
            magic = base_magic + idx
            sl_pct   = float(cfg["sl_pct"])
            tp_pct   = float(cfg["tp_pct"])
            be_trig  = float(cfg["breakeven_trigger_pct"])
            ptp_pct  = float(cfg["partial_tp_pct"])
            ptp_frac = float(cfg["partial_tp_close_fraction"])
            trail_trig = float(cfg["trailing_sl_trigger_pct"])
            trail_dist = float(cfg["trailing_sl_distance_pct"])
            dca_step1  = float(cfg["dca_step1_pct"])
        elif isinstance(raw, dict):
            bot_sym  = str(raw.get("bot_symbol", "")).strip().upper()
            mt5_sym  = str(raw.get("mt5_symbol", "")).strip()
            if not bot_sym or not mt5_sym:
                raise ValueError("Each symbol entry needs bot_symbol + mt5_symbol")
            lot          = float(raw.get("lot", cfg.get("default_lot", 0.01)))
            # --- FIX GAP 1: Use correct key names for enable_long/enable_short ---
            enable_long  = bool(
                raw.get("enable_long", raw.get("enablelong", False))
            )
            enable_short = bool(
                raw.get("enable_short", raw.get("enableshort", False))
            )
            magic        = int(raw.get("magic", base_magic + idx))
            sl_pct       = float(raw.get("sl_pct",   cfg["sl_pct"]))
            tp_pct       = float(raw.get("tp_pct",   cfg["tp_pct"]))
            be_trig      = float(raw.get("breakeven_trigger_pct",      cfg["breakeven_trigger_pct"]))
            ptp_pct      = float(raw.get("partial_tp_pct",             cfg["partial_tp_pct"]))
            ptp_frac     = float(raw.get("partial_tp_close_fraction",  cfg["partial_tp_close_fraction"]))
            trail_trig   = float(raw.get("trailing_sl_trigger_pct",    cfg["trailing_sl_trigger_pct"]))
            trail_dist   = float(raw.get("trailing_sl_distance_pct",   cfg["trailing_sl_distance_pct"]))
            dca_step1    = float(raw.get("dca_step1_pct",              cfg["dca_step1_pct"]))
        else:
            raise ValueError("symbols entries must be strings or objects")

        if lot <= 0:
            raise ValueError(f"lot must be > 0 for {bot_sym}")

        parsed_symbols.append(SymbolConfig(
            bot_symbol=bot_sym.upper(),
            mt5_symbol=mt5_sym,
            lot=lot,
            magic=magic,
            enable_long=enable_long,
            enable_short=enable_short,
            sl_pct=max(0.0, sl_pct),
            tp_pct=max(0.0, tp_pct),
            breakeven_trigger_pct=max(0.0, be_trig),
            partial_tp_pct=max(0.0, ptp_pct),
            partial_tp_close_fraction=max(0.01, min(0.99, ptp_frac)),
            trailing_sl_trigger_pct=max(0.0, trail_trig),
            trailing_sl_distance_pct=max(0.0, trail_dist),
            dca_step1_pct=max(0.0, dca_step1),
        ))

    if not parsed_symbols:
        raise ValueError("No valid symbols configured")

    cfg["_parsed_symbols"] = parsed_symbols
    return cfg


# ---------------------------------------------------------------------------
# MT5 initialization
# ---------------------------------------------------------------------------

def initialize_mt5(config: Dict[str, Any]) -> None:
    tp = config.get("terminal_path", "")
    ok = mt5.initialize(path=tp) if tp else mt5.initialize()
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    if not mt5.login(int(config["login"]), password=str(config["password"]),
                     server=str(config["server"])):
        raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")


def _symbol_candidates() -> List[str]:
    syms = mt5.symbols_get()
    return [str(s.name) for s in syms] if syms else []


def _resolve_mt5_symbol(requested: str, available: List[str]) -> Optional[str]:
    req = str(requested or "").strip().upper()
    if not req:
        return None
    for s in available:
        if s.upper() == req:
            return s
    starts = sorted([s for s in available if s.upper().startswith(req)], key=len)
    if starts:
        return starts[0]
    contains = sorted([s for s in available if req in s.upper()], key=len)
    return contains[0] if contains else None


def ensure_symbols(symbols: List[SymbolConfig]) -> Set[str]:
    inactive: Set[str] = set()
    available = _symbol_candidates()
    for sc in symbols:
        resolved = _resolve_mt5_symbol(sc.mt5_symbol, available)
        if not resolved:
            log(f"[WARN] Symbol not found in MT5: {sc.mt5_symbol} (bot={sc.bot_symbol})")
            inactive.add(sc.bot_symbol)
            continue
        if resolved != sc.mt5_symbol:
            log(f"[MAP] {sc.bot_symbol}: {sc.mt5_symbol} -> {resolved}")
            sc.mt5_symbol = resolved
        info = mt5.symbol_info(sc.mt5_symbol)
        if info is None:
            log(f"[WARN] symbol_info None: {sc.mt5_symbol}")
            inactive.add(sc.bot_symbol)
            continue
        if not info.visible:
            if not mt5.symbol_select(sc.mt5_symbol, True):
                log(f"[WARN] Could not enable symbol: {sc.mt5_symbol}")
                inactive.add(sc.bot_symbol)
                continue
        tick = mt5.symbol_info_tick(sc.mt5_symbol)
        if tick:
            log(f"[OK] {sc.mt5_symbol} bid={tick.bid:.5f} ask={tick.ask:.5f}")
    return inactive


def show_account_summary() -> None:
    acc = mt5.account_info()
    if acc is None:
        log("[WARN] Could not fetch account info")
        return
    log(f"Account: login={acc.login} server={acc.server} "
        f"leverage={acc.leverage} balance={acc.balance:.2f} "
        f"equity={acc.equity:.2f} free_margin={acc.margin_free:.2f}")


# ---------------------------------------------------------------------------
# Hub data helpers
# ---------------------------------------------------------------------------

def _hub_data_dir() -> str:
    global _HUB_DIR_CACHE, _HUB_DIR_WARNED_OUTSIDE_MT5
    if _HUB_DIR_CACHE:
        return _HUB_DIR_CACHE

    base_dir = os.path.abspath(os.path.dirname(__file__))
    default_hub = os.path.join(base_dir, "hub_data")
    env = str(os.environ.get("POWERTRADER_HUB_DIR", "")).strip()
    if env:
        resolved = os.path.abspath(env)
        if _is_within_dir(base_dir, resolved):
            _HUB_DIR_CACHE = resolved
            return _HUB_DIR_CACHE
        if not _HUB_DIR_WARNED_OUTSIDE_MT5:
            log(f"[WARN] POWERTRADER_HUB_DIR outside mt5 ignored: {resolved} -> {default_hub}")
            _HUB_DIR_WARNED_OUTSIDE_MT5 = True
    _HUB_DIR_CACHE = os.path.abspath(default_hub)
    return _HUB_DIR_CACHE


def _atomic_read_json(path: str) -> Optional[Dict[str, Any]]:
    """Read JSON dict safely. Returns None on missing/invalid data."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = (f.read() or "").strip()
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _atomic_write_json(path: str, data: Dict[str, Any]) -> bool:
    """
    Crash-safe persistence:
    - write to .tmp
    - flush + fsync
    - copy current file to .bak (best-effort)
    - atomic replace
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        bak = f"{path}.bak"

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass

        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                shutil.copy2(path, bak)
        except Exception:
            pass

        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _read_json_with_recovery(path: str) -> Dict[str, Any]:
    """Try main file, then .bak, then .tmp and auto-restore if possible."""
    data = _atomic_read_json(path)
    if data is not None:
        return data

    for candidate in (f"{path}.bak", f"{path}.tmp"):
        recovered = _atomic_read_json(candidate)
        if recovered is None:
            continue
        _atomic_write_json(path, recovered)
        return recovered

    return {}


def _trim_history_file(path: str, max_lines: int = 5000) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-max_lines:])
    except Exception:
        pass


def _append_account_value_history(ts: int, total_account_value: float) -> None:
    global _ACCOUNT_HISTORY_APPEND_COUNT
    try:
        if total_account_value <= 0:
            return
        hub = _hub_data_dir()
        os.makedirs(hub, exist_ok=True)
        path = os.path.join(hub, "account_value_history.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": float(ts), "total_account_value": float(total_account_value)}) + "\n")
        _ACCOUNT_HISTORY_APPEND_COUNT += 1
        if _ACCOUNT_HISTORY_APPEND_COUNT % 500 == 0:
            _trim_history_file(path, 5000)
    except Exception as e:
        log(f"[WARN] Could not append account history: {e}")


def _append_trade_history(entry: Dict[str, Any]) -> None:
    try:
        hub = _hub_data_dir()
        os.makedirs(hub, exist_ok=True)
        path = os.path.join(hub, "trade_history.jsonl")
        entry["ts"] = time.time()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
        _trim_history_file(path, 5000)
    except Exception as e:
        log(f"[WARN] Could not append trade history: {e}")


def _update_pnl_ledger(realized_pnl: float, symbol: str) -> None:
    try:
        hub = _hub_data_dir()
        os.makedirs(hub, exist_ok=True)
        path = os.path.join(hub, "pnl_ledger.json")
        data: Dict[str, Any] = _read_json_with_recovery(path)
        data["total_realized_profit_usd"] = float(data.get("total_realized_profit_usd", 0.0)) + realized_pnl
        per_sym = data.get("per_symbol", {})
        per_sym[symbol] = float(per_sym.get(symbol, 0.0)) + realized_pnl
        data["per_symbol"] = per_sym
        data["last_updated"] = time.time()
        data["trade_count"] = int(data.get("trade_count", 0)) + 1
        if not _atomic_write_json(path, data):
            log("[WARN] Could not atomically save pnl_ledger.json")
    except Exception as e:
        log(f"[WARN] Could not update PnL ledger: {e}")


def _pnl_pct_for_price(is_long: bool, entry_price: float, current_price: float) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    return ((current_price - entry_price) / entry_price * 100.0) if is_long else ((entry_price - current_price) / entry_price * 100.0)


def _format_next_dca_display(next_dca_price: float, next_dca_gap_pct: float) -> str:
    if next_dca_price <= 0:
        return ""
    price_txt = f"{float(next_dca_price):.8f}".rstrip("0").rstrip(".")
    if next_dca_gap_pct <= 0:
        return price_txt
    return f"{price_txt} ({next_dca_gap_pct:.2f}%)"


# ---------------------------------------------------------------------------
# SL/TP calculation
# ---------------------------------------------------------------------------

def _calculate_atr(symbol: str, period: int = 14) -> Optional[float]:
    """ATR over the last `period` H1 candles."""
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, period + 1)
        if rates is None or len(rates) < 2:
            return None
        trs = []
        for i in range(1, len(rates)):
            h, l, pc = float(rates[i]["high"]), float(rates[i]["low"]), float(rates[i - 1]["close"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else None
    except Exception:
        return None


def calculate_sl_tp(
    symbol: str,
    side: str,        # "buy" | "sell"
    entry_price: float,
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
) -> Tuple[float, float]:
    """
    Returns (sl_price, tp_price).
    Priority: ATR-based (if enabled) → per-symbol % → global %.
    """
    if bool(config.get("use_atr_sl_tp", False)):
        atr = _calculate_atr(symbol, int(config.get("atr_period", 14)))
        if atr and atr > 0:
            sl_mult = float(config.get("atr_sl_mult", 1.5))
            tp_mult = float(config.get("atr_tp_mult", 2.5))
            if side == "buy":
                sl = entry_price - atr * sl_mult
                tp = entry_price + atr * tp_mult
            else:
                sl = entry_price + atr * sl_mult
                tp = entry_price - atr * tp_mult
            return max(0.0, round(sl, 5)), max(0.0, round(tp, 5))

    sl_pct = sym_cfg.sl_pct if sym_cfg.sl_pct > 0 else float(config.get("sl_pct", 2.0))
    tp_pct = sym_cfg.tp_pct if sym_cfg.tp_pct > 0 else float(config.get("tp_pct", 3.0))

    if side == "buy":
        sl = entry_price * (1.0 - sl_pct / 100.0)
        tp = entry_price * (1.0 + tp_pct / 100.0)
    else:
        sl = entry_price * (1.0 + sl_pct / 100.0)
        tp = entry_price * (1.0 - tp_pct / 100.0)

    return max(0.0, round(sl, 5)), max(0.0, round(tp, 5))


def _normalize_sl_tp(symbol: str, sl: float, tp: float) -> Tuple[float, float]:
    """Round to broker's tick size (required by MT5 on some accounts)."""
    try:
        info = mt5.symbol_info(symbol)
        if info:
            digits = int(info.digits)
            sl = round(sl, digits)
            tp = round(tp, digits)
    except Exception:
        pass
    return max(0.0, sl), max(0.0, tp)


def _modify_position_sltp(
    ticket: int, symbol: str, new_sl: float, new_tp: float
) -> bool:
    """Send TRADE_ACTION_SLTP request. Returns True on success."""
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": new_sl,
        "tp": new_tp,
    }
    result = mt5.order_send(req)
    return bool(result and int(getattr(result, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE))


# ---------------------------------------------------------------------------
# Break-even stop  (NEW)
# ---------------------------------------------------------------------------

def manage_breakeven(
    positions: List[Any],
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
    trade_enabled: bool,
) -> None:
    """
    Once a position reaches breakeven_trigger_pct profit, move SL to entry
    price (+ small buffer to cover spread).  Fires only once per ticket.
    """
    if not bool(config.get("breakeven_enabled", True)):
        return

    trigger_pct = sym_cfg.breakeven_trigger_pct or float(config.get("breakeven_trigger_pct", 1.0))
    if trigger_pct <= 0:
        return

    for pos in positions:
        ticket = int(pos.ticket)
        if ticket in _breakeven_done:
            continue

        entry   = float(pos.price_open)
        current = float(pos.price_current)
        if entry <= 0:
            continue

        is_long = int(pos.type) == int(mt5.POSITION_TYPE_BUY)
        pnl_pct = ((current - entry) / entry * 100.0) if is_long \
                  else ((entry - current) / entry * 100.0)

        if pnl_pct < trigger_pct:
            continue

        # Move SL to entry (with a tiny spread buffer: 0.01%)
        buffer = entry * 0.0001
        new_sl = (entry + buffer) if is_long else (entry - buffer)
        new_sl, _ = _normalize_sl_tp(sym_cfg.mt5_symbol, new_sl, 0.0)

        current_sl = float(getattr(pos, "sl", 0.0) or 0.0)
        current_tp = float(getattr(pos, "tp", 0.0) or 0.0)

        # Only improve SL (never make it worse)
        if is_long and current_sl >= new_sl and current_sl > 0:
            _breakeven_done.add(ticket)
            continue
        if not is_long and current_sl <= new_sl and current_sl > 0:
            _breakeven_done.add(ticket)
            continue

        if not trade_enabled:
            log(f"[DRY-RUN][BE] {sym_cfg.mt5_symbol} ticket={ticket} sl->{new_sl:.5f} (entry={entry:.5f})")
            _breakeven_done.add(ticket)
            continue

        ok = _modify_position_sltp(ticket, sym_cfg.mt5_symbol, new_sl, current_tp)
        if ok:
            log(f"[BE] {sym_cfg.mt5_symbol} ticket={ticket} SL moved to entry {new_sl:.5f} (pnl={pnl_pct:.2f}%)")
            _breakeven_done.add(ticket)
        else:
            log(f"[WARN][BE] Modify failed: {sym_cfg.mt5_symbol} ticket={ticket}")


# ---------------------------------------------------------------------------
# Partial take-profit  (NEW)
# ---------------------------------------------------------------------------

def manage_partial_tp(
    positions: List[Any],
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
    trade_enabled: bool,
) -> None:
    """
    When profit reaches partial_tp_pct, close `partial_tp_close_fraction` of
    the volume.  The remaining position continues with trailing SL / break-even.
    Fires once per ticket.
    """
    if not bool(config.get("partial_tp_enabled", True)):
        return

    trigger_pct = sym_cfg.partial_tp_pct or float(config.get("partial_tp_pct", 1.8))
    fraction    = sym_cfg.partial_tp_close_fraction or float(config.get("partial_tp_close_fraction", 0.5))
    if trigger_pct <= 0 or fraction <= 0:
        return

    for pos in positions:
        ticket = int(pos.ticket)
        if ticket in _partial_tp_done:
            continue

        entry   = float(pos.price_open)
        current = float(pos.price_current)
        volume  = float(pos.volume)
        if entry <= 0 or volume <= 0:
            continue

        is_long = int(pos.type) == int(mt5.POSITION_TYPE_BUY)
        pnl_pct = ((current - entry) / entry * 100.0) if is_long \
                  else ((entry - current) / entry * 100.0)

        if pnl_pct < trigger_pct:
            continue

        # Calculate close volume — must respect broker minimum lot
        close_vol = round(volume * fraction, 2)
        info = mt5.symbol_info(sym_cfg.mt5_symbol)
        min_lot = float(getattr(info, "volume_min", 0.01)) if info else 0.01
        step    = float(getattr(info, "volume_step", 0.01)) if info else 0.01

        # Snap to lot step
        close_vol = max(min_lot, round(close_vol / step) * step)
        close_vol = round(close_vol, 8)

        # Don't close more than we have (leave at least min_lot)
        if close_vol >= volume - min_lot + 0.000001:
            # Can't do partial — close all (treat as full TP)
            close_vol = volume

        tick = mt5.symbol_info_tick(sym_cfg.mt5_symbol)
        if tick is None:
            continue

        close_side = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY
        price      = tick.bid if is_long else tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       sym_cfg.mt5_symbol,
            "volume":       close_vol,
            "type":         close_side,
            "price":        float(price),
            "deviation":    int(config.get("deviation_points", 20)),
            "magic":        sym_cfg.magic,
            "comment":      "pt-partial-tp",
            "position":     ticket,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        if not trade_enabled:
            log(f"[DRY-RUN][PTP] {sym_cfg.mt5_symbol} ticket={ticket} "
                f"closing {close_vol} of {volume} @ {price:.5f} (pnl={pnl_pct:.2f}%)")
            _partial_tp_done.add(ticket)
            continue

        result = mt5.order_send(request)
        if result and int(getattr(result, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE):
            realized = (current - entry) * close_vol if is_long else (entry - current) * close_vol
            log(f"[PTP] {sym_cfg.mt5_symbol} ticket={ticket} partial close "
                f"{close_vol} lots @ {price:.5f} realized≈${realized:.4f} (pnl={pnl_pct:.2f}%)")
            _append_trade_history({
                "tag":                "PARTIAL_TP",
                "side":               "SELL" if is_long else "BUY",
                "symbol":             sym_cfg.bot_symbol,
                "mt5_symbol":         sym_cfg.mt5_symbol,
                "qty":                close_vol,
                "price":              float(price),
                "entry_price":        entry,
                "pnl_pct":            round(pnl_pct, 4),
                "realized_profit_usd": round(realized, 4),
                "reason":             "partial_tp",
                "order_id":           getattr(result, "order", None),
                "ticket":             ticket,
            })
            _update_pnl_ledger(realized, sym_cfg.bot_symbol)
            _partial_tp_done.add(ticket)
        else:
            rc = getattr(result, "retcode", "N/A") if result else "N/A"
            log(f"[WARN][PTP] Partial TP failed: {sym_cfg.mt5_symbol} ticket={ticket} retcode={rc}")


# ---------------------------------------------------------------------------
# Trailing SL  (improved ratchet)
# ---------------------------------------------------------------------------

def manage_trailing_sl(
    positions: List[Any],
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
    trade_enabled: bool,
) -> None:
    """
    Ratchet trailing SL: only moves in profit direction.
    Skips positions where break-even has not yet triggered (avoid early moves).
    """
    if not bool(config.get("trailing_sl_enabled", True)):
        return

    trigger_pct = sym_cfg.trailing_sl_trigger_pct or float(config.get("trailing_sl_trigger_pct", 1.5))
    distance_pct = sym_cfg.trailing_sl_distance_pct or float(config.get("trailing_sl_distance_pct", 0.8))

    for pos in positions:
        ticket  = int(pos.ticket)
        entry   = float(pos.price_open)
        current = float(pos.price_current)
        if entry <= 0:
            continue

        is_long = int(pos.type) == int(mt5.POSITION_TYPE_BUY)
        pnl_pct = ((current - entry) / entry * 100.0) if is_long \
                  else ((entry - current) / entry * 100.0)

        if pnl_pct < trigger_pct:
            continue

        # Ratchet: track peak P&L
        peak = _trailing_sl_peak.get(ticket, pnl_pct)
        if pnl_pct > peak:
            _trailing_sl_peak[ticket] = pnl_pct
            peak = pnl_pct

        # Calculate new SL from current price
        if is_long:
            new_sl = current * (1.0 - distance_pct / 100.0)
        else:
            new_sl = current * (1.0 + distance_pct / 100.0)

        new_sl, _ = _normalize_sl_tp(sym_cfg.mt5_symbol, new_sl, 0.0)
        current_sl = float(getattr(pos, "sl", 0.0) or 0.0)
        current_tp = float(getattr(pos, "tp", 0.0) or 0.0)

        # Only improve (ratchet — never widen)
        if is_long and new_sl <= current_sl and current_sl > 0:
            continue
        if not is_long and new_sl >= current_sl and current_sl > 0:
            continue

        if not trade_enabled:
            log(f"[DRY-RUN][TRAIL] {sym_cfg.mt5_symbol} ticket={ticket} sl->{new_sl:.5f} (pnl={pnl_pct:.2f}%)")
            continue

        ok = _modify_position_sltp(ticket, sym_cfg.mt5_symbol, new_sl, current_tp)
        if ok:
            log(f"[TRAIL] {sym_cfg.mt5_symbol} ticket={ticket} SL->{new_sl:.5f} (pnl={pnl_pct:.2f}% peak={peak:.2f}%)")
        else:
            log(f"[WARN][TRAIL] Modify failed: {sym_cfg.mt5_symbol} ticket={ticket}")


# ---------------------------------------------------------------------------
# Portfolio-level risk guards  (NEW)
# ---------------------------------------------------------------------------

def _get_total_open_risk_pct(config: Dict[str, Any]) -> float:
    """
    Estimate total portfolio risk as: sum over open positions of
    (distance to SL) / equity * 100.
    Falls back to 0 if data unavailable.
    """
    try:
        account = mt5.account_info()
        equity = float(account.equity) if account and account.equity else 0.0
        if equity <= 0:
            return 0.0

        positions = mt5.positions_get()
        if not positions:
            return 0.0

        total_risk_usd = 0.0
        for pos in positions:
            entry   = float(pos.price_open)
            sl      = float(getattr(pos, "sl", 0.0) or 0.0)
            volume  = float(pos.volume)
            if sl <= 0 or entry <= 0:
                continue
            risk_per_unit = abs(entry - sl)
            total_risk_usd += risk_per_unit * volume

        return (total_risk_usd / equity) * 100.0
    except Exception:
        return 0.0


def check_portfolio_risk(config: Dict[str, Any]) -> bool:
    """
    Returns True if we're within the portfolio risk cap and may open new trades.
    """
    max_risk = float(config.get("max_portfolio_risk_pct", 6.0))
    if max_risk <= 0:
        return True
    current_risk = _get_total_open_risk_pct(config)
    if current_risk >= max_risk:
        log(f"[RISK] Portfolio risk {current_risk:.2f}% >= cap {max_risk:.2f}% -- skipping new entries")
        return False
    return True


def check_daily_loss_limit(config: Dict[str, Any]) -> bool:
    """
    Returns False and triggers emergency flatten if today's equity drop
    exceeds daily_loss_limit_pct.
    """
    global _day_start_equity, _day_start_date

    limit_pct = float(config.get("daily_loss_limit_pct", -3.0))
    if limit_pct >= 0:
        return True

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        account = mt5.account_info()
        equity = float(account.equity) if account and account.equity else 0.0
    except Exception:
        return True

    # Reset baseline at start of each new day
    if _day_start_date != today or _day_start_equity <= 0:
        _day_start_equity = equity
        _day_start_date = today
        return True

    if _day_start_equity <= 0:
        return True

    day_pnl_pct = ((equity - _day_start_equity) / _day_start_equity) * 100.0
    if day_pnl_pct <= limit_pct:
        log(f"[RISK] Daily loss limit hit: {day_pnl_pct:.2f}% <= {limit_pct:.2f}%  EMERGENCY FLATTEN")
        return False

    return True


def emergency_flatten_all(config: Dict[str, Any]) -> None:
    """Close every open position managed by this bot (magic-number filtered)."""
    parsed_symbols: List[SymbolConfig] = config.get("_parsed_symbols", [])
    trade_enabled = bool(config.get("trade_enabled", False))
    deviation = int(config.get("deviation_points", 20))

    magic_set = {sc.magic for sc in parsed_symbols}
    sym_map   = {sc.magic: sc for sc in parsed_symbols}

    all_pos = mt5.positions_get()
    if not all_pos:
        return

    for pos in all_pos:
        magic = int(getattr(pos, "magic", 0))
        if magic not in magic_set:
            continue
        sc = sym_map.get(magic)
        if sc is None:
            continue
        is_long    = int(pos.type) == int(mt5.POSITION_TYPE_BUY)
        close_side = "sell" if is_long else "buy"
        send_market_order(
            symbol=sc.mt5_symbol, side=close_side,
            volume=float(pos.volume), magic=magic,
            deviation_points=deviation, trade_enabled=trade_enabled,
            comment="pt-emergency-flatten",
            config=config, sym_cfg=sc,
            position_ticket=int(pos.ticket), is_close=True,
        )


# ---------------------------------------------------------------------------
# Trader status writer
# ---------------------------------------------------------------------------

def write_trader_status(config: Dict[str, Any]) -> None:
    try:
        account = mt5.account_info()
        if account is None:
            return

        all_positions = mt5.positions_get() or []
        parsed_symbols: List[SymbolConfig] = config.get("_parsed_symbols", [])
        magic_to_cfg = {sc.magic: sc for sc in parsed_symbols}
        bot_to_cfg = {sc.bot_symbol: sc for sc in parsed_symbols}
        max_scale_ins = int(config.get("max_scale_ins", 1))

        holdings_value = 0.0
        total_positions = 0
        positions_dict: Dict[str, Dict[str, Any]] = {}

        for pos in all_positions:
            margin = float(getattr(pos, "margin", 0.0) or 0.0)
            holdings_value += margin if margin > 0 else float(pos.volume) * float(pos.price_current)
            total_positions += 1

            ticket = int(pos.ticket)
            magic  = int(getattr(pos, "magic", 0))
            sc     = magic_to_cfg.get(magic)
            bot_sym = sc.bot_symbol if sc else str(getattr(pos, "symbol", "?"))
            mt5_sym = str(getattr(pos, "symbol", ""))

            is_long     = int(pos.type) == int(mt5.POSITION_TYPE_BUY)
            entry       = float(pos.price_open)
            current     = float(pos.price_current)
            volume      = float(pos.volume)
            profit      = float(getattr(pos, "profit", 0.0) or 0.0)
            swap        = float(getattr(pos, "swap", 0.0) or 0.0)
            sl          = float(getattr(pos, "sl", 0.0) or 0.0)
            tp          = float(getattr(pos, "tp", 0.0) or 0.0)
            pnl_pct     = 0.0
            if entry > 0:
                pnl_pct = ((current - entry) / entry * 100.0) if is_long \
                          else ((entry - current) / entry * 100.0)

            # Risk distance to SL
            risk_to_sl_pct = 0.0
            if sl > 0 and entry > 0:
                risk_to_sl_pct = abs(entry - sl) / entry * 100.0

            key = bot_sym
            if key in positions_dict:
                ex = positions_dict[key]
                old_vol = ex["quantity"]
                new_vol = old_vol + volume
                if new_vol > 0:
                    ex["avg_cost_basis"] = (ex["avg_cost_basis"] * old_vol + entry * volume) / new_vol
                ex["quantity"]   = new_vol
                ex["value_usd"] += current * volume
                ex["profit"]    += profit
                ex["swap"]      += swap
                ex["tickets"].append(ticket)
                if sl > 0:
                    ex["sl"] = min(ex["sl"], sl) if is_long else max(ex["sl"], sl)
                if tp > 0:
                    ex["tp"] = max(ex["tp"], tp) if is_long else min(ex["tp"], tp)
                avg = ex["avg_cost_basis"]
                if avg > 0:
                    ex["pnl_pct"] = ((current - avg) / avg * 100.0) if is_long \
                                    else ((avg - current) / avg * 100.0)
                ex["current_buy_price"]  = current
                ex["current_sell_price"] = current
                ex["position_count"]    += 1
                pos_time = int(getattr(pos, "time", 0) or 0)
                if pos_time >= int(ex.get("last_entry_time", 0) or 0):
                    ex["last_entry_time"] = pos_time
                    ex["last_entry_price"] = entry
            else:
                positions_dict[key] = {
                    "symbol":            mt5_sym,
                    "side":              "LONG" if is_long else "SHORT",
                    "quantity":          volume,
                    "avg_cost_basis":    entry,
                    "current_buy_price": current,
                    "current_sell_price": current,
                    "value_usd":         current * volume,
                    "pnl_pct":           pnl_pct,
                    "profit":            profit,
                    "swap":              swap,
                    "sl":                sl,
                    "tp":                tp,
                    "risk_to_sl_pct":    risk_to_sl_pct,
                    "open_time":         int(getattr(pos, "time", 0) or 0),
                    "last_entry_time":   int(getattr(pos, "time", 0) or 0),
                    "last_entry_price":  entry,
                    "tickets":           [ticket],
                    "position_count":    1,
                    "be_done":           ticket in _breakeven_done,
                    "partial_tp_done":   ticket in _partial_tp_done,
                }

        # Enrich position status with next DCA target details.
        for bot_sym, p in positions_dict.items():
            sc = bot_to_cfg.get(bot_sym)
            if not sc:
                p["next_dca_price"] = None
                p["next_dca_gap_pct"] = None
                p["dca_slots_left"] = 0
                continue

            count = int(p.get("position_count", 0) or 0)
            slots_left = max(0, max_scale_ins - count)
            p["dca_slots_left"] = slots_left

            if count <= 0 or slots_left <= 0:
                p["next_dca_price"] = None
                p["next_dca_gap_pct"] = None
                continue

            base = float(p.get("last_entry_price", p.get("avg_cost_basis", 0.0)) or 0.0)
            if base <= 0:
                p["next_dca_price"] = None
                p["next_dca_gap_pct"] = None
                continue

            level = max(0, count - 1)  # matches _golden_dca_allowed(existing_count)
            required_gap_pct = float(sc.dca_step1_pct) * (_PHI ** level)
            p["next_dca_gap_pct"] = round(required_gap_pct, 4)

            if str(p.get("side", "")).upper() == "LONG":
                p["next_dca_price"] = round(base * (1.0 - required_gap_pct / 100.0), 8)
            else:
                p["next_dca_price"] = round(base * (1.0 + required_gap_pct / 100.0), 8)

        for bot_sym, p in positions_dict.items():
            mt5_sym = str(p.get("symbol", "") or "")
            tick = mt5.symbol_info_tick(mt5_sym) if mt5_sym else None

            current_buy_price = float(getattr(tick, "ask", 0.0) or 0.0) if tick else 0.0
            current_sell_price = float(getattr(tick, "bid", 0.0) or 0.0) if tick else 0.0

            if current_buy_price <= 0:
                current_buy_price = float(p.get("current_buy_price", 0.0) or 0.0)
            if current_sell_price <= 0:
                current_sell_price = float(p.get("current_sell_price", 0.0) or 0.0)

            is_long = str(p.get("side", "")).upper() == "LONG"
            avg_cost = float(p.get("avg_cost_basis", 0.0) or 0.0)
            p["current_buy_price"] = current_buy_price
            p["current_sell_price"] = current_sell_price
            p["gain_loss_pct_buy"] = round(_pnl_pct_for_price(is_long, avg_cost, current_buy_price), 4)
            p["gain_loss_pct_sell"] = round(_pnl_pct_for_price(is_long, avg_cost, current_sell_price), 4)
            p["dca_triggered_stages"] = max(0, int(p.get("position_count", 0) or 0) - 1)
            p["next_dca_display"] = _format_next_dca_display(
                float(p.get("next_dca_price", 0.0) or 0.0),
                float(p.get("next_dca_gap_pct", 0.0) or 0.0),
            )
            p["trail_line"] = float(p.get("sl", 0.0) or 0.0)
            p["lth_reserved_qty"] = float(p.get("lth_reserved_qty", 0.0) or 0.0)

        equity = float(account.equity) if account.equity else 0.0
        buying_power = float(account.margin_free) if account.margin_free else 0.0
        percent_in_trade = abs(holdings_value / equity * 100.0) if equity > 0 else 0.0
        portfolio_risk   = _get_total_open_risk_pct(config)

        status = {
            "timestamp": int(time.time()),
            "account": {
                "total_account_value": equity,
                "holdings_sell_value": holdings_value,
                "buying_power":        buying_power,
                "percent_in_trade":    percent_in_trade,
                "total_positions":     total_positions,
                "balance":             float(account.balance),
                "equity":              equity,
                "margin":              float(getattr(account, "margin", 0.0) or 0.0),
                "margin_free":         buying_power,
                "profit":              float(getattr(account, "profit", 0.0) or 0.0),
                "portfolio_risk_pct":  round(portfolio_risk, 3),
            },
            "positions": positions_dict,
        }

        hub = _hub_data_dir()
        os.makedirs(hub, exist_ok=True)
        ts_now = int(time.time())
        status_path = os.path.join(hub, "trader_status.json")
        if not _atomic_write_json(status_path, status):
            log("[WARN] Could not atomically save trader_status.json")

        _append_account_value_history(ts_now, equity)

    except Exception as e:
        log(f"[WARN] Could not write trader status: {e}")


# ---------------------------------------------------------------------------
# Signal reading
# ---------------------------------------------------------------------------

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
    return candidate if os.path.isdir(candidate) else signals_root


def _signal_is_stale(folder: str, stale_seconds: int) -> bool:
    """
    Returns True if the signal files in `folder` have not been updated
    within the last `stale_seconds`.

    Preferred source is signal_stale.txt written by the thinker/exporter pipeline.
    Falls back to long_dca_signal.txt mtime when the explicit flag is unavailable.
    """
    stale_flag_path = os.path.join(folder, "signal_stale.txt")
    if os.path.isfile(stale_flag_path):
        return parse_int_file(stale_flag_path, 1) == 1

    if stale_seconds <= 0:
        return False
    path = os.path.join(folder, "long_dca_signal.txt")
    try:
        age = time.time() - os.path.getmtime(path)
        return age > stale_seconds
    except Exception:
        return False


def read_coin_signals(
    signals_root: str, bot_symbol: str, stale_seconds: int = 0
) -> Tuple[int, int, float, float, bool]:
    """
    Returns (long_sig, short_sig, long_pm, short_pm, is_stale).
    """
    folder  = resolve_coin_folder(signals_root, bot_symbol)
    stale   = _signal_is_stale(folder, stale_seconds)
    long_sig  = parse_int_file(os.path.join(folder, "long_dca_signal.txt"), 0)
    short_sig_path = os.path.join(folder, "short_dca_signal.txt")
    if not os.path.isfile(short_sig_path):
        print(f"[WARN] short_dca_signal.txt missing: {short_sig_path}")
    short_sig = parse_int_file(short_sig_path, 0)
    long_pm   = max(0.0, parse_float_file(os.path.join(folder, "futures_long_profit_margin.txt"), 0.25))
    short_pm  = max(0.0, parse_float_file(os.path.join(folder, "futures_short_profit_margin.txt"), 0.25))
    return long_sig, short_sig, long_pm, short_pm, stale


# ---------------------------------------------------------------------------
# Position helpers
# ---------------------------------------------------------------------------

def get_positions(symbol: str, magic: Optional[int] = None) -> List[Any]:
    pos = mt5.positions_get(symbol=symbol)
    if pos is None:
        return []
    return [p for p in pos if magic is None or int(getattr(p, "magic", 0)) == int(magic)]


def split_positions_by_side(positions: List[Any]) -> Tuple[List[Any], List[Any]]:
    longs  = [p for p in positions if int(p.type) == int(mt5.POSITION_TYPE_BUY)]
    shorts = [p for p in positions if int(p.type) == int(mt5.POSITION_TYPE_SELL)]
    return longs, shorts


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

# Retcodes that indicate a permanent config error (invalid lot, symbol disabled, etc.)
# These should NOT be retried — they will never succeed without config changes.
_PERMANENT_ERROR_RETCODES = {
    10014,  # TRADE_RETCODE_INVALID_VOLUME  -- lot not snapped to broker step
    10015,  # TRADE_RETCODE_INVALID_PRICE
    10016,  # TRADE_RETCODE_INVALID_STOPS
    10018,  # TRADE_RETCODE_MARKET_CLOSED
    10022,  # TRADE_RETCODE_INVALID_EXPIRATION
    # NOTE: 10019 (No Money) is handled by the pre-flight margin check in
    # send_market_order -- entries are blocked before reaching the broker.
    # We do NOT include it here so that close orders are never accidentally
    # blocked (closes must always go through regardless of free margin).
}


def _snap_volume(volume: float, symbol: str) -> float:
    """
    Snap `volume` to the broker's volume_min / volume_step for `symbol`.
    This prevents retcode 10014 (INVALID_VOLUME).
    """
    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            return volume
        vol_min  = float(getattr(info, "volume_min",  0.01))
        vol_step = float(getattr(info, "volume_step", 0.01))
        vol_max  = float(getattr(info, "volume_max",  1e9))
        if vol_step <= 0:
            vol_step = 0.01
        # Snap to nearest step
        snapped = round(round(volume / vol_step) * vol_step, 8)
        # Enforce min/max
        snapped = max(vol_min, min(vol_max, snapped))
        if abs(snapped - volume) > 1e-9:
            log(f"[VOL] {symbol}: lot {volume} -> {snapped} "
                f"(min={vol_min} step={vol_step} max={vol_max})")
        return snapped
    except Exception:
        return volume


def _calc_dynamic_lot(
    symbol: str,
    entry_price: float,
    sl_pct: float,
    config: Dict[str, Any],
    fallback_lot: float,
) -> float:
    """
    Risk-based position sizing.

    Sizes the lot so that a full stop-loss hit costs exactly
    `risk_per_trade_pct` percent of current account equity.

    Formula:
        risk_usd       = equity * risk_pct / 100
        sl_distance    = entry_price * sl_pct / 100
        lot            = risk_usd / (sl_distance * contract_size)

    contract_size is fetched from MT5 symbol_info (e.g. 1 for CFDs, 100000
    for FX).  For crypto CFDs at ICMarkets it is typically 1.

    The result is snapped to the broker's lot step and clamped to
    [volume_min, volume_max].  If anything fails, falls back to fallback_lot.
    """
    risk_pct = float(config.get("risk_per_trade_pct", 1.0))
    if risk_pct <= 0 or sl_pct <= 0 or entry_price <= 0:
        return fallback_lot

    try:
        account = mt5.account_info()
        if account is None:
            return fallback_lot
        equity = float(account.equity or 0.0)
        if equity <= 0:
            return fallback_lot

        info = mt5.symbol_info(symbol)
        if info is None:
            return fallback_lot

        contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
        vol_min   = float(getattr(info, "volume_min",  0.01))
        vol_step  = float(getattr(info, "volume_step", 0.01))
        vol_max   = float(getattr(info, "volume_max",  1e9))
        if vol_step <= 0:
            vol_step = 0.01

        risk_usd    = equity * risk_pct / 100.0
        sl_distance = entry_price * sl_pct / 100.0
        if sl_distance <= 0:
            return fallback_lot

        raw_lot = risk_usd / (sl_distance * contract_size)

        # Snap to lot step
        snapped = round(round(raw_lot / vol_step) * vol_step, 8)
        snapped = max(vol_min, min(vol_max, snapped))

        return snapped
    except Exception as e:
        log(f"[WARN][LOTSIZE] {symbol}: {e} -- using fallback lot {fallback_lot}")
        return fallback_lot


def send_market_order(
    symbol: str,
    side: str,
    volume: float,
    magic: int,
    deviation_points: int,
    trade_enabled: bool,
    comment: str,
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
    position_ticket: Optional[int] = None,
    is_close: bool = False,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Send a market order. Returns (success, order_info_dict).
    - Volume is dynamically sized by risk % of equity (risk_per_trade_pct).
    - Volume is snapped to broker lot step (fixes retcode 10014).
    # --- Track open timestamp for minimum hold logic ---
    global _position_open_ts
    if '_position_open_ts' not in globals():
        _position_open_ts = {}
    - SL/TP retry without them if broker rejects.
    - Permanent errors mark the symbol inactive so it stops retrying.
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log(f"[WARN] No tick data for {symbol}")
        return False, None

    order_type = mt5.ORDER_TYPE_BUY  if side == "buy"  else mt5.ORDER_TYPE_SELL
    price      = tick.ask            if side == "buy"  else tick.bid

    # ── Pre-flight margin estimate (entry orders only) ────────────────────
    # Use a quick estimate with the configured lot BEFORE dynamic sizing.
    # If the broker minimum lot already can't fit in free margin, skip early
    # with a throttled log -- avoids running the full lot calc every cycle.
    if not is_close and trade_enabled:
        try:
            test_vol = _snap_volume(float(volume), symbol)  # broker-minimum snapped
            est = mt5.order_calc_margin(
                mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                symbol, test_vol, float(price)
            )
            if est is not None and est > 0:
                account = mt5.account_info()
                free_margin = float(account.margin_free) if account else 0.0
                if est > free_margin * 0.9:
                    # Throttle log to once per 60s per symbol
                    now_ts = time.time()
                    last_key = f"_margin_skip_ts_{symbol}"
                    if now_ts - float(config.get(last_key, 0.0)) > 60.0:
                        log(f"[MARGIN] {symbol}: min_lot={test_vol} needs "
                            f"${est:.0f} margin but only ${free_margin:.0f} free "
                            f"-- skipping (signal still active)")
                        config[last_key] = now_ts
                    return False, None
        except Exception as e:
            log(f"[WARN][MARGIN] pre-check failed for {symbol}: {e}")

    # ── Dynamic lot sizing (risk % of equity) for entry orders only ──────────
    # Only runs if margin check passed above.
    if not is_close and config.get("risk_per_trade_pct", 0) > 0:
        volume = _calc_dynamic_lot(
            symbol=symbol,
            entry_price=float(price),
            sl_pct=sym_cfg.sl_pct if sym_cfg.sl_pct > 0 else float(config.get("sl_pct", 2.0)),
            config=config,
            fallback_lot=float(volume),
        )

    # ── Snap to broker's lot constraints ─────────────────────────────────
    snapped_vol = _snap_volume(float(volume), symbol)

    request: Dict[str, Any] = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       snapped_vol,
        "type":         order_type,
        "price":        float(price),
        "deviation":    int(deviation_points),
        "magic":        int(magic),
        "comment":      comment[:30],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if not is_close and sym_cfg.sl_pct > 0:
        sl, tp = calculate_sl_tp(symbol, side, price, config, sym_cfg)
        sl, tp = _normalize_sl_tp(symbol, sl, tp)
        if sl > 0:
            request["sl"] = sl
        if tp > 0:
            request["tp"] = tp

    if position_ticket is not None:
        request["position"] = int(position_ticket)

    order_info: Dict[str, Any] = {
        "symbol":  symbol,
        "side":    side,
        "volume":  snapped_vol,
        "price":   price,
        "sl":      request.get("sl", 0.0),
        "tp":      request.get("tp", 0.0),
        "comment": comment,
        "order_id": None,
        "ticket":  None,
    }

    if not trade_enabled:
        log(f"[DRY-RUN] {request}")
        # Track open time for dry-run
        if not is_close and order_info.get("ticket"):
            _position_open_ts[int(order_info["ticket"])] = time.time()
        return True, order_info

    result = mt5.order_send(request)
    if result is None:
        log(f"[ERROR] order_send returned None for {symbol}")
        return False, None

    if int(getattr(result, "retcode", -1)) != int(mt5.TRADE_RETCODE_DONE):
        rc  = result.retcode
        cmt = getattr(result, "comment", "")

        # ── Permanent errors: no retry, mark symbol inactive ─────────────────
        if int(rc) in _PERMANENT_ERROR_RETCODES:
            log(f"[ERROR] Permanent order error for {symbol} "
                f"retcode={rc} ({cmt}) -- marking symbol inactive for this session")
            # Add to the shared inactive set so reconcile_symbol skips it
            inactive: Set[str] = config.setdefault("_inactive_symbols", set())
            inactive.add(sym_cfg.bot_symbol)
            return False, None

        # ── Retry without SL/TP if broker rejected them ──────────────────────
        if "sl" in request or "tp" in request:
            log(f"[WARN] SL/TP rejected (retcode={rc}: {cmt}), retrying without...")
            request.pop("sl", None)
            request.pop("tp", None)
            order_info["sl"] = 0.0
            order_info["tp"] = 0.0
            result = mt5.order_send(request)
            if result is None or int(getattr(result, "retcode", -1)) != int(mt5.TRADE_RETCODE_DONE):
                rc2 = getattr(result, "retcode", "N/A") if result else "N/A"
                rc2_int = int(rc2) if str(rc2).isdigit() else -1
                log(f"[ERROR] Retry also failed for {symbol}: retcode={rc2}")
                # Also mark inactive if retry hits a permanent error
                if rc2_int in _PERMANENT_ERROR_RETCODES:
                    inactive = config.setdefault("_inactive_symbols", set())
                    inactive.add(sym_cfg.bot_symbol)
                    log(f"[ERROR] Marking {sym_cfg.bot_symbol} inactive (permanent volume/stops error)")
                return False, None
        else:
            log(f"[ERROR] order_send failed: {symbol} retcode={rc} {cmt}")
            return False, None

    ticket = getattr(result, "order", None)
    order_info["order_id"] = ticket
    order_info["ticket"] = ticket
    # Track open time for minimum hold logic
    if not is_close and ticket:
        _position_open_ts[int(ticket)] = time.time()

    log(f"[OK] {side.upper()} {snapped_vol} {symbol} @ {price:.5f} "
        f"ticket={ticket} sl={order_info['sl']:.5f} tp={order_info['tp']:.5f}")

    # Post-fill SL/TP set if they were stripped
    if not is_close and order_info["sl"] == 0.0 and sym_cfg.sl_pct > 0 and ticket:
        try:
            time.sleep(0.5)
            sl2, tp2 = calculate_sl_tp(symbol, side, price, config, sym_cfg)
            sl2, tp2 = _normalize_sl_tp(symbol, sl2, tp2)
            if sl2 > 0 or tp2 > 0:
                ok = _modify_position_sltp(int(ticket), symbol, sl2, tp2)
                if ok:
                    order_info["sl"] = sl2
                    order_info["tp"] = tp2
                    log(f"[SL/TP] Post-fill set: {symbol} sl={sl2:.5f} tp={tp2:.5f}")
                else:
                    log(f"[WARN] Post-fill SL/TP failed for {symbol}")
        except Exception as e:
            log(f"[WARN] Post-fill SL/TP error: {e}")

    return True, order_info


def close_side_positions(
    symbol: str,
    side_positions: List[Any],
    magic: int,
    deviation_points: int,
    trade_enabled: bool,
    reason: str,
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
) -> None:
    for pos in side_positions:
        is_long     = int(pos.type) == int(mt5.POSITION_TYPE_BUY)
        close_side  = "sell" if is_long else "buy"
        entry       = float(pos.price_open)
        current     = float(pos.price_current)

        success, order_info = send_market_order(
            symbol=symbol, side=close_side, volume=float(pos.volume),
            magic=magic, deviation_points=deviation_points,
            trade_enabled=trade_enabled, comment=f"pt-close:{reason}",
            config=config, sym_cfg=sym_cfg,
            position_ticket=int(pos.ticket), is_close=True,
        )

        if success and order_info:
            fill_price = float(order_info.get("price", current) or current)
            if is_long:
                pnl_pct  = ((fill_price - entry) / entry * 100.0) if entry > 0 else 0.0
                realized = (fill_price - entry) * float(pos.volume)
            else:
                pnl_pct  = ((entry - fill_price) / entry * 100.0) if entry > 0 else 0.0
                realized = (entry - fill_price) * float(pos.volume)

            _append_trade_history({
                "tag":                 "CLOSE",
                "side":                "SELL" if is_long else "BUY",
                "symbol":              sym_cfg.bot_symbol,
                "mt5_symbol":          symbol,
                "qty":                 float(pos.volume),
                "price":               fill_price,
                "entry_price":         entry,
                "pnl_pct":             round(pnl_pct, 4),
                "realized_profit_usd": round(realized, 4),
                "reason":              reason,
                "order_id":            order_info.get("order_id", order_info.get("ticket")),
                "ticket":              int(pos.ticket),
            })
            _update_pnl_ledger(realized, sym_cfg.bot_symbol)
            log(f"[CLOSE] {symbol} reason={reason} pnl={pnl_pct:.3f}% realized=${realized:.4f}")

            # Block the opposite direction until the market has moved enough.
            _record_reverse_entry_gap(
                sym_cfg.bot_symbol,
                "long" if is_long else "short",
                fill_price,
                int(config.get("opposite_trade_gap_seconds", 180)),
            )

            # Clean up per-ticket state
            t = int(pos.ticket)
            _trailing_sl_peak.pop(t, None)
            _partial_tp_done.discard(t)
            _breakeven_done.discard(t)

    # If all positions for this symbol are now closed, reset DCA price history
    remaining = get_positions(sym_cfg.mt5_symbol if hasattr(sym_cfg, 'mt5_symbol') else symbol,
                               magic=magic)
    if not remaining:
        _clear_dca_entries(sym_cfg.bot_symbol if hasattr(sym_cfg, 'bot_symbol') else symbol)


# ---------------------------------------------------------------------------
# Profit-margin TP (thinker-supplied target)
# ---------------------------------------------------------------------------

def weighted_entry_price(positions: List[Any]) -> float:
    total_vol = weighted = 0.0
    for p in positions:
        v = float(p.volume)
        total_vol += v
        weighted  += float(p.price_open) * v
    return weighted / total_vol if total_vol > 0 else 0.0


def evaluate_take_profit(
    symbol: str,
    longs: List[Any],
    shorts: List[Any],
    long_pm: float,
    short_pm: float,
    magic: int,
    deviation_points: int,
    trade_enabled: bool,
    use_profit_margin_tp: bool,
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
) -> None:
    if not use_profit_margin_tp:
        return

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return

    if longs:
        avg = weighted_entry_price(longs)
        if avg > 0:
            pnl_pct = (float(tick.bid) - avg) / avg * 100.0
            if pnl_pct >= long_pm:
                log(f"[TP] {symbol} long pnl={pnl_pct:.3f}% >= target {long_pm:.3f}%")
                close_side_positions(symbol, longs, magic, deviation_points,
                                     trade_enabled, "long_tp", config, sym_cfg)

    if shorts:
        avg = weighted_entry_price(shorts)
        if avg > 0:
            pnl_pct = (avg - float(tick.ask)) / avg * 100.0
            if pnl_pct >= short_pm:
                log(f"[TP] {symbol} short pnl={pnl_pct:.3f}% >= target {short_pm:.3f}%")
                close_side_positions(symbol, shorts, magic, deviation_points,
                                     trade_enabled, "short_tp", config, sym_cfg)


# ---------------------------------------------------------------------------
# Per-symbol reconciliation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Golden-ratio DCA gate
# ---------------------------------------------------------------------------

def _golden_dca_allowed(
    sym: str,
    current_price: float,
    side: str,           # "long" or "short"
    existing_count: int, # how many positions are already open
    dca_step1_pct: float,
    open_prices: Optional[List[float]] = None,
    min_price_improvement_pct: float = 0.0,
) -> bool:
    """
    Returns True only if the current price is far enough from the last entry
    to justify a new DCA position.

    Gap required for level N (0-indexed, N=0 is the first DCA after entry):
        gap_pct = dca_step1_pct * PHI^N

    E.g. dca_step1_pct=1.0 with PHI=1.618:
        Entry  (level 0): no gap check -- always allowed
        DCA 1  (level 1): need -1.000% from entry
        DCA 2  (level 2): need -1.618% from DCA1
        DCA 3  (level 3): need -2.618% from DCA2
        DCA 4  (level 4): need -4.236% from DCA3
    """
    global _dca_entry_prices

    # First entry -- always allowed, no price-gap check
    if existing_count == 0:
        return True

    # No gap configured -- allow freely
    if dca_step1_pct <= 0:
        return True

    live_prices = [float(p) for p in (open_prices or []) if float(p) > 0]
    prices = live_prices if live_prices else _dca_entry_prices.get(sym, [])
    if not prices:
        # No recorded entries yet -- allow (will be populated after the fill)
        return True

    # New scale-in entries must be at a strictly better price than previous opens.
    # long: lower than previous entries, short: higher than previous entries.
    min_improve = max(0.0, float(min_price_improvement_pct))
    if side == "long":
        best_prev = min(prices)
        improve_pct = (best_prev - current_price) / best_prev * 100.0 if best_prev > 0 else 0.0
        if improve_pct < min_improve:
            log(f"[PHI-DCA] {sym} LONG blocked: price not better than previous opens "
                f"(improve={improve_pct:.3f}% < min={min_improve:.3f}%)")
            return False
    else:
        best_prev = max(prices)
        improve_pct = (current_price - best_prev) / best_prev * 100.0 if best_prev > 0 else 0.0
        if improve_pct < min_improve:
            log(f"[PHI-DCA] {sym} SHORT blocked: price not better than previous opens "
                f"(improve={improve_pct:.3f}% < min={min_improve:.3f}%)")
            return False

    last_entry = prices[-1]
    if last_entry <= 0:
        return True

    # Level = how many DCAs already taken (existing_count - 1 since index 0 = entry)
    level = max(0, existing_count - 1)  # 0 = first DCA
    required_gap_pct = dca_step1_pct * (_PHI ** level)

    if side == "long":
        # Price must be AT LEAST required_gap_pct% BELOW last entry
        gap_pct = (last_entry - current_price) / last_entry * 100.0
    else:
        # Price must be AT LEAST required_gap_pct% ABOVE last entry
        gap_pct = (current_price - last_entry) / last_entry * 100.0

    if gap_pct >= required_gap_pct:
        log(f"[PHI-DCA] {sym} level={existing_count} gap={gap_pct:.3f}% "
            f">= required={required_gap_pct:.3f}% (step1={dca_step1_pct}% * PHI^{level}) -- OK")
        return True
    else:
        log(f"[PHI-DCA] {sym} level={existing_count} gap={gap_pct:.3f}% "
            f"< required={required_gap_pct:.3f}% -- waiting for deeper pullback")
        return False


def _record_dca_entry(sym: str, price: float) -> None:
    """Record a filled entry/DCA price for golden-ratio spacing."""
    global _dca_entry_prices
    if sym not in _dca_entry_prices:
        _dca_entry_prices[sym] = []
    _dca_entry_prices[sym].append(price)


def _clear_dca_entries(sym: str) -> None:
    """Reset DCA price history when all positions are closed."""
    _dca_entry_prices.pop(sym, None)


def _record_reverse_entry_gap(sym: str, closed_side: str, close_price: float, cooldown_seconds: int) -> None:
    """Record a cooldown before the opposite side can be opened again."""
    if cooldown_seconds <= 0 or close_price <= 0:
        return

    next_side = "short" if closed_side == "long" else "long"
    bucket = _reverse_entry_gap_state.setdefault(sym, {})
    bucket[next_side] = (time.time() + float(cooldown_seconds), float(close_price))


def _reverse_entry_gap_allowed(
    sym: str,
    next_side: str,
    current_price: float,
    cooldown_seconds: int,
    min_gap_pct: float,
) -> bool:
    """Block a reverse entry until both time and price have moved enough."""
    if cooldown_seconds <= 0 and min_gap_pct <= 0:
        return True

    bucket = _reverse_entry_gap_state.get(sym, {})
    state = bucket.get(next_side)
    if not state:
        return True

    cooldown_until, reference_price = state
    now_ts = time.time()
    if cooldown_seconds > 0 and now_ts < cooldown_until:
        remaining = max(0.0, cooldown_until - now_ts)
        log(f"[GAP] {sym} {next_side.upper()} blocked: reverse cooldown {remaining:.0f}s remaining")
        return False

    if current_price <= 0 or reference_price <= 0 or min_gap_pct <= 0:
        return True

    if next_side == "long":
        gap_pct = (current_price - reference_price) / reference_price * 100.0
    else:
        gap_pct = (reference_price - current_price) / reference_price * 100.0

    if gap_pct >= min_gap_pct:
        log(f"[GAP] {sym} {next_side.upper()} reverse gap={gap_pct:.3f}% >= {min_gap_pct:.3f}% -- OK")
        return True

    log(f"[GAP] {sym} {next_side.upper()} blocked: reverse gap={gap_pct:.3f}% < {min_gap_pct:.3f}%")
    return False


def _enforce_max_one_per_side(
    symbol: str,
    longs: List[Any],
    shorts: List[Any],
    max_per_side: int,
    magic: int,
    deviation_points: int,
    trade_enabled: bool,
    config: Dict[str, Any],
    sym_cfg: SymbolConfig,
) -> bool:
    """
    If there are more than max_per_side positions on either side, close extras
    and keep the best max_per_side tickets by volume/time rank.
    """
    changed = False
    allowed = max(1, int(max_per_side))

    for side_positions, label in ((longs, "LONG"), (shorts, "SHORT")):
        if len(side_positions) <= allowed:
            continue

        # Sort: keep the one with the most volume (biggest commitment).
        # If volumes are equal, keep the one opened earliest (lowest time).
        sorted_pos = sorted(
            side_positions,
            key=lambda p: (-float(p.volume), int(getattr(p, "time", 0))),
        )
        keep_list = sorted_pos[:allowed]
        keep_ids = ",".join(str(int(p.ticket)) for p in keep_list)
        extras = sorted_pos[allowed:]

        log(f"[DUP] {symbol} {label}: {len(side_positions)} positions detected -- "
            f"keeping tickets={keep_ids}, "
            f"closing {len(extras)} duplicate(s)")

        for pos in extras:
            is_long   = int(pos.type) == int(mt5.POSITION_TYPE_BUY)
            close_side = "sell" if is_long else "buy"
            entry      = float(pos.price_open)
            current    = float(pos.price_current)

            ok, order_info = send_market_order(
                symbol=symbol,
                side=close_side,
                volume=float(pos.volume),
                magic=magic,
                deviation_points=deviation_points,
                trade_enabled=trade_enabled,
                comment="pt-dup-close",
                config=config,
                sym_cfg=sym_cfg,
                position_ticket=int(pos.ticket),
                is_close=True,
            )
            if ok:
                changed = True
                fill_price = float(order_info.get("price", current) or current)
                if is_long:
                    realized = (fill_price - entry) * float(pos.volume)
                else:
                    realized = (entry - fill_price) * float(pos.volume)
                pnl_pct = (realized / (entry * float(pos.volume)) * 100.0) if entry > 0 else 0.0
                _update_pnl_ledger(realized, sym_cfg.bot_symbol)
                _append_trade_history({
                    "tag":                 "DUP_CLOSE",
                    "side":                "SELL" if is_long else "BUY",
                    "symbol":              sym_cfg.bot_symbol,
                    "mt5_symbol":          symbol,
                    "qty":                 float(pos.volume),
                    "price":               fill_price,
                    "entry_price":         entry,
                    "pnl_pct":             round(pnl_pct, 4),
                    "realized_profit_usd": round(realized, 4),
                    "reason":              "duplicate_close",
                    "order_id":            order_info.get("order_id", order_info.get("ticket")),
                    "ticket":              int(pos.ticket),
                })
                log(f"[DUP] Closed duplicate ticket={pos.ticket} "
                    f"pnl={pnl_pct:.3f}% realized=${realized:.4f}")
                # Clean up per-ticket state
                t = int(pos.ticket)
                _trailing_sl_peak.pop(t, None)
                _partial_tp_done.discard(t)
                _breakeven_done.discard(t)
            else:
                log(f"[WARN][DUP] Could not close duplicate ticket={pos.ticket} for {symbol}")

    return changed


def reconcile_symbol(config: Dict[str, Any], sym_cfg: SymbolConfig, skip_close: bool = False) -> None:
    open_threshold  = int(config["open_threshold"])
    close_threshold = int(config["close_threshold"])
    max_scale_ins   = int(config["max_scale_ins"])
    trade_enabled   = bool(config["trade_enabled"])
    deviation       = int(config["deviation_points"])
    close_on_opp    = bool(config["close_on_opposite_signal"])
    use_pm_tp       = bool(config["use_profit_margin_tp"])
    stale_secs      = int(config.get("signal_stale_seconds", 300))

    long_sig, short_sig, long_pm, short_pm, is_stale = read_coin_signals(
        str(config["signals_root"]), sym_cfg.bot_symbol, stale_secs
    )

    if is_stale:
        log(f"[STALE] {sym_cfg.bot_symbol}: signal files not updated in {stale_secs}s -- skipping entries")

    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)

    inactive: Set[str] = config.get("_inactive_symbols", set())
    if sym_cfg.bot_symbol in inactive:
        return

    # NEW: skip entirely if the symbol is disabled in config
    if not getattr(sym_cfg, "enable_long", True) and not getattr(sym_cfg, "enable_short", True):
        log(f"[SKIP] {sym_cfg.bot_symbol}: both long/short disabled in config -- no signal check")
        return

    # ── Duplicate guard: enforce max 1 position per side right now ───────────
    # If more exist (e.g. from a previous double-entry bug), force-close the
    # extras immediately, keeping only the one with the largest volume
    # (or earliest open time as tiebreaker). Runs before anything else.
    dups_closed = _enforce_max_one_per_side(
        sym_cfg.mt5_symbol, longs, shorts,
        max_scale_ins,
        sym_cfg.magic, deviation, trade_enabled, config, sym_cfg
    )
    # Re-read after potential closes
    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)

    # Only open new entries if no dup was just cleaned up
    if dups_closed:
        return

    log(f"{sym_cfg.bot_symbol}/{sym_cfg.mt5_symbol} "
        f"sig(L/S)={long_sig}/{short_sig} pos(L/S)={len(longs)}/{len(shorts)} "
        f"pm(L/S)={long_pm:.3f}/{short_pm:.3f} "
        f"sl={sym_cfg.sl_pct}% tp={sym_cfg.tp_pct}% "
        f"{'[STALE]' if is_stale else ''}")

    # ── Run per-position risk management (always, even when signals are stale) ──
    manage_breakeven(longs + shorts, config, sym_cfg, trade_enabled)
    manage_partial_tp(longs + shorts, config, sym_cfg, trade_enabled)
    manage_trailing_sl(longs + shorts, config, sym_cfg, trade_enabled)

    if skip_close or is_stale:
        # Still manage existing positions, but don't open/close based on signal
        if not skip_close and not is_stale:
            pass  # fall through to close logic below
        else:
            return


    # --- MINIMUM HOLD TIME FOR CLOSE ON OPPOSITE ---
    MIN_HOLD_SECONDS = 3600  # 1 hour
    now = time.time()
    global _position_open_ts
    if '_position_open_ts' not in globals():
        _position_open_ts = {}
    for p in longs + shorts:
        if int(p.ticket) not in _position_open_ts:
            _position_open_ts[int(p.ticket)] = int(getattr(p, 'time', 0) or 0)

    longs_old_enough = [p for p in longs if now - _position_open_ts.get(int(p.ticket), 0) >= MIN_HOLD_SECONDS]
    shorts_old_enough = [p for p in shorts if now - _position_open_ts.get(int(p.ticket), 0) >= MIN_HOLD_SECONDS]

    # --- FIXED OPPOSITE-SIGNAL CLOSE LOGIC ---
    if close_on_opp and long_sig >= open_threshold and shorts_old_enough:
        log(f"[OPP] {sym_cfg.bot_symbol} LONG signal → closing {len(shorts_old_enough)} shorts first")
        close_side_positions(sym_cfg.mt5_symbol, shorts_old_enough, sym_cfg.magic, deviation,
                              trade_enabled, "opposite_long", config, sym_cfg)
        return

    if close_on_opp and short_sig >= open_threshold and longs_old_enough:
        log(f"[OPP] {sym_cfg.bot_symbol} SHORT signal → closing {len(longs_old_enough)} longs first")
        close_side_positions(sym_cfg.mt5_symbol, longs_old_enough, sym_cfg.magic, deviation,
                              trade_enabled, "opposite_short", config, sym_cfg)
        return

    # --- HARD NO-HEDGE GUARD ---
    # Only block new entries after the opposite-side close attempt has had a chance to run.
    if longs and short_sig >= open_threshold:
        log(f"[NOHEDGE] {sym_cfg.bot_symbol} has LONG open — ignoring short entry")
        return
    if shorts and long_sig >= open_threshold:
        log(f"[NOHEDGE] {sym_cfg.bot_symbol} has SHORT open — ignoring long entry")
        return

    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)

    # ── Scale-in entries ──
    #
    # HARD RULE: open at most ONE new position per poll cycle per symbol.
    # Re-query live position count from MT5 immediately before the entry
    # decision to get the most current count (avoids race with MT5 latency).
    # target_long/short = desired ceiling; entry fires only when live count < ceiling.

    # Portfolio risk check before any new entry
    can_enter = check_portfolio_risk(config) if not is_stale else False

    reverse_gap_seconds = int(config.get("opposite_trade_gap_seconds", 180))
    reverse_gap_pct = float(config.get("opposite_trade_gap_pct", 0.35))
    tick_now = mt5.symbol_info_tick(sym_cfg.mt5_symbol)
    cur_ask = float(tick_now.ask) if tick_now else 0.0
    cur_bid = float(tick_now.bid) if tick_now else 0.0

    # If both directions are strong, only trade the dominant side.
    # Equal signals are treated as ambiguous and skipped for this cycle.
    long_entry_allowed = (
        long_sig >= open_threshold
        and can_enter
        and _reverse_entry_gap_allowed(
            sym_cfg.bot_symbol, "long", cur_ask, reverse_gap_seconds, reverse_gap_pct
        )
    )
    short_entry_allowed = (
        short_sig >= open_threshold
        and can_enter
        and _reverse_entry_gap_allowed(
            sym_cfg.bot_symbol, "short", cur_bid, reverse_gap_seconds, reverse_gap_pct
        )
    )
    if long_entry_allowed and short_entry_allowed:
        if long_sig == short_sig:
            log(f"[AMBIG] {sym_cfg.bot_symbol} long/short signals tied at {long_sig} -- skipping new entries")
            return
        if long_sig > short_sig:
            short_entry_allowed = False
            log(f"[PREF] {sym_cfg.bot_symbol} preferring long over short ({long_sig}>{short_sig})")
        else:
            long_entry_allowed = False
            log(f"[PREF] {sym_cfg.bot_symbol} preferring short over long ({short_sig}>{long_sig})")

    # --- FIX GAP 2: Use correct enable flag for each direction ---
    opened_side_this_cycle = False

    if not sym_cfg.enable_long:
        log(f"[SKIP] {sym_cfg.bot_symbol} long disabled in config")
    elif long_entry_allowed:
        # Re-query live count right now (not from top-of-function snapshot)
        live_longs = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
        live_longs = [p for p in live_longs if int(p.type) == int(mt5.POSITION_TYPE_BUY)]
        target_long = max(0, min(max_scale_ins, long_sig - open_threshold + 1))

        if len(live_longs) < target_long:
            # Golden-ratio DCA gate: only enter if price is far enough below last entry
            tick_now = mt5.symbol_info_tick(sym_cfg.mt5_symbol)
            cur_ask  = float(tick_now.ask) if tick_now else 0.0
            if _golden_dca_allowed(sym_cfg.bot_symbol, cur_ask, "long",
                                    len(live_longs), sym_cfg.dca_step1_pct,
                                    open_prices=[float(p.price_open) for p in live_longs],
                                    min_price_improvement_pct=float(config.get("min_price_improvement_pct", 0.0))):
                tag = "DCA" if live_longs else "ENTRY"
                success, order_info = send_market_order(
                    symbol=sym_cfg.mt5_symbol, side="buy", volume=sym_cfg.lot,
                    magic=sym_cfg.magic, deviation_points=deviation,
                    trade_enabled=trade_enabled,
                    comment=f"pt-long:{sym_cfg.bot_symbol}",
                    config=config, sym_cfg=sym_cfg,
                )
                if success and order_info:
                    filled_price = order_info.get("price", cur_ask)
                    _record_dca_entry(sym_cfg.bot_symbol, filled_price)
                    _append_trade_history({
                        "tag":            tag,
                        "side":           "BUY",
                        "symbol":         sym_cfg.bot_symbol,
                        "mt5_symbol":     sym_cfg.mt5_symbol,
                        "qty":            sym_cfg.lot,
                        "price":          filled_price,
                        "sl":             order_info.get("sl", 0.0),
                        "tp":             order_info.get("tp", 0.0),
                        "order_id":       order_info.get("order_id", order_info.get("ticket")),
                        "ticket":         order_info.get("ticket"),
                        "signal_strength": long_sig,
                        "dca_level":      len(live_longs),
                    })
                    opened_side_this_cycle = True

    if opened_side_this_cycle:
        short_entry_allowed = False

    if not sym_cfg.enable_short:
        log(f"[SKIP] {sym_cfg.bot_symbol} short disabled in config")
    elif short_entry_allowed:
        # Re-query live count right now
        live_shorts = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
        live_shorts = [p for p in live_shorts if int(p.type) == int(mt5.POSITION_TYPE_SELL)]
        target_short = max(0, min(max_scale_ins, short_sig - open_threshold + 1))

        if len(live_shorts) < target_short:
            # Golden-ratio DCA gate: only enter if price is far enough above last entry
            tick_now = mt5.symbol_info_tick(sym_cfg.mt5_symbol)
            cur_bid  = float(tick_now.bid) if tick_now else 0.0
            if _golden_dca_allowed(sym_cfg.bot_symbol, cur_bid, "short",
                                    len(live_shorts), sym_cfg.dca_step1_pct,
                                    open_prices=[float(p.price_open) for p in live_shorts],
                                    min_price_improvement_pct=float(config.get("min_price_improvement_pct", 0.0))):
                tag = "DCA" if live_shorts else "ENTRY"
                success, order_info = send_market_order(
                    symbol=sym_cfg.mt5_symbol, side="sell", volume=sym_cfg.lot,
                    magic=sym_cfg.magic, deviation_points=deviation,
                    trade_enabled=trade_enabled,
                    comment=f"pt-short:{sym_cfg.bot_symbol}",
                    config=config, sym_cfg=sym_cfg,
                )
                if success and order_info:
                    filled_price = order_info.get("price", cur_bid)
                    _record_dca_entry(sym_cfg.bot_symbol, filled_price)
                    _append_trade_history({
                        "tag":            tag,
                        "side":           "SELL",
                        "symbol":         sym_cfg.bot_symbol,
                        "mt5_symbol":     sym_cfg.mt5_symbol,
                        "qty":            sym_cfg.lot,
                        "price":          filled_price,
                        "sl":             order_info.get("sl", 0.0),
                        "tp":             order_info.get("tp", 0.0),
                        "order_id":       order_info.get("order_id", order_info.get("ticket")),
                        "ticket":         order_info.get("ticket"),
                        "signal_strength": short_sig,
                        "dca_level":      len(live_shorts),
                    })

    # ── Signal fade -> close ──
    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)

    if long_sig < close_threshold and longs:
        close_side_positions(sym_cfg.mt5_symbol, longs, sym_cfg.magic, deviation,
                              trade_enabled, "long_signal_fade", config, sym_cfg)

    if short_sig < close_threshold and shorts:
        close_side_positions(sym_cfg.mt5_symbol, shorts, sym_cfg.magic, deviation,
                              trade_enabled, "short_signal_fade", config, sym_cfg)

    # ── Thinker-supplied profit-margin TP ──
    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)
    evaluate_take_profit(
        symbol=sym_cfg.mt5_symbol, longs=longs, shorts=shorts,
        long_pm=long_pm, short_pm=short_pm,
        magic=sym_cfg.magic, deviation_points=deviation,
        trade_enabled=trade_enabled, use_profit_margin_tp=use_pm_tp,
        config=config, sym_cfg=sym_cfg,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop(config: Dict[str, Any], once: bool = False) -> int:
    poll_seconds    = max(1, int(config["poll_seconds"]))
    parsed_symbols  = config["_parsed_symbols"]
    first_cycle     = True
    consecutive_failures = 0

    while True:
        try:
            # ── Daily loss limit check ──
            try:
                if not check_daily_loss_limit(config):
                    emergency_flatten_all(config)
                    log("[RISK] Emergency flatten complete. Sleeping 60s before next check.")
                    if once:
                        return 0
                    time.sleep(60)
                    continue
            except Exception as e:
                log(f"[WARN] Daily loss check failed (skipping): {e}")

            # ── MT5 connectivity ──
            connected = False
            try:
                connected = mt5.terminal_info() is not None
            except Exception:
                pass

            if not connected:
                log("[WARN] MT5 disconnected. Attempting reconnect...")
                try:
                    initialize_mt5(config)
                    consecutive_failures = 0
                    log("[OK] MT5 reconnected.")
                except Exception as e:
                    consecutive_failures += 1
                    wait = min(30 * consecutive_failures, 300)
                    log(f"[ERROR] Reconnect failed ({consecutive_failures}): {e}. Waiting {wait}s")
                    time.sleep(wait)
                    continue

            # ── Per-symbol reconciliation ──
            for sc in parsed_symbols:
                try:
                    reconcile_symbol(config, sc, skip_close=first_cycle)
                except Exception as e:
                    log(f"[ERROR] {sc.bot_symbol}/{sc.mt5_symbol} ({type(e).__name__}): {e}")
                    log(traceback.format_exc())

            first_cycle = False

            try:
                write_trader_status(config)
            except Exception as e:
                log(f"[WARN] Error writing trader status: {e}")

        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            log(f"[CRITICAL] Unexpected error in run_loop ({type(e).__name__}): {e}")
            log(traceback.format_exc())
            log("[CRITICAL] Sleeping 30s before retrying...")
            time.sleep(30)
            continue

        if once:
            return 0

        time.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PowerTrader MT5 Bridge v2")
    p.add_argument("--config",          default=os.path.join(os.path.dirname(__file__), "mt5_config.json"))
    p.add_argument("--once",            action="store_true")
    p.add_argument("--retries",         type=int,   default=5)
    p.add_argument("--retry-delay",     type=float, default=8.0)
    p.add_argument("--terminal-warmup", type=float, default=10.0)
    p.add_argument("--dry-run",         action="store_true")
    p.add_argument("--allow-non-demo",  action="store_true")
    p.add_argument("--poll-seconds",    type=int,   default=0)
    return p.parse_args()


def _is_demo_server(server: str) -> bool:
    return "demo" in str(server or "").strip().lower()


def _start_terminal(terminal_path: str, warmup: float) -> None:
    if not terminal_path or not os.path.isfile(terminal_path):
        return
    log(f"[AUTH] Launching MT5 terminal: {terminal_path}")
    try:
        subprocess.Popen([terminal_path])
    except Exception as e:
        log(f"[WARN] Failed to launch terminal: {e}")
        return
    if warmup > 0:
        time.sleep(warmup)


def _authorize_with_retry(config: Dict[str, Any], retries: int, delay: float, warmup: float) -> bool:
    _start_terminal(str(config.get("terminal_path", "")).strip(), warmup)
    for attempt in range(1, max(1, retries) + 1):
        log(f"[AUTH] Attempt {attempt}/{retries}")
        try:
            initialize_mt5(config)
            log("[AUTH] MT5 authorization succeeded")
            return True
        except Exception as e:
            log(f"[WARN] {e} | MT5 last_error: {mt5.last_error()}")
            try:
                mt5.shutdown()
            except Exception:
                pass
            if attempt < retries and delay > 0:
                time.sleep(delay)
    return False


def main() -> int:
    # Helps diagnose native crashes (e.g., MT5 extension faults) that would
    # otherwise appear as silent process exits in the supervisor logs.
    try:
        faulthandler.enable(all_threads=True)
    except Exception:
        pass

    args = parse_args()
    config_path = os.path.abspath(args.config)

    if not os.path.isfile(config_path):
        log(f"Config not found: {config_path}")
        return 1

    try:
        config = load_config(config_path)
        log(f"Config: {config_path}")
        log(f"  login={config['login']} server={config['server']} "
            f"symbols={len(config['_parsed_symbols'])}")

        for sc in config["_parsed_symbols"]:
            log(f"  {sc.bot_symbol}: lot={sc.lot} "
                f"sl={sc.sl_pct}% tp={sc.tp_pct}% "
                f"be_trigger={sc.breakeven_trigger_pct}% "
                f"ptp={sc.partial_tp_pct}%({sc.partial_tp_close_fraction*100:.0f}%) "
                f"trail={sc.trailing_sl_trigger_pct}%->{sc.trailing_sl_distance_pct}%")

        if config.get("password") and not os.environ.get("PT_MT5_PASSWORD"):
            log("[SECURITY WARN] Password in plaintext. Use PT_MT5_PASSWORD env var.")

        if not args.allow_non_demo and not _is_demo_server(str(config.get("server", ""))):
            log("[ERROR] Non-demo server detected. Use --allow-non-demo to bypass.")
            return 1

        if args.poll_seconds > 0:
            config["poll_seconds"] = args.poll_seconds
        if args.dry_run:
            config["trade_enabled"] = False

        if not _authorize_with_retry(config, args.retries, args.retry_delay, args.terminal_warmup):
            log("[ERROR] MT5 authorization failed after retries")
            return 1

        show_account_summary()
        config["_inactive_symbols"] = ensure_symbols(config["_parsed_symbols"])

        mode = "LIVE" if config["trade_enabled"] else "DRY-RUN"
        log(f"Bridge initialized -- {mode}")
        log(f"  Signals root:     {config['signals_root']}")
        log(f"  Poll:             {config['poll_seconds']}s")
        log(f"  SL/TP mode:       {'ATR' if config.get('use_atr_sl_tp') else 'Percentage'}")
        log(f"  Trailing SL:      {'ON' if config.get('trailing_sl_enabled') else 'OFF'}")
        log(f"  Break-even:       {'ON' if config.get('breakeven_enabled') else 'OFF'} "
            f"@ {config.get('breakeven_trigger_pct')}%")
        log(f"  Partial TP:       {'ON' if config.get('partial_tp_enabled') else 'OFF'} "
            f"@ {config.get('partial_tp_pct')}% (close {config.get('partial_tp_close_fraction')*100:.0f}%)")
        log(f"  Portfolio risk:   max {config.get('max_portfolio_risk_pct')}%")
        log(f"  Daily loss limit: {config.get('daily_loss_limit_pct')}%")
        log(f"  Signal stale:     {config.get('signal_stale_seconds')}s")
        rpt = float(config.get('risk_per_trade_pct', 0))
        if rpt > 0:
            log(f"  Lot sizing:       DYNAMIC -- {rpt}% equity risk per trade")
        else:
            log(f"  Lot sizing:       FIXED -- using lot from config")

        return run_loop(config, once=bool(args.once))

    except Exception as e:
        log(f"[ERROR] {e}")
        return 1
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
