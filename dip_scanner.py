"""
╔══════════════════════════════════════════════════════════════════╗
║           ALPHAEDGE DIP BUY SCANNER  v3.4                       ║
║           Unified Build — Bounce Tracking + Quality Improvements ║
╠══════════════════════════════════════════════════════════════════╣
║  PURPOSE                                                         ║
║  Scans a curated stock universe for healthy pullbacks in strong  ║
║  uptrends that are temporarily oversold.  Sends a structured     ║
║  Telegram alert ranked by quality tier (ELITE / STRONG /         ║
║  WATCHLIST) with buy zones, stops, and risk labels.              ║
║  Tracks alerted dips through their lifecycle: dipping →          ║
║  deepening → bouncing → cleared.                                 ║
║                                                                  ║
║  CHANGELOG  v3.4  (vs v3.3)                                      ║
║  ─────────────────────────                                        ║
║  NEW  Bounce tracking state machine — DipPhase enum, per-symbol  ║
║       dip_state_{sym} records in scanner_state.json.             ║
║  NEW  _run_bounce_pass() — checks DIPPING symbols each run,      ║
║       fires "DIP RECOVERY" alert when price recovers ≥ 3%.       ║
║  NEW  _run_deepen_pass() — fires "DIP DEEPENING" update when     ║
║       price extends ≥ 3% below the previous dip_low.            ║
║  NEW  format_bounce_alert() — compact recovery alert card.       ║
║  NEW  format_deepen_alert() — compact deepening update card.     ║
║  NEW  _purge_stale_dip_states() — auto-removes records > 48h.    ║
║  NEW  VIX-aware scoring — high-VIX penalty in qualify_dip().     ║
║  NEW  Sector clustering note in format_alert() header.           ║
║  FIX  calc_relative_strength() called with sym string not ctx    ║
║       dict — was a live TypeError bug (market_intel v3.2 API).   ║
║  FIX  ATR proxy floor — min $0.10 prevents collapsed buy zones   ║
║       on low-price stocks.                                        ║
║  IMPR Earnings proximity score penalty (-1 pt for 4–7 days out). ║
║  IMPR scan_window default start moved from 7:30 to 8:00 ET;      ║
║       pre-market data before 8 AM is unreliable from yfinance.  ║
║  IMPR ZoneInfo imported at top level (was local import in purge).║
║  IMPR _scan_one() logs exception type at DEBUG level.            ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ════════════════════════════════════════════════════════════════════
# SECTION 1 — STANDARD-LIBRARY IMPORTS
# ════════════════════════════════════════════════════════════════════
from __future__ import annotations

import fcntl
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, time as dtime, timedelta
from enum import Enum
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ════════════════════════════════════════════════════════════════════
# SECTION 2 — THIRD-PARTY IMPORTS
# ════════════════════════════════════════════════════════════════════
import numpy as np
import pandas as pd

# ════════════════════════════════════════════════════════════════════
# SECTION 3 — INTERNAL / MARKET-INTEL IMPORTS
# ════════════════════════════════════════════════════════════════════
from market_intel import (
    SECTORS,
    SYMBOL_EMOJI,
    SYMBOL_META,
    SYMBOL_TO_SECTOR,
    YAML_SETTINGS,
    STATE_FILE,               # "scanner_state.json" — shared cooldown store
    _yf_download,
    calc_relative_strength,   # v3.2 API: takes symbol STRING, not ctx dict
    can_alert,
    display_now,
    get_earnings_date,
    get_full_context,
    get_market_ctx,
    load_json,                # safe JSON reader — returns {} on any error
    mark_alert,
    market_now,
    send_telegram,
    tg_escape as md,
)

# ════════════════════════════════════════════════════════════════════
# SECTION 4 — PATHS & CONSTANTS
# ════════════════════════════════════════════════════════════════════

LOGS_DIR             = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
QUALIFIED_LOG_FILE   = LOGS_DIR / "dip_qualified.jsonl"

H_RULE = "─────────────────────"   # 21-char mobile-optimised rule

# ── Bounce / deepen configuration ────────────────────────────────
BOUNCE_PCT      = 3.0    # % recovery from dip_low to fire bounce alert
DEEPEN_PCT      = 3.0    # % extension below dip_low to fire deepen alert
DIP_TTL_HOURS   = 48     # auto-purge dip states older than this
DEEPEN_COOLDOWN = 2      # minimum hours between deepening alerts per symbol

# ════════════════════════════════════════════════════════════════════
# SECTION 5 — CONFIGURATION DATACLASS
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    # ── Qualification thresholds ──────────────────────────────────
    rsi_min:              float = 25
    rsi_max:              float = 48
    drop_1d_max:          float = -1.5
    drop_5d_max:          float = -4.0
    ath_min:              float = -30.0
    vol_ratio_min:        float = 0.6
    ema200_flex_pct:      float = 5.0
    ema_slope_min_pct:    float = 0.5

    # ── Operational settings ──────────────────────────────────────
    cooldown_hours:       int   = 4
    # Default start moved from 7:30 → 8:00 ET.
    # yfinance pre-market data before 8 AM is unreliable (thin bars,
    # stale quotes) — scanning earlier produces noisy context reads.
    scan_window:          tuple = (dtime(8, 0), dtime(20, 30))

    # ── Execution / concurrency ───────────────────────────────────
    fetch_workers:        int   = 5

    # ── Trade plan maths ─────────────────────────────────────────
    max_loss_pct:         float = 8.0

    # ── Alert display ─────────────────────────────────────────────
    top_per_tier:         int   = 5
    max_total_shown:      int   = 12

    # ── VIX stress thresholds ─────────────────────────────────────
    vix_caution:          float = 20.0   # score -1 when VIX above this
    vix_stress:           float = 25.0   # skip WATCHLIST tier above this


_YAML_COERCE: dict[str, Any] = {
    "rsi_min":            float,
    "rsi_max":            float,
    "drop_1d_max":        float,
    "drop_5d_max":        float,
    "ath_min":            float,
    "vol_ratio_min":      float,
    "ema200_flex_pct":    float,
    "ema_slope_min_pct":  float,
    "cooldown_hours":     int,
    "fetch_workers":      int,
    "max_loss_pct":       float,
    "top_per_tier":       int,
    "max_total_shown":    int,
    "vix_caution":        float,
    "vix_stress":         float,
}


def _parse_scan_window(raw: Any) -> tuple[dtime, dtime]:
    if not (isinstance(raw, (list, tuple)) and len(raw) == 2):
        raise ValueError(
            f"scan_window must be a 2-element list of 'HH:MM' strings, got: {raw!r}"
        )
    result = []
    for part in raw:
        try:
            parts = [int(x) for x in str(part).split(":")]
            result.append(dtime(*parts))
        except Exception:
            raise ValueError(f"Cannot parse scan_window time: {part!r} — use 'HH:MM'")
    return tuple(result)


def _apply_yaml_overrides(cfg: Config) -> Config:
    overrides = (YAML_SETTINGS or {}).get("dip_scanner") or {}
    if not overrides:
        return cfg

    valid_fields = {f.name for f in fields(cfg)}
    safe: dict[str, Any] = {}

    for key, raw_val in overrides.items():
        if key not in valid_fields:
            logging.debug(f"dip_scanner YAML: unknown key '{key}' — ignored")
            continue
        if key == "scan_window":
            try:
                safe[key] = _parse_scan_window(raw_val)
            except ValueError as exc:
                logging.warning(f"dip_scanner YAML: scan_window override skipped — {exc}")
            continue
        coerce_fn = _YAML_COERCE.get(key)
        if coerce_fn is None:
            logging.debug(f"dip_scanner YAML: no coerce for '{key}' — skipping")
            continue
        try:
            safe[key] = coerce_fn(raw_val)
        except (TypeError, ValueError) as exc:
            logging.warning(
                f"dip_scanner YAML: cannot coerce '{key}' value {raw_val!r} "
                f"via {coerce_fn.__name__} — {exc}; keeping default"
            )

    if safe:
        logging.info(f"dip_scanner: applied YAML overrides: {sorted(safe)}")
    return replace(cfg, **safe)


CFG = _apply_yaml_overrides(Config())


# ════════════════════════════════════════════════════════════════════
# SECTION 6 — UNIVERSE CONSTRUCTION
# ════════════════════════════════════════════════════════════════════

def _build_dip_universe() -> tuple[list[str], dict[str, str]]:
    if SYMBOL_META:
        syms = [s for s, m in SYMBOL_META.items() if "dip" in (m.get("roles") or [])]
        if not syms:
            logging.warning("dip_scanner: no symbols have role='dip'; scanning full universe")
            syms = list(SYMBOL_META.keys())
    else:
        logging.warning("dip_scanner: SYMBOL_META empty; falling back to SECTORS union")
        syms = list({s for ss in SECTORS.values() for s in ss})

    sym_sector = {s: SYMBOL_TO_SECTOR.get(s, "Other") for s in syms}
    return syms, sym_sector


DIP_UNIVERSE, SYMBOL_SECTOR = _build_dip_universe()
SECTOR_COUNT = len({v for v in SYMBOL_SECTOR.values()})


# ════════════════════════════════════════════════════════════════════
# SECTION 7 — CLOCK HELPERS
# ════════════════════════════════════════════════════════════════════

def is_weekend() -> bool:
    return market_now().weekday() >= 5


def in_window(win: tuple[dtime, dtime]) -> bool:
    t = market_now().time()
    return win[0] <= t < win[1]


# ════════════════════════════════════════════════════════════════════
# SECTION 8 — COOLDOWN HELPERS
# ════════════════════════════════════════════════════════════════════

def cooldown_key(symbol: str) -> str:
    return f"dip_alert_{symbol}"


def is_in_cooldown(symbol: str) -> bool:
    return not can_alert(cooldown_key(symbol), CFG.cooldown_hours)


# ════════════════════════════════════════════════════════════════════
# SECTION 8b — DIP STATE MACHINE
# ════════════════════════════════════════════════════════════════════

class DipPhase(str, Enum):
    """
    Lifecycle phases for a tracked dip position.

    WATCHING  — in universe, no active dip alerted
    DIPPING   — dip alert sent; watching for deeper legs or bounce
    BOUNCING  — bounce alert sent; waiting for full price recovery
    CLEARED   — price recovered fully; state ready to reset
    """
    WATCHING  = "watching"
    DIPPING   = "dipping"
    BOUNCING  = "bouncing"
    CLEARED   = "cleared"


def _dip_state_update(mutator) -> None:
    """
    Atomic fcntl-locked read-modify-write on STATE_FILE.
    Mirrors market_intel._state_update() so dip states and cooldown
    timestamps coexist safely in the same JSON file without races.
    """
    Path(STATE_FILE).touch(exist_ok=True)
    with open(STATE_FILE, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            raw = fh.read()
            try:
                state = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                logging.warning("dip_scanner: STATE_FILE corrupt; resetting to {}")
                state = {}
            mutator(state)
            fh.seek(0)
            fh.truncate()
            json.dump(state, fh, default=str)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _dip_state_key(symbol: str) -> str:
    return f"dip_state_{symbol}"


def get_dip_state(symbol: str) -> dict | None:
    """
    Read the current dip-state record for symbol.
    Returns None when no active record exists.

    Record schema
    -------------
    phase             : DipPhase value string
    dip_low           : float  — lowest close seen since dip alert
    dip_high_entry    : float  — price at the moment the dip alert fired
    bounce_threshold  : float  — dip_low * (1 + BOUNCE_PCT/100)
    deepen_threshold  : float  — dip_low * (1 - DEEPEN_PCT/100)
    alerted_at        : ISO timestamp of the original dip alert
    last_deepen_at    : ISO timestamp of the last deepening alert (or None)
    alert_count       : int  — number of dip alerts sent for this leg
    """
    return load_json(STATE_FILE, {}).get(_dip_state_key(symbol))


def write_dip_state(
    symbol:       str,
    phase:        DipPhase,
    dip_low:      float,
    entry_price:  float,
    alerted_at:   str,
    alert_count:  int = 1,
    last_deepen_at: str | None = None,
) -> None:
    """
    Atomically write (or overwrite) the dip-state record for symbol.
    Derives bounce_threshold and deepen_threshold from dip_low so they
    stay consistent whenever dip_low is updated.
    """
    record = {
        "phase":             phase.value,
        "dip_low":           dip_low,
        "dip_high_entry":    entry_price,
        "bounce_threshold":  round(dip_low * (1.0 + BOUNCE_PCT / 100.0), 4),
        "deepen_threshold":  round(dip_low * (1.0 - DEEPEN_PCT / 100.0), 4),
        "alerted_at":        alerted_at,
        "last_deepen_at":    last_deepen_at,
        "alert_count":       alert_count,
    }
    _dip_state_update(lambda s: s.__setitem__(_dip_state_key(symbol), record))
    logging.info(
        f"dip_state write | {symbol} phase={phase.value} "
        f"dip_low={dip_low:.2f} bounce≥{record['bounce_threshold']:.2f} "
        f"deepen<{record['deepen_threshold']:.2f}"
    )


def _purge_stale_dip_states() -> int:
    """
    Remove dip-state records older than DIP_TTL_HOURS.
    Returns number of records purged.
    """
    cutoff       = market_now() - timedelta(hours=DIP_TTL_HOURS)
    market_tz    = ZoneInfo("America/New_York")
    to_delete: list[str] = []

    for key, rec in load_json(STATE_FILE, {}).items():
        if not key.startswith("dip_state_"):
            continue
        try:
            dt = datetime.fromisoformat(rec["alerted_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=market_tz)
            if dt < cutoff:
                to_delete.append(key)
        except Exception:
            to_delete.append(key)   # malformed — purge

    if to_delete:
        def _remove(s):
            for k in to_delete:
                s.pop(k, None)
        _dip_state_update(_remove)
        logging.info(f"dip_state purge | removed {len(to_delete)} stale records")

    return len(to_delete)


def check_bounce(symbol: str, current_price: float) -> dict | None:
    """
    Return a bounce-info dict when a DIPPING symbol has recovered ≥ BOUNCE_PCT
    from its dip_low.  Returns None if not yet bounced or already bounced.

    Also updates dip_low + thresholds when price sinks to a new low
    (deepening leg — the deepen alert is handled separately in _run_deepen_pass).

    Return dict keys: symbol, current_price, dip_low, dip_high_entry,
                      recovery_pct, alerted_at
    """
    rec = get_dip_state(symbol)
    if rec is None or rec.get("phase") != DipPhase.DIPPING.value:
        return None

    dip_low          = float(rec["dip_low"])
    bounce_threshold = float(rec["bounce_threshold"])

    # New low — update thresholds atomically
    if current_price < dip_low:
        def _deepen(s):
            r = s.get(_dip_state_key(symbol))
            if r:
                r["dip_low"]          = current_price
                r["bounce_threshold"] = round(current_price * (1.0 + BOUNCE_PCT / 100.0), 4)
                r["deepen_threshold"] = round(current_price * (1.0 - DEEPEN_PCT / 100.0), 4)
        _dip_state_update(_deepen)
        return None

    if current_price >= bounce_threshold:
        return {
            "symbol":         symbol,
            "current_price":  current_price,
            "dip_low":        dip_low,
            "dip_high_entry": float(rec.get("dip_high_entry", current_price)),
            "recovery_pct":   (current_price / dip_low - 1.0) * 100.0,
            "alerted_at":     rec.get("alerted_at", ""),
        }
    return None


def check_deepen(symbol: str, current_price: float) -> dict | None:
    """
    Return a deepen-info dict when price has dropped ≥ DEEPEN_PCT below
    the last known dip_low AND the deepen cooldown has elapsed.
    Returns None otherwise.

    Return dict keys: symbol, current_price, prev_low, drop_extension_pct,
                      alerted_at
    """
    rec = get_dip_state(symbol)
    if rec is None or rec.get("phase") != DipPhase.DIPPING.value:
        return None

    dip_low           = float(rec["dip_low"])
    deepen_threshold  = float(rec["deepen_threshold"])

    if current_price >= deepen_threshold:
        return None   # not deep enough yet

    # Respect deepen cooldown — don't spam on every scan
    last_deepen = rec.get("last_deepen_at")
    if last_deepen:
        try:
            last_dt = datetime.fromisoformat(last_deepen)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=ZoneInfo("America/New_York"))
            if (market_now() - last_dt) < timedelta(hours=DEEPEN_COOLDOWN):
                return None
        except Exception:
            pass

    return {
        "symbol":              symbol,
        "current_price":       current_price,
        "prev_low":            dip_low,
        "drop_extension_pct":  (current_price / dip_low - 1.0) * 100.0,
        "alerted_at":          rec.get("alerted_at", ""),
    }


# ════════════════════════════════════════════════════════════════════
# SECTION 9 — PRICE STATS
# ════════════════════════════════════════════════════════════════════

@dataclass
class PriceStats:
    drop_5d:       float | None
    ema200_rising: bool  | None
    swing_low_20d: float | None
    atr_14:        float | None


def fetch_price_stats(symbol: str) -> PriceStats:
    """
    Compute extended daily indicators for symbol.
    Returns all-None on any error — callers must guard.
    """
    df = _yf_download(symbol, period="1y", interval="1d")
    if df is None or df.empty or len(df) < 30:
        logging.debug(f"fetch_price_stats({symbol}): insufficient data")
        return PriceStats(None, None, None, None)

    try:
        close = df["Close"]

        drop_5d: float | None = None
        if len(close) >= 6:
            drop_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)

        ema200_rising: bool | None = None
        if len(close) >= 210:
            ema   = close.ewm(span=200, adjust=False).mean()
            slope = float((ema.iloc[-1] - ema.iloc[-10]) / close.iloc[-1] * 100)
            ema200_rising = slope >= CFG.ema_slope_min_pct

        swing_low_20d: float | None = None
        if len(close) >= 21:
            swing_low_20d = float(close.iloc[-21:-1].min())

        atr_14: float | None = None
        if len(df) >= 15 and {"High", "Low", "Close"}.issubset(df.columns):
            high       = df["High"]
            low        = df["Low"]
            prev       = close.shift(1)
            true_range = pd.concat(
                [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
            ).max(axis=1)
            atr_14 = float(true_range.rolling(14).mean().iloc[-1])

        return PriceStats(drop_5d, ema200_rising, swing_low_20d, atr_14)

    except Exception as exc:
        logging.debug(f"fetch_price_stats({symbol}): {exc}")
        return PriceStats(None, None, None, None)


# ════════════════════════════════════════════════════════════════════
# SECTION 10 — QUALIFICATION ENGINE
# ════════════════════════════════════════════════════════════════════

class FailCode(str, Enum):
    BELOW_EMA200_HARD        = "Too far below EMA200"
    BELOW_EMA200_FLAT_SLOPE  = "Below EMA200 with flat/falling slope"
    RSI_TOO_LOW              = "RSI too low (breakdown risk)"
    RSI_TOO_HIGH             = "RSI not oversold"
    NO_5D_DATA               = "Could not compute 5d change"
    INSUFFICIENT_DIP         = "Insufficient dip (1d & 5d)"
    TOO_FAR_FROM_ATH         = "Too far from ATH"
    VOLUME_THIN              = "Volume too thin"
    EARNINGS_SOON            = "Earnings within 3 days"
    MISSING_FIELDS           = "Missing context fields"
    EMA50_INVALID            = "EMA50 missing or zero"


@dataclass
class QualifyResult:
    qualified:    bool             = False
    score:        int              = 0
    reasons:      list[str]        = field(default_factory=list)
    fail_code:    FailCode | None  = None
    fail_detail:  str              = ""
    drop_5d:      float | None     = None
    rs_score:     float | None     = None
    rs_label:     str   | None     = None
    trend_note:   str              = ""
    buy_low:      float | None     = None
    buy_high:     float | None     = None
    stop:         float | None     = None
    vix_penalised: bool            = False   # True when VIX reduced score


def qualify_dip(
    ctx:        dict,
    stats:      PriceStats,
    market_ctx: dict | None = None,
) -> QualifyResult:
    """
    Apply all qualification gates and compute score (0–17 with VIX bonus).

    Gate order (all must pass):
        0. EMA50 sanity
        1. EMA200 position / slope
        2. RSI band
        3. Dip magnitude (1d OR 5d)
        4. ATH proximity
        5. Volume ratio
        6. No earnings within 3 days

    Score breakdown (max 16 base + VIX context):
        Trend position      1–3
        RSI depth           1–3
        ATH proximity       0–3
        Volume character    0–2
        5-day drop depth    1–3
        Relative strength   0–2
        VIX adjustment      -1 if VIX > vix_caution (20)
                            -2 if VIX > vix_stress  (25)
        Earnings proximity  -1 if 4–7 days out
    """
    res = QualifyResult()
    sym     = ctx["symbol"]
    current = ctx["current"]

    # ── Gate 0: EMA50 sanity ──────────────────────────────────────
    ema50 = ctx.get("ema50") or 0.0
    if not ema50 or ema50 <= 0:
        res.fail_code, res.fail_detail = FailCode.EMA50_INVALID, "ema50=0"
        return res

    # ── Gate 1: EMA200 position & slope ──────────────────────────
    ema200 = ctx.get("ema200_real") or ctx.get("ema200") or 0.0
    if not ema200 or ema200 <= 0:
        res.fail_code, res.fail_detail = FailCode.MISSING_FIELDS, "ema200"
        return res

    pct_from_ema200 = (current / ema200 - 1) * 100
    above_200       = current > ema200

    if above_200:
        res.trend_note = f"Above EMA200 ({pct_from_ema200:+.1f}%)"
    elif pct_from_ema200 >= -CFG.ema200_flex_pct:
        if stats.ema200_rising:
            res.trend_note = f"Below EMA200 ({pct_from_ema200:+.1f}%) but rising"
        else:
            res.fail_code  = FailCode.BELOW_EMA200_FLAT_SLOPE
            res.fail_detail = f"{pct_from_ema200:+.1f}%"
            return res
    else:
        res.fail_code   = FailCode.BELOW_EMA200_HARD
        res.fail_detail = f"{pct_from_ema200:+.1f}%"
        return res

    # ── Gate 2: RSI band ──────────────────────────────────────────
    rsi = ctx["rsi"]
    if rsi < CFG.rsi_min:
        res.fail_code, res.fail_detail = FailCode.RSI_TOO_LOW, f"{rsi:.0f}"
        return res
    if rsi > CFG.rsi_max:
        res.fail_code, res.fail_detail = FailCode.RSI_TOO_HIGH, f"{rsi:.0f}"
        return res

    # ── Gate 3: Dip magnitude ─────────────────────────────────────
    if stats.drop_5d is None:
        res.fail_code = FailCode.NO_5D_DATA
        return res

    res.drop_5d = stats.drop_5d
    day_drop    = ctx["day_change_pct"]
    if not (day_drop <= CFG.drop_1d_max or stats.drop_5d <= CFG.drop_5d_max):
        res.fail_code   = FailCode.INSUFFICIENT_DIP
        res.fail_detail = f"1d {day_drop:+.1f}% / 5d {stats.drop_5d:+.1f}%"
        return res

    # ── Gate 4: ATH proximity ─────────────────────────────────────
    ath_pct = ctx["ath_pct"]
    if ath_pct < CFG.ath_min:
        res.fail_code   = FailCode.TOO_FAR_FROM_ATH
        res.fail_detail = f"{ath_pct:+.0f}%"
        return res

    # ── Gate 5: Volume ratio ──────────────────────────────────────
    vol_ratio = ctx["vol_ratio"]
    if vol_ratio < CFG.vol_ratio_min:
        res.fail_code   = FailCode.VOLUME_THIN
        res.fail_detail = f"{vol_ratio:.2f}×"
        return res

    # ── Gate 6: Earnings proximity ────────────────────────────────
    _, days_to_earn = get_earnings_date(sym)
    if days_to_earn is not None and 0 <= days_to_earn <= 3:
        res.fail_code   = FailCode.EARNINGS_SOON
        res.fail_detail = f"{days_to_earn}d"
        return res

    # ─────────────────────────────────────────────────────────────
    # ALL GATES PASSED — compute score
    # ─────────────────────────────────────────────────────────────
    score:   int       = 0
    reasons: list[str] = []
    above_50 = current > ema50

    # Trend position (1–3 pts)
    if above_50 and above_200:
        score += 3; reasons.append("📈 Strong trend (above EMA50 & EMA200)")
    elif above_200:
        score += 2; reasons.append("📉 Pulling back to EMA50 zone")
    else:
        score += 1; reasons.append("⚠️ Testing EMA200 (slope rising)")

    # RSI depth (1–3 pts)
    if rsi <= 30:
        score += 3; reasons.append(f"🔥 Deeply oversold (RSI {rsi:.0f})")
    elif rsi <= 35:
        score += 2; reasons.append(f"📊 Oversold (RSI {rsi:.0f})")
    else:
        score += 1; reasons.append(f"📊 Cooling off (RSI {rsi:.0f})")

    # ATH proximity (0–3 pts)
    if   ath_pct > -5:   score += 3; reasons.append(f"🏔️ Very near ATH ({ath_pct:+.1f}%)")
    elif ath_pct > -10:  score += 2; reasons.append(f"📍 Close to ATH ({ath_pct:+.1f}%)")
    elif ath_pct > -20:  score += 1; reasons.append(f"📍 Moderate pullback ({ath_pct:+.1f}%)")

    # Volume character (0–2 pts)
    if   vol_ratio > 1.8: score += 2; reasons.append(f"🔊 High vol capitulation ({vol_ratio:.1f}×)")
    elif vol_ratio > 1.2: score += 1; reasons.append(f"📊 Above-avg volume ({vol_ratio:.1f}×)")

    # 5-day drop depth (1–3 pts)
    d5 = stats.drop_5d
    if   d5 <= -10: score += 3; reasons.append(f"💥 Sharp 5d selloff ({d5:+.1f}%)")
    elif d5 <= -7:  score += 2; reasons.append(f"📉 Significant 5d drop ({d5:+.1f}%)")
    else:           score += 1; reasons.append(f"📉 Moderate 5d dip ({d5:+.1f}%)")

    # Relative strength vs SPY (0–2 pts)
    # FIX v3.4: pass sym string, not ctx dict (market_intel v3.2 API)
    try:
        rs_score, rs_label = calc_relative_strength(sym)
        res.rs_score, res.rs_label = rs_score, rs_label
        if rs_score is not None:
            if   rs_score > 2: score += 2; reasons.append(f"💪 Outperforming SPY ({rs_label})")
            elif rs_score > 0: score += 1; reasons.append(f"📊 Holding vs SPY ({rs_label})")
    except Exception as exc:
        logging.debug(f"qualify_dip RS {sym}: {exc}")

    # ── VIX-aware score adjustment ────────────────────────────────
    # High VIX = market stress = dip buys are riskier = penalise score.
    # We read VIX from market_ctx when passed; if not available we skip.
    vix_val = None
    if market_ctx:
        vix_val = market_ctx.get("^VIX", {}).get("price")

    if vix_val is not None:
        if vix_val >= CFG.vix_stress:
            score -= 2
            reasons.append(f"🩸 VIX stress penalty ({vix_val:.0f}) — high-risk environment")
            res.vix_penalised = True
        elif vix_val >= CFG.vix_caution:
            score -= 1
            reasons.append(f"⚠️ VIX caution penalty ({vix_val:.0f}) — elevated volatility")
            res.vix_penalised = True

    # ── Earnings proximity score penalty ──────────────────────────
    # Gate 6 already blocks ≤3 days. Add a -1 penalty for 4–7 days
    # so near-earnings setups surface lower in rankings.
    if days_to_earn is not None and 4 <= days_to_earn <= 7:
        score -= 1
        reasons.append(f"📅 Earnings in {days_to_earn}d — avoid full size")

    # Floor score at 0 — penalties should not create negative totals
    score = max(score, 0)

    # ── Trade plan: buy zone + stop ──────────────────────────────
    # ATR proxy floor: min $0.10 prevents collapsed zones on low-price stocks.
    atr   = stats.atr_14 or max(current * 0.02, 0.10)
    swing = stats.swing_low_20d or (current - 2 * atr)

    buy_low  = max(swing, ema50 - 0.5 * atr)
    buy_high = max(current, ema50)

    if buy_low >= buy_high:
        buy_low = buy_high * 0.99

    raw_stop = max(ema200, swing - atr)
    cap_stop = current * (1.0 - CFG.max_loss_pct / 100.0)
    stop     = max(raw_stop, cap_stop)

    if stop >= current:
        stop = current * (1.0 - CFG.max_loss_pct / 100.0)

    res.qualified = True
    res.score     = score
    res.reasons   = reasons
    res.buy_low   = buy_low
    res.buy_high  = buy_high
    res.stop      = stop
    return res


# ════════════════════════════════════════════════════════════════════
# SECTION 11 — FORMATTING HELPERS
# ════════════════════════════════════════════════════════════════════

def dip_header(
    emoji:    str,
    title:    str,
    subtitle: str | None = None,
    border:   str = "━━━━━━━━━━━━━━━━━━━━━",
) -> str:
    msg = f"{emoji} *{title}*\n`{border}`\n"
    if subtitle:
        msg += f"_{md(subtitle)}_\n"
    msg += "\n"
    return msg


def _tier(score: int) -> tuple[str, str]:
    if score >= 13: return ("🏆", "ELITE")
    if score >= 9:  return ("⭐", "STRONG")
    return ("✅", "WATCHLIST")


def _name_label(sym: str) -> str:
    meta = SYMBOL_META.get(sym, {})
    name = meta.get("name", "")
    exch = meta.get("exchange", "")
    if name and exch:
        return f"{md(sym)} — {md(name)} ({md(exch)})"
    if name:
        return f"{md(sym)} — {md(name)}"
    return md(sym)


def _rsi_mood(rsi: float) -> str:
    if rsi <= 30: return "🔥 Deep oversold"
    if rsi <= 35: return "🟢 Oversold"
    if rsi <= 42: return "🟡 Cooling"
    return "⚪ Mild dip"


def _risk_label(stop_pct: float) -> str:
    if stop_pct >= -3: return "Low risk"
    if stop_pct >= -6: return "Medium risk"
    return "Higher risk"


def format_bounce_alert(info: dict, ctx: dict) -> str:
    """
    Compact recovery alert sent when a DIPPING symbol crosses its
    bounce_threshold.  Intentionally lighter than format_candidate()
    — this is a lifecycle update on a known setup, not a new recommendation.
    """
    sym          = info["symbol"]
    em           = SYMBOL_EMOJI.get(sym, "📊")
    sector       = SYMBOL_SECTOR.get(sym, "Other")
    current      = info["current_price"]
    dip_low      = info["dip_low"]
    entry        = info["dip_high_entry"]
    recovery_pct = info["recovery_pct"]

    try:
        orig_label = datetime.fromisoformat(info["alerted_at"]).strftime("%b %d %I:%M %p ET")
    except Exception:
        orig_label = "earlier"

    ts       = display_now().strftime("%a %b %d • %I:%M %p ET")
    recov_em = "🚀" if recovery_pct >= 5.0 else "🟢"
    ema50    = ctx.get("ema50")
    rsi      = ctx.get("rsi", 50)
    vol      = ctx.get("vol_ratio", 1.0)

    ema50_note = ""
    if ema50:
        if current >= ema50:
            ema50_note = f" \\(price reclaimed EMA50 `${ema50:.2f}`\\)"
        else:
            gap = (ema50 / current - 1) * 100
            ema50_note = f" \\(EMA50 `${ema50:.2f}` still {gap:.1f}% above\\)"

    msg  = f"\n{recov_em} *DIP RECOVERY ALERT*\n"
    msg += f"`{H_RULE}`\n"
    msg += f"{em} *{_name_label(sym)}*\n"
    msg += f"Sector: _{md(sector)}_  •  _{md(ts)}_\n\n"
    msg += f"*RECOVERY*\n"
    msg += f"`─────────────────`\n"
    msg += f"📍 Dip low:  `${dip_low:.2f}` \\(alerted {md(orig_label)}\\)\n"
    msg += f"💵 Now:       `${current:.2f}`\n"
    msg += f"{recov_em} Bounce:   `{recovery_pct:+.2f}%` from low\n"
    msg += f"📊 RSI: `{rsi:.0f}` • Volume: `{vol:.1f}×`\n\n"
    msg += f"*NEXT LEVELS*\n"
    msg += f"`─────────────────`\n"
    msg += f"🎯 EMA50 resistance{ema50_note}\n"
    if entry and entry > current:
        msg += f"📈 Original entry zone: `${entry:.2f}` \\({(entry/current-1)*100:+.1f}% above\\)\n"
    msg += f"\n*ACTION*\n"
    msg += f"`─────────────────`\n"
    if recovery_pct >= 5.0:
        msg += "✅ Strong bounce — confirm volume \\+ EMA50 reclaim before adding\n"
    else:
        msg += "👀 Early bounce — wait for EMA50 reclaim before scaling in\n"
    msg += "🛡️ Keep original stop until EMA50 confirmed as support\n"
    msg += f"\n`{H_RULE}`\n"
    msg += "_AlphaEdge Dip Scanner — Bounce Update_"
    return msg


def format_deepen_alert(info: dict, ctx: dict) -> str:
    """
    Compact deepening alert sent when a tracked dip extends ≥ DEEPEN_PCT
    below its previous low.  Warns the user the dip is accelerating.
    """
    sym           = info["symbol"]
    em            = SYMBOL_EMOJI.get(sym, "📊")
    sector        = SYMBOL_SECTOR.get(sym, "Other")
    current       = info["current_price"]
    prev_low      = info["prev_low"]
    extension_pct = info["drop_extension_pct"]

    try:
        orig_label = datetime.fromisoformat(info["alerted_at"]).strftime("%b %d %I:%M %p ET")
    except Exception:
        orig_label = "earlier"

    ts    = display_now().strftime("%a %b %d • %I:%M %p ET")
    ema50 = ctx.get("ema50")
    rsi   = ctx.get("rsi", 50)
    ema200 = ctx.get("ema200_real") or ctx.get("ema200")

    msg  = f"\n🔻 *DIP DEEPENING ALERT*\n"
    msg += f"`{H_RULE}`\n"
    msg += f"{em} *{_name_label(sym)}*\n"
    msg += f"Sector: _{md(sector)}_  •  _{md(ts)}_\n\n"
    msg += f"*EXTENSION*\n"
    msg += f"`─────────────────`\n"
    msg += f"📍 Prior low:   `${prev_low:.2f}` \\(alerted {md(orig_label)}\\)\n"
    msg += f"💵 Now:          `${current:.2f}`\n"
    msg += f"🔻 Extended:    `{extension_pct:.2f}%` below prior low\n"
    msg += f"📊 RSI: `{rsi:.0f}`\n\n"
    msg += f"*KEY LEVELS*\n"
    msg += f"`─────────────────`\n"
    if ema200:
        msg += f"🛡️ EMA200 support: `${ema200:.2f}`\n"
    if ema50:
        msg += f"📉 EMA50 \\(now resistance\\): `${ema50:.2f}`\n"
    msg += f"\n*ACTION*\n"
    msg += f"`─────────────────`\n"
    msg += "⚠️ Do NOT average down — wait for stabilisation\n"
    msg += "🛡️ If stop not triggered, hold — dip may be deepening into a better entry\n"
    msg += "👀 Watch for RSI < 30 \\+ volume spike as exhaustion signal\n"
    msg += f"\n`{H_RULE}`\n"
    msg += "_AlphaEdge Dip Scanner — Deepening Update_"
    return msg


# ════════════════════════════════════════════════════════════════════
# SECTION 12 — CANDIDATE CARD FORMATTER
# ════════════════════════════════════════════════════════════════════

def format_candidate(c: dict, rank: int) -> str:
    ctx: dict        = c["ctx"]
    q:   QualifyResult = c["q"]

    sym     = ctx["symbol"]
    em      = SYMBOL_EMOJI.get(sym, "📊")
    sector  = SYMBOL_SECTOR.get(sym, "Other")
    current = ctx["current"]

    badge, tier_name = _tier(q.score)

    rs_part = ""
    if q.rs_score is not None:
        rs_icon = "💪" if q.rs_score > 0 else "📉"
        rs_part = f" • RS {rs_icon} `{q.rs_score:+.1f}%`"

    stop_pct   = (q.stop / current - 1) * 100 if q.stop else 0.0
    risk_label = _risk_label(stop_pct)

    vix_note = " ⚠️ _VIX penalty applied_" if q.vix_penalised else ""

    block  = ""
    block += f"\n{badge} *\\#{rank} — {md(tier_name)} SETUP*{vix_note}\n"
    block += f"`─────────────────`\n"
    block += f"{em} *{_name_label(sym)}*\n"
    block += f"Sector: _{md(sector)}_\n\n"
    block += f"*PRICE SNAPSHOT*\n"
    block += f"💵 Current: `${current:.2f}`\n"
    block += f"📉 1D: `{ctx['day_change_pct']:+.2f}%` • 5D: `{q.drop_5d:+.2f}%`\n"
    block += f"📊 RSI: `{ctx['rsi']:.0f}` — {_rsi_mood(ctx['rsi'])}\n"
    block += f"🏔️ From ATH: `{ctx['ath_pct']:+.1f}%`\n"
    block += f"🔊 Volume: `{ctx['vol_ratio']:.1f}×`{rs_part}\n\n"
    block += f"*WHY IT QUALIFIED*\n"
    for reason in q.reasons[:3]:
        block += f"• {md(reason)}\n"
    block += f"\n*TRADE PLAN*\n"
    block += f"🟢 Buy zone: `${q.buy_low:.2f}` → `${q.buy_high:.2f}`\n"
    block += f"🛡️ Stop: `${q.stop:.2f}` (`{stop_pct:+.1f}%`) — {md(risk_label)}\n"
    block += f"🎯 Setup score: *{q.score}/16*\n"
    return block


# ════════════════════════════════════════════════════════════════════
# SECTION 13 — FULL ALERT FORMATTER
# ════════════════════════════════════════════════════════════════════

def format_alert(
    candidates:  list[dict],
    market_ctx:  dict,
    stats:       dict,
) -> str:
    ts = display_now().strftime("%a %b %d • %I:%M %p ET")
    msg = dip_header("🎯", "DIP BUY SCANNER", ts, "━━━━━━━━━━━━━━━━━━━━━")

    msg += f"*SCAN SUMMARY*\n"
    msg += f"`─────────────────`\n"
    msg += f"📊 Scanned: `{stats['scanned']}`\n"
    msg += f"✅ Qualified: `{len(candidates)}`\n"
    if stats["failed"]:
        msg += f"⚠️ Failed: `{stats['failed']}`\n"
    if stats["cooldown"]:
        msg += f"🔕 Cooldown: `{stats['cooldown']}`\n"

    # ── Market backdrop ───────────────────────────────────────────
    if market_ctx:
        spy = market_ctx.get("SPY", {})
        qqq = market_ctx.get("QQQ", {})
        vix = market_ctx.get("^VIX", {})

        spy_pct = spy.get("pct", 0.0)
        qqq_pct = qqq.get("pct", 0.0)
        vix_p   = vix.get("price", 15.0)

        spy_em = "🟢" if spy_pct >= 0 else "🔴"
        qqq_em = "🟢" if qqq_pct >= 0 else "🔴"
        vix_em = "🩸" if vix_p >= 30 else "🟡" if vix_p >= 20 else "🟢"

        msg += f"\n*MARKET BACKDROP*\n"
        msg += f"`─────────────────`\n"
        msg += f"SPY: {spy_em} `{spy_pct:+.2f}%`\n"
        msg += f"QQQ: {qqq_em} `{qqq_pct:+.2f}%`\n"
        msg += f"VIX: {vix_em} `{vix_p:.1f}`\n"

        if vix_p >= CFG.vix_stress:
            msg += "⚠️ _High VIX — scores penalised, WATCHLIST suppressed_\n"
        elif vix_p >= CFG.vix_caution:
            msg += "⚡ _Elevated VIX — scores penalised, be selective_\n"
        else:
            msg += "✅ _Market volatility acceptable_\n"

    # ── Sector clustering note ────────────────────────────────────
    # When 3+ candidates share the same sector, flag it as a rotation play.
    sector_counts: dict[str, int] = {}
    for c in candidates:
        s = SYMBOL_SECTOR.get(c["ctx"]["symbol"], "Other")
        sector_counts[s] = sector_counts.get(s, 0) + 1

    cluster_sectors = [s for s, n in sector_counts.items() if n >= 3]
    if cluster_sectors:
        msg += f"\n*SECTOR ROTATION SIGNAL*\n"
        msg += f"`─────────────────`\n"
        for s in cluster_sectors:
            msg += f"📦 {md(s)}: `{sector_counts[s]}` dip candidates — possible sector rotation\n"

    # ── Bucket into tiers ─────────────────────────────────────────
    tiers: dict[str, list[dict]] = {"ELITE": [], "STRONG": [], "WATCHLIST": []}
    for c in candidates:
        _, tier_name = _tier(c["q"].score)
        # Under VIX stress suppress WATCHLIST tier entirely
        vix_p = (market_ctx or {}).get("^VIX", {}).get("price", 0)
        if tier_name == "WATCHLIST" and vix_p >= CFG.vix_stress:
            continue
        tiers[tier_name].append(c)

    tier_headers = {
        "ELITE":     {"title": "🏆 ELITE SETUPS",     "desc": "Best risk/reward pullbacks",           "border": "━━━━━━━━━━━━━━━━━━━━━"},
        "STRONG":    {"title": "⭐ STRONG SETUPS",    "desc": "Good dips, still need confirmation",   "border": "═════════════════════"},
        "WATCHLIST": {"title": "✅ WATCHLIST SETUPS", "desc": "Interesting, but lower conviction",    "border": "─────────────────────"},
    }

    rank        = 1
    total_shown = 0

    for tier_name in ("ELITE", "STRONG", "WATCHLIST"):
        bucket = tiers[tier_name]
        if not bucket:
            continue

        bucket.sort(key=lambda c: (SYMBOL_SECTOR.get(c["ctx"]["symbol"], ""), -c["q"].score))
        h     = tier_headers[tier_name]
        shown = bucket[:CFG.top_per_tier]

        msg += "\n"
        msg += f"*{h['title']}*\n"
        msg += f"_{h['desc']}_\n"
        msg += f"`{h['border']}`\n"

        for c in shown:
            if total_shown >= CFG.max_total_shown:
                break
            msg += format_candidate(c, rank)
            rank        += 1
            total_shown += 1

        if len(bucket) > len(shown):
            msg += f"\n_\\+{len(bucket) - len(shown)} more in this tier_\n"

    # ── Top disqualification reasons ─────────────────────────────
    disq = stats.get("disqualified") or {}
    if disq:
        msg += f"\n*TOP DISQUALIFICATIONS*\n"
        msg += f"`─────────────────`\n"
        for code, cnt in sorted(disq.items(), key=lambda x: -x[1])[:4]:
            msg += f"• {md(code)}: `{cnt}`\n"

    msg += f"\n*RULES*\n"
    msg += f"`─────────────────`\n"
    msg += "✅ Pick only 1–3 best setups\n"
    msg += "📏 Size 2–5% per trade\n"
    msg += "🛡️ Respect stop — EMA200 and max\\-loss protected\n"
    msg += "🧱 Scale in, don't full\\-send\n"
    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += "_AlphaEdge Dip Scanner_"
    return msg


# ════════════════════════════════════════════════════════════════════
# SECTION 14 — SINGLE-SYMBOL FETCH WORKER
# ════════════════════════════════════════════════════════════════════

def _scan_one(symbol: str) -> tuple[str, dict | None, PriceStats | None, str | None]:
    """
    Fetch full context + price stats for one symbol.
    Never raises. Returns error code string on failure.
    """
    try:
        ctx = get_full_context(symbol)
        if not ctx:
            return symbol, None, None, "no_ctx"

        required = ("current", "ema50", "rsi", "day_change_pct", "ath_pct", "vol_ratio")
        missing  = [f for f in required if ctx.get(f) is None]
        if missing:
            return symbol, None, None, f"missing_fields:{','.join(missing)}"

        price_stats = fetch_price_stats(symbol)
        return symbol, ctx, price_stats, None

    except Exception as exc:
        # Log type + message at DEBUG so post-mortems can identify pattern
        logging.debug(f"_scan_one({symbol}): {type(exc).__name__}: {exc}", exc_info=True)
        return symbol, None, None, f"err:{type(exc).__name__}: {exc}"


# ════════════════════════════════════════════════════════════════════
# SECTION 15 — BOUNCE + DEEPEN PASS HELPERS
# ════════════════════════════════════════════════════════════════════

def _run_bounce_pass() -> None:
    """
    Check every DIPPING symbol for price recovery.
    Runs BEFORE the cooldown filter — bouncing symbols are in cooldown
    by design and we want to alert them regardless.
    Only fetches context for symbols with an active dip_state record (cheap).
    """
    state        = load_json(STATE_FILE, {})
    dipping_syms = [
        key.removeprefix("dip_state_")
        for key, rec in state.items()
        if key.startswith("dip_state_")
        and rec.get("phase") == DipPhase.DIPPING.value
    ]

    if not dipping_syms:
        return

    print(f"\n  🔍 Bounce pass: {len(dipping_syms)} tracked dip(s)")
    logging.info(f"bounce_pass start | symbols={dipping_syms}")

    for sym in dipping_syms:
        try:
            ctx = get_full_context(sym)
            if not ctx:
                continue

            current = ctx["current"]

            # ── Bounce check ──────────────────────────────────────
            bounce_info = check_bounce(sym, current)
            if bounce_info:
                print(f"  {sym:6s} 🟢 BOUNCE +{bounce_info['recovery_pct']:.1f}% from ${bounce_info['dip_low']:.2f}")
                msg    = format_bounce_alert(bounce_info, ctx)
                sent   = send_telegram(msg, silent=False)
                if sent:
                    rec = get_dip_state(sym)
                    write_dip_state(
                        symbol        = sym,
                        phase         = DipPhase.BOUNCING,
                        dip_low       = bounce_info["dip_low"],
                        entry_price   = bounce_info["dip_high_entry"],
                        alerted_at    = rec["alerted_at"] if rec else market_now().isoformat(),
                        alert_count   = (rec.get("alert_count", 1) if rec else 1),
                        last_deepen_at= rec.get("last_deepen_at") if rec else None,
                    )
                    logging.info(f"bounce_alert sent | {sym}")
                continue   # done with this symbol this pass

            # ── Deepen check (only if no bounce) ──────────────────
            deepen_info = check_deepen(sym, current)
            if deepen_info:
                print(f"  {sym:6s} 🔻 DEEPEN {deepen_info['drop_extension_pct']:.1f}% below prev low")
                msg  = format_deepen_alert(deepen_info, ctx)
                sent = send_telegram(msg, silent=False)
                if sent:
                    rec = get_dip_state(sym)
                    if rec:
                        def _update_deepen(s, _sym=sym):
                            r = s.get(_dip_state_key(_sym))
                            if r:
                                r["last_deepen_at"] = market_now().isoformat()
                        _dip_state_update(_update_deepen)
                    logging.info(f"deepen_alert sent | {sym}")
            else:
                rec = get_dip_state(sym)
                if rec:
                    gap = rec["bounce_threshold"] - current
                    print(f"  {sym:6s} ↓ dipping (${current:.2f}, bounce at ${rec['bounce_threshold']:.2f}, gap ${gap:.2f})")

        except Exception as exc:
            logging.error(f"bounce/deepen pass {sym}: {exc}")


# ════════════════════════════════════════════════════════════════════
# SECTION 16 — MAIN SCAN PIPELINE
# ════════════════════════════════════════════════════════════════════

def run_dip_scan() -> None:
    """
    Full scan pipeline:
        1. Weekend / window guard
        2. Market context fetch
        3. Bounce + deepen pass (checks previously alerted dips)
        4. Purge stale dip states
        5. Cooldown filter
        6. Parallel fetch + qualify_dip
        7. Sort, format, send
        8. Post-send: cooldown + dip state write + JSONL log
    """
    print(f"\n{'='*50}")
    print(f"🎯 ALPHAEDGE DIP SCANNER v3.4")
    print(f"🕒 {display_now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"📊 Universe: {len(DIP_UNIVERSE)} stocks / {SECTOR_COUNT} sectors")
    print('='*50)
    logging.info(f"Scan start | universe={len(DIP_UNIVERSE)}")

    if is_weekend():
        print("⚠️  Weekend — skipping")
        logging.info("skip:weekend")
        return
    if not in_window(CFG.scan_window):
        print(f"⚠️  Outside scan window {CFG.scan_window[0]}–{CFG.scan_window[1]} ET")
        logging.info("skip:outside_window")
        return

    # ── Market context ────────────────────────────────────────────
    market_ctx = get_market_ctx()

    # ── Bounce + deepen pass (before cooldown filter) ─────────────
    _run_bounce_pass()

    # ── Purge stale states ────────────────────────────────────────
    purged = _purge_stale_dip_states()
    if purged:
        print(f"  🗑️  Purged {purged} stale dip state(s) (>{DIP_TTL_HOURS}h)")

    # ── Cooldown filter ───────────────────────────────────────────
    eligible:    list[str] = []
    in_cooldown: int       = 0
    for sym in DIP_UNIVERSE:
        if is_in_cooldown(sym):
            in_cooldown += 1
        else:
            eligible.append(sym)
    print(f"  Cooldown: {in_cooldown} • Eligible: {len(eligible)}")

    # ── Parallel fetch + qualify ──────────────────────────────────
    candidates: list[dict]     = []
    scanned:    int            = 0
    failed:     int            = 0
    disq:       dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=CFG.fetch_workers) as executor:
        future_map = {executor.submit(_scan_one, s): s for s in eligible}
        for fut in as_completed(future_map):
            sym, ctx, price_stats, err = fut.result()

            if err is not None or ctx is None or price_stats is None:
                failed += 1
                print(f"  {sym:6s} ✗ {err}")
                continue

            scanned += 1
            # Pass market_ctx so qualify_dip can apply VIX-aware scoring
            q = qualify_dip(ctx, price_stats, market_ctx)

            if not q.qualified:
                code = q.fail_code.value if q.fail_code else "unknown"
                disq[code] = disq.get(code, 0) + 1
                print(f"  {sym:6s} ✗ {code} {q.fail_detail}")
                continue

            print(f"  {sym:6s} 🎯 score={q.score}/16")
            candidates.append({"ctx": ctx, "q": q})

    candidates.sort(key=lambda c: (-c["q"].score, c["q"].drop_5d or 0))

    print(f"\n{'-'*50}")
    print(f"📊 Scanned: {scanned} • Failed: {failed} • Cooldown: {in_cooldown}")
    print(f"   Qualified: {len(candidates)}")
    if disq:
        for code, cnt in sorted(disq.items(), key=lambda x: -x[1])[:5]:
            print(f"   • {code}: {cnt}")

    logging.info(
        f"Scan done | scanned={scanned} failed={failed} "
        f"cooldown={in_cooldown} qualified={len(candidates)}"
    )

    if not candidates:
        print("\n✅ No qualifying setups.")
        return

    # ── Format & send ─────────────────────────────────────────────
    alert_msg = format_alert(
        candidates,
        market_ctx,
        stats={
            "scanned":      scanned,
            "failed":       failed,
            "cooldown":     in_cooldown,
            "disqualified": disq,
        },
    )
    sent_ok = send_telegram(alert_msg, silent=False)

    # ── Post-send: cooldown + dip state + JSONL ───────────────────
    if sent_ok:
        top_shown = candidates[:CFG.max_total_shown]
        for c in top_shown:
            sym = c["ctx"]["symbol"]
            q   = c["q"]

            mark_alert(cooldown_key(sym))

            # Write initial DIPPING state for bounce/deepen tracking
            try:
                dip_anchor = q.buy_low if q.buy_low is not None else c["ctx"]["current"]
                write_dip_state(
                    symbol      = sym,
                    phase       = DipPhase.DIPPING,
                    dip_low     = dip_anchor,
                    entry_price = c["ctx"]["current"],
                    alerted_at  = market_now().isoformat(),
                    alert_count = 1,
                )
            except Exception as exc:
                logging.warning(f"write_dip_state post-alert {sym}: {exc}")

            try:
                record = json.dumps({
                    "ts":       market_now().isoformat(),
                    "sym":      sym,
                    "score":    q.score,
                    "rsi":      c["ctx"]["rsi"],
                    "drop_5d":  q.drop_5d,
                    "buy_low":  q.buy_low,
                    "buy_high": q.buy_high,
                    "stop":     q.stop,
                })
                with open(QUALIFIED_LOG_FILE, "a") as log_fh:
                    log_fh.write(record + "\n")
                    log_fh.flush()
            except Exception as log_exc:
                logging.warning(f"jsonl log {sym}: {log_exc}")

        print(f"\n✅ Alert sent ({len(candidates)} qualified, top {len(top_shown)} shown)")
    else:
        print("\n❌ Telegram send failed — cooldown NOT recorded; will retry next scan")


# ════════════════════════════════════════════════════════════════════
# SECTION 17 — DIAGNOSTIC MODE
# ════════════════════════════════════════════════════════════════════

def run_diagnostics() -> None:
    """
    Developer / debug mode — first 20 universe symbols.
    Does NOT send Telegram or touch cooldown / dip state.
    Also prints active dip states from scanner_state.json.
    Invoke:  python dip_scanner.py --diagnostics
    """
    print("\n🔍 DIAGNOSTIC MODE — first 20 universe symbols\n")

    # Show active dip states
    state = load_json(STATE_FILE, {})
    active = {k: v for k, v in state.items() if k.startswith("dip_state_")}
    if active:
        print(f"📋 Active dip states ({len(active)}):")
        for key, rec in active.items():
            sym = key.removeprefix("dip_state_")
            print(
                f"   {sym}: phase={rec.get('phase')} "
                f"low=${rec.get('dip_low', 0):.2f} "
                f"bounce≥${rec.get('bounce_threshold', 0):.2f} "
                f"alerted={rec.get('alerted_at','?')[:16]}"
            )
        print()

    market_ctx = get_market_ctx()

    for symbol in DIP_UNIVERSE[:20]:
        print(f"\n{'-'*40}\n📊 {symbol}")
        try:
            ctx = get_full_context(symbol)
            if not ctx:
                print("   ❌ no ctx")
                continue

            price_stats = fetch_price_stats(symbol)
            ema200_disp = ctx.get("ema200_real") or ctx.get("ema200")
            print(
                f"   Price ${ctx.get('current')} • EMA50 ${ctx.get('ema50')} "
                f"• EMA200 ${ema200_disp}"
            )
            print(
                f"   RSI {ctx.get('rsi')} • Day {ctx.get('day_change_pct')}% "
                f"• ATH {ctx.get('ath_pct')}%"
            )
            print(
                f"   Vol {ctx.get('vol_ratio')}× • 5d {price_stats.drop_5d} "
                f"• EMA200↑ {price_stats.ema200_rising}"
            )

            q = qualify_dip(ctx, price_stats, market_ctx)
            if q.qualified:
                vix_note = " (VIX penalised)" if q.vix_penalised else ""
                print(f"   ✅ score={q.score}/16{vix_note}")
                for r in q.reasons:
                    print(f"      • {r}")
                print(
                    f"   🟢 Buy ${q.buy_low:.2f}–${q.buy_high:.2f} "
                    f"• Stop ${q.stop:.2f}"
                )
            else:
                detail = f"({q.fail_detail})" if q.fail_detail else ""
                print(f"   ❌ {q.fail_code.value if q.fail_code else '?'} {detail}")
        except Exception as exc:
            print(f"   💥 {exc}")


# ════════════════════════════════════════════════════════════════════
# SECTION 18 — LOGGING SETUP
# ════════════════════════════════════════════════════════════════════

def setup_logging() -> None:
    logger = logging.getLogger("dip_scanner")
    if logger.handlers:
        return
    handler = TimedRotatingFileHandler(
        LOGS_DIR / "dipscan.log", when="midnight", backupCount=14,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = True


# ════════════════════════════════════════════════════════════════════
# SECTION 19 — ENTRYPOINT
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    setup_logging()
    if "--debug" in sys.argv or "--diagnostics" in sys.argv:
        run_diagnostics()
    else:
        run_dip_scan()


# ════════════════════════════════════════════════════════════════════
# END OF FILE
# ════════════════════════════════════════════════════════════════════
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  CONTINUATION PROMPT — paste this into a new chat to resume    │
# │  or extend this codebase with full context.                    │
# └─────────────────────────────────────────────────────────────────┘
#
# """
# ALPHAEDGE DIP BUY SCANNER — New-Chat Continuation Prompt
# ─────────────────────────────────────────────────────────
#
# WHAT THIS FILE IS:
#   dip_scanner.py v3.3 — a stock dip-buy alert system.
#   It scans a curated universe of tickers (from symbols.yaml via
#   market_intel.SYMBOL_META) for pullbacks in strong uptrends that
#   are temporarily oversold, then sends a ranked Telegram alert.
#
# ARCHITECTURE:
#   • Universe:   symbols.yaml → SYMBOL_META (role="dip")
#   • Data:       market_intel._yf_download (cached yfinance daily bars)
#   • Indicators: EMA50, EMA200, RSI, ATR(14), swing-low, vol ratio,
#                 relative strength vs SPY
#   • Delivery:   market_intel.send_telegram (auto-split, MarkdownV2)
#   • Cooldown:   market_intel.can_alert / mark_alert (atomic, 4h)
#   • Earnings:   market_intel.get_earnings_date (12h cached)
#   • Config:     Config frozen dataclass + YAML overrides
#
# QUALIFICATION LOGIC (all gates must pass in order):
#   1. EMA200 position: above OR within flex-band with rising slope
#   2. RSI in [25, 48]
#   3. Dip magnitude: 1d ≤ -1.5% OR 5d ≤ -4.0%  (OR gate)
#   4. ATH proximity: not more than 30% below ATH
#   5. Volume ratio ≥ 0.6× 20d avg
#   6. No earnings within 3 calendar days
#
# SCORING (0–16):
#   Trend position  1–3  (above both EMAs = 3)
#   RSI depth       1–3  (≤30 = 3)
#   ATH proximity   0–3  (>-5% = 3)
#   Volume char     0–2  (>1.8× = 2)
#   5d drop depth   1–3  (≤-10% = 3)
#   Relative strength 0–2 (vs SPY)
#
#   Tiers: ELITE ≥13 | STRONG ≥9 | WATCHLIST <9
#
# TRADE PLAN:
#   buy_low  = max(swing_low_20d, ema50 - 0.5*atr)
#   buy_high = max(current, ema50)
#   stop     = max(ema200, swing_low - atr) capped at 8% loss
#   (v3.3 FIX: removed erroneous 0.1% clamp that broke the 8% cap)
#
# KEY BUGS FIXED IN v3.3:
#   1. Stop-loss clamp: min(stop, current*0.999) collapsed all stops
#      to 0.1% below current, making max_loss_pct irrelevant.
#   2. YAML override type coercion: no casting meant YAML string "4"
#      stored as str in frozen dataclass.
#   3. YAML scan_window: YAML list ["07:30","20:30"] not parsed to dtime.
#   4. Dead imports: ZoneInfo unused; H_RULE from market_intel unused.
#   5. JSONL log: missing flush() — partial entries on crash.
#   6. Logging dedup: isinstance check allowed duplicate handlers.
#   7. ema50 zero guard: buy-zone maths could divide by zero.
#   8. Telegram escape: some dynamic strings in format_candidate
#      were not escaped via md().
#
# WHAT TO VERIFY WHEN MODIFYING:
#   □ Config field added? → add entry to _YAML_COERCE dict.
#   □ New scoring dimension? → update max score comment (currently 16).
#   □ New gate added? → add FailCode enum value + update docstring order.
#   □ market_intel API change? → check all imports in SECTION 3.
#   □ symbols.yaml schema change? → update _build_dip_universe().
#   □ Telegram MarkdownV2 special chars? → wrap ALL dynamic values in md().
#   □ Stop maths changed? → ensure stop < current always holds.
#   □ buy_low/buy_high changed? → ensure buy_low < buy_high always holds.
#   □ Concurrency changed? → review ThreadPoolExecutor worker count vs
#     yfinance rate limits.
#   □ After any logic change: run --diagnostics on known symbols and
#     manually verify score, buy zone, and stop make sense.
#
# RELATED FILES:
#   market_intel.py  — shared data layer (caching, delivery, escape)
#   symbols.yaml     — universe definition + per-symbol metadata
#   brief.py         — daily market brief (shares SECTORS taxonomy)
#   logs/dipscan.log — rotating operational log
#   logs/dip_qualified.jsonl — JSONL record of every alerted setup
# """
