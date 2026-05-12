"""
ALPHAEDGE MARKET INTELLIGENCE v3.0 — AUDITED BUILD
═══════════════════════════════════════════════════════════════
Trading-system core: data fetch, indicators, verdict engine,
sector/leadership detection, Telegram delivery.

v3.0 vs v2.2 — fixes & hardening
────────────────────────────────
P0
  • can_alert() is now PURE (no side effect); mark_alert() writes atomically
  • All state writes are fcntl-locked (no races with brief.py / dip_scanner.py)
  • RSI uses CLOSED candles only (no live forming-bar leakage)
  • EMA200 returns None on insufficient history (no silent fallback to EMA50)
  • prev_close anchored to last *completed* daily bar by date
  • vol_avg_20d uses prior 20 days (excludes today's partial)
  • Per-scan in-memory caches: SPY history, ticker history, earnings
  • Earnings persisted in 12h JSON cache (no 4× duplicate fetches)
  • All dynamic values escaped via tg_escape() before Telegram
  • AI rate-limit no longer blocks 15s in the scan loop
  • Crypto "today open" uses 24h rolling, not EST date filter
  • Display TZ = America/Toronto; market TZ = America/New_York

P1
  • Parallel ticker fetch (ThreadPoolExecutor, bounded)
  • Verdict engine accepts earnings_days param (no redundant fetch)
  • Verdict + alert format use snapshot of `current` (no live drift)
  • 52W window uses date filter, not 252-row slice
  • SECTORS deduplicated (PRIMARY sector wins, secondary tags retained)
  • Auto-split on blank-line boundaries (no mid-section cuts)

P2
  • Logging: TimedRotatingFileHandler in __main__ (not at import)
  • Consistent `H_RULE` / `SUB_RULE` separators across all alerts
  • Telemetry: scan duration, fetch failures, AI calls in summary
  • format_earnings_warning's unused `symbol` param removed (kept arg
    for backward-compat; param is ignored, _UNUSED suppresses warnings)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

warnings.filterwarnings("ignore", category=FutureWarning)

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════

EST        = ZoneInfo("America/New_York")    # legacy alias used elsewhere
MARKET_TZ  = EST                              # market clock — DO NOT change
DISPLAY_TZ = ZoneInfo("America/Toronto")      # same offset, user-facing

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

STATE_FILE   = "scanner_state.json"
SYMBOLS_YAML = "symbols.yaml"
LOGS_DIR     = Path("logs"); LOGS_DIR.mkdir(exist_ok=True)
EARNINGS_CACHE_FILE = "earnings_cache.json"

H_RULE   = "`━━━━━━━━━━━━━━━━━━━━━`"
SUB_RULE = "`─────────────────`"
TG_LIMIT = 4096


@dataclass(frozen=True)
class Config:
    big_drop_warn:      float = -5.0
    big_drop_critical:  float = -10.0
    big_gain_alert:     float = 8.0
    near_52w_low_pct:   float = 10.0
    ath_pullback_alert: float = -15.0

    cooldown_hours:           int = 4
    sector_bleed_cooldown_h:  int = 4
    leadership_cooldown_h:    int = 3
    earnings_warning_days:    int = 3
    earnings_cache_ttl_h:     int = 12

    fetch_workers: int = 5
    ai_timeout_s:  int = 20

CFG = Config()

# Backward-compat scalar names (used by brief.py / dip_scanner.py)
BIG_DROP_WARN          = CFG.big_drop_warn
BIG_DROP_CRITICAL      = CFG.big_drop_critical
BIG_GAIN_ALERT         = CFG.big_gain_alert
NEAR_52W_LOW_PCT       = CFG.near_52w_low_pct
ATH_PULLBACK_ALERT     = CFG.ath_pullback_alert
COOLDOWN_HOURS         = CFG.cooldown_hours
SECTOR_BLEED_COOLDOWN  = CFG.sector_bleed_cooldown_h
LEADERSHIP_COOLDOWN    = CFG.leadership_cooldown_h
EARNINGS_WARNING_DAYS  = CFG.earnings_warning_days
FETCH_DELAY            = 0.0   # serial sleep no longer needed


# ════════════════════════════════════════════════════════════
# HTTP SESSION (reused, with retry/backoff)
# ════════════════════════════════════════════════════════════

def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.8,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]),
                  respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8))
    return s

SESSION = _build_session()


# ════════════════════════════════════════════════════════════
# CLOCK
# ════════════════════════════════════════════════════════════

def now_est()     -> datetime: return datetime.now(MARKET_TZ)
def market_now()  -> datetime: return now_est()
def display_now() -> datetime: return now_est().astimezone(DISPLAY_TZ)


# ════════════════════════════════════════════════════════════
# JSON I/O — atomic, locked
# ════════════════════════════════════════════════════════════

def load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def save_json(path, data):
    """Non-atomic legacy writer, kept for backward compat (cache files)."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

def _state_update(mutator) -> None:
    """Atomic read-modify-write on STATE_FILE."""
    Path(STATE_FILE).touch(exist_ok=True)
    with open(STATE_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(f.read() or "{}")
            except json.JSONDecodeError:
                logging.warning("STATE_FILE corrupt; resetting")
                state = {}
            mutator(state)
            f.seek(0); f.truncate()
            json.dump(state, f, default=str)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ════════════════════════════════════════════════════════════
# MARKDOWN ESCAPING
# ════════════════════════════════════════════════════════════

_MD_SPECIALS = re.compile(r"([_*`\[])")

def tg_escape(text: Any) -> str:
    if text is None: return "—"
    return _MD_SPECIALS.sub(r"\\\1", str(text))

md = tg_escape  # shorter alias for internal use

_AI_STRIP = re.compile(r"[`*_\[\]()]")

def _sanitize_ai(text: str | None, max_lines: int = 4, max_line_len: int = 140) -> str | None:
    if not text: return None
    cleaned = _AI_STRIP.sub("", text).strip()
    lines = [ln.strip()[:max_line_len] for ln in cleaned.splitlines() if ln.strip()]
    return "\n".join(lines[:max_lines]) or None


# ════════════════════════════════════════════════════════════
# SYMBOLS — yaml with hardcoded fallback
# ════════════════════════════════════════════════════════════

def _load_from_yaml():
    yaml_path = Path(SYMBOLS_YAML)
    if not yaml_path.exists(): return None
    try:
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        emoji_map, sector_map, all_syms = {}, {}, []
        for bucket in ("crypto", "extended_hours", "regular_hours"):
            for item in (raw.get(bucket) or []):
                sym = item["symbol"]
                all_syms.append(sym)
                emoji_map[sym]  = item.get("emoji", "📊")
                sector_map[sym] = item.get("sector", "Other")
        sectors: dict[str, list[str]] = {}
        for sym, sec in sector_map.items():
            sectors.setdefault(sec, []).append(sym)
        return all_syms, sectors, emoji_map
    except Exception as e:
        logging.warning(f"symbols.yaml load failed: {e} — using hardcoded fallback")
        return None

_yaml = _load_from_yaml()
if _yaml:
    MONITOR_LIST, SECTORS, SYMBOL_EMOJI = _yaml
    logging.info(f"market_intel: loaded {len(MONITOR_LIST)} symbols from yaml")
else:
    SECTORS = {
        "AI/Semis":       ["NVDA", "AMD", "MU", "SNDK", "NBIS"],
        "Crypto":         ["BTC-USD", "ETH-USD", "XRP-USD"],
        "Crypto-Adj":     ["IREN"],            # SOFI removed (was duplicate)
        "Quantum":        ["IONQ", "RGTI", "QBTS"],
        "Nuclear/Energy": ["OKLO", "UAMY"],
        "Mega Tech":      ["GOOGL", "MSFT", "META", "AMZN", "AAPL"],
        "EV/Auto":        ["TSLA"],
        "Fintech":        ["SOFI"],
        "Biotech":        ["NVO", "WGRX"],
        "Streaming":      ["NFLX"],
        "Safe Haven":     ["GC=F"],
    }
    MONITOR_LIST = list(dict.fromkeys(s for syms in SECTORS.values() for s in syms))
    SYMBOL_EMOJI = {
        "BTC-USD": "₿", "ETH-USD": "Ξ", "XRP-USD": "◇", "GC=F": "🥇",
        "NVDA": "💎", "TSLA": "🚘", "META": "👓", "AMZN": "📦",
        "GOOGL": "🔍", "MSFT": "🪟", "NFLX": "🎬", "AMD": "⚡", "AAPL": "🍎",
        "MU": "💾", "SNDK": "💽", "NBIS": "🌐",
        "IONQ": "⚛️", "RGTI": "🧪", "QBTS": "🔬",
        "OKLO": "☢️", "IREN": "🪙", "UAMY": "⚒️", "WGRX": "💊",
        "SOFI": "🏦", "NVO": "💉",
    }

# Reverse lookup (PRIMARY sector — first-write wins)
SYMBOL_TO_SECTOR: dict[str, str] = {}
for _sec, _syms in SECTORS.items():
    for _s in _syms:
        SYMBOL_TO_SECTOR.setdefault(_s, _sec)


# ════════════════════════════════════════════════════════════
# PER-SCAN CACHES (in-memory, cleared per scan)
# ════════════════════════════════════════════════════════════

_DAILY_CACHE: dict[str, pd.DataFrame] = {}
_INTRADAY_CACHE: dict[str, pd.DataFrame] = {}
_CACHE_LOCK = Lock()

def clear_caches() -> None:
    with _CACHE_LOCK:
        _DAILY_CACHE.clear()
        _INTRADAY_CACHE.clear()

def _yf_download(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
    """Cached, per-scan yfinance wrapper. Use clear_caches() between scans."""
    key = f"{symbol}|{period}|{interval}"
    with _CACHE_LOCK:
        cached_map = _DAILY_CACHE if interval == "1d" else _INTRADAY_CACHE
        if key in cached_map:
            return cached_map[key]
    for attempt in range(2):
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                if attempt == 0:
                    time.sleep(0.5); continue
                return None
            df = _clean_df(df)
            with _CACHE_LOCK:
                (_DAILY_CACHE if interval == "1d" else _INTRADAY_CACHE)[key] = df
            return df
        except Exception as e:
            logging.debug(f"yf {symbol} {period}/{interval} attempt {attempt+1}: {e}")
            time.sleep(0.5)
    return None


# ════════════════════════════════════════════════════════════
# DATAFRAME / INDICATOR HELPERS
# ════════════════════════════════════════════════════════════

def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0 / length, adjust=False).mean()

def pine_rsi(src: pd.Series, length: int = 14) -> pd.Series:
    delta = src.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    rs = rma(gain, length) / rma(loss, length).replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _last_completed_index(daily: pd.DataFrame) -> int:
    """
    Returns the iloc index of the last *completed* daily bar.
    During market hours yfinance often appends today's partial bar; drop it.
    """
    if daily.empty:
        return -1
    last_dt = daily.index[-1]
    last_date = last_dt.date() if hasattr(last_dt, "date") else last_dt
    today = market_now().date()
    if last_date == today:
        # During RTH the last bar is partial; treat -2 as last completed.
        return len(daily) - 2 if len(daily) >= 2 else -1
    return len(daily) - 1


def ath_recency_label(ath_date_str: str) -> str:
    try:
        ath_dt = datetime.strptime(ath_date_str[:10], "%Y-%m-%d")
        days = (datetime.now() - ath_dt).days
        if days == 0:   return "set TODAY 🔥"
        if days == 1:   return "set YESTERDAY 🔥"
        if days <= 7:   return f"set {days}d ago"
        if days <= 30:  return f"set {days // 7}w ago"
        if days <= 365: return f"set {days // 30}mo ago"
        return f"set {days // 365}y ago"
    except Exception:
        return f"on {ath_date_str}"


# ════════════════════════════════════════════════════════════
# EARNINGS CACHE (12h)
# ════════════════════════════════════════════════════════════

def _earnings_cached(symbol: str):
    cache = load_json(EARNINGS_CACHE_FILE, {})
    rec = cache.get(symbol)
    if not rec:
        return None
    try:
        cached_at = datetime.fromisoformat(rec["cached_at"])
    except Exception:
        return None
    if datetime.now(MARKET_TZ) - cached_at > timedelta(hours=CFG.earnings_cache_ttl_h):
        return None
    ed = rec.get("date")
    days = rec.get("days")
    if isinstance(ed, str):
        try: ed = datetime.fromisoformat(ed).date()
        except Exception: pass
    return ed, days

def _earnings_put(symbol, ed, days):
    cache = load_json(EARNINGS_CACHE_FILE, {})
    cache[symbol] = {
        "date": ed.isoformat() if hasattr(ed, "isoformat") else ed,
        "days": days,
        "cached_at": datetime.now(MARKET_TZ).isoformat(),
    }
    save_json(EARNINGS_CACHE_FILE, cache)


def get_earnings_date(symbol: str):
    """Returns (date, days_until) or (None, None). 12h-cached."""
    if symbol.endswith("-USD") or symbol == "GC=F":
        return None, None
    hit = _earnings_cached(symbol)
    if hit is not None:
        return hit
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is None:
            _earnings_put(symbol, None, None)
            return None, None
        ed = None
        if isinstance(cal, dict):
            v = cal.get("Earnings Date")
            ed = v[0] if isinstance(v, list) and v else v
        elif hasattr(cal, "loc"):
            try:
                if "Earnings Date" in cal.index:
                    ed = cal.loc["Earnings Date"].iloc[0]
            except Exception:
                ed = None
        if ed is None:
            _earnings_put(symbol, None, None); return None, None
        if isinstance(ed, str):
            ed = datetime.fromisoformat(ed.split("T")[0])
        elif hasattr(ed, "to_pydatetime"):
            ed = ed.to_pydatetime()
        if hasattr(ed, "date"):
            ed = ed.date()
        today = market_now().date()
        days_until = (ed - today).days
        if days_until < 0 or days_until > 60:
            _earnings_put(symbol, None, None); return None, None
        _earnings_put(symbol, ed, days_until)
        return ed, days_until
    except Exception as e:
        logging.debug(f"Earnings {symbol}: {e}")
        return None, None


def format_earnings_warning(symbol_unused, earnings_date, days_until):
    """`symbol_unused` retained for backward compat — not referenced."""
    if earnings_date is None: return None
    if days_until <= 0:                       return "🚨 *Earnings TODAY* — extreme volatility risk"
    if days_until == 1:                       return f"⚠️ *Earnings TOMORROW* ({earnings_date}) — SKIP new longs"
    if days_until <= CFG.earnings_warning_days: return f"⚠️ *Earnings in {days_until} days* ({earnings_date}) — consider waiting"
    if days_until <= 7:                       return f"📅 Earnings in {days_until} days ({earnings_date})"
    return None


# ════════════════════════════════════════════════════════════
# MARKET CONTEXT (SPY / QQQ / VIX) — fetched once per scan
# ════════════════════════════════════════════════════════════

def get_market_ctx():
    try:
        out = {}
        for t in ("SPY", "QQQ", "^VIX"):
            df = _yf_download(t, period="5d", interval="1d")
            if df is None or df.empty or len(df) < 2:
                continue
            li = _last_completed_index(df)
            if li < 1:
                continue
            last = float(df["Close"].iloc[li])
            prev = float(df["Close"].iloc[li - 1])
            out[t] = {"price": last, "pct": (last - prev) / prev * 100}
        return out or None
    except Exception as e:
        logging.error(f"Market ctx: {e}")
        return None


# ════════════════════════════════════════════════════════════
# RELATIVE STRENGTH (SPY pre-fetched once)
# ════════════════════════════════════════════════════════════

def calc_relative_strength(ctx, benchmark: str = "SPY", lookback_days: int = 5):
    try:
        df_sym   = _yf_download(ctx["symbol"], period="1mo", interval="1d")
        df_bench = _yf_download(benchmark,    period="1mo", interval="1d")
        if df_sym is None or df_bench is None: return None, None
        if len(df_sym) < lookback_days + 1 or len(df_bench) < lookback_days + 1:
            return None, None
        sym_perf   = (df_sym["Close"].iloc[-1]   / df_sym["Close"].iloc[-(lookback_days + 1)]   - 1) * 100
        bench_perf = (df_bench["Close"].iloc[-1] / df_bench["Close"].iloc[-(lookback_days + 1)] - 1) * 100
        diff = float(sym_perf - bench_perf)
        if   diff > 5:  label = "🟢🟢 Strong Leader"
        elif diff > 2:  label = "🟢 Outperforming"
        elif diff > -2: label = "⚖️ In-line"
        elif diff > -5: label = "🔴 Underperforming"
        else:           label = "🔴🔴 Weak / Laggard"
        return round(diff, 2), label
    except Exception as e:
        logging.debug(f"RS {ctx.get('symbol')}: {e}")
        return None, None


# ════════════════════════════════════════════════════════════
# FULL CONTEXT — single source of indicator truth
# ════════════════════════════════════════════════════════════

def get_full_context(symbol: str) -> dict | None:
    try:
        daily = _yf_download(symbol, period="5y", interval="1d")
        if daily is None or len(daily) < 50: return None

        intraday = _yf_download(symbol, period="2d", interval="5m")
        if intraday is None or intraday.empty: return None

        # ── price snapshot ───────────────────────────────
        current = float(intraday["Close"].iloc[-1])

        li = _last_completed_index(daily)
        if li < 1:  # need at least 2 completed daily bars
            return None
        prev_close = float(daily["Close"].iloc[li - 1])
        last_completed_close = float(daily["Close"].iloc[li])

        # ── today's bars (24h-aware for crypto) ──────────
        is_crypto = symbol.endswith("-USD")
        try:
            if intraday.index.tz is None:
                idx_tz = intraday.tz_localize("UTC").tz_convert(MARKET_TZ)
            else:
                idx_tz = intraday.tz_convert(MARKET_TZ)
        except Exception:
            idx_tz = intraday

        if is_crypto:
            # Last 24h rolling — crypto has no calendar day
            cutoff = idx_tz.index[-1] - pd.Timedelta(hours=24)
            today_bars = idx_tz[idx_tz.index >= cutoff]
        else:
            today_bars = idx_tz[idx_tz.index.date == market_now().date()]
        if today_bars.empty:
            today_bars = idx_tz.iloc[-78:]

        today_open = float(today_bars["Open"].iloc[0])
        today_high = float(today_bars["High"].max())
        today_low  = float(today_bars["Low"].min())
        vol_today  = float(today_bars["Volume"].sum())

        day_change_pct = (current - prev_close) / prev_close * 100 if prev_close else 0.0
        intraday_pct   = (current - today_open) / today_open * 100 if today_open else 0.0

        # ── 52W / ATH (date-windowed, not row-sliced) ────
        cutoff_52w = daily.index[-1] - pd.Timedelta(days=365)
        win = daily[daily.index >= cutoff_52w]
        if len(win) < 20:
            win = daily
        low_52w  = float(win["Low"].min())
        high_52w = float(win["High"].max())
        ath      = float(daily["High"].max())
        ath_idx  = daily["High"].idxmax()

        ath_pct          = (current - ath) / ath * 100
        pct_from_52w_low  = (current - low_52w)  / low_52w  * 100 if low_52w  > 0 else 0
        pct_from_52w_high = (current - high_52w) / high_52w * 100 if high_52w > 0 else 0
        range_pos = ((current - low_52w) / (high_52w - low_52w) * 100) if high_52w > low_52w else 50

        # ── EMAs / RSI on CLOSED candles only ────────────
        closed = daily.iloc[: li + 1]
        ema20  = float(closed["Close"].ewm(span=20,  adjust=False).mean().iloc[-1])
        ema50  = float(closed["Close"].ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200 = float(closed["Close"].ewm(span=200, adjust=False).mean().iloc[-1]) if len(closed) >= 200 else None

        rsi_series = pine_rsi(closed["Close"], 14)
        rsi = float(rsi_series.iloc[-1])
        if np.isnan(rsi): rsi = 50.0

        # Volume avg uses prior 20 *completed* days
        vol_window = closed["Volume"].iloc[-20:]
        vol_avg_20d = float(vol_window.mean()) if len(vol_window) else 0
        vol_ratio = vol_today / vol_avg_20d if vol_avg_20d > 0 else 1.0

        # ── Trend ────────────────────────────────────────
        ema200_for_trend = ema200 if ema200 is not None else ema50
        if   current > ema20 > ema50 > ema200_for_trend:        trend = "🚀 STRONG UPTREND"
        elif current < ema20 < ema50 < ema200_for_trend:        trend = "💀 STRONG DOWNTREND"
        elif current > ema50 > ema200_for_trend:                trend = "📈 UPTREND"
        elif current < ema50 < ema200_for_trend:                trend = "📉 DOWNTREND"
        elif current > ema200_for_trend and current < ema50:    trend = "🔄 PULLBACK IN UPTREND"
        elif current < ema200_for_trend and current > ema50:    trend = "🔀 RECOVERING"
        else:                                                   trend = "⚖️ MIXED"
        if ema200 is None:
            trend += " ⓘ"   # marker: EMA200 unavailable

        return {
            "symbol":          symbol,
            "current":         current,
            "prev_close":      prev_close,
            "last_close":      last_completed_close,
            "today_open":      today_open,
            "today_high":      today_high,
            "today_low":       today_low,
            "day_change_pct":  day_change_pct,
            "intraday_pct":    intraday_pct,
            "ath":             ath,
            "ath_date":        ath_idx.strftime("%Y-%m-%d") if hasattr(ath_idx, "strftime") else str(ath_idx)[:10],
            "ath_pct":         ath_pct,
            "low_52w":         low_52w,
            "high_52w":        high_52w,
            "pct_from_52w_low":  pct_from_52w_low,
            "pct_from_52w_high": pct_from_52w_high,
            "range_pos":       range_pos,
            "ema20":           ema20,
            "ema50":           ema50,
            "ema200":          ema200 if ema200 is not None else ema50,  # legacy field always populated
            "ema200_real":     ema200,   # None if insufficient history
            "rsi":             rsi,
            "vol_ratio":       vol_ratio,
            "trend":           trend,
        }
    except Exception as e:
        logging.error(f"Context {symbol}: {e}")
        return None


# ════════════════════════════════════════════════════════════
# VERDICT ENGINE
# ════════════════════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None, *, earnings_days: int | None = "AUTO"):
    """
    Returns (verdict, zone, [reasons]).
    Pass earnings_days to skip the network call (the brief/scanner already know).
    """
    c = ctx
    rsi, trend = c["rsi"], c["trend"]
    drop, from_ath = c["day_change_pct"], c["ath_pct"]
    range_pos = c["range_pos"]
    above_50  = c["current"] > c["ema50"]
    above_200 = c["ema200_real"] is not None and c["current"] > c["ema200_real"]
    vol_ratio = c["vol_ratio"]

    verdict = zone = None
    reasons: list[str] = []

    # ── 0. PARABOLIC / CRASH ──
    if abs(drop) >= 15:
        if drop > 0:
            verdict, zone = "⚠️ PARABOLIC", f"News/Catalyst Spike +{drop:.0f}%"
            reasons = [
                f"+{drop:.1f}% single-day — likely news/catalyst driven",
                "Parabolic moves mean-revert — high risk to chase",
                f"Volume {vol_ratio:.1f}× avg — {'confirms activity' if vol_ratio > 1.5 else 'weak — possible pump'}",
            ]
        else:
            verdict, zone = "🚨 CRASH", f"Severe Drop {drop:.0f}%"
            reasons = [
                f"{drop:.1f}% single-day — likely news driven",
                "Wait for dust to settle before any entry",
            ]
        return verdict, zone, reasons

    # ── 1. MOMENTUM at ATH ──
    if "UPTREND" in trend and from_ath > -5 and above_50 and above_200 and rsi < 80:
        verdict, zone = "🚀 MOMENTUM", "AT ATH — Continuation"
        reasons = [
            f"At/near all-time high ({from_ath:+.1f}%)",
            "EMA stack fully bullish",
            f"RSI {rsi:.0f} — not overbought, room to run",
        ]
    # ── 2. UPTREND PULLBACK ──
    elif "UPTREND" in trend and rsi < 52 and above_200:
        verdict, zone = "🟢 BUY ZONE", "Pullback in Uptrend"
        reasons = ["Healthy pullback in confirmed uptrend", f"RSI {rsi:.0f} — room to run"]
        if from_ath > -20:
            reasons.append("Near ATH — strong stock pulling back")
    # ── 3. EMA50 PULLBACK ──
    elif "PULLBACK" in trend and rsi < 55:
        verdict, zone = "🟢 BUY ZONE", "EMA50 Pullback"
        reasons = ["Above EMA200 — uptrend structure intact",
                   f"Pulling toward EMA50 ${c['ema50']:.2f}",
                   f"RSI {rsi:.0f} — watch for bounce"]
    # ── 4. EXTENDED NEAR ATH ──
    elif from_ath > -8 and rsi > 75:
        verdict, zone = "🟠 EXTENDED", "Overbought Near ATH"
        reasons = [f"RSI {rsi:.0f} — overbought at highs", "Risk/reward not ideal for new entry"]
    # ── 5. STRONG DOWNTREND ──
    elif "DOWNTREND" in trend and not above_200:
        verdict, zone = "🔴 AVOID", "Falling Knife"
        reasons = ["Below EMA50 & EMA200 — confirmed downtrend", "No base formed"]
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal")
    # ── 6. NEAR 52W LOW ──
    elif c["pct_from_52w_low"] < 8 and drop < -3:
        verdict, zone = "⚠️ CAUTION", "Breaking Down"
        reasons = ["Near 52W low — key support at risk", "Wait for base formation"]
    # ── 7. OVERBOUGHT NON-ATH ──
    elif rsi > 75 and drop > 2:
        verdict, zone = "🟠 TAKE PROFITS", "Extended"
        reasons = [f"RSI overbought ({rsi:.0f})", "Consider trimming, not entering"]
    # ── 8. RECOVERING ──
    elif "RECOVERING" in trend:
        if rsi > 55 and drop > 0:
            verdict, zone = "🟡 WATCH", "Recovery Attempt"
            reasons = ["Reclaiming EMA50 — potential recovery",
                       f"Must clear EMA200 ${c['ema200']:.2f}"]
        else:
            verdict, zone = "⏸️ HOLD", "Below EMA200"
            reasons = ["Below EMA200 — no structural confirmation"]
    # ── 9. MIXED ──
    elif "MIXED" in trend:
        if range_pos < 30 and rsi < 45:
            verdict, zone = "🟡 WATCH", "Potential Accumulation"
            reasons = ["Lower 52W range — possible accumulation", "Wait for trend confirmation"]
        elif rsi > 72:
            verdict, zone = "🟠 EXTENDED", "Overbought in Chop"
            reasons = [f"RSI {rsi:.0f} extended in mixed trend"]
        else:
            verdict, zone = "⏸️ HOLD", "No Edge"
            reasons = ["Mixed signals — wait for clarity"]
    # ── 10. DEFAULT ──
    else:
        if above_50 and above_200 and rsi > 55:
            verdict, zone = "🟡 WATCH", "Building Momentum"
            reasons = ["Above both EMAs — structure improving",
                       f"RSI {rsi:.0f} — momentum building"]
        elif drop < -5:
            verdict, zone = "⚠️ WATCH", "Sharp Drop"
            reasons = ["Large move — wait for stabilisation"]
        else:
            verdict, zone = "⏸️ NEUTRAL", "No Clear Setup"
            reasons = ["No strong directional signal"]

    # ── Market context override ──
    if market_ctx:
        vix     = market_ctx.get("^VIX", {}).get("price", 15)
        spy_pct = market_ctx.get("SPY",  {}).get("pct", 0)
        if vix > 25 and spy_pct < -1.5 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
            verdict = "⚠️ WAIT"
            reasons.insert(0, f"Market bleeding — VIX {vix:.0f}, SPY {spy_pct:.1f}%")

    # ── Earnings override (uses passed-in days if provided) ──
    if any(x in verdict for x in ["BUY", "MOMENTUM", "WATCH"]):
        days = earnings_days
        if days == "AUTO":
            _, days = get_earnings_date(c["symbol"])
        if days is not None and days <= CFG.earnings_warning_days:
            verdict = "⚠️ WAIT — Earnings"
            zone = f"Earnings in {days}d"
            reasons.insert(0, f"Earnings in {days} days — avoid new entries")

    return verdict, zone, reasons


# ════════════════════════════════════════════════════════════
# AI ANALYSIS — non-blocking on rate limit
# ════════════════════════════════════════════════════════════

def ai_analyze_drop(ctx, market_ctx=None) -> str | None:
    if not GEMINI_API_KEY: return None
    c = ctx
    mkt = ""
    if market_ctx:
        mkt = (f"\nMarket: SPY {market_ctx.get('SPY',{}).get('pct',0):+.2f}%, "
               f"QQQ {market_ctx.get('QQQ',{}).get('pct',0):+.2f}%, "
               f"VIX {market_ctx.get('^VIX',{}).get('price',15):.1f}")

    ema200_str = f"${c['ema200_real']:.2f}" if c.get("ema200_real") is not None else "n/a"
    prompt = f"""You are a senior market analyst. Analyze this move in EXACTLY 4 lines (max 110 chars each).

{c['symbol']} — Today: {c['day_change_pct']:+.2f}% | Price: ${c['current']:.2f} | Volume: {c['vol_ratio']:.1f}× avg
52W: Low ${c['low_52w']:.2f} / High ${c['high_52w']:.2f} / ATH ${c['ath']:.2f} ({c['ath_pct']:+.1f}% from ATH)
Trend: {c['trend']} | RSI: {c['rsi']:.0f} | Position in 52W range: {c['range_pos']:.0f}%
EMA50: ${c['ema50']:.2f} | EMA200: {ema200_str}{mkt}

Respond EXACTLY:
📊 [Is this technical, sector-driven, or likely news/catalyst? Be specific]
🎯 [Setup quality — healthy pullback, correction, extended, or bleed?]
⚠️ [Biggest risk — specific price level or condition that invalidates]
💡 [STRONG BUY / BUY / HOLD / AVOID / WAIT] — [one sharp actionable sentence]

4 lines only. No extra text."""

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}")
    try:
        r = SESSION.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 400},
        }, timeout=CFG.ai_timeout_s)
        if r.status_code == 200:
            data = r.json()
            cands = data.get("candidates") or []
            if cands:
                return _sanitize_ai(cands[0]["content"]["parts"][0]["text"])
            logging.warning(f"Gemini empty for {c['symbol']}")
        elif r.status_code == 429:
            logging.warning(f"Gemini rate-limited for {c['symbol']} — skip (no blocking sleep)")
        else:
            logging.error(f"Gemini {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"AI {c['symbol']}: {e}")
    return None


# ════════════════════════════════════════════════════════════
# ALERT FORMATTER
# ════════════════════════════════════════════════════════════

def _vol_descriptor(vol_ratio: float) -> str:
    base = f"{vol_ratio:.1f}× average"
    if vol_ratio >= 2.0: return base + " 🔥 Unusually high"
    if vol_ratio >= 1.5: return base + " ⬆️ Above average"
    if vol_ratio < 0.8:  return base + " ⬇️ Below average"
    return base

def _ath_tag(ath_pct: float) -> str:
    if ath_pct > -5:   return "🏔️ AT/NEAR ATH"
    if ath_pct > -15:  return "📍 Near ATH"
    if ath_pct > -30:  return "📉 Pullback from ATH"
    if ath_pct > -50:  return "💀 Deep drawdown"
    return "⚰️ Far from ATH"

def _rsi_tag(rsi: float) -> str:
    if rsi < 30: return " _(oversold)_"
    if rsi > 70: return " _(overbought)_"
    return " _(neutral)_"

def _ma_status(above_50: bool, above_200: bool) -> str:
    if above_50 and above_200:        return "✅ Above EMA50 & EMA200 (bullish structure)"
    if above_200 and not above_50:    return "⚠️ Below EMA50, above EMA200 (pullback)"
    if not above_200 and above_50:    return "🔀 Above EMA50, below EMA200 (recovery)"
    return "🔴 Below EMA50 & EMA200 (bearish)"


def format_big_move_alert(ctx, verdict, zone, reasons, ai_text, market_ctx,
                          earnings_date=None, days_until=None):
    c = ctx
    em = SYMBOL_EMOJI.get(c["symbol"], "📊")
    drop = c["day_change_pct"]

    if drop <= CFG.big_drop_critical:    head, sev = "🚨🩸", "CRITICAL DROP"
    elif drop <= CFG.big_drop_warn:      head, sev = "⚠️📉", "BIG DROP"
    elif drop >= CFG.big_gain_alert:     head, sev = "🚀📈", "BIG GAIN"
    else: return None

    ts = display_now().strftime("%a %b %d • %I:%M %p ET")
    sign    = "+" if drop >= 0 else ""
    drop_em = "🔴" if drop < 0 else "🟢"

    msg  = f"{head} *{sev}* — {em} *{md(c['symbol'])}*\n"
    msg += f"🕒 {ts}\n{H_RULE}\n"
    msg += f"💵 *Price:* `${c['current']:.2f}` ({drop_em} {sign}{drop:.2f}% today)\n"
    msg += f"📊 *Range:* L `${c['today_low']:.2f}` → H `${c['today_high']:.2f}`\n"
    msg += f"📈 *Volume:* {_vol_descriptor(c['vol_ratio'])}\n"
    if drop >= CFG.big_gain_alert and c["vol_ratio"] < 1.3:
        msg += "⚠️ _Low volume on big gain — thin/news-driven, less reliable_\n"

    # Verdict block
    msg += f"\n*🎯 VERDICT: {md(verdict)}*\n_Zone: {md(zone)}_\n"
    for r in reasons[:3]:
        msg += f"  • {md(r)}\n"

    # Top-line AI bias if present
    if ai_text:
        bias_line = next((l for l in ai_text.splitlines() if "💡" in l), None)
        if bias_line:
            msg += f"\n{md(bias_line)}\n"

    # Positional context
    msg += f"\n*📏 POSITIONAL CONTEXT*\n{SUB_RULE}\n"
    msg += (f"🏔️ *ATH:* `${c['ath']:.2f}` ({c['ath_pct']:+.1f}%) "
            f"{_ath_tag(c['ath_pct'])} — {ath_recency_label(c['ath_date'])}\n")

    pos = max(0, min(10, int(c["range_pos"] / 10)))
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"📊 *52W Range:* `${c['low_52w']:.2f}` → `${c['high_52w']:.2f}`\n"
    msg += f"   `{bar}` {c['range_pos']:.0f}% of range\n"
    msg += (f"   From low: {c['pct_from_52w_low']:+.1f}% • "
            f"From high: {c['pct_from_52w_high']:+.1f}%\n")
    if c["pct_from_52w_low"] > 1000:
        msg += "   ⚠️ _Extreme range — likely corporate action / spin-off_\n"

    # Trend & technicals
    msg += f"\n*📈 TREND & TECHNICALS*\n{SUB_RULE}\n"
    msg += f"Trend: {md(c['trend'])}\n"
    msg += f"RSI (Daily, closed): `{c['rsi']:.0f}`{_rsi_tag(c['rsi'])}\n"
    ema200_disp = f"${c['ema200_real']:.2f}" if c.get("ema200_real") is not None else "_n/a (short history)_"
    msg += f"EMA50: `${c['ema50']:.2f}` • EMA200: {ema200_disp}\n"
    above_50  = c["current"] > c["ema50"]
    above_200 = c.get("ema200_real") is not None and c["current"] > c["ema200_real"]
    msg += f"{_ma_status(above_50, above_200)}\n"

    # Earnings (use passed-in if available — no extra fetch)
    if earnings_date is None and days_until is None:
        earnings_date, days_until = get_earnings_date(c["symbol"])
    warn = format_earnings_warning(c["symbol"], earnings_date, days_until)
    if warn:
        msg += f"\n*📅 EARNINGS*\n{SUB_RULE}\n{warn}\n"

    # Relative strength (cached SPY)
    rs_score, rs_label = calc_relative_strength(c)
    if rs_score is not None:
        sign_rs = "+" if rs_score >= 0 else ""
        msg += f"\n*💪 RELATIVE STRENGTH (5d vs SPY)*\n{SUB_RULE}\n"
        msg += f"{md(rs_label)}: `{sign_rs}{rs_score}%` vs SPY\n"

    # Market
    if market_ctx:
        spy = market_ctx.get("SPY", {}).get("pct", 0)
        vix = market_ctx.get("^VIX", {}).get("price", 15)
        spy_em = "🔴" if spy < 0 else "🟢"
        vix_em = "🔴" if vix > 25 else "🟡" if vix > 18 else "🟢"
        msg += f"\n*🌍 MARKET*\n{SUB_RULE}\n"
        msg += f"SPY: {spy_em} `{spy:+.2f}%` • VIX: {vix_em} `{vix:.1f}`\n"
        if vix > 22:                     msg += "⚠️ _Elevated VIX — broad risk-off_\n"
        elif spy < -1 and drop < -5:     msg += "⚠️ _Moving with market bleed_\n"
        elif spy > 0 and drop < -5:      msg += "🚨 _Stock-specific weakness — market is UP_\n"

    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n{SUB_RULE}\n{md(ai_text)}\n"

    # Entry guidance
    msg += f"\n*💡 ENTRY GUIDANCE*\n{SUB_RULE}\n"
    ema200_safe = c["ema200_real"] if c.get("ema200_real") is not None else c["ema50"]
    if "BUY" in verdict:
        support1 = min(c["ema50"], c["low_52w"] * 1.03)
        msg += f"🟢 *Buy Zone:* `${support1:.2f}` – `${c['current']:.2f}`\n"
        msg += f"🛡️ *Support:* `${ema200_safe:.2f}` (EMA200)\n"
        msg += f"🚪 *Invalidation:* Below `${ema200_safe:.2f}`\n"
    elif "MOMENTUM" in verdict:
        msg += f"🚀 *Breakout entry:* Above ATH `${c['ath']:.2f}` with volume\n"
        msg += f"🔄 *Pullback entry:* Dip to EMA50 `${c['ema50']:.2f}`\n"
        msg += f"🛡️ *Stop:* Below EMA50 `${c['ema50']:.2f}`\n"
    elif "TAKE PROFITS" in verdict or "EXTENDED" in verdict:
        msg += f"🟠 *If holding:* Trim 25–33% here\n"
        msg += f"🔄 *Re-entry zone:* EMA50 `${c['ema50']:.2f}`\n"
        msg += f"🛡️ *Trail stop:* `${c['ema50'] * 0.97:.2f}` (3% below EMA50)\n"
        msg += f"🚫 *Don't add* at these levels\n"
    elif "PARABOLIC" in verdict:
        msg += f"🚫 *Do NOT chase* current levels\n"
        msg += f"⏳ *Wait:* 3–5 day consolidation\n"
        msg += f"🔄 *Re-entry:* First pullback to EMA50 `${c['ema50']:.2f}`\n"
    elif "CRASH" in verdict:
        msg += f"🚫 *Do NOT catch this today*\n"
        msg += f"⏳ *Wait minimum* 3 days for stabilisation\n"
        msg += f"👀 *Watch:* Hold of EMA200 `${ema200_safe:.2f}`?\n"
    elif "AVOID" in verdict or "WAIT" in verdict:
        msg += f"🚫 *Don't enter now*\n"
        msg += f"⏳ *Wait for:* Base above `${ema200_safe:.2f}`\n"
        msg += f"👀 *Trigger:* RSI reversal + reclaim EMA50 `${c['ema50']:.2f}`\n"
    elif "CAUTION" in verdict or "WATCH" in verdict:
        msg += f"👀 *Watch level:* `${c['ema50']:.2f}` (EMA50)\n"
        msg += f"🟡 *Scale-in zone:* `${ema200_safe:.2f}` if holds\n"
    else:
        msg += f"⏸️ *No clear edge* — wait for setup\n"
        msg += f"👀 *Watch:* EMA50 `${c['ema50']:.2f}` for direction\n"

    return msg


# ════════════════════════════════════════════════════════════
# SECTOR BLEED + LEADERSHIP
# ════════════════════════════════════════════════════════════

def check_sector_bleeds(all_contexts: dict[str, dict]):
    out = {}
    for sector, syms in SECTORS.items():
        moves = [(s, all_contexts[s]["day_change_pct"])
                 for s in syms if s in all_contexts and all_contexts[s]]
        if len(moves) < 2: continue
        avg = sum(m[1] for m in moves) / len(moves)
        bleeding = [m for m in moves if m[1] < -2]
        if avg < -2 and len(bleeding) >= max(2, len(moves) // 2):
            out[sector] = {"avg": avg, "bleeding": bleeding, "all": moves}
    return out

def format_sector_bleed_alert(sector_moves):
    if not sector_moves: return None
    ts = display_now().strftime("%I:%M %p ET")
    msg = f"🩸 *SECTOR BLEED DETECTED*\n🕒 {ts}\n{H_RULE}\n"
    for sector, data in sorted(sector_moves.items(), key=lambda x: x[1]["avg"]):
        msg += f"\n🔻 *{md(sector)}* (avg `{data['avg']:+.2f}%`)\n"
        for sym, pct in sorted(data["all"], key=lambda x: x[1]):
            em = SYMBOL_EMOJI.get(sym, "📊")
            pct_em = ("🔴" if pct < -5 else "🟠" if pct < -2
                      else "🟡" if pct < 0 else "🟢")
            msg += f"  {em} {md(sym)}: {pct_em} `{pct:+.2f}%`\n"
    msg += "\n💡 _Avoid longs in bleeding sectors. Wait for stabilization._"
    return msg


def check_leadership(all_contexts, sector_full):
    leaders, laggards = [], []
    for sector, data in sector_full.items():
        s_avg = data["avg"]
        if abs(s_avg) < 1.5: continue
        for sym, pct in data["all"]:
            if SYMBOL_TO_SECTOR.get(sym) != sector: continue
            ctx = all_contexts.get(sym)
            if not ctx: continue
            div = pct - s_avg
            if s_avg < -2 and div > 2:
                leaders.append({"symbol": sym, "ctx": ctx, "sector": sector,
                                "sector_avg": s_avg, "divergence": div})
            elif s_avg > 2 and div < -2:
                laggards.append({"symbol": sym, "ctx": ctx, "sector": sector,
                                 "sector_avg": s_avg, "divergence": div})
    return leaders, laggards

def format_leadership_alert(leaders, laggards):
    if not leaders and not laggards: return None
    ts = display_now().strftime("%I:%M %p ET")
    msg = f"💪 *RELATIVE STRENGTH SIGNALS*\n🕒 {ts}\n{H_RULE}\n"
    if leaders:
        msg += f"\n🏆 *LEADERS* — holding while sector bleeds\n{SUB_RULE}\n"
        for l in sorted(leaders, key=lambda x: -x["divergence"]):
            em = SYMBOL_EMOJI.get(l["symbol"], "📊")
            msg += f"  {em} *{md(l['symbol'])}* ({md(l['sector'])})\n"
            msg += (f"     Stock `{l['ctx']['day_change_pct']:+.2f}%` • "
                    f"Sector `{l['sector_avg']:+.2f}%`\n")
            msg += f"     💪 Outperforming by *{l['divergence']:+.2f}%*\n"
        msg += "\n💡 _Leaders during weakness = future winners._\n"
    if laggards:
        msg += f"\n🔻 *LAGGARDS* — weak vs strong sector\n{SUB_RULE}\n"
        for l in sorted(laggards, key=lambda x: x["divergence"]):
            em = SYMBOL_EMOJI.get(l["symbol"], "📊")
            msg += f"  {em} *{md(l['symbol'])}* ({md(l['sector'])})\n"
            msg += (f"     Stock `{l['ctx']['day_change_pct']:+.2f}%` • "
                    f"Sector `{l['sector_avg']:+.2f}%`\n")
            msg += f"     📉 Underperforming by *{l['divergence']:+.2f}%*\n"
        msg += "\n⚠️ _Laggards in strong sectors = relative weakness._\n"
    return msg


# ════════════════════════════════════════════════════════════
# COOLDOWN — PURE check + atomic write (separate)
# ════════════════════════════════════════════════════════════

def can_alert(key: str, hours: int = COOLDOWN_HOURS) -> bool:
    """PURE check — does NOT write state. Use mark_alert() to record."""
    state = load_json(STATE_FILE, {})
    last = state.get(key)
    if not last: return True
    try:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=MARKET_TZ)
        return (now_est() - dt) >= timedelta(hours=hours)
    except Exception:
        return True

def mark_alert(key: str) -> None:
    """Atomic record-of-fire. Call only after a confirmed Telegram send."""
    iso = now_est().isoformat()
    _state_update(lambda s: s.__setitem__(key, iso))


# ════════════════════════════════════════════════════════════
# TELEGRAM
# ════════════════════════════════════════════════════════════

def _split_for_telegram(msg: str, limit: int = TG_LIMIT) -> list[str]:
    if len(msg) <= limit: return [msg]
    chunks, current = [], ""
    for block in msg.split("\n\n"):
        cand = (current + "\n\n" + block) if current else block
        if len(cand) <= limit:
            current = cand
        else:
            if current: chunks.append(current)
            while len(block) > limit:
                chunks.append(block[:limit]); block = block[limit:]
            current = block
    if current: chunks.append(current)
    if len(chunks) > 1:
        chunks = [f"{c}\n\n_(part {i+1}/{len(chunks)})_" if i < len(chunks) - 1 else c
                  for i, c in enumerate(chunks)]
    return chunks

def _send_single(message: str, silent: bool = False) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram credentials missing"); return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = SESSION.post(url, json={
            "chat_id": CHAT_ID, "text": message,
            "parse_mode": "Markdown", "disable_notification": silent,
        }, timeout=10)
        if r.status_code == 200: return True
        logging.error(f"Telegram {r.status_code}: {r.text[:200]}")
        if "parse" in r.text.lower():
            logging.warning("Retrying without parse_mode")
            r = SESSION.post(url, json={
                "chat_id": CHAT_ID, "text": message,
                "disable_notification": silent,
            }, timeout=10)
            return r.status_code == 200
        return False
    except Exception as e:
        logging.error(f"Telegram send: {e}"); return False

def send_telegram(message: str, silent: bool = False) -> bool:
    ok = True
    for chunk in _split_for_telegram(message):
        if not _send_single(chunk, silent): ok = False
        time.sleep(0.3)
    return ok


# ════════════════════════════════════════════════════════════
# MAIN ORCHESTRATION — parallel, telemetric
# ════════════════════════════════════════════════════════════

def _scan_one(symbol: str) -> tuple[str, dict | None]:
    try:
        return symbol, get_full_context(symbol)
    except Exception as e:
        logging.error(f"scan_one {symbol}: {e}")
        return symbol, None


def run_intel_scan() -> None:
    t0 = time.time()
    print(f"\n🧠 Market Intelligence Scan @ {display_now().strftime('%H:%M ET')}")
    logging.info("Intel scan start")
    clear_caches()

    market_ctx = get_market_ctx()
    all_contexts: dict[str, dict] = {}
    fail = 0; alerts_fired = 0

    with ThreadPoolExecutor(max_workers=CFG.fetch_workers) as ex:
        futures = {ex.submit(_scan_one, s): s for s in MONITOR_LIST}
        for fut in as_completed(futures):
            sym, ctx = fut.result()
            if not ctx:
                print(f"  → {sym:10s} —"); fail += 1; continue
            all_contexts[sym] = ctx
            print(f"  → {sym:10s} {ctx['day_change_pct']:+.2f}%")

    # Big-move alerts (sequential — they're per-symbol and few)
    for sym, ctx in all_contexts.items():
        drop = ctx["day_change_pct"]
        if not (drop <= CFG.big_drop_warn or drop >= CFG.big_gain_alert):
            continue
        cool_key = f"intel_bigmove_{sym}"
        if not can_alert(cool_key, CFG.cooldown_hours):
            print(f"  🔕 {sym} cooldown"); continue

        ed, days = get_earnings_date(sym)
        verdict, zone, reasons = get_verdict(ctx, market_ctx, earnings_days=days)
        ai = ai_analyze_drop(ctx, market_ctx) if abs(drop) >= 5 else None
        msg = format_big_move_alert(ctx, verdict, zone, reasons, ai, market_ctx,
                                    earnings_date=ed, days_until=days)
        if msg and send_telegram(msg, silent=False):
            mark_alert(cool_key)            # ← only after confirmed send
            alerts_fired += 1
            print(f"  🚨 {sym} alert sent")

    if not all_contexts:
        print("\n⚠️ No contexts — skipping aggregate detectors")
        logging.warning("No contexts"); return

    # Sector bleed
    sector_moves = check_sector_bleeds(all_contexts)
    if sector_moves and can_alert("last_sector_bleed", CFG.sector_bleed_cooldown_h):
        msg = format_sector_bleed_alert(sector_moves)
        if msg and send_telegram(msg, silent=False):
            mark_alert("last_sector_bleed")
            alerts_fired += 1
            print("🩸 Sector bleed alert sent")

    # Leadership / laggard
    sector_full = {}
    for sector, syms in SECTORS.items():
        moves = [(s, all_contexts[s]["day_change_pct"])
                 for s in syms if s in all_contexts]
        if len(moves) >= 2:
            sector_full[sector] = {"avg": sum(m[1] for m in moves) / len(moves), "all": moves}
    leaders, laggards = check_leadership(all_contexts, sector_full)
    if (leaders or laggards) and can_alert("last_leadership_alert", CFG.leadership_cooldown_h):
        msg = format_leadership_alert(leaders, laggards)
        if msg and send_telegram(msg, silent=True):
            mark_alert("last_leadership_alert")
            alerts_fired += 1
            print("💪 Leadership alert sent")

    elapsed = time.time() - t0
    print(f"\n✅ Intel scan done — {alerts_fired} alert(s), {fail} fail, "
          f"{len(all_contexts)} ok in {elapsed:.1f}s")
    logging.info(f"Intel scan | alerts={alerts_fired} fail={fail} "
                 f"ok={len(all_contexts)} elapsed={elapsed:.1f}s")


def setup_logging() -> None:
    handler = TimedRotatingFileHandler(LOGS_DIR / "intel.log",
                                       when="midnight", backupCount=14)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


if __name__ == "__main__":
    setup_logging()
    run_intel_scan()
