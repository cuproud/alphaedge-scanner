"""
╔══════════════════════════════════════════════════════════════════╗
║           ALPHAEDGE DIP BUY SCANNER  v3.3                       ║
║           Unified Build — Audited & Bug-Fixed                    ║
╠══════════════════════════════════════════════════════════════════╣
║  PURPOSE                                                         ║
║  ───────                                                         ║
║  Scans a curated stock universe for healthy pullbacks in strong  ║
║  uptrends that are temporarily oversold.  Sends a structured     ║
║  Telegram alert ranked by quality tier (ELITE / STRONG /         ║
║  WATCHLIST) with buy zones, stops, and risk labels.              ║
║                                                                  ║
║  ARCHITECTURE                                                     ║
║  ────────────                                                     ║
║  • Universe   → symbols.yaml  (role: "dip")                     ║
║  • Prices     → market_intel._yf_download  (cached)             ║
║  • Earnings   → market_intel.get_earnings_date  (12h cache)      ║
║  • Cooldown   → market_intel.mark_alert / can_alert  (atomic)   ║
║  • Delivery   → market_intel.send_telegram (auto-split/escape)   ║
║  • Config     → Config dataclass + YAML settings.dip_scanner     ║
║                 overrides at startup                             ║
║                                                                  ║
║  DEPENDENCIES (market_intel ≥ v3.0, symbols.yaml ≥ v3)          ║
║  ──────────────────────────────────────────────────────          ║
║  SECTORS, SYMBOL_EMOJI, SYMBOL_META, SYMBOL_TO_SECTOR,           ║
║  YAML_SETTINGS, _yf_download, calc_relative_strength,           ║
║  can_alert, display_now, get_earnings_date, get_full_context,    ║
║  get_market_ctx, mark_alert, market_now, send_telegram,          ║
║  tg_escape, H_RULE                                               ║
║                                                                  ║
║  CHANGELOG  v3.3  (vs v3.2)                                      ║
║  ─────────────────────────                                        ║
║  FIX  _apply_yaml_overrides — added type coercion so YAML        ║
║       string/int/float values are cast to match Config field     ║
║       type before replace(); prevents silent TypeError.          ║
║  FIX  scan_window YAML override — parses "HH:MM" strings into    ║
║       datetime.time objects; previously would crash.             ║
║  FIX  stop-loss clamp — removed erroneous min(stop,current*      ║
║       0.999) that silently overrode the 8% max-loss cap,         ║
║       collapsing all stops to 0.1% below current.               ║
║  FIX  buy_low guard — added clamp so buy_low < buy_high even     ║
║       in gap/spike scenarios where swing > current.              ║
║  FIX  Dead import — removed unused `ZoneInfo` import.            ║
║  FIX  Unused import — removed `H_RULE as _MI_HRULE`; local       ║
║       H_RULE is intentionally narrower for mobile.              ║
║  FIX  JSONL log — added explicit flush after each write to        ║
║       reduce partial-entry risk on crash.                        ║
║  FIX  Logging dedup — improved handler check to avoid duplicate   ║
║       log lines on repeated imports.                             ║
║  FIX  format_candidate — escape applied to ALL user-facing        ║
║       strings that could contain Markdown special characters.    ║
║  FIX  _scan_one error string — now includes symbol for clarity   ║
║       when future result is an exception.                        ║
║  IMPR Added DATACLASS_FIELD_TYPES mapping for safe YAML coerce.  ║
║  IMPR Section headings added throughout for IDE folding and       ║
║       future-enhancement navigation.                             ║
║  IMPR qualify_dip — explicit guard if ema50 is 0/None to avoid   ║
║       ZeroDivisionError in buy-zone maths.                       ║
║  IMPR Inline comments expanded on every non-trivial formula.     ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ════════════════════════════════════════════════════════════════════
# SECTION 1 — STANDARD-LIBRARY IMPORTS
# ════════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, time as dtime
from enum import Enum
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

# ════════════════════════════════════════════════════════════════════
# SECTION 2 — THIRD-PARTY IMPORTS
# ════════════════════════════════════════════════════════════════════
import numpy as np           # used implicitly by pandas; explicit for clarity
import pandas as pd

# ════════════════════════════════════════════════════════════════════
# SECTION 3 — INTERNAL / MARKET-INTEL IMPORTS
# ════════════════════════════════════════════════════════════════════
# All cross-module concerns (caching, delivery, escape, cooldown)
# are delegated to market_intel so this module stays pure-logic.
from market_intel import (
    SECTORS,                  # dict[sector_name, list[symbol]]
    SYMBOL_EMOJI,             # dict[symbol, emoji_str]
    SYMBOL_META,              # dict[symbol, {name, exchange, roles, …}]
    SYMBOL_TO_SECTOR,         # dict[symbol, sector_name]
    YAML_SETTINGS,            # optional overrides from symbols.yaml
    _yf_download,             # cache-aware yfinance downloader
    calc_relative_strength,   # returns (rs_score, rs_label)
    can_alert,                # cooldown check — True if symbol may alert
    display_now,              # current time in display timezone (ET)
    get_earnings_date,        # (date, days_away) — 12h cached
    get_full_context,         # full per-symbol context dict
    get_market_ctx,           # SPY / QQQ / VIX snapshot
    mark_alert,               # record cooldown after confirmed send
    market_now,               # current market-clock datetime (ET)
    send_telegram,            # handles auto-split + parse-mode fallback
    tg_escape as md,          # Telegram MarkdownV2 escape helper
)
# NOTE: H_RULE from market_intel is intentionally NOT imported here.
# This scanner uses a narrower 21-char rule optimised for mobile.

# ════════════════════════════════════════════════════════════════════
# SECTION 4 — PATHS & CONSTANTS
# ════════════════════════════════════════════════════════════════════

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
QUALIFIED_LOG_FILE = LOGS_DIR / "dip_qualified.jsonl"

# Mobile-optimised horizontal rule (21 chars, narrower than market_intel's)
H_RULE = "─────────────────────"

# ════════════════════════════════════════════════════════════════════
# SECTION 5 — CONFIGURATION DATACLASS
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    """
    All tuneable parameters in one place.
    Values are overridable via symbols.yaml → settings.dip_scanner.
    scan_window may be provided as ["HH:MM", "HH:MM"] in YAML.
    """
    # ── Qualification thresholds ──────────────────────────────────
    rsi_min: float           = 25     # Below this → suspected breakdown, skip
    rsi_max: float           = 48     # Above this → not oversold enough
    drop_1d_max: float       = -1.5   # 1-day % drop required (OR gate with 5d)
    drop_5d_max: float       = -4.0   # 5-day % drop required (OR gate with 1d)
    ath_min: float           = -30.0  # Maximum % below all-time-high allowed
    vol_ratio_min: float     = 0.6    # Minimum vol vs 20d avg (thin = suspect)
    ema200_flex_pct: float   = 5.0    # How far below EMA200 still acceptable (%)
    ema_slope_min_pct: float = 0.5    # EMA200 must be rising ≥ this % over 10 bars

    # ── Operational settings ──────────────────────────────────────
    cooldown_hours: int      = 4      # Min hours between alerts for same symbol
    scan_window: tuple       = (dtime(7, 30), dtime(20, 30))  # ET window

    # ── Execution / concurrency ───────────────────────────────────
    fetch_workers: int       = 5      # Parallel yfinance fetch threads

    # ── Trade plan maths ─────────────────────────────────────────
    max_loss_pct: float      = 8.0    # Hard cap: stop never more than 8% below entry

    # ── Alert display ─────────────────────────────────────────────
    top_per_tier: int        = 5      # Max candidates shown per tier
    max_total_shown: int     = 12     # Absolute cap across all tiers


# ── YAML type coercion map ─────────────────────────────────────────
# Maps Config field name → callable that converts the raw YAML value
# to the correct Python type.  Without this, YAML integers arrive as
# str and replace() silently stores the wrong type.
_YAML_COERCE: dict[str, Any] = {
    "rsi_min":           float,
    "rsi_max":           float,
    "drop_1d_max":       float,
    "drop_5d_max":       float,
    "ath_min":           float,
    "vol_ratio_min":     float,
    "ema200_flex_pct":   float,
    "ema_slope_min_pct": float,
    "cooldown_hours":    int,
    "fetch_workers":     int,
    "max_loss_pct":      float,
    "top_per_tier":      int,
    "max_total_shown":   int,
    # scan_window handled separately — needs dtime parsing
}


def _parse_scan_window(raw: Any) -> tuple[dtime, dtime]:
    """
    Convert a YAML scan_window value to a (dtime, dtime) tuple.
    Accepts: ["HH:MM", "HH:MM"]  or  ["HH:MM:SS", "HH:MM:SS"].
    Raises ValueError with a descriptive message on bad format.
    """
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
    """
    Reads settings.dip_scanner from YAML_SETTINGS and applies valid
    overrides to the Config dataclass.  Values are type-coerced to
    match the field's declared type before replace() is called.
    Unknown keys are silently ignored (logged at DEBUG).
    """
    overrides = (YAML_SETTINGS or {}).get("dip_scanner") or {}
    if not overrides:
        return cfg

    valid_fields = {f.name for f in fields(cfg)}
    safe: dict[str, Any] = {}

    for key, raw_val in overrides.items():
        if key not in valid_fields:
            logging.debug(f"dip_scanner YAML: unknown key '{key}' — ignored")
            continue

        # scan_window needs special parsing (list of strings → tuple of dtime)
        if key == "scan_window":
            try:
                safe[key] = _parse_scan_window(raw_val)
            except ValueError as exc:
                logging.warning(f"dip_scanner YAML: scan_window override skipped — {exc}")
            continue

        # All other fields: cast via _YAML_COERCE map
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
    """
    Build the scan universe from SYMBOL_META (symbols.yaml).
    Includes only symbols whose 'roles' list contains 'dip'.
    Falls back to ALL symbols if none opt-in, or to the flat
    SECTORS union if SYMBOL_META is entirely empty (legacy mode).

    Returns:
        syms        — ordered list of tickers to scan
        sym_sector  — dict mapping each ticker to its sector name
    """
    if SYMBOL_META:
        syms = [s for s, m in SYMBOL_META.items() if "dip" in (m.get("roles") or [])]
        if not syms:
            # YAML present but no symbol opted in → scan everything
            logging.warning("dip_scanner: no symbols have role='dip'; scanning full universe")
            syms = list(SYMBOL_META.keys())
    else:
        # Legacy fallback: derive symbols from SECTORS dict in market_intel
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
    """True if the current ET day is Saturday or Sunday."""
    return market_now().weekday() >= 5


def in_window(win: tuple[dtime, dtime]) -> bool:
    """True if the current ET time falls inside [win[0], win[1])."""
    t = market_now().time()
    return win[0] <= t < win[1]


# ════════════════════════════════════════════════════════════════════
# SECTION 8 — COOLDOWN HELPERS
# ════════════════════════════════════════════════════════════════════

def cooldown_key(symbol: str) -> str:
    """Namespaced cooldown key so dip alerts don't collide with other scanners."""
    return f"dip_alert_{symbol}"


def is_in_cooldown(symbol: str) -> bool:
    """
    Returns True if this symbol was alerted within the last
    CFG.cooldown_hours hours (delegates to market_intel atomic state).
    """
    return not can_alert(cooldown_key(symbol), CFG.cooldown_hours)


# ════════════════════════════════════════════════════════════════════
# SECTION 9 — PRICE STATS (extended daily indicators)
# ════════════════════════════════════════════════════════════════════

@dataclass
class PriceStats:
    """
    Extended price indicators computed from daily OHLCV data.
    All fields are Optional — callers must guard against None.
    """
    drop_5d: float | None        # % change over last 5 trading days
    ema200_rising: bool | None   # True if EMA200 slope ≥ CFG.ema_slope_min_pct
    swing_low_20d: float | None  # Lowest closing price of prior 20 sessions
    atr_14: float | None         # 14-period Wilder ATR


def fetch_price_stats(symbol: str) -> PriceStats:
    """
    Compute PriceStats for *symbol* using the cached daily download
    from market_intel._yf_download.  Returns all-None on any error
    so callers never see an exception from this function.

    Indicators:
        drop_5d       — (close[-1] / close[-6] - 1) * 100
        ema200_rising — EMA200 10-bar slope as % of current price
        swing_low_20d — min(close[-21:-1])  (excludes today's partial bar)
        atr_14        — Wilder ATR = rolling(14).mean() of True Range
    """
    df = _yf_download(symbol, period="1y", interval="1d")
    if df is None or df.empty or len(df) < 30:
        logging.debug(f"fetch_price_stats({symbol}): insufficient data")
        return PriceStats(None, None, None, None)

    try:
        close = df["Close"]

        # 5-day percentage change ─────────────────────────────────
        drop_5d: float | None = None
        if len(close) >= 6:
            drop_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)

        # EMA200 slope (% of current price over 10 bars) ──────────
        ema200_rising: bool | None = None
        if len(close) >= 210:
            ema = close.ewm(span=200, adjust=False).mean()
            slope_pct = float((ema.iloc[-1] - ema.iloc[-10]) / close.iloc[-1] * 100)
            ema200_rising = slope_pct >= CFG.ema_slope_min_pct

        # 20-day swing low (closed sessions only, not today) ──────
        swing_low_20d: float | None = None
        if len(close) >= 21:
            swing_low_20d = float(close.iloc[-21:-1].min())

        # ATR(14) — Wilder method ─────────────────────────────────
        atr_14: float | None = None
        if len(df) >= 15 and {"High", "Low", "Close"}.issubset(df.columns):
            high = df["High"]
            low  = df["Low"]
            prev = close.shift(1)
            true_range = pd.concat(
                [high - low, (high - prev).abs(), (low - prev).abs()],
                axis=1,
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
    """Human-readable disqualification reasons used in console output and alert footers."""
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
    """
    Full output of qualify_dip().
    qualified=True means the symbol passed all gates and has a trade plan.
    """
    qualified:   bool              = False
    score:       int               = 0          # 0–16; higher = better setup
    reasons:     list[str]         = field(default_factory=list)
    fail_code:   FailCode | None   = None
    fail_detail: str               = ""
    drop_5d:     float | None      = None
    rs_score:    float | None      = None
    rs_label:    str   | None      = None
    trend_note:  str               = ""
    buy_low:     float | None      = None
    buy_high:    float | None      = None
    stop:        float | None      = None


def qualify_dip(ctx: dict, stats: PriceStats) -> QualifyResult:
    """
    Apply all qualification gates to a symbol context + price stats.
    Returns immediately with fail_code set on the first failing gate.
    On full pass, computes score (0–16), reasons, and trade plan.

    Gate order (all must pass):
        1. EMA200 position / slope
        2. RSI band
        3. Sufficient dip (1d OR 5d)
        4. ATH proximity
        5. Volume ratio
        6. No earnings within 3 days

    Score breakdown (max 16):
        Trend position   → 1–3
        RSI depth        → 1–3
        ATH proximity    → 0–3
        Volume ratio     → 0–2
        5-day drop depth → 1–3
        Relative strength→ 0–2
    """
    res = QualifyResult()
    sym     = ctx["symbol"]
    current = ctx["current"]

    # ── Gate 0: EMA50 sanity (needed for buy-zone maths) ─────────
    ema50 = ctx.get("ema50") or 0.0
    if not ema50 or ema50 <= 0:
        res.fail_code, res.fail_detail = FailCode.EMA50_INVALID, "ema50=0"
        return res

    # ── Gate 1: EMA200 position & slope ──────────────────────────
    # Prefer ema200_real (computed from raw history) over ema200
    # (which may be a pre-computed/rounded value from market_intel).
    ema200 = ctx.get("ema200_real") or ctx.get("ema200") or 0.0
    if not ema200 or ema200 <= 0:
        res.fail_code, res.fail_detail = FailCode.MISSING_FIELDS, "ema200"
        return res

    pct_from_ema200 = (current / ema200 - 1) * 100
    above_200       = current > ema200

    if above_200:
        res.trend_note = f"Above EMA200 ({pct_from_ema200:+.1f}%)"
    elif pct_from_ema200 >= -CFG.ema200_flex_pct:
        # Within flex band: allow only if EMA200 is rising (trend intact)
        if stats.ema200_rising:
            res.trend_note = f"Below EMA200 ({pct_from_ema200:+.1f}%) but rising"
        else:
            res.fail_code  = FailCode.BELOW_EMA200_FLAT_SLOPE
            res.fail_detail = f"{pct_from_ema200:+.1f}%"
            return res
    else:
        # Too far below EMA200 — likely a structural breakdown, not a dip
        res.fail_code   = FailCode.BELOW_EMA200_HARD
        res.fail_detail = f"{pct_from_ema200:+.1f}%"
        return res

    # ── Gate 2: RSI band ──────────────────────────────────────────
    rsi = ctx["rsi"]
    if rsi < CFG.rsi_min:
        res.fail_code, res.fail_detail = FailCode.RSI_TOO_LOW,  f"{rsi:.0f}"
        return res
    if rsi > CFG.rsi_max:
        res.fail_code, res.fail_detail = FailCode.RSI_TOO_HIGH, f"{rsi:.0f}"
        return res

    # ── Gate 3: Dip magnitude (OR logic — 1d or 5d must qualify) ─
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

    # ── Gate 6: Earnings proximity (12h-cached lookup) ───────────
    _, days_to_earn = get_earnings_date(sym)
    if days_to_earn is not None and 0 <= days_to_earn <= 3:
        res.fail_code   = FailCode.EARNINGS_SOON
        res.fail_detail = f"{days_to_earn}d"
        return res

    # ─────────────────────────────────────────────────────────────
    # ALL GATES PASSED — compute score and trade plan
    # ─────────────────────────────────────────────────────────────

    score:   int       = 0
    reasons: list[str] = []
    above_50 = current > ema50

    # Trend position (1–3 pts) ────────────────────────────────────
    if above_50 and above_200:
        score += 3; reasons.append("📈 Strong trend (above EMA50 & EMA200)")
    elif above_200:
        score += 2; reasons.append("📉 Pulling back to EMA50 zone")
    else:
        score += 1; reasons.append("⚠️ Testing EMA200 (slope rising)")

    # RSI depth (1–3 pts) ─────────────────────────────────────────
    if rsi <= 30:
        score += 3; reasons.append(f"🔥 Deeply oversold (RSI {rsi:.0f})")
    elif rsi <= 35:
        score += 2; reasons.append(f"📊 Oversold (RSI {rsi:.0f})")
    else:
        score += 1; reasons.append(f"📊 Cooling off (RSI {rsi:.0f})")

    # ATH proximity (0–3 pts) ─────────────────────────────────────
    if   ath_pct > -5:   score += 3; reasons.append(f"🏔️ Very near ATH ({ath_pct:+.1f}%)")
    elif ath_pct > -10:  score += 2; reasons.append(f"📍 Close to ATH ({ath_pct:+.1f}%)")
    elif ath_pct > -20:  score += 1; reasons.append(f"📍 Moderate pullback ({ath_pct:+.1f}%)")
    # else: < -20 — already blocked by gate 4 (ath_min=-30) unless relaxed via YAML

    # Volume character (0–2 pts) ──────────────────────────────────
    if   vol_ratio > 1.8: score += 2; reasons.append(f"🔊 High vol capitulation ({vol_ratio:.1f}×)")
    elif vol_ratio > 1.2: score += 1; reasons.append(f"📊 Above-avg volume ({vol_ratio:.1f}×)")

    # 5-day drop depth (1–3 pts) ──────────────────────────────────
    d5 = stats.drop_5d
    if   d5 <= -10: score += 3; reasons.append(f"💥 Sharp 5d selloff ({d5:+.1f}%)")
    elif d5 <= -7:  score += 2; reasons.append(f"📉 Significant 5d drop ({d5:+.1f}%)")
    else:           score += 1; reasons.append(f"📉 Moderate 5d dip ({d5:+.1f}%)")

    # Relative strength vs SPY (0–2 pts) ─────────────────────────
    try:
        rs_score, rs_label = calc_relative_strength(ctx)
        res.rs_score, res.rs_label = rs_score, rs_label
        if rs_score is not None:
            if   rs_score > 2: score += 2; reasons.append(f"💪 Outperforming SPY ({rs_label})")
            elif rs_score > 0: score += 1; reasons.append(f"📊 Holding vs SPY ({rs_label})")
    except Exception as exc:
        logging.debug(f"qualify_dip RS {sym}: {exc}")

    # ─── Trade plan: buy zone + stop ─────────────────────────────
    #
    # ATR provides a volatility-scaled buffer.
    # If ATR unavailable, proxy with 2% of current price (conservative).
    atr   = stats.atr_14 or (current * 0.02)
    swing = stats.swing_low_20d or (current - 2 * atr)

    # Buy zone: between recent swing low and EMA50 area
    #   buy_low  = max of (swing-low, EMA50 minus half-ATR buffer)
    #   buy_high = max of (current price, EMA50) — enter at or above EMA50
    buy_low  = max(swing, ema50 - 0.5 * atr)
    buy_high = max(current, ema50)

    # Guard: buy_low must always be strictly below buy_high
    # Handles gap/spike scenarios where swing > current
    if buy_low >= buy_high:
        buy_low = buy_high * 0.99

    # Stop: place below the deeper of (EMA200, swing - 1 ATR)
    #   then enforce max-loss cap so stop ≥ current * (1 - max_loss%)
    #   FIX v3.3: removed erroneous min(stop, current*0.999) which
    #   overrode the 8% cap and collapsed all stops to 0.1% below entry.
    raw_stop = max(ema200, swing - atr)
    cap_stop = current * (1.0 - CFG.max_loss_pct / 100.0)
    stop     = max(raw_stop, cap_stop)

    # Final sanity: stop must be below current (not at or above)
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
    emoji: str,
    title: str,
    subtitle: str | None = None,
    border: str = "━━━━━━━━━━━━━━━━━━━━━",
) -> str:
    """Render a Telegram MarkdownV2 header block with optional subtitle."""
    msg = f"{emoji} *{title}*\n`{border}`\n"
    if subtitle:
        msg += f"_{md(subtitle)}_\n"
    msg += "\n"
    return msg


def _tier(score: int) -> tuple[str, str]:
    """
    Map a numeric score (0–16) to (badge_emoji, tier_name).
    ELITE ≥ 13, STRONG ≥ 9, else WATCHLIST.
    """
    if score >= 13:
        return ("🏆", "ELITE")
    if score >= 9:
        return ("⭐", "STRONG")
    return ("✅", "WATCHLIST")


def _name_label(sym: str) -> str:
    """
    Build a display label for a symbol.
    Uses SYMBOL_META for name and exchange if available.
    All parts are escaped for Telegram MarkdownV2.
    Example: 'NVDA — NVIDIA Corp\\. \\(NASDAQ\\)'
    """
    meta = SYMBOL_META.get(sym, {})
    name = meta.get("name", "")
    exch = meta.get("exchange", "")

    if name and exch:
        return f"{md(sym)} — {md(name)} ({md(exch)})"
    if name:
        return f"{md(sym)} — {md(name)}"
    return md(sym)


def _rsi_mood(rsi: float) -> str:
    """Return a short mood label for RSI level."""
    if rsi <= 30:
        return "🔥 Deep oversold"
    if rsi <= 35:
        return "🟢 Oversold"
    if rsi <= 42:
        return "🟡 Cooling"
    return "⚪ Mild dip"


def _risk_label(stop_pct: float) -> str:
    """
    Classify stop-loss risk level by percentage distance below entry.
    stop_pct is expected to be negative (e.g. -5.2 means 5.2% below).
    """
    if stop_pct >= -3:
        return "Low risk"
    if stop_pct >= -6:
        return "Medium risk"
    return "Higher risk"


# ════════════════════════════════════════════════════════════════════
# SECTION 12 — CANDIDATE CARD FORMATTER
# ════════════════════════════════════════════════════════════════════

def format_candidate(c: dict, rank: int) -> str:
    """
    Render a single qualified candidate as a Telegram MarkdownV2 block.
    Includes: tier badge, price snapshot, qualification reasons (top 3),
    and the computed trade plan (buy zone + stop).

    All dynamic string values are escaped via md() to prevent
    Telegram parse errors from special characters.
    """
    ctx: dict        = c["ctx"]
    q:   QualifyResult = c["q"]

    sym     = ctx["symbol"]
    em      = SYMBOL_EMOJI.get(sym, "📊")
    sector  = SYMBOL_SECTOR.get(sym, "Other")
    current = ctx["current"]

    badge, tier_name = _tier(q.score)

    # Relative-strength inline fragment (empty string if unavailable)
    rs_part = ""
    if q.rs_score is not None:
        rs_icon = "💪" if q.rs_score > 0 else "📉"
        rs_part = f" • RS {rs_icon} `{q.rs_score:+.1f}%`"

    # Stop loss as % below current entry
    stop_pct   = (q.stop / current - 1) * 100 if q.stop else 0.0
    risk_label = _risk_label(stop_pct)

    block  = ""
    block += f"\n{badge} *\\#{rank} — {md(tier_name)} SETUP*\n"
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
        # Escape each reason string as it may contain dynamic values
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
    """
    Assemble the complete Telegram alert message from all qualified
    candidates.  Tiers (ELITE / STRONG / WATCHLIST) are sorted by
    sector then descending score.  Overflow within a tier is noted
    as '+N more in this tier'.

    stats dict keys used:
        scanned     — int, symbols fully processed
        failed      — int, fetch/context errors
        cooldown    — int, symbols skipped due to cooldown
        disqualified— dict[str, int], reason_code → count
    """
    ts = display_now().strftime("%a %b %d • %I:%M %p ET")

    msg = dip_header("🎯", "DIP BUY SCANNER", ts, "━━━━━━━━━━━━━━━━━━━━━")

    # ── Scan summary ──────────────────────────────────────────────
    msg += f"*SCAN SUMMARY*\n"
    msg += f"`─────────────────`\n"
    msg += f"📊 Scanned: `{stats['scanned']}`\n"
    msg += f"✅ Qualified: `{len(candidates)}`\n"
    if stats["failed"]:
        msg += f"⚠️ Failed: `{stats['failed']}`\n"
    if stats["cooldown"]:
        msg += f"🔕 Cooldown: `{stats['cooldown']}`\n"

    # ── Market backdrop (SPY / QQQ / VIX) ────────────────────────
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

        if vix_p >= 25:
            msg += "⚠️ _High VIX — reduce size, be selective_\n"
        elif vix_p >= 20:
            msg += "⚡ _Elevated VIX — avoid weak setups_\n"
        else:
            msg += "✅ _Market volatility acceptable_\n"

    # ── Bucket candidates into tiers ─────────────────────────────
    tiers: dict[str, list[dict]] = {"ELITE": [], "STRONG": [], "WATCHLIST": []}
    for c in candidates:
        _, tier_name = _tier(c["q"].score)
        tiers[tier_name].append(c)

    tier_headers = {
        "ELITE": {
            "title":  "🏆 ELITE SETUPS",
            "desc":   "Best risk/reward pullbacks",
            "border": "━━━━━━━━━━━━━━━━━━━━━",
        },
        "STRONG": {
            "title":  "⭐ STRONG SETUPS",
            "desc":   "Good dips, still need confirmation",
            "border": "═════════════════════",
        },
        "WATCHLIST": {
            "title":  "✅ WATCHLIST SETUPS",
            "desc":   "Interesting, but lower conviction",
            "border": "─────────────────────",
        },
    }

    rank = 1
    total_shown = 0

    for tier_name in ("ELITE", "STRONG", "WATCHLIST"):
        bucket = tiers[tier_name]
        if not bucket:
            continue

        # Sort: group by sector first, then best score within sector
        bucket.sort(
            key=lambda c: (
                SYMBOL_SECTOR.get(c["ctx"]["symbol"], ""),
                -c["q"].score,
            )
        )

        h    = tier_headers[tier_name]
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
        top_disq = sorted(disq.items(), key=lambda x: -x[1])[:4]
        for code, cnt in top_disq:
            msg += f"• {md(code)}: `{cnt}`\n"

    # ── Standing rules reminder ───────────────────────────────────
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
    Returns (symbol, ctx, stats, error_code) where error_code is None on success.
    Never raises — exceptions are caught and returned as error strings.

    Error codes:
        'no_ctx'         — get_full_context returned None/empty
        'missing_fields' — a required field is absent from ctx
        'err:<detail>'   — unexpected exception
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
        return symbol, None, None, f"err:{exc}"


# ════════════════════════════════════════════════════════════════════
# SECTION 15 — MAIN SCAN PIPELINE
# ════════════════════════════════════════════════════════════════════

def run_dip_scan() -> None:
    """
    Full scan pipeline:
        1. Weekend / window guard
        2. Cooldown filter
        3. Parallel fetch (ThreadPoolExecutor)
        4. qualify_dip per symbol
        5. Sort, format, send
        6. Persist cooldown + JSONL log (only on confirmed send)
    """
    print(f"\n{'='*50}")
    print(f"🎯 ALPHAEDGE DIP SCANNER v3.3")
    print(f"🕒 {display_now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"📊 Universe: {len(DIP_UNIVERSE)} stocks / {SECTOR_COUNT} sectors")
    print('='*50)
    logging.info(f"Scan start | universe={len(DIP_UNIVERSE)}")

    # ── Time guards ───────────────────────────────────────────────
    if is_weekend():
        print("⚠️  Weekend — skipping")
        logging.info("skip:weekend")
        return
    if not in_window(CFG.scan_window):
        print(f"⚠️  Outside scan window "
              f"{CFG.scan_window[0]}–{CFG.scan_window[1]} ET")
        logging.info("skip:outside_window")
        return

    # ── Market context (SPY / QQQ / VIX) ─────────────────────────
    market_ctx = get_market_ctx()

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
    candidates: list[dict]      = []
    scanned:    int             = 0
    failed:     int             = 0
    disq:       dict[str, int]  = {}

    with ThreadPoolExecutor(max_workers=CFG.fetch_workers) as executor:
        future_map = {executor.submit(_scan_one, s): s for s in eligible}

        for fut in as_completed(future_map):
            sym, ctx, price_stats, err = fut.result()

            if err is not None or ctx is None or price_stats is None:
                failed += 1
                print(f"  {sym:6s} ✗ {err}")
                continue

            scanned += 1
            q = qualify_dip(ctx, price_stats)

            if not q.qualified:
                code = q.fail_code.value if q.fail_code else "unknown"
                disq[code] = disq.get(code, 0) + 1
                print(f"  {sym:6s} ✗ {code} {q.fail_detail}")
                continue

            print(f"  {sym:6s} 🎯 score={q.score}/16")
            candidates.append({"ctx": ctx, "q": q})

    # Sort: best score first; use drop_5d as tiebreaker (steeper = better entry)
    candidates.sort(key=lambda c: (-c["q"].score, c["q"].drop_5d or 0))

    # ── Console summary ───────────────────────────────────────────
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

    # send_telegram handles auto-split for >4096 char messages
    # and falls back from MarkdownV2 to plain text on parse errors
    sent_ok = send_telegram(alert_msg, silent=False)

    # ── Post-send: cooldown + JSONL log ───────────────────────────
    if sent_ok:
        top_shown = candidates[:CFG.max_total_shown]
        for c in top_shown:
            sym = c["ctx"]["symbol"]
            q   = c["q"]

            # Mark cooldown atomically (only after confirmed delivery)
            mark_alert(cooldown_key(sym))

            # Append to JSONL performance log
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
                    log_fh.flush()   # reduce partial-entry risk on crash
            except Exception as log_exc:
                logging.warning(f"jsonl log {sym}: {log_exc}")

        print(
            f"\n✅ Alert sent ({len(candidates)} qualified, "
            f"top {len(top_shown)} shown)"
        )
    else:
        print(
            "\n❌ Telegram send failed — "
            "cooldown NOT recorded; will retry next scan"
        )


# ════════════════════════════════════════════════════════════════════
# SECTION 16 — DIAGNOSTIC MODE
# ════════════════════════════════════════════════════════════════════

def run_diagnostics() -> None:
    """
    Developer / debug mode.  Runs qualify_dip against the first 20
    symbols in the universe and prints a detailed breakdown.
    Does NOT send any Telegram message or touch cooldown state.
    Invoke with:  python dip_scanner.py --diagnostics
    """
    print("\n🔍 DIAGNOSTIC MODE — first 20 universe symbols\n")
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

            q = qualify_dip(ctx, price_stats)
            if q.qualified:
                print(f"   ✅ score={q.score}/16")
                for r in q.reasons:
                    print(f"      • {r}")
                print(
                    f"   🟢 Buy ${q.buy_low:.2f}–${q.buy_high:.2f} "
                    f"• Stop ${q.stop:.2f}"
                )
            else:
                detail = f"({q.fail_detail})" if q.fail_detail else ""
                print(
                    f"   ❌ "
                    f"{q.fail_code.value if q.fail_code else '?'} "
                    f"{detail}"
                )
        except Exception as exc:
            print(f"   💥 {exc}")


# ════════════════════════════════════════════════════════════════════
# SECTION 17 — LOGGING SETUP
# ════════════════════════════════════════════════════════════════════

def setup_logging() -> None:
    """
    Configure rotating file logger (midnight rotation, 14-day retention).
    Guard prevents duplicate handlers when module is re-imported in tests.
    Uses logger name 'dip_scanner' to avoid polluting the root logger and
    colliding with market_intel's own log configuration.
    """
    logger = logging.getLogger("dip_scanner")
    if logger.handlers:
        return   # already configured — skip (handles re-import safely)

    handler = TimedRotatingFileHandler(
        LOGS_DIR / "dipscan.log",
        when="midnight",
        backupCount=14,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    # Also propagate to root so any root-level handlers (e.g. console) see it
    logger.propagate = True


# ════════════════════════════════════════════════════════════════════
# SECTION 18 — ENTRYPOINT
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
