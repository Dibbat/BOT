#!/usr/bin/env python3
"""
PowerTrader live file dashboard (read-only).
Run: python dashboard.py
Open: http://localhost:5000

Shows: account info, current positions with SL/TP, trade history, signal status.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from flask import Flask, render_template_string, jsonify, Response

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Path resolution -- mirrors the hub's own logic exactly.
#
# Directory layout on Windows:
#   D:\BOT\                         <- ROOT_DIR  (main_neural_dir)
#     hub_data\                     <- HUB_DIR   (trader_status, pnl_ledger, etc.)
#     BTC\, ETH\, XRP\, ...         <- signal txt files per coin
#     mt5\                          <- BASE_DIR  (where dashboard.py lives)
#       mt5_config.json
#       signal_history\             <- SIG_DIR   (CSV files from exporter)
#
# Override any path with env vars:
#   PT_DASHBOARD_BASE_DIR   -- folder containing dashboard.py  (default: auto)
#   PT_DASHBOARD_ROOT_REL   -- relative path from BASE_DIR to ROOT_DIR (default: ..)
#   POWERTRADER_HUB_DIR     -- absolute path to hub_data folder (default: ROOT_DIR/hub_data)
# ---------------------------------------------------------------------------

BASE_DIR = os.path.abspath(
    os.environ.get("PT_DASHBOARD_BASE_DIR", os.path.dirname(os.path.abspath(__file__)))
)

# ROOT_DIR is the BOT root -- one level UP from BASE_DIR (same as hub's main_neural_dir)
# Override with PT_DASHBOARD_ROOT_REL if your layout differs.
_root_rel = os.environ.get("PT_DASHBOARD_ROOT_REL", "..")
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, _root_rel))

# HUB_DIR: where bridge writes trader_status.json, pnl_ledger.json, trade_history.jsonl
HUB_DIR = os.path.abspath(
    os.environ.get("POWERTRADER_HUB_DIR", os.path.join(ROOT_DIR, "hub_data"))
)

# SIG_DIR: where the thinker exporter writes *_signals.csv
SIG_DIR = os.path.abspath(os.path.join(BASE_DIR, "signal_history"))

# CONFIG_PATH: mt5_config.json (same folder as dashboard.py)
CONFIG_PATH = os.path.abspath(os.path.join(BASE_DIR, "mt5_config.json"))

# TRAINER_STATUS_PATH: written by trainer to ROOT_DIR
TRAINER_STATUS_PATH = os.path.abspath(os.path.join(ROOT_DIR, "trainer_status.json"))

print(f"[DASHBOARD] BASE_DIR = {BASE_DIR}")
print(f"[DASHBOARD] ROOT_DIR = {ROOT_DIR}")
print(f"[DASHBOARD] HUB_DIR  = {HUB_DIR}")
print(f"[DASHBOARD] SIG_DIR  = {SIG_DIR}")
print(f"[DASHBOARD] CONFIG   = {CONFIG_PATH}")
print(f"[DASHBOARD] HUB_DIR exists: {os.path.isdir(HUB_DIR)}")
print(f"[DASHBOARD] CONFIG exists:  {os.path.isfile(CONFIG_PATH)}")


def _read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return default


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def load_coins() -> List[str]:
    cfg = _read_json(CONFIG_PATH, {})
    rows = cfg.get("symbols") if isinstance(cfg, dict) else []
    coins: List[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        c = str(row.get("bot_symbol", "")).strip().upper()
        if c:
            coins.append(c)
    return coins or ["BTC", "ETH", "XRP", "DOGE", "BNB"]


def load_symbol_configs() -> Dict[str, Any]:
    cfg = _read_json(CONFIG_PATH, {})
    rows = cfg.get("symbols") if isinstance(cfg, dict) else []
    out: Dict[str, Any] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        c = str(row.get("bot_symbol", "")).strip().upper()
        if c:
            out[c] = row
    return out


def coin_folder(coin: str) -> str:
    return ROOT_DIR if coin == "BTC" else os.path.join(ROOT_DIR, coin)


def _safe_int(text: str, fallback: int = 0) -> int:
    try:
        return int(float(str(text).strip()))
    except Exception:
        return fallback


def _safe_float(text: str, fallback: float = 0.0) -> float:
    try:
        return float(str(text).strip())
    except Exception:
        return fallback


def read_signal_files() -> Dict[str, Dict[str, float]]:
    data: Dict[str, Dict[str, float]] = {}
    for coin in load_coins():
        folder = coin_folder(coin)
        ls = _safe_int(_read_text(os.path.join(folder, "long_dca_signal.txt"), "0"), 0)
        ss = _safe_int(_read_text(os.path.join(folder, "short_dca_signal.txt"), "0"), 0)
        lp = _safe_float(_read_text(os.path.join(folder, "futures_long_profit_margin.txt"), "0.25"), 0.25)
        sp = _safe_float(_read_text(os.path.join(folder, "futures_short_profit_margin.txt"), "0.25"), 0.25)
        data[coin] = {
            "sig_long": ls,
            "sig_short": ss,
            "pm_long": lp,
            "pm_short": sp,
        }
    return data


def read_signal_csvs() -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for coin in load_coins():
        path = os.path.join(SIG_DIR, f"{coin}_signals.csv")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if len(lines) < 2:
                continue
            header = [h.strip().lower() for h in lines[0].split(",")]
            last = [c.strip() for c in lines[-1].split(",")]
            row = {header[i]: last[i] for i in range(min(len(header), len(last)))}

            def val(*keys: str) -> int:
                for k in keys:
                    if k in row:
                        try:
                            return int(float(row[k]))
                        except Exception:
                            continue
                return 0

            out[coin] = {
                "long_csv": val("long_sig", "long_signal", "long", "long_strength"),
                "short_csv": val("short_sig", "short_signal", "short", "short_strength"),
            }
        except Exception:
            continue
    return out


def read_trader_status() -> Dict[str, Any]:
    return _read_json(os.path.join(HUB_DIR, "trader_status.json"), {})


def read_runner_ready() -> Dict[str, Any]:
    return _read_json(os.path.join(HUB_DIR, "runner_ready.json"), {})


def read_trainer_status() -> Dict[str, Any]:
    return _read_json(TRAINER_STATUS_PATH, {})


def read_pnl_ledger() -> Dict[str, Any]:
    return _read_json(os.path.join(HUB_DIR, "pnl_ledger.json"), {})


def read_trade_history(limit: int = 50) -> List[Dict[str, Any]]:
    path = os.path.join(HUB_DIR, "trade_history.jsonl")
    trades: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    trades.append(obj)
                except Exception:
                    continue
    except Exception:
        pass
    return trades[-limit:]


def read_account_history(limit: int = 60) -> List[Dict[str, float]]:
    path = os.path.join(HUB_DIR, "account_value_history.jsonl")
    points: List[Dict[str, float]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = obj.get("ts")
                val = obj.get("total_account_value")
                if ts is None or val is None:
                    continue
                points.append({"ts": float(ts), "v": float(val)})
    except Exception:
        pass
    return points[-max(1, int(limit)):]


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<!-- Perplexity Computer -->
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PowerTrader · Live Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* ── Design Tokens ───────────────────────────────────────────── */
:root {
  --font-body: 'Inter', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'Consolas', monospace;

  --text-xs:   clamp(0.75rem,  0.7rem  + 0.25vw, 0.8125rem);
  --text-sm:   clamp(0.8125rem,0.78rem + 0.2vw,  0.9rem);
  --text-base: clamp(0.875rem, 0.85rem + 0.15vw, 0.9375rem);
  --text-lg:   clamp(1rem,     0.95rem + 0.25vw, 1.125rem);
  --text-xl:   clamp(1.25rem,  1.1rem  + 0.6vw,  1.5rem);

  --space-1: 0.25rem; --space-2: 0.5rem; --space-3: 0.75rem;
  --space-4: 1rem;    --space-5: 1.25rem;--space-6: 1.5rem;
  --space-8: 2rem;    --space-10: 2.5rem;

  --radius-sm: 0.25rem; --radius-md: 0.5rem;
  --radius-lg: 0.75rem; --radius-xl: 1rem;

  --transition: 160ms cubic-bezier(0.16, 1, 0.3, 1);

  /* Sidebar */
  --sidebar-w: 220px;
}

/* ── Dark Theme ──────────────────────────────────────────────── */
[data-theme="dark"] {
  --bg:          #0a0d12;
  --surface:     #0f1318;
  --surface-2:   #141920;
  --surface-3:   #1a2130;
  --border:      #1e2738;
  --border-2:    #253047;

  --text:        #c8d4e8;
  --text-muted:  #5e7294;
  --text-faint:  #3a4a62;
  --text-bright: #e8f0ff;

  --green:       #22d47e;
  --green-dim:   #0d3d26;
  --green-glow:  rgba(34,212,126,0.12);
  --red:         #ff4d6a;
  --red-dim:     #3d0d16;
  --red-glow:    rgba(255,77,106,0.12);
  --yellow:      #f5c842;
  --yellow-dim:  #3d3309;
  --blue:        #4d9fff;
  --blue-dim:    #0a1f3d;
  --purple:      #a78bfa;
  --orange:      #fb923c;

  --accent:      #4d9fff;
  --accent-glow: rgba(77,159,255,0.15);

  --shadow-sm:  0 1px 3px rgba(0,0,0,0.4);
  --shadow-md:  0 4px 16px rgba(0,0,0,0.5);
  --shadow-lg:  0 8px 32px rgba(0,0,0,0.6);
  --shadow-glow: 0 0 20px rgba(77,159,255,0.08);
}

/* ── Light Theme ─────────────────────────────────────────────── */
[data-theme="light"] {
  --bg:          #f0f4fb;
  --surface:     #ffffff;
  --surface-2:   #f7f9fd;
  --surface-3:   #eef2fb;
  --border:      #dde3f0;
  --border-2:    #c8d0e8;

  --text:        #1a2340;
  --text-muted:  #5a6a90;
  --text-faint:  #a0aec0;
  --text-bright: #0a1020;

  --green:       #16a34a;
  --green-dim:   #dcfce7;
  --green-glow:  rgba(22,163,74,0.08);
  --red:         #dc2626;
  --red-dim:     #fee2e2;
  --red-glow:    rgba(220,38,38,0.08);
  --yellow:      #d97706;
  --yellow-dim:  #fef3c7;
  --blue:        #2563eb;
  --blue-dim:    #dbeafe;
  --purple:      #7c3aed;
  --orange:      #ea580c;

  --accent:      #2563eb;
  --accent-glow: rgba(37,99,235,0.1);

  --shadow-sm:  0 1px 3px rgba(0,0,0,0.07);
  --shadow-md:  0 4px 16px rgba(0,0,0,0.08);
  --shadow-lg:  0 8px 32px rgba(0,0,0,0.1);
  --shadow-glow: 0 0 20px rgba(37,99,235,0.06);
}

/* ── Reset & Base ────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
html {
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
body {
  font-family: var(--font-body);
  font-size: var(--text-base);
  color: var(--text);
  background: var(--bg);
  line-height: 1.5;
}
button { cursor: pointer; background: none; border: none; font: inherit; color: inherit; }
table { border-collapse: collapse; width: 100%; }
code, .mono { font-family: var(--font-mono); }

/* ── Layout Shell ────────────────────────────────────────────── */
.shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) 1fr;
  grid-template-rows: 1fr;
  height: 100dvh;
}

/* ── Sidebar ─────────────────────────────────────────────────── */
.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overscroll-behavior: contain;
  z-index: 20;
}
.sidebar-logo {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-5) var(--space-5);
  border-bottom: 1px solid var(--border);
  min-height: 60px;
}
.logo-icon {
  width: 32px; height: 32px;
  flex-shrink: 0;
}
.logo-text {
  font-size: var(--text-base);
  font-weight: 700;
  color: var(--text-bright);
  letter-spacing: -0.02em;
  line-height: 1.2;
}
.logo-sub {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-weight: 400;
}
.sidebar-section {
  padding: var(--space-4) var(--space-3);
}
.sidebar-section + .sidebar-section {
  border-top: 1px solid var(--border);
}
.sidebar-label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 0 var(--space-2) var(--space-2);
}
.nav-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  color: var(--text-muted);
  cursor: pointer;
  transition: background var(--transition), color var(--transition);
  text-decoration: none;
}
.nav-item:hover { background: var(--surface-3); color: var(--text); }
.nav-item.active {
  background: var(--accent-glow);
  color: var(--accent);
  font-weight: 600;
}
.nav-icon { width: 16px; height: 16px; opacity: 0.8; flex-shrink: 0; }
.nav-item.active .nav-icon { opacity: 1; }

.sidebar-coins {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.coin-nav-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-2) var(--space-3);
  margin: 1px var(--space-2);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition);
  font-size: var(--text-sm);
}
.coin-nav-item:hover { background: var(--surface-3); }
.coin-nav-left { display: flex; align-items: center; gap: var(--space-2); }
.coin-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.coin-dot.active { background: var(--green); box-shadow: 0 0 6px var(--green); }
.coin-dot.signal { background: var(--yellow); }
.coin-dot.idle { background: var(--text-faint); }
.coin-nav-sym { font-weight: 600; color: var(--text-bright); font-size: var(--text-xs); }
.coin-nav-sig { font-size: var(--text-xs); color: var(--text-muted); font-family: var(--font-mono); }

.sidebar-footer {
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--border);
  font-size: var(--text-xs);
  color: var(--text-faint);
}
.sidebar-footer a { color: var(--text-faint); text-decoration: none; }
.sidebar-footer a:hover { color: var(--text-muted); }

/* ── Main Area ───────────────────────────────────────────────── */
.main-area {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Topbar ──────────────────────────────────────────────────── */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--space-6);
  height: 60px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  flex-shrink: 0;
  gap: var(--space-4);
}
.topbar-left {
  display: flex; align-items: center; gap: var(--space-3);
}
.page-title {
  font-size: var(--text-base);
  font-weight: 700;
  color: var(--text-bright);
  letter-spacing: -0.01em;
}
.topbar-right {
  display: flex; align-items: center; gap: var(--space-3);
}
.status-pill {
  display: flex; align-items: center; gap: var(--space-2);
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-xl);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.02em;
  border: 1px solid transparent;
}
.pill-live {
  background: var(--green-dim);
  color: var(--green);
  border-color: rgba(34,212,126,0.2);
}
.pill-warn {
  background: var(--yellow-dim);
  color: var(--yellow);
  border-color: rgba(245,200,66,0.2);
}
.pill-dry {
  background: var(--blue-dim);
  color: var(--blue);
  border-color: rgba(77,159,255,0.2);
}
.pill-dot {
  width: 6px; height: 6px; border-radius: 50%;
  animation: pulse-dot 2s ease-in-out infinite;
}
.pill-live .pill-dot { background: var(--green); }
.pill-warn .pill-dot { background: var(--yellow); }
.pill-dry .pill-dot { background: var(--blue); }
@keyframes pulse-dot {
  0%,100% { opacity: 1; }
  50% { opacity: 0.3; }
}
.clock-badge {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--text-muted);
  background: var(--surface-3);
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
}
.theme-toggle {
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  border-radius: var(--radius-md);
  color: var(--text-muted);
  background: var(--surface-3);
  border: 1px solid var(--border);
  transition: all var(--transition);
  flex-shrink: 0;
}
.theme-toggle:hover { color: var(--text); border-color: var(--border-2); }
.refresh-badge {
  font-size: var(--text-xs);
  color: var(--text-faint);
}

/* ── Scroll Region ───────────────────────────────────────────── */
.scroll-region {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

/* ── KPI Cards Row ───────────────────────────────────────────── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(155px, 1fr));
  gap: var(--space-3);
}
.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  position: relative;
  overflow: hidden;
  transition: border-color var(--transition), box-shadow var(--transition);
}
.kpi-card:hover {
  border-color: var(--border-2);
  box-shadow: var(--shadow-md);
}
.kpi-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: var(--kpi-accent, var(--accent));
  opacity: 0.6;
}
.kpi-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.kpi-value {
  font-size: var(--text-xl);
  font-weight: 700;
  color: var(--kpi-color, var(--text-bright));
  font-variant-numeric: tabular-nums lining-nums;
  line-height: 1;
  letter-spacing: -0.02em;
}
.kpi-sub {
  font-size: var(--text-xs);
  color: var(--text-faint);
}

/* ── Two-column layout ───────────────────────────────────────── */
.row-2col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-4);
}
@media (max-width: 1100px) {
  .row-2col { grid-template-columns: 1fr; }
}

/* ── Cards ───────────────────────────────────────────────────── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-5);
  border-bottom: 1px solid var(--border);
  background: var(--surface-2);
  min-height: 44px;
}
.card-title {
  font-size: var(--text-xs);
  font-weight: 700;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.card-badge {
  font-size: var(--text-xs);
  background: var(--surface-3);
  color: var(--text-faint);
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
}
.card-body {
  padding: var(--space-4) var(--space-5);
}
.card-body.no-pad { padding: 0; }

/* ── Chart ───────────────────────────────────────────────────── */
.chart-wrap {
  padding: var(--space-4) var(--space-5) var(--space-3);
  position: relative;
}
.chart-wrap canvas { height: 130px !important; }
.chart-empty {
  height: 130px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-faint);
  font-size: var(--text-sm);
}

/* ── Tables ──────────────────────────────────────────────────── */
.tbl-wrap { overflow-x: auto; }
.data-table {
  width: 100%;
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums lining-nums;
}
.data-table th {
  background: var(--surface-2);
  color: var(--text-muted);
  text-align: left;
  padding: var(--space-2) var(--space-4);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  white-space: nowrap;
  position: sticky;
  top: 0;
  z-index: 1;
  border-bottom: 1px solid var(--border);
}
.data-table td {
  padding: var(--space-2) var(--space-4);
  border-bottom: 1px solid var(--border);
  color: var(--text);
  white-space: nowrap;
}
.data-table tr:last-child td { border-bottom: none; }
.data-table tbody tr {
  transition: background var(--transition);
}
.data-table tbody tr:hover { background: var(--surface-3); }
.data-table .col-sym {
  font-weight: 700;
  color: var(--text-bright);
  font-size: var(--text-sm);
}
.data-table .col-price { font-family: var(--font-mono); }

/* Profit/Loss coloring */
.c-green { color: var(--green); }
.c-red   { color: var(--red); }
.c-blue  { color: var(--blue); }
.c-yellow { color: var(--yellow); }
.c-muted { color: var(--text-muted); }
.c-faint { color: var(--text-faint); }
.c-bright { color: var(--text-bright); }

/* Side badges */
.side-badge {
  display: inline-flex; align-items: center; gap: 3px;
  font-size: 10px; font-weight: 700;
  padding: 2px 7px;
  border-radius: var(--radius-sm);
  text-transform: uppercase; letter-spacing: 0.04em;
}
.side-long { background: var(--green-dim); color: var(--green); }
.side-short { background: var(--red-dim); color: var(--red); }
.side-buy { background: var(--green-dim); color: var(--green); }
.side-sell { background: var(--red-dim); color: var(--red); }

/* Tag badges */
.tag {
  display: inline-block;
  font-size: 10px; font-weight: 600;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  letter-spacing: 0.03em;
  text-transform: uppercase;
}
.tag-entry { background: var(--green-dim); color: var(--green); }
.tag-dca   { background: var(--yellow-dim); color: var(--yellow); }
.tag-close { background: var(--red-dim); color: var(--red); }
.tag-tp    { background: var(--blue-dim); color: var(--blue); }
.tag-other { background: var(--surface-3); color: var(--text-muted); }

/* SL/TP cells */
.sl-val { color: var(--red); font-family: var(--font-mono); font-size: 10px; }
.tp-val { color: var(--green); font-family: var(--font-mono); font-size: 10px; }

/* Empty state */
.empty-state {
  padding: var(--space-8) var(--space-5);
  text-align: center;
  color: var(--text-faint);
  font-size: var(--text-sm);
}
.empty-icon { font-size: 1.5rem; margin-bottom: var(--space-2); opacity: 0.5; }

/* ── Signal Grid ─────────────────────────────────────────────── */
.signal-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--space-3);
}
.signal-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  transition: border-color var(--transition), box-shadow var(--transition);
  position: relative;
  overflow: hidden;
}
.signal-card:hover {
  border-color: var(--border-2);
  box-shadow: var(--shadow-md);
}
.signal-card.sc-active {
  border-color: rgba(34,212,126,0.35);
  box-shadow: 0 0 0 1px rgba(34,212,126,0.1), var(--shadow-md);
}
.signal-card.sc-signal {
  border-color: rgba(245,200,66,0.35);
}
.signal-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.signal-sym {
  font-size: var(--text-lg);
  font-weight: 800;
  color: var(--text-bright);
  letter-spacing: -0.02em;
}
.signal-status-dot {
  width: 9px; height: 9px; border-radius: 50%;
}
.ssd-active {
  background: var(--green);
  box-shadow: 0 0 8px var(--green);
  animation: pulse-dot 1.8s ease-in-out infinite;
}
.ssd-signal { background: var(--yellow); }
.ssd-idle   { background: var(--text-faint); }
.signal-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-1) 0;
  font-size: var(--text-xs);
  border-bottom: 1px solid var(--border);
}
.signal-row:last-of-type { border-bottom: none; }
.signal-row-label { color: var(--text-muted); }
.sig-bars {
  display: flex; flex-direction: column; gap: 3px;
  margin-top: var(--space-1);
}
.sig-bar-wrap {
  display: flex; align-items: center; gap: var(--space-2);
}
.sig-bar-label {
  font-size: 10px;
  color: var(--text-faint);
  width: 10px;
  font-weight: 600;
}
.sig-bar-track {
  flex: 1; height: 5px;
  background: var(--surface-3);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.sig-bar-fill {
  height: 100%;
  border-radius: var(--radius-sm);
  transition: width 0.4s ease;
}
.sig-bar-fill.long-bar  { background: var(--green); }
.sig-bar-fill.short-bar { background: var(--red); }
.sig-bar-val {
  font-size: 10px;
  font-family: var(--font-mono);
  color: var(--text-muted);
  width: 18px;
  text-align: right;
}

/* ── Symbol config table rows ────────────────────────────────── */
.config-mini {
  font-size: var(--text-xs);
  color: var(--text-faint);
  font-family: var(--font-mono);
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-top: 2px;
}
.config-chip {
  background: var(--surface-3);
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  font-size: 10px;
  color: var(--text-faint);
  border: 1px solid var(--border);
}

/* ── PnL delta pill ──────────────────────────────────────────── */
.delta-pill {
  display: inline-flex; align-items: center; gap: 3px;
  font-size: var(--text-xs); font-weight: 600;
  padding: 2px 8px; border-radius: var(--radius-xl);
}
.delta-pos { background: var(--green-dim); color: var(--green); }
.delta-neg { background: var(--red-dim); color: var(--red); }
.delta-nil { background: var(--surface-3); color: var(--text-muted); }

/* ── Divider ─────────────────────────────────────────────────── */
.section-divider {
  font-size: var(--text-xs);
  font-weight: 700;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.section-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}

/* ── Scrollbar ───────────────────────────────────────────────── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 99px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-faint); }

/* ── Responsive ──────────────────────────────────────────────── */
@media (max-width: 900px) {
  :root { --sidebar-w: 0px; }
  .sidebar { display: none; }
  .shell { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<div class="shell">
  <!-- ── Sidebar ──────────────────────────────────────────────── -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <!-- PowerTrader SVG Logo -->
      <svg class="logo-icon" viewBox="0 0 32 32" fill="none" aria-label="PowerTrader">
        <rect width="32" height="32" rx="7" fill="#0f1318"/>
        <polyline points="4,22 10,14 15,18 20,10 28,10" stroke="#4d9fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="28" cy="10" r="2.5" fill="#22d47e"/>
        <line x1="4" y1="22" x2="28" y2="22" stroke="#1e2738" stroke-width="1"/>
      </svg>
      <div>
        <div class="logo-text">PowerTrader</div>
        <div class="logo-sub">MT5 Live Dashboard</div>
      </div>
    </div>

    <div class="sidebar-section">
      <div class="sidebar-label">Views</div>
      <a class="nav-item active" href="#overview" onclick="showSection('overview',this)">
        <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1" y="1" width="6" height="6" rx="1"/><rect x="9" y="1" width="6" height="6" rx="1"/><rect x="1" y="9" width="6" height="6" rx="1"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>
        Overview
      </a>
      <a class="nav-item" href="#positions" onclick="showSection('positions',this)">
        <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 12V4l4 4 4-4 4 4v8"/></svg>
        Positions
      </a>
      <a class="nav-item" href="#history" onclick="showSection('history',this)">
        <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></svg>
        Trade History
      </a>
      <a class="nav-item" href="#signals" onclick="showSection('signals',this)">
        <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1 8c1.5-4 5-6 7-6s5.5 2 7 6c-1.5 4-5 6-7 6s-5.5-2-7-6z"/><circle cx="8" cy="8" r="2"/></svg>
        Signals
      </a>
    </div>

    <div class="sidebar-section sidebar-coins" id="sidebar-coins">
      <div class="sidebar-label">Symbols</div>
      {% for coin, d in coins.items() %}
      {% set has_pos = d.sig_long >= open_threshold or d.sig_short >= open_threshold %}
      {% set has_sig = d.sig_long >= 1 or d.sig_short >= 1 %}
      <div class="coin-nav-item" onclick="scrollToCoin('{{ coin }}')">
        <div class="coin-nav-left">
          <div class="coin-dot {{ 'active' if has_pos else ('signal' if has_sig else 'idle') }}"></div>
          <span class="coin-nav-sym">{{ coin }}</span>
        </div>
        <span class="coin-nav-sig">L{{ d.sig_long }} S{{ d.sig_short }}</span>
      </div>
      {% endfor %}
    </div>

    <div class="sidebar-footer">
      <a href="https://www.perplexity.ai/computer" target="_blank" rel="noopener noreferrer">
        Built with Perplexity Computer
      </a>
    </div>
  </aside>

  <!-- ── Main Area ─────────────────────────────────────────────── -->
  <div class="main-area">
    <!-- Topbar -->
    <header class="topbar">
      <div class="topbar-left">
        <span class="page-title" id="page-title-text">Overview</span>
      </div>
      <div class="topbar-right">
        <span id="clock" class="clock-badge">--:--:-- UTC</span>
        <span id="update-indicator" style="font-size:var(--text-xs);color:var(--text-faint);display:none"></span>

        {% if runner_ready %}
        <span id="pill-runner" class="status-pill pill-live"><span class="pill-dot"></span> LIVE</span>
        {% else %}
        <span class="status-pill pill-warn"><span class="pill-dot"></span> NOT READY</span>
        {% endif %}

        {% if trade_enabled %}
        <span id="pill-trade" class="status-pill pill-live"><span class="pill-dot"></span> TRADING</span>
        {% else %}
        <span class="status-pill pill-dry"><span class="pill-dot"></span> DRY RUN</span>
        {% endif %}

        <span class="refresh-badge">↻ 5s</span>
        <button class="theme-toggle" data-theme-toggle aria-label="Toggle theme">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        </button>
      </div>
    </header>

    <!-- Scroll Region -->
    <main class="scroll-region" id="scroll-region">

      <!-- ── OVERVIEW SECTION ── -->
      <section id="sec-overview">

        <!-- KPI Cards -->
        <div class="kpi-grid">
          <div class="kpi-card" style="--kpi-accent:var(--blue);--kpi-color:var(--blue)">
            <div class="kpi-label">Equity</div>
            <div class="kpi-value" id="kpi-equity">${{ '%.2f'|format(account.get('total_account_value',0)) }}</div>
            <div class="kpi-sub">Total account value</div>
          </div>
          <div class="kpi-card" style="--kpi-accent:var(--text-muted)">
            <div class="kpi-label">Balance</div>
            <div class="kpi-value" id="kpi-balance">${{ '%.2f'|format(account.get('balance',0)) }}</div>
            <div class="kpi-sub">Available cash</div>
          </div>
          <div class="kpi-card" style="--kpi-accent:var(--green);--kpi-color:var(--green)">
            <div class="kpi-label">Free Margin</div>
            <div class="kpi-value" id="kpi-margin">${{ '%.2f'|format(account.get('buying_power',0)) }}</div>
            <div class="kpi-sub">Buying power</div>
          </div>
          <div class="kpi-card" style="--kpi-accent:var(--yellow);--kpi-color:var(--yellow)">
            <div class="kpi-label">Open Positions</div>
            <div class="kpi-value" id="kpi-openpos">{{ account.get('total_positions',0) }}</div>
            <div class="kpi-sub">Active trades</div>
          </div>
          <div class="kpi-card" style="--kpi-accent:var(--purple);--kpi-color:var(--purple)">
            <div class="kpi-label">In Trade</div>
            <div class="kpi-value" id="kpi-intrade">{{ '%.1f'|format(account.get('percent_in_trade',0)) }}%</div>
            <div class="kpi-sub">Capital deployed</div>
          </div>
          <div class="kpi-card" style="--kpi-accent:{{ 'var(--green)' if pnl_total >= 0 else 'var(--red)' }};--kpi-color:{{ 'var(--green)' if pnl_total >= 0 else 'var(--red)' }}">
            <div class="kpi-label">Realized P&L</div>
            <div class="kpi-value" id="kpi-pnl">{{ '%+.2f'|format(pnl_total) }}</div>
            <div class="kpi-sub">Closed profits</div>
          </div>
          <div class="kpi-card" style="--kpi-accent:var(--text-faint)">
            <div class="kpi-label">Patterns</div>
            <div class="kpi-value" id="kpi-patterns">{{ trainer.get('patterns_saved', trainer.get('total_patterns','—')) }}</div>
            <div class="kpi-sub">ML model data</div>
          </div>
        </div>

        <!-- Chart + Bot Config -->
        <div class="row-2col">
          <!-- Account Value Chart -->
          <div class="card">
            <div class="card-header">
              <span class="card-title">Account Value</span>
              <span class="card-badge" id="chart-pts-badge">— pts</span>
            </div>
            {% if history %}
            <div class="chart-wrap">
              <canvas id="achart"></canvas>
            </div>
            {% else %}
            <div class="chart-empty">No history data yet</div>
            {% endif %}
            <script type="application/json" id="chart-data">{{ history_json }}</script>
          </div>

          <!-- Bot Config Summary -->
          <div class="card">
            <div class="card-header">
              <span class="card-title">Bot Configuration</span>
              <span class="card-badge">{{ 'LIVE' if trade_enabled else 'DRY RUN' }}</span>
            </div>
            <div class="card-body no-pad">
              <div class="tbl-wrap">
                <table class="data-table">
                  <tbody>
                    <tr><td class="c-muted">Open Threshold</td><td class="c-bright mono">{{ open_threshold }}</td></tr>
                    <tr><td class="c-muted">Server</td><td class="c-blue mono" style="font-family:var(--font-mono);font-size:var(--text-xs)">{{ config.get('server','—') }}</td></tr>
                    <tr><td class="c-muted">Poll Interval</td><td class="c-bright mono">{{ config.get('poll_seconds','—') }}s</td></tr>
                    <tr><td class="c-muted">SL %</td><td class="c-red mono">{{ config.get('sl_pct','—') }}%</td></tr>
                    <tr><td class="c-muted">TP %</td><td class="c-green mono">{{ config.get('tp_pct','—') }}%</td></tr>
                    <tr><td class="c-muted">Trailing SL</td><td class="mono c-bright">{{ 'ON' if config.get('trailing_sl_enabled') else 'OFF' }}</td></tr>
                    <tr><td class="c-muted">DCA Multiplier</td><td class="mono c-bright">{{ config.get('dca_multiplier','—') }}×</td></tr>
                    <tr><td class="c-muted">Max DCA / Trade</td><td class="mono c-bright">{{ config.get('max_dca_per_trade','—') }}</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

      </section>

      <!-- ── POSITIONS SECTION ── -->
      <section id="sec-positions">
        <div class="section-divider">Current Positions</div>
        <div class="card" style="margin-top:var(--space-3)">
          <div class="card-header">
            <span class="card-title">Open Positions</span>
            <span class="card-badge">{{ positions|length }} open</span>
          </div>
          <div class="card-body no-pad">
            {% if positions %}
            <div class="tbl-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Volume</th>
                    <th>Entry</th>
                    <th>Current</th>
                    <th>P&L %</th>
                    <th>Profit $</th>
                    <th>Stop Loss</th>
                    <th>Take Profit</th>
                    <th>Swap</th>
                  </tr>
                </thead>
                <tbody id="pos-tbody">
                  {% for sym, pos in positions.items() %}
                  {% set pnl_val = pos.get('pnl_pct', 0) %}
                  <tr>
                    <td class="col-sym">{{ sym }}</td>
                    <td>
                      <span class="side-badge {{ 'side-long' if pos.get('side','')=='LONG' else 'side-short' }}">
                        {{ pos.get('side','?') }}
                      </span>
                    </td>
                    <td class="col-price">{{ '%.6f'|format(pos.get('quantity',0)) }}</td>
                    <td class="col-price">${{ '%.5f'|format(pos.get('avg_cost_basis',0)) }}</td>
                    <td class="col-price">${{ '%.5f'|format(pos.get('current_buy_price',0)) }}</td>
                    <td>
                      <span class="delta-pill {{ 'delta-pos' if pnl_val >= 0 else 'delta-neg' }}">
                        {{ '%+.3f'|format(pnl_val) }}%
                      </span>
                    </td>
                    <td class="{{ 'c-green' if pos.get('profit',0) >= 0 else 'c-red' }}">
                      ${{ '%+.2f'|format(pos.get('profit',0)) }}
                    </td>
                    <td>
                      {% if pos.get('sl',0) > 0 %}
                      <span class="sl-val">${{ '%.5f'|format(pos.get('sl',0)) }}</span>
                      {% else %}<span class="c-faint">—</span>{% endif %}
                    </td>
                    <td>
                      {% if pos.get('tp',0) > 0 %}
                      <span class="tp-val">${{ '%.5f'|format(pos.get('tp',0)) }}</span>
                      {% else %}<span class="c-faint">—</span>{% endif %}
                    </td>
                    <td class="c-muted col-price">{{ '%.2f'|format(pos.get('swap',0)) }}</td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
            {% else %}
            <div class="empty-state">
              <div class="empty-icon">📭</div>
              No open positions
            </div>
            {% endif %}
          </div>
        </div>
      </section>

      <!-- ── TRADE HISTORY SECTION ── -->
      <section id="sec-history">
        <div class="section-divider">Trade History</div>
        <div class="card" style="margin-top:var(--space-3)">
          <div class="card-header">
            <span class="card-title">Recent Trades</span>
            <span class="card-badge">last {{ trade_history|length }}</span>
          </div>
          <div class="card-body no-pad">
            {% if trade_history %}
            <div class="tbl-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Tag</th>
                    <th>Qty</th>
                    <th>Price</th>
                    <th>SL</th>
                    <th>TP</th>
                    <th>P&L %</th>
                    <th>Realized $</th>
                  </tr>
                </thead>
                <tbody id="trade-tbody">
                  {% for t in trade_history|reverse %}
                  {% set tag = t.get('tag','') %}
                  {% set side = t.get('side','') %}
                  <tr>
                    <td class="c-faint" style="font-family:var(--font-mono);font-size:10px">{{ t.get('time_str','?') }}</td>
                    <td class="col-sym">{{ t.get('symbol','?') }}</td>
                    <td>
                      <span class="side-badge {{ 'side-buy' if side=='BUY' else 'side-sell' }}">{{ side }}</span>
                    </td>
                    <td>
                      {% if tag == 'ENTRY' %}<span class="tag tag-entry">ENTRY</span>
                      {% elif tag == 'DCA' %}<span class="tag tag-dca">DCA</span>
                      {% elif tag == 'CLOSE' %}<span class="tag tag-close">CLOSE</span>
                      {% elif 'tp' in (t.get('reason',''))|lower %}<span class="tag tag-tp">TP</span>
                      {% else %}<span class="tag tag-other">{{ tag or '—' }}</span>
                      {% endif %}
                    </td>
                    <td class="col-price">{{ '%.6f'|format(t.get('qty',0)) }}</td>
                    <td class="col-price">${{ '%.5f'|format(t.get('price',0)) }}</td>
                    <td>
                      {% if t.get('sl',0) > 0 %}<span class="sl-val">${{ '%.2f'|format(t.get('sl',0)) }}</span>{% else %}<span class="c-faint">—</span>{% endif %}
                    </td>
                    <td>
                      {% if t.get('tp',0) > 0 %}<span class="tp-val">${{ '%.2f'|format(t.get('tp',0)) }}</span>{% else %}<span class="c-faint">—</span>{% endif %}
                    </td>
                    <td>
                      {% if t.get('pnl_pct') is not none %}
                      <span class="{{ 'c-green' if t.get('pnl_pct',0) >= 0 else 'c-red' }}">{{ '%+.3f'|format(t.get('pnl_pct',0)) }}%</span>
                      {% else %}<span class="c-faint">—</span>{% endif %}
                    </td>
                    <td>
                      {% if t.get('realized_profit_usd') is not none %}
                      <span class="{{ 'c-green' if t.get('realized_profit_usd',0) >= 0 else 'c-red' }}">${{ '%+.4f'|format(t.get('realized_profit_usd',0)) }}</span>
                      {% else %}<span class="c-faint">—</span>{% endif %}
                    </td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
            {% else %}
            <div class="empty-state">
              <div class="empty-icon">📊</div>
              No trade history yet
            </div>
            {% endif %}
          </div>
        </div>
      </section>

      <!-- ── SIGNALS SECTION ── -->
      <section id="sec-signals">
        <div class="section-divider">Signal Monitor</div>
        <div class="signal-grid" style="margin-top:var(--space-3)" id="signal-grid">
          {% for coin, d in coins.items() %}
          {% set has_pos = d.sig_long >= open_threshold or d.sig_short >= open_threshold %}
          {% set has_sig = d.sig_long >= 1 or d.sig_short >= 1 %}
          <div class="signal-card {{ 'sc-active' if has_pos else ('sc-signal' if has_sig else '') }}" id="coin-{{ coin }}">
            <div class="signal-card-top">
              <span class="signal-sym">{{ coin }}</span>
              <div class="signal-status-dot {{ 'ssd-active' if has_pos else ('ssd-signal' if has_sig else 'ssd-idle') }}"></div>
            </div>

            <div>
              <div class="signal-row">
                <span class="signal-row-label">Long Sig</span>
                <span class="{{ 'c-green' if d.sig_long else 'c-faint' }}" data-field="long_sig" style="font-family:var(--font-mono)">{{ d.sig_long }}</span>
              </div>
              <div class="signal-row">
                <span class="signal-row-label">Short Sig</span>
                <span class="{{ 'c-red' if d.sig_short else 'c-faint' }}" data-field="short_sig" style="font-family:var(--font-mono)">{{ d.sig_short }}</span>
              </div>
              <div class="signal-row">
                <span class="signal-row-label">P(Long)</span>
                <span class="{{ 'c-green' if d.pm_long > 0.26 else ('c-yellow' if d.pm_long >= 0.25 else 'c-red') }}" data-field="pm_long" style="font-family:var(--font-mono)">{{ '%.4f'|format(d.pm_long) }}</span>
              </div>
              <div class="signal-row">
                <span class="signal-row-label">P(Short)</span>
                <span class="{{ 'c-red' if d.pm_short > 0.26 else ('c-yellow' if d.pm_short >= 0.25 else 'c-muted') }}" data-field="pm_short" style="font-family:var(--font-mono)">{{ '%.4f'|format(d.pm_short) }}</span>
              </div>
              {% if d.get('long_csv') is not none %}
              <div class="signal-row">
                <span class="signal-row-label">CSV L/S</span>
                <span class="c-muted" style="font-family:var(--font-mono);font-size:10px">{{ d.long_csv }} / {{ d.short_csv }}</span>
              </div>
              {% endif %}
            </div>

            <div class="sig-bars">
              <div class="sig-bar-wrap">
                <span class="sig-bar-label c-green">L</span>
                <div class="sig-bar-track">
                  <div class="sig-bar-fill long-bar" style="width:{{ [d.sig_long*20,100]|min }}%"></div>
                </div>
                <span class="sig-bar-val">{{ d.sig_long }}</span>
              </div>
              <div class="sig-bar-wrap">
                <span class="sig-bar-label c-red">S</span>
                <div class="sig-bar-track">
                  <div class="sig-bar-fill short-bar" style="width:{{ [d.sig_short*20,100]|min }}%"></div>
                </div>
                <span class="sig-bar-val">{{ d.sig_short }}</span>
              </div>
            </div>

            {% if symbol_configs.get(coin) %}
            {% set sc = symbol_configs[coin] %}
            <div class="config-mini">
              <span class="config-chip">{{ sc.get('mt5_symbol','?') }}</span>
              <span class="config-chip">lot {{ sc.get('lot','?') }}</span>
              <span class="config-chip c-red">SL {{ sc.get('sl_pct','?') }}%</span>
              <span class="config-chip c-green">TP {{ sc.get('tp_pct','?') }}%</span>
            </div>
            {% endif %}
          </div>
          {% endfor %}
        </div>
      </section>

    </main>
  </div>
</div>

<script>
// ── Live data polling (no full-page reload) ──────────────────
(function() {
  var _chart = null;
  var _chartPts = 0;

  function _c(id) { return document.getElementById(id); }
  function _html(id, v) { var e=_c(id); if(e && e.innerHTML!==v) e.innerHTML=v; }
  function _text(id, v) { var e=_c(id); if(e && e.textContent!==v) e.textContent=v; }
  function _cls(id, v) { var e=_c(id); if(e) e.className=v; }
  function _style(id, prop, v) { var e=_c(id); if(e) e.style[prop]=v; }

  function applyColors(el, val, pos_color, neg_color) {
    if (!el) return;
    var v = parseFloat(val);
    el.style.color = isNaN(v) ? '' : (v >= 0 ? pos_color : neg_color);
  }

  function rebuildChart(pts) {
    if (!pts || !pts.length) return;
    if (_chartPts === pts.length) return; // no new points
    _chartPts = pts.length;

    var canvas = _c('achart');
    if (!canvas) return;
    var labels = pts.map(function(p){
      var d=new Date(p.ts*1000);
      return d.getUTCHours().toString().padStart(2,'0')+':'+d.getUTCMinutes().toString().padStart(2,'0');
    });
    var vals = pts.map(function(p){return p.v;});
    var trend = vals[vals.length-1] >= vals[0];
    var accent = trend ? '#22d47e' : '#ff4d6a';
    var glow   = trend ? 'rgba(34,212,126,' : 'rgba(255,77,106,';

    var isDark = document.documentElement.getAttribute('data-theme') !== 'light';

    if (_chart) { _chart.destroy(); _chart = null; }
    _chart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          data: vals,
          borderColor: accent, borderWidth: 2,
          pointRadius: 0, pointHoverRadius: 5,
          pointHoverBackgroundColor: accent,
          fill: true,
          backgroundColor: function(ctx){
            var chart=ctx.chart, ca=chart.chartArea;
            if(!ca) return glow+'0.15)';
            var g=chart.ctx.createLinearGradient(0,ca.top,0,ca.bottom);
            g.addColorStop(0,glow+'0.2)'); g.addColorStop(1,glow+'0)');
            return g;
          },
          tension: 0.4
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { intersect: false, mode: 'index' },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: isDark?'#141920':'#fff',
            borderColor: isDark?'#1e2738':'#dde3f0', borderWidth:1,
            titleColor: isDark?'#5e7294':'#5a6a90',
            bodyColor: isDark?'#e8f0ff':'#1a2340', padding:10,
            titleFont:{family:'Inter',size:11},
            bodyFont:{family:'JetBrains Mono',size:12,weight:'600'},
            callbacks:{ label: function(ctx){return ' $'+ctx.raw.toFixed(2);} }
          }
        },
        scales: {
          x: { grid:{display:false}, ticks:{color:isDark?'#3a4a62':'#a0aec0',font:{size:10},maxTicksLimit:6}, border:{display:false} },
          y: { position:'right', grid:{color:isDark?'#1e2738':'#dde3f0'}, ticks:{color:isDark?'#3a4a62':'#a0aec0',font:{size:10},callback:function(v){return '$'+v.toFixed(0);},maxTicksLimit:5}, border:{display:false} }
        }
      }
    });
    var badge = _c('chart-pts-badge');
    if (badge) badge.textContent = pts.length + ' pts';
  }

  function patchKPI(data) {
    var acc = data.account || {};
    var fmt2 = function(v){ return '$'+(parseFloat(v)||0).toFixed(2); };
    var fmtp = function(v){ return ((parseFloat(v)||0).toFixed(1))+'%'; };

    var eq  = parseFloat(acc.total_account_value||0);
    var bal = parseFloat(acc.balance||0);
    var fm  = parseFloat(acc.buying_power||0);
    var pnl = parseFloat(data.pnl_total||0);

    // KPI values
    var kpis = {
      'kpi-equity':   '$'+eq.toFixed(2),
      'kpi-balance':  '$'+bal.toFixed(2),
      'kpi-margin':   '$'+fm.toFixed(2),
      'kpi-openpos':  String(acc.total_positions||0),
      'kpi-intrade':  fmtp(acc.percent_in_trade||0),
      'kpi-pnl':      (pnl>=0?'+':'')+pnl.toFixed(2),
      'kpi-patterns': String(data.trainer_patterns||'--'),
      'kpi-risk':     fmtp(acc.portfolio_risk_pct||0),
    };
    for (var id in kpis) { _text(id, kpis[id]); }

    // PnL color
    var pnlEl = _c('kpi-pnl');
    if (pnlEl) pnlEl.style.color = pnl>=0 ? 'var(--green)' : 'var(--red)';

    // Status pills
    var ready = data.runner_ready;
    var trading = data.trade_enabled;
    var rp = _c('pill-runner');
    var tp = _c('pill-trade');
    if (rp) {
      rp.textContent = ready ? 'LIVE' : 'NOT READY';
      rp.className = 'status-pill ' + (ready ? 'pill-live' : 'pill-warn');
    }
    if (tp) {
      tp.textContent = trading ? 'TRADING' : 'DRY RUN';
      tp.className = 'status-pill ' + (trading ? 'pill-live' : 'pill-dry');
    }
  }

  function patchPositions(positions) {
    var tbody = _c('pos-tbody');
    if (!tbody) return;
    if (!positions || !Object.keys(positions).length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty-state"><div class="empty-icon">📭</div>No open positions</td></tr>';
      return;
    }
    var rows = '';
    for (var sym in positions) {
      var pos = positions[sym];
      var pnl = parseFloat(pos.pnl_pct||0);
      var prof = parseFloat(pos.profit||0);
      var side = (pos.side||'?');
      var sideCls = side==='LONG' ? 'side-long' : 'side-short';
      var pnlCls = pnl>=0 ? 'c-green' : 'c-red';
      var profCls = prof>=0 ? 'c-green' : 'c-red';
      var sl = parseFloat(pos.sl||0);
      var tp2 = parseFloat(pos.tp||0);
      rows += '<tr>';
      rows += '<td class="col-sym">'+sym+'</td>';
      rows += '<td><span class="side-badge '+sideCls+'">'+side+'</span></td>';
      rows += '<td class="col-price">'+(parseFloat(pos.quantity||0)).toFixed(6)+'</td>';
      rows += '<td class="col-price">$'+(parseFloat(pos.avg_cost_basis||0)).toFixed(5)+'</td>';
      rows += '<td class="col-price">$'+(parseFloat(pos.current_buy_price||0)).toFixed(5)+'</td>';
      rows += '<td><span class="delta-pill '+(pnl>=0?'delta-pos':'delta-neg')+'">'+(pnl>=0?'+':'')+pnl.toFixed(3)+'%</span></td>';
      rows += '<td class="'+profCls+'">$'+(prof>=0?'+':'')+prof.toFixed(2)+'</td>';
      rows += '<td>'+(sl>0?'<span class="sl-val">$'+sl.toFixed(5)+'</span>':'<span class="c-faint">-</span>')+'</td>';
      rows += '<td>'+(tp2>0?'<span class="tp-val">$'+tp2.toFixed(5)+'</span>':'<span class="c-faint">-</span>')+'</td>';
      rows += '<td class="c-muted col-price">'+(parseFloat(pos.swap||0)).toFixed(2)+'</td>';
      rows += '</tr>';
    }
    tbody.innerHTML = rows;
  }

  function patchSignals(coins, open_threshold) {
    for (var coin in coins) {
      var d = coins[coin];
      var ls = parseInt(d.sig_long||0);
      var ss = parseInt(d.sig_short||0);
      var card = _c('coin-'+coin);
      if (!card) continue;
      var hasPos = ls>=open_threshold || ss>=open_threshold;
      var hasSig = ls>=1 || ss>=1;
      card.className = 'signal-card' + (hasPos?' sc-active':(hasSig?' sc-signal':''));
      var dot = card.querySelector('.signal-status-dot');
      if (dot) dot.className = 'signal-status-dot '+(hasPos?'ssd-active':(hasSig?'ssd-signal':'ssd-idle'));
      // update rows
      var rows = card.querySelectorAll('[data-field]');
      rows.forEach(function(r) {
        var f = r.getAttribute('data-field');
        if (f==='long_sig') r.textContent = ls;
        if (f==='short_sig') r.textContent = ss;
        if (f==='pm_long') { r.textContent = parseFloat(d.pm_long||0).toFixed(4); r.className=parseFloat(d.pm_long||0)>0.26?'c-green':(parseFloat(d.pm_long||0)>=0.25?'c-yellow':'c-red'); }
        if (f==='pm_short') { r.textContent = parseFloat(d.pm_short||0).toFixed(4); r.className=parseFloat(d.pm_short||0)>0.26?'c-red':(parseFloat(d.pm_short||0)>=0.25?'c-yellow':'c-muted'); }
        if (f==='confidence') r.textContent = parseFloat(d.confidence||0).toFixed(3);
      });
      // progress bars
      var lb = card.querySelector('.sig-bar-fill.long-bar');
      var sb = card.querySelector('.sig-bar-fill.short-bar');
      if (lb) lb.style.width = Math.min(ls*20,100)+'%';
      if (sb) sb.style.width = Math.min(ss*20,100)+'%';
    }
  }

  function patchTrades(trades) {
    var tbody = _c('trade-tbody');
    if (!tbody) return;
    if (!trades || !trades.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty-state"><div class="empty-icon">📊</div>No trade history yet</td></tr>';
      return;
    }
    // Only rebuild if count changed (avoid flicker when no new trades)
    var cur = tbody.querySelectorAll('tr').length;
    if (cur === trades.length) return;
    var rows = '';
    for (var i=trades.length-1; i>=0; i--) {
      var t = trades[i];
      var tag = (t.tag||'').toUpperCase();
      var side = (t.side||'').toUpperCase();
      var tagHtml = tag==='ENTRY'?'<span class="tag tag-entry">ENTRY</span>':
                    tag==='DCA'  ?'<span class="tag tag-dca">DCA</span>':
                    tag==='CLOSE'?'<span class="tag tag-close">CLOSE</span>':
                    tag==='PARTIAL_TP'?'<span class="tag tag-tp">PTP</span>':
                    tag==='DUP_CLOSE'?'<span class="tag tag-other">DUP</span>':
                    '<span class="tag tag-other">'+(tag||'-')+'</span>';
      var pnlPct = t.pnl_pct!=null ? parseFloat(t.pnl_pct) : null;
      var realUsd = t.realized_profit_usd!=null ? parseFloat(t.realized_profit_usd) : null;
      var sl2=parseFloat(t.sl||0), tp3=parseFloat(t.tp||0);
      rows += '<tr>';
      rows += '<td class="c-faint" style="font-family:var(--font-mono);font-size:10px">'+(t.time_str||'?')+'</td>';
      rows += '<td class="col-sym">'+(t.symbol||'?')+'</td>';
      rows += '<td><span class="side-badge '+(side==='BUY'?'side-buy':'side-sell')+'">'+side+'</span></td>';
      rows += '<td>'+tagHtml+'</td>';
      rows += '<td class="col-price">'+(parseFloat(t.qty||0)).toFixed(6)+'</td>';
      rows += '<td class="col-price">$'+(parseFloat(t.price||0)).toFixed(5)+'</td>';
      rows += '<td>'+(sl2>0?'<span class="sl-val">$'+sl2.toFixed(2)+'</span>':'<span class="c-faint">-</span>')+'</td>';
      rows += '<td>'+(tp3>0?'<span class="tp-val">$'+tp3.toFixed(2)+'</span>':'<span class="c-faint">-</span>')+'</td>';
      rows += '<td>'+(pnlPct!=null?'<span class="'+(pnlPct>=0?'c-green':'c-red')+'">'+(pnlPct>=0?'+':'')+pnlPct.toFixed(3)+'%</span>':'<span class="c-faint">-</span>')+'</td>';
      rows += '<td>'+(realUsd!=null?'<span class="'+(realUsd>=0?'c-green':'c-red')+'">$'+(realUsd>=0?'+':'')+realUsd.toFixed(4)+'</span>':'<span class="c-faint">-</span>')+'</td>';
      rows += '</tr>';
    }
    tbody.innerHTML = rows;
  }

  var _lastUpdate = 0;
  function fetchAndPatch() {
    fetch('/api/data?_t='+Date.now())
      .then(function(r){ return r.json(); })
      .then(function(data) {
        patchKPI(data);
        patchPositions(data.positions || {});
        patchSignals(data.coins || {}, data.open_threshold || 3);
        patchTrades(data.trade_history || []);
        rebuildChart(data.history || []);
        var ind = _c('update-indicator');
        if (ind) { ind.textContent = 'Updated '+new Date().toUTCString().replace(' GMT',''); }
      })
      .catch(function(err){ console.warn('Dashboard fetch error:', err); });
  }

  // Initial draw happens from server-rendered HTML, then patch every 2s
  setTimeout(fetchAndPatch, 2000);
  setInterval(fetchAndPatch, 2000);
})();

// ── Clock ────────────────────────────────────────────────────
(function updateClock() {
  const el = document.getElementById('clock');
  if (el) {
    const now = new Date();
    const hh = String(now.getUTCHours()).padStart(2,'0');
    const mm = String(now.getUTCMinutes()).padStart(2,'0');
    const ss = String(now.getUTCSeconds()).padStart(2,'0');
    el.textContent = `${hh}:${mm}:${ss} UTC`;
  }
  setTimeout(updateClock, 1000);
})();

// ── Theme Toggle ─────────────────────────────────────────────
(function() {
  const btn = document.querySelector('[data-theme-toggle]');
  const html = document.documentElement;
  const SUN = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
  const MOON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  // Set initial icon
  if (btn) btn.innerHTML = html.getAttribute('data-theme') === 'dark' ? SUN : MOON;
  if (btn) btn.addEventListener('click', () => {
    const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    btn.innerHTML = next === 'dark' ? SUN : MOON;
    btn.setAttribute('aria-label', `Switch to ${next === 'dark' ? 'light' : 'dark'} mode`);
  });
})();

// ── Nav / Section switching ───────────────────────────────────
const SECTIONS = { overview: 'Overview', positions: 'Positions', history: 'Trade History', signals: 'Signal Monitor' };
const ALL_SECS = ['sec-overview','sec-positions','sec-history','sec-signals'];

function showSection(id, el) {
  // Show/hide sections
  ALL_SECS.forEach(s => {
    const sec = document.getElementById(s);
    if (sec) sec.style.display = (s === 'sec-' + id) ? '' : 'none';
  });
  // Update nav active
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  if (el) el.classList.add('active');
  // Update page title
  const t = document.getElementById('page-title-text');
  if (t) t.textContent = SECTIONS[id] || id;
}

// Default: show all (overview page = show all sections for single-page feel)
// Actually on first load show all
ALL_SECS.forEach(s => {
  const sec = document.getElementById(s);
  if (sec) sec.style.display = '';
});

function scrollToCoin(coin) {
  const el = document.getElementById('coin-' + coin);
  if (el) {
    // Switch to signals view
    showSection('signals', document.querySelectorAll('.nav-item')[3]);
    setTimeout(() => el.scrollIntoView({ behavior: 'smooth', block: 'center' }), 80);
  }
}

// ── Chart.js Account Value ───────────────────────────────────
window.addEventListener('load', function() {
  const raw = document.getElementById('chart-data');
  const badge = document.getElementById('chart-pts-badge');
  if (!raw) return;
  let pts = [];
  try { pts = JSON.parse(raw.textContent); } catch(e) { return; }
  if (!pts.length) return;

  if (badge) badge.textContent = pts.length + ' pts';

  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  const labels = pts.map(p => {
    const d = new Date(p.ts * 1000);
    return d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false });
  });
  const vals = pts.map(p => p.v);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const trend = vals[vals.length - 1] >= vals[0];
  const accentColor = trend ? '#22d47e' : '#ff4d6a';
  const accentGlow  = trend ? 'rgba(34,212,126,' : 'rgba(255,77,106,';

  const canvas = document.getElementById('achart');
  if (!canvas) return;

  new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: vals,
        borderColor: accentColor,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: accentColor,
        fill: true,
        backgroundColor: function(ctx) {
          const chart = ctx.chart;
          const { ctx: c, chartArea } = chart;
          if (!chartArea) return accentGlow + '0.15)';
          const grad = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          grad.addColorStop(0, accentGlow + '0.2)');
          grad.addColorStop(1, accentGlow + '0)');
          return grad;
        },
        tension: 0.4,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: isDark ? '#141920' : '#fff',
          borderColor: isDark ? '#1e2738' : '#dde3f0',
          borderWidth: 1,
          titleColor: isDark ? '#5e7294' : '#5a6a90',
          bodyColor: isDark ? '#e8f0ff' : '#1a2340',
          padding: 10,
          titleFont: { family: 'Inter', size: 11 },
          bodyFont: { family: 'JetBrains Mono', size: 12, weight: '600' },
          callbacks: {
            label: ctx => ' $' + ctx.raw.toFixed(2),
          }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            color: isDark ? '#3a4a62' : '#a0aec0',
            font: { family: 'Inter', size: 10 },
            maxTicksLimit: 6,
          },
          border: { display: false },
        },
        y: {
          position: 'right',
          grid: { color: isDark ? '#1e2738' : '#dde3f0', drawBorder: false },
          ticks: {
            color: isDark ? '#3a4a62' : '#a0aec0',
            font: { family: 'JetBrains Mono', size: 10 },
            callback: v => '$' + v.toFixed(0),
            maxTicksLimit: 5,
          },
          border: { display: false },
        }
      }
    }
  });
});
</script>
</body>
</html>"""


@app.route("/")
def index() -> str:
    sig_files = read_signal_files()
    csv_data = read_signal_csvs()
    trader_data = read_trader_status() or {}
    account = trader_data.get("account", {}) or {}
    positions = trader_data.get("positions", {}) or {}
    rr = read_runner_ready() or {}
    trainer = read_trainer_status() or {}
    history = read_account_history(60)
    pnl_ledger = read_pnl_ledger()
    pnl_total = float(pnl_ledger.get("total_realized_profit_usd", 0.0))
    trade_hist_raw = read_trade_history(50)
    cfg = _read_json(CONFIG_PATH, {})
    symbol_configs = load_symbol_configs()

    # Add formatted time string to each trade history entry
    trade_history = []
    for t in trade_hist_raw:
        entry = dict(t)
        ts = entry.get("ts")
        if isinstance(ts, (int, float)):
            entry["time_str"] = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
        else:
            entry["time_str"] = "?"
        trade_history.append(entry)

    coins: Dict[str, Any] = {}
    for coin, d in sig_files.items():
        row = dict(d)
        if coin in csv_data:
            row.update(csv_data[coin])
        coins[coin] = row

    trade_enabled = bool(cfg.get("trade_enabled", False)) if isinstance(cfg, dict) else False
    open_threshold = int(cfg.get("open_threshold", 3)) if isinstance(cfg, dict) else 3
    runner_ready = bool(rr.get("ready", False)) if isinstance(rr, dict) else False

    return render_template_string(
        TEMPLATE,
        coins=coins,
        account=account,
        positions=positions,
        trainer=trainer,
        history=history,
        history_json=json.dumps(history),
        runner_ready=runner_ready,
        trade_enabled=trade_enabled,
        open_threshold=max(1, open_threshold),
        config=cfg if isinstance(cfg, dict) else {},
        symbol_configs=symbol_configs,
        pnl_total=pnl_total,
        trade_history=trade_history,
    )


@app.route("/api/debug")
def api_debug():
    """Shows resolved paths and whether key files exist. Visit /api/debug to diagnose missing data."""
    def _exists(p):
        return {"path": p, "exists": os.path.isfile(p) or os.path.isdir(p)}

    files = {
        "hub_dir":           _exists(HUB_DIR),
        "trader_status":     _exists(os.path.join(HUB_DIR, "trader_status.json")),
        "pnl_ledger":        _exists(os.path.join(HUB_DIR, "pnl_ledger.json")),
        "trade_history":     _exists(os.path.join(HUB_DIR, "trade_history.jsonl")),
        "account_history":   _exists(os.path.join(HUB_DIR, "account_value_history.jsonl")),
        "runner_ready":      _exists(os.path.join(HUB_DIR, "runner_ready.json")),
        "config":            _exists(CONFIG_PATH),
        "sig_dir":           _exists(SIG_DIR),
        "trainer_status":    _exists(TRAINER_STATUS_PATH),
    }
    coins = load_coins()
    for coin in coins:
        folder = coin_folder(coin)
        files[f"{coin}_long_sig"]  = _exists(os.path.join(folder, "long_dca_signal.txt"))
        files[f"{coin}_short_sig"] = _exists(os.path.join(folder, "short_dca_signal.txt"))
        files[f"{coin}_csv"]       = _exists(os.path.join(SIG_DIR, f"{coin}_signals.csv"))

    return jsonify({
        "BASE_DIR":  BASE_DIR,
        "ROOT_DIR":  ROOT_DIR,
        "HUB_DIR":   HUB_DIR,
        "SIG_DIR":   SIG_DIR,
        "CONFIG":    CONFIG_PATH,
        "files":     files,
    })


@app.route("/api/data")
def api_data():
    """JSON endpoint polled every 2s by the browser for live updates."""
    sig_files  = read_signal_files()
    csv_data   = read_signal_csvs()
    trader_data = read_trader_status() or {}
    account    = trader_data.get("account", {}) or {}
    positions  = trader_data.get("positions", {}) or {}
    rr         = read_runner_ready() or {}
    trainer    = read_trainer_status() or {}
    history    = read_account_history(60)
    pnl_ledger = read_pnl_ledger()
    pnl_total  = float(pnl_ledger.get("total_realized_profit_usd", 0.0))
    trade_hist_raw = read_trade_history(50)
    cfg        = _read_json(CONFIG_PATH, {})

    trade_history = []
    for t in trade_hist_raw:
        entry = dict(t)
        ts = entry.get("ts")
        if isinstance(ts, (int, float)):
            entry["time_str"] = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
        else:
            entry["time_str"] = "?"
        trade_history.append(entry)

    coins: Dict[str, Any] = {}
    for coin, d in sig_files.items():
        row = dict(d)
        if coin in csv_data:
            row.update(csv_data[coin])
        # add confidence/direction from thinker files
        folder = ROOT_DIR if coin == "BTC" else os.path.join(ROOT_DIR, coin)
        row["confidence"] = _read_text(os.path.join(folder, "signal_confidence.txt"), "0")
        row["direction"]  = _read_text(os.path.join(folder, "signal_direction.txt"),  "NEUTRAL")
        row["stale"]      = _read_text(os.path.join(folder, "signal_stale.txt"),       "0")
        coins[coin] = row

    trade_enabled  = bool(cfg.get("trade_enabled", False)) if isinstance(cfg, dict) else False
    open_threshold = int(cfg.get("open_threshold", 3))     if isinstance(cfg, dict) else 3
    runner_ready   = bool(rr.get("ready", False))          if isinstance(rr, dict) else False

    return jsonify({
        "ts":              time.time(),
        "account":         account,
        "positions":       positions,
        "coins":           coins,
        "history":         history,
        "trade_history":   trade_history,
        "trainer_patterns": trainer.get("patterns_saved", trainer.get("total_patterns", "--")),
        "runner_ready":    runner_ready,
        "trade_enabled":   trade_enabled,
        "open_threshold":  max(1, open_threshold),
        "pnl_total":       pnl_total,
    })


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    print(f"BASE_DIR={BASE_DIR}")
    print(f"ROOT_DIR={ROOT_DIR}")
    print(f"HUB_DIR={HUB_DIR}")
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=True)
