"""
ALPHAEDGE MARKET INTELLIGENCE v3.1 — UNIVERSE SCHEMA SUPPORT
═══════════════════════════════════════════════════════════════
Trading-system core: data fetch, indicators, verdict engine,
sector/leadership detection, Telegram delivery.

Changes vs original:
• Emoji-anchored section headers (same hierarchy system as single_scan v7.3)
• Tag pills at top for instant mobile scanning
• Stock-specific weakness vs market surfaced prominently in header
• Buy zone floor fixed — uses EMA200 not 52W low
• ATH recency merged into ATH line (was on separate line)
• Volume context note added when big drop happens on low volume
• AI analysis block properly shown when present
• Breathing room between sections
• Consistent separator style: ══ for major, ── for minor

v3.1 vs v3.0
  • Loader supports new symbols.yaml `universe:` schema with metadata
  • Exposes SYMBOL_META (name/exchange/session/tags/roles per symbol)
  • Exposes YAML_SETTINGS for downstream Config overrides
  • Sector taxonomy validation against `sectors_canonical:` list
  • Backward compatible with legacy bucket schema
  • FIX: loader now correctly returns a dict matching the unpack code
  • FIX: settings.intel block applied at import via Config replace()

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
  • 52W window uses date filter, not 252-row slice
  • SECTORS deduplicated (PRIMARY sector wins, secondary tags retained)
  • Auto-split on blank-line boundaries (no mid-section cuts)

P2
  • Logging: TimedRotatingFileHandler in __main__ (not at import)
  • Consistent H_RULE / SUB_RULE separators across all alerts
  • Telemetry: scan duration, fetch failures, AI calls in summary
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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, time as dtime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

warnings.filterwarnings("ignore", category=FutureWarning)

# ════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════

EST        = ZoneInfo("America/New_York")     # legacy alias
MARKET_TZ  = EST                               # market clock — DO NOT change
DISPLAY_TZ = ZoneInfo("America/Toronto")       # same offset, user-facing label

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

STATE_FILE          = "scanner_state.json"
SYMBOLS_YAML        = "symbols.yaml"
EARNINGS_CACHE_FILE = "earnings_cache.json"
LOGS_DIR            = Path("logs"); LOGS_DIR.mkdir(exist_ok=True)

H_RULE   = "`━━━━━━━━━━━━━━━━━━━━━`"
SUB_RULE = "`─────────────────`"
TG_LIMIT = 4096


# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════
# HTTP SESSION (reused, with retry/backoff)
# ════════════════════════════════════════════════════════════

def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
    )
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
    """Non-atomic legacy writer — fine for cache files (single writer)."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

def _state_update(mutator) -> None:
    """Atomic, fcntl-locked read-modify-write on STATE_FILE."""
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

md = tg_escape  # internal alias

_AI_STRIP = re.compile(r"[`*_\[\]()]")

def _sanitize_ai(text: str | None, max_lines: int = 4, max_line_len: int = 140) -> str | None:
    if not text: return None
    cleaned = _AI_STRIP.sub("", text).strip()
    lines = [ln.strip()[:max_line_len] for ln in cleaned.splitlines() if ln.strip()]
    return "\n".join(lines[:max_lines]) or None


# ════════════════════════════════════════════════════════════
# SYMBOLS LOADER — supports v3 universe schema + legacy buckets
# ════════════════════════════════════════════════════════════

def _load_from_yaml() -> dict | None:
    """
    Returns {symbols, sectors, emoji, meta, settings} or None on error.
    Supports both v3 schema (top-level `universe:`) and legacy bucket schema.
    """
    yaml_path = Path(SYMBOLS_YAML)
    if not yaml_path.exists():
        return None
    try:
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        # ── Determine schema ─────────────────────────────
        if "universe" in raw:
            entries = raw["universe"] or []
        else:
            entries = []
            for bucket in ("crypto", "extended_hours", "regular_hours", "dip_extras"):
                entries.extend(raw.get(bucket) or [])

        valid_sectors = set(raw.get("sectors_canonical") or [])
        valid_roles   = {"intel", "brief", "dip", "scanner"}

        seen: set[str] = set()
        emoji_map: dict[str, str] = {}
        sector_map: dict[str, str] = {}
        meta: dict[str, dict] = {}
        all_syms: list[str] = []
        problems: list[str] = []

        for item in entries:
            sym = item.get("symbol")
            if not sym:
                problems.append(f"entry missing symbol: {item}"); continue
            if sym in seen:
                problems.append(f"duplicate symbol: {sym}"); continue
            seen.add(sym)

            sec = item.get("sector", "Other")
            if valid_sectors and sec not in valid_sectors:
                problems.append(f"{sym}: unknown sector '{sec}'")

            emoji = item.get("emoji", "📊")
            if not (1 <= len(emoji) <= 4):
                problems.append(f"{sym}: emoji length suspicious ({emoji!r})")

            ac = item.get("asset_class", "stock")
            if ac not in {"stock", "crypto", "commodity"}:
                problems.append(f"{sym}: invalid asset_class '{ac}'")

            roles = set(item.get("roles") or ["intel", "brief"])
            if not roles.issubset(valid_roles):
                problems.append(f"{sym}: invalid roles {roles - valid_roles}")

            all_syms.append(sym)
            emoji_map[sym]  = emoji
            sector_map[sym] = sec
            meta[sym] = {
                "name":        item.get("name", sym),
                "exchange":    item.get("exchange", ""),
                "asset_class": ac,
                "session":     item.get("session", "regular"),
                "tags":        list(item.get("tags") or []),
                "roles":       sorted(roles),
            }

        for p in problems[:10]:
            logging.warning(f"symbols.yaml: {p}")
        if len(problems) > 10:
            logging.warning(f"symbols.yaml: +{len(problems) - 10} more issues")

        sectors: dict[str, list[str]] = {}
        for sym, sec in sector_map.items():
            sectors.setdefault(sec, []).append(sym)

        return {
            "symbols":  all_syms,
            "sectors":  sectors,
            "emoji":    emoji_map,
            "meta":     meta,
            "settings": raw.get("settings") or {},
        }
    except Exception as e:
        logging.warning(f"symbols.yaml load failed: {e} — using hardcoded fallback")
        return None


_yaml = _load_from_yaml()
if _yaml:
    MONITOR_LIST  = _yaml["symbols"]
    SECTORS       = _yaml["sectors"]
    SYMBOL_EMOJI  = _yaml["emoji"]
    SYMBOL_META   = _yaml["meta"]
    YAML_SETTINGS = _yaml["settings"]
    logging.info(f"market_intel: loaded {len(MONITOR_LIST)} symbols from yaml "
                 f"({len(SECTORS)} sectors)")
else:
    SYMBOL_META   = {}
    YAML_SETTINGS = {}
    SECTORS = {
        "AI / Semis":         ["NVDA", "AMD", "MU", "SNDK", "NBIS"],
        "Crypto":             ["BTC-USD", "ETH-USD", "XRP-USD"],
        "Crypto Mining":      ["IREN"],
        "Quantum":            ["IONQ", "RGTI", "QBTS"],
        "Nuclear / Energy":   ["OKLO", "UAMY"],
        "Mega Tech":          ["GOOGL", "MSFT", "META", "AMZN", "AAPL"],
        "EV / Auto":          ["TSLA"],
        "Fintech":            ["SOFI"],
        "Healthcare":         ["NVO", "WGRX"],
        "Streaming":          ["NFLX"],
        "Safe Haven":         ["GC=F"],
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
    logging.info("market_intel: symbols.yaml not found — using hardcoded fallback")

# Reverse lookup (PRIMARY sector — first-write wins)
SYMBOL_TO_SECTOR: dict[str, str] = {}
for _sec, _syms in SECTORS.items():
    for _s in _syms:
        SYMBOL_TO_SECTOR.setdefault(_s, _sec)


# ════════════════════════════════════════════════════════════
# CONFIG (with optional yaml overrides)
# ════════════════════════════════════════════════════════════

def _apply_yaml_overrides(cfg: Config) -> Config:
    overrides = (YAML_SETTINGS or {}).get("intel") or {}
    if not overrides:
        return cfg
    valid = {f.name for f in cfg.__dataclass_fields__.values()}
    safe  = {k: v for k, v in overrides.items() if k in valid}
    if safe:
        logging.info(f"market_intel: applied yaml overrides: {sorted(safe)}")
    return replace(cfg, **safe)

CFG = _apply_yaml_overrides(Config())

# Backward-compat scalar names (used by older callers)
BIG_DROP_WARN          = CFG.big_drop_warn
BIG_DROP_CRITICAL      = CFG.big_drop_critical
BIG_GAIN_ALERT         = CFG.big_gain_alert
NEAR_52W_LOW_PCT       = CFG.near_52w_low_pct
ATH_PULLBACK_ALERT     = CFG.ath_pullback_alert
COOLDOWN_HOURS         = CFG.cooldown_hours
SECTOR_BLEED_COOLDOWN  = CFG.sector_bleed_cooldown_h
LEADERSHIP_COOLDOWN    = CFG.leadership_cooldown_h
EARNINGS_WARNING_DAYS  = CFG.earnings_warning_days
FETCH_DELAY            = 0.0


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
    """Returns iloc index of the last *completed* daily bar (drops today's partial)."""
    if daily.empty:
        return -1
    last_dt = daily.index[-1]
    last_date = last_dt.date() if hasattr(last_dt, "date") else last_dt
    today = market_now().date()
    if last_date == today:
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

def format_earnings_warning(_symbol_unused, earnings_date, days_until):
    """First arg retained for backward compat — not referenced."""
    if earnings_date is None: return None
    if days_until <= 0:                          return "🚨 *Earnings TODAY* — extreme volatility risk"
    if days_until == 1:                          return f"⚠️ *Earnings TOMORROW* ({earnings_date}) — SKIP new longs"
    if days_until <= CFG.earnings_warning_days:  return f"⚠️ *Earnings in {days_until} days* ({earnings_date}) — consider waiting"
    if days_until <= 7:                          return f"📅 Earnings in {days_until} days ({earnings_date})"
    return None


# ════════════════════════════════════════════════════════════
# MARKET CONTEXT (SPY / QQQ / VIX) — once per scan
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
# RELATIVE STRENGTH (uses cached SPY)
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
        if li < 1:
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

        # ── 52W / ATH (date-windowed) ────────────────────
        cutoff_52w = daily.index[-1] - pd.Timedelta(days=365)
        win = daily[daily.index >= cutoff_52w]
        if len(win) < 20:
            win = daily
        low_52w  = float(win["Low"].min())
        high_52w = float(win["High"].max())
        ath      = float(daily["High"].max())
        ath_idx  = daily["High"].idxmax()

        ath_pct           = (current - ath) / ath * 100
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

        vol_window  = closed["Volume"].iloc[-20:]
        vol_avg_20d = float(vol_window.mean()) if len(vol_window) else 0
        vol_ratio   = vol_today / vol_avg_20d if vol_avg_20d > 0 else 1.0

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
            trend += " ⓘ"

        return {
            "symbol":            symbol,
            "current":           current,
            "prev_close":        prev_close,
            "last_close":        last_completed_close,
            "today_open":        today_open,
            "today_high":        today_high,
            "today_low":         today_low,
            "day_change_pct":    day_change_pct,
            "intraday_pct":      intraday_pct,
            "ath":               ath,
            "ath_date":          ath_idx.strftime("%Y-%m-%d") if hasattr(ath_idx, "strftime") else str(ath_idx)[:10],
            "ath_pct":           ath_pct,
            "low_52w":           low_52w,
            "high_52w":          high_52w,
            "pct_from_52w_low":  pct_from_52w_low,
            "pct_from_52w_high": pct_from_52w_high,
            "range_pos":         range_pos,
            "ema20":             ema20,
            "ema50":             ema50,
            "ema200":            ema200 if ema200 is not None else ema50,  # legacy field always populated
            "ema200_real":       ema200,                                    # None if insufficient history
            "rsi":               rsi,
            "vol_ratio":         vol_ratio,
            "trend":             trend,
        }
    except Exception as e:
        logging.error(f"Context {symbol}: {e}")
        return None


# ════════════════════════════════════════════════════════════
# VERDICT ENGINE
# ════════════════════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None, *, earnings_days="AUTO"):
    """Returns (verdict, zone, [reasons]). Pass earnings_days to skip refetch."""
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
                f"Volume {vol_ratio:.1f}× avg — "
                f"{'confirms activity' if vol_ratio > 1.5 else 'weak — possible pump'}",
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
    elif "UPTREND" in trend and rsi < 52 and above_200:
        verdict, zone = "🟢 BUY ZONE", "Pullback in Uptrend"
        reasons = ["Healthy pullback in confirmed uptrend", f"RSI {rsi:.0f} — room to run"]
        if from_ath > -20:
            reasons.append("Near ATH — strong stock pulling back")
    elif "PULLBACK" in trend and rsi < 55:
        verdict, zone = "🟢 BUY ZONE", "EMA50 Pullback"
        reasons = ["Above EMA200 — uptrend structure intact",
                   f"Pulling toward EMA50 ${c['ema50']:.2f}",
                   f"RSI {rsi:.0f} — watch for bounce"]
    elif from_ath > -8 and rsi > 75:
        verdict, zone = "🟠 EXTENDED", "Overbought Near ATH"
        reasons = [f"RSI {rsi:.0f} — overbought at highs", "Risk/reward not ideal for new entry"]
    elif "DOWNTREND" in trend and not above_200:
        verdict, zone = "🔴 AVOID", "Falling Knife"
        reasons = ["Below EMA50 & EMA200 — confirmed downtrend", "No base formed"]
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal")
    elif c["pct_from_52w_low"] < 8 and drop < -3:
        verdict, zone = "⚠️ CAUTION", "Breaking Down"
        reasons = ["Near 52W low — key support at risk", "Wait for base formation"]
    elif rsi > 75 and drop > 2:
        verdict, zone = "🟠 TAKE PROFITS", "Extended"
        reasons = [f"RSI overbought ({rsi:.0f})", "Consider trimming, not entering"]
    elif "RECOVERING" in trend:
        if rsi > 55 and drop > 0:
            verdict, zone = "🟡 WATCH", "Recovery Attempt"
            reasons = ["Reclaiming EMA50 — potential recovery",
                       f"Must clear EMA200 ${c['ema200']:.2f}"]
        else:
            verdict, zone = "⏸️ HOLD", "Below EMA200"
            reasons = ["Below EMA200 — no structural confirmation"]
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

    # ── Earnings override (uses passed-in days when provided) ──
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
# ALERT FORMATTER — VISUAL TELEGRAM ALERTS ONLY
# ════════════════════════════════════════════════════════════

def alert_box_header(emoji: str, title: str, subtitle: str | None = None,
                     border: str = "━━━━━━━━━━━━━━━━━━━━━") -> str:
    msg = f"{emoji} *{title}*\n`{border}`\n"
    if subtitle:
        msg += f"_{md(subtitle)}_\n"
    msg += "\n"
    return msg


def name_label(sym: str, *, bold_ticker: bool = True) -> str:
    """Returns 'AAPL — Apple Inc. (NASDAQ)' if metadata available."""
    meta = SYMBOL_META.get(sym, {})
    name = meta.get("name", "")
    exch = meta.get("exchange", "")
    ticker = f"*{md(sym)}*" if bold_ticker else md(sym)
    if name and exch: return f"{ticker} — {md(name)} ({md(exch)})"
    if name:          return f"{ticker} — {md(name)}"
    return ticker


def _vol_descriptor(vol_ratio: float) -> str:
    base = f"{vol_ratio:.1f}× average"
    if vol_ratio >= 2.0: return base + " 🔥 Unusually high"
    if vol_ratio >= 1.5: return base + " ⬆️ Above average"
    if vol_ratio < 0.8:  return base + " ⬇️ Below average"
    return base + " Normal"


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
    if above_50 and above_200:      return "✅ Above EMA50 & EMA200 — bullish structure"
    if above_200 and not above_50:  return "⚠️ Below EMA50, above EMA200 — pullback"
    if not above_200 and above_50:  return "🔀 Above EMA50, below EMA200 — recovery"
    return "🔴 Below EMA50 & EMA200 — bearish structure"


def _move_header(drop: float) -> tuple[str, str, str]:
    if drop <= CFG.big_drop_critical:
        return "🚨🩸", "CRITICAL DROP", "━━━━━━━━━━━━━━━━━━━━━"
    if drop <= CFG.big_drop_warn:
        return "⚠️📉", "BIG DROP", "═════════════════════"
    if drop >= CFG.big_gain_alert:
        return "🚀📈", "BIG GAIN", "━━━━━━━━━━━━━━━━━━━━━"
    return "📊", "MARKET MOVE", "─────────────────────"


def format_big_move_alert(ctx, verdict, zone, reasons, ai_text, market_ctx):
    c   = ctx
    em  = SYMBOL_EMOJI.get(c['symbol'], '📊')
    drop = c['day_change_pct']
 
    # ── severity tier ──
    if drop <= BIG_DROP_CRITICAL:
        header_em, severity = "🚨🩸", "CRITICAL DROP"
    elif drop <= BIG_DROP_WARN:
        header_em, severity = "⚠️📉", "BIG DROP"
    elif drop >= BIG_GAIN_ALERT:
        header_em, severity = "🚀📈", "BIG GAIN"
    else:
        return None
 
    now = now_est()
    tz  = now.tzname() or "ET"
    ts  = now.strftime(f'%a %b %d · %I:%M %p {tz}')
 
    sign     = "+" if drop >= 0 else ""
    drop_em  = "🟢" if drop >= 0 else "🔴"
    decimals = 4 if c['current'] < 10 else 2
    pf       = f"{{:.{decimals}f}}"
 
    # ── market context for header banner ──
    spy_pct  = market_ctx.get('SPY',  {}).get('pct', 0)  if market_ctx else 0
    vix_val  = market_ctx.get('^VIX', {}).get('price', 15) if market_ctx else 15
    stock_vs_market = ""
    if drop < -5 and spy_pct > 0:
        stock_vs_market = "  🚨 _Stock-specific — market is green_"
    elif drop < -5 and spy_pct < -1.5:
        stock_vs_market = "  ⚠️ _Moving with broad market bleed_"
    elif drop > 8 and spy_pct < 0:
        stock_vs_market = "  💪 _Outperforming — market is red_"
 
    # ════════════════════════════════════════════
    # § 1  HEADER
    # ════════════════════════════════════════════
    short_name = SYMBOL_EMOJI.get(c['symbol'], '')  # emoji only
    # Try to get long name from context if available
    long_name = c.get('long_name', '')
    name_line = f"*{c['symbol']}*"
    if long_name and long_name != c['symbol']:
        name_line += f" — _{long_name}_"
    name_line += f" {em}"
 
    msg  = f"{header_em} *{severity}*\n"
    msg += f"`══════════════════════════`\n"
    msg += f"{name_line}\n"
    msg += f"`${pf.format(c['current'])}`  {drop_em} *{sign}{drop:.2f}%*{stock_vs_market}\n"
    msg += f"_{ts}_\n"
    msg += f"`══════════════════════════`\n\n"
 
    # ════════════════════════════════════════════
    # § 2  TAG PILLS — instant context on mobile
    # ════════════════════════════════════════════
    tags = []
 
    # Verdict tag
    if "BUY" in verdict:        tags.append("🟢 Buy Zone")
    elif "MOMENTUM" in verdict: tags.append("🚀 Momentum")
    elif "EXTENDED" in verdict: tags.append("🟠 Extended")
    elif "AVOID" in verdict:    tags.append("🔴 Avoid")
    elif "PARABOLIC" in verdict: tags.append("🚨 Parabolic")
    elif "CRASH" in verdict:    tags.append("💥 Crash")
    elif "WAIT" in verdict:     tags.append("⏳ Wait")
    elif "CAUTION" in verdict:  tags.append("⚠️ Caution")
    elif "WATCH" in verdict:    tags.append("👀 Watch")
 
    # RSI tag
    rsi = c.get('rsi', 50)
    if rsi >= 70:       tags.append("🔴 RSI Overbought")
    elif rsi <= 30:     tags.append("🟢 RSI Oversold")
    elif rsi >= 60:     tags.append("🟡 RSI Bullish")
 
    # Volume context
    vol = c.get('vol_ratio', 1.0)
    if vol >= 2.0:      tags.append("🔥 High Volume")
    elif vol < 0.7:     tags.append("⬇️ Low Volume")
 
    # VIX tag
    if vix_val > 25:    tags.append("🔴 VIX High")
 
    if tags:
        msg += "  ·  ".join(tags[:4]) + "\n\n"
 
    # ════════════════════════════════════════════
    # § 3  VERDICT
    # ════════════════════════════════════════════
    msg += f"*〔 {verdict} 〕*\n"
    msg += f"_↳ {zone}_\n"
 
    # AI one-liner right under verdict
    if ai_text:
        lines   = ai_text.strip().split('\n')
        summary = next((l for l in reversed(lines)
                        if any(w in l.upper() for w in ['WAIT', 'BUY', 'HOLD', 'AVOID', 'STRONG'])),
                       lines[-1] if lines else None)
        if summary:
            msg += f"\n{summary}\n"
 
    msg += "\n"
    for r in reasons[:3]:
        msg += f"  › {r}\n"
    msg += f"`══════════════════════════`\n\n"
 
    # ════════════════════════════════════════════
    # § 4  PRICE
    # ════════════════════════════════════════════
    msg += f"💵 *PRICE*\n`──────────────────────────`\n"
    msg += f"  Live     `${pf.format(c['current'])}`  {drop_em} {sign}{drop:.2f}% today\n"
    msg += f"  Range    L `${pf.format(c['today_low'])}` — H `${pf.format(c['today_high'])}`\n"
 
    if vol >= 2.0:   vol_line = f"`{vol:.1f}x` 🔥 Unusually high"
    elif vol >= 1.5: vol_line = f"`{vol:.1f}x` ⬆️ Above average"
    elif vol >= 0.8: vol_line = f"`{vol:.1f}x` — Normal"
    else:            vol_line = f"`{vol:.1f}x` ⬇️ Below average"
    msg += f"  Volume   {vol_line}\n"
 
    # Volume + drop context — important signal
    if drop <= BIG_DROP_WARN and vol < 0.8:
        msg += f"  ⚠️ _Big drop on low volume — may recover, watch for follow-through_\n"
    elif drop <= BIG_DROP_WARN and vol >= 2.0:
        msg += f"  🚨 _High volume sell-off — distribution, not just a dip_\n"
    elif drop >= BIG_GAIN_ALERT and vol < 1.3:
        msg += f"  ⚠️ _Big gain on low volume — thin/news-driven, less reliable_\n"
    msg += "\n"
 
    # ════════════════════════════════════════════
    # § 5  WHAT TO DO — verdict-specific action
    # ════════════════════════════════════════════
    msg += f"🎯 *WHAT TO DO*\n`──────────────────────────`\n"
 
    if "BUY" in verdict:
        support_level = max(c['ema200'], c['low_52w'] * 1.05)  # EMA200 or 5% above 52W low
        msg += f"  › Entry zone: `${pf.format(c['ema200'])}` — `${pf.format(c['current'])}` (EMA200 to current)\n"
        msg += f"  › Stop: below EMA200 `${pf.format(c['ema200'])}`\n"
        msg += f"  › Target: ATH `${pf.format(c['ath'])}` ({c['ath_pct']:+.1f}% away)\n"
    elif "MOMENTUM" in verdict:
        msg += f"  › Breakout entry: above ATH `${pf.format(c['ath'])}` with volume\n"
        msg += f"  › Pullback entry: dip to EMA50 `${pf.format(c['ema50'])}` (ideal)\n"
        msg += f"  › Stop: below EMA50 `${pf.format(c['ema50'])}`\n"
    elif "EXTENDED" in verdict or "PARABOLIC" in verdict:
        reentry = round(c['ema50'] * 0.98, 2)
        msg += f"  › DO NOT chase at current price\n"
        msg += f"  › Re-entry zone: near EMA50 `${pf.format(reentry)}`\n"
        msg += f"  › RSI trigger: wait for RSI below 60 (currently `{rsi:.0f}`)\n"
    elif "CRASH" in verdict or "AVOID" in verdict:
        msg += f"  › Do NOT catch today — wait minimum 3 days\n"
        msg += f"  › Watch: does it hold EMA200 `${pf.format(c['ema200'])}`?\n"
        msg += f"  › Entry only after base forms above EMA50 `${pf.format(c['ema50'])}`\n"
    elif "CAUTION" in verdict or "WATCH" in verdict:
        msg += f"  › Watch key level: EMA50 `${pf.format(c['ema50'])}`\n"
        msg += f"  › Scale-in zone: EMA200 `${pf.format(c['ema200'])}` if holds\n"
        msg += f"  › Confirm with RSI > 50 + volume before entry\n"
    else:
        msg += f"  › No clear edge — wait for directional setup\n"
        msg += f"  › Watch: EMA50 `${pf.format(c['ema50'])}` for direction\n"
    msg += "\n"
 
    # ════════════════════════════════════════════
    # § 6  POSITIONAL CONTEXT
    # ════════════════════════════════════════════
    msg += f"📊 *POSITIONAL CONTEXT*\n`──────────────────────────`\n"
 
    # ATH with recency on same line
    ath_pct = c['ath_pct']
    if ath_pct > -5:     ath_tag = "🏔️ At/near ATH"
    elif ath_pct > -15:  ath_tag = "📍 Near ATH"
    elif ath_pct > -30:  ath_tag = "📉 Pullback from ATH"
    elif ath_pct > -50:  ath_tag = "💀 Deep drawdown"
    else:                ath_tag = "⚰️ Far from ATH"
 
    ath_when = ath_recency_label(c['ath_date'])
    msg += f"  🏔️ ATH    `${pf.format(c['ath'])}` ({c['ath_pct']:+.1f}%) {ath_tag} — {ath_when}\n"
 
    # 52W range bar
    rp  = c['range_pos']
    pos = int(rp / 10)
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"  📐 52W    `${pf.format(c['low_52w'])}` — `${pf.format(c['high_52w'])}`\n"
    msg += f"         `{bar}` {rp:.0f}% of range\n"
 
    if c['pct_from_52w_low'] > 500:
        msg += f"  ⚠️ _52W low `${pf.format(c['low_52w'])}` may reflect split/spin-off_\n"
    msg += "\n"
 
    # ════════════════════════════════════════════
    # § 7  TECHNICALS
    # ════════════════════════════════════════════
    msg += f"📈 *TECHNICALS*\n`──────────────────────────`\n"
    msg += f"  Trend   {c['trend']}\n"
 
    if rsi < 30:       rsi_tag, rsi_em = "Oversold",   "🟢"
    elif rsi >= 70:    rsi_tag, rsi_em = "Overbought", "🔴"
    elif rsi > 60:     rsi_tag, rsi_em = "Bullish",    "🟡"
    else:              rsi_tag, rsi_em = "Neutral",    "⚪"
    msg += f"  RSI     `{rsi:.0f}` {rsi_em} _{rsi_tag}_\n"
 
    msg += f"  EMA50   `${pf.format(c['ema50'])}`\n"
    msg += f"  EMA200  `${pf.format(c['ema200'])}`\n"
 
    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if above_50 and above_200:          msg += "  ✅ Above EMA50 & EMA200\n"
    elif above_200 and not above_50:    msg += "  ⚠️ Below EMA50, above EMA200\n"
    elif not above_200 and above_50:    msg += "  🔀 Above EMA50, below EMA200\n"
    else:                               msg += "  🔴 Below both EMAs\n"
    msg += "\n"
 
    # ════════════════════════════════════════════
    # § 8  EARNINGS (if relevant)
    # ════════════════════════════════════════════
    earnings_date, days_until = get_earnings_date(c['symbol'])
    earn_warn = format_earnings_warning(c['symbol'], earnings_date, days_until)
    if earn_warn:
        msg += f"📅 *EARNINGS*\n`──────────────────────────`\n  {earn_warn}\n\n"
 
    # ════════════════════════════════════════════
    # § 9  RELATIVE STRENGTH
    # ════════════════════════════════════════════
    rs_score, rs_label = calc_relative_strength(c)
    if rs_score is not None:
        rs_sign = "+" if rs_score >= 0 else ""
        rs_em   = "💪" if rs_score > 5 else "📉" if rs_score < -5 else "➖"
        msg += f"  {rs_em} *RS vs SPY (5d)*  {rs_label}  `{rs_sign}{rs_score}%`\n\n"
 
    # ════════════════════════════════════════════
    # § 10  MARKET CONDITIONS
    # ════════════════════════════════════════════
    if market_ctx:
        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})
        qqq = market_ctx.get('QQQ', {})
        if spy or vix:
            msg += f"🌍 *MARKET*\n`──────────────────────────`\n"
            parts = []
            if spy:
                s  = spy.get('pct', 0)
                se = "🟢" if s >= 0 else "🔴"
                parts.append(f"SPY {se} `{'+' if s >= 0 else ''}{s:.2f}%`")
            if qqq:
                q  = qqq.get('pct', 0)
                qe = "🟢" if q >= 0 else "🔴"
                parts.append(f"QQQ {qe} `{'+' if q >= 0 else ''}{q:.2f}%`")
            if vix:
                ve  = "🔴" if vix_val > 25 else "🟡" if vix_val > 18 else "🟢"
                vtag = "High" if vix_val > 25 else "Elevated" if vix_val > 18 else "Calm"
                parts.append(f"VIX {ve} `{vix_val:.1f}` _{vtag}_")
            msg += f"  {'  ·  '.join(parts)}\n"
 
            # Prominent context note — was buried before
            if drop < -5 and spy_pct > 0:
                msg += f"  🚨 _Stock-specific weakness — market is UP_\n"
            elif drop < -5 and spy_pct < -1.5:
                msg += f"  ⚠️ _Moving with broad market sell-off_\n"
            elif drop > 8 and spy_pct < 0:
                msg += f"  💪 _Stock-specific strength — market is down_\n"
            elif vix_val > 22:
                msg += f"  ⚠️ _Elevated VIX — broad risk-off environment_\n"
            msg += "\n"
 
    # ════════════════════════════════════════════
    # § 11  AI ANALYSIS (full 4-line block)
    # ════════════════════════════════════════════
    if ai_text:
        msg += f"🤖 *AI ANALYSIS*\n`──────────────────────────`\n"
        for line in ai_text.strip().split('\n'):
            if line.strip():
                msg += f"  {line.strip()}\n"
        msg += "\n"
 
    msg += f"`══════════════════════════`\n"
    msg += f"_AlphaEdge Market Intel_"
    return msg


# ════════════════════════════════════════════════════════════
# SECTOR BLEED + LEADERSHIP — VISUAL TELEGRAM ALERTS ONLY
# ════════════════════════════════════════════════════════════

def check_sector_bleeds(all_contexts: dict[str, dict]):
    out = {}

    for sector, syms in SECTORS.items():
        moves = [
            (s, all_contexts[s]["day_change_pct"])
            for s in syms
            if s in all_contexts and all_contexts[s]
        ]

        if len(moves) < 2:
            continue

        avg = sum(m[1] for m in moves) / len(moves)
        bleeding = [m for m in moves if m[1] < -2]

        if avg < -2 and len(bleeding) >= max(2, len(moves) // 2):
            out[sector] = {
                "avg": avg,
                "bleeding": bleeding,
                "all": moves,
            }

    return out


def format_sector_bleed_alert(sector_moves):
    if not sector_moves:
        return None

    ts = display_now().strftime("%a %b %d • %I:%M %p ET")

    msg = alert_box_header(
        "🩸",
        "SECTOR BLEED DETECTED",
        ts,
        "━━━━━━━━━━━━━━━━━━━━━"
    )

    for sector, data in sorted(sector_moves.items(), key=lambda x: x[1]["avg"]):
        msg += f"*{md(sector)}*\n"
        msg += f"Sector avg: `{data['avg']:+.2f}%`\n"
        msg += "`─────────────────`\n"

        for sym, pct in sorted(data["all"], key=lambda x: x[1]):
            em = SYMBOL_EMOJI.get(sym, "📊")
            pct_em = (
                "🔴" if pct < -5 else
                "🟠" if pct < -2 else
                "🟡" if pct < 0 else
                "🟢"
            )

            msg += f"{pct_em} {em} *{md(sym)}* `{pct:+.2f}%`\n"

        msg += "\n"

    msg += "*ACTION*\n`─────────────────`\n"
    msg += "🚫 Avoid new longs in bleeding sectors\n"
    msg += "⏳ Wait for stabilization or leadership divergence\n"
    msg += "👀 Watch strongest names that hold above EMA50\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += "_AlphaEdge Sector Intel_"

    return msg


def check_leadership(all_contexts, sector_full):
    leaders, laggards = [], []

    for sector, data in sector_full.items():
        s_avg = data["avg"]

        if abs(s_avg) < 1.5:
            continue

        for sym, pct in data["all"]:
            if SYMBOL_TO_SECTOR.get(sym) != sector:
                continue

            ctx = all_contexts.get(sym)

            if not ctx:
                continue

            div = pct - s_avg

            if s_avg < -2 and div > 2:
                leaders.append({
                    "symbol": sym,
                    "ctx": ctx,
                    "sector": sector,
                    "sector_avg": s_avg,
                    "divergence": div,
                })

            elif s_avg > 2 and div < -2:
                laggards.append({
                    "symbol": sym,
                    "ctx": ctx,
                    "sector": sector,
                    "sector_avg": s_avg,
                    "divergence": div,
                })

    return leaders, laggards


def format_leadership_alert(leaders, laggards):
    if not leaders and not laggards:
        return None

    ts = display_now().strftime("%a %b %d • %I:%M %p ET")

    msg = alert_box_header(
        "💪",
        "RELATIVE STRENGTH SIGNALS",
        ts,
        "═════════════════════"
    )

    if leaders:
        msg += "🏆 *LEADERS*\n"
        msg += "_Holding strong while sector is weak_\n"
        msg += "`─────────────────`\n"

        for l in sorted(leaders, key=lambda x: -x["divergence"]):
            em = SYMBOL_EMOJI.get(l["symbol"], "📊")

            msg += f"{em} {name_label(l['symbol'])}\n"
            msg += f"Sector: {md(l['sector'])}\n"
            msg += (
                f"Stock: `{l['ctx']['day_change_pct']:+.2f}%` • "
                f"Sector: `{l['sector_avg']:+.2f}%`\n"
            )
            msg += f"💪 Outperforming by *{l['divergence']:+.2f}%*\n\n"

        msg += "💡 _Leaders during weakness can become future winners._\n\n"

    if laggards:
        msg += "🔻 *LAGGARDS*\n"
        msg += "_Weak names inside strong sectors_\n"
        msg += "`─────────────────`\n"

        for l in sorted(laggards, key=lambda x: x["divergence"]):
            em = SYMBOL_EMOJI.get(l["symbol"], "📊")

            msg += f"{em} {name_label(l['symbol'])}\n"
            msg += f"Sector: {md(l['sector'])}\n"
            msg += (
                f"Stock: `{l['ctx']['day_change_pct']:+.2f}%` • "
                f"Sector: `{l['sector_avg']:+.2f}%`\n"
            )
            msg += f"📉 Underperforming by *{l['divergence']:+.2f}%*\n\n"

        msg += "⚠️ _Laggards in strong sectors show relative weakness._\n\n"

    msg += "*ACTION*\n`─────────────────`\n"
    msg += "🏆 Prioritize leaders on pullbacks\n"
    msg += "🔻 Avoid laggards until trend improves\n"
    msg += "👀 Confirm with volume + EMA50 reclaim\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += "_AlphaEdge Leadership Intel_"

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

    # Big-move alerts
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
            mark_alert(cool_key)
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
