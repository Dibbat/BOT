from __future__ import annotations
import os
import sys
import json
import time
import math
import queue
import threading
import subprocess
import shutil
import glob
import bisect
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter
from matplotlib.transforms import blended_transform_factory
from datetime import datetime

# ── Premium Dark Palette ─────────────────────────────────────────────────────────────────────────────
DARK_BG       = "#060A0F"    # true deep navy-black
DARK_BG2      = "#0A0F1A"    # slightly lighter
DARK_PANEL    = "#0D1525"    # card/panel background
DARK_PANEL2   = "#111D30"    # raised panel
DARK_BORDER   = "#1C2D45"    # subtle border
DARK_FG       = "#C8D4E8"    # primary text — softer than pure white
DARK_MUTED    = "#5A7294"    # secondary text
DARK_ACCENT   = "#22D47E"    # green accent (profit/long)
DARK_ACCENT2  = "#4D9FFF"    # blue accent (neutral/info)
DARK_SELECT_BG = "#163048"   # selection background
DARK_SELECT_FG = "#22D47E"   # selection text
DARK_SUCCESS  = "#22D47E"    # green
DARK_DANGER   = "#FF4D6A"    # red (loss/short)
DARK_WARNING  = "#F5C842"    # yellow (warning)
DARK_ORANGE   = "#FB923C"    # DCA / partial signals


@dataclass
class _WrapItem:
    w: tk.Widget
    padx: Tuple[int, int] = (0, 0)
    pady: Tuple[int, int] = (0, 0)


class WrapFrame(ttk.Frame):

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._items: List[_WrapItem] = []
        self._reflow_pending = False
        self._in_reflow = False
        self.bind("<Configure>", self._schedule_reflow)

    def add(self, widget: tk.Widget, padx=(0, 0), pady=(0, 0)) -> None:
        self._items.append(_WrapItem(widget, padx=padx, pady=pady))
        self._schedule_reflow()

    def clear(self, destroy_widgets: bool = True) -> None:

        for it in list(self._items):
            try:
                it.w.grid_forget()
            except Exception:
                pass
            if destroy_widgets:
                try:
                    it.w.destroy()
                except Exception:
                    pass
        self._items = []
        self._schedule_reflow()

    def _schedule_reflow(self, event=None) -> None:
        if self._reflow_pending:
            return
        self._reflow_pending = True
        self.after_idle(self._reflow)

    def _reflow(self) -> None:
        if self._in_reflow:
            self._reflow_pending = False
            return

        self._reflow_pending = False
        self._in_reflow = True
        try:
            width = self.winfo_width()
            if width <= 1:
                return
            usable_width = max(1, width - 6)

            for it in self._items:
                it.w.grid_forget()

            row = 0
            col = 0
            x = 0

            for it in self._items:
                reqw = max(it.w.winfo_reqwidth(), it.w.winfo_width())

                needed = 10 + reqw + it.padx[0] + it.padx[1]

                if col > 0 and (x + needed) > usable_width:
                    row += 1
                    col = 0
                    x = 0

                it.w.grid(row=row, column=col, sticky="w", padx=it.padx, pady=it.pady)
                x += needed
                col += 1
        finally:
            self._in_reflow = False


class StatusLED(tk.Canvas):
    """Simple colored circle LED for process status."""

    def __init__(self, parent: tk.Widget, size: int = 12, **kwargs):
        super().__init__(
            parent,
            width=size,
            height=size,
            bg=DARK_BG,
            highlightthickness=0,
            bd=0,
            **kwargs,
        )
        self._dot = self.create_oval(1, 1, size - 1, size - 1, fill=DARK_DANGER, outline="")
        self._blink_on = False
        self._blink_job = None

    def set_state(self, state: str) -> None:
        st = str(state).strip().lower()
        try:
            if self._blink_job:
                self.after_cancel(self._blink_job)
                self._blink_job = None
        except Exception:
            self._blink_job = None

        if st == "starting":
            self._blink("#FFD700")
            return

        color = {
            "running": DARK_SUCCESS,
            "stopped": DARK_DANGER,
        }.get(st, DARK_DANGER)
        try:
            self.itemconfigure(self._dot, fill=color)
        except Exception:
            pass

    def _blink(self, color: str) -> None:
        self._blink_on = not self._blink_on
        try:
            self.itemconfigure(self._dot, fill=(color if self._blink_on else DARK_PANEL))
            self._blink_job = self.after(500, lambda: self._blink(color))
        except Exception:
            self._blink_job = None


class NeuralSignalTile(ttk.Frame):

    def __init__(self, parent: tk.Widget, coin: str, bar_height: int = 52, levels: int = 8, trade_start_level: int = 3):
        super().__init__(parent)
        self.coin = coin

        self._hover_on = False
        self._normal_canvas_bg = DARK_PANEL2
        self._hover_canvas_bg = DARK_PANEL
        self._normal_border = DARK_BORDER
        self._hover_border = DARK_ACCENT2
        self._normal_fg = DARK_FG
        self._hover_fg = DARK_ACCENT2

        self._levels = max(2, int(levels))             
        self._display_levels = self._levels - 1        

        self._bar_h = max(64, int(bar_height))
        self._bar_w = 22
        self._gap = 24
        self._pad = 6

        self._base_fill = DARK_PANEL
        self._long_fill = "#1E7FFF"
        self._short_fill = "#FF8C00"

        self.title_lbl = ttk.Label(self, text=coin)
        self.title_lbl.pack(anchor="center")

        w = (self._pad * 2) + (self._bar_w * 2) + self._gap
        h = (self._pad * 2) + self._bar_h

        self.canvas = tk.Canvas(
            self,
            width=w,
            height=h,
            bg=self._normal_canvas_bg,
            highlightthickness=1,
            highlightbackground=self._normal_border,
        )
        self.canvas.pack(padx=2, pady=(2, 0))

        x0 = self._pad
        x1 = x0 + self._bar_w
        x2 = x1 + self._gap
        x3 = x2 + self._bar_w
        yb = self._pad + self._bar_h

        # Build segmented bars: 7 segments for levels 1..7 (level 0 is "no highlight")
        self._long_segs: List[int] = []
        self._short_segs: List[int] = []

        for seg in range(self._display_levels):
            # seg=0 is bottom segment (level 1), seg=display_levels-1 is top segment (level 7)
            y_top = int(round(yb - ((seg + 1) * self._bar_h / self._display_levels)))
            y_bot = int(round(yb - (seg * self._bar_h / self._display_levels)))

            self._long_segs.append(
                self.canvas.create_rectangle(
                    x0, y_top, x1, y_bot,
                    fill=self._base_fill,
                    outline=DARK_BORDER,
                    width=1,
                )
            )
            self._short_segs.append(
                self.canvas.create_rectangle(
                    x2, y_top, x3, y_bot,
                    fill=self._base_fill,
                    outline=DARK_BORDER,
                    width=1,
                )
            )

        # Trade-start marker line (boundary before the trade-start level).
        # Example: trade_start_level=3 => line after 2nd block (between 2 and 3).
        self._trade_line_geom = (x0, x1, x2, x3, yb)
        self._trade_line_long = self.canvas.create_line(x0, yb, x1, yb, fill=DARK_FG, width=2)
        self._trade_line_short = self.canvas.create_line(x2, yb, x3, yb, fill=DARK_FG, width=2)
        self._trade_start_level = 3
        self.set_trade_start_level(trade_start_level)


        self.value_lbl = ttk.Label(self, text="L:0 S:0")
        self.value_lbl.pack(anchor="center", pady=(1, 0))

        self.set_values(0, 0)

    def set_hover(self, on: bool) -> None:
        """Visually highlight the tile on hover (like a button hover state)."""
        if bool(on) == bool(self._hover_on):
            return
        self._hover_on = bool(on)

        try:
            if self._hover_on:
                self.canvas.configure(
                    bg=self._hover_canvas_bg,
                    highlightbackground=self._hover_border,
                    highlightthickness=2,
                )
                self.title_lbl.configure(foreground=self._hover_fg)
                self.value_lbl.configure(foreground=self._hover_fg)
            else:
                self.canvas.configure(
                    bg=self._normal_canvas_bg,
                    highlightbackground=self._normal_border,
                    highlightthickness=1,
                )
                self.title_lbl.configure(foreground=self._normal_fg)
                self.value_lbl.configure(foreground=self._normal_fg)
        except Exception:
            pass

    def set_trade_start_level(self, level: Any) -> None:
        """Move the marker line to the boundary before the chosen start level."""
        self._trade_start_level = self._clamp_trade_start_level(level)
        self._update_trade_lines()

    def _clamp_trade_start_level(self, value: Any) -> int:
        try:
            v = int(float(value))
        except Exception:
            v = 3
        # Trade starts at levels 1..display_levels (usually 1..7)
        return max(1, min(v, self._display_levels))

    def _update_trade_lines(self) -> None:
        try:
            x0, x1, x2, x3, yb = self._trade_line_geom
        except Exception:
            return

        k = max(0, min(int(self._trade_start_level) - 1, self._display_levels))
        y = int(round(yb - (k * self._bar_h / self._display_levels)))

        try:
            self.canvas.coords(self._trade_line_long, x0, y, x1, y)
            self.canvas.coords(self._trade_line_short, x2, y, x3, y)
        except Exception:
            pass



    def _clamp_level(self, value: Any) -> int:
        try:
            v = int(float(value))
        except Exception:
            v = 0
        return max(0, min(v, self._levels - 1))  # logical clamp: 0..7

    def _set_level(self, seg_ids: List[int], level: int, active_fill: str) -> None:
        # Reset all segments to base
        for rid in seg_ids:
            self.canvas.itemconfigure(rid, fill=self._base_fill)

        # Level 0 -> show nothing (no highlight)
        if level <= 0:
            return

        # Level 1..7 -> fill from bottom up through the current level
        idx = level - 1  # level 1 maps to seg index 0
        if idx < 0:
            return
        if idx >= len(seg_ids):
            idx = len(seg_ids) - 1

        for i in range(idx + 1):
            alpha = 0.4 + 0.6 * (i / max(len(seg_ids) - 1, 1))
            if active_fill == self._long_fill:
                r, g, b = int(30 * alpha), int(127 * alpha), int(255 * alpha)
            else:
                r, g, b = int(255 * alpha), int(140 * alpha), 0
            fill = f"#{r:02x}{g:02x}{b:02x}"
            self.canvas.itemconfigure(seg_ids[i], fill=fill)


    def set_values(self, long_sig: Any, short_sig: Any) -> None:
        ls = self._clamp_level(long_sig)
        ss = self._clamp_level(short_sig)

        self.value_lbl.config(text=f"L:{ls} S:{ss}")
        self._set_level(self._long_segs, ls, self._long_fill)
        self._set_level(self._short_segs, ss, self._short_fill)









# -----------------------------
# Settings / Paths
# -----------------------------

DEFAULT_SETTINGS = {
    "main_neural_dir": ".",  # MT5-only: keep BTC/ETH/XRP/BNB/DOGE folders under mt5/
    "coins": ["BTC", "ETH", "XRP", "BNB", "DOGE"],
    "trade_start_level": 3,  # trade starts when long signal >= this level (1..7)
    "start_allocation_pct": 0.005,  # % of total account value for initial entry (min $0.50 per coin)
    "dca_multiplier": 2.0,  # DCA buy size = current value * this (2.0 => total scales ~3x per DCA)
    "dca_levels": [-2.5, -5.0, -10.0, -20.0, -30.0, -40.0, -50.0],  # Hard DCA triggers (percent PnL)
    "max_dca_buys_per_24h": 2,  # max DCA buys per coin in rolling 24h window (0 disables DCA buys)

    # --- Trailing Profit Margin settings (used by pt_trader.py; shown in GUI settings) ---
    "pm_start_pct_no_dca": 5.0,
    "pm_start_pct_with_dca": 2.5,
    "trailing_gap_pct": 0.5,

    "default_timeframe": "1hour",
    "trainer_lookback_days": 90,  # Days of history to train on (more = better patterns)
    "timeframes": [
        "1min", "5min", "15min", "30min",
        "1hour", "4hour",
        "1day", "1week"
    ],
    "candles_limit": 120,
    "ui_refresh_seconds": 1.0,
    "chart_refresh_seconds": 10.0,
    "hub_data_dir": "hub_data",  # MT5-only hub_data used by bridge + hub
    "script_neural_runner2": "pt_mt5_thinker.py",  # MT5-local live signal bridge
    "script_neural_trainer": "pt_mt5_trainer.py",  # MT5-local trainer script
    "script_trader": "pt_mt5_bridge.py",  # MT5-local trader/bridge script
    "auto_start_scripts": False,
}











SETTINGS_FILE = "gui_settings.json"


def _safe_read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _safe_write_json(path: str, data: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_trade_history_jsonl(path: str) -> List[dict]:
    """
    Reads hub_data/trade_history.jsonl written by pt_trader.py.
    Returns a list of dicts (only buy/sell rows).
    """
    out: List[dict] = []
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = json.loads(ln)
                        side = str(obj.get("side", "")).lower().strip()
                        if side not in ("buy", "sell"):
                            continue
                        out.append(obj)
                    except Exception:
                        continue
    except Exception:
        pass
    return out


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _mt5_local_script_name(value: str, default_name: str) -> str:
    """Allow only mt5-local script filenames (no directories, no absolute paths)."""
    base = os.path.basename(str(value or "").strip())
    if not base:
        return default_name
    return base



def _fmt_money(x: float) -> str:
    """Format a USD *amount* (account value, position value, etc.) as dollars with 2 decimals."""
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "N/A"


def _fmt_price(x: Any) -> str:
    """
    Format a USD *price/level* with dynamic decimals based on magnitude.
    Examples:
      50234.12   -> $50,234.12
      123.4567   -> $123.457
      1.234567   -> $1.2346
      0.06234567 -> $0.062346
      0.00012345 -> $0.00012345
    """
    try:
        if x is None:
            return "N/A"

        v = float(x)
        if not math.isfinite(v):
            return "N/A"

        sign = "-" if v < 0 else ""
        av = abs(v)

        # Choose decimals by magnitude (more detail for smaller prices).
        if av >= 1000:
            dec = 2
        elif av >= 100:
            dec = 3
        elif av >= 1:
            dec = 4
        elif av >= 0.1:
            dec = 5
        elif av >= 0.01:
            dec = 6
        elif av >= 0.001:
            dec = 7
        else:
            dec = 8

        s = f"{av:,.{dec}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")

        return f"{sign}${s}"
    except Exception:
        return "N/A"


def _fmt_pct(x: float) -> str:
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "N/A"


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# -----------------------------
# Neural folder detection
# -----------------------------

def build_coin_folders(main_dir: str, coins: List[str]) -> Dict[str, str]:
    """
    Mirrors your convention:
      BTC uses main_dir directly
      other coins typically have subfolders inside main_dir (auto-detected)

    Returns { "BTC": "...", "ETH": "...", ... }
    """
    out: Dict[str, str] = {}
    main_dir = main_dir or os.getcwd()

    # BTC folder
    out["BTC"] = main_dir

    # Auto-detect subfolders
    if os.path.isdir(main_dir):
        for name in os.listdir(main_dir):
            p = os.path.join(main_dir, name)
            if not os.path.isdir(p):
                continue
            sym = name.upper().strip()
            if sym in coins and sym != "BTC":
                out[sym] = p

    # Fallbacks for missing ones
    for c in coins:
        c = c.upper().strip()
        if c not in out:
            out[c] = os.path.join(main_dir, c)  # best-effort fallback

    return out


def read_price_levels_from_html(path: str) -> List[float]:
    """
    pt_thinker writes a python-list-like string into low_bound_prices.html / high_bound_prices.html.

    Example (commas often remain):
        "43210.1, 43100.0, 42950.5"

    So we normalize separators before parsing.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()

        if not raw:
            return []

        # Normalize common separators that pt_thinker can leave behind
        raw = (
            raw.replace(",", " ")
               .replace("[", " ")
               .replace("]", " ")
               .replace("'", " ")
        )

        vals: List[float] = []
        for tok in raw.split():
            try:
                v = float(tok)

                # Filter obvious sentinel values used by pt_thinker for "inactive" slots
                if v <= 0:
                    continue
                if v >= 9e15:  # pt_thinker uses 99999999999999999
                    continue


                vals.append(v)
            except Exception:
                pass

        # De-dupe while preserving order (small rounding to avoid float-noise duplicates)
        out: List[float] = []
        seen = set()
        for v in vals:
            key = round(v, 12)
            if key in seen:
                continue
            seen.add(key)
            out.append(v)

        return out
    except Exception:
        return []



def read_int_from_file(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        return int(float(raw))
    except Exception:
        return 0


def read_short_signal(folder: str) -> int:
    txt = os.path.join(folder, "short_dca_signal.txt")
    if os.path.isfile(txt):
        return read_int_from_file(txt)
    else:
        return 0


# -----------------------------
# Candle fetching (KuCoin)
# -----------------------------

class CandleFetcher:
    """Fetch candles from local MT5 terminal for hub chart rendering."""

    _TF_MAP = {
        "1min": "TIMEFRAME_M1",
        "5min": "TIMEFRAME_M5",
        "15min": "TIMEFRAME_M15",
        "30min": "TIMEFRAME_M30",
        "1hour": "TIMEFRAME_H1",
        "4hour": "TIMEFRAME_H4",
        "1day": "TIMEFRAME_D1",
        "1week": "TIMEFRAME_W1",
    }

    _SYMBOL_MAP = {
        "BTC": "BTCUSD",
        "ETH": "ETHUSD",
        "XRP": "XRPUSD",
        "DOGE": "DOGUSD",
        "BNB": "BNBUSD",
    }

    def __init__(self, mt5_config_path: str = ""):
        self._cache: Dict[Tuple[str, str, int], Tuple[float, List[dict]]] = {}
        self._cache_ttl_seconds: float = 15.0
        self._mt5 = None
        self._mt5_ok = False
        self._mt5_config_path = os.path.abspath(mt5_config_path) if mt5_config_path else ""

    def _init_mt5(self) -> bool:
        if self._mt5_ok and self._mt5 is not None:
            return True

        try:
            mt5_mod = __import__("MetaTrader5")
        except Exception:
            return False

        cfg = {}
        try:
            if self._mt5_config_path and os.path.isfile(self._mt5_config_path):
                with open(self._mt5_config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
        except Exception:
            cfg = {}

        terminal = str(cfg.get("terminal_path", "") or "").strip()
        try:
            ok = mt5_mod.initialize(path=terminal) if terminal else mt5_mod.initialize()
        except Exception:
            ok = False

        if not ok:
            return False

        login = cfg.get("login")
        password = cfg.get("password")
        server = cfg.get("server")
        if login and password and server:
            try:
                mt5_mod.login(int(login), password=str(password), server=str(server))
            except Exception:
                pass

        self._mt5 = mt5_mod
        self._mt5_ok = True
        return True

    def _resolve_symbol(self, symbol: str) -> str:
        mt5_sym = self._SYMBOL_MAP.get(str(symbol or "").strip().upper(), f"{symbol}USD")
        mt5 = self._mt5
        if mt5 is None:
            return mt5_sym

        try:
            info = mt5.symbol_info(mt5_sym)
            if info is not None:
                return mt5_sym
        except Exception:
            pass

        try:
            all_syms = [str(s.name) for s in (mt5.symbols_get() or [])]
            matches = [s for s in all_syms if str(symbol).upper() in s.upper()]
            if matches:
                return sorted(matches, key=len)[0]
        except Exception:
            pass

        return mt5_sym

    def get_klines(self, symbol: str, timeframe: str, limit: int = 120) -> List[dict]:
        key = (symbol, timeframe, int(limit))
        cached = self._cache.get(key)
        if cached and (time.time() - cached[0]) < self._cache_ttl_seconds:
            return cached[1]

        if not self._init_mt5():
            return []

        mt5 = self._mt5
        if mt5 is None:
            return []

        try:
            tf_attr = self._TF_MAP.get(str(timeframe or "").strip().lower(), "TIMEFRAME_H1")
            tf_const = getattr(mt5, tf_attr, mt5.TIMEFRAME_H1)

            mt5_symbol = self._resolve_symbol(symbol)
            try:
                mt5.symbol_select(mt5_symbol, True)
            except Exception:
                pass

            rates = mt5.copy_rates_from_pos(mt5_symbol, tf_const, 0, int(limit))
            if rates is None or len(rates) == 0:
                return []

            candles: List[dict] = []
            for r in rates:
                try:
                    tv = float(r["tick_volume"])
                except Exception:
                    try:
                        tv = float(getattr(r, "tick_volume", 0.0))
                    except Exception:
                        tv = 0.0

                candles.append(
                    {
                        "ts": int(r["time"]),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "vol": tv,
                    }
                )

            self._cache[key] = (time.time(), candles)
            return candles
        except Exception:
            self._mt5_ok = False
            return []



# -----------------------------
# Chart widget
# -----------------------------

class CandleChart(ttk.Frame):
    def __init__(
        self,
        parent: tk.Widget,
        fetcher: CandleFetcher,
        coin: str,
        settings_getter,
        trade_history_path: str,
    ):
        super().__init__(parent)
        self.fetcher = fetcher
        self.coin = coin
        self.settings_getter = settings_getter
        self.trade_history_path = trade_history_path

        self.timeframe_var = tk.StringVar(value=self.settings_getter()["default_timeframe"])


        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)

        ttk.Label(top, text=f"{coin} chart").pack(side="left")

        ttk.Label(top, text="Timeframe:").pack(side="left", padx=(12, 4))
        self.tf_combo = ttk.Combobox(
            top,
            textvariable=self.timeframe_var,
            values=self.settings_getter()["timeframes"],
            state="readonly",
            width=10,
        )
        self.tf_combo.pack(side="left")

        # Debounce rapid timeframe changes so redraws don't stack
        self._tf_after_id = None

        def _debounced_tf_change(*_):
            try:
                if self._tf_after_id:
                    self.after_cancel(self._tf_after_id)
            except Exception:
                pass

            def _do():
                # Ask the hub to refresh charts on the next tick (single refresh)
                try:
                    self.event_generate("<<TimeframeChanged>>", when="tail")
                except Exception:
                    pass

            self._tf_after_id = self.after(120, _do)

        self.tf_combo.bind("<<ComboboxSelected>>", _debounced_tf_change)


        self.neural_status_label = ttk.Label(top, text="Neural: N/A")
        self.neural_status_label.pack(side="left", padx=(12, 0))

        self.last_update_label = ttk.Label(top, text="Last: N/A")
        self.last_update_label.pack(side="right")

        # Figure
        # IMPORTANT: keep a stable DPI and resize the figure to the widget's pixel size.
        # On Windows scaling, trying to "sync DPI" via winfo_fpixels("1i") can produce the
        # exact right-side blank/covered region you're seeing.
        self.fig = Figure(figsize=(6.5, 3.5), dpi=100)
        self.fig.patch.set_facecolor(DARK_BG)

        # Reserve bottom space so date+time x tick labels are always visible
        # Also reserve right space so the price labels (Bid/Ask/DCA/Sell) can sit outside the plot.
        # Also reserve a bit of top space so the title never gets clipped.
        self.fig.subplots_adjust(bottom=0.20, right=0.87, top=0.8)

        self.ax = self.fig.add_subplot(111)
        self._apply_dark_chart_style()
        self.ax.set_title(f"{coin}", color=DARK_FG)

        legend_items = [
            ("ASK", "purple"),
            ("BID", "teal"),
            ("AVG", "yellow"),
            ("DCA", "red"),
            ("SELL", "green"),
        ]
        handles = [Line2D([0], [0], color=color, linewidth=1.5, label=label) for label, color in legend_items]
        self.ax.legend(
            handles=handles,
            loc="upper left",
            fontsize=7,
            facecolor=DARK_PANEL2,
            edgecolor=DARK_BORDER,
            labelcolor=DARK_FG,
            framealpha=0.85,
        )

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        canvas_w = self.canvas.get_tk_widget()
        canvas_w.configure(bg=DARK_BG)

        # Remove horizontal padding here so the chart widget truly fills the container.
        canvas_w.pack(fill="both", expand=True, padx=0, pady=(0, 6))

        # Keep the matplotlib figure EXACTLY the same pixel size as the Tk widget.
        # FigureCanvasTkAgg already sizes its backing PhotoImage to e.width/e.height.
        # Multiplying by tk scaling here makes the renderer larger than the PhotoImage,
        # which produces the "blank/covered strip" on the right.
        self._last_canvas_px = (0, 0)
        self._resize_after_id = None

        def _on_canvas_configure(e):
            try:
                w = int(e.width)
                h = int(e.height)
                if w <= 1 or h <= 1:
                    return

                if (w, h) == self._last_canvas_px:
                    return
                self._last_canvas_px = (w, h)

                dpi = float(self.fig.get_dpi() or 100.0)
                self.fig.set_size_inches(w / dpi, h / dpi, forward=True)

                # Debounce redraws during live resize
                if self._resize_after_id:
                    try:
                        self.after_cancel(self._resize_after_id)
                    except Exception:
                        pass
                self._resize_after_id = self.after_idle(self.canvas.draw_idle)
            except Exception:
                pass

        canvas_w.bind("<Configure>", _on_canvas_configure, add="+")







        self._last_refresh = 0.0


    def _apply_dark_chart_style(self) -> None:
        """Apply dark styling (called on init and after every ax.clear())."""
        try:
            self.fig.patch.set_facecolor(DARK_BG)
            self.ax.set_facecolor(DARK_PANEL)
            self.ax.tick_params(colors=DARK_FG)
            for spine in self.ax.spines.values():
                spine.set_color(DARK_BORDER)
            self.ax.grid(True, color=DARK_BORDER, linewidth=0.6, alpha=0.35)
        except Exception:
            pass

    def refresh(
        self,
        coin_folders: Dict[str, str],
        current_buy_price: Optional[float] = None,
        current_sell_price: Optional[float] = None,
        trail_line: Optional[float] = None,
        dca_line_price: Optional[float] = None,
        avg_cost_basis: Optional[float] = None,
    ) -> None:



        cfg = self.settings_getter()

        tf = self.timeframe_var.get().strip()
        limit = int(cfg.get("candles_limit", 120))

        candles = self.fetcher.get_klines(self.coin, tf, limit=limit)

        folder = coin_folders.get(self.coin, "")
        low_path = os.path.join(folder, "low_bound_prices.html")
        high_path = os.path.join(folder, "high_bound_prices.html")

        # --- Cached neural reads (per path, by mtime) ---
        if not hasattr(self, "_neural_cache"):
            self._neural_cache = {}  # path -> (mtime, value)

        def _cached(path: str, loader, default):
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                return default
            hit = self._neural_cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1]
            v = loader(path)
            self._neural_cache[path] = (mtime, v)
            return v

        long_levels = _cached(low_path, read_price_levels_from_html, []) if folder else []
        short_levels = _cached(high_path, read_price_levels_from_html, []) if folder else []

        long_sig_path = os.path.join(folder, "long_dca_signal.txt")
        long_sig = _cached(long_sig_path, read_int_from_file, 0) if folder else 0
        short_sig = read_short_signal(folder) if folder else 0

        # --- Avoid full ax.clear() (expensive). Just clear artists. ---
        try:
            self.ax.lines.clear()
            self.ax.patches.clear()
            self.ax.collections.clear()  # scatter dots live here
            self.ax.texts.clear()        # labels/annotations live here
        except Exception:
            # fallback if matplotlib version lacks .clear() on these lists
            self.ax.cla()
            self._apply_dark_chart_style()


        if not candles:
            self.ax.set_title(f"{self.coin} ({tf}) - no candles", color=DARK_FG)
            self.ax.text(
                0.5,
                0.5,
                "No candle data\n(MT5 integration pending)",
                transform=self.ax.transAxes,
                ha="center",
                va="center",
                fontsize=11,
                color=DARK_MUTED,
                style="italic",
            )
            self.canvas.draw_idle()
            return


        # Candlestick drawing (green up / red down) - batch rectangles
        xs = getattr(self, "_xs", None)
        if not xs or len(xs) != len(candles):
            xs = list(range(len(candles)))
            self._xs = xs

        rects = []
        for i, c in enumerate(candles):
            o = float(c["open"])
            cl = float(c["close"])
            h = float(c["high"])
            l = float(c["low"])

            up = cl >= o
            candle_color = "green" if up else "red"

            # wick
            self.ax.plot([i, i], [l, h], linewidth=1, color=candle_color)

            # body
            bottom = min(o, cl)
            height = abs(cl - o)
            if height < 1e-12:
                height = 1e-12

            rects.append(
                Rectangle(
                    (i - 0.35, bottom),
                    0.7,
                    height,
                    facecolor=candle_color,
                    edgecolor=candle_color,
                    linewidth=1,
                    alpha=0.9,
                )
            )

        for r in rects:
            self.ax.add_patch(r)

        # Lock y-limits to candle range so overlay lines can go offscreen without expanding the chart.
        try:
            y_low = min(float(c["low"]) for c in candles)
            y_high = max(float(c["high"]) for c in candles)
            pad = (y_high - y_low) * 0.03
            if not math.isfinite(pad) or pad <= 0:
                pad = max(abs(y_low) * 0.001, 1e-6)
            self.ax.set_ylim(y_low - pad, y_high + pad)
        except Exception:
            pass



        # Overlay Neural levels (blue long, orange short)
        for lv in long_levels:
            try:
                self.ax.axhline(y=float(lv), linewidth=1, color="blue", alpha=0.8)
            except Exception:
                pass

        for lv in short_levels:
            try:
                self.ax.axhline(y=float(lv), linewidth=1, color="orange", alpha=0.8)
            except Exception:
                pass


        # Overlay Trailing PM line (sell) and next DCA line
        try:
            if trail_line is not None and float(trail_line) > 0:
                self.ax.axhline(y=float(trail_line), linewidth=1.5, color="green", alpha=0.95)
        except Exception:
            pass

        try:
            if dca_line_price is not None and float(dca_line_price) > 0:
                self.ax.axhline(y=float(dca_line_price), linewidth=1.5, color="red", alpha=0.95)
        except Exception:
            pass

        # Overlay avg cost basis (yellow)
        try:
            if avg_cost_basis is not None and float(avg_cost_basis) > 0:
                self.ax.axhline(y=float(avg_cost_basis), linewidth=1.5, color="yellow", alpha=0.95)
        except Exception:
            pass

        # Overlay current ask/bid prices
        try:
            if current_buy_price is not None and float(current_buy_price) > 0:
                self.ax.axhline(y=float(current_buy_price), linewidth=1.5, color="purple", alpha=0.95)
        except Exception:
            pass

        try:
            if current_sell_price is not None and float(current_sell_price) > 0:
                self.ax.axhline(y=float(current_sell_price), linewidth=1.5, color="teal", alpha=0.95)
        except Exception:
            pass

        # Right-side price labels (so you can read Bid/Ask/AVG/DCA/Sell at a glance)
        try:
            trans = blended_transform_factory(self.ax.transAxes, self.ax.transData)
            used_y: List[float] = []
            y0, y1 = self.ax.get_ylim()
            y_pad = max((y1 - y0) * 0.012, 1e-9)

            def _label_right(y: Optional[float], tag: str, color: str) -> None:
                if y is None:
                    return
                try:
                    yy = float(y)
                    if (not math.isfinite(yy)) or yy <= 0:
                        return
                except Exception:
                    return

                # Nudge labels apart if levels are very close
                for prev in used_y:
                    if abs(yy - prev) < y_pad:
                        yy = prev + y_pad
                used_y.append(yy)

                self.ax.text(
                    1.01,
                    yy,
                    f"{tag} {_fmt_price(yy)}",
                    transform=trans,
                    ha="left",
                    va="center",
                    fontsize=8,
                    color=color,
                    bbox=dict(
                        facecolor=DARK_BG2,
                        edgecolor=color,
                        boxstyle="round,pad=0.18",
                        alpha=0.85,
                    ),
                    zorder=20,
                    clip_on=False,
                )

            # Map to your terminology: Ask=buy line, Bid=sell line
            _label_right(current_buy_price, "ASK", "purple")
            _label_right(current_sell_price, "BID", "teal")
            _label_right(avg_cost_basis, "AVG", "yellow")
            _label_right(dca_line_price, "DCA", "red")
            _label_right(trail_line, "SELL", "green")

        except Exception:
            pass




        # --- Trade dots (BUY / DCA / SELL) for THIS coin only ---
        try:
            trades = _read_trade_history_jsonl(self.trade_history_path) if self.trade_history_path else []
            if trades:
                candle_ts = [int(c["ts"]) for c in candles]  # oldest->newest
                t_min = float(candle_ts[0])
                t_max = float(candle_ts[-1])

                for tr in trades:
                    sym = str(tr.get("symbol", "")).upper()
                    base = sym.split("-")[0].strip() if sym else ""
                    if base != self.coin.upper().strip():
                        continue

                    side = str(tr.get("side", "")).lower().strip()
                    tag = str(tr.get("tag") or "").upper().strip()

                    if side == "buy":
                        label = "DCA" if tag == "DCA" else "BUY"
                        color = "purple" if tag == "DCA" else "red"
                    elif side == "sell":
                        label = "SELL"
                        color = "green"
                    else:
                        continue

                    tts = tr.get("ts", None)
                    if tts is None:
                        continue
                    try:
                        tts = float(tts)
                    except Exception:
                        continue
                    if tts < t_min or tts > t_max:
                        continue

                    i = bisect.bisect_left(candle_ts, tts)
                    if i <= 0:
                        idx = 0
                    elif i >= len(candle_ts):
                        idx = len(candle_ts) - 1
                    else:
                        idx = i if abs(candle_ts[i] - tts) < abs(tts - candle_ts[i - 1]) else (i - 1)

                    # y = trade price if present, else candle close
                    y = None
                    try:
                        p = tr.get("price", None)
                        if p is not None and float(p) > 0:
                            y = float(p)
                    except Exception:
                        y = None
                    if y is None:
                        try:
                            y = float(candles[idx].get("close", 0.0))
                        except Exception:
                            y = None
                    if y is None:
                        continue

                    x = idx
                    self.ax.scatter([x], [y], s=35, color=color, zorder=6)
                    self.ax.annotate(
                        label,
                        (x, y),
                        textcoords="offset points",
                        xytext=(0, 10),
                        ha="center",
                        fontsize=8,
                        color=DARK_FG,
                        zorder=7,
                    )
        except Exception:
            pass


        self.ax.set_xlim(-0.5, (len(candles) - 0.5) + 0.6)

        self.ax.set_title(f"{self.coin} ({tf})", color=DARK_FG)



        # x tick labels (date + time) - evenly spaced, never overlapping duplicates
        n = len(candles)
        want = 5  # keep it readable even when the window is narrow
        if n <= want:
            idxs = list(range(n))
        else:
            step = (n - 1) / float(want - 1)
            idxs = []
            last = -1
            for j in range(want):
                i = int(round(j * step))
                if i <= last:
                    i = last + 1
                if i >= n:
                    i = n - 1
                idxs.append(i)
                last = i

        tick_x = [xs[i] for i in idxs]
        tick_lbl = [
            time.strftime("%Y-%m-%d\n%H:%M", time.localtime(int(candles[i].get("ts", 0))))
            for i in idxs
        ]

        try:
            self.ax.minorticks_off()
            self.ax.set_xticks(tick_x)
            self.ax.set_xticklabels(tick_lbl)
            self.ax.tick_params(axis="x", labelsize=8)
        except Exception:
            pass


        self.canvas.draw_idle()


        self.neural_status_label.config(text=f"Neural: long={long_sig} short={short_sig} | levels L={len(long_levels)} S={len(short_levels)}")

        # show file update time if possible
        last_ts = None
        try:
            if os.path.isfile(low_path):
                last_ts = os.path.getmtime(low_path)
            elif os.path.isfile(high_path):
                last_ts = os.path.getmtime(high_path)
        except Exception:
            last_ts = None

        if last_ts:
            self.last_update_label.config(text=f"Last: {time.strftime('%H:%M:%S', time.localtime(last_ts))}")
        else:
            self.last_update_label.config(text="Last: N/A")


# -----------------------------
# Account Value chart widget
# -----------------------------

class AccountValueChart(ttk.Frame):
    def __init__(self, parent: tk.Widget, history_path: str, trade_history_path: str, max_points: int = 250):
        super().__init__(parent)
        self.history_path = history_path
        self.trade_history_path = trade_history_path
        # Hard-cap to 250 points max (account value chart only)
        self.max_points = min(int(max_points or 0) or 250, 250)
        self._last_mtime: Optional[float] = None


        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)

        ttk.Label(top, text="Account value").pack(side="left")
        self.last_update_label = ttk.Label(top, text="Last: N/A")
        self.last_update_label.pack(side="right")

        self.fig = Figure(figsize=(6.5, 3.5), dpi=100)
        self.fig.patch.set_facecolor(DARK_BG)

        # Reserve bottom space so date+time x tick labels are always visible
        # Also reserve right space so the price labels (Bid/Ask/DCA/Sell) can sit outside the plot.
        # Also reserve a bit of top space so the title never gets clipped.
        self.fig.subplots_adjust(bottom=0.25, right=0.87, top=0.8)

        self.ax = self.fig.add_subplot(111)
        self._apply_dark_chart_style()
        self.ax.set_title("Account Value", color=DARK_FG)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        canvas_w = self.canvas.get_tk_widget()
        canvas_w.configure(bg=DARK_BG)

        # Remove horizontal padding here so the chart widget truly fills the container.
        canvas_w.pack(fill="both", expand=True, padx=0, pady=(0, 6))

        # Keep the matplotlib figure EXACTLY the same pixel size as the Tk widget.
        # FigureCanvasTkAgg already sizes its backing PhotoImage to e.width/e.height.
        # Multiplying by tk scaling here makes the renderer larger than the PhotoImage,
        # which produces the "blank/covered strip" on the right.
        self._last_canvas_px = (0, 0)
        self._resize_after_id = None

        def _on_canvas_configure(e):
            try:
                w = int(e.width)
                h = int(e.height)
                if w <= 1 or h <= 1:
                    return

                if (w, h) == self._last_canvas_px:
                    return
                self._last_canvas_px = (w, h)

                dpi = float(self.fig.get_dpi() or 100.0)
                self.fig.set_size_inches(w / dpi, h / dpi, forward=True)

                # Debounce redraws during live resize
                if self._resize_after_id:
                    try:
                        self.after_cancel(self._resize_after_id)
                    except Exception:
                        pass
                self._resize_after_id = self.after_idle(self.canvas.draw_idle)
            except Exception:
                pass

        canvas_w.bind("<Configure>", _on_canvas_configure, add="+")








    def _apply_dark_chart_style(self) -> None:
        try:
            self.fig.patch.set_facecolor(DARK_BG)
            self.ax.set_facecolor(DARK_PANEL)
            self.ax.tick_params(colors=DARK_FG)
            for spine in self.ax.spines.values():
                spine.set_color(DARK_BORDER)
            self.ax.grid(True, color=DARK_BORDER, linewidth=0.6, alpha=0.35)
        except Exception:
            pass

    def refresh(self) -> None:
        path = self.history_path

        # mtime cache so we don't redraw if nothing changed (account history OR trade history)
        try:
            m_hist = os.path.getmtime(path)
        except Exception:
            m_hist = None

        try:
            m_trades = os.path.getmtime(self.trade_history_path) if self.trade_history_path else None
        except Exception:
            m_trades = None

        candidates = [m for m in (m_hist, m_trades) if m is not None]
        mtime = max(candidates) if candidates else None

        if mtime is not None and self._last_mtime == mtime:
            return
        self._last_mtime = mtime


        points: List[Tuple[float, float]] = []

        try:
            if os.path.isfile(path):
                # Read the FULL history so the chart shows from the very beginning
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()

                for ln in lines:
                    try:
                        obj = json.loads(ln)
                        ts = obj.get("ts", None)
                        v = obj.get("total_account_value", None)
                        if ts is None or v is None:
                            continue

                        tsf = float(ts)
                        vf = float(v)

                        # Drop obviously invalid points early
                        if (not math.isfinite(tsf)) or (not math.isfinite(vf)) or (vf <= 0.0):
                            continue

                        points.append((tsf, vf))
                    except Exception:
                        continue
        except Exception:
            points = []

        # ---- Clean up history so single-tick bogus dips/spikes don't render ----
        if points:
            # Ensure chronological order
            points.sort(key=lambda x: x[0])

            # De-dupe identical timestamps (keep the latest occurrence)
            dedup: List[Tuple[float, float]] = []
            for tsf, vf in points:
                if dedup and tsf == dedup[-1][0]:
                    dedup[-1] = (tsf, vf)
                else:
                    dedup.append((tsf, vf))
            points = dedup


        # Downsample to <= 250 points by AVERAGING buckets instead of skipping points.
        # IMPORTANT: never average the VERY FIRST or VERY LAST point.
        # - First point should remain the true first historical value.
        # - Last point should remain the true current/final account value (so the title and chart end match account info).
        max_keep = min(max(2, int(self.max_points or 250)), 250)
        n = len(points)

        if n > max_keep:
            first_pt = points[0]
            last_pt = points[-1]

            mid_points = points[1:-1]
            mid_n = len(mid_points)
            keep_mid = max_keep - 2

            if keep_mid <= 0 or mid_n <= 0:
                points = [first_pt, last_pt]
            elif mid_n <= keep_mid:
                points = [first_pt] + mid_points + [last_pt]
            else:
                bucket_size = mid_n / float(keep_mid)
                new_mid: List[Tuple[float, float]] = []

                for i in range(keep_mid):
                    start = int(i * bucket_size)
                    end = int((i + 1) * bucket_size)
                    if end <= start:
                        end = start + 1
                    if start >= mid_n:
                        break
                    if end > mid_n:
                        end = mid_n

                    bucket = mid_points[start:end]
                    if not bucket:
                        continue

                    # Average timestamp and account value within the bucket (MID ONLY)
                    avg_ts = sum(p[0] for p in bucket) / len(bucket)
                    avg_val = sum(p[1] for p in bucket) / len(bucket)
                    new_mid.append((avg_ts, avg_val))

                points = [first_pt] + new_mid + [last_pt]



        # clear artists (fast) / fallback to cla()
        try:
            self.ax.lines.clear()
            self.ax.patches.clear()
            self.ax.collections.clear()  # scatter dots live here
            self.ax.texts.clear()        # labels/annotations live here
        except Exception:
            self.ax.cla()
            self._apply_dark_chart_style()


        if not points:
            self.ax.set_title("Account Value - no data", color=DARK_FG)
            self.last_update_label.config(text="Last: N/A")
            self.canvas.draw_idle()
            return

        xs = list(range(len(points)))
        # Only show cent-level changes (hide sub-cent noise)
        ys = [round(p[1], 2) for p in points]

        self.ax.plot(xs, ys, linewidth=1.5)

        # --- Trade dots (BUY / DCA / SELL) for ALL coins ---
        try:
            trades = _read_trade_history_jsonl(self.trade_history_path) if self.trade_history_path else []
            if trades:
                ts_list = [float(p[0]) for p in points]  # matches xs/ys indices
                t_min = ts_list[0]
                t_max = ts_list[-1]

                for tr in trades:
                    # Determine label/color
                    side = str(tr.get("side", "")).lower().strip()
                    tag = str(tr.get("tag", "")).upper().strip()

                    if side == "buy":
                        action_label = "DCA" if tag == "DCA" else "BUY"
                        color = "purple" if tag == "DCA" else "red"
                    elif side == "sell":
                        action_label = "SELL"
                        color = "green"
                    else:
                        continue

                    # Prefix with coin (so the dot says which coin it is)
                    sym = str(tr.get("symbol", "")).upper().strip()
                    coin_tag = (sym.split("-")[0].split("/")[0].strip() if sym else "") or (sym or "?")
                    label = f"{coin_tag} {action_label}"

                    tts = tr.get("ts")
                    try:
                        tts = float(tts)
                    except Exception:
                        continue
                    if tts < t_min or tts > t_max:
                        continue

                    # nearest account-value point
                    i = bisect.bisect_left(ts_list, tts)
                    if i <= 0:
                        idx = 0
                    elif i >= len(ts_list):
                        idx = len(ts_list) - 1
                    else:
                        idx = i if abs(ts_list[i] - tts) < abs(tts - ts_list[i - 1]) else (i - 1)

                    x = idx
                    y = ys[idx]

                    self.ax.scatter([x], [y], s=30, color=color, zorder=6)
                    self.ax.annotate(
                        label,
                        (x, y),
                        textcoords="offset points",
                        xytext=(0, 10),
                        ha="center",
                        fontsize=8,
                        color=DARK_FG,
                        zorder=7,
                    )

        except Exception:
            pass

        # Force 2 decimals on the y-axis labels (account value chart only)
        try:
            self.ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _pos: f"${y:,.2f}"))
        except Exception:
            pass


        # x labels: show a few timestamps (date + time) - evenly spaced, never overlapping duplicates
        n = len(points)
        want = 5
        if n <= want:
            idxs = list(range(n))
        else:
            step = (n - 1) / float(want - 1)
            idxs = []
            last = -1
            for j in range(want):
                i = int(round(j * step))
                if i <= last:
                    i = last + 1
                if i >= n:
                    i = n - 1
                idxs.append(i)
                last = i

        tick_x = [xs[i] for i in idxs]
        tick_lbl = [time.strftime("%Y-%m-%d\n%H:%M:%S", time.localtime(points[i][0])) for i in idxs]
        try:
            self.ax.minorticks_off()
            self.ax.set_xticks(tick_x)
            self.ax.set_xticklabels(tick_lbl)
            self.ax.tick_params(axis="x", labelsize=8)
        except Exception:
            pass





        self.ax.set_xlim(-0.5, (len(points) - 0.5) + 0.6)

        try:
            self.ax.set_title(f"Account Value ({_fmt_money(ys[-1])})", color=DARK_FG)
        except Exception:
            self.ax.set_title("Account Value", color=DARK_FG)

        try:
            self.last_update_label.config(
                text=f"Last: {time.strftime('%H:%M:%S', time.localtime(points[-1][0]))}"
            )
        except Exception:
            self.last_update_label.config(text="Last: N/A")

        self.canvas.draw_idle()



# -----------------------------
# Hub App
# -----------------------------

@dataclass
class ProcInfo:
    name: str
    path: str
    proc: Optional[subprocess.Popen] = None
    stop_requested: bool = False
    auto_restart: bool = True
    restart_attempts: int = 0
    last_start_ts: float = 0.0
    next_restart_ts: float = 0.0



@dataclass
class LogProc:
    """
    A running process with a live log queue for stdout/stderr lines.
    """
    info: ProcInfo
    log_q: "queue.Queue[str]"
    thread: Optional[threading.Thread] = None
    is_trainer: bool = False
    coin: Optional[str] = None



class PowerTraderHub(tk.Tk):

    def _cycle_trainer_logs(self):
        """Cycle through logs of all running trainers in the trainer log window."""
        running = [c for c, lp in self.trainers.items() if lp.info.proc and lp.info.proc.poll() is None]
        if not running:
            return
        # Get the current coin shown in the trainer log
        current = (self.trainer_coin_var.get() or "").strip().upper()
        try:
            idx = running.index(current)
        except Exception:
            idx = -1
        # Move to next running trainer
        next_idx = (idx + 1) % len(running)
        next_coin = running[next_idx]
        self.trainer_coin_var.set(next_coin)
        # Schedule next cycle if more than one trainer is running
        if len(running) > 1:
            self.after(3000, self._cycle_trainer_logs)

    def __init__(self):
        super().__init__()
        self.title("PowerTrader MT5 - Hub")
        self.geometry("1280x800")

        # Hard minimum window size so the UI can't be shrunk to a point where panes vanish.
        # (Keeps things usable even if someone aggressively resizes.)
        self.minsize(900, 600)

        # Debounce map for panedwindow clamp operations
        self._paned_clamp_after_ids: Dict[str, str] = {}

        # Force one and only one theme: dark mode everywhere.
        self._apply_forced_dark_mode()

        self.settings = self._load_settings()

        self.project_dir = os.path.abspath(os.path.dirname(__file__))

        main_dir = str(self.settings.get("main_neural_dir") or "").strip()
        if main_dir and not os.path.isabs(main_dir):
            main_dir = os.path.abspath(os.path.join(self.project_dir, main_dir))
        if (not main_dir) or (not os.path.isdir(main_dir)):
            main_dir = self.project_dir
        if os.path.commonpath([self.project_dir, os.path.abspath(main_dir)]) != self.project_dir:
            main_dir = self.project_dir
        self.settings["main_neural_dir"] = main_dir


        # hub data dir
        hub_dir = str(self.settings.get("hub_data_dir") or "").strip()
        if not hub_dir:
            hub_dir = os.path.join(self.project_dir, "hub_data")
        if not os.path.isabs(hub_dir):
            hub_dir = os.path.join(self.project_dir, hub_dir)
        hub_dir = os.path.abspath(hub_dir)
        if os.path.commonpath([self.project_dir, hub_dir]) != self.project_dir:
            hub_dir = os.path.join(self.project_dir, "hub_data")
        self.hub_dir = os.path.abspath(hub_dir)
        _ensure_dir(self.hub_dir)

        # file paths written by pt_trader.py (after edits below)
        self.trader_status_path = os.path.join(self.hub_dir, "trader_status.json")
        self.trade_history_path = os.path.join(self.hub_dir, "trade_history.jsonl")
        self.pnl_ledger_path = os.path.join(self.hub_dir, "pnl_ledger.json")
        self.account_value_history_path = os.path.join(self.hub_dir, "account_value_history.jsonl")

        # file written by pt_mt5_thinker.py (runner readiness gate used for Start All)
        self.runner_ready_path = os.path.join(self.hub_dir, "runner_ready.json")


        # internal: when Start All is pressed, we start the runner first and only start the trader once ready
        self._auto_start_trader_pending = False


        # cache latest trader status so charts can overlay buy/sell lines
        self._last_positions: Dict[str, dict] = {}

        # account value chart widget (created in _build_layout)
        self.account_chart = None



        # coin folders (neural outputs)
        self.coins = [c.upper().strip() for c in self.settings["coins"]]

        # On startup (like on Settings-save), create missing alt folders and copy the trainer into them.
        self._ensure_alt_coin_folders_and_trainer_on_startup()

        # Rebuild folder map after potential folder creation
        self.coin_folders = build_coin_folders(self.settings["main_neural_dir"], self.coins)


        # scripts
        self.proc_neural = ProcInfo(
            name="Neural Runner",
            path=os.path.abspath(os.path.join(self.project_dir, self.settings["script_neural_runner2"]))
        )
        self.proc_trader = ProcInfo(
            name="Trader",
            path=os.path.abspath(os.path.join(self.project_dir, self.settings["script_trader"]))
        )

        self.proc_trainer_path = os.path.abspath(os.path.join(self.project_dir, self.settings["script_neural_trainer"]))

        # live log queues
        self.runner_log_q: "queue.Queue[str]" = queue.Queue()
        self.trader_log_q: "queue.Queue[str]" = queue.Queue()

        # trainers: coin -> LogProc
        self.trainers: Dict[str, LogProc] = {}

        mt5_cfg_path = os.path.join(self.project_dir, "mt5_config.json")
        self.fetcher = CandleFetcher(mt5_config_path=mt5_cfg_path)

        self._build_menu()
        self._build_layout()

        # Refresh charts immediately when a timeframe is changed (don't wait for the 10s throttle).
        self.bind_all("<<TimeframeChanged>>", self._on_timeframe_changed)

        self._last_chart_refresh = 0.0

        if bool(self.settings.get("auto_start_scripts", False)):
            self.start_all_scripts()

        self.after(250, self._tick)

        self.protocol("WM_DELETE_WINDOW", self._on_close)


    # ---- forced dark mode ----

    def _apply_forced_dark_mode(self) -> None:
        """Force a single, global, non-optional dark theme."""
        # Prefer a modern UI font available on the host OS.
        try:
            families = set(tkfont.families(self))
        except Exception:
            families = set()

        ui_candidates = ["Segoe UI Variable", "Segoe UI", "Inter", "Noto Sans", "Helvetica", "Arial"]
        mono_candidates = ["Cascadia Code", "JetBrains Mono", "Consolas", "Menlo", "Courier New", "Courier"]

        self._ui_font_family = next((f for f in ui_candidates if f in families), "TkDefaultFont")
        self._mono_font_family = next((f for f in mono_candidates if f in families), "TkFixedFont")

        # Root background (handles the areas behind ttk widgets)
        try:
            self.configure(bg=DARK_BG)
        except Exception:
            pass

        # Defaults for classic Tk widgets (Text/Listbox/Menu) created later
        try:
            self.option_add("*Text.background", DARK_PANEL)
            self.option_add("*Text.foreground", DARK_FG)
            self.option_add("*Text.insertBackground", DARK_FG)
            self.option_add("*Text.selectBackground", DARK_SELECT_BG)
            self.option_add("*Text.selectForeground", DARK_SELECT_FG)

            self.option_add("*Listbox.background", DARK_PANEL)
            self.option_add("*Listbox.foreground", DARK_FG)
            self.option_add("*Listbox.selectBackground", DARK_SELECT_BG)
            self.option_add("*Listbox.selectForeground", DARK_SELECT_FG)
            self.option_add("*Listbox.font", (self._ui_font_family, 10))

            self.option_add("*Menu.background", DARK_BG2)
            self.option_add("*Menu.foreground", DARK_FG)
            self.option_add("*Menu.activeBackground", DARK_SELECT_BG)
            self.option_add("*Menu.activeForeground", DARK_SELECT_FG)

            # Global typography baseline for Tk widgets.
            self.option_add("*Font", (self._ui_font_family, 10))
        except Exception:
            pass

        style = ttk.Style(self)

        # Pick a theme that is actually recolorable (Windows 'vista' theme ignores many color configs)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Base defaults
        try:
            style.configure(".", background=DARK_BG, foreground=DARK_FG, font=(self._ui_font_family, 10))
        except Exception:
            pass

        # Containers / text
        for name in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton"):
            try:
                style.configure(name, background=DARK_BG, foreground=DARK_FG)
            except Exception:
                pass

        try:
            style.configure("TLabelframe", background=DARK_BG, foreground=DARK_FG, bordercolor=DARK_BORDER)
            style.configure("TLabelframe.Label", background=DARK_BG, foreground=DARK_ACCENT)

            style.configure("Card.TLabelframe", background=DARK_BG, foreground=DARK_FG, bordercolor=DARK_BORDER)
            style.configure("Card.TLabelframe.Label", background=DARK_BG, foreground=DARK_ACCENT2, font=(self._ui_font_family, 10, "bold"))

            style.configure("Title.TLabel", background=DARK_BG, foreground=DARK_ACCENT2, font=(self._ui_font_family, 15, "bold"))
            style.configure("Subtle.TLabel", background=DARK_BG, foreground=DARK_MUTED, font=(self._ui_font_family, 9))
        except Exception:
            pass

        try:
            style.configure("TSeparator", background=DARK_BORDER)
        except Exception:
            pass

        # Buttons
        try:
            style.configure(
                "TButton",
                background=DARK_BG2,
                foreground=DARK_FG,
                bordercolor=DARK_BORDER,
                focusthickness=1,
                focuscolor=DARK_ACCENT,
                padding=(12, 7),
            )
            style.map(
                "TButton",
                background=[
                    ("active", "#17324A"),
                    ("pressed", DARK_PANEL),
                    ("disabled", DARK_BG2),
                ],
                foreground=[
                    ("active", DARK_ACCENT2),
                    ("disabled", DARK_MUTED),
                ],
                bordercolor=[
                    ("active", DARK_ACCENT2),
                    ("focus", DARK_ACCENT),
                ],
            )

            style.configure(
                "Primary.TButton",
                background=DARK_ACCENT2,
                foreground=DARK_BG,
                bordercolor=DARK_ACCENT2,
                focusthickness=1,
                focuscolor=DARK_ACCENT,
                padding=(12, 7),
            )
            style.map(
                "Primary.TButton",
                background=[("active", "#20F0FF"), ("pressed", "#00CDE5"), ("disabled", DARK_BG2)],
                foreground=[("disabled", DARK_MUTED)],
                bordercolor=[("focus", DARK_ACCENT), ("active", "#20F0FF")],
            )

            style.configure(
                "Danger.TButton",
                background=DARK_DANGER,
                foreground=DARK_BG,
                bordercolor=DARK_DANGER,
                focusthickness=1,
                focuscolor=DARK_ACCENT,
                padding=(12, 7),
            )
            style.map(
                "Danger.TButton",
                background=[("active", "#FF7A86"), ("pressed", "#E34C58"), ("disabled", DARK_BG2)],
                foreground=[("disabled", DARK_MUTED)],
                bordercolor=[("focus", DARK_ACCENT), ("active", "#FF7A86")],
            )

            style.configure(
                "Neutral.TButton",
                background=DARK_PANEL2,
                foreground=DARK_ACCENT2,
                bordercolor=DARK_ACCENT2,
                focusthickness=1,
                focuscolor=DARK_ACCENT,
                padding=(12, 7),
            )
            style.map(
                "Neutral.TButton",
                background=[("active", DARK_ACCENT2), ("pressed", "#00CDE5"), ("disabled", DARK_BG2)],
                foreground=[("active", DARK_BG), ("disabled", DARK_MUTED)],
                bordercolor=[("focus", DARK_ACCENT), ("active", DARK_ACCENT2)],
            )
        except Exception:
            pass

        # Entries / combos
        try:
            style.configure(
                "TEntry",
                fieldbackground=DARK_PANEL,
                foreground=DARK_FG,
                bordercolor=DARK_BORDER,
                insertcolor=DARK_FG,
            )
        except Exception:
            pass

        try:
            style.configure(
                "TCombobox",
                fieldbackground=DARK_PANEL,
                background=DARK_PANEL,
                foreground=DARK_FG,
                bordercolor=DARK_BORDER,
                arrowcolor=DARK_ACCENT,
            )
            style.map(
                "TCombobox",
                fieldbackground=[
                    ("readonly", DARK_PANEL),
                    ("focus", DARK_PANEL2),
                ],
                foreground=[("readonly", DARK_FG)],
                background=[("readonly", DARK_PANEL)],
            )
        except Exception:
            pass

        # Notebooks
        try:
            style.configure("TNotebook", background=DARK_BG, bordercolor=DARK_BORDER)
            style.configure("TNotebook.Tab", background=DARK_BG2, foreground=DARK_FG, padding=(10, 6))
            style.map(
                "TNotebook.Tab",
                background=[
                    ("selected", DARK_PANEL),
                    ("active", DARK_PANEL2),
                ],
                foreground=[
                    ("selected", DARK_ACCENT),
                    ("active", DARK_ACCENT2),
                ],
            )

            # Charts tabs need to wrap to multiple lines. ttk.Notebook can't do that,
            # so we hide the Notebook's native tabs and render our own wrapping tab bar.
            #
            # IMPORTANT: the layout must exclude Notebook.tab entirely, and on some themes
            # you must keep Notebook.padding for proper sizing; otherwise the tab strip
            # can still render.
            style.configure("HiddenTabs.TNotebook", tabmargins=0)
            style.layout(
                "HiddenTabs.TNotebook",
                [
                    (
                        "Notebook.padding",
                        {
                            "sticky": "nswe",
                            "children": [
                                ("Notebook.client", {"sticky": "nswe"}),
                            ],
                        },
                    )
                ],
            )

            # Wrapping chart-tab buttons (normal + selected)
            style.configure(
                "ChartTab.TButton",
                background=DARK_BG2,
                foreground=DARK_FG,
                bordercolor=DARK_BORDER,
                padding=(10, 6),
            )
            style.map(
                "ChartTab.TButton",
                background=[("active", DARK_PANEL2), ("pressed", DARK_PANEL)],
                foreground=[("active", DARK_ACCENT2)],
                bordercolor=[("active", DARK_ACCENT2), ("focus", DARK_ACCENT)],
            )

            style.configure(
                "ChartTabSelected.TButton",
                background=DARK_PANEL,
                foreground=DARK_ACCENT,
                bordercolor=DARK_ACCENT2,
                padding=(10, 6),
            )
        except Exception:
            pass


        # Treeview (Current Trades table)
        try:
            style.configure(
                "Treeview",
                background=DARK_PANEL,
                fieldbackground=DARK_PANEL,
                foreground=DARK_FG,
                bordercolor=DARK_BORDER,
                lightcolor=DARK_BORDER,
                darkcolor=DARK_BORDER,
                rowheight=24,
            )
            style.map(
                "Treeview",
                background=[("selected", DARK_SELECT_BG)],
                foreground=[("selected", DARK_SELECT_FG)],
            )

            style.configure("Treeview.Heading", background=DARK_BG2, foreground=DARK_ACCENT, relief="flat")
            style.map(
                "Treeview.Heading",
                background=[("active", DARK_PANEL2)],
                foreground=[("active", DARK_ACCENT2)],
            )
        except Exception:
            pass

        # Panedwindows / scrollbars
        try:
            style.configure("TPanedwindow", background=DARK_BG)
        except Exception:
            pass

        for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            try:
                style.configure(
                    sb,
                    background=DARK_BG2,
                    troughcolor=DARK_BG,
                    bordercolor=DARK_BORDER,
                    arrowcolor=DARK_ACCENT,
                )
            except Exception:
                pass

        # ── Progress bar ────────────────────────────────────────────────────────────────────
        try:
            style.configure("TProgressbar",
                background=DARK_ACCENT2,
                troughcolor=DARK_BG2,
                bordercolor=DARK_BORDER,
                lightcolor=DARK_ACCENT2,
                darkcolor=DARK_ACCENT2,
            )
        except Exception:
            pass

        # ── Spinbox ─────────────────────────────────────────────────────────────────────────
        try:
            style.configure("TSpinbox",
                fieldbackground=DARK_PANEL,
                foreground=DARK_FG,
                background=DARK_PANEL2,
                bordercolor=DARK_BORDER,
                arrowcolor=DARK_ACCENT2,
                insertcolor=DARK_FG,
            )
        except Exception:
            pass

        # ── Scale ───────────────────────────────────────────────────────────────────────────
        try:
            style.configure("TScale",
                background=DARK_BG,
                troughcolor=DARK_PANEL2,
                bordercolor=DARK_BORDER,
                slidercolor=DARK_ACCENT2,
            )
        except Exception:
            pass

        # ── Additional label variants ─────────────────────────────────────────────────────
        try:
            style.configure("Success.TLabel",
                background=DARK_BG, foreground=DARK_SUCCESS,
                font=(self._ui_font_family, 10, "bold"))
            style.configure("Danger.TLabel",
                background=DARK_BG, foreground=DARK_DANGER,
                font=(self._ui_font_family, 10, "bold"))
            style.configure("Warning.TLabel",
                background=DARK_BG, foreground=DARK_WARNING,
                font=(self._ui_font_family, 10))
            style.configure("Mono.TLabel",
                background=DARK_BG, foreground=DARK_FG,
                font=("Consolas", 9))
            style.configure("MonoMuted.TLabel",
                background=DARK_BG, foreground=DARK_MUTED,
                font=("Consolas", 9))
            style.configure("KPI.TLabel",
                background=DARK_PANEL, foreground=DARK_FG,
                font=(self._ui_font_family, 18, "bold"))
            style.configure("KPISmall.TLabel",
                background=DARK_PANEL, foreground=DARK_MUTED,
                font=(self._ui_font_family, 8))
        except Exception:
            pass

        # ── Treeview row tags — call after Treeview style ─────────────────────────────────
        try:
            style.configure("GreenRow.Treeview",
                background=DARK_PANEL, foreground=DARK_SUCCESS)
            style.configure("RedRow.Treeview",
                background=DARK_PANEL, foreground=DARK_DANGER)
        except Exception:
            pass

    # ---- settings ----

    def _load_settings(self) -> dict:
        settings_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), SETTINGS_FILE)
        data = _safe_read_json(settings_path)
        if not isinstance(data, dict):
            data = {}

        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        # Migrate older/non-local script defaults to MT5-local filenames.
        runner_base = os.path.basename(str(merged.get("script_neural_runner2", "")).strip())
        trainer_base = os.path.basename(str(merged.get("script_neural_trainer", "")).strip())
        trader_base = os.path.basename(str(merged.get("script_trader", "")).strip())

        if runner_base in {"pt_thinker.py", "pt_mt5_thinker.py", "pt_mt5_signal_exporter.py"}:
            merged["script_neural_runner2"] = "pt_mt5_thinker.py"
        if trainer_base in {"pt_trainer.py", "pt_mt5_trainer.py"}:
            merged["script_neural_trainer"] = "pt_mt5_trainer.py"
        if trader_base in {"pt_trader.py", "pt_mt5_demo_live.py", "pt_mt5_bridge.py"}:
            merged["script_trader"] = "pt_mt5_bridge.py"

        # Enforce mt5-local script filenames only.
        merged["script_neural_runner2"] = _mt5_local_script_name(
            merged.get("script_neural_runner2", ""), "pt_mt5_thinker.py"
        )
        merged["script_neural_trainer"] = _mt5_local_script_name(
            merged.get("script_neural_trainer", ""), "pt_mt5_trainer.py"
        )
        merged["script_trader"] = _mt5_local_script_name(
            merged.get("script_trader", ""), "pt_mt5_bridge.py"
        )
        # normalize
        merged["coins"] = [c.upper().strip() for c in merged.get("coins", [])]

        # Keep timeframe settings aligned with MT5 trainer/exporter support.
        supported_tfs = {"1min", "5min", "15min", "30min", "1hour", "4hour", "1day", "1week"}
        merged["timeframes"] = [tf for tf in (merged.get("timeframes") or []) if str(tf).strip().lower() in supported_tfs]
        if not merged["timeframes"]:
            merged["timeframes"] = list(DEFAULT_SETTINGS["timeframes"])
        if str(merged.get("default_timeframe", "")).strip().lower() not in supported_tfs:
            merged["default_timeframe"] = "1hour"
        return merged

    def _save_settings(self) -> None:
        settings_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), SETTINGS_FILE)
        _safe_write_json(settings_path, self.settings)


    def _settings_getter(self) -> dict:
        return self.settings

    def _ensure_alt_coin_folders_and_trainer_on_startup(self) -> None:
        """
        Startup behavior (mirrors Settings-save behavior):
        - For every alt coin in the coin list that does NOT have its folder yet:
            - create the folder
            - copy neural_trainer.py from the MAIN (BTC) folder into the new folder
        """
        try:
            coins = [str(c).strip().upper() for c in (self.settings.get("coins") or []) if str(c).strip()]
            main_dir = (self.settings.get("main_neural_dir") or self.project_dir or os.getcwd()).strip()

            trainer_name = os.path.basename(str(self.settings.get("script_neural_trainer", "pt_mt5_trainer.py")))

            # Source trainer: MAIN folder (BTC folder)
            src_main_trainer = os.path.join(main_dir, trainer_name)

            # Best-effort fallback if the main folder doesn't have it (keeps behavior robust)
            src_cfg_trainer = str(self.settings.get("script_neural_trainer", trainer_name))
            src_trainer_path = src_main_trainer if os.path.isfile(src_main_trainer) else src_cfg_trainer

            for coin in coins:
                if coin == "BTC":
                    continue  # BTC uses main folder; no per-coin folder needed

                coin_dir = os.path.join(main_dir, coin)

                created = False
                if not os.path.isdir(coin_dir):
                    os.makedirs(coin_dir, exist_ok=True)
                    created = True

                # Only copy into folders created at startup (per your request)
                if created:
                    dst_trainer_path = os.path.join(coin_dir, trainer_name)
                    if (not os.path.isfile(dst_trainer_path)) and os.path.isfile(src_trainer_path):
                        shutil.copy2(src_trainer_path, dst_trainer_path)
        except Exception:
            pass

    # ---- menu / layout ----


    def _build_menu(self) -> None:
        menubar = tk.Menu(
            self,
            bg=DARK_BG2,
            fg=DARK_FG,
            activebackground=DARK_SELECT_BG,
            activeforeground=DARK_SELECT_FG,
            bd=0,
            relief="flat",
        )

        m_scripts = tk.Menu(
            menubar,
            tearoff=0,
            bg=DARK_BG2,
            fg=DARK_FG,
            activebackground=DARK_SELECT_BG,
            activeforeground=DARK_SELECT_FG,
        )
        m_scripts.add_command(label="Start All", command=self.start_all_scripts)
        m_scripts.add_command(label="Stop All", command=self.stop_all_scripts)
        m_scripts.add_separator()
        m_scripts.add_command(label="Start Neural Runner", command=self.start_neural)
        m_scripts.add_command(label="Stop Neural Runner", command=self.stop_neural)
        m_scripts.add_separator()
        m_scripts.add_command(label="Start Trader", command=self.start_trader)
        m_scripts.add_command(label="Stop Trader", command=self.stop_trader)
        menubar.add_cascade(label="Scripts", menu=m_scripts)

        m_settings = tk.Menu(
            menubar,
            tearoff=0,
            bg=DARK_BG2,
            fg=DARK_FG,
            activebackground=DARK_SELECT_BG,
            activeforeground=DARK_SELECT_FG,
        )
        m_settings.add_command(label="Settings...", command=self.open_settings_dialog)
        menubar.add_cascade(label="Settings", menu=m_settings)

        m_file = tk.Menu(
            menubar,
            tearoff=0,
            bg=DARK_BG2,
            fg=DARK_FG,
            activebackground=DARK_SELECT_BG,
            activeforeground=DARK_SELECT_FG,
        )
        m_file.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=m_file)

        self.config(menu=menubar)


    def _build_layout(self) -> None:
        # Top header strip with process LEDs and clock.
        top_header = ttk.Frame(self)
        top_header.pack(fill="x", padx=10, pady=(10, 0))

        ttk.Label(top_header, text="PowerTrader MT5", style="Title.TLabel").pack(side="left")

        status_bar = ttk.Frame(top_header)
        status_bar.pack(side="right")

        self.led_neural = StatusLED(status_bar)
        self.led_neural.pack(side="left", padx=(0, 4), pady=4)
        ttk.Label(status_bar, text="Neural", style="Subtle.TLabel").pack(side="left", padx=(0, 14))

        self.led_trader = StatusLED(status_bar)
        self.led_trader.pack(side="left", padx=(0, 4), pady=4)
        ttk.Label(status_bar, text="Trader", style="Subtle.TLabel").pack(side="left", padx=(0, 14))

        self.lbl_clock = ttk.Label(status_bar, text="--:--:--", style="Subtle.TLabel")
        self.lbl_clock.pack(side="left", padx=(8, 0))

        outer = ttk.Panedwindow(self, orient="horizontal")
        outer.pack(fill="both", expand=True)

        # LEFT + RIGHT panes
        left = ttk.Frame(outer)
        right = ttk.Frame(outer)

        outer.add(left, weight=1)
        outer.add(right, weight=2)

        # Prevent the outer (left/right) panes from being collapsible to 0 width
        try:
            outer.paneconfigure(left, minsize=360)
            outer.paneconfigure(right, minsize=520)
        except Exception:
            pass

        # LEFT: vertical split (Controls, Live Output)
        left_split = ttk.Panedwindow(left, orient="vertical")
        left_split.pack(fill="both", expand=True, padx=8, pady=8)


        # RIGHT: vertical split (Charts on top, Trades+History underneath)
        right_split = ttk.Panedwindow(right, orient="vertical")
        right_split.pack(fill="both", expand=True, padx=8, pady=8)

        # Keep references so we can clamp sash positions later
        self._pw_outer = outer
        self._pw_left_split = left_split
        self._pw_right_split = right_split

        # Clamp panes when the user releases a sash or the window resizes
        outer.bind("<Configure>", lambda e: self._schedule_paned_clamp(self._pw_outer))
        outer.bind("<ButtonRelease-1>", lambda e: (
            setattr(self, "_user_moved_outer", True),
            self._schedule_paned_clamp(self._pw_outer),
        ))

        left_split.bind("<Configure>", lambda e: self._schedule_paned_clamp(self._pw_left_split))
        left_split.bind("<ButtonRelease-1>", lambda e: (
            setattr(self, "_user_moved_left_split", True),
            self._schedule_paned_clamp(self._pw_left_split),
        ))

        right_split.bind("<Configure>", lambda e: self._schedule_paned_clamp(self._pw_right_split))
        right_split.bind("<ButtonRelease-1>", lambda e: (
            setattr(self, "_user_moved_right_split", True),
            self._schedule_paned_clamp(self._pw_right_split),
        ))

        # Set a startup default width that matches the screenshot (so left has room for Neural Levels).
        def _init_outer_sash_once():
            try:
                if getattr(self, "_did_init_outer_sash", False):
                    return

                # If the user already moved it, never override it.
                if getattr(self, "_user_moved_outer", False):
                    self._did_init_outer_sash = True
                    return

                total = outer.winfo_width()
                if total <= 2:
                    self.after(10, _init_outer_sash_once)
                    return

                min_left = 360
                min_right = 520
                desired_left = 470  # ~matches your screenshot
                target = max(min_left, min(total - min_right, desired_left))
                outer.sashpos(0, int(target))

                self._did_init_outer_sash = True
            except Exception:
                pass

        self.after_idle(_init_outer_sash_once)

        # Global safety: on some themes/platforms, the mouse events land on the sash element,
        # not the panedwindow widget, so the widget-level binds won't always fire.
        self.bind_all("<ButtonRelease-1>", lambda e: (
            self._schedule_paned_clamp(getattr(self, "_pw_outer", None)),
            self._schedule_paned_clamp(getattr(self, "_pw_left_split", None)),
            self._schedule_paned_clamp(getattr(self, "_pw_right_split", None)),
            self._schedule_paned_clamp(getattr(self, "_pw_right_bottom_split", None)),
        ))


        # ----------------------------
        # LEFT: 1) Controls / Health (pane)
        # ----------------------------
        top_controls = ttk.LabelFrame(left_split, text="Controls / Health", style="Card.TLabelframe")

        # Layout requirement:
        #   - Buttons (full width) ABOVE
        #   - Dual section BELOW:
        #       LEFT  = Status + Account + Profit
        #       RIGHT = Training
        buttons_bar = ttk.Frame(top_controls)
        buttons_bar.pack(fill="x", expand=False, padx=0, pady=0)

        info_row = ttk.Frame(top_controls)
        info_row.pack(fill="x", expand=False, padx=0, pady=0)

        # LEFT column (status + account/profit)
        controls_left = ttk.Frame(info_row)
        controls_left.pack(side="left", fill="both", expand=True)

        # RIGHT column (training)
        training_section = ttk.LabelFrame(info_row, text="Training", style="Card.TLabelframe")
        training_section.pack(side="right", fill="both", expand=False, padx=6, pady=6)

        training_left = ttk.Frame(training_section)
        training_left.pack(side="left", fill="both", expand=True)

        # Train coin selector (so you can choose what "Train Selected" targets)
        train_row = ttk.Frame(training_left)
        train_row.pack(fill="x", padx=6, pady=(6, 0))

        self.train_coin_var = tk.StringVar(value=(self.coins[0] if self.coins else ""))
        ttk.Label(train_row, text="Train coin:").pack(side="left")
        self.train_coin_combo = ttk.Combobox(
            train_row,
            textvariable=self.train_coin_var,
            values=["All"] + self.coins,
            width=8,
            state="readonly",
        )
        self.train_coin_combo.pack(side="left", padx=(6, 0))

        def _sync_train_coin(*_):
            try:
                val = self.train_coin_var.get()
                # keep the Trainers tab dropdown in sync (if present)
                self.trainer_coin_var.set(val)
            except Exception:
                pass

        self.train_coin_combo.bind("<<ComboboxSelected>>", _sync_train_coin)
        _sync_train_coin()



        # Fixed controls bar (stable layout; no wrapping/reflow on resize)
        # Wrapped in a scrollable canvas so buttons are never cut off when the window is resized.
        btn_scroll_wrap = ttk.Frame(buttons_bar)
        btn_scroll_wrap.pack(fill="x", expand=False, padx=6, pady=6)

        btn_canvas = tk.Canvas(btn_scroll_wrap, bg=DARK_BG, highlightthickness=0, bd=0, height=1)
        btn_scroll_y = ttk.Scrollbar(btn_scroll_wrap, orient="vertical", command=btn_canvas.yview)
        btn_scroll_x = ttk.Scrollbar(btn_scroll_wrap, orient="horizontal", command=btn_canvas.xview)
        btn_canvas.configure(yscrollcommand=btn_scroll_y.set, xscrollcommand=btn_scroll_x.set)


        btn_scroll_wrap.grid_columnconfigure(0, weight=1)
        btn_scroll_wrap.grid_rowconfigure(0, weight=0)

        btn_canvas.grid(row=0, column=0, sticky="ew")
        btn_scroll_y.grid(row=0, column=1, sticky="ns")
        btn_scroll_x.grid(row=1, column=0, sticky="ew")


        # Start hidden; we only show scrollbars when needed.
        btn_scroll_y.grid_remove()
        btn_scroll_x.grid_remove()

        btn_inner = ttk.Frame(btn_canvas)
        _btn_inner_id = btn_canvas.create_window((0, 0), window=btn_inner, anchor="nw")

        def _btn_update_scrollbars(event=None):
            try:
                # Always keep scrollregion accurate
                btn_canvas.configure(scrollregion=btn_canvas.bbox("all"))
                sr = btn_canvas.bbox("all")
                if not sr:
                    return

                # --- KEY FIX ---
                # Resize the canvas height to the buttons' requested height so there is no
                # dead/empty gap above the horizontal scrollbar.
                try:
                    desired_h = max(1, int(btn_inner.winfo_reqheight()))
                    cur_h = int(btn_canvas.cget("height") or 0)
                    if cur_h != desired_h:
                        btn_canvas.configure(height=desired_h)
                except Exception:
                    pass

                x0, y0, x1, y1 = sr
                cw = btn_canvas.winfo_width()
                ch = btn_canvas.winfo_height()

                need_x = (x1 - x0) > (cw + 1)
                need_y = (y1 - y0) > (ch + 1)

                if need_x:
                    btn_scroll_x.grid()
                else:
                    btn_scroll_x.grid_remove()
                    btn_canvas.xview_moveto(0)

                if need_y:
                    btn_scroll_y.grid()
                else:
                    btn_scroll_y.grid_remove()
                    btn_canvas.yview_moveto(0)
            except Exception:
                pass


        def _btn_canvas_on_configure(event=None):
            try:
                # Keep the inner window pinned to top-left
                btn_canvas.coords(_btn_inner_id, 0, 0)
            except Exception:
                pass
            _btn_update_scrollbars()

        btn_inner.bind("<Configure>", _btn_update_scrollbars)
        btn_canvas.bind("<Configure>", _btn_canvas_on_configure)

        # The original button layout (unchanged), placed inside the scrollable inner frame.
        btn_bar = ttk.Frame(btn_inner)
        btn_bar.pack(fill="x", expand=False)

        # Keep groups left-aligned; the spacer column absorbs extra width.
        btn_bar.grid_columnconfigure(0, weight=0)
        btn_bar.grid_columnconfigure(1, weight=0)
        btn_bar.grid_columnconfigure(2, weight=1)

        BTN_W = 14

        # (Start All button moved into the left-side info section above Account.)
        train_group = ttk.Frame(btn_bar)
        train_group.grid(row=0, column=0, sticky="w", padx=(0, 18), pady=(0, 6))


        # One more pass after layout so scrollbars reflect the true initial size.
        self.after_idle(_btn_update_scrollbars)






        self.lbl_neural = ttk.Label(controls_left, text="Neural: stopped", style="Subtle.TLabel")
        self.lbl_neural.pack(anchor="w", padx=6, pady=(0, 2))

        self.lbl_trader = ttk.Label(controls_left, text="Trader: stopped", style="Subtle.TLabel")
        self.lbl_trader.pack(anchor="w", padx=6, pady=(0, 6))

        self.lbl_last_status = ttk.Label(controls_left, text="Last status: N/A", style="Subtle.TLabel")
        self.lbl_last_status.pack(anchor="w", padx=6, pady=(0, 2))


        # ----------------------------
        # Training section (everything training-specific lives here)
        # ----------------------------
        train_buttons_row = ttk.Frame(training_left)
        train_buttons_row.pack(fill="x", padx=6, pady=(6, 6))

        ttk.Button(train_buttons_row, text="Train Selected", width=BTN_W, style="Primary.TButton", command=self.train_selected_coin).pack(anchor="w", pady=(0, 6))
        ttk.Button(train_buttons_row, text="Train All", width=BTN_W, style="Primary.TButton", command=self.train_all_coins).pack(anchor="w", pady=(0, 6))
        ttk.Button(train_buttons_row, text="Train ML Model", width=BTN_W, style="Accent.TButton", command=self.train_ml_selected).pack(anchor="w", pady=(0, 6))
        ttk.Button(train_buttons_row, text="Train ML All",   width=BTN_W, style="Accent.TButton", command=self.train_ml_all).pack(anchor="w", pady=(0, 8))

        # Force retrain checkbox
        # When checked: skips the --incremental guard so training always runs.
        # Use this when memory is fresh but you want to force a full retrain.
        self._force_retrain_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            train_buttons_row,
            text="Force Retrain (ignore age)",
            variable=self._force_retrain_var,
            style="TCheckbutton",
        ).pack(anchor="w", pady=(0, 4))

        # Training status (per-coin + gating reason)
        self.lbl_training_overview = ttk.Label(training_left, text="Training: N/A", style="Subtle.TLabel")
        self.lbl_training_overview.pack(anchor="w", padx=6, pady=(0, 2))

        self.lbl_flow_hint = ttk.Label(training_left, text="Flow: Train → Start All", style="Subtle.TLabel")
        self.lbl_flow_hint.pack(anchor="w", padx=6, pady=(0, 6))

        self.training_list = tk.Listbox(
            training_left,
            height=5,
            bg=DARK_PANEL,
            fg=DARK_FG,
            selectbackground=DARK_SELECT_BG,
            selectforeground=DARK_SELECT_FG,
            highlightbackground=DARK_BORDER,
            highlightcolor=DARK_ACCENT,
            activestyle="none",
        )
        self.training_list.pack(fill="both", expand=True, padx=6, pady=(0, 6))


        # Start All (moved here: LEFT side of the dual section, directly above Account)
        start_all_row = ttk.Frame(controls_left)
        start_all_row.pack(fill="x", padx=6, pady=(0, 6))

        self.btn_toggle_all = ttk.Button(
            start_all_row,
            text="Start All",
            width=BTN_W,
            style="Primary.TButton",
            command=self.toggle_all_scripts,
        )
        self.btn_toggle_all.pack(side="left")


        def _make_stat_card(parent: tk.Widget, title: str, value: str, color: str) -> Tuple[tk.Frame, tk.Label]:
            card = tk.Frame(parent, bg=DARK_PANEL2, highlightbackground=DARK_BORDER, highlightthickness=1)
            tk.Label(
                card,
                text=title,
                bg=DARK_PANEL2,
                fg=DARK_MUTED,
                font=(self._mono_font_family, 8),
            ).pack(anchor="w", padx=8, pady=(6, 0))
            value_lbl = tk.Label(
                card,
                text=value,
                bg=DARK_PANEL2,
                fg=color,
                font=(self._mono_font_family, 14, "bold"),
            )
            value_lbl.pack(anchor="w", padx=8, pady=(0, 6))
            return card, value_lbl

        # Account info (LEFT column, under status)
        acct_box = ttk.LabelFrame(controls_left, text="Account", style="Card.TLabelframe")
        acct_box.pack(fill="x", padx=6, pady=6)

        cards_row = tk.Frame(acct_box, bg=DARK_BG)
        cards_row.pack(fill="x", padx=8, pady=(8, 2))
        for i in range(2):
            cards_row.grid_columnconfigure(i, weight=1)

        card_total, self.lbl_acct_total_value = _make_stat_card(cards_row, "Total Value", "$0.00", DARK_ACCENT)
        card_total.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 6))

        card_hold, self.lbl_acct_holdings_value = _make_stat_card(cards_row, "Holdings", "N/A", DARK_FG)
        card_hold.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 6))

        card_bp, self.lbl_acct_buying_power = _make_stat_card(cards_row, "Buying Power", "N/A", DARK_ACCENT2)
        card_bp.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(0, 6))

        card_pit, self.lbl_acct_percent_in_trade = _make_stat_card(cards_row, "% In Trade", "N/A", "#FFD700")
        card_pit.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(0, 6))

        self.lbl_acct_total_positions = ttk.Label(acct_box, text="Open Positions: N/A", style="Subtle.TLabel")
        self.lbl_acct_total_positions.pack(anchor="w", padx=8, pady=(0, 0))

        # DCA affordability
        self.lbl_acct_dca_spread = ttk.Label(acct_box, text="DCA Levels (spread): N/A", style="Subtle.TLabel")
        self.lbl_acct_dca_spread.pack(anchor="w", padx=8, pady=(2, 0))

        self.lbl_acct_dca_single = ttk.Label(acct_box, text="DCA Levels (single): N/A", style="Subtle.TLabel")
        self.lbl_acct_dca_single.pack(anchor="w", padx=8, pady=(2, 0))

        self.lbl_pnl = ttk.Label(acct_box, text="Realized P&L: N/A", font=(self._ui_font_family, 11, "bold"))
        self.lbl_pnl.pack(anchor="w", padx=8, pady=(4, 6))



        # Neural levels overview (spans FULL width under the dual section)
        # Shows the current LONG/SHORT level (0..7) for every coin at once.
        neural_box = ttk.LabelFrame(top_controls, text="Neural Levels (0–7)", style="Card.TLabelframe")
        neural_box.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        legend = ttk.Frame(neural_box)
        legend.pack(fill="x", padx=6, pady=(4, 0))

        ttk.Label(legend, text="Level bars: 0 = bottom, 7 = top").pack(side="left")
        ttk.Label(legend, text="   ").pack(side="left")
        ttk.Label(legend, text="Blue = Long").pack(side="left")
        ttk.Label(legend, text="  ").pack(side="left")
        ttk.Label(legend, text="Orange = Short").pack(side="left")

        self.lbl_neural_overview_last = ttk.Label(legend, text="Last: N/A")
        self.lbl_neural_overview_last.pack(side="right")

        # Scrollable area for tiles (auto-hides the scrollbar if everything fits)
        neural_viewport = ttk.Frame(neural_box)
        neural_viewport.pack(fill="both", expand=True, padx=6, pady=(4, 6))
        neural_viewport.grid_rowconfigure(0, weight=1)
        neural_viewport.grid_columnconfigure(0, weight=1)

        self._neural_overview_canvas = tk.Canvas(
            neural_viewport,
            bg=DARK_PANEL2,
            highlightthickness=1,
            highlightbackground=DARK_BORDER,
            bd=0,
        )
        self._neural_overview_canvas.grid(row=0, column=0, sticky="nsew")

        self._neural_overview_scroll = ttk.Scrollbar(
            neural_viewport,
            orient="vertical",
            command=self._neural_overview_canvas.yview,
        )
        self._neural_overview_scroll.grid(row=0, column=1, sticky="ns")

        self._neural_overview_canvas.configure(yscrollcommand=self._neural_overview_scroll.set)

        self.neural_wrap = WrapFrame(self._neural_overview_canvas)
        self._neural_overview_window = self._neural_overview_canvas.create_window(
            (0, 0),
            window=self.neural_wrap,
            anchor="nw",
        )

        def _update_neural_overview_scrollbars(event=None) -> None:
            """Update scrollregion + hide/show the scrollbar depending on overflow."""
            try:
                c = self._neural_overview_canvas
                win = self._neural_overview_window

                c.update_idletasks()
                bbox = c.bbox(win)
                if not bbox:
                    self._neural_overview_scroll.grid_remove()
                    return

                c.configure(scrollregion=bbox)
                content_h = int(bbox[3] - bbox[1])
                view_h = int(c.winfo_height())

                if content_h > (view_h + 1):
                    self._neural_overview_scroll.grid()
                else:
                    self._neural_overview_scroll.grid_remove()
                    try:
                        c.yview_moveto(0)
                    except Exception:
                        pass
            except Exception:
                pass

        def _on_neural_canvas_configure(e) -> None:
            # Keep the inner wrap frame exactly the canvas width so wrapping is correct.
            try:
                self._neural_overview_canvas.itemconfigure(self._neural_overview_window, width=int(e.width))
            except Exception:
                pass
            _update_neural_overview_scrollbars()

        self._neural_overview_canvas.bind("<Configure>", _on_neural_canvas_configure, add="+")
        self.neural_wrap.bind("<Configure>", _update_neural_overview_scrollbars, add="+")
        self._update_neural_overview_scrollbars = _update_neural_overview_scrollbars

        # Mousewheel scroll inside the tiles area
        def _wheel(e):
            try:
                if self._neural_overview_scroll.winfo_ismapped():
                    self._neural_overview_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass

        self._neural_overview_canvas.bind("<Enter>", lambda _e: self._neural_overview_canvas.focus_set(), add="+")
        self._neural_overview_canvas.bind("<MouseWheel>", _wheel, add="+")

        # tiles by coin
        self.neural_tiles: Dict[str, NeuralSignalTile] = {}
        # small cache: path -> (mtime, value)
        self._neural_overview_cache: Dict[str, Tuple[float, Any]] = {}

        self._rebuild_neural_overview()
        try:
            self.after_idle(self._update_neural_overview_scrollbars)
        except Exception:
            pass








        # ----------------------------
        # LEFT: 3) Live Output (pane)
        # ----------------------------

        # Half-size fixed-width font for live logs (Runner/Trader/Trainers)
        _base = tkfont.nametofont("TkFixedFont")
        _half = max(6, int(round(abs(int(_base.cget("size"))) / 2.0)))
        self._live_log_font = _base.copy()
        self._live_log_font.configure(size=_half)

        logs_frame = ttk.LabelFrame(left_split, text="Live Output", style="Card.TLabelframe")
        self.logs_nb = ttk.Notebook(logs_frame)
        self.logs_nb.pack(fill="both", expand=True, padx=6, pady=6)


        # Runner tab
        runner_tab = ttk.Frame(self.logs_nb)
        self.logs_nb.add(runner_tab, text="Runner")
        self.runner_text = tk.Text(
            runner_tab,
            height=8,
            wrap="none",
            font=self._live_log_font,
            bg=DARK_PANEL,
            fg=DARK_FG,
            insertbackground=DARK_FG,
            selectbackground=DARK_SELECT_BG,
            selectforeground=DARK_SELECT_FG,
            highlightbackground=DARK_BORDER,
            highlightcolor=DARK_ACCENT,
        )

        runner_scroll = ttk.Scrollbar(runner_tab, orient="vertical", command=self.runner_text.yview)
        self.runner_text.configure(yscrollcommand=runner_scroll.set)
        self.runner_text.pack(side="left", fill="both", expand=True)
        runner_scroll.pack(side="right", fill="y")

        # Trader tab
        trader_tab = ttk.Frame(self.logs_nb)
        self.logs_nb.add(trader_tab, text="Trader")
        self.trader_text = tk.Text(
            trader_tab,
            height=8,
            wrap="none",
            font=self._live_log_font,
            bg=DARK_PANEL,
            fg=DARK_FG,
            insertbackground=DARK_FG,
            selectbackground=DARK_SELECT_BG,
            selectforeground=DARK_SELECT_FG,
            highlightbackground=DARK_BORDER,
            highlightcolor=DARK_ACCENT,
        )

        trader_scroll = ttk.Scrollbar(trader_tab, orient="vertical", command=self.trader_text.yview)
        self.trader_text.configure(yscrollcommand=trader_scroll.set)
        self.trader_text.pack(side="left", fill="both", expand=True)
        trader_scroll.pack(side="right", fill="y")

        # Trainers tab (multi-coin)
        trainer_tab = ttk.Frame(self.logs_nb)
        self.logs_nb.add(trainer_tab, text="Trainers")

        top_bar = ttk.Frame(trainer_tab)
        top_bar.pack(fill="x", padx=6, pady=6)

        self.trainer_coin_var = tk.StringVar(value=(self.coins[0] if self.coins else "BTC"))
        ttk.Label(top_bar, text="Coin:").pack(side="left")
        self.trainer_coin_combo = ttk.Combobox(
            top_bar,
            textvariable=self.trainer_coin_var,
            values=["All"] + self.coins,
            state="readonly",
            width=8
        )
        self.trainer_coin_combo.pack(side="left", padx=(6, 12))

        ttk.Button(top_bar, text="Start Trainer", style="Neutral.TButton", command=self.start_trainer_for_selected_coin).pack(side="left")
        ttk.Button(top_bar, text="Stop Trainer", style="Neutral.TButton", command=self.stop_trainer_for_selected_coin).pack(side="left", padx=(6, 0))

        self.trainer_status_lbl = ttk.Label(top_bar, text="(no trainers running)")
        self.trainer_status_lbl.pack(side="left", padx=(12, 0))

        self.trainer_text = tk.Text(
            trainer_tab,
            height=8,
            wrap="none",
            font=self._live_log_font,
            bg=DARK_PANEL,
            fg=DARK_FG,
            insertbackground=DARK_FG,
            selectbackground=DARK_SELECT_BG,
            selectforeground=DARK_SELECT_FG,
            highlightbackground=DARK_BORDER,
            highlightcolor=DARK_ACCENT,
        )

        trainer_scroll = ttk.Scrollbar(trainer_tab, orient="vertical", command=self.trainer_text.yview)
        self.trainer_text.configure(yscrollcommand=trainer_scroll.set)
        self.trainer_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        trainer_scroll.pack(side="right", fill="y", padx=(0, 6), pady=(0, 6))

        # Backtest tab
        backtest_tab = ttk.Frame(self.logs_nb)
        self.logs_nb.add(backtest_tab, text="Backtest")
        self._build_backtest_tab(backtest_tab)

        # Log coloring for fast scanning.
        for _txt_widget in (self.runner_text, self.trader_text, self.trainer_text):
            try:
                _txt_widget.tag_config("error", foreground=DARK_DANGER)
                _txt_widget.tag_config("warn", foreground="#FFD700")
                _txt_widget.tag_config("success", foreground=DARK_SUCCESS)
                _txt_widget.tag_config("info", foreground=DARK_FG)
            except Exception:
                pass


        # Add left panes (no trades/history on the left anymore)
        # Default should match the screenshot: more room for Controls/Health + Neural Levels.
        left_split.add(top_controls, weight=1)
        left_split.add(logs_frame, weight=1)

        try:
            # Ensure the top pane can't start (or be clamped) too small to show Neural Levels.
            left_split.paneconfigure(top_controls, minsize=360)
            left_split.paneconfigure(logs_frame, minsize=220)
        except Exception:
            pass

        def _init_left_split_sash_once():
            try:
                if getattr(self, "_did_init_left_split_sash", False):
                    return

                # If the user already moved the sash, never override it.
                if getattr(self, "_user_moved_left_split", False):
                    self._did_init_left_split_sash = True
                    return

                total = left_split.winfo_height()
                if total <= 2:
                    self.after(10, _init_left_split_sash_once)
                    return

                min_top = 360
                min_bottom = 220

                # Match screenshot feel: keep Live Output ~260px high, give the rest to top.
                desired_bottom = 260
                target = total - max(min_bottom, desired_bottom)
                target = max(min_top, min(total - min_bottom, target))

                left_split.sashpos(0, int(target))
                self._did_init_left_split_sash = True
            except Exception:
                pass

        self.after_idle(_init_left_split_sash_once)






        # ----------------------------
        # RIGHT TOP: Charts (tabs)
        # ----------------------------
        charts_frame = ttk.LabelFrame(right_split, text="Charts (Neural lines overlaid)", style="Card.TLabelframe")
        self._charts_frame = charts_frame

        # Multi-row "tabs" (WrapFrame)
        self.chart_tabs_bar = WrapFrame(charts_frame)
        # Keep left padding, remove right padding so tabs can reach the edge
        self.chart_tabs_bar.pack(fill="x", padx=(6, 0), pady=(6, 0))

        # Page container (no ttk.Notebook, so there are NO native tabs to show)
        self.chart_pages_container = ttk.Frame(charts_frame)
        # Keep left padding, remove right padding so charts fill to the edge
        self.chart_pages_container.pack(fill="both", expand=True, padx=(6, 0), pady=(0, 6))


        self._chart_tab_buttons: Dict[str, ttk.Button] = {}
        self.chart_pages: Dict[str, ttk.Frame] = {}
        self._current_chart_page: str = "ACCOUNT"

        def _show_page(name: str) -> None:
            self._current_chart_page = name
            # hide all pages
            for f in self.chart_pages.values():
                try:
                    f.pack_forget()
                except Exception:
                    pass
            # show selected
            f = self.chart_pages.get(name)
            if f is not None:
                f.pack(fill="both", expand=True)

            # style selected tab
            for txt, b in self._chart_tab_buttons.items():
                try:
                    b.configure(style=("ChartTabSelected.TButton" if txt == name else "ChartTab.TButton"))
                except Exception:
                    pass

            # Immediately refresh the newly shown coin chart so candles appear right away
            # (even if trader/neural scripts are not running yet).
            try:
                tab = str(name or "").strip().upper()
                if tab and tab != "ACCOUNT":
                    coin = tab
                    chart = self.charts.get(coin)
                    if chart:
                        def _do_refresh_visible():
                            try:
                                # Ensure coin folders exist (best-effort; fast)
                                try:
                                    cf_sig = (self.settings.get("main_neural_dir"), tuple(self.coins))
                                    if getattr(self, "_coin_folders_sig", None) != cf_sig:
                                        self._coin_folders_sig = cf_sig
                                        self.coin_folders = build_coin_folders(self.settings["main_neural_dir"], self.coins)
                                except Exception:
                                    pass

                                pos = self._last_positions.get(coin, {}) if isinstance(self._last_positions, dict) else {}
                                buy_px = pos.get("current_buy_price", None)
                                sell_px = pos.get("current_sell_price", None)
                                trail_line = pos.get("trail_line", None)
                                dca_line_price = pos.get("dca_line_price", None)
                                avg_cost_basis = pos.get("avg_cost_basis", None)

                                chart.refresh(
                                    self.coin_folders,
                                    current_buy_price=buy_px,
                                    current_sell_price=sell_px,
                                    trail_line=trail_line,
                                    dca_line_price=dca_line_price,
                                    avg_cost_basis=avg_cost_basis,
                                )

                            except Exception:
                                pass

                        self.after(1, _do_refresh_visible)
            except Exception:
                pass


        self._show_chart_page = _show_page  # used by _rebuild_coin_chart_tabs()

        # ACCOUNT page
        acct_page = ttk.Frame(self.chart_pages_container)
        self.chart_pages["ACCOUNT"] = acct_page

        acct_btn = ttk.Button(
            self.chart_tabs_bar,
            text="ACCOUNT",
            style="ChartTab.TButton",
            command=lambda: self._show_chart_page("ACCOUNT"),
        )
        self.chart_tabs_bar.add(acct_btn, padx=(0, 6), pady=(0, 6))
        self._chart_tab_buttons["ACCOUNT"] = acct_btn

        self.account_chart = AccountValueChart(
            acct_page,
            self.account_value_history_path,
            self.trade_history_path,
        )
        self.account_chart.pack(fill="both", expand=True)

        # Coin pages
        self.charts: Dict[str, CandleChart] = {}
        for coin in self.coins:
            page = ttk.Frame(self.chart_pages_container)
            self.chart_pages[coin] = page

            btn = ttk.Button(
                self.chart_tabs_bar,
                text=coin,
                style="ChartTab.TButton",
                command=lambda c=coin: self._show_chart_page(c),
            )
            self.chart_tabs_bar.add(btn, padx=(0, 6), pady=(0, 6))
            self._chart_tab_buttons[coin] = btn

            chart = CandleChart(page, self.fetcher, coin, self._settings_getter, self.trade_history_path)
            chart.pack(fill="both", expand=True)
            self.charts[coin] = chart

        # show initial page
        self._show_chart_page("ACCOUNT")





        # ----------------------------
        # RIGHT BOTTOM: Current Trades + Trade History (stacked)
        # ----------------------------
        right_bottom_split = ttk.Panedwindow(right_split, orient="vertical")
        self._pw_right_bottom_split = right_bottom_split

        right_bottom_split.bind("<Configure>", lambda e: self._schedule_paned_clamp(self._pw_right_bottom_split))
        right_bottom_split.bind("<ButtonRelease-1>", lambda e: (
            setattr(self, "_user_moved_right_bottom_split", True),
            self._schedule_paned_clamp(self._pw_right_bottom_split),
        ))

        # Current trades (top)
        trades_frame = ttk.LabelFrame(right_bottom_split, text="Current Trades", style="Card.TLabelframe")

        cols = (
            "coin",
            "qty",
            "value",          # <-- right after qty
            "avg_cost",
            "buy_price",
            "buy_pnl",
            "sell_price",
            "sell_pnl",
            "dca_stages",
            "dca_24h",
            "next_dca",
            "trail_line",     # keep trail line column
        )

        header_labels = {
            "coin": "Coin",
            "qty": "Qty",
            "value": "Value",
            "avg_cost": "Avg Cost",
            "buy_price": "Ask Price",
            "buy_pnl": "DCA PnL",
            "sell_price": "Bid Price",
            "sell_pnl": "Sell PnL",
            "dca_stages": "DCA Stage",
            "dca_24h": "DCA 24h",
            "next_dca": "Next DCA",
            "trail_line": "Trail Line",
        }

        trades_table_wrap = ttk.Frame(trades_frame)
        trades_table_wrap.pack(fill="both", expand=True, padx=6, pady=6)

        self.trades_tree = ttk.Treeview(
            trades_table_wrap,
            columns=cols,
            show="headings",
            height=10
        )
        for c in cols:
            self.trades_tree.heading(c, text=header_labels.get(c, c))
            self.trades_tree.column(c, width=110, anchor="center", stretch=True)

        # Reasonable starting widths (they will be dynamically scaled on resize)
        self.trades_tree.column("coin", width=70)
        self.trades_tree.column("qty", width=95)
        self.trades_tree.column("value", width=110)
        self.trades_tree.column("next_dca", width=160)
        self.trades_tree.column("dca_stages", width=90)
        self.trades_tree.column("dca_24h", width=80)

        ysb = ttk.Scrollbar(trades_table_wrap, orient="vertical", command=self.trades_tree.yview)
        xsb = ttk.Scrollbar(trades_table_wrap, orient="horizontal", command=self.trades_tree.xview)
        self.trades_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        # Position row coloring by current PnL.
        try:
            self.trades_tree.tag_configure("profit", background="#0D2B1A", foreground=DARK_SUCCESS)
            self.trades_tree.tag_configure("loss", background="#2B0D0F", foreground=DARK_DANGER)
            self.trades_tree.tag_configure("neutral", background=DARK_PANEL, foreground=DARK_FG)
        except Exception:
            pass

        self.trades_tree.pack(side="top", fill="both", expand=True)
        xsb.pack(side="bottom", fill="x")
        ysb.pack(side="right", fill="y")

        def _resize_trades_columns(*_):
            # Scale the initial column widths proportionally so the table always fits the current window.
            try:
                total_w = int(self.trades_tree.winfo_width())
            except Exception:
                return
            if total_w <= 1:
                return

            try:
                sb_w = int(ysb.winfo_width() or 0)
            except Exception:
                sb_w = 0

            avail = max(200, total_w - sb_w - 8)

            base = {
                "coin": 70,
                "qty": 95,
                "value": 110,
                "avg_cost": 110,
                "buy_price": 110,
                "buy_pnl": 110,
                "sell_price": 110,
                "sell_pnl": 110,
                "dca_stages": 90,
                "dca_24h": 80,
                "next_dca": 160,
                "trail_line": 110,
            }
            base_total = sum(base.get(c, 110) for c in cols) or 1
            scale = avail / base_total

            for c in cols:
                w = int(base.get(c, 110) * scale)
                self.trades_tree.column(c, width=max(60, min(420, w)))

        self.trades_tree.bind("<Configure>", lambda e: self.after_idle(_resize_trades_columns))
        self.after_idle(_resize_trades_columns)


        # Trade history (bottom)
        hist_frame = ttk.LabelFrame(right_bottom_split, text="Trade History (scroll)", style="Card.TLabelframe")

        hist_top = ttk.Frame(hist_frame)
        hist_top.pack(fill="x", padx=6, pady=(6, 0))

        ttk.Button(
            hist_top,
            text="Copy Trades",
            style="Neutral.TButton",
            command=self._copy_trade_history_to_clipboard,
        ).pack(side="left")

        ttk.Label(
            hist_top,
            text="Copies selected rows, or all visible rows if none selected",
            style="Subtle.TLabel",
        ).pack(side="left", padx=(10, 0))

        hist_wrap = ttk.Frame(hist_frame)
        hist_wrap.pack(fill="both", expand=True, padx=6, pady=6)

        self.hist_list = tk.Listbox(
            hist_wrap,
            height=10,
            bg=DARK_PANEL,
            fg=DARK_FG,
            selectbackground=DARK_SELECT_BG,
            selectforeground=DARK_SELECT_FG,
            highlightbackground=DARK_BORDER,
            highlightcolor=DARK_ACCENT,
            activestyle="none",
        )
        ysb2 = ttk.Scrollbar(hist_wrap, orient="vertical", command=self.hist_list.yview)
        xsb2 = ttk.Scrollbar(hist_wrap, orient="horizontal", command=self.hist_list.xview)
        self.hist_list.configure(yscrollcommand=ysb2.set, xscrollcommand=xsb2.set)

        self.hist_list.bind("<Control-c>", lambda e: (self._copy_trade_history_to_clipboard(), "break"))
        self.hist_list.bind("<Control-C>", lambda e: (self._copy_trade_history_to_clipboard(), "break"))

        # Add redundant copy affordances so copy works even when list focus is lost.
        self.hist_list.bind("<Button-3>", self._show_trade_history_context_menu)
        self.hist_list.bind("<Button-2>", self._show_trade_history_context_menu)
        self.bind_all("<Control-Shift-c>", lambda e: (self._copy_trade_history_to_clipboard(), "break"))
        self.bind_all("<Control-Shift-C>", lambda e: (self._copy_trade_history_to_clipboard(), "break"))

        self._hist_menu = tk.Menu(self, tearoff=0)
        self._hist_menu.add_command(label="Copy Trades", command=self._copy_trade_history_to_clipboard)

        self.hist_list.pack(side="left", fill="both", expand=True)
        ysb2.pack(side="right", fill="y")
        xsb2.pack(side="bottom", fill="x")


        # Assemble right side
        right_split.add(charts_frame, weight=3)
        right_split.add(right_bottom_split, weight=2)

        right_bottom_split.add(trades_frame, weight=2)
        right_bottom_split.add(hist_frame, weight=1)

        try:
            # Screenshot-style sizing: don't force Charts to be enormous by default.
            right_split.paneconfigure(charts_frame, minsize=360)
            right_split.paneconfigure(right_bottom_split, minsize=220)
        except Exception:
            pass

        try:
            right_bottom_split.paneconfigure(trades_frame, minsize=140)
            right_bottom_split.paneconfigure(hist_frame, minsize=120)
        except Exception:
            pass

        # Startup defaults to match the screenshot (but never override if user already dragged).
        def _init_right_split_sash_once():
            try:
                if getattr(self, "_did_init_right_split_sash", False):
                    return

                if getattr(self, "_user_moved_right_split", False):
                    self._did_init_right_split_sash = True
                    return

                total = right_split.winfo_height()
                if total <= 2:
                    self.after(10, _init_right_split_sash_once)
                    return

                min_top = 360
                min_bottom = 220
                desired_top = 410  # ~matches screenshot chart pane height
                target = max(min_top, min(total - min_bottom, desired_top))

                right_split.sashpos(0, int(target))
                self._did_init_right_split_sash = True
            except Exception:
                pass

        def _init_right_bottom_split_sash_once():
            try:
                if getattr(self, "_did_init_right_bottom_split_sash", False):
                    return

                if getattr(self, "_user_moved_right_bottom_split", False):
                    self._did_init_right_bottom_split_sash = True
                    return

                total = right_bottom_split.winfo_height()
                if total <= 2:
                    self.after(10, _init_right_bottom_split_sash_once)
                    return

                min_top = 140
                min_bottom = 120
                desired_top = 280  # more space for Current Trades (like screenshot)
                target = max(min_top, min(total - min_bottom, desired_top))

                right_bottom_split.sashpos(0, int(target))
                self._did_init_right_bottom_split_sash = True
            except Exception:
                pass

        self.after_idle(_init_right_split_sash_once)
        self.after_idle(_init_right_bottom_split_sash_once)

        # Initial clamp once everything is laid out
        self.after_idle(lambda: (
            self._schedule_paned_clamp(getattr(self, "_pw_outer", None)),
            self._schedule_paned_clamp(getattr(self, "_pw_left_split", None)),
            self._schedule_paned_clamp(getattr(self, "_pw_right_split", None)),
            self._schedule_paned_clamp(getattr(self, "_pw_right_bottom_split", None)),
        ))


        # status bar
        self.status = ttk.Label(self, text="Ready", anchor="w")
        self.status.pack(fill="x", side="bottom")



    # ---- panedwindow anti-collapse helpers ----

    def _schedule_paned_clamp(self, pw: ttk.Panedwindow) -> None:
        """
        Debounced clamp so we don't fight the geometry manager mid-resize.

        IMPORTANT: use `after(1, ...)` instead of `after_idle(...)` so it still runs
        while the mouse is held during sash dragging (Tk often doesn't go "idle"
        until after the drag ends, which is exactly when panes can vanish).
        """
        try:
            if not pw or not int(pw.winfo_exists()):
                return
        except Exception:
            return

        key = str(pw)
        if key in self._paned_clamp_after_ids:
            return

        def _run():
            try:
                self._paned_clamp_after_ids.pop(key, None)
            except Exception:
                pass
            self._clamp_panedwindow_sashes(pw)

        try:
            self._paned_clamp_after_ids[key] = self.after(1, _run)
        except Exception:
            pass


    def _clamp_panedwindow_sashes(self, pw: ttk.Panedwindow) -> None:
        """
        Enforces each pane's configured 'minsize' by clamping sash positions.

        NOTE:
        ttk.Panedwindow.paneconfigure(pane) typically returns dict values like:
            {"minsize": ("minsize", "minsize", "Minsize", "140"), ...}
        so we MUST pull the last element when it's a tuple/list.
        """
        try:
            if not pw or not int(pw.winfo_exists()):
                return

            panes = list(pw.panes())
            if len(panes) < 2:
                return

            orient = str(pw.cget("orient"))
            total = pw.winfo_height() if orient == "vertical" else pw.winfo_width()
            if total <= 2:
                return

            def _get_minsize(pane_id) -> int:
                try:
                    cfg = pw.paneconfigure(pane_id)
                    ms = cfg.get("minsize", 0)

                    # ttk returns tuples like ('minsize','minsize','Minsize','140')
                    if isinstance(ms, (tuple, list)) and ms:
                        ms = ms[-1]

                    # sometimes it's already int/float-like, sometimes it's a string
                    return max(0, int(float(ms)))
                except Exception:
                    return 0

            mins: List[int] = [_get_minsize(p) for p in panes]

            # If total space is smaller than sum(mins), we still clamp as best-effort
            # by scaling mins down proportionally but never letting a pane hit 0.
            if sum(mins) >= total:
                # best-effort: keep every pane at least 24px so it can’t disappear
                floor = 24
                mins = [max(floor, m) for m in mins]

                # if even floors don't fit, just stop here (window minsize should prevent this)
                if sum(mins) >= total:
                    return

            # Two-pass clamp so constraints settle even with multiple sashes
            for _ in range(2):
                for i in range(len(panes) - 1):
                    min_pos = sum(mins[: i + 1])
                    max_pos = total - sum(mins[i + 1 :])

                    try:
                        cur = int(pw.sashpos(i))
                    except Exception:
                        continue

                    new = max(min_pos, min(max_pos, cur))
                    if new != cur:
                        try:
                            pw.sashpos(i, new)
                        except Exception:
                            pass


        except Exception:
            pass



    # ---- process control ----


    def _reader_thread(self, proc: subprocess.Popen, q: "queue.Queue[str]", prefix: str) -> None:
        try:
            # line-buffered text mode
            while True:
                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                q.put(f"{prefix}{line.rstrip()}")
        except Exception:
            pass
        finally:
            code = None
            try:
                code = proc.poll()
                if code is None:
                    try:
                        code = proc.wait(timeout=0.2)
                    except Exception:
                        code = proc.poll()
            except Exception:
                pass
            q.put(f"{prefix}[process exited code={code if code is not None else 'unknown'}]")

    def _start_process(self, p: ProcInfo, log_q: Optional["queue.Queue[str]"] = None, prefix: str = "") -> None:
        if p.proc and p.proc.poll() is None:
            return
        if not os.path.isfile(p.path):
            messagebox.showerror("Missing script", f"Cannot find: {p.path}")
            return

        env = os.environ.copy()
        env["POWERTRADER_HUB_DIR"] = self.hub_dir  # so rhcb writes where GUI reads

        try:
            p.proc = subprocess.Popen(
                [sys.executable, "-u", p.path],  # -u for unbuffered prints
                cwd=self.project_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            p.stop_requested = False
            p.last_start_ts = time.time()
            p.next_restart_ts = 0.0
            if log_q is not None:
                t = threading.Thread(target=self._reader_thread, args=(p.proc, log_q, prefix), daemon=True)
                t.start()
        except Exception as e:
            messagebox.showerror("Failed to start", f"{p.name} failed to start:\n{e}")


    def _stop_process(self, p: ProcInfo) -> None:
        if not p.proc or p.proc.poll() is not None:
            return
        p.stop_requested = True
        p.next_restart_ts = 0.0
        try:
            p.proc.terminate()
        except Exception:
            pass

    def _supervise_process(self, p: ProcInfo, log_q: "queue.Queue[str]", prefix: str) -> None:
        proc = p.proc
        if not proc:
            return
        rc = proc.poll()
        if rc is None:
            # Decay restart penalty after a stable run window.
            if p.restart_attempts > 0 and p.last_start_ts > 0 and (time.time() - p.last_start_ts) >= 180.0:
                p.restart_attempts = 0
            return
        if p.stop_requested or not p.auto_restart:
            return

        now_ts = time.time()
        if p.next_restart_ts <= 0.0:
            p.restart_attempts = max(1, int(p.restart_attempts) + 1)
            backoff = min(60.0, 2.0 ** min(p.restart_attempts, 5))
            p.next_restart_ts = now_ts + backoff
            log_q.put(f"{prefix}[SUPERVISOR] Unexpected exit rc={rc}; restarting in {backoff:.0f}s")
            return

        if now_ts < p.next_restart_ts:
            return

        log_q.put(f"{prefix}[SUPERVISOR] Restarting {p.name} now")
        self._start_process(p, log_q=log_q, prefix=prefix)

    def start_neural(self) -> None:
        # Reset runner-ready gate file (prevents stale "ready" from a prior run)
        try:
            with open(self.runner_ready_path, "w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time(), "ready": False, "stage": "starting"}, f)
        except Exception:
            pass

        self._start_process(self.proc_neural, log_q=self.runner_log_q, prefix="[RUNNER] ")


    def start_trader(self) -> None:
        self._start_process(self.proc_trader, log_q=self.trader_log_q, prefix="[TRADER] ")


    def stop_neural(self) -> None:
        self._stop_process(self.proc_neural)



    def stop_trader(self) -> None:
        self._stop_process(self.proc_trader)

    def toggle_all_scripts(self) -> None:
        neural_running = bool(self.proc_neural.proc and self.proc_neural.proc.poll() is None)
        trader_running = bool(self.proc_trader.proc and self.proc_trader.proc.poll() is None)

        # If anything is running (or we're waiting on runner readiness), toggle means "stop"
        if neural_running or trader_running or bool(getattr(self, "_auto_start_trader_pending", False)):
            self.stop_all_scripts()
            return

        # Otherwise, toggle means "start"
        self.start_all_scripts()

    def _read_runner_ready(self) -> Dict[str, Any]:
        try:
            if os.path.isfile(self.runner_ready_path):
                with open(self.runner_ready_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"ready": False}

    def _poll_runner_ready_then_start_trader(self) -> None:
        # Cancelled or already started
        if not bool(getattr(self, "_auto_start_trader_pending", False)):
            return

        # If runner died, stop waiting
        if not (self.proc_neural.proc and self.proc_neural.proc.poll() is None):
            self._auto_start_trader_pending = False
            return

        st = self._read_runner_ready()
        if bool(st.get("ready", False)):
            self._auto_start_trader_pending = False

            # Start trader if not already running
            if not (self.proc_trader.proc and self.proc_trader.proc.poll() is None):
                self.start_trader()
            return

        # Not ready yet — keep polling
        try:
            self.after(250, self._poll_runner_ready_then_start_trader)
        except Exception:
            pass

    def start_all_scripts(self) -> None:
        # Reset runtime state in a known order.
        self.stop_neural()
        self.stop_trader()
        self._auto_start_trader_pending = False

        try:
            with open(self.runner_ready_path, "w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time(), "ready": False, "stage": "starting"}, f)
        except Exception:
            pass

        # Enforce flow: Train → Neural → (wait for runner READY) → Trader
        all_trained = all(self._coin_is_trained(c) for c in self.coins) if self.coins else False
        if not all_trained:
            messagebox.showwarning(
                "Training required",
                "All coins must be trained before starting.\nTrain All first."
            )
            return

        self._auto_start_trader_pending = True
        self.start_neural()

        # Wait for runner to signal readiness before starting trader
        try:
            self.after(250, self._poll_runner_ready_then_start_trader)
        except Exception:
            pass


    def _coin_is_trained(self, coin: str) -> bool:
        coin = coin.upper().strip()

        # MT5 trainer writes shared outputs under parent folder (not per-coin stamp files).
        # In MT5 mode, allow Start All without blocking on legacy per-coin training stamps.
        trainer_script = os.path.basename(str(self.settings.get("script_neural_trainer", ""))).lower()
        if trainer_script == "pt_mt5_trainer.py":
            return True

        folder = self.coin_folders.get(coin, "")
        if not folder or not os.path.isdir(folder):
            return False

        # If trainer reports it's currently training, it's not "trained" yet.
        try:
            st = _safe_read_json(os.path.join(folder, "trainer_status.json"))
            if isinstance(st, dict) and str(st.get("state", "")).upper() == "TRAINING":
                return False
        except Exception:
            pass

        stamp_path = os.path.join(folder, "trainer_last_training_time.txt")
        try:
            if not os.path.isfile(stamp_path):
                return False
            with open(stamp_path, "r", encoding="utf-8") as f:
                raw = (f.read() or "").strip()
            ts = float(raw) if raw else 0.0
            if ts <= 0:
                return False
            return (time.time() - ts) <= (14 * 24 * 60 * 60)
        except Exception:
            return False

    def _running_trainers(self) -> List[str]:
        running: List[str] = []

        # Trainers launched by this GUI instance
        for c, lp in self.trainers.items():
            try:
                if lp.info.proc and lp.info.proc.poll() is None:
                    running.append(c)
            except Exception:
                pass

        # Trainers launched elsewhere: look at per-coin status file
        for c in self.coins:
            try:
                coin = (c or "").strip().upper()
                folder = self.coin_folders.get(coin, "")
                if not folder or not os.path.isdir(folder):
                    continue

                status_path = os.path.join(folder, "trainer_status.json")
                st = _safe_read_json(status_path)

                if isinstance(st, dict) and str(st.get("state", "")).upper() == "TRAINING":
                    stamp_path = os.path.join(folder, "trainer_last_training_time.txt")

                    try:
                        if os.path.isfile(stamp_path) and os.path.isfile(status_path):
                            if os.path.getmtime(stamp_path) >= os.path.getmtime(status_path):
                                continue
                    except Exception:
                        pass

                    running.append(coin)
            except Exception:
                pass

        # de-dupe while preserving order
        out: List[str] = []
        seen = set()
        for c in running:
            cc = (c or "").strip().upper()
            if cc and cc not in seen:
                seen.add(cc)
                out.append(cc)
        return out



    def _training_status_map(self) -> Dict[str, str]:
        """
        Returns {coin: "TRAINED" | "TRAINING" | "NOT TRAINED"}.
        """
        running = set(self._running_trainers())
        out: Dict[str, str] = {}
        for c in self.coins:
            if c in running:
                out[c] = "TRAINING"
            elif self._coin_is_trained(c):
                out[c] = "TRAINED"
            else:
                out[c] = "NOT TRAINED"
        return out

    def train_selected_coin(self) -> None:
        coin = (getattr(self, 'train_coin_var', self.trainer_coin_var).get() or "").strip().upper()

        if not coin or coin == "ALL":
            # If 'All' is selected, train all coins
            self.train_all_coins()
            return
        # Reuse the trainers pane runner — start trainer for selected coin
        self.start_trainer_for_selected_coin()

    def train_all_coins(self) -> None:
        """Launch trainers for all coins in parallel."""
        if not self.coins:
            return

        # Stop the Neural Runner ONCE before any training starts (training modifies artifacts the runner reads)
        self.stop_neural()

        # Launch all coin trainers in parallel
        launched: List[str] = []
        failed: List[str] = []
        for c in self.coins:
            try:
                self.status.config(text=f"Attempting to start trainer for {c}...")
                self._launch_trainer_for_coin(c, skip_neural_stop=True)
                lp = self.trainers.get(c)
                if lp and lp.info.proc and lp.info.proc.poll() is None:
                    launched.append(c)
                else:
                    failed.append(c)
            except Exception as e:
                failed.append(c)
                try:
                    self.status.config(text=f"Failed to start trainer for {c}: {e}")
                except Exception:
                    pass

        try:
            msg = []
            if launched:
                msg.append(f"Training: {', '.join(launched)}...")
            if failed:
                msg.append(f"Failed: {', '.join(failed)}")
            if not msg:
                msg = ["Train All: no new trainers started (already running or unavailable)"]
            self.status.config(text=" | ".join(msg))
            # Start cycling trainer logs if more than one trainer is running
            if launched and len(launched) > 1:
                self._cycle_trainer_logs()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # ML model training (XGBoost scorer)
    # ------------------------------------------------------------------

    def train_ml_selected(self) -> None:
        """Train XGBoost ML model for the selected coin."""
        coin = (getattr(self, 'train_coin_var', self.trainer_coin_var).get() or "").strip().upper()
        if not coin:
            return
        self._launch_ml_trainer_for_coin(coin)

    def train_ml_all(self) -> None:
        """Train XGBoost ML model for all coins."""
        for c in list(self.coins):
            self._launch_ml_trainer_for_coin(c)

    def _launch_ml_trainer_for_coin(self, coin: str) -> None:
        """
        Launch pt_mt5_ml_scorer.py train as a streaming subprocess whose output
        goes to the Trainers log tab -- same pattern as _launch_trainer_for_coin.
        """
        coin = str(coin).strip().upper()
        if not coin:
            return

        ml_script = os.path.join(self.project_dir, "pt_mt5_ml_scorer.py")
        if not os.path.isfile(ml_script):
            try:
                self.status.config(text="ML Train: pt_mt5_ml_scorer.py not found -- copy it to the project folder")
            except Exception:
                pass
            return

        # Block if an ML trainer is already running for this coin
        ml_key = f"ML-{coin}"
        existing = self.trainers.get(ml_key)
        if existing and existing.info.proc and existing.info.proc.poll() is None:
            try:
                self.status.config(text=f"ML Train {coin}: already running")
            except Exception:
                pass
            return

        main_dir = os.path.abspath(str(self.settings.get("main_neural_dir", self.project_dir)))
        mem_dir  = main_dir if coin == "BTC" else os.path.join(main_dir, coin)
        tf       = str(self.settings.get("default_timeframe", "1hour"))
        os.makedirs(mem_dir, exist_ok=True)

        cmd = [
            sys.executable, "-u", ml_script,
            "train",
            "--coin",       coin,
            "--timeframe",  tf,
            "--memory-dir", mem_dir,
            "--min-move",   "0.20",
            "--min-samples", "30",
        ]

        try:
            self.status.config(text=f"ML Train {coin}: starting...")
        except Exception:
            pass

        q: "queue.Queue[str]" = queue.Queue()
        info = ProcInfo(name=f"ML-Trainer-{coin}", path=ml_script)
        env  = os.environ.copy()
        env["POWERTRADER_HUB_DIR"] = self.hub_dir

        try:
            info.proc = subprocess.Popen(
                cmd,
                cwd=self.project_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
            t = threading.Thread(
                target=self._reader_thread,
                args=(info.proc, q, f"[ML-{coin}] "),
                daemon=True,
            )
            t.start()
            self.trainers[ml_key] = LogProc(info=info, log_q=q, thread=t,
                                            is_trainer=True, coin=coin)
            try:
                self.status.config(text=f"ML Train {coin}: running (see Trainers log tab)")
            except Exception:
                pass
        except Exception as e:
            try:
                self.status.config(text=f"ML Train {coin}: failed to start -- {e}")
            except Exception:
                pass

    # ------------------------------------------------------------------

    def start_trainer_for_selected_coin(self) -> None:
        """Launch trainer for selected coin from UI dropdown."""
        coin = (self.trainer_coin_var.get() or "").strip().upper()
        if not coin or coin == "ALL":
            self.train_all_coins()
            return
        self._launch_trainer_for_coin(coin, skip_neural_stop=False)

    def _launch_trainer_for_coin(self, coin: str, skip_neural_stop: bool = False) -> None:
        """Internal: Launch trainer for a specific coin."""
        coin = str(coin).strip().upper()
        if not coin:
            return

        # Only stop neural if not already stopped (for single-coin training from UI)
        if not skip_neural_stop:
            self.stop_neural()

        # --- IMPORTANT ---
        # Legacy trainer expects per-coin folders; MT5 trainer lives in mt5 folder.
        coin_cwd = self.coin_folders.get(coin, self.project_dir)
        trainer_name = os.path.basename(str(self.settings.get("script_neural_trainer", "pt_mt5_trainer.py")))
        configured_trainer_path = os.path.abspath(os.path.join(self.project_dir, str(self.settings.get("script_neural_trainer", trainer_name))))

        # If an alt coin folder doesn't exist yet, create it and copy the trainer script from the main (BTC) folder.
        # (Also: overwrite to avoid running stale trainer copies in alt folders.)

        if coin != "BTC":
            try:
                if not os.path.isdir(coin_cwd):
                    os.makedirs(coin_cwd, exist_ok=True)

                src_main_folder = self.coin_folders.get("BTC", self.project_dir)
                src_trainer_path = os.path.join(src_main_folder, trainer_name)
                dst_trainer_path = os.path.join(coin_cwd, trainer_name)

                if os.path.isfile(src_trainer_path):
                    shutil.copy2(src_trainer_path, dst_trainer_path)
            except Exception:
                pass

        trainer_path = os.path.join(coin_cwd, trainer_name)
        trainer_cwd = coin_cwd

        # MT5 trainer runs from mt5 folder and takes --coin flag.
        if trainer_name.lower() == "pt_mt5_trainer.py":
            trainer_path = configured_trainer_path
            trainer_cwd = self.project_dir

        # General fallback to configured path if per-coin copy is not present.
        if (not os.path.isfile(trainer_path)) and os.path.isfile(configured_trainer_path):
            trainer_path = configured_trainer_path
            trainer_cwd = self.project_dir

        if not os.path.isfile(trainer_path):
            messagebox.showerror(
                "Missing trainer",
                f"Cannot find trainer for {coin} at:\n{trainer_path}"
            )
            return

        if coin in self.trainers and self.trainers[coin].info.proc and self.trainers[coin].info.proc.poll() is None:
            return


        # FIX: Do NOT delete memories_*.txt or memory_weights_*.txt before training.
        # The trainer uses --incremental mode which checks if memory is fresh enough
        # and skips retraining if it is.  Deleting memories here caused the bot to
        # start from 0 patterns on every manual "Train" press, losing all accumulated
        # pattern history and producing 0 quality-filtered results.
        # Only delete transient status/lock files, never the learned pattern data.
        try:
            safe_delete_patterns = [
                "trainer_last_training_time.txt",
                "trainer_last_start_time.txt",
                "killer.txt",
                "neural_perfect_threshold_*.txt",
            ]
            # trainer_status.json is intentionally preserved -- the dashboard reads it.

            deleted = 0
            for pat in safe_delete_patterns:
                for fp in glob.glob(os.path.join(coin_cwd, pat)):
                    try:
                        os.remove(fp)
                        deleted += 1
                    except Exception:
                        pass

            if deleted:
                try:
                    self.status.config(text=f"Cleared {deleted} lock file(s) for {coin} before training")
                except Exception:
                    pass
        except Exception:
            pass

        q: "queue.Queue[str]" = queue.Queue()
        info = ProcInfo(name=f"Trainer-{coin}", path=trainer_path)

        env = os.environ.copy()
        env["POWERTRADER_HUB_DIR"] = self.hub_dir

        mt5_cfg_path = os.path.join(self.project_dir, "mt5_config.json")
        mt5_terminal_path = ""
        try:
            cfg = _safe_read_json(mt5_cfg_path) or {}
            mt5_terminal_path = str(cfg.get("terminal_path", "") or "").strip()
        except Exception:
            mt5_terminal_path = ""

        try:
            # IMPORTANT: pass `coin` so neural_trainer trains the correct market instead of defaulting to BTC
            cmd = [sys.executable, "-u", info.path, coin]
            if trainer_name.lower() == "pt_mt5_trainer.py":
                main_dir = os.path.abspath(str(self.settings.get("main_neural_dir", self.project_dir)))
                memory_dir = main_dir if coin == "BTC" else os.path.join(main_dir, coin)
                os.makedirs(memory_dir, exist_ok=True)

                # Use --incremental by default to avoid wiping memory on duplicate runs.
                # When the user checks "Force Retrain (ignore age)", skip --incremental
                # so training always runs regardless of memory file age.
                force_retrain = False
                try:
                    force_retrain = bool(getattr(self, '_force_retrain_var', None) and
                                         self._force_retrain_var.get())
                except Exception:
                    pass

                cmd = [
                    sys.executable,
                    "-u",
                    info.path,
                    "--coin",
                    coin,
                    "--timeframe",
                    str(self.settings.get("default_timeframe", "1hour")),
                    "--memory-dir",
                    memory_dir,
                    "--min-move",    "0.20",
                    "--lookback-days", str(self.settings.get("trainer_lookback_days", 90)),
                ]
                if not force_retrain:
                    cmd += ["--incremental", "--incremental-max-age", "6"]
                else:
                    try:
                        self.status.config(text=f"Force retraining {coin} (ignoring age limit)...")
                    except Exception:
                        pass
                if mt5_terminal_path:
                    cmd += ["--terminal-path", mt5_terminal_path]

            info.proc = subprocess.Popen(
                cmd,
                cwd=trainer_cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            t = threading.Thread(target=self._reader_thread, args=(info.proc, q, f"[{coin}] "), daemon=True)
            t.start()

            self.trainers[coin] = LogProc(info=info, log_q=q, thread=t, is_trainer=True, coin=coin)
        except Exception as e:
            messagebox.showerror("Failed to start", f"Trainer for {coin} failed to start:\n{e}")




    def stop_trainer_for_selected_coin(self) -> None:
        coin = (self.trainer_coin_var.get() or "").strip().upper()
        lp = self.trainers.get(coin)
        if not lp or not lp.info.proc or lp.info.proc.poll() is not None:
            return
        try:
            lp.info.proc.terminate()
        except Exception:
            pass


    def stop_all_scripts(self) -> None:
        # Cancel any pending "wait for runner then start trader"
        self._auto_start_trader_pending = False

        self.stop_neural()
        self.stop_trader()

        # Also reset the runner-ready gate file (best-effort)
        try:
            with open(self.runner_ready_path, "w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time(), "ready": False, "stage": "stopped"}, f)
        except Exception:
            pass


    def _on_timeframe_changed(self, event) -> None:
        """
        Immediate redraw when the user changes a timeframe in any CandleChart.
        Avoids waiting for the chart_refresh_seconds throttle in _tick().
        """
        try:
            chart = getattr(event, "widget", None)
            if not isinstance(chart, CandleChart):
                return

            coin = getattr(chart, "coin", None)
            if not coin:
                return

            self.coin_folders = build_coin_folders(self.settings["main_neural_dir"], self.coins)

            pos = self._last_positions.get(coin, {}) if isinstance(self._last_positions, dict) else {}
            buy_px = pos.get("current_buy_price", None)
            sell_px = pos.get("current_sell_price", None)
            trail_line = pos.get("trail_line", None)
            dca_line_price = pos.get("dca_line_price", None)
            avg_cost_basis = pos.get("avg_cost_basis", None)

            chart.refresh(
                self.coin_folders,
                current_buy_price=buy_px,
                current_sell_price=sell_px,
                trail_line=trail_line,
                dca_line_price=dca_line_price,
                avg_cost_basis=avg_cost_basis,
            )

            # Keep the periodic refresh behavior consistent (prevents an immediate full refresh right after this).
            self._last_chart_refresh = time.time()
        except Exception:
            pass

    def _build_backtest_tab(self, parent: tk.Widget) -> None:
        """Premium Backtest panel -- uses pt_mt5_backtest.py engine."""
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True, padx=14, pady=10)

        # ── Settings card ──────────────────────────────────────────────────────────────────
        ctrl = ttk.LabelFrame(outer, text="  BACKTEST CONFIGURATION  ", style="Card.TLabelframe")
        ctrl.pack(fill="x", pady=(0, 10))

        row1 = ttk.Frame(ctrl)
        row1.pack(fill="x", padx=12, pady=(8, 4))

        # Coin
        ttk.Label(row1, text="Coin:").pack(side="left")
        self._bt_coin_var = tk.StringVar(value=self.coins[0] if self.coins else "BTC")
        coin_cb = ttk.Combobox(row1, textvariable=self._bt_coin_var,
                               values=self.coins, width=7, state="readonly")
        coin_cb.pack(side="left", padx=(4, 16))

        # Timeframe
        ttk.Label(row1, text="Timeframe:").pack(side="left")
        self._bt_tf_var = tk.StringVar(value=self.settings.get("default_timeframe", "1hour"))
        ttk.Combobox(row1, textvariable=self._bt_tf_var,
                     values=["1min","5min","15min","30min","1hour","4hour","1day"],
                     width=8, state="readonly").pack(side="left", padx=(4, 16))

        # Period
        ttk.Label(row1, text="Period:").pack(side="left")
        self._bt_period_var = tk.StringVar(value="Last 90 days")
        period_cb = ttk.Combobox(row1, textvariable=self._bt_period_var,
                                  values=["Last 7 days","Last 30 days","Last 60 days",
                                          "Last 90 days","Last 180 days","Last 365 days",
                                          "Custom range"],
                                  width=14, state="readonly")
        period_cb.pack(side="left", padx=(4, 0))
        period_cb.bind("<<ComboboxSelected>>", lambda _e: self._bt_toggle_custom_dates())

        # Custom date row (hidden by default)
        self._bt_date_frame = ttk.Frame(ctrl)
        ttk.Label(self._bt_date_frame, text="Start (YYYY-MM-DD):").pack(side="left")
        self._bt_start_var = tk.StringVar(value="")
        ttk.Entry(self._bt_date_frame, textvariable=self._bt_start_var, width=12).pack(side="left", padx=(4, 16))
        ttk.Label(self._bt_date_frame, text="End (YYYY-MM-DD):").pack(side="left")
        self._bt_end_var = tk.StringVar(value="")
        ttk.Entry(self._bt_date_frame, textvariable=self._bt_end_var, width=12).pack(side="left", padx=(4, 0))
        self._bt_date_frame.pack_forget()

        row2 = ttk.Frame(ctrl)
        row2.pack(fill="x", padx=12, pady=4)

        # SL / TP / Open level
        for lbl, attr, default, width in [
            ("Open Level:",   "_bt_open_var",  "5",   4),
            ("Close Level:",  "_bt_close_var", "2",   4),
            ("SL %:",         "_bt_sl_var",    "2.0", 5),
            ("TP %:",         "_bt_tp_var",    "3.0", 5),
            ("Partial TP %:", "_bt_ptp_var",   "1.8", 5),
            ("Trail Trig %:", "_bt_trail_var", "1.5", 5),
            ("Balance $:",    "_bt_bal_var",   "10000", 7),
        ]:
            ttk.Label(row2, text=lbl).pack(side="left", padx=(0, 2))
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            ttk.Entry(row2, textvariable=var, width=width).pack(side="left", padx=(0, 12))

        row3 = ttk.Frame(ctrl)
        row3.pack(fill="x", padx=12, pady=(4, 10))

        # Direction toggles
        self._bt_long_var  = tk.BooleanVar(value=True)
        self._bt_short_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="Enable Long",  variable=self._bt_long_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row3, text="Enable Short", variable=self._bt_short_var).pack(side="left", padx=(0, 24))

        self._bt_run_btn = ttk.Button(row3, text="  Run Backtest  ",
                                       style="Primary.TButton",
                                       command=self._run_backtest)
        self._bt_run_btn.pack(side="left", padx=(0, 12))

        self._bt_status_lbl = ttk.Label(row3, text="Ready", style="Subtle.TLabel")
        self._bt_status_lbl.pack(side="left")

        # ── Progress bar ──────────────────────────────────────────────────────────────────
        self._bt_progress = ttk.Progressbar(outer, mode="indeterminate", length=200)
        self._bt_progress.pack(fill="x", pady=(0, 8))

        # ── KPI summary cards ───────────────────────────────────────────────────────────
        kpi_frame = ttk.Frame(outer)
        kpi_frame.pack(fill="x", pady=(0, 10))

        self._bt_summary_labels: Dict[str, ttk.Label] = {}
        self._bt_kpi_cards: Dict[str, ttk.Frame] = {}

        kpi_defs = [
            ("total_pnl_pct",     "Total P&L %",    DARK_ACCENT),
            ("win_rate_pct",      "Win Rate %",      DARK_ACCENT2),
            ("total_trades",      "Trades",          DARK_FG),
            ("profit_factor",     "Profit Factor",   DARK_ACCENT2),
            ("max_drawdown_pct",  "Max Drawdown %",  DARK_DANGER),
            ("sharpe_ratio",      "Sharpe Ratio",    DARK_WARNING),
            ("final_balance",     "Final Balance $", DARK_ACCENT),
        ]
        for key, label, color in kpi_defs:
            card = tk.Frame(kpi_frame, bg=DARK_PANEL,
                            highlightbackground=DARK_BORDER, highlightthickness=1)
            card.pack(side="left", padx=4, pady=2, ipady=6, ipadx=8)
            lbl_title = tk.Label(card, text=label, bg=DARK_PANEL,
                                  fg=DARK_MUTED, font=(self._ui_font_family, 8))
            lbl_title.pack()
            lbl_val = tk.Label(card, text="--", bg=DARK_PANEL,
                                fg=color, font=(self._ui_font_family, 15, "bold"))
            lbl_val.pack()
            self._bt_summary_labels[key] = lbl_val
            self._bt_kpi_cards[key] = card

        # ── Trade log treeview ──────────────────────────────────────────────────────────
        logf = ttk.LabelFrame(outer, text="  TRADE LOG  ", style="Card.TLabelframe")
        logf.pack(fill="both", expand=True)

        cols = ("Entry Time", "Exit Time", "Side", "Entry $", "Exit $",
                "P&L %", "P&L $", "Reason")
        self._bt_tree = ttk.Treeview(logf, columns=cols, show="headings", height=14)
        widths = [130, 130, 55, 95, 95, 72, 82, 110]
        for col, w in zip(cols, widths):
            self._bt_tree.heading(col, text=col,
                command=lambda c=col: self._bt_sort_column(c))
            self._bt_tree.column(col, width=w, anchor="center")

        # Row color tags
        self._bt_tree.tag_configure("win",      foreground=DARK_SUCCESS, background=DARK_PANEL)
        self._bt_tree.tag_configure("loss",     foreground=DARK_DANGER,  background=DARK_PANEL)
        self._bt_tree.tag_configure("neutral",  foreground=DARK_FG,      background=DARK_PANEL)
        self._bt_tree.tag_configure("long_tag", foreground=DARK_ACCENT2, background=DARK_PANEL)
        self._bt_tree.tag_configure("short_tag",foreground=DARK_ORANGE,  background=DARK_PANEL)

        vsb = ttk.Scrollbar(logf, orient="vertical",   command=self._bt_tree.yview)
        hsb = ttk.Scrollbar(logf, orient="horizontal", command=self._bt_tree.xview)
        self._bt_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._bt_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        logf.grid_rowconfigure(0, weight=1)
        logf.grid_columnconfigure(0, weight=1)

        self._bt_sort_col   = None
        self._bt_sort_asc   = True

    def _bt_sort_column(self, col: str) -> None:
        """Sort backtest trade log by column header click."""
        try:
            items = [(self._bt_tree.set(k, col), k)
                     for k in self._bt_tree.get_children("")]
            try:
                items.sort(key=lambda t: float(t[0].replace("$","").replace("%","").replace(",","")),
                           reverse=not self._bt_sort_asc if self._bt_sort_col == col else False)
            except Exception:
                items.sort(key=lambda t: t[0],
                           reverse=not self._bt_sort_asc if self._bt_sort_col == col else False)

            for idx, (_, k) in enumerate(items):
                self._bt_tree.move(k, "", idx)

            self._bt_sort_asc = not self._bt_sort_asc if self._bt_sort_col == col else True
            self._bt_sort_col = col
        except Exception:
            pass

    def _bt_toggle_custom_dates(self) -> None:
        if "Custom" in self._bt_period_var.get():
            self._bt_date_frame.pack(fill="x", padx=12, pady=2,
                                      after=self._bt_date_frame.master.winfo_children()[0])
        else:
            self._bt_date_frame.pack_forget()

    def _run_backtest(self) -> None:
        try:
            self._bt_run_btn.configure(state="disabled")
            self._bt_status_lbl.configure(text="Running backtest...", foreground=DARK_ACCENT2)
            self._bt_progress.start(10)
            # Reset KPIs
            for lbl in self._bt_summary_labels.values():
                lbl.configure(text="...")
        except Exception:
            pass
        threading.Thread(target=self._backtest_worker, daemon=True).start()

    def _backtest_worker(self) -> None:
        try:
            coin   = self._bt_coin_var.get().strip().upper()
            tf     = self._bt_tf_var.get().strip()
            period = self._bt_period_var.get()

            if "Custom" in period:
                start_s   = self._bt_start_var.get().strip()
                end_s     = self._bt_end_var.get().strip()
                extra_args = ["--start", start_s, "--end", end_s]
            else:
                try:
                    days = int(period.split()[1])
                except Exception:
                    days = 90
                extra_args = ["--lookback-days", str(days)]

            # Collect all extra params safely
            def _get(attr: str, default: str = "0") -> str:
                try:
                    return getattr(self, attr).get().strip() or default
                except Exception:
                    return default

            bt_script = os.path.join(self.project_dir, "pt_mt5_backtest.py")
            if not os.path.isfile(bt_script):
                # Try mt5 subfolder
                bt_script = os.path.join(self.project_dir, "mt5", "pt_mt5_backtest.py")
            if not os.path.isfile(bt_script):
                def _missing():
                    self._bt_progress.stop()
                    self._bt_status_lbl.configure(
                        text="ERROR: pt_mt5_backtest.py not found in project folder",
                        foreground=DARK_DANGER)
                    self._bt_run_btn.configure(state="normal")
                self.after(0, _missing)
                return

            logs_dir = os.path.join(self.project_dir, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            out_json = os.path.join(logs_dir, f"bt_{coin}_{tf}_{int(time.time())}.json")

            _main_dir = str(self.settings.get("main_neural_dir", self.project_dir) or self.project_dir)
            mem_dir = _main_dir if coin == "BTC" else os.path.join(_main_dir, coin)

            config_path = os.path.join(self.project_dir, "mt5_config.json")
            if not os.path.isfile(config_path):
                config_path = os.path.join(self.project_dir, "mt5", "mt5_config.json")

            cmd = [
                sys.executable, "-u", bt_script,
                "--coin",          coin,
                "--timeframe",     tf,
                "--memory-dir",    mem_dir,
                "--output-json",   out_json,
                "--sl-pct",        _get("_bt_sl_var",    "2.0"),
                "--tp-pct",        _get("_bt_tp_var",    "3.0"),
                "--partial-tp-pct",_get("_bt_ptp_var",   "1.8"),
                "--trail-trigger", _get("_bt_trail_var", "1.5"),
                "--open-threshold",_get("_bt_open_var",  "3"),
                "--close-threshold",_get("_bt_close_var","2"),
                "--balance",       _get("_bt_bal_var",   "10000"),
                *extra_args,
            ]
            if os.path.isfile(config_path):
                cmd += ["--config", config_path]
            try:
                if not self._bt_long_var.get():
                    cmd.append("--no-long")
                if not self._bt_short_var.get():
                    cmd.append("--no-short")
            except Exception:
                pass

            cp = subprocess.run(
                cmd,
                cwd=self.project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=600,
                encoding="utf-8",
                errors="replace",
            )

            out   = cp.stdout or ""
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]

            bt_data: Dict[str, Any] = {}
            if cp.returncode == 0 and os.path.isfile(out_json):
                try:
                    with open(out_json, "r", encoding="utf-8") as f:
                        bt_data = json.load(f) or {}
                except Exception:
                    bt_data = {}

            summary = bt_data.get("summary", {}) if isinstance(bt_data, dict) else {}
            trades  = bt_data.get("trades",  []) if isinstance(bt_data, dict) else []

            def _fv(k: str, default: float = 0.0) -> float:
                try:
                    return float(summary.get(k, default) or default)
                except Exception:
                    return default

            total_pnl    = _fv("total_pnl_pct")
            win_rate     = _fv("win_rate_pct")
            total_trades = int(_fv("total_trades"))
            pf           = _fv("profit_factor")
            max_dd       = _fv("max_drawdown_pct")
            sharpe       = _fv("sharpe_ratio")
            final_bal    = _fv("final_balance")
            init_bal     = _fv("initial_balance", 10000.0)

            def _fmt_ts(ts: Any) -> str:
                try:
                    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    return "-"

            def update_ui() -> None:
                self._bt_progress.stop()

                if cp.returncode != 0 and not summary:
                    tail = " | ".join(lines[-3:]) if lines else "Unknown error"
                    self._bt_status_lbl.configure(
                        text=f"Error: {tail}", foreground=DARK_DANGER)
                    self._bt_run_btn.configure(state="normal")
                    for lbl in self._bt_summary_labels.values():
                        lbl.configure(text="--")
                    return

                if not summary:
                    self._bt_status_lbl.configure(
                        text="Finished -- no results (check memory files exist)",
                        foreground=DARK_WARNING)
                    self._bt_run_btn.configure(state="normal")
                    return

                # Update KPI cards
                self._bt_summary_labels["total_pnl_pct"].configure(
                    text=f"{total_pnl:+.2f}%",
                    foreground=(DARK_SUCCESS if total_pnl >= 0 else DARK_DANGER))
                self._bt_summary_labels["win_rate_pct"].configure(
                    text=f"{win_rate:.1f}%")
                self._bt_summary_labels["total_trades"].configure(
                    text=str(total_trades))
                self._bt_summary_labels["profit_factor"].configure(
                    text=f"{pf:.3f}",
                    foreground=(DARK_SUCCESS if pf >= 1.0 else DARK_DANGER))
                self._bt_summary_labels["max_drawdown_pct"].configure(
                    text=f"{max_dd:.2f}%",
                    foreground=(DARK_WARNING if max_dd < 15 else DARK_DANGER))
                self._bt_summary_labels["sharpe_ratio"].configure(
                    text=f"{sharpe:.3f}",
                    foreground=(DARK_SUCCESS if sharpe >= 1.0 else
                                DARK_WARNING if sharpe >= 0 else DARK_DANGER))
                self._bt_summary_labels["final_balance"].configure(
                    text=f"${final_bal:,.2f}",
                    foreground=(DARK_SUCCESS if final_bal >= init_bal else DARK_DANGER))

                # Populate trade log
                for iid in self._bt_tree.get_children():
                    self._bt_tree.delete(iid)

                for t in trades:
                    pnl_pct = float(t.get("pnl_pct", 0.0))
                    pnl_usd = float(t.get("pnl_usd", 0.0))
                    side_str = str(t.get("side", "-")).upper()

                    tag = "win" if pnl_usd > 0 else ("loss" if pnl_usd < 0 else "neutral")

                    self._bt_tree.insert(
                        "", "end",
                        values=(
                            _fmt_ts(t.get("entry_ts")),
                            _fmt_ts(t.get("exit_ts")),
                            side_str,
                            f"{float(t.get('entry', 0.0)):.5f}",
                            f"{float(t.get('exit',  0.0)):.5f}",
                            f"{pnl_pct:+.2f}%",
                            f"${pnl_usd:+.2f}",
                            str(t.get("reason", "-")),
                        ),
                        tags=(tag,),
                    )

                self._bt_status_lbl.configure(
                    text=(f"Done   {total_trades} trades  |  "
                          f"P&L: {total_pnl:+.2f}%  |  "
                          f"WR: {win_rate:.1f}%  |  "
                          f"PF: {pf:.3f}  |  "
                          f"DD: {max_dd:.2f}%"),
                    foreground=(DARK_SUCCESS if total_pnl >= 0 else DARK_WARNING),
                )
                self._bt_run_btn.configure(state="normal")

            self.after(0, update_ui)

        except Exception as e:
            _err_msg = str(e)
            def _err(msg=_err_msg):
                try:
                    self._bt_progress.stop()
                except Exception:
                    pass
                self._bt_status_lbl.configure(text=f"Error: {msg}", foreground=DARK_DANGER)
                self._bt_run_btn.configure(state="normal")
            self.after(0, _err)


    # ---- refresh loop ----
    def _append_log_line(self, widget: tk.Text, line: str) -> None:
        s = str(line or "").rstrip()
        if not s:
            return

        tag = "info"
        up = s.upper()
        if "[ERROR]" in up:
            tag = "error"
        elif "[WARN]" in up:
            tag = "warn"
        elif "AUTHORIZATION SUCCEEDED" in up or "[OK]" in up:
            tag = "success"

        try:
            widget.insert("end", s + "\n", tag)
        except Exception:
            widget.insert("end", s + "\n")

    def _drain_queue_to_text(self, q: "queue.Queue[str]", txt: tk.Text, max_lines: int = 2500) -> None:

        try:
            changed = False
            while True:
                line = q.get_nowait()
                self._append_log_line(txt, line)
                changed = True
        except queue.Empty:
            pass
        except Exception:
            pass

        if changed:
            # trim very old lines
            try:
                current = int(txt.index("end-1c").split(".")[0])
                if current > max_lines:
                    txt.delete("1.0", f"{current - max_lines}.0")
            except Exception:
                pass
            txt.see("end")

    def _tick(self) -> None:
        # Keep managed long-running processes alive unless user explicitly stopped them.
        self._supervise_process(self.proc_neural, self.runner_log_q, "[RUNNER] ")
        self._supervise_process(self.proc_trader, self.trader_log_q, "[TRADER] ")

        # process labels
        neural_running = bool(self.proc_neural.proc and self.proc_neural.proc.poll() is None)
        trader_running = bool(self.proc_trader.proc and self.proc_trader.proc.poll() is None)

        self.lbl_neural.config(text=f"Neural: {'running' if neural_running else 'stopped'}")
        self.lbl_trader.config(text=f"Trader: {'running' if trader_running else 'stopped'}")

        try:
            self.lbl_clock.config(text=time.strftime("%H:%M:%S"))
        except Exception:
            pass

        try:
            if neural_running:
                self.led_neural.set_state("running")
            elif bool(getattr(self, "_auto_start_trader_pending", False)):
                self.led_neural.set_state("starting")
            else:
                self.led_neural.set_state("stopped")

            self.led_trader.set_state("running" if trader_running else "stopped")
        except Exception:
            pass

        try:
            self.lbl_neural.config(foreground=(DARK_SUCCESS if neural_running else DARK_MUTED))
            self.lbl_trader.config(foreground=(DARK_SUCCESS if trader_running else DARK_MUTED))
        except Exception:
            pass

        # Start All is now a toggle (Start/Stop)
        try:
            if hasattr(self, "btn_toggle_all") and self.btn_toggle_all:
                if neural_running or trader_running or bool(getattr(self, "_auto_start_trader_pending", False)):
                    self.btn_toggle_all.config(text="■ Stop All")
                    self.btn_toggle_all.configure(style="Danger.TButton")
                else:
                    self.btn_toggle_all.config(text="▶ Start All")
                    self.btn_toggle_all.configure(style="Primary.TButton")
        except Exception:
            pass

        # --- flow gating: Train -> Start All ---
        status_map = self._training_status_map()
        all_trained = all(v == "TRAINED" for v in status_map.values()) if status_map else False

        # Disable Start All until training is done (but always allow it if something is already running/pending,
        # so the user can still stop everything).
        can_toggle_all = True
        if (not all_trained) and (not neural_running) and (not trader_running) and (not self._auto_start_trader_pending):
            can_toggle_all = False

        try:
            self.btn_toggle_all.configure(state=("normal" if can_toggle_all else "disabled"))
        except Exception:
            pass

        # Training overview + per-coin list
        try:
            training_running = [c for c, s in status_map.items() if s == "TRAINING"]
            not_trained = [c for c, s in status_map.items() if s == "NOT TRAINED"]

            if training_running:
                self.lbl_training_overview.config(text=f"Training: RUNNING ({', '.join(training_running)})")
                self.lbl_training_overview.config(foreground=DARK_ACCENT2)
            elif not_trained:
                self.lbl_training_overview.config(text=f"Training: REQUIRED ({len(not_trained)} not trained)")
                self.lbl_training_overview.config(foreground=DARK_DANGER)
            else:
                self.lbl_training_overview.config(text="Training: READY (all trained)")
                self.lbl_training_overview.config(foreground=DARK_SUCCESS)

            # show each coin status (ONLY redraw the list if it actually changed)
            sig = tuple((c, status_map.get(c, "N/A")) for c in self.coins)
            if getattr(self, "_last_training_sig", None) != sig:
                self._last_training_sig = sig
                self.training_list.delete(0, "end")
                for c, st in sig:
                    self.training_list.insert("end", f"{c}: {st}")

            # show gating hint (Start All handles the runner->ready->trader sequence)
            if not all_trained:
                self.lbl_flow_hint.config(text="Flow: Train All required → then Start All")
            elif self._auto_start_trader_pending:
                self.lbl_flow_hint.config(text="Flow: Starting runner → waiting for ready → trader will auto-start")
            elif neural_running or trader_running:
                self.lbl_flow_hint.config(text="Flow: Running (use the button to stop)")
            else:
                self.lbl_flow_hint.config(text="Flow: Start All")
        except Exception:
            pass

        # neural overview bars (mtime-cached inside)
        self._refresh_neural_overview()

        # trader status -> current trades table (now mtime-cached inside)
        self._refresh_trader_status()

        # pnl ledger -> realized profit (now mtime-cached inside)
        self._refresh_pnl()

        # trade history (now mtime-cached inside)
        self._refresh_trade_history()


        # charts (throttle)
        now = time.time()
        if (now - self._last_chart_refresh) >= float(self.settings.get("chart_refresh_seconds", 10.0)):
            # account value chart (internally mtime-cached already)
            try:
                if self.account_chart:
                    self.account_chart.refresh()
            except Exception:
                pass

            # Only rebuild coin_folders when inputs change (avoids directory scans every refresh)
            try:
                cf_sig = (self.settings.get("main_neural_dir"), tuple(self.coins))
                if getattr(self, "_coin_folders_sig", None) != cf_sig:
                    self._coin_folders_sig = cf_sig
                    self.coin_folders = build_coin_folders(self.settings["main_neural_dir"], self.coins)
            except Exception:
                try:
                    self.coin_folders = build_coin_folders(self.settings["main_neural_dir"], self.coins)
                except Exception:
                    pass

            # Refresh ONLY the currently visible coin tab (prevents O(N_coins) network/plot stalls)
            selected_tab = None

            # Primary: our custom chart pages (multi-row tab buttons)
            try:
                selected_tab = getattr(self, "_current_chart_page", None)
            except Exception:
                selected_tab = None

            # Fallback: old notebook-based UI (if it exists)
            if not selected_tab:
                try:
                    if hasattr(self, "nb") and self.nb:
                        selected_tab = self.nb.tab(self.nb.select(), "text")
                except Exception:
                    selected_tab = None

            if selected_tab and str(selected_tab).strip().upper() != "ACCOUNT":
                coin = str(selected_tab).strip().upper()
                chart = self.charts.get(coin)
                if chart:
                    pos = self._last_positions.get(coin, {}) if isinstance(self._last_positions, dict) else {}
                    buy_px = pos.get("current_buy_price", None)
                    sell_px = pos.get("current_sell_price", None)
                    trail_line = pos.get("trail_line", None)
                    dca_line_price = pos.get("dca_line_price", None)
                    avg_cost_basis = pos.get("avg_cost_basis", None)

                    try:
                        chart.refresh(
                            self.coin_folders,
                            current_buy_price=buy_px,
                            current_sell_price=sell_px,
                            trail_line=trail_line,
                            dca_line_price=dca_line_price,
                            avg_cost_basis=avg_cost_basis,
                        )
                    except Exception:
                        pass



            self._last_chart_refresh = now

        # drain logs into panes
        self._drain_queue_to_text(self.runner_log_q, self.runner_text)
        self._drain_queue_to_text(self.trader_log_q, self.trader_text)

        # trainer logs: show selected trainer output
        try:
            sel = (self.trainer_coin_var.get() or "").strip().upper()
            running = [c for c, lp in self.trainers.items() if lp.info.proc and lp.info.proc.poll() is None]
            self.trainer_status_lbl.config(text=f"running: {', '.join(running)}" if running else "(no trainers running)")

            lp = self.trainers.get(sel)
            if lp:
                self._drain_queue_to_text(lp.log_q, self.trainer_text)
        except Exception:
            pass

        self.status.config(text=f"{_now_str()} | hub_dir={self.hub_dir}")
        self.after(int(float(self.settings.get("ui_refresh_seconds", 1.0)) * 1000), self._tick)



    def _refresh_trader_status(self) -> None:
        # mtime cache: rebuilding the whole tree every tick is expensive with many rows
        try:
            mtime = os.path.getmtime(self.trader_status_path)
        except Exception:
            mtime = None

        if getattr(self, "_last_trader_status_mtime", object()) == mtime:
            return
        self._last_trader_status_mtime = mtime

        data = _safe_read_json(self.trader_status_path)
        if not data:
            self.lbl_last_status.config(text="Last status: N/A (no trader_status.json yet)")

            # account summary (right-side status area)
            try:
                self.lbl_acct_total_value.config(text="N/A")
                self.lbl_acct_holdings_value.config(text="N/A")
                self.lbl_acct_buying_power.config(text="N/A")
                self.lbl_acct_percent_in_trade.config(text="N/A")
                self.lbl_acct_total_positions.config(text="Open Positions: N/A")

                # DCA affordability
                self.lbl_acct_dca_spread.config(text="DCA Levels (spread): N/A")
                self.lbl_acct_dca_single.config(text="DCA Levels (single): N/A")
            except Exception:
                pass

            # clear tree (once; subsequent ticks are mtime-short-circuited)
            for iid in self.trades_tree.get_children():
                self.trades_tree.delete(iid)
            return



        ts = data.get("timestamp")
        try:
            if isinstance(ts, (int, float)):
                self.lbl_last_status.config(text=f"Last status: {time.strftime('%H:%M:%S', time.localtime(ts))}")
            else:
                self.lbl_last_status.config(text="Last status: (unknown timestamp)")
        except Exception:
            self.lbl_last_status.config(text="Last status: (timestamp parse error)")

        # --- account summary (same info the trader prints above current trades) ---
        acct = data.get("account", {}) or {}
        try:
            total_val = float(acct.get("total_account_value", 0.0) or 0.0)

            self._last_total_account_value = total_val

            self.lbl_acct_total_value.config(
                text=f"{_fmt_money(acct.get('total_account_value', None))}"
            )
            self.lbl_acct_holdings_value.config(
                text=f"{_fmt_money(acct.get('holdings_sell_value', None))}"
            )
            self.lbl_acct_buying_power.config(
                text=f"{_fmt_money(acct.get('buying_power', None))}"
            )

            pit = acct.get("percent_in_trade", None)
            try:
                pit_txt = f"{float(pit):.2f}%"
            except Exception:
                pit_txt = "N/A"
            self.lbl_acct_percent_in_trade.config(text=pit_txt)

            tp = acct.get("total_positions", None)
            try:
                tp_txt = str(int(tp))
            except Exception:
                tp_txt = "N/A"
            self.lbl_acct_total_positions.config(text=f"Open Positions: {tp_txt}")


            # -------------------------
            # DCA affordability
            # - Entry allocation mirrors pt_trader.py:
            #     total_val * ((start_allocation_pct/100) / N) with min $0.50
            # - Each DCA buy mirrors pt_trader.py: dca_amount = value * dca multiplier  (=> total scales ~(1+multiplier)x per DCA)
            # -------------------------
            coins = getattr(self, "coins", None) or []
            n = len(coins)
            spread_levels = 0
            single_levels = 0

            if total_val > 0.0:
                alloc_pct = float(self.settings.get("start_allocation_pct", 0.005) or 0.005)
                if alloc_pct < 0.0:
                    alloc_pct = 0.0
                alloc_frac = alloc_pct / 100.0

                dca_mult = float(self.settings.get("dca_multiplier", 2.0) or 2.0)
                if dca_mult < 0.0:
                    dca_mult = 0.0
                dca_factor = 1.0 + dca_mult

                # Spread across all coins

                alloc_spread = total_val * alloc_frac
                if alloc_spread < 0.5:
                    alloc_spread = 0.5

                required = alloc_spread * n  # initial buys for all coins
                while required > 0.0 and (required * dca_factor) <= (total_val + 1e-9):
                    required *= dca_factor
                    spread_levels += 1


                # All DCA into a single coin
                alloc_single = total_val * alloc_frac
                if alloc_single < 0.5:
                    alloc_single = 0.5

                required = alloc_single  # initial buy for one coin
                while required > 0.0 and (required * dca_factor) <= (total_val + 1e-9):
                    required *= dca_factor
                    single_levels += 1



            # Show labels + number (one line each)
            self.lbl_acct_dca_spread.config(text=f"DCA Levels (spread): {spread_levels}")
            self.lbl_acct_dca_single.config(text=f"DCA Levels (single): {single_levels}")


        except Exception:
            pass


        positions = data.get("positions", {}) or {}
        self._last_positions = positions

        # --- precompute per-coin DCA count in rolling 24h (and after last SELL for that coin) ---
        dca_24h_by_coin: Dict[str, int] = {}
        try:
            now = time.time()
            window_floor = now - (24 * 3600)

            trades = _read_trade_history_jsonl(self.trade_history_path) if self.trade_history_path else []

            last_sell_ts: Dict[str, float] = {}
            for tr in trades:
                sym = str(tr.get("symbol", "")).upper().strip()
                base = sym.split("-")[0].strip() if sym else ""
                if not base:
                    continue

                side = str(tr.get("side", "")).lower().strip()
                if side != "sell":
                    continue

                try:
                    tsf = float(tr.get("ts", 0))
                except Exception:
                    continue

                prev = float(last_sell_ts.get(base, 0.0))
                if tsf > prev:
                    last_sell_ts[base] = tsf

            for tr in trades:
                sym = str(tr.get("symbol", "")).upper().strip()
                base = sym.split("-")[0].strip() if sym else ""
                if not base:
                    continue

                side = str(tr.get("side", "")).lower().strip()
                if side != "buy":
                    continue

                tag = str(tr.get("tag") or "").upper().strip()
                if tag != "DCA":
                    continue

                try:
                    tsf = float(tr.get("ts", 0))
                except Exception:
                    continue

                start_ts = max(window_floor, float(last_sell_ts.get(base, 0.0)))
                if tsf >= start_ts:
                    dca_24h_by_coin[base] = int(dca_24h_by_coin.get(base, 0)) + 1
        except Exception:
            dca_24h_by_coin = {}

        # rebuild tree (only when file changes)
        for iid in self.trades_tree.get_children():
            self.trades_tree.delete(iid)

        for sym, pos in positions.items():
            coin = sym
            qty = pos.get("quantity", 0.0)

            # Hide "not in trade" rows (0 qty), but keep them in _last_positions for chart overlays
            try:
                if float(qty) <= 0.0:
                    continue
            except Exception:
                continue

            value = pos.get("value_usd", 0.0)
            avg_cost = pos.get("avg_cost_basis", 0.0)

            buy_price = pos.get("current_buy_price", 0.0)
            buy_pnl = pos.get("gain_loss_pct_buy", 0.0)

            sell_price = pos.get("current_sell_price", 0.0)
            sell_pnl = pos.get("gain_loss_pct_sell", 0.0)

            dca_stages = pos.get("dca_triggered_stages", 0)
            dca_24h = int(dca_24h_by_coin.get(str(coin).upper().strip(), 0))

            # Display + heading reflect the current max DCA setting (hot-reload friendly)
            try:
                max_dca_24h = int(float(self.settings.get("max_dca_buys_per_24h", DEFAULT_SETTINGS.get("max_dca_buys_per_24h", 2)) or 2))
            except Exception:
                max_dca_24h = int(DEFAULT_SETTINGS.get("max_dca_buys_per_24h", 2) or 2)
            if max_dca_24h < 0:
                max_dca_24h = 0
            try:
                self.trades_tree.heading("dca_24h", text=f"DCA 24h (max {max_dca_24h})")
            except Exception:
                pass
            dca_24h_display = f"{dca_24h}/{max_dca_24h}"


            # Display + heading reflect trailing PM settings (hot-reload friendly)
            try:
                pm0 = float(self.settings.get("pm_start_pct_no_dca", DEFAULT_SETTINGS.get("pm_start_pct_no_dca", 5.0)) or 5.0)
                pm1 = float(self.settings.get("pm_start_pct_with_dca", DEFAULT_SETTINGS.get("pm_start_pct_with_dca", 2.5)) or 2.5)
                tg = float(self.settings.get("trailing_gap_pct", DEFAULT_SETTINGS.get("trailing_gap_pct", 0.5)) or 0.5)
                self.trades_tree.heading("trail_line", text=f"Trail Line (start {pm0:g}/{pm1:g}%, gap {tg:g}%)")
            except Exception:
                pass


            next_dca = pos.get("next_dca_display", "")

            trail_line = pos.get("trail_line", 0.0)

            try:
                sell_pnl_val = float(sell_pnl)
            except Exception:
                sell_pnl_val = 0.0
            row_tag = "profit" if sell_pnl_val > 0 else ("loss" if sell_pnl_val < 0 else "neutral")

            self.trades_tree.insert(
                "",
                "end",
                tags=(row_tag,),
                values=(
                    coin,
                    f"{qty:.8f}".rstrip("0").rstrip("."),
                    _fmt_money(value),       # position value (USD)
                    _fmt_price(avg_cost),    # per-unit price (USD) -> dynamic decimals
                    _fmt_price(buy_price),
                    _fmt_pct(buy_pnl),
                    _fmt_price(sell_price),
                    _fmt_pct(sell_pnl),
                    dca_stages,
                    dca_24h_display,
                    next_dca,
                    _fmt_price(trail_line),  # trail line is a price level
                ),
            )









    def _refresh_pnl(self) -> None:
        # mtime cache: avoid reading/parsing every tick
        try:
            mtime = os.path.getmtime(self.pnl_ledger_path)
        except Exception:
            mtime = None

        if getattr(self, "_last_pnl_mtime", object()) == mtime:
            return
        self._last_pnl_mtime = mtime

        data = _safe_read_json(self.pnl_ledger_path)
        if not data:
            self.lbl_pnl.config(text="Realized P&L: N/A", foreground=DARK_FG)
            return
        total = float(data.get("total_realized_profit_usd", 0.0))
        self.lbl_pnl.config(
            text=f"Realized P&L: {_fmt_money(total)}",
            foreground=(DARK_SUCCESS if total >= 0 else DARK_DANGER),
        )


    def _refresh_trade_history(self) -> None:
        # mtime cache: avoid reading/parsing/rebuilding the list every tick
        try:
            mtime = os.path.getmtime(self.trade_history_path)
        except Exception:
            mtime = None

        if getattr(self, "_last_trade_history_mtime", object()) == mtime:
            return
        self._last_trade_history_mtime = mtime

        if not os.path.isfile(self.trade_history_path):
            self.hist_list.delete(0, "end")
            self.hist_list.insert("end", "(no trade_history.jsonl yet)")
            return

        # show last N lines
        try:
            with open(self.trade_history_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return

        lines = lines[-250:]  # cap for UI
        self.hist_list.delete(0, "end")
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts = obj.get("ts", None)
                tss = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "?"
                side = str(obj.get("side", "")).upper()
                tag = str(obj.get("tag", "") or "").upper()

                sym = obj.get("symbol", "")
                qty = obj.get("qty", "")
                px = obj.get("price", None)
                pnl = obj.get("realized_profit_usd", None)

                pnl_pct = obj.get("pnl_pct", None)

                px_txt = _fmt_price(px) if px is not None else "N/A"

                action = side
                if tag:
                    action = f"{side}/{tag}"

                txt = f"{tss} | {action:10s} {sym:5s} | qty={qty} | px={px_txt}"

                # Show the exact trade-time PnL%:
                # - DCA buys: show the BUY-side PnL (how far below avg cost it was when it bought)
                # - sells: show the SELL-side PnL (how far above/below avg cost it sold)
                show_trade_pnl_pct = None
                if side == "SELL":
                    show_trade_pnl_pct = pnl_pct
                elif side == "BUY" and tag == "DCA":
                    show_trade_pnl_pct = pnl_pct

                if show_trade_pnl_pct is not None:
                    try:
                        txt += f" | pnl@trade={_fmt_pct(float(show_trade_pnl_pct))}"
                    except Exception:
                        txt += f" | pnl@trade={show_trade_pnl_pct}"

                if pnl is not None:
                    try:
                        txt += f" | realized={float(pnl):+.2f}"
                    except Exception:
                        txt += f" | realized={pnl}"

                self.hist_list.insert("end", txt)
            except Exception:
                self.hist_list.insert("end", line)

    def _copy_trade_history_to_clipboard(self) -> None:
        try:
            total = int(self.hist_list.size())
        except Exception:
            total = 0

        if total <= 0:
            try:
                self.status.config(text="Trade History: nothing to copy")
            except Exception:
                pass
            return

        try:
            selected = list(self.hist_list.curselection())
        except Exception:
            selected = []

        indices = selected if selected else list(range(total))

        lines: List[str] = []
        for i in indices:
            try:
                row = str(self.hist_list.get(i))
            except Exception:
                row = ""
            if row:
                lines.append(row)

        if not lines:
            try:
                self.status.config(text="Trade History: nothing to copy")
            except Exception:
                pass
            return

        payload = "\n".join(lines)
        try:
            self.clipboard_clear()
            self.clipboard_append(payload)
            # update() helps commit clipboard ownership on some Linux/remote Tk setups.
            self.update()
            copied_n = len(lines)
            scope = "selected" if selected else "all visible"
            self.status.config(text=f"Trade History: copied {copied_n} {scope} row(s)")
        except Exception as e:
            try:
                self.status.config(text=f"Trade History copy failed: {e}")
            except Exception:
                pass

    def _show_trade_history_context_menu(self, event) -> str:
        try:
            self._hist_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._hist_menu.grab_release()
            except Exception:
                pass
        return "break"



    def _refresh_coin_dependent_ui(self, prev_coins: List[str]) -> None:
        """
        After settings change: refresh every coin-driven UI element:
          - Training dropdown (Train coin)
          - Trainers tab dropdown (Coin)
          - Chart tabs (Notebook): add/remove tabs to match current coin list
          - Neural overview tiles (new): add/remove tiles to match current coin list
        """
        # Rebuild dependent pieces
        self.coins = [c.upper().strip() for c in (self.settings.get("coins") or []) if c.strip()]
        self.coin_folders = build_coin_folders(self.settings.get("main_neural_dir") or self.project_dir, self.coins)

        # Refresh coin dropdowns (they don't auto-update)
        try:
            # Training pane dropdown
            if hasattr(self, "train_coin_combo") and self.train_coin_combo.winfo_exists():
                self.train_coin_combo["values"] = self.coins
                cur = (self.train_coin_var.get() or "").strip().upper() if hasattr(self, "train_coin_var") else ""
                if self.coins and cur not in self.coins:
                    self.train_coin_var.set(self.coins[0])

            # Trainers tab dropdown
            if hasattr(self, "trainer_coin_combo") and self.trainer_coin_combo.winfo_exists():
                self.trainer_coin_combo["values"] = self.coins
                cur = (self.trainer_coin_var.get() or "").strip().upper() if hasattr(self, "trainer_coin_var") else ""
                if self.coins and cur not in self.coins:
                    self.trainer_coin_var.set(self.coins[0])

            # Keep both selectors aligned if both exist
            if hasattr(self, "train_coin_var") and hasattr(self, "trainer_coin_var"):
                if self.train_coin_var.get():
                    self.trainer_coin_var.set(self.train_coin_var.get())
        except Exception:
            pass

        # Rebuild neural overview tiles (if the widget exists)
        try:
            if hasattr(self, "neural_wrap") and self.neural_wrap.winfo_exists():
                self._rebuild_neural_overview()
                self._refresh_neural_overview()
        except Exception:
            pass

        # Rebuild chart tabs if the coin list changed
        try:
            prev_set = set([str(c).strip().upper() for c in (prev_coins or []) if str(c).strip()])
            if prev_set != set(self.coins):
                self._rebuild_coin_chart_tabs()
        except Exception:
            pass


    def _rebuild_neural_overview(self) -> None:
        """
        Recreate the coin tiles in the left-side Neural Signals box to match self.coins.
        Uses WrapFrame so it automatically breaks into multiple rows.
        Adds hover highlighting and click-to-open chart.
        """
        if not hasattr(self, "neural_wrap") or self.neural_wrap is None:
            return

        # Clear old tiles
        try:
            if hasattr(self.neural_wrap, "clear"):
                self.neural_wrap.clear(destroy_widgets=True)
            else:
                for ch in list(self.neural_wrap.winfo_children()):
                    ch.destroy()
        except Exception:
            pass

        self.neural_tiles = {}

        for coin in (self.coins or []):
            tile = NeuralSignalTile(self.neural_wrap, coin, trade_start_level=int(self.settings.get("trade_start_level", 3) or 3))


            # --- Hover highlighting (real, visible) ---
            def _on_enter(_e=None, t=tile):
                try:
                    t.set_hover(True)
                except Exception:
                    pass

            def _on_leave(_e=None, t=tile):
                # Avoid flicker: when moving between child widgets, ignore "leave" if pointer is still inside tile.
                try:
                    x = t.winfo_pointerx()
                    y = t.winfo_pointery()
                    w = t.winfo_containing(x, y)
                    while w is not None:
                        if w == t:
                            return
                        w = getattr(w, "master", None)
                except Exception:
                    pass

                try:
                    t.set_hover(False)
                except Exception:
                    pass

            tile.bind("<Enter>", _on_enter, add="+")
            tile.bind("<Leave>", _on_leave, add="+")
            try:
                for w in tile.winfo_children():
                    w.bind("<Enter>", _on_enter, add="+")
                    w.bind("<Leave>", _on_leave, add="+")
            except Exception:
                pass

            # --- Click: open chart page ---
            def _open_coin_chart(_e=None, c=coin):
                try:
                    fn = getattr(self, "_show_chart_page", None)
                    if callable(fn):
                        fn(str(c).strip().upper())
                except Exception:
                    pass

            tile.bind("<Button-1>", _open_coin_chart, add="+")
            try:
                for w in tile.winfo_children():
                    w.bind("<Button-1>", _open_coin_chart, add="+")
            except Exception:
                pass

            self.neural_wrap.add(tile, padx=(0, 6), pady=(0, 6))
            self.neural_tiles[coin] = tile

        # Layout and scrollbar refresh
        try:
            self.neural_wrap._schedule_reflow()
        except Exception:
            pass

        try:
            fn = getattr(self, "_update_neural_overview_scrollbars", None)
            if callable(fn):
                self.after_idle(fn)
        except Exception:
            pass






    def _refresh_neural_overview(self) -> None:
        """
        Update each coin tile with long/short neural signals.
        Uses mtime caching so it's cheap to call every UI tick.
        """
        if not hasattr(self, "neural_tiles"):
            return

        # Keep coin_folders aligned with current settings/coins
        try:
            sig = (str(self.settings.get("main_neural_dir") or ""), tuple(self.coins or []))
            if getattr(self, "_coin_folders_sig", None) != sig:
                self._coin_folders_sig = sig
                self.coin_folders = build_coin_folders(self.settings.get("main_neural_dir") or self.project_dir, self.coins)
        except Exception:
            pass

        if not hasattr(self, "_neural_overview_cache"):
            self._neural_overview_cache = {}  # path -> (mtime, value)

        def _cached(path: str, loader, default: Any):
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                return default, None

            hit = self._neural_overview_cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1], mtime

            v = loader(path)
            self._neural_overview_cache[path] = (mtime, v)
            return v, mtime

        def _load_short_from_memory_json(path: str) -> int:
            try:
                obj = _safe_read_json(path) or {}
                return int(float(obj.get("short_dca_signal", 0)))
            except Exception:
                return 0

        latest_ts = None

        for coin, tile in list(self.neural_tiles.items()):
            folder = ""
            try:
                folder = (self.coin_folders or {}).get(coin, "")
            except Exception:
                folder = ""

            if not folder or not os.path.isdir(folder):
                tile.set_values(0, 0)
                continue

            long_sig = 0
            short_sig = 0
            mt_candidates: List[float] = []

            # Long signal
            long_path = os.path.join(folder, "long_dca_signal.txt")
            if os.path.isfile(long_path):
                long_sig, mt = _cached(long_path, read_int_from_file, 0)
                if mt:
                    mt_candidates.append(float(mt))

            # Short signal (prefer txt; fallback to memory.json)
            short_txt = os.path.join(folder, "short_dca_signal.txt")
            if os.path.isfile(short_txt):
                short_sig, mt = _cached(short_txt, read_int_from_file, 0)
                if mt:
                    mt_candidates.append(float(mt))
            else:
                mem = os.path.join(folder, "memory.json")
                if os.path.isfile(mem):
                    short_sig, mt = _cached(mem, _load_short_from_memory_json, 0)
                    if mt:
                        mt_candidates.append(float(mt))

            tile.set_values(long_sig, short_sig)

            if mt_candidates:
                mx = max(mt_candidates)
                latest_ts = mx if (latest_ts is None or mx > latest_ts) else latest_ts

        # Update "Last:" label
        try:
            if hasattr(self, "lbl_neural_overview_last") and self.lbl_neural_overview_last.winfo_exists():
                if latest_ts:
                    self.lbl_neural_overview_last.config(
                        text=f"Last: {time.strftime('%H:%M:%S', time.localtime(float(latest_ts)))}"
                    )
                else:
                    self.lbl_neural_overview_last.config(text="Last: N/A")
        except Exception:
            pass



    def _rebuild_coin_chart_tabs(self) -> None:
        """
        Ensure the Charts multi-row tab bar + pages match self.coins.
        Keeps the ACCOUNT page intact and preserves the currently selected page when possible.
        """
        charts_frame = getattr(self, "_charts_frame", None)
        if charts_frame is None or (hasattr(charts_frame, "winfo_exists") and not charts_frame.winfo_exists()):
            return

        # Remember selected page (coin or ACCOUNT)
        selected = getattr(self, "_current_chart_page", "ACCOUNT")
        if selected not in (["ACCOUNT"] + list(self.coins)):
            selected = "ACCOUNT"

        # Destroy existing tab bar + pages container (clean rebuild)
        try:
            if hasattr(self, "chart_tabs_bar") and self.chart_tabs_bar.winfo_exists():
                self.chart_tabs_bar.destroy()
        except Exception:
            pass

        try:
            if hasattr(self, "chart_pages_container") and self.chart_pages_container.winfo_exists():
                self.chart_pages_container.destroy()
        except Exception:
            pass

        # Recreate
        self.chart_tabs_bar = WrapFrame(charts_frame)
        self.chart_tabs_bar.pack(fill="x", padx=6, pady=(6, 0))

        self.chart_pages_container = ttk.Frame(charts_frame)
        self.chart_pages_container.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self._chart_tab_buttons = {}
        self.chart_pages = {}
        self._current_chart_page = selected

        def _show_page(name: str) -> None:
            self._current_chart_page = name
            for f in self.chart_pages.values():
                try:
                    f.pack_forget()
                except Exception:
                    pass
            f = self.chart_pages.get(name)
            if f is not None:
                f.pack(fill="both", expand=True)

            for txt, b in self._chart_tab_buttons.items():
                try:
                    b.configure(style=("ChartTabSelected.TButton" if txt == name else "ChartTab.TButton"))
                except Exception:
                    pass

        self._show_chart_page = _show_page

        # ACCOUNT page
        acct_page = ttk.Frame(self.chart_pages_container)
        self.chart_pages["ACCOUNT"] = acct_page

        acct_btn = ttk.Button(
            self.chart_tabs_bar,
            text="ACCOUNT",
            style="ChartTab.TButton",
            command=lambda: self._show_chart_page("ACCOUNT"),
        )
        self.chart_tabs_bar.add(acct_btn, padx=(0, 6), pady=(0, 6))
        self._chart_tab_buttons["ACCOUNT"] = acct_btn

        self.account_chart = AccountValueChart(
            acct_page,
            self.account_value_history_path,
            self.trade_history_path,
        )
        self.account_chart.pack(fill="both", expand=True)

        # Coin pages
        self.charts = {}
        for coin in self.coins:
            page = ttk.Frame(self.chart_pages_container)
            self.chart_pages[coin] = page

            btn = ttk.Button(
                self.chart_tabs_bar,
                text=coin,
                style="ChartTab.TButton",
                command=lambda c=coin: self._show_chart_page(c),
            )
            self.chart_tabs_bar.add(btn, padx=(0, 6), pady=(0, 6))
            self._chart_tab_buttons[coin] = btn

            chart = CandleChart(page, self.fetcher, coin, self._settings_getter, self.trade_history_path)
            chart.pack(fill="both", expand=True)
            self.charts[coin] = chart

        # Restore selection
        self._show_chart_page(selected)




    # ---- settings dialog ----

    def open_settings_dialog(self) -> None:

        win = tk.Toplevel(self)
        win.title("Settings")
        # Big enough for the bottom buttons on most screens + still scrolls if someone resizes smaller.
        win.geometry("860x680")
        win.minsize(760, 560)
        win.configure(bg=DARK_BG)

        # Scrollable settings content (auto-hides the scrollbar if everything fits),
        # using the same pattern as the Neural Levels scrollbar.
        viewport = ttk.Frame(win)
        viewport.pack(fill="both", expand=True, padx=12, pady=12)
        viewport.grid_rowconfigure(0, weight=1)
        viewport.grid_columnconfigure(0, weight=1)

        settings_canvas = tk.Canvas(
            viewport,
            bg=DARK_BG,
            highlightthickness=1,
            highlightbackground=DARK_BORDER,
            bd=0,
        )
        settings_canvas.grid(row=0, column=0, sticky="nsew")

        settings_scroll = ttk.Scrollbar(
            viewport,
            orient="vertical",
            command=settings_canvas.yview,
        )
        settings_scroll.grid(row=0, column=1, sticky="ns")

        settings_canvas.configure(yscrollcommand=settings_scroll.set)

        frm = ttk.Frame(settings_canvas)
        settings_window = settings_canvas.create_window((0, 0), window=frm, anchor="nw")

        def _update_settings_scrollbars(event=None) -> None:
            """Update scrollregion + hide/show the scrollbar depending on overflow."""
            try:
                c = settings_canvas
                win_id = settings_window

                c.update_idletasks()
                bbox = c.bbox(win_id)
                if not bbox:
                    settings_scroll.grid_remove()
                    return

                c.configure(scrollregion=bbox)
                content_h = int(bbox[3] - bbox[1])
                view_h = int(c.winfo_height())

                if content_h > (view_h + 1):
                    settings_scroll.grid()
                else:
                    settings_scroll.grid_remove()
                    try:
                        c.yview_moveto(0)
                    except Exception:
                        pass
            except Exception:
                pass

        def _on_settings_canvas_configure(e) -> None:
            # Keep the inner frame exactly the canvas width so wrapping is correct.
            try:
                settings_canvas.itemconfigure(settings_window, width=int(e.width))
            except Exception:
                pass
            _update_settings_scrollbars()

        settings_canvas.bind("<Configure>", _on_settings_canvas_configure, add="+")
        frm.bind("<Configure>", _update_settings_scrollbars, add="+")

        # Mousewheel scrolling when the mouse is over the settings window.
        def _wheel(e):
            try:
                if settings_scroll.winfo_ismapped():
                    settings_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass

        settings_canvas.bind("<Enter>", lambda _e: settings_canvas.focus_set(), add="+")
        settings_canvas.bind("<MouseWheel>", _wheel, add="+")  # Windows / Mac
        settings_canvas.bind("<Button-4>", lambda _e: settings_canvas.yview_scroll(-3, "units"), add="+")  # Linux
        settings_canvas.bind("<Button-5>", lambda _e: settings_canvas.yview_scroll(3, "units"), add="+")   # Linux



        # Make the entry column expand
        frm.columnconfigure(0, weight=0)  # labels
        frm.columnconfigure(1, weight=1)  # entries
        frm.columnconfigure(2, weight=0)  # browse buttons

        def add_row(r: int, label: str, var: tk.Variable, browse: Optional[str] = None):
            """
            browse: "dir" to attach a directory chooser, else None.
            """
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=6)

            ent = ttk.Entry(frm, textvariable=var)
            ent.grid(row=r, column=1, sticky="ew", pady=6)

            if browse == "dir":
                def do_browse():
                    picked = filedialog.askdirectory()
                    if picked:
                        var.set(picked)
                ttk.Button(frm, text="Browse", command=do_browse).grid(row=r, column=2, sticky="e", padx=(10, 0), pady=6)
            else:
                # keep column alignment consistent
                ttk.Label(frm, text="").grid(row=r, column=2, sticky="e", padx=(10, 0), pady=6)

        main_dir_var = tk.StringVar(value=self.settings["main_neural_dir"])
        coins_var = tk.StringVar(value=",".join(self.settings["coins"]))
        trade_start_level_var = tk.StringVar(value=str(self.settings.get("trade_start_level", 3)))
        start_alloc_pct_var = tk.StringVar(value=str(self.settings.get("start_allocation_pct", 0.005)))
        dca_mult_var = tk.StringVar(value=str(self.settings.get("dca_multiplier", 2.0)))
        _dca_levels = self.settings.get("dca_levels", DEFAULT_SETTINGS.get("dca_levels", []))
        if not isinstance(_dca_levels, list):
            _dca_levels = DEFAULT_SETTINGS.get("dca_levels", [])
        dca_levels_var = tk.StringVar(value=",".join(str(x) for x in _dca_levels))
        max_dca_var = tk.StringVar(value=str(self.settings.get("max_dca_buys_per_24h", DEFAULT_SETTINGS.get("max_dca_buys_per_24h", 2))))

        # --- Trailing PM settings (editable; hot-reload friendly) ---
        pm_no_dca_var = tk.StringVar(value=str(self.settings.get("pm_start_pct_no_dca", DEFAULT_SETTINGS.get("pm_start_pct_no_dca", 5.0))))
        pm_with_dca_var = tk.StringVar(value=str(self.settings.get("pm_start_pct_with_dca", DEFAULT_SETTINGS.get("pm_start_pct_with_dca", 2.5))))
        trailing_gap_var = tk.StringVar(value=str(self.settings.get("trailing_gap_pct", DEFAULT_SETTINGS.get("trailing_gap_pct", 0.5))))

        hub_dir_var = tk.StringVar(value=self.settings.get("hub_data_dir", ""))



        neural_script_var = tk.StringVar(value=self.settings["script_neural_runner2"])
        trainer_script_var = tk.StringVar(value=self.settings.get("script_neural_trainer", "pt_mt5_trainer.py"))
        trader_script_var = tk.StringVar(value=self.settings["script_trader"])

        ui_refresh_var = tk.StringVar(value=str(self.settings["ui_refresh_seconds"]))
        chart_refresh_var = tk.StringVar(value=str(self.settings["chart_refresh_seconds"]))
        candles_limit_var = tk.StringVar(value=str(self.settings["candles_limit"]))
        auto_start_var = tk.BooleanVar(value=bool(self.settings.get("auto_start_scripts", False)))

        r = 0
        add_row(r, "Main neural folder:", main_dir_var, browse="dir"); r += 1
        add_row(r, "Coins (comma):", coins_var); r += 1
        add_row(r, "Trade start level (1-7):", trade_start_level_var); r += 1

        # Start allocation % (shows approx $/coin using the last known account value; always displays the $0.50 minimum)
        ttk.Label(frm, text="Start allocation %:").grid(row=r, column=0, sticky="w", padx=(0, 10), pady=6)
        ttk.Entry(frm, textvariable=start_alloc_pct_var).grid(row=r, column=1, sticky="ew", pady=6)

        start_alloc_hint_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=start_alloc_hint_var).grid(row=r, column=2, sticky="w", padx=(10, 0), pady=6)

        def _update_start_alloc_hint(*_):
            # Parse % (allow "0.01" or "0.01%")
            try:
                pct_txt = (start_alloc_pct_var.get() or "").strip().replace("%", "")
                pct = float(pct_txt) if pct_txt else 0.0
            except Exception:
                pct = float(self.settings.get("start_allocation_pct", 0.005) or 0.005)

            if pct < 0.0:
                pct = 0.0

            # Use the last account value we saw in trader_status.json (no extra API calls).
            try:
                total_val = float(getattr(self, "_last_total_account_value", 0.0) or 0.0)
            except Exception:
                total_val = 0.0

            coins_list = [c.strip().upper() for c in (coins_var.get() or "").split(",") if c.strip()]
            n_coins = len(coins_list) if coins_list else 1

            per_coin = 0.0
            if total_val > 0.0:
                per_coin = total_val * (pct / 100.0)
            if per_coin < 0.5:
                per_coin = 0.5

            if total_val > 0.0:
                start_alloc_hint_var.set(f"≈ {_fmt_money(per_coin)} per coin (min $0.50)")
            else:
                start_alloc_hint_var.set("≈ $0.50 min per coin (needs account value)")

        _update_start_alloc_hint()
        start_alloc_pct_var.trace_add("write", _update_start_alloc_hint)
        coins_var.trace_add("write", _update_start_alloc_hint)

        r += 1

        add_row(r, "DCA levels (% list):", dca_levels_var); r += 1

        add_row(r, "DCA multiplier:", dca_mult_var); r += 1

        add_row(r, "Max DCA buys / coin (rolling 24h):", max_dca_var); r += 1

        add_row(r, "Trailing PM start % (no DCA):", pm_no_dca_var); r += 1
        add_row(r, "Trailing PM start % (with DCA):", pm_with_dca_var); r += 1
        add_row(r, "Trailing gap % (behind peak):", trailing_gap_var); r += 1

        add_row(r, "Hub data dir (optional):", hub_dir_var, browse="dir"); r += 1




        ttk.Separator(frm, orient="horizontal").grid(row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1

        add_row(r, "pt_mt5_thinker.py path:", neural_script_var); r += 1
        add_row(r, "pt_mt5_trainer.py path:", trainer_script_var); r += 1
        add_row(r, "pt_mt5_bridge.py path:", trader_script_var); r += 1

        ttk.Separator(frm, orient="horizontal").grid(row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1

        def _open_robinhood_api_wizard() -> None:
            """MT5 hub - Robinhood API setup disabled for MT5."""
            messagebox.showinfo("MT5 Hub", "API setup is disabled for MT5.\nBroker credentials are managed via mt5_config.json.")


            # Scrollable content area (same pattern as the Neural Levels scrollbar).
            viewport = ttk.Frame(wiz)
            viewport.pack(fill="both", expand=True, padx=12, pady=12)
            viewport.grid_rowconfigure(0, weight=1)
            viewport.grid_columnconfigure(0, weight=1)

            wiz_canvas = tk.Canvas(
                viewport,
                bg=DARK_BG,
                highlightthickness=1,
                highlightbackground=DARK_BORDER,
                bd=0,
            )
            wiz_canvas.grid(row=0, column=0, sticky="nsew")

            wiz_scroll = ttk.Scrollbar(viewport, orient="vertical", command=wiz_canvas.yview)
            wiz_scroll.grid(row=0, column=1, sticky="ns")
            wiz_canvas.configure(yscrollcommand=wiz_scroll.set)

            container = ttk.Frame(wiz_canvas)
            wiz_window = wiz_canvas.create_window((0, 0), window=container, anchor="nw")
            container.columnconfigure(0, weight=1)

            def _update_wiz_scrollbars(event=None) -> None:
                """Update scrollregion + hide/show the scrollbar depending on overflow."""
                try:
                    c = wiz_canvas
                    win_id = wiz_window

                    c.update_idletasks()
                    bbox = c.bbox(win_id)
                    if not bbox:
                        wiz_scroll.grid_remove()
                        return

                    c.configure(scrollregion=bbox)
                    content_h = int(bbox[3] - bbox[1])
                    view_h = int(c.winfo_height())

                    if content_h > (view_h + 1):
                        wiz_scroll.grid()
                    else:
                        wiz_scroll.grid_remove()
                        try:
                            c.yview_moveto(0)
                        except Exception:
                            pass
                except Exception:
                    pass

            def _on_wiz_canvas_configure(e) -> None:
                # Keep the inner frame exactly the canvas width so labels wrap nicely.
                try:
                    wiz_canvas.itemconfigure(wiz_window, width=int(e.width))
                except Exception:
                    pass
                _update_wiz_scrollbars()

            wiz_canvas.bind("<Configure>", _on_wiz_canvas_configure, add="+")
            container.bind("<Configure>", _update_wiz_scrollbars, add="+")

            def _wheel(e):
                try:
                    if wiz_scroll.winfo_ismapped():
                        wiz_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
                except Exception:
                    pass

            wiz_canvas.bind("<Enter>", lambda _e: wiz_canvas.focus_set(), add="+")
            wiz_canvas.bind("<MouseWheel>", _wheel, add="+")  # Windows / Mac
            wiz_canvas.bind("<Button-4>", lambda _e: wiz_canvas.yview_scroll(-3, "units"), add="+")  # Linux
            wiz_canvas.bind("<Button-5>", lambda _e: wiz_canvas.yview_scroll(3, "units"), add="+")   # Linux


            key_path, secret_path = _api_paths()

            # Load any existing credentials so users can update without re-generating keys.
            existing_api_key, existing_private_b64 = _read_api_files()
            private_b64_state = {"value": (existing_private_b64 or "").strip()}

            # -----------------------------
            # Helpers (open folder, copy, etc.)
            # -----------------------------
            def _open_in_file_manager(path: str) -> None:
                try:
                    p = os.path.abspath(path)
                    if os.name == "nt":
                        os.startfile(p)  # type: ignore[attr-defined]
                        return
                    if sys.platform == "darwin":
                        subprocess.Popen(["open", p])
                        return
                    subprocess.Popen(["xdg-open", p])
                except Exception as e:
                    messagebox.showerror("Couldn't open folder", f"Tried to open:\n{path}\n\nError:\n{e}")

            def _copy_to_clipboard(txt: str, title: str = "Copied") -> None:
                try:
                    wiz.clipboard_clear()
                    wiz.clipboard_append(txt)
                    messagebox.showinfo(title, "Copied to clipboard.")
                except Exception:
                    pass

            def _mask_path(p: str) -> str:
                try:
                    return os.path.abspath(p)
                except Exception:
                    return p

            # -----------------------------
            # Big, beginner-friendly instructions
            # -----------------------------
            intro = (
                "This trader uses Robinhood's Crypto Trading API credentials.\n\n"
                "You only do this once. When finished, pt_trader.py can authenticate automatically.\n\n"
                "✅ What you will do in this window:\n"
                "  1) Generate a Public Key + Private Key (Ed25519).\n"
                "  2) Copy the PUBLIC key and paste it into Robinhood to create an API credential.\n"
                "  3) Robinhood will show you an API Key (usually starts with 'rh...'). Copy it.\n"
                "  4) Paste that API Key back here and click Save.\n\n"
                "🧭 EXACTLY where to paste the Public Key on Robinhood (desktop web is best):\n"
                "  A) Log in to Robinhood on a computer.\n"
                "  B) Click Account (top-right) → Settings.\n"
                "  C) Click Crypto.\n"
                "  D) Scroll down to API Trading and click + Add Key (or Add key).\n"
                "  E) Paste the Public Key into the Public key field.\n"
                "  F) Give it any name (example: PowerTrader).\n"
                "  G) Permissions: this TRADER needs READ + TRADE. (READ-only cannot place orders.)\n"
                "  H) Click Save. Robinhood shows your API Key — copy it right away (it may only show once).\n\n"
                "📱 Mobile note: if you can't find API Trading in the app, use robinhood.com in a browser.\n\n"
                "This wizard will save two files in the same folder as pt_hub.py:\n"
                "  - r_key.txt    (your API Key)\n"
                "  - r_secret.txt (your PRIVATE key in base64)  ← keep this secret like a password\n"
            )

            intro_lbl = ttk.Label(container, text=intro, justify="left")
            intro_lbl.grid(row=0, column=0, sticky="ew", pady=(0, 10))

            top_btns = ttk.Frame(container)
            top_btns.grid(row=1, column=0, sticky="ew", pady=(0, 10))
            top_btns.columnconfigure(0, weight=1)

            def open_robinhood_page():
                # Robinhood entry point. User will still need to click into Settings → Crypto → API Trading.
                webbrowser.open("https://robinhood.com/account/crypto")

            ttk.Button(top_btns, text="Open Robinhood API Credentials page (Crypto)", command=open_robinhood_page).pack(side="left")
            ttk.Button(top_btns, text="Open Robinhood Crypto Trading API docs", command=lambda: webbrowser.open("https://docs.robinhood.com/crypto/trading/")).pack(side="left", padx=8)
            ttk.Button(top_btns, text="Open Folder With r_key.txt / r_secret.txt", command=lambda: _open_in_file_manager(self.project_dir)).pack(side="left", padx=8)

            # -----------------------------
            # Step 1 — Generate keys
            # -----------------------------
            step1 = ttk.LabelFrame(container, text="Step 1 — Generate your keys (click once)")
            step1.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
            step1.columnconfigure(0, weight=1)

            ttk.Label(step1, text="Public Key (this is what you paste into Robinhood):").grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))

            pub_box = tk.Text(step1, height=4, wrap="none")
            pub_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(6, 10))
            pub_box.configure(bg=DARK_PANEL, fg=DARK_FG, insertbackground=DARK_FG)

            def _render_public_from_private_b64(priv_b64: str) -> str:
                """Return Robinhood-compatible Public Key: base64(raw_ed25519_public_key_32_bytes)."""
                try:
                    raw = base64.b64decode(priv_b64)

                    # Accept either:
                    #   - 32 bytes: Ed25519 seed
                    #   - 64 bytes: NaCl/tweetnacl secretKey (seed + public)
                    if len(raw) == 64:
                        seed = raw[:32]
                    elif len(raw) == 32:
                        seed = raw
                    else:
                        return ""

                    pk = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
                    pub_raw = pk.public_key().public_bytes(
                        encoding=serialization.Encoding.Raw,
                        format=serialization.PublicFormat.Raw,
                    )
                    return base64.b64encode(pub_raw).decode("utf-8")
                except Exception:
                    return ""

            def _set_pub_text(txt: str) -> None:
                try:
                    pub_box.delete("1.0", "end")
                    pub_box.insert("1.0", txt or "")
                except Exception:
                    pass

            # If already configured before, show the public key again (derived from stored private key)
            if private_b64_state["value"]:
                _set_pub_text(_render_public_from_private_b64(private_b64_state["value"]))

            def generate_keys():
                # Generate an Ed25519 keypair (Robinhood expects base64 raw public key bytes)
                priv = ed25519.Ed25519PrivateKey.generate()
                pub = priv.public_key()

                seed = priv.private_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PrivateFormat.Raw,
                    encryption_algorithm=serialization.NoEncryption(),
                )
                pub_raw = pub.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )

                # Store PRIVATE key as base64(seed32) because pt_mt5_thinker.py uses nacl.signing.SigningKey(seed)
                # and it requires exactly 32 bytes.
                private_b64_state["value"] = base64.b64encode(seed).decode("utf-8")

                # Show what you paste into Robinhood: base64(raw public key)
                _set_pub_text(base64.b64encode(pub_raw).decode("utf-8"))


                messagebox.showinfo(
                    "Step 1 complete",
                    "Public/Private keys generated.\n\n"
                    "Next (Robinhood):\n"
                    "  1) Click 'Copy Public Key' in this window\n"
                    "  2) On Robinhood (desktop web): Account → Settings → Crypto\n"
                    "  3) Scroll to 'API Trading' → click '+ Add Key'\n"
                    "  4) Paste the Public Key (base64) into the 'Public key' field\n"
                    "  5) Enable permissions READ + TRADE (this trader needs both), then Save\n"
                    "  6) Robinhood shows an API Key (usually starts with 'rh...') — copy it right away\n\n"
                    "Then come back here and paste that API Key into the 'API Key' box."
                )



            def copy_public_key():
                txt = (pub_box.get("1.0", "end") or "").strip()
                if not txt:
                    messagebox.showwarning("Nothing to copy", "Click 'Generate Keys' first.")
                    return
                _copy_to_clipboard(txt, title="Public Key copied")

            step1_btns = ttk.Frame(step1)
            step1_btns.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 10))
            ttk.Button(step1_btns, text="Generate Keys", command=generate_keys).pack(side="left")
            ttk.Button(step1_btns, text="Copy Public Key", command=copy_public_key).pack(side="left", padx=8)

            # -----------------------------
            # Step 2 — Paste API key (from Robinhood)
            # -----------------------------
            step2 = ttk.LabelFrame(container, text="Step 2 — Paste your Robinhood API Key here")
            step2.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
            step2.columnconfigure(0, weight=1)

            step2_help = (
                "In Robinhood, after you add the Public Key, Robinhood will show an API Key.\n"
                "Paste that API Key below. (It often starts with 'rh.'.)"
            )
            ttk.Label(step2, text=step2_help, justify="left").grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))

            api_key_var = tk.StringVar(value=existing_api_key or "")
            api_ent = ttk.Entry(step2, textvariable=api_key_var)
            api_ent.grid(row=1, column=0, sticky="ew", padx=10, pady=(6, 10))

            def _test_credentials() -> None:
                api_key = (api_key_var.get() or "").strip()
                priv_b64 = (private_b64_state.get("value") or "").strip()

                if not requests:
                    messagebox.showerror(
                        "Missing dependency",
                        "The 'requests' package is required for the Test button.\n\n"
                        "Fix: pip install requests\n\n"
                        "(You can still Save without testing.)"
                    )
                    return

                if not priv_b64:
                    messagebox.showerror("Missing private key", "Step 1: click 'Generate Keys' first.")
                    return
                if not api_key:
                    messagebox.showerror("Missing API key", "Paste the API key from Robinhood into Step 2 first.")
                    return

                # Safe test: market-data endpoint (no trading)
                base_url = "https://trading.robinhood.com"
                path = "/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD"
                method = "GET"
                body = ""
                ts = int(time.time())
                msg = f"{api_key}{ts}{path}{method}{body}".encode("utf-8")

                try:
                    raw = base64.b64decode(priv_b64)

                    # Accept either:
                    #   - 32 bytes: Ed25519 seed
                    #   - 64 bytes: NaCl/tweetnacl secretKey (seed + public)
                    if len(raw) == 64:
                        seed = raw[:32]
                    elif len(raw) == 32:
                        seed = raw
                    else:
                        raise ValueError(f"Unexpected private key length: {len(raw)} bytes (expected 32 or 64)")

                    pk = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
                    sig_b64 = base64.b64encode(pk.sign(msg)).decode("utf-8")
                except Exception as e:
                    messagebox.showerror("Bad private key", f"Couldn't use your private key (r_secret.txt).\n\nError:\n{e}")
                    return


                headers = {
                    "x-api-key": api_key,
                    "x-timestamp": str(ts),
                    "x-signature": sig_b64,
                    "Content-Type": "application/json",
                }

                try:
                    resp = requests.get(f"{base_url}{path}", headers=headers, timeout=10)
                    if resp.status_code >= 400:
                        # Give layman-friendly hints for common failures
                        hint = ""
                        if resp.status_code in (401, 403):
                            hint = (
                                "\n\nCommon fixes:\n"
                                "  • Make sure you pasted the API Key (not the public key).\n"
                                "  • In Robinhood, ensure the key has permissions READ + TRADE.\n"
                                "  • If you just created the key, wait 30–60 seconds and try again.\n"
                            )
                        messagebox.showerror("Test failed", f"Robinhood returned HTTP {resp.status_code}.\n\n{resp.text}{hint}")
                        return

                    data = resp.json()
                    # Try to show something reassuring
                    ask = None
                    try:
                        if data.get("results"):
                            ask = data["results"][0].get("ask_inclusive_of_buy_spread")
                    except Exception:
                        pass

                    messagebox.showinfo(
                        "Test successful",
                        "✅ Your API Key + Private Key worked!\n\n"
                        "Robinhood responded successfully.\n"
                        f"BTC-USD ask (example): {ask if ask is not None else 'received'}\n\n"
                        "Next: click Save."
                    )
                except Exception as e:
                    messagebox.showerror("Test failed", f"Couldn't reach Robinhood.\n\nError:\n{e}")

            step2_btns = ttk.Frame(step2)
            step2_btns.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 10))
            ttk.Button(step2_btns, text="Test Credentials (safe, no trading)", command=_test_credentials).pack(side="left")

            # -----------------------------
            # Step 3 — Save
            # -----------------------------
            step3 = ttk.LabelFrame(container, text="Step 3 — Save to files (required)")
            step3.grid(row=4, column=0, sticky="nsew")
            step3.columnconfigure(0, weight=1)

            ack_var = tk.BooleanVar(value=False)
            ack = ttk.Checkbutton(
                step3,
                text="I understand r_secret.txt is PRIVATE and I will not share it.",
                variable=ack_var,
            )
            ack.grid(row=0, column=0, sticky="w", padx=10, pady=(10, 6))

            save_btns = ttk.Frame(step3)
            save_btns.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 12))

            def do_save():
                api_key = (api_key_var.get() or "").strip()
                priv_b64 = (private_b64_state.get("value") or "").strip()

                if not priv_b64:
                    messagebox.showerror("Missing private key", "Step 1: click 'Generate Keys' first.")
                    return

                # Normalize private key so pt_mt5_thinker.py can load it:
                # - Accept 32 bytes (seed) OR 64 bytes (seed+pub) from older hub versions
                # - Save ONLY base64(seed32) to r_secret.txt
                try:
                    raw = base64.b64decode(priv_b64)
                    if len(raw) == 64:
                        raw = raw[:32]
                        priv_b64 = base64.b64encode(raw).decode("utf-8")
                        private_b64_state["value"] = priv_b64  # keep UI state consistent
                    elif len(raw) != 32:
                        messagebox.showerror(
                            "Bad private key",
                            f"Your private key decodes to {len(raw)} bytes, but it must be 32 bytes.\n\n"
                            "Click 'Generate Keys' again to create a fresh keypair."
                        )
                        return
                except Exception as e:
                    messagebox.showerror(
                        "Bad private key",
                        f"Couldn't decode the private key as base64.\n\nError:\n{e}"
                    )
                    return

                if not api_key:
                    messagebox.showerror("Missing API key", "Step 2: paste your API key from Robinhood first.")
                    return
                if not bool(ack_var.get()):
                    messagebox.showwarning(
                        "Please confirm",
                        "For safety, please check the box confirming you understand r_secret.txt is private."
                    )
                    return


                # Small sanity warning (don’t block, just help)
                if len(api_key) < 10:
                    if not messagebox.askyesno(
                        "API key looks short",
                        "That API key looks unusually short. Are you sure you pasted the API Key from Robinhood?"
                    ):
                        return

                # Back up existing files (so user can undo mistakes)
                try:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    if os.path.isfile(key_path):
                        shutil.copy2(key_path, f"{key_path}.bak_{ts}")
                    if os.path.isfile(secret_path):
                        shutil.copy2(secret_path, f"{secret_path}.bak_{ts}")
                except Exception:
                    pass

                try:
                    with open(key_path, "w", encoding="utf-8") as f:
                        f.write(api_key)
                    with open(secret_path, "w", encoding="utf-8") as f:
                        f.write(priv_b64)
                except Exception as e:
                    messagebox.showerror("Save failed", f"Couldn't write the credential files.\n\nError:\n{e}")
                    return

                _refresh_api_status()
                messagebox.showinfo(
                    "Saved",
                    "✅ Saved!\n\n"
                    "The trader will automatically read these files next time it starts:\n"
                    f"  API Key → {_mask_path(key_path)}\n"
                    f"  Private Key → {_mask_path(secret_path)}\n\n"
                    "Next steps:\n"
                    "  1) Close this window\n"
                    "  2) Start the trader (pt_trader.py)\n"
                    "If something fails, come back here and click 'Test Credentials'."
                )
                wiz.destroy()

            ttk.Button(save_btns, text="Save", command=do_save).pack(side="left")
            ttk.Button(save_btns, text="Close", command=wiz.destroy).pack(side="left", padx=8)

        ttk.Label(frm, text="Broker:").grid(row=r, column=0, sticky="w", padx=(0, 10), pady=6)
        ttk.Label(frm, text="MetaTrader 5 (configured in mt5_config.json)").grid(row=r, column=1, columnspan=2, sticky="w", pady=6)
        r += 1


        ttk.Separator(frm, orient="horizontal").grid(row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1


        add_row(r, "UI refresh seconds:", ui_refresh_var); r += 1
        add_row(r, "Chart refresh seconds:", chart_refresh_var); r += 1
        add_row(r, "Candles limit:", candles_limit_var); r += 1

        chk = ttk.Checkbutton(frm, text="Auto start scripts on GUI launch", variable=auto_start_var)
        chk.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0)); r += 1

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=14)
        btns.columnconfigure(0, weight=1)

        def save():
            try:
                # Track coins before changes so we can detect newly added coins
                prev_coins = set([str(c).strip().upper() for c in (self.settings.get("coins") or []) if str(c).strip()])

                self.settings["main_neural_dir"] = main_dir_var.get().strip()
                self.settings["coins"] = [c.strip().upper() for c in coins_var.get().split(",") if c.strip()]
                self.settings["trade_start_level"] = max(1, min(int(float(trade_start_level_var.get().strip())), 7))
                # --- FIX GAP 3: Sync trade_start_level to mt5_config.json ---
                mt5_cfg_path = os.path.join(self.project_dir, "mt5", "mt5_config.json")
                try:
                    with open(mt5_cfg_path, "r", encoding="utf-8") as f:
                        mt5_cfg = json.load(f)
                except Exception:
                    mt5_cfg = {}
                mt5_cfg["open_threshold"] = int(self.settings["trade_start_level"])
                try:
                    with open(mt5_cfg_path, "w", encoding="utf-8") as f:
                        json.dump(mt5_cfg, f, indent=2)
                except Exception as e:
                    print(f"[WARN] Could not update mt5_config.json: {e}")

                sap = (start_alloc_pct_var.get() or "").strip().replace("%", "")
                self.settings["start_allocation_pct"] = max(0.0, float(sap or 0.0))

                dm = (dca_mult_var.get() or "").strip()
                try:
                    dm_f = float(dm)
                except Exception:
                    dm_f = float(self.settings.get("dca_multiplier", DEFAULT_SETTINGS.get("dca_multiplier", 2.0)) or 2.0)
                if dm_f < 0.0:
                    dm_f = 0.0
                self.settings["dca_multiplier"] = dm_f

                raw_dca = (dca_levels_var.get() or "").replace(",", " ").split()
                dca_levels = []
                for tok in raw_dca:
                    try:
                        dca_levels.append(float(tok))
                    except Exception:
                        pass
                if not dca_levels:
                    dca_levels = list(DEFAULT_SETTINGS.get("dca_levels", []))
                self.settings["dca_levels"] = dca_levels

                md = (max_dca_var.get() or "").strip()
                try:
                    md_i = int(float(md))
                except Exception:
                    md_i = int(self.settings.get("max_dca_buys_per_24h", DEFAULT_SETTINGS.get("max_dca_buys_per_24h", 2)) or 2)
                if md_i < 0:
                    md_i = 0
                self.settings["max_dca_buys_per_24h"] = md_i


                # --- Trailing PM settings ---
                try:
                    pm0 = float((pm_no_dca_var.get() or "").strip().replace("%", "") or 0.0)
                except Exception:
                    pm0 = float(self.settings.get("pm_start_pct_no_dca", DEFAULT_SETTINGS.get("pm_start_pct_no_dca", 5.0)) or 5.0)
                if pm0 < 0.0:
                    pm0 = 0.0
                self.settings["pm_start_pct_no_dca"] = pm0

                try:
                    pm1 = float((pm_with_dca_var.get() or "").strip().replace("%", "") or 0.0)
                except Exception:
                    pm1 = float(self.settings.get("pm_start_pct_with_dca", DEFAULT_SETTINGS.get("pm_start_pct_with_dca", 2.5)) or 2.5)
                if pm1 < 0.0:
                    pm1 = 0.0
                self.settings["pm_start_pct_with_dca"] = pm1

                try:
                    tg = float((trailing_gap_var.get() or "").strip().replace("%", "") or 0.0)
                except Exception:
                    tg = float(self.settings.get("trailing_gap_pct", DEFAULT_SETTINGS.get("trailing_gap_pct", 0.5)) or 0.5)
                if tg < 0.0:
                    tg = 0.0
                self.settings["trailing_gap_pct"] = tg



                self.settings["hub_data_dir"] = hub_dir_var.get().strip()




                self.settings["script_neural_runner2"] = _mt5_local_script_name(
                    neural_script_var.get().strip(), "pt_mt5_thinker.py"
                )
                self.settings["script_neural_trainer"] = _mt5_local_script_name(
                    trainer_script_var.get().strip(), "pt_mt5_trainer.py"
                )
                self.settings["script_trader"] = _mt5_local_script_name(
                    trader_script_var.get().strip(), "pt_mt5_bridge.py"
                )

                self.settings["ui_refresh_seconds"] = float(ui_refresh_var.get().strip())
                self.settings["chart_refresh_seconds"] = float(chart_refresh_var.get().strip())
                self.settings["candles_limit"] = int(float(candles_limit_var.get().strip()))
                self.settings["auto_start_scripts"] = bool(auto_start_var.get())
                self._save_settings()

                # If new coin(s) were added and their training folder doesn't exist yet,
                # create the folder and copy neural_trainer.py into it RIGHT AFTER saving settings.
                try:
                    new_coins = [c.strip().upper() for c in (self.settings.get("coins") or []) if c.strip()]
                    added = [c for c in new_coins if c and c not in prev_coins]

                    main_dir = self.settings.get("main_neural_dir") or self.project_dir
                    trainer_name = os.path.basename(str(self.settings.get("script_neural_trainer", "neural_trainer.py")))

                    # Best-effort resolve source trainer path:
                    # Prefer trainer living in the main (BTC) folder; fallback to the configured trainer path.
                    src_main_trainer = os.path.join(main_dir, trainer_name)
                    src_cfg_trainer = str(self.settings.get("script_neural_trainer", trainer_name))
                    src_trainer_path = src_main_trainer if os.path.isfile(src_main_trainer) else src_cfg_trainer

                    for coin in added:
                        if coin == "BTC":
                            continue  # BTC uses main folder; no per-coin folder needed

                        coin_dir = os.path.join(main_dir, coin)
                        if not os.path.isdir(coin_dir):
                            os.makedirs(coin_dir, exist_ok=True)

                        dst_trainer_path = os.path.join(coin_dir, trainer_name)
                        if (not os.path.isfile(dst_trainer_path)) and os.path.isfile(src_trainer_path):
                            shutil.copy2(src_trainer_path, dst_trainer_path)
                except Exception:
                    pass

                # Refresh all coin-driven UI (dropdowns + chart tabs)
                self._refresh_coin_dependent_ui(prev_coins)

                messagebox.showinfo("Saved", "Settings saved.")
                win.destroy()


            except Exception as e:
                messagebox.showerror("Error", f"Failed to save settings:\n{e}")


        ttk.Button(btns, text="Save", command=save).pack(side="left")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=8)


    # ---- close ----

    def _on_close(self) -> None:
        # Don’t force kill; just stop if running (you can change this later)
        try:
            self.stop_all_scripts()
        except Exception:
            pass
        self.destroy()


import argparse
from datetime import timezone, timedelta

# --- Inlined Trainer Logic (from pt_mt5_trainer.py) ---
def train_mt5(
    coin: str,
    mt5_symbol: str,
    timeframe: str,
    terminal_path: str,
    lookback_days: int = 60,
    pattern_length: int = 12,
    candles_to_predict: int = 2,
    memory_dir: str = "",
    min_move_pct: float = 0.20,
    recency_half_life_days: int = 30,
    incremental: bool = False,
    incremental_max_age_hours: int = 24,
) -> dict:
    """
    Train pattern memory on MT5 historical candles.
    Returns a dict with training statistics.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("[ERROR] MetaTrader5 package not installed.")
        return {"error": "mt5_not_installed"}

    def log(msg):
        print(msg, flush=True)

    def normalize_timeframe(tf):
        return str(tf).replace("_", "").replace("-", "").lower()

    def tf_to_mt5_const(tf):
        # Map string to MT5 timeframe constant
        tf_map = {
            "1min": mt5.TIMEFRAME_M1,
            "5min": mt5.TIMEFRAME_M5,
            "15min": mt5.TIMEFRAME_M15,
            "30min": mt5.TIMEFRAME_M30,
            "1hour": mt5.TIMEFRAME_H1,
            "4hour": mt5.TIMEFRAME_H4,
            "1day": mt5.TIMEFRAME_D1,
            "1week": mt5.TIMEFRAME_W1,
        }
        return tf_map.get(tf, mt5.TIMEFRAME_H1)

    def compute_atr(highs, lows, closes, period=14):
        n = len(closes)
        atr = [0.0] * n
        for i in range(1, n):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            if i < period:
                atr[i] = tr
            else:
                atr[i] = (atr[i - 1] * (period - 1) + tr) / period
        return atr

    def load_memory_file(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [ln.strip() for ln in f if ln.strip()]
        except Exception:
            return []

    def load_weights_file(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [float(ln.strip()) for ln in f if ln.strip()]
        except Exception:
            return []

    def save_memory_file(path, patterns):
        with open(path, "w", encoding="utf-8") as f:
            for p in patterns:
                f.write(f"{p}\n")

    def save_weights_file(path, weights):
        with open(path, "w", encoding="utf-8") as f:
            for w in weights:
                f.write(f"{w}\n")

    tf_label = normalize_timeframe(timeframe)
    log(f"[TRAIN] {coin} {tf_label} — lookback={lookback_days}d pattern_len={pattern_length} predict={candles_to_predict}")

    memory_dir = os.path.abspath(memory_dir or os.path.dirname(__file__))
    coin_dir = os.path.join(memory_dir, coin.upper())
    os.makedirs(coin_dir, exist_ok=True)
    mem_path     = os.path.join(coin_dir, f"memories_{tf_label}.txt")
    weights_path = os.path.join(coin_dir, f"memory_weights_{tf_label}.txt")

    # Incremental mode: skip if memory was updated recently
    if incremental and os.path.isfile(mem_path):
        age_hours = (time.time() - os.path.getmtime(mem_path)) / 3600.0
        if age_hours < incremental_max_age_hours:
            log(f"[TRAIN] Incremental skip: memory is {age_hours:.1f}h old (limit={incremental_max_age_hours}h)")
            existing = load_memory_file(mem_path)
            return {"coin": coin, "timeframe": tf_label, "patterns": len(existing), "skipped": True, "reason": "incremental"}

    # Initialize MT5
    try:
        ok = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    except Exception as e:
        log(f"[WARN] MT5 init note: {e}")

    tf_const = tf_to_mt5_const(tf_label)
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    log(f"[TRAIN] Fetching {mt5_symbol} {tf_label} from {start_dt.date()} to {end_dt.date()}...")

    candles = mt5.copy_rates_range(mt5_symbol, tf_const, start_dt, end_dt)
    if candles is None or len(candles) == 0:
        log(f"[ERROR] No candles for {mt5_symbol}")
        try:
            mt5.shutdown()
        except Exception:
            pass
        return {"coin": coin, "timeframe": tf_label, "patterns": 0, "error": "no_candles"}

    log(f"[TRAIN] Fetched {len(candles)} candles")

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

    atr_values = compute_atr(highs, lows, closes, period=14)

    pct_changes:  list = [0.0]
    high_changes: list = [0.0]
    low_changes:  list = [0.0]
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

    bars_per_day  = max(1, int(len(candles) / lookback_days))
    half_life_bars = recency_half_life_days * bars_per_day
    recency_w = [math.exp(-(n - 1 - i) / max(1, half_life_bars)) for i in range(n)]

    memory_patterns = load_memory_file(mem_path)
    weights         = load_weights_file(weights_path)
    if len(weights) < len(memory_patterns):
        weights.extend([1.0] * (len(memory_patterns) - len(weights)))

    existing_key_to_idx = {}
    for idx, p in enumerate(memory_patterns):
        key = str(p).split("{}", 1)[0]
        if key and key not in existing_key_to_idx:
            existing_key_to_idx[key] = idx

    log(f"[TRAIN] Loaded {len(memory_patterns)} existing patterns, {len(weights)} weights")

    new_count    = 0
    updated_count = 0
    skipped_quality = 0
    MIN_CANDLES_REQUIRED = pattern_length + candles_to_predict + 1
    for i in range(pattern_length, n - candles_to_predict):
        window = pct_changes[i - pattern_length:i]
        if len(window) < pattern_length:
            continue
        bar_atr = atr_values[i] if atr_values[i] > 0 else (sum(abs(c) for c in window) / len(window) or 1.0)
        window_norm = [round(v / bar_atr, 4) for v in window] if bar_atr > 0 else window
        future_highs = high_changes[i: i + candles_to_predict]
        future_lows  = low_changes[i: i + candles_to_predict]
        avg_high = sum(future_highs) / len(future_highs) if future_highs else 0.0
        avg_low  = sum(future_lows)  / len(future_lows)  if future_lows  else 0.0
        expected_move_pct = abs(avg_high) + abs(avg_low)
        if expected_move_pct < min_move_pct:
            skipped_quality += 1
            continue
        pattern_str   = " ".join(f"{v:.4f}" for v in window_norm)
        pattern_entry = f"{pattern_str}{{}}{avg_high:.4f}{{}}{avg_low:.4f}"
        idx_existing = existing_key_to_idx.get(pattern_str)
        rec_w = recency_w[i]
        if idx_existing is None:
            memory_patterns.append(pattern_entry)
            weights.append(rec_w)
            existing_key_to_idx[pattern_str] = len(memory_patterns) - 1
            new_count += 1
        else:
            if idx_existing < len(weights):
                weights[idx_existing] = min(float(weights[idx_existing]) + 0.1 * rec_w, 10.0)
                updated_count += 1

    log(f"[TRAIN] Patterns: {new_count} new, {updated_count} updated, {skipped_quality} quality-filtered (min_move={min_move_pct:.2f}%)")
    log(f"[TRAIN] Total patterns: {len(memory_patterns)}")

    save_memory_file(mem_path, memory_patterns)
    save_weights_file(weights_path, weights)

    status_path = os.path.join(memory_dir, "trainer_status.json")
    existing_status = {}
    if os.path.isfile(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                existing_status = json.load(f)
        except Exception:
            pass
    per_tf = existing_status.get("per_timeframe", {})
    per_tf[tf_label] = {
        "patterns":       len(memory_patterns),
        "new_this_run":   new_count,
        "updated":        updated_count,
        "quality_skipped": skipped_quality,
        "candles_used":   n,
        "lookback_days":  lookback_days,
        "trained_at":     int(datetime.now(timezone.utc).timestamp()),
    }
    status = {
        "coin":             coin,
        "state":            "FINISHED",
        "timestamp":        int(datetime.now(timezone.utc).timestamp()),
        "patterns_saved":   len(memory_patterns),
        "total_patterns":   len(memory_patterns),
        "lookback_days":    lookback_days,
        "timeframe":        tf_label,
        "per_timeframe":    per_tf,
    }
    os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    log(f"[TRAIN] Done! {len(memory_patterns)} patterns saved -> {mem_path}")
    log(f"[TRAIN] Status -> {status_path}")
    try:
        mt5.shutdown()
    except Exception:
        pass
    return {
        "coin":            coin,
        "timeframe":       tf_label,
        "patterns":        len(memory_patterns),
        "new":             new_count,
        "updated":         updated_count,
        "quality_skipped": skipped_quality,
    }


def trainer_cli():
    parser = argparse.ArgumentParser(description="PowerTrader MT5 Trainer v2 (via pt_hub_mt5.py)")
    parser.add_argument("--coin", help="Coin symbol, e.g. BTC")
    parser.add_argument("--all-coins", action="store_true", help="Train all enabled coins from config")
    parser.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "mt5_config.json"), help="Path to config file for all-coins mode")
    parser.add_argument("--mt5-symbol", default=None, help="MT5 symbol (auto-resolved if omitted)")
    parser.add_argument("--timeframe", default="1hour", help="Single timeframe (legacy flag, use --timeframes for multi)")
    parser.add_argument("--timeframes", nargs="+", default=None, help="One or more timeframes to train: 1hour 4hour 1day")
    parser.add_argument("--terminal-path", default="", help="Path to terminal64.exe")
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--pattern-length", type=int, default=12)
    parser.add_argument("--candles-predict", type=int, default=2, help="Candles ahead to predict for TP/SL scoring")
    parser.add_argument("--memory-dir", default="", help="Folder for memories/weights (default: BOT parent dir)")
    parser.add_argument("--min-move", type=float, default=0.20, help="Min predicted move (%% units) to keep pattern. Default 0.20 = 0.20%%")
    parser.add_argument("--recency-half-life", type=int, default=30, help="Recency half-life in days (higher = weight history more)")
    parser.add_argument("--incremental", action="store_true", help="Skip if memory file is fresh enough")
    parser.add_argument("--incremental-max-age", type=int, default=24, help="Hours before incremental training re-runs")
    args = parser.parse_args()

    def get_enabled_coins(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            coins = []
            for coin, cdata in cfg.get("coins", {}).items():
                if cdata.get("enable_long", False) or cdata.get("enable_short", False):
                    coins.append(coin.upper())
            return coins
        except Exception as e:
            print(f"[ERROR] Failed to load config: {e}")
            return []

    default_sym_map = {
        "BTC": "BTCUSD", "ETH": "ETHUSD", "XRP": "XRPUSD",
        "DOGE": "DOGUSD", "BNB": "BNBUSD",
    }

    coins = []
    if args.all_coins:
        coins = get_enabled_coins(args.config)
        if not coins:
            print("[ERROR] No enabled coins found in config.")
            sys.exit(1)
    elif args.coin:
        coins = [args.coin.upper()]
    else:
        print("[ERROR] Must specify --coin or --all-coins.")
        sys.exit(1)

    timeframes = args.timeframes or [args.timeframe]
    errors = 0
    for coin in coins:
        mt5_symbol = args.mt5_symbol or default_sym_map.get(coin, f"{coin}USD")
        print(f"[INFO] {coin} -> MT5 symbol: {mt5_symbol}")
        for tf in timeframes:
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
                )
                print(f"[RESULT] {coin} {tf}: {result}")
            except Exception as e:
                print(f"[ERROR] {coin} {tf}: {e}")
                import traceback
                traceback.print_exc()
                errors += 1
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    # CLI: --mode train (or --mode=gui for GUI)
    if any(arg.startswith("--mode") for arg in sys.argv):
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--mode", choices=["gui", "train"], default="gui")
        args, unknown = parser.parse_known_args()
        if args.mode == "train":
            sys.argv = [sys.argv[0]] + unknown  # Remove --mode for trainer_cli
            trainer_cli()
        else:
            app = PowerTraderHub()
            app.mainloop()
    else:
        app = PowerTraderHub()
        app.mainloop()
