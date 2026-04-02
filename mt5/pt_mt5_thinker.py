#!/usr/bin/env python3
"""
MT5 Thinker  --  v2.1
CSV signal file -> long/short signal txt files

Changes from v2.0:
  • ML Scorer integration   -- loads XGBoost model (ml_model_{coin}_{tf}.json)
                               and blends ML probability with cosine-similarity
                               signal. Falls back to cosine-only if model absent.
  • Blend mode              -- PT_MT5_THINKER_ML_BLEND env var controls weight:
                               1.0 = ML only, 0.0 = cosine only, 0.6 = default
  • Per-coin ML model reload-- hot-reloads model if .json mtime changes

Changes from v1:
  • Signal confidence scoring  -- reads signal strength AND CSV signal in parallel
  • Staleness detection        -- marks runner_ready=False if CSVs are old
  • Exporter retry             -- retries failed exports up to N times with backoff
  • Config hot-reload          -- re-reads mt5_config.json each export cycle
  • Graceful degradation       -- if exporter is missing, runs on pre-existing CSVs
  • Confidence file            -- writes signal_confidence.txt for dashboard
"""

import csv
import json
import math
import os
import subprocess
import sys
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Constants / env
# ---------------------------------------------------------------------------

DEFAULT_COINS = ["BTC", "ETH", "XRP", "DOGE", "BNB"]

BASE_DIR            = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR            = BASE_DIR
SIGNAL_HISTORY_DIR  = os.path.join(BASE_DIR, "signal_history")

_hub_env = str(os.environ.get("POWERTRADER_HUB_DIR", "")).strip()
if _hub_env:
    _hub_candidate = os.path.abspath(_hub_env)
    if os.path.commonpath([BASE_DIR, _hub_candidate]) == BASE_DIR:
        HUB_DIR = _hub_candidate
    else:
        HUB_DIR = os.path.join(BASE_DIR, "hub_data")
        print(f"[WARN] POWERTRADER_HUB_DIR outside mt5 ignored: {_hub_candidate} -> {HUB_DIR}")
else:
    HUB_DIR = os.path.join(BASE_DIR, "hub_data")

RUNNER_READY_PATH   = os.path.join(HUB_DIR, "runner_ready.json")

MT5_CONFIG_PATH     = os.path.join(BASE_DIR, "mt5_config.json")
ML_SCORER_PATH      = os.path.join(BASE_DIR, "pt_mt5_ml_scorer.py")

# Path to the legacy exporter (for backward compatibility; not used after inlining, but checked for existence)
EXPORTER_PATH = os.path.join(BASE_DIR, "pt_mt5_signal_exporter.py")

AUTO_EXPORT             = str(os.environ.get("PT_MT5_THINKER_AUTO_EXPORT", "1")).strip().lower() \
                          not in {"0", "false", "no", "off"}
EXPORT_INTERVAL_SECONDS = max(30,  int(float(os.environ.get("PT_MT5_THINKER_EXPORT_INTERVAL", "120") or 120)))
LOOKBACK_DAYS           = max(1,   int(float(os.environ.get("PT_MT5_THINKER_LOOKBACK_DAYS", "14") or 14)))
POLL_INTERVAL_SECONDS   = max(5,   int(float(os.environ.get("PT_MT5_THINKER_POLL_INTERVAL", "10") or 10)))
EXPORT_MAX_RETRIES      = max(1,   int(float(os.environ.get("PT_MT5_THINKER_EXPORT_RETRIES", "3") or 3)))
EXPORT_RETRY_DELAY      = max(5.0, float(os.environ.get("PT_MT5_THINKER_EXPORT_RETRY_DELAY", "15") or 15))
EXPORT_MAX_WORKERS      = max(1,   int(float(os.environ.get("PT_MT5_THINKER_EXPORT_WORKERS", "1") or 1)))

# CSV staleness: if the newest CSV is older than this, mark runner not-ready
CSV_STALE_SECONDS       = max(60, int(float(os.environ.get("PT_MT5_THINKER_CSV_STALE_SECS", "600") or 600)))

# ML blend weight: 0.0 = cosine-only, 1.0 = ML-only, 0.6 = default blend
ML_BLEND_WEIGHT         = max(0.0, min(1.0, float(os.environ.get("PT_MT5_THINKER_ML_BLEND", "0.6") or 0.6)))

# ---------------------------------------------------------------------------
# ML scorer integration
# ---------------------------------------------------------------------------


# --- Inlined MLScorer and dependencies from pt_mt5_ml_scorer.py ---
import math
import numpy as np

def _rsi_from_pct(pct_changes: List[float], period: int = 14) -> float:
    if len(pct_changes) < period:
        return 50.0
    recent = pct_changes[-period:]
    gains = [x for x in recent if x > 0]
    losses = [-x for x in recent if x < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def _atr_from_window(window: List[float], period: int = 14) -> float:
    if not window:
        return 1.0
    use = window[-period:] if len(window) >= period else window
    if not use:
        return 1.0
    mean = sum(use) / len(use)
    variance = sum((x - mean) ** 2 for x in use) / len(use)
    return math.sqrt(variance) or 1.0

def build_features(window: List[float]) -> List[float]:
    n = len(window)
    if n == 0:
        return [0.0] * 20
    if n < 12:
        window = [0.0] * (12 - n) + window
    else:
        window = window[-12:]
    raw = list(window)
    mean_val = sum(raw) / 12
    std_val  = _atr_from_window(raw)
    momentum = sum(raw[8:]) - sum(raw[:4])
    skew     = sum((x - mean_val) ** 3 for x in raw) / (12 * (std_val ** 3 + 1e-9))
    rsi      = _rsi_from_pct(raw)
    atr_norm = mean_val / (std_val + 1e-9)
    trend    = sum(i * x for i, x in enumerate(raw)) / 12
    reversal = raw[-1] - raw[-2] if len(raw) >= 2 else 0.0
    stat_feats = [mean_val, std_val, momentum, skew, rsi / 100.0, atr_norm, trend, reversal]
    return raw + stat_feats

class MLScorer:
    """
    Load a trained XGBoost model and score live price windows.
    Usage:
        scorer = MLScorer(model_path)
        long_sig, short_sig = scorer.predict(window_pct_changes)
    """
    def __init__(self, model_path: str):
        self._model_path = model_path
        self._model = None
        self._meta: Dict = {}
        self._classes: List[int] = [-1, 0, 1]
        self._loaded_mtime: float = 0.0
        self._load()

    def _check_deps(self) -> bool:
        try:
            import xgboost  # noqa: F401
            import sklearn  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self) -> None:
        if not self._check_deps():
            return
        try:
            import xgboost as xgb
            m = xgb.XGBClassifier()
            m.load_model(self._model_path)
            self._model = m
            self._loaded_mtime = os.path.getmtime(self._model_path)
            meta_path = self._model_path.replace(".json", ".meta.json")
            if os.path.isfile(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    self._meta = json.load(f)
                self._classes = [int(c) for c in self._meta.get("classes", [-1, 0, 1])]
            log(f"[ML] Model loaded from {self._model_path} "
                f"(samples={self._meta.get('samples','?')} "
                f"cv={self._meta.get('cv_accuracy','?')})")
        except Exception as e:
            log(f"[ML] Model load error: {e}")
            self._model = None

    def _maybe_reload(self) -> None:
        try:
            mtime = os.path.getmtime(self._model_path)
            if mtime > self._loaded_mtime + 1:
                log("[ML] Model file changed, reloading...")
                self._load()
        except Exception:
            pass

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def predict(self, window: List[float]) -> Tuple[int, int]:
        if not self.is_loaded:
            return 0, 0
        self._maybe_reload()
        try:
            feats = build_features(window)
            X = np.array([feats], dtype=np.float32)
            probs = self._model.predict_proba(X)[0]
            class_prob: Dict[int, float] = {}
            for idx, cls in enumerate(self._classes):
                if idx < len(probs):
                    class_prob[cls] = float(probs[idx])
            prob_long  = class_prob.get(1,  0.0)
            prob_short = class_prob.get(-1, 0.0)
            prob_flat  = class_prob.get(0,  1.0)
            confidence = 1.0 - prob_flat
            long_sig  = int(round(prob_long  * confidence * 8))
            short_sig = int(round(prob_short * confidence * 8))
            return max(0, min(8, long_sig)), max(0, min(8, short_sig))
        except Exception as e:
            log(f"[ML] Predict error: {e}")
            return 0, 0

    def predict_with_confidence(self, window: List[float]) -> Dict:
        if not self.is_loaded:
            return {"long_sig": 0, "short_sig": 0, "confidence": 0.0,
                    "prob_long": 0.0, "prob_short": 0.0, "prob_flat": 1.0}
        try:
            feats = build_features(window)
            X = np.array([feats], dtype=np.float32)
            probs = self._model.predict_proba(X)[0]
            class_prob: Dict[int, float] = {}
            for idx, cls in enumerate(self._classes):
                if idx < len(probs):
                    class_prob[cls] = float(probs[idx])
            prob_long  = class_prob.get(1,  0.0)
            prob_short = class_prob.get(-1, 0.0)
            prob_flat  = class_prob.get(0,  1.0)
            confidence = 1.0 - prob_flat
            long_sig   = max(0, min(8, int(round(prob_long  * confidence * 8))))
            short_sig  = max(0, min(8, int(round(prob_short * confidence * 8))))
            return {
                "long_sig":   long_sig,
                "short_sig":  short_sig,
                "confidence": round(confidence, 4),
                "prob_long":  round(prob_long,  4),
                "prob_short": round(prob_short, 4),
                "prob_flat":  round(prob_flat,  4),
            }
        except Exception as e:
            log(f"[ML] Predict error: {e}")
            return {"long_sig": 0, "short_sig": 0, "confidence": 0.0,
                    "prob_long": 0.0, "prob_short": 0.0, "prob_flat": 1.0}

# Per-coin MLScorer cache: coin -> MLScorer or None
_ml_scorers: Dict[str, MLScorer] = {}

def _get_ml_scorer(coin: str, memory_dir: str, tf_label: str) -> Optional[MLScorer]:
    try:
        model_path = os.path.join(memory_dir, f"ml_model_{coin.lower()}_{tf_label}.json")
        if not os.path.isfile(model_path):
            return None
        cache_key = f"{coin}_{tf_label}"
        existing = _ml_scorers.get(cache_key)
        if existing is not None:
            return existing
        scorer = MLScorer(model_path)
        _ml_scorers[cache_key] = scorer
        return scorer
    except Exception as e:
        log(f"[ML] Scorer load error for {coin}: {e}")
        return None


def _ml_score_window(
    coin: str,
    memory_dir: str,
    tf_label: str,
    csv_window: List[float],
    csv_long_sig: int,
    csv_short_sig: int,
) -> Tuple[int, int, float]:
    """
    Blend ML signal with cosine-similarity CSV signal.

    Returns (long_sig, short_sig, ml_confidence).
    If ML model not available, returns the raw CSV signals unchanged.
    """
    scorer = _get_ml_scorer(coin, memory_dir, tf_label)
    if scorer is None or not scorer.is_loaded:
        return csv_long_sig, csv_short_sig, 0.0

    try:
        result = scorer.predict_with_confidence(csv_window)
        ml_long  = result["long_sig"]
        ml_short = result["short_sig"]
        ml_conf  = result["confidence"]

        # Blend: weighted average of ML and cosine signals
        blend = ML_BLEND_WEIGHT
        blended_long  = round(blend * ml_long  + (1.0 - blend) * csv_long_sig)
        blended_short = round(blend * ml_short + (1.0 - blend) * csv_short_sig)

        return max(0, min(8, int(blended_long))), max(0, min(8, int(blended_short))), ml_conf
    except Exception as e:
        log(f"[ML] Blend error for {coin}: {e}")
        return csv_long_sig, csv_short_sig, 0.0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


_LOG_LOCK = threading.Lock()
_MT5_EXPORT_LOCK = threading.Lock()
_WARNED_SHORT_MIRROR: Set[str] = set()
_WARNED_SHORT_MISSING: Set[str] = set()
_WARNED_DISABLED_EXPORT: Set[str] = set()
_WARNED_DISABLED_UPDATE: Set[str] = set()


def log(msg: str) -> None:
    line = f"[{utc_now()}] {msg}"
    with _LOG_LOCK:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            # Windows CP1252 / legacy terminal fallback - strip non-ASCII safely
            enc = sys.stdout.encoding or "ascii"
            safe = line.encode(enc, errors="replace").decode(enc, errors="replace")
            print(safe, flush=True)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_coins(config_path: str) -> List[str]:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        rows = cfg.get("symbols") or []
        coins = [
            str(row.get("bot_symbol", "")).strip().upper()
            for row in rows
            if isinstance(row, dict)
        ]
        out = [c for c in coins if c]
        return out or list(DEFAULT_COINS)
    except Exception:
        return list(DEFAULT_COINS)


def load_symbol_map(config_path: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {c: f"{c}USD" for c in DEFAULT_COINS}
    mapping["DOGE"] = "DOGUSD"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for row in (cfg.get("symbols") or []):
            coin = str(row.get("bot_symbol", "")).strip().upper()
            sym  = str(row.get("mt5_symbol", "")).strip().upper()
            if coin and sym:
                mapping[coin] = sym
    except Exception:
        pass
    return mapping


def load_enabled_symbol_flags(config_path: str) -> Dict[str, bool]:
    """Return bot_symbol -> whether at least one direction is enabled."""
    enabled: Dict[str, bool] = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for row in (cfg.get("symbols") or []):
            if not isinstance(row, dict):
                continue
            coin = str(row.get("bot_symbol", "")).strip().upper()
            if not coin:
                continue
            en_long = bool(row.get("enable_long", True))
            en_short = bool(row.get("enable_short", True))
            enabled[coin] = bool(en_long or en_short)
    except Exception:
        pass
    return enabled


def _normalize_timeframe(value: str) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "1m":    "1hour", "m1":    "1hour",   # fallback defaults to H1
        "5m":    "5min",  "m5":    "5min",    "5min":  "5min",
        "15m":   "15min", "m15":   "15min",   "15min": "15min",
        "30m":   "30min", "m30":   "30min",   "30min": "30min",
        "1h":    "1hour", "h1":    "1hour",   "1hour": "1hour",
        "4h":    "4hour", "h4":    "4hour",   "4hour": "4hour",
        "1d":    "1day",  "d1":    "1day",    "1day":  "1day",
        "1w":    "1week", "w1":    "1week",   "1week": "1week",
        "1min":  "1min",  "m1min": "1min",
    }
    return aliases.get(raw, "1hour")


def load_runtime_settings(config_path: str) -> Dict[str, str]:
    cfg: Dict = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    tf_env = str(os.environ.get("PT_MT5_THINKER_TIMEFRAME", "") or "").strip()
    tf_cfg = str(
        cfg.get("timeframe_vote") or cfg.get("signal_timeframe") or cfg.get("timeframe") or ""
    ).strip()
    return {"signal_timeframe": _normalize_timeframe(tf_env or tf_cfg or "1hour")}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def coin_dir(coin: str) -> str:
    # BTC → project root (matches bridge's signals_root for BTC)
    # Others → project_root/COIN/ (matches bridge's signals_root/SYM/ for others)
    if coin.upper() == "BTC":
        return ROOT_DIR
    return os.path.join(ROOT_DIR, coin.upper())


def coin_memory_dir(coin: str) -> str:
    coin_key = str(coin or "").upper()
    if coin_key != "BTC":
        base = os.path.join(ROOT_DIR, coin_key)
        nested = os.path.join(base, coin_key)
        if os.path.isdir(nested):
            # Backward-compatibility for older trainer runs that wrote to COIN/COIN.
            def _latest_short_mtime(folder: str) -> float:
                try:
                    best = 0.0
                    for name in os.listdir(folder):
                        if name.startswith("memories_short_") and name.endswith(".txt"):
                            p = os.path.join(folder, name)
                            best = max(best, os.path.getmtime(p))
                    return best
                except Exception:
                    return 0.0

            base_m = _latest_short_mtime(base)
            nested_m = _latest_short_mtime(nested)
            if nested_m > base_m:
                return nested
        return base

    # BTC historically lived in ROOT_DIR; prefer ROOT_DIR/BTC when files are present.
    btc_dir = os.path.join(ROOT_DIR, "BTC")
    if os.path.isdir(btc_dir):
        return btc_dir
    return ROOT_DIR


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def write_text(path: str, value: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(value).strip())


def write_runner_ready(ready: bool, stage: str, extra: Optional[Dict] = None) -> None:
    try:
        os.makedirs(HUB_DIR, exist_ok=True)
        payload = {
            "timestamp": time.time(),
            "ready":     bool(ready),
            "stage":     str(stage),
        }
        if extra:
            payload.update(extra)
        with open(RUNNER_READY_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        log(f"[WARN] write_runner_ready failed: {e}")


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def _int_field(row: Dict[str, str], keys: Tuple[str, ...], default: int = 0) -> int:
    for k in keys:
        if k in row and str(row[k]).strip():
            try:
                return int(float(str(row[k]).strip()))
            except Exception:
                continue
    return default


def _float_field(row: Dict[str, str], keys: Tuple[str, ...], default: float = 0.0) -> float:
    for k in keys:
        if k in row and str(row[k]).strip():
            try:
                return float(str(row[k]).strip())
            except Exception:
                continue
    return default


def read_latest_signals(csv_path: str) -> Tuple[int, int, float, float, float]:
    """
    Returns (long_sig, short_sig, long_strength, short_strength, csv_age_seconds).
    csv_age_seconds = 0 if file not found.
    """
    if not os.path.isfile(csv_path):
        return 0, 0, 0.0, 0.0, 0.0

    age_secs = time.time() - os.path.getmtime(csv_path)

    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return 0, 0, 0.0, 0.0, age_secs

    if not rows:
        return 0, 0, 0.0, 0.0, age_secs

    last = rows[-1]
    long_sig  = _int_field(last,   ("long_sig",    "long_signal",  "long",   "long_strength"),  0)
    short_sig = _int_field(last,   ("short_sig",   "short_signal", "short",  "short_strength"), 0)
    long_str  = _float_field(last, ("long_strength", "long_score",  "long_conf", "long_sig"),   float(long_sig))
    short_str = _float_field(last, ("short_strength","short_score", "short_conf","short_sig"),  float(short_sig))

    return long_sig, short_sig, long_str, short_str, age_secs


# ---------------------------------------------------------------------------
# Confidence scoring  (NEW)
# ---------------------------------------------------------------------------

def _compute_confidence(
    long_sig: int,
    short_sig: int,
    long_strength: float,
    short_strength: float,
    csv_age_seconds: float,
    stale_threshold: float = CSV_STALE_SECONDS,
) -> float:
    """
    Returns a confidence score 0.0-1.0 for the dominant signal.
    Factors:
      1. Signal magnitude (long_sig vs short_sig difference)
      2. Raw strength from CSV if available
      3. Freshness penalty (gentle linear decay, not aggressive exponential)

    FIX: Old formula used exp(-age / stale_threshold) which decays to 0.37
    at 1 * stale_threshold seconds and to 0.14 at 2x.  With stale_threshold=600s
    and export_interval=120s, a signal written at t=0 has confidence *= exp(-120/600)
    = 0.819 by the next export -- rapidly collapsing to 0.  This caused the bridge
    to see near-zero confidence even on fresh, valid signals.

    New formula: linear decay from 1.0 at age=0 to 0.5 at age=stale_threshold,
    then clamps to 0.5 minimum so a stale-but-present signal still counts.
    This keeps confidence high during normal operation and only softly penalises
    slow exports -- the binary stale flag is the hard cutoff.
    """
    dominant = max(long_sig, short_sig)
    other    = min(long_sig, short_sig)

    if dominant == 0:
        return 0.0

    # Strength ratio -- how much stronger is the dominant signal
    strength_ratio = (dominant - other) / max(1, dominant)

    # Raw CSV strength bonus
    dom_strength  = long_strength if long_sig >= short_sig else short_strength
    norm_strength = min(dom_strength / max(1.0, dominant + 1), 1.0)

    # Gentle linear freshness: 1.0 at age=0, 0.5 at age=stale_threshold, floor 0.5
    if stale_threshold > 0 and csv_age_seconds > 0:
        freshness = max(0.5, 1.0 - 0.5 * (csv_age_seconds / stale_threshold))
    else:
        freshness = 1.0

    # Weighted composite
    raw_conf = 0.7 * strength_ratio + 0.3 * norm_strength
    return round(raw_conf * freshness, 4)


# ---------------------------------------------------------------------------
# Exporter runner (with retry)  (NEW)
# ---------------------------------------------------------------------------


# --- Inlined exporter logic and dependencies from pt_mt5_signal_exporter.py ---
import re
import platform
try:
    mt5 = __import__("MetaTrader5")
except ImportError:
    mt5 = None

def parse_dt(text: str) -> datetime:
    s = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Invalid datetime: {text}")

def rate_value(rate, field: str, default: float = 0.0) -> float:
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
    raw = str(name).strip().lower()
    aliases = {
        "m1": "1min", "1m": "1min", "1min": "1min",
        "m5": "5min", "5m": "5min", "5min": "5min",
        "m15": "15min", "15m": "15min", "15min": "15min",
        "m30": "30min", "30m": "30min", "30min": "30min",
        "h1": "1hour", "1h": "1hour", "1hour": "1hour",
        "h4": "4hour", "4h": "4hour", "4hour": "4hour",
        "d1": "1day", "1d": "1day", "1day": "1day",
        "w1": "1week", "1w": "1week", "1week": "1week",
    }
    if raw not in aliases:
        raise ValueError(f"Unsupported timeframe: {name}")
    return aliases[raw]

def timeframe_label_to_mt5_key(label: str) -> str:
    mapping = {
        "1min": "M1", "5min": "M5", "15min": "M15", "30min": "M30",
        "1hour": "H1", "4hour": "H4", "1day": "D1", "1week": "W1",
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
        log(f"[WARN] Failed to load memory file {path}: {e}")
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
        log(f"[WARN] Failed to load weights file {path}: {e}")
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
    if mt5 is None:
        log("[EXPORT] MetaTrader5 package not available.")
        return
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
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return token or default

def candles_to_price_changes(candles: List) -> List[float]:
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
    candidates.sort(key=lambda x: x[0])
    selected = candidates[: max(1, int(top_k))]
    matched_weights = []
    high_preds = []
    low_preds = []
    for avg_diff, base_w, high_val, low_val in selected:
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


def generate_short_signal(high_pred: float, low_pred: float) -> int:
    """Direction-aware short signal from predicted downside minus upside risk."""
    downside = abs(low_pred) if low_pred < 0 else 0.0
    upside_risk = high_pred if high_pred > 0 else 0.0
    edge = downside - upside_risk
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
    memory_path_short = os.path.join(memory_dir, f"memories_short_{timeframe_label}.txt")
    weights_path_short = os.path.join(memory_dir, f"memory_weights_short_{timeframe_label}.txt")
    memory_patterns_short = load_memory_file(memory_path_short)
    weights_short = load_weights_file(weights_path_short)

    # Guard against mirrored long/short memory files; this creates symmetric L/S signals.
    short_files_mirrored = False
    try:
        if os.path.isfile(memory_path) and os.path.isfile(memory_path_short):
            with open(memory_path, "r", encoding="utf-8") as f_long:
                with open(memory_path_short, "r", encoding="utf-8") as f_short:
                    short_files_mirrored = f_long.read().strip() == f_short.read().strip()
    except Exception:
        short_files_mirrored = False

    warn_key = f"{coin.upper()}::{timeframe_label}"
    if short_files_mirrored:
        if warn_key not in _WARNED_SHORT_MIRROR:
            log(f"[WARN] {coin}: short memory mirrors long memory; disabling short-pattern scoring for this export")
            _WARNED_SHORT_MIRROR.add(warn_key)
        memory_patterns_short = []
        weights_short = []
    else:
        _WARNED_SHORT_MIRROR.discard(warn_key)

    short_patterns_ready = bool(memory_patterns_short and weights_short)
    if not short_patterns_ready and not short_files_mirrored:
        if warn_key not in _WARNED_SHORT_MISSING:
            log(
                f"[WARN] Short pattern files missing or empty for {coin} {timeframe_label}: "
                f"{memory_path_short}, {weights_path_short}"
            )
            _WARNED_SHORT_MISSING.add(warn_key)
    else:
        _WARNED_SHORT_MISSING.discard(warn_key)
    log(f"[EXPORT] Loaded {len(memory_patterns)} patterns, {len(weights)} weights, threshold={threshold:.2f}")
    initialize_mt5_with_config(mt5_config or {})
    tf_const = timeframe_from_name(timeframe_label_to_mt5_key(timeframe_label))
    log(f"[EXPORT] Fetching {mt5_symbol} {timeframe_label} from MT5...")
    candles = mt5.copy_rates_range(mt5_symbol, tf_const, start_dt, end_dt)
    if candles is None or len(candles) == 0:
        raise RuntimeError(f"No candles fetched for {mt5_symbol}")
    log(f"[EXPORT] Got {len(candles)} candles")
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
        if not short_patterns_ready:
            short_sig = 0
        else:
            high_pred_short, low_pred_short = match_patterns(
                current_changes,
                memory_patterns_short,
                weights_short,
                threshold,
                top_k=top_k,
                max_match_diff=max_match_diff,
            )
            short_sig = generate_short_signal(high_pred_short, low_pred_short)
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
    log(f"[EXPORT] Wrote {len(signals)} signals to {output_path}")
    try:
        mt5.shutdown()
    except Exception:
        pass
    return output_path

def _is_auth_error_message(text: str) -> bool:
    t = str(text or "").lower()
    return (
        "authorization failed" in t
        or "mt5 initialize failed" in t
        or "mt5 login failed" in t
    )


def _run_exporter_once(coin: str, mt5_symbol: str, timeframe: str, config_path: str) -> Tuple[bool, bool]:
    try:
        # MT5 Python bindings are not reliably thread-safe across concurrent
        # initialize/login/copy_rates/shutdown calls. Serialize exporter access.
        with _MT5_EXPORT_LOCK:
            mt5_config = load_mt5_config(config_path)
            end_dt = datetime.now(timezone.utc)
            start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
            export_historical_signals(
                coin=coin,
                mt5_symbol=mt5_symbol,
                timeframe=timeframe,
                start_dt=start_dt,
                end_dt=end_dt,
                memory_dir=coin_memory_dir(coin),
                output_dir=SIGNAL_HISTORY_DIR,
                pattern_length=12,
                top_k=25,
                max_match_diff=2.0,
                mt5_config=mt5_config,
            )
        return True, False
    except Exception as e:
        auth_error = _is_auth_error_message(str(e))
        log(f"[WARN] Exporter exception {coin}: {e}")
        return False, auth_error
    finally:
        # Ensure MT5 session is closed even when export fails mid-run.
        try:
            if mt5 is not None:
                mt5.shutdown()
        except Exception:
            pass


def _run_exporter_with_retry(
    coin: str, mt5_symbol: str, timeframe: str, config_path: str,
    max_retries: int = EXPORT_MAX_RETRIES, retry_delay: float = EXPORT_RETRY_DELAY,
) -> bool:
    for attempt in range(1, max_retries + 1):
        ok, auth_error = _run_exporter_once(coin, mt5_symbol, timeframe, config_path)
        if ok:
            return True
        if auth_error:
            log(f"[WARN] Exporter {coin} aborted retries due to MT5 authorization failure")
            return False
        if attempt < max_retries:
            log(f"[RETRY] Exporter {coin} attempt {attempt}/{max_retries}, waiting {retry_delay}s")
            time.sleep(retry_delay)
    log(f"[WARN] Exporter {coin} failed after {max_retries} attempts")
    return False


def refresh_signal_csvs(
    symbol_map: Dict[str, str],
    coins: List[str],
    timeframe: str,
    config_path: str,
) -> Dict[str, bool]:
    """
    Run signal exporter for all coins in parallel.
    Returns {coin: success_bool}.
    """
    if not AUTO_EXPORT:
        return {c: False for c in coins}

    results: Dict[str, bool] = {}

    enabled_flags = load_enabled_symbol_flags(config_path)
    active_coins: List[str] = []
    for c in coins:
        if enabled_flags.get(c.upper(), True):
            active_coins.append(c)
            continue
        if c.upper() not in _WARNED_DISABLED_EXPORT:
            log(f"[EXPORT] {c}: skipped (disabled in config)")
            _WARNED_DISABLED_EXPORT.add(c.upper())
        results[c] = True

    if not active_coins:
        return results

    def _task(coin: str) -> Tuple[str, bool]:
        mt5_sym = symbol_map.get(coin, f"{coin}USD")
        ok = _run_exporter_with_retry(coin, mt5_sym, timeframe, config_path)
        return coin, ok

    workers = max(1, min(len(active_coins), EXPORT_MAX_WORKERS))
    if workers == 1:
        for c in active_coins:
            coin, ok = _task(c)
            results[coin] = ok
            status = "OK" if ok else "FAIL"
            log(f"[EXPORT] {coin}: {status}")
        return results

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_task, c): c for c in active_coins}
        for fut in as_completed(futures):
            coin = futures[fut]
            try:
                coin, ok = fut.result()
            except Exception as e:
                ok = False
                log(f"[WARN] Export future failed for {coin}: {e}")
            results[coin] = ok
            status = "OK" if ok else "FAIL"
            log(f"[EXPORT] {coin}: {status}")

    return results


# ---------------------------------------------------------------------------
# Coin update (writes all signal files)
# ---------------------------------------------------------------------------

def _read_recent_pct_changes(csv_path: str, n: int = 12) -> List[float]:
    """Read the last N close % changes from the signal CSV for ML feature input."""
    if not os.path.isfile(csv_path):
        return []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return []
        pct_key = None
        for k in ("pct_change", "close_pct", "return", "change_pct"):
            if k in rows[0]:
                pct_key = k
                break
        if pct_key is None:
            closes = []
            for r in rows:
                for k in ("close", "Close", "CLOSE"):
                    if k in r:
                        try:
                            closes.append(float(r[k]))
                        except Exception:
                            pass
                        break
            if len(closes) >= 2:
                changes = [(closes[i] - closes[i-1]) / closes[i-1] * 100.0
                           for i in range(1, len(closes))]
                return changes[-n:]
            return []
        recent = rows[-n:] if len(rows) >= n else rows
        return [float(r.get(pct_key, 0) or 0) for r in recent]
    except Exception:
        return []


def update_coin(coin: str) -> Dict:
    # --- Prevent writing signals for disabled coins ---
    try:
        with open(MT5_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg_data = json.load(f)
        sym_entry = next(
            (s for s in cfg_data.get("symbols", [])
             if isinstance(s, dict) and s.get("bot_symbol", "").upper() == coin.upper()),
            None
        )
        if sym_entry:
            enable_long = bool(sym_entry.get("enable_long", True))
            enable_short = bool(sym_entry.get("enable_short", True))
            if not enable_long and not enable_short:
                # Write zeroed signals so mtime stays fresh (prevents spurious stale warning
                # on re-enable, and keeps the folder layout consistent)
                folder = coin_dir(coin)
                write_text(os.path.join(folder, "long_dca_signal.txt"), "0")
                write_text(os.path.join(folder, "short_dca_signal.txt"), "0")
                write_text(os.path.join(folder, "signal_stale.txt"), "0")
                coin_key = coin.upper()
                if coin_key not in _WARNED_DISABLED_UPDATE:
                    log(f"[SKIP] {coin}: disabled in config (enable_long/short=False) — zeroed signals written")
                    _WARNED_DISABLED_UPDATE.add(coin_key)
                return {}
            _WARNED_DISABLED_UPDATE.discard(coin.upper())
    except Exception:
        pass

    # Read latest CSV signals, blend with XGBoost ML scorer if model exists,
    # and write signal txt files + confidence file.
    #
    # Signal txt files are ALWAYS rewritten every poll so their mtime stays fresh.
    # ML blend weight is controlled by PT_MT5_THINKER_ML_BLEND (default 0.6).
    # Falls back to cosine-only if ml_model_{coin}_{tf}.json not found.
    csv_path = os.path.join(SIGNAL_HISTORY_DIR, f"{coin}_signals.csv")
    long_sig, short_sig, long_str, short_str, csv_age = read_latest_signals(csv_path)

    folder = coin_dir(coin)

    # -- ML blend (if model available) --
    ml_conf   = 0.0
    ml_active = False
    try:
        runtime   = load_runtime_settings(MT5_CONFIG_PATH)
        tf_label  = runtime["signal_timeframe"]
        mem_dir   = coin_memory_dir(coin)
        csv_window = _read_recent_pct_changes(csv_path)
        if csv_window and ML_BLEND_WEIGHT > 0:
            long_sig, short_sig, ml_conf = _ml_score_window(
                coin, mem_dir, tf_label, csv_window, long_sig, short_sig
            )
            ml_active = ml_conf > 0
    except Exception as e:
        log(f"[ML] update_coin blend error for {coin}: {e}")

    # Always write signal files (refreshes mtime every poll cycle)
    write_text(os.path.join(folder, "long_dca_signal.txt"),  str(long_sig))
    write_text(os.path.join(folder, "short_dca_signal.txt"), str(short_sig))

    # Default profit margins
    write_text(os.path.join(folder, "futures_long_profit_margin.txt"),  "3.0")
    write_text(os.path.join(folder, "futures_short_profit_margin.txt"), "3.0")

    # Confidence score -- blend base confidence with ML confidence
    base_confidence = _compute_confidence(
        long_sig, short_sig, long_str, short_str, csv_age
    )
    confidence = max(base_confidence, ml_conf) if ml_active else base_confidence
    write_text(os.path.join(folder, "signal_confidence.txt"), f"{confidence:.4f}")
    write_text(os.path.join(folder, "ml_confidence.txt"),     f"{ml_conf:.4f}")

    # Dominant direction
    if long_sig > short_sig:
        direction = "LONG"
    elif short_sig > long_sig:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"
    write_text(os.path.join(folder, "signal_direction.txt"), direction)

    # CSV freshness flag
    is_stale = csv_age > CSV_STALE_SECONDS
    write_text(os.path.join(folder, "signal_stale.txt"), "1" if is_stale else "0")

    ml_tag = f" [ML conf={ml_conf:.2f}]" if ml_active else " [cosine-only]"
    status_str = (f"L{long_sig}({long_str:.1f}) S{short_sig}({short_str:.1f}) "
                  f"conf={confidence:.3f} dir={direction} "
                  f"age={csv_age:.0f}s{ml_tag} {'[STALE]' if is_stale else ''}")
    log(f"{coin}: {status_str}")

    return {
        "coin":       coin,
        "long_sig":   long_sig,
        "short_sig":  short_sig,
        "confidence": confidence,
        "ml_conf":    ml_conf,
        "direction":  direction,
        "stale":      is_stale,
        "csv_age":    csv_age,
    }


# ---------------------------------------------------------------------------
# Runner ready with staleness awareness
# ---------------------------------------------------------------------------

def _all_csvs_fresh(coins: List[str]) -> Tuple[bool, str]:
    """
    Returns (True, "") if all coin CSVs exist and are fresh,
    or (False, reason_string) otherwise.
    """
    stale_coins = []
    missing_coins = []
    for coin in coins:
        csv_path = os.path.join(SIGNAL_HISTORY_DIR, f"{coin}_signals.csv")
        if not os.path.isfile(csv_path):
            missing_coins.append(coin)
            continue
        age = time.time() - os.path.getmtime(csv_path)
        if age > CSV_STALE_SECONDS:
            stale_coins.append(f"{coin}({age:.0f}s)")

    if missing_coins:
        return False, f"Missing CSVs: {', '.join(missing_coins)}"
    if stale_coins:
        return False, f"Stale CSVs: {', '.join(stale_coins)}"
    return True, ""


def _zero_disabled_coin_signals(coins: List[str], enabled_flags: Dict[str, bool]) -> None:
    """Write neutral signal files once for fully disabled coins."""
    for coin in coins:
        if enabled_flags.get(coin.upper(), True):
            _WARNED_DISABLED_UPDATE.discard(coin.upper())
            continue
        try:
            folder = coin_dir(coin)
            write_text(os.path.join(folder, "long_dca_signal.txt"), "0")
            write_text(os.path.join(folder, "short_dca_signal.txt"), "0")
            write_text(os.path.join(folder, "signal_stale.txt"), "0")
            if coin.upper() not in _WARNED_DISABLED_UPDATE:
                log(f"[SKIP] {coin}: disabled in config (enable_long/short=False) — zeroed signals written")
                _WARNED_DISABLED_UPDATE.add(coin.upper())
        except Exception as e:
            log(f"[WARN] {coin}: failed to zero disabled signals: {e}")


def _enabled_active_coins(coins: List[str], enabled_flags: Dict[str, bool]) -> List[str]:
    return [c for c in coins if enabled_flags.get(c.upper(), True)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    log("MT5 Thinker v2 started (CSV -> signal txt bridge)")
    write_runner_ready(False, "starting")

    # Initial load
    coins      = load_coins(MT5_CONFIG_PATH)
    symbol_map = load_symbol_map(MT5_CONFIG_PATH)
    runtime    = load_runtime_settings(MT5_CONFIG_PATH)
    timeframe  = runtime["signal_timeframe"]
    enabled_flags = load_enabled_symbol_flags(MT5_CONFIG_PATH)
    active_coins = _enabled_active_coins(coins, enabled_flags)

    log(f"Coins: {coins}")
    log(f"Timeframe: {timeframe}")
    log(f"Export interval: {EXPORT_INTERVAL_SECONDS}s  Poll: {POLL_INTERVAL_SECONDS}s")
    log(f"Export workers: {EXPORT_MAX_WORKERS} (MT5 exporter serialized)")
    log(f"CSV stale threshold: {CSV_STALE_SECONDS}s  Export retries: {EXPORT_MAX_RETRIES}")

    last_export_ts = 0.0

    # Initial export pass
    try:
        refresh_signal_csvs(symbol_map, coins, timeframe, MT5_CONFIG_PATH)
        last_export_ts = time.time()
    except Exception as e:
        log(f"[WARN] Initial export failed: {e}")

    # Keep disabled-coin signal files fresh without processing them each poll.
    _zero_disabled_coin_signals(coins, enabled_flags)

    # Initial coin update
    for c in active_coins:
        try:
            update_coin(c)
        except Exception as e:
            log(f"[WARN] {c}: {e}")

    # Mark ready based on CSV freshness
    fresh, reason = _all_csvs_fresh(active_coins)
    write_runner_ready(fresh, "ready" if fresh else f"stale:{reason}",
                       extra={"coins": coins, "active_coins": active_coins, "timeframe": timeframe})
    if not fresh:
        log(f"[WARN] Runner marked not-ready: {reason}")

    # ── Main poll loop ──
    first_loop = True
    while True:
        try:
            now_ts = time.time()

            # Hot-reload config (pick up symbol changes without restart)
            try:
                new_coins  = load_coins(MT5_CONFIG_PATH)
                new_symmap = load_symbol_map(MT5_CONFIG_PATH)
                new_tf     = load_runtime_settings(MT5_CONFIG_PATH)["signal_timeframe"]
                new_enabled = load_enabled_symbol_flags(MT5_CONFIG_PATH)
                if new_coins != coins or new_tf != timeframe or new_enabled != enabled_flags:
                    log(f"[CONFIG] Hot-reload: coins={new_coins} tf={new_tf}")
                    coins      = new_coins
                    symbol_map = new_symmap
                    timeframe  = new_tf
                    enabled_flags = new_enabled
                    active_coins = _enabled_active_coins(coins, enabled_flags)
            except Exception as e:
                log(f"[WARN] Config hot-reload failed: {e}")

            # Periodic exporter refresh
            if AUTO_EXPORT and (now_ts - last_export_ts) >= EXPORT_INTERVAL_SECONDS:
                try:
                    refresh_signal_csvs(symbol_map, coins, timeframe, MT5_CONFIG_PATH)
                    # Anchor cadence to completion time so long exports do not
                    # trigger immediate back-to-back runs.
                    last_export_ts = time.time()
                except Exception as e:
                    log(f"[WARN] Periodic export failed: {e}")

            _zero_disabled_coin_signals(coins, enabled_flags)

            # Initial export + coin update already happened before entering the loop.
            # Skip one immediate pass so startup doesn't emit duplicate status lines.
            if first_loop:
                first_loop = False
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Update coin signal files
            for c in active_coins:
                try:
                    update_coin(c)
                except Exception as e:
                    log(f"[WARN] {c}: {e}")

            # Re-evaluate runner readiness
            fresh, reason = _all_csvs_fresh(active_coins)
            write_runner_ready(fresh, "ready" if fresh else f"stale:{reason}",
                               extra={"coins": coins, "active_coins": active_coins, "timeframe": timeframe})

            time.sleep(POLL_INTERVAL_SECONDS)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            # Keep the thinker alive even on unexpected runtime failures.
            log(f"[CRITICAL] Thinker loop error ({type(e).__name__}): {e}")
            log(traceback.format_exc())
            time.sleep(max(2, POLL_INTERVAL_SECONDS))


if __name__ == "__main__":
    raise SystemExit(main())
