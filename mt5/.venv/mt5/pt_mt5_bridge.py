import argparse
import json
import os
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple


try:
    mt5 = __import__("MetaTrader5")
except ImportError:
    os_name = platform.system() or "Unknown"
    if os_name == "Windows":
        print("MetaTrader5 package is not installed in this Python environment.")
        print("Run: pip install -r requirements.txt")
    else:
        print(f"MetaTrader5 package is not available on {os_name}.")
        print("This bridge must run on Windows with MetaTrader 5 installed.")
        print("Tip: run it on a Windows machine/VPS and point it to your config file.")
    sys.exit(1)


@dataclass
class SymbolConfig:
    bot_symbol: str
    mt5_symbol: str
    lot: float
    magic: int
    enable_long: bool
    enable_short: bool


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    print(f"[{now()}] {msg}")


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

    cfg.setdefault("trade_enabled", False)
    cfg.setdefault("poll_seconds", 10)
    cfg.setdefault("deviation_points", 20)
    cfg.setdefault("open_threshold", 3)
    cfg.setdefault("close_threshold", 2)
    cfg.setdefault("max_scale_ins", 5)
    cfg.setdefault("close_on_opposite_signal", True)
    cfg.setdefault("use_profit_margin_tp", True)
    cfg.setdefault("terminal_path", "")

    parsed_symbols: List[SymbolConfig] = []
    base_magic = int(cfg.get("base_magic", 880000))
    for idx, raw in enumerate(cfg["symbols"]):
        if isinstance(raw, str):
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
        elif isinstance(raw, dict):
            bot_symbol = str(raw.get("bot_symbol", "")).strip().upper()
            mt5_symbol = str(raw.get("mt5_symbol", "")).strip()
            if not bot_symbol or not mt5_symbol:
                raise ValueError("Each symbol object must include bot_symbol and mt5_symbol")
            lot = float(raw.get("lot", cfg.get("default_lot", 0.01)))
            enable_long = bool(raw.get("enable_long", True))
            enable_short = bool(raw.get("enable_short", True))
            magic = int(raw.get("magic", base_magic + idx))
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

    # Exact match (case-insensitive) first.
    for s in available:
        if s.upper() == requested_upper:
            return s

    # Common broker convention: keep base symbol and append suffix.
    starts_with = [s for s in available if s.upper().startswith(requested_upper)]
    if starts_with:
        return sorted(starts_with, key=len)[0]

    # Fallback if broker prefixes group text before symbol.
    contains = [s for s in available if requested_upper in s.upper()]
    if contains:
        return sorted(contains, key=len)[0]

    return None


def ensure_symbols(symbols: List[SymbolConfig]) -> Set[str]:
    inactive_symbols: Set[str] = set()
    available = _symbol_candidates()

    for sym_cfg in symbols:
        requested_symbol = sym_cfg.mt5_symbol
        resolved_symbol = _resolve_mt5_symbol(requested_symbol, available)

        if not resolved_symbol:
            log(f"[WARN] Symbol not found in MT5: {requested_symbol} (bot={sym_cfg.bot_symbol})")
            inactive_symbols.add(sym_cfg.bot_symbol)
            continue

        if resolved_symbol != requested_symbol:
            log(f"[MAP] {sym_cfg.bot_symbol}: {requested_symbol} -> {resolved_symbol}")
            sym_cfg.mt5_symbol = resolved_symbol

        info = mt5.symbol_info(sym_cfg.mt5_symbol)
        if info is None:
            log(f"[WARN] Symbol not found in MT5: {sym_cfg.mt5_symbol} (bot={sym_cfg.bot_symbol})")
            inactive_symbols.add(sym_cfg.bot_symbol)
            continue

        if not info.visible:
            if not mt5.symbol_select(sym_cfg.mt5_symbol, True):
                log(f"[WARN] Could not enable symbol: {sym_cfg.mt5_symbol}")
                inactive_symbols.add(sym_cfg.bot_symbol)
                continue

        tick = mt5.symbol_info_tick(sym_cfg.mt5_symbol)
        if tick is None:
            log(f"[WARN] No tick data available for: {sym_cfg.mt5_symbol}")
            continue

        log(f"[OK] {sym_cfg.mt5_symbol} bid={tick.bid} ask={tick.ask} time={tick.time}")

    return inactive_symbols


def show_account_summary() -> None:
    account = mt5.account_info()
    if account is None:
        log("[WARN] Could not fetch account info")
        return

    log("Account summary")
    log(f"  login: {account.login}")
    log(f"  server: {account.server}")
    log(f"  leverage: {account.leverage}")
    log(f"  balance: {account.balance}")
    log(f"  equity: {account.equity}")
    log(f"  margin_free: {account.margin_free}")


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


def read_coin_signals(signals_root: str, bot_symbol: str) -> Tuple[int, int, float, float]:
    folder = resolve_coin_folder(signals_root, bot_symbol)
    long_sig = parse_int_file(os.path.join(folder, "long_dca_signal.txt"), 0)
    short_sig = parse_int_file(os.path.join(folder, "short_dca_signal.txt"), 0)
    long_pm = parse_float_file(os.path.join(folder, "futures_long_profit_margin.txt"), 0.25)
    short_pm = parse_float_file(os.path.join(folder, "futures_short_profit_margin.txt"), 0.25)
    return long_sig, short_sig, max(0.0, long_pm), max(0.0, short_pm)


def get_positions(symbol: str, magic: Optional[int] = None) -> List[Any]:
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return []
    if magic is None:
        return list(positions)
    return [p for p in positions if int(getattr(p, "magic", 0)) == int(magic)]


def split_positions_by_side(positions: List[Any]) -> Tuple[List[Any], List[Any]]:
    longs = [p for p in positions if int(p.type) == int(mt5.POSITION_TYPE_BUY)]
    shorts = [p for p in positions if int(p.type) == int(mt5.POSITION_TYPE_SELL)]
    return longs, shorts


def send_market_order(
    symbol: str,
    side: str,
    volume: float,
    magic: int,
    deviation_points: int,
    trade_enabled: bool,
    comment: str,
    position_ticket: Optional[int] = None,
) -> bool:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log(f"[WARN] No tick data for {symbol}, order skipped")
        return False

    if side == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif side == "sell":
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        raise ValueError(f"Invalid side: {side}")

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": float(price),
        "deviation": int(deviation_points),
        "magic": int(magic),
        "comment": comment[:30],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if position_ticket is not None:
        request["position"] = int(position_ticket)

    if not trade_enabled:
        log(f"[DRY-RUN] {request}")
        return True

    result = mt5.order_send(request)
    if result is None:
        log(f"[ERROR] order_send returned None for {symbol}")
        return False

    if int(getattr(result, "retcode", -1)) != int(mt5.TRADE_RETCODE_DONE):
        log(
            f"[ERROR] order_send failed for {symbol}, retcode={result.retcode}, "
            f"comment={getattr(result, 'comment', '')}"
        )
        return False

    log(
        f"[OK] order executed: symbol={symbol} side={side} volume={volume} "
        f"ticket={getattr(result, 'order', 'n/a')}"
    )
    return True


def close_side_positions(
    symbol: str,
    side_positions: List[Any],
    magic: int,
    deviation_points: int,
    trade_enabled: bool,
    reason: str,
) -> None:
    for p in side_positions:
        if int(p.type) == int(mt5.POSITION_TYPE_BUY):
            close_side = "sell"
        else:
            close_side = "buy"
        send_market_order(
            symbol=symbol,
            side=close_side,
            volume=float(p.volume),
            magic=magic,
            deviation_points=deviation_points,
            trade_enabled=trade_enabled,
            comment=f"pt-close:{reason}",
            position_ticket=int(p.ticket),
        )


def weighted_entry_price(positions: List[Any]) -> float:
    total_vol = 0.0
    weighted = 0.0
    for p in positions:
        v = float(p.volume)
        total_vol += v
        weighted += float(p.price_open) * v
    if total_vol <= 0:
        return 0.0
    return weighted / total_vol


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
) -> None:
    if not use_profit_margin_tp:
        return

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return

    if longs:
        avg_open = weighted_entry_price(longs)
        if avg_open > 0:
            pnl_pct = ((float(tick.bid) - avg_open) / avg_open) * 100.0
            if pnl_pct >= long_pm:
                log(f"[TP] {symbol} long pnl={pnl_pct:.3f}% >= target {long_pm:.3f}%")
                close_side_positions(
                    symbol,
                    longs,
                    magic,
                    deviation_points,
                    trade_enabled,
                    reason="long_tp",
                )

    if shorts:
        avg_open = weighted_entry_price(shorts)
        if avg_open > 0:
            pnl_pct = ((avg_open - float(tick.ask)) / avg_open) * 100.0
            if pnl_pct >= short_pm:
                log(f"[TP] {symbol} short pnl={pnl_pct:.3f}% >= target {short_pm:.3f}%")
                close_side_positions(
                    symbol,
                    shorts,
                    magic,
                    deviation_points,
                    trade_enabled,
                    reason="short_tp",
                )


def reconcile_symbol(config: Dict[str, Any], sym_cfg: SymbolConfig) -> None:
    inactive_symbols: Set[str] = config.get("_inactive_symbols", set())
    if sym_cfg.bot_symbol in inactive_symbols:
        return

    open_threshold = int(config["open_threshold"])
    close_threshold = int(config["close_threshold"])
    max_scale_ins = int(config["max_scale_ins"])
    trade_enabled = bool(config["trade_enabled"])
    deviation_points = int(config["deviation_points"])
    close_on_opposite = bool(config["close_on_opposite_signal"])
    use_profit_margin_tp = bool(config["use_profit_margin_tp"])

    long_sig, short_sig, long_pm, short_pm = read_coin_signals(
        str(config["signals_root"]), sym_cfg.bot_symbol
    )

    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)

    log(
        f"{sym_cfg.bot_symbol}/{sym_cfg.mt5_symbol} "
        f"sig(L/S)={long_sig}/{short_sig} pos(L/S)={len(longs)}/{len(shorts)} "
        f"pm(L/S)={long_pm:.3f}/{short_pm:.3f}"
    )

    # If one side strongly signals, force close the opposite side.
    if close_on_opposite and long_sig >= open_threshold and short_sig < open_threshold and shorts:
        close_side_positions(
            sym_cfg.mt5_symbol,
            shorts,
            sym_cfg.magic,
            deviation_points,
            trade_enabled,
            reason="opposite_long",
        )

    if close_on_opposite and short_sig >= open_threshold and long_sig < open_threshold and longs:
        close_side_positions(
            sym_cfg.mt5_symbol,
            longs,
            sym_cfg.magic,
            deviation_points,
            trade_enabled,
            reason="opposite_short",
        )

    # Refresh positions after possible closes.
    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)

    # Scale-in target count based on neural level: 3->1, 4->2, ... capped by max_scale_ins
    target_long_count = max(0, min(max_scale_ins, long_sig - open_threshold + 1))
    target_short_count = max(0, min(max_scale_ins, short_sig - open_threshold + 1))

    if sym_cfg.enable_long and short_sig < open_threshold:
        while len(longs) < target_long_count:
            if send_market_order(
                symbol=sym_cfg.mt5_symbol,
                side="buy",
                volume=sym_cfg.lot,
                magic=sym_cfg.magic,
                deviation_points=deviation_points,
                trade_enabled=trade_enabled,
                comment=f"pt-long:{sym_cfg.bot_symbol}",
            ):
                longs.append(object())
            else:
                break

    if sym_cfg.enable_short and long_sig < open_threshold:
        while len(shorts) < target_short_count:
            if send_market_order(
                symbol=sym_cfg.mt5_symbol,
                side="sell",
                volume=sym_cfg.lot,
                magic=sym_cfg.magic,
                deviation_points=deviation_points,
                trade_enabled=trade_enabled,
                comment=f"pt-short:{sym_cfg.bot_symbol}",
            ):
                shorts.append(object())
            else:
                break

    # If signal has faded below close threshold, flatten that side.
    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)

    if long_sig < close_threshold and longs:
        close_side_positions(
            sym_cfg.mt5_symbol,
            longs,
            sym_cfg.magic,
            deviation_points,
            trade_enabled,
            reason="long_signal_fade",
        )

    if short_sig < close_threshold and shorts:
        close_side_positions(
            sym_cfg.mt5_symbol,
            shorts,
            sym_cfg.magic,
            deviation_points,
            trade_enabled,
            reason="short_signal_fade",
        )

    # Optional simple PM target close.
    positions = get_positions(sym_cfg.mt5_symbol, magic=sym_cfg.magic)
    longs, shorts = split_positions_by_side(positions)
    evaluate_take_profit(
        symbol=sym_cfg.mt5_symbol,
        longs=longs,
        shorts=shorts,
        long_pm=long_pm,
        short_pm=short_pm,
        magic=sym_cfg.magic,
        deviation_points=deviation_points,
        trade_enabled=trade_enabled,
        use_profit_margin_tp=use_profit_margin_tp,
    )


def run_loop(config: Dict[str, Any], once: bool = False) -> int:
    poll_seconds = max(1, int(config["poll_seconds"]))
    parsed_symbols: List[SymbolConfig] = config["_parsed_symbols"]

    while True:
        for sym_cfg in parsed_symbols:
            try:
                reconcile_symbol(config, sym_cfg)
            except Exception as e:
                log(f"[ERROR] {sym_cfg.bot_symbol}/{sym_cfg.mt5_symbol}: {e}")

        if once:
            return 0

        time.sleep(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PowerTrader MT5 auto bridge"
    )
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "mt5_config.json"),
        help="Path to MT5 JSON config file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one reconciliation cycle and exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = os.path.abspath(args.config)

    if not os.path.isfile(config_path):
        log(f"Config file not found: {config_path}")
        log("Copy mt5_config.example.json to mt5_config.json and fill it out.")
        return 1

    try:
        config = load_config(config_path)
        log(f"Using config: {config_path}")
        log(
            f"Config credentials: login={config['login']} server={config['server']} "
            f"symbols={len(config['_parsed_symbols'])}"
        )
        initialize_mt5(config)
        show_account_summary()
        inactive = ensure_symbols(config["_parsed_symbols"])
        config["_inactive_symbols"] = inactive
        if inactive:
            log(f"[WARN] Inactive symbols: {', '.join(sorted(inactive))}")

        mode = "LIVE" if bool(config["trade_enabled"]) else "DRY-RUN"
        log(f"Bridge initialized in {mode} mode")
        log(f"Signals root: {config['signals_root']}")
        log(f"Polling interval: {config['poll_seconds']}s")

        return run_loop(config, once=bool(args.once))
    except Exception as e:
        log(f"[ERROR] {e}")
        return 1
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
