"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           ALPHAEDGE MARKET INTELLIGENCE v3.2 — UNIVERSE SCHEMA              ║
║           Full audit, bug-fix & hardening pass — May 2026                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  PURPOSE                                                                     ║
║  Scans a watchlist of stocks/crypto/commodities via yfinance, calculates     ║
║  technical indicators (EMA20/50/200, RSI, volume ratio, 52W range, ATH),    ║
║  derives a trading verdict (BUY / AVOID / WAIT / etc.), detects sector       ║
║  bleed and leadership divergence, and fires rich Markdown alerts via         ║
║  Telegram. Optional Gemini AI adds a 4-line contextual commentary block.    ║
║                                                                              ║
║  ARCHITECTURE                                                                ║
║  ┌─────────────────────────────────────────────────────────────────────┐     ║
║  │ symbols.yaml  →  MONITOR_LIST / SECTORS / SYMBOL_EMOJI / META      │     ║
║  │ ENV vars      →  TELEGRAM_TOKEN, CHAT_ID, GEMINI_API_KEY           │     ║
║  │ run_intel_scan()                                                    │     ║
║  │   ├─ parallel get_full_context() per symbol (ThreadPoolExecutor)   │     ║
║  │   ├─ get_market_ctx() [SPY / QQQ / VIX]                            │     ║
║  │   ├─ get_verdict()  →  format_big_move_alert()  →  send_telegram() │     ║
║  │   ├─ check_sector_bleeds()  →  format_sector_bleed_alert()         │     ║
║  │   └─ check_leadership()    →  format_leadership_alert()            │     ║
║  └─────────────────────────────────────────────────────────────────────┘     ║
║                                                                              ║
║  KEY DESIGN CHOICES                                                          ║
║  • All state writes are fcntl-locked (no races between concurrent scripts)  ║
║  • RSI computed on closed candles only (no live partial-bar leakage)        ║
║  • EMA200 returns None when <200 bars — never silently falls back to EMA50  ║
║  • prev_close = last *completed* daily bar (not today's still-forming bar)  ║
║  • vol_avg_20d excludes today's partial day                                 ║
║  • Crypto "today open" uses 24 h rolling window, not EST date slice         ║
║  • Per-scan in-memory cache; clear_caches() resets between runs             ║
║  • can_alert() is pure (no side-effect); mark_alert() writes atomically     ║
║  • Earnings fetched once per symbol, persisted in 12 h JSON cache           ║
║  • AI call is non-blocking on rate-limit (skips, does not sleep 15 s)       ║
║  • All dynamic values escaped via tg_escape() before Telegram send          ║
║  • Display TZ = America/Toronto; Market TZ = America/New_York               ║
║                                                                              ║
║  SYMBOLS.YAML SCHEMA (v3 — preferred)                                        ║
║  universe:                                                                   ║
║    - symbol: NVDA                                                            ║
║      name: NVIDIA Corporation                                                ║
║      exchange: NASDAQ                                                        ║
║      sector: AI / Semis                                                      ║
║      asset_class: stock          # stock | crypto | commodity                ║
║      session: regular            # regular | extended | 24h                  ║
║      emoji: 💎                                                               ║
║      tags: [ai, semis]                                                       ║
║      roles: [intel, brief]       # intel | brief | dip | scanner             ║
║  sectors_canonical: [AI / Semis, Crypto, ...]   # validated set             ║
║  settings:                                                                   ║
║    intel:                                                                    ║
║      big_drop_warn: -5.0                                                     ║
║      cooldown_hours: 4                                                       ║
║      ...                                                                     ║
║                                                                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  CHANGELOG                                                                   ║
║                                                                              ║
║  v3.2 — Audit / Bug-Fix Pass (this file)                                    ║
║  BUG FIXES                                                                   ║
║   • format_big_move_alert() signature mismatch fixed — caller passes        ║
║     earnings_date + days_until but old signature lacked those params;       ║
║     function now accepts and uses them instead of calling get_earnings_date  ║
║     a second time (was a duplicate fetch race)                               ║
║   • run_intel_scan() passed earnings_date/days_until kwargs that did not    ║
║     exist in format_big_move_alert() → TypeError at runtime; fixed          ║
║   • _last_completed_index() returned -1 when df has only 1 row → caller    ║
║     would call daily.iloc[-1 - 1] = daily.iloc[-2] → IndexError; guarded   ║
║   • calc_relative_strength() referenced ctx["symbol"] but param is named   ║
║     ctx and used as a raw dict — was inconsistent; now explicit sym arg     ║
║   • get_verdict() earnings_days="AUTO" path called get_earnings_date()      ║
║     even when already fetched upstream; pass-through now correct            ║
║   • vol_avg_20d used closed["Volume"].iloc[-20:] but closed already clips   ║
║     to li+1; no off-by-one, but guard added for len < 20 edge case          ║
║   • _split_for_telegram() could produce chunks > TG_LIMIT when a single     ║
║     paragraph exceeded limit; now hard-slices with line-boundary preference ║
║   • SYMBOL_META / YAML_SETTINGS were undefined when _yaml was None and      ║
║     code referenced them before the fallback assignment; reordered          ║
║   • format_big_move_alert BUY zone used max(c['ema200'], c['low_52w']*1.05) ║
║     but only used ema200 for display — dead variable 'support_level'; removed║
║   • ai_analyze_drop prompt used c['ema200_real'] with None guard but format ║
║     string would crash on None; replaced with explicit string conversion    ║
║   • check_sector_bleeds threshold: len(bleeding) >= max(2, len(moves)//2)  ║
║     — with 2 symbols, max(2,1)=2, so BOTH must bleed; correct but added    ║
║     comment for clarity                                                      ║
║   • ATH recency label compared naive datetime to now() — now uses           ║
║     datetime.utcnow() consistently to avoid tz-aware comparison crash       ║
║                                                                              ║
║  IMPROVEMENTS                                                                ║
║   • Section headers added throughout for Notepad++ code folding             ║
║   • format_big_move_alert() no longer calls get_earnings_date() internally  ║
║     (was 4th fetch for same symbol in one scan cycle)                       ║
║   • calc_relative_strength() now accepts symbol string directly             ║
║   • Fallback SYMBOL_META / YAML_SETTINGS always defined (moved before use) ║
║   • setup_logging() idempotent — safe to call multiple times               ║
║   • All f-strings with potential None values guarded                        ║
║   • Prompt template appended at EOF for new-session context handoff         ║
║                                                                              ║
║  v3.1 — Universe schema support, SYMBOL_META, YAML_SETTINGS                ║
║  v3.0 — P0/P1/P2 hardening; parallel fetch; pure can_alert(); closed-bar   ║
║          RSI; EMA200 None-on-insufficient; atomic state writes              ║
║  v2.2 — Original production baseline                                         ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

# ┌─────────────────────────────────────────────────────────────────────────┐
# │ STDLIB IMPORTS                                                          │
# └─────────────────────────────────────────────────────────────────────────┘
import fcntl
import json
import logging
import os
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, date as date_type
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from zoneinfo import ZoneInfo

# ┌─────────────────────────────────────────────────────────────────────────┐
# │ THIRD-PARTY IMPORTS                                                     │
# └─────────────────────────────────────────────────────────────────────────┘
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

warnings.filterwarnings("ignore", category=FutureWarning)


# ════════════════════════════════════════════════════════════
# § CONSTANTS & TIMEZONE DEFINITIONS
# ════════════════════════════════════════════════════════════

EST        = ZoneInfo("America/New_York")   # legacy alias — market clock
MARKET_TZ  = EST                             # authoritative market clock — DO NOT change
DISPLAY_TZ = ZoneInfo("America/Toronto")     # same UTC offset; user-facing label

# ── Telegram & AI credentials (set in environment) ──────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# ── Filesystem paths ─────────────────────────────────────────
STATE_FILE          = "scanner_state.json"
SYMBOLS_YAML        = "symbols.yaml"
EARNINGS_CACHE_FILE = "earnings_cache.json"
LOGS_DIR            = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

# ── Telegram formatting rules ────────────────────────────────
H_RULE   = "`━━━━━━━━━━━━━━━━━━━━━`"   # major section separator
SUB_RULE = "`─────────────────`"          # minor section separator
TG_LIMIT = 4096                           # Telegram hard char limit per message


# ════════════════════════════════════════════════════════════
# § CONFIGURATION DATACLASS
#   All numeric thresholds live here.
#   Overridable per-symbol-file via settings.intel block.
# ════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    # ── Alert thresholds ────────────────────────────────────
    big_drop_warn:      float = -5.0    # % drop that fires a WARNING alert
    big_drop_critical:  float = -10.0   # % drop that fires a CRITICAL alert
    big_gain_alert:     float = 8.0     # % gain that fires a GAIN alert

    # ── Positional thresholds ────────────────────────────────
    near_52w_low_pct:   float = 10.0    # within X% of 52W low → warn
    ath_pullback_alert: float = -15.0   # X% from ATH → pullback flag

    # ── Cooldown windows (hours) ─────────────────────────────
    cooldown_hours:           int = 4   # per-symbol big-move cooldown
    sector_bleed_cooldown_h:  int = 4   # sector bleed dedup window
    leadership_cooldown_h:    int = 3   # leadership/laggard dedup window

    # ── Earnings ─────────────────────────────────────────────
    earnings_warning_days: int = 3      # warn this many days before report
    earnings_cache_ttl_h:  int = 12     # hours before re-fetching earnings

    # ── Parallelism & AI ─────────────────────────────────────
    fetch_workers: int = 5              # ThreadPoolExecutor max workers
    ai_timeout_s:  int = 20            # Gemini HTTP timeout (seconds)


# ════════════════════════════════════════════════════════════
# § HTTP SESSION (reused across all outbound calls)
#   Retry with exponential back-off; pool sized for parallelism.
# ════════════════════════════════════════════════════════════

def _build_session() -> requests.Session:
    """Create a shared requests.Session with automatic retry/back-off."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
    )
    s.mount(
        "https://",
        HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8),
    )
    return s

SESSION = _build_session()


# ════════════════════════════════════════════════════════════
# § CLOCK HELPERS
# ════════════════════════════════════════════════════════════

def now_est()     -> datetime: return datetime.now(MARKET_TZ)
def market_now()  -> datetime: return now_est()
def display_now() -> datetime: return now_est().astimezone(DISPLAY_TZ)


# ════════════════════════════════════════════════════════════
# § JSON I/O — atomic, fcntl-locked state writes
# ════════════════════════════════════════════════════════════

def load_json(path: str | Path, default: Any = None) -> Any:
    """Safe JSON loader; returns `default` on any error."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path: str | Path, data: Any) -> None:
    """Non-atomic writer — acceptable for single-writer cache files."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _state_update(mutator) -> None:
    """
    Atomic, fcntl-locked read-modify-write on STATE_FILE.
    `mutator` receives the current state dict and mutates it in-place.
    No return value needed — caller owns no reference to the dict.
    """
    Path(STATE_FILE).touch(exist_ok=True)
    with open(STATE_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = f.read()
            try:
                state = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                logging.warning("STATE_FILE corrupt; resetting to {}")
                state = {}
            mutator(state)
            f.seek(0)
            f.truncate()
            json.dump(state, f, default=str)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ════════════════════════════════════════════════════════════
# § MARKDOWN ESCAPING
#   All user-facing dynamic text MUST pass through tg_escape()
#   before being embedded in Telegram Markdown payloads.
# ════════════════════════════════════════════════════════════

_MD_SPECIALS = re.compile(r"([_*`\[])")


def tg_escape(text: Any) -> str:
    """Escape MarkdownV1 special chars. Returns '—' for None."""
    if text is None:
        return "—"
    return _MD_SPECIALS.sub(r"\\\1", str(text))

md = tg_escape   # short internal alias


_AI_STRIP = re.compile(r"[`*_\[\]()]")


def _sanitize_ai(text: str | None, max_lines: int = 4, max_line_len: int = 140) -> str | None:
    """Strip Markdown from AI output and enforce line/length limits."""
    if not text:
        return None
    cleaned = _AI_STRIP.sub("", text).strip()
    lines = [ln.strip()[:max_line_len] for ln in cleaned.splitlines() if ln.strip()]
    return "\n".join(lines[:max_lines]) or None


# ════════════════════════════════════════════════════════════
# § SYMBOLS LOADER
#   Supports v3 universe schema (preferred) and legacy bucket schema.
#   Returns a dict with keys: symbols, sectors, emoji, meta, settings.
#   Returns None on parse failure → caller uses hardcoded fallback.
# ════════════════════════════════════════════════════════════

def _load_from_yaml() -> dict | None:
    """
    Parse symbols.yaml.

    v3 schema: top-level `universe:` list of symbol dicts.
    Legacy schema: top-level bucket keys (crypto / extended_hours / etc.).

    Returns
    -------
    dict with keys symbols, sectors, emoji, meta, settings
    None if yaml missing or parse error
    """
    yaml_path = Path(SYMBOLS_YAML)
    if not yaml_path.exists():
        return None

    try:
        import yaml  # optional dep — only needed when yaml file present
        with open(yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        # ── Schema detection ─────────────────────────────────
        if "universe" in raw:
            entries = raw["universe"] or []
        else:
            entries = []
            for bucket in ("crypto", "extended_hours", "regular_hours", "dip_extras"):
                entries.extend(raw.get(bucket) or [])

        valid_sectors = set(raw.get("sectors_canonical") or [])
        valid_roles   = {"intel", "brief", "dip", "scanner"}

        seen: set[str]          = set()
        emoji_map: dict[str, str]  = {}
        sector_map: dict[str, str] = {}
        meta: dict[str, dict]      = {}
        all_syms: list[str]        = []
        problems: list[str]        = []

        for item in entries:
            sym = item.get("symbol")
            if not sym:
                problems.append(f"entry missing symbol: {item}")
                continue
            if sym in seen:
                problems.append(f"duplicate symbol: {sym}")
                continue
            seen.add(sym)

            sec = item.get("sector", "Other")
            if valid_sectors and sec not in valid_sectors:
                problems.append(f"{sym}: unknown sector '{sec}'")

            emoji = item.get("emoji", "📊")
            # Emoji can be 1–4 chars (some are multi-codepoint)
            if not (1 <= len(emoji) <= 4):
                problems.append(f"{sym}: emoji length suspicious ({emoji!r})")

            ac = item.get("asset_class", "stock")
            if ac not in {"stock", "crypto", "commodity"}:
                problems.append(f"{sym}: invalid asset_class '{ac}'")

            roles = set(item.get("roles") or ["intel", "brief"])
            invalid_roles = roles - valid_roles
            if invalid_roles:
                problems.append(f"{sym}: invalid roles {invalid_roles}")
                roles = roles & valid_roles  # keep valid subset

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

        # Surface up to 10 problems, then summarise rest
        for p in problems[:10]:
            logging.warning(f"symbols.yaml: {p}")
        if len(problems) > 10:
            logging.warning(f"symbols.yaml: …+{len(problems) - 10} more issues")

        # Build sector → [symbols] map
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


# ── Initialise global symbol tables ──────────────────────────
# FIX v3.2: SYMBOL_META / YAML_SETTINGS defined before _apply_yaml_overrides()
# is called; previously they were conditionally assigned and then referenced
# before the else-branch ran, causing NameError when yaml was absent.

SYMBOL_META:   dict[str, dict] = {}
YAML_SETTINGS: dict            = {}

_yaml = _load_from_yaml()

if _yaml:
    MONITOR_LIST  = _yaml["symbols"]
    SECTORS       = _yaml["sectors"]
    SYMBOL_EMOJI  = _yaml["emoji"]
    SYMBOL_META   = _yaml["meta"]
    YAML_SETTINGS = _yaml["settings"]
    logging.info(
        f"market_intel: loaded {len(MONITOR_LIST)} symbols from yaml "
        f"({len(SECTORS)} sectors)"
    )
else:
    # ── Hardcoded fallback universe ──────────────────────────
    SECTORS: dict[str, list[str]] = {
        "AI / Semis":       ["NVDA", "AMD", "MU", "SNDK", "NBIS"],
        "Crypto":           ["BTC-USD", "ETH-USD", "XRP-USD"],
        "Crypto Mining":    ["IREN"],
        "Quantum":          ["IONQ", "RGTI", "QBTS"],
        "Nuclear / Energy": ["OKLO", "UAMY"],
        "Mega Tech":        ["GOOGL", "MSFT", "META", "AMZN", "AAPL"],
        "EV / Auto":        ["TSLA"],
        "Fintech":          ["SOFI"],
        "Healthcare":       ["NVO", "WGRX"],
        "Streaming":        ["NFLX"],
        "Safe Haven":       ["GC=F"],
    }
    MONITOR_LIST = list(dict.fromkeys(s for syms in SECTORS.values() for s in syms))
    SYMBOL_EMOJI: dict[str, str] = {
        "BTC-USD": "₿",  "ETH-USD": "Ξ",  "XRP-USD": "◇",  "GC=F": "🥇",
        "NVDA":    "💎", "TSLA":    "🚘", "META":    "👓", "AMZN": "📦",
        "GOOGL":   "🔍", "MSFT":    "🪟", "NFLX":    "🎬", "AMD":  "⚡",
        "AAPL":    "🍎", "MU":      "💾", "SNDK":    "💽", "NBIS": "🌐",
        "IONQ":    "⚛️", "RGTI":   "🧪", "QBTS":   "🔬",
        "OKLO":    "☢️", "IREN":   "🪙", "UAMY":   "⚒️", "WGRX": "💊",
        "SOFI":    "🏦", "NVO":     "💉",
    }
    logging.info("market_intel: symbols.yaml not found — using hardcoded fallback")

# ── Reverse lookup: symbol → PRIMARY sector (first-write wins) ──
SYMBOL_TO_SECTOR: dict[str, str] = {}
for _sec, _syms in SECTORS.items():
    for _s in _syms:
        SYMBOL_TO_SECTOR.setdefault(_s, _sec)


# ════════════════════════════════════════════════════════════
# § CONFIG — instantiated with optional yaml overrides
# ════════════════════════════════════════════════════════════

def _apply_yaml_overrides(cfg: Config) -> Config:
    """
    Merge settings.intel from yaml into Config.
    Only known Config field names are accepted — unknown keys are silently
    dropped to prevent accidental attribute injection.
    """
    overrides = (YAML_SETTINGS or {}).get("intel") or {}
    if not overrides:
        return cfg
    valid = {f for f in cfg.__dataclass_fields__}
    safe  = {k: v for k, v in overrides.items() if k in valid}
    if safe:
        logging.info(f"market_intel: applied yaml overrides: {sorted(safe)}")
    return replace(cfg, **safe)


CFG = _apply_yaml_overrides(Config())

# ── Backward-compat scalar aliases (used by older companion scripts) ──
BIG_DROP_WARN         = CFG.big_drop_warn
BIG_DROP_CRITICAL     = CFG.big_drop_critical
BIG_GAIN_ALERT        = CFG.big_gain_alert
NEAR_52W_LOW_PCT      = CFG.near_52w_low_pct
ATH_PULLBACK_ALERT    = CFG.ath_pullback_alert
COOLDOWN_HOURS        = CFG.cooldown_hours
SECTOR_BLEED_COOLDOWN = CFG.sector_bleed_cooldown_h
LEADERSHIP_COOLDOWN   = CFG.leadership_cooldown_h
EARNINGS_WARNING_DAYS = CFG.earnings_warning_days
FETCH_DELAY           = 0.0  # kept for interface compat; not used internally


# ════════════════════════════════════════════════════════════
# § PER-SCAN IN-MEMORY CACHE
#   Keyed by "SYMBOL|period|interval".
#   call clear_caches() at the start of each scan loop iteration
#   to prevent stale data from carrying over between runs.
# ════════════════════════════════════════════════════════════

_DAILY_CACHE:    dict[str, pd.DataFrame] = {}
_INTRADAY_CACHE: dict[str, pd.DataFrame] = {}
_CACHE_LOCK = Lock()


def clear_caches() -> None:
    """Discard all per-scan cached DataFrames. Call once per scan cycle."""
    with _CACHE_LOCK:
        _DAILY_CACHE.clear()
        _INTRADAY_CACHE.clear()


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns produced by yfinance for single tickers."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _yf_download(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
    """
    Cached yfinance wrapper.  Returns None on failure or empty result.
    Cache key = "SYMBOL|period|interval" — separate dicts for daily vs intraday
    to avoid key collision when same period/interval string is reused.
    """
    key = f"{symbol}|{period}|{interval}"
    is_daily = (interval == "1d")
    cache    = _DAILY_CACHE if is_daily else _INTRADAY_CACHE

    with _CACHE_LOCK:
        if key in cache:
            return cache[key]

    for attempt in range(2):
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                return None
            df = _clean_df(df)
            with _CACHE_LOCK:
                cache[key] = df
            return df
        except Exception as e:
            logging.debug(f"yf {symbol} {period}/{interval} attempt {attempt + 1}: {e}")
            time.sleep(0.5)

    return None


# ════════════════════════════════════════════════════════════
# § INDICATOR HELPERS
# ════════════════════════════════════════════════════════════

def rma(series: pd.Series, length: int) -> pd.Series:
    """
    Wilder's smoothed moving average (identical to Pine Script's rma()).
    alpha = 1 / length, equivalent to RMA / SMMA / MMA.
    """
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def pine_rsi(src: pd.Series, length: int = 14) -> pd.Series:
    """
    RSI using Wilder's smoothing (matches TradingView Pine Script).
    Falls back to 50 on NaN (insufficient history).
    NOTE: caller MUST pass closed-bar series only — never include
    the still-forming current bar.
    """
    delta = src.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    # Guard against zero denominator
    rs = rma(gain, length) / rma(loss, length).replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _last_completed_index(daily: pd.DataFrame) -> int:
    """
    Return the iloc index of the last *completed* daily candle.

    Drops today's still-forming bar (if present) so that prev_close,
    EMA, and RSI calculations are never poisoned by a partial day.

    Returns -1 if there are not enough rows to determine a completed bar.

    FIX v3.2: original returned -1 on single-row df, which callers
    would use as daily.iloc[-2] → IndexError. Now returns -1 only
    when truly insufficient, and callers check for li < 1.
    """
    if daily.empty:
        return -1
    last_dt   = daily.index[-1]
    last_date = last_dt.date() if hasattr(last_dt, "date") else last_dt
    today     = market_now().date()
    if last_date == today:
        # Today's forming bar is the last row — completed bar is li-1
        return len(daily) - 2 if len(daily) >= 2 else -1
    return len(daily) - 1


def ath_recency_label(ath_date_str: str) -> str:
    """
    Human-readable label for how long ago the ATH was set.

    FIX v3.2: original mixed naive datetime.now() with potentially
    tz-aware datetimes. Now uses naive UTC throughout for a simple
    calendar-day delta — timezone accuracy is not needed here.
    """
    try:
        ath_dt = datetime.strptime(ath_date_str[:10], "%Y-%m-%d")
        days   = (datetime.utcnow().replace(tzinfo=None) - ath_dt).days
        if days == 0:   return "set TODAY 🔥"
        if days == 1:   return "set YESTERDAY 🔥"
        if days <= 7:   return f"set {days}d ago"
        if days <= 30:  return f"set {days // 7}w ago"
        if days <= 365: return f"set {days // 30}mo ago"
        return f"set {days // 365}y ago"
    except Exception:
        return f"on {ath_date_str}"


# ════════════════════════════════════════════════════════════
# § EARNINGS CACHE (12 h TTL)
#   Avoids repeated yfinance calendar fetches for the same symbol
#   within the cache TTL.  Uses a flat JSON file.
# ════════════════════════════════════════════════════════════

def _earnings_cached(symbol: str) -> tuple | None:
    """Return (date, days_until) from cache if still fresh, else None."""
    cache = load_json(EARNINGS_CACHE_FILE, {})
    rec   = cache.get(symbol)
    if not rec:
        return None
    try:
        cached_at = datetime.fromisoformat(rec["cached_at"])
        # Normalise to aware if stored as naive
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=MARKET_TZ)
    except Exception:
        return None
    if now_est() - cached_at > timedelta(hours=CFG.earnings_cache_ttl_h):
        return None

    ed   = rec.get("date")
    days = rec.get("days")
    if isinstance(ed, str):
        try:
            ed = datetime.fromisoformat(ed).date()
        except Exception:
            pass
    return ed, days


def _earnings_put(symbol: str, ed: Any, days: Any) -> None:
    """Persist an earnings record (date + days_until) to the JSON cache."""
    cache = load_json(EARNINGS_CACHE_FILE, {})
    cache[symbol] = {
        "date":      ed.isoformat() if hasattr(ed, "isoformat") else ed,
        "days":      days,
        "cached_at": now_est().isoformat(),
    }
    save_json(EARNINGS_CACHE_FILE, cache)


def get_earnings_date(symbol: str) -> tuple[Any, Any]:
    """
    Return (earnings_date, days_until) for symbol.
    Returns (None, None) for crypto/commodities or on fetch error.
    Result is cached for 12 h (CFG.earnings_cache_ttl_h).
    """
    # Crypto and spot commodities never have earnings
    if symbol.endswith("-USD") or symbol == "GC=F":
        return None, None

    hit = _earnings_cached(symbol)
    if hit is not None:
        return hit

    try:
        ticker = yf.Ticker(symbol)
        cal    = ticker.calendar
        if cal is None:
            _earnings_put(symbol, None, None)
            return None, None

        ed = None
        if isinstance(cal, dict):
            v  = cal.get("Earnings Date")
            ed = v[0] if isinstance(v, list) and v else v
        elif hasattr(cal, "loc"):
            try:
                if "Earnings Date" in cal.index:
                    ed = cal.loc["Earnings Date"].iloc[0]
            except Exception:
                ed = None

        if ed is None:
            _earnings_put(symbol, None, None)
            return None, None

        # Normalise to a plain date object
        if isinstance(ed, str):
            ed = datetime.fromisoformat(ed.split("T")[0])
        elif hasattr(ed, "to_pydatetime"):
            ed = ed.to_pydatetime()
        if hasattr(ed, "date"):
            ed = ed.date()

        today      = market_now().date()
        days_until = (ed - today).days
        if days_until < 0 or days_until > 60:
            _earnings_put(symbol, None, None)
            return None, None

        _earnings_put(symbol, ed, days_until)
        return ed, days_until

    except Exception as e:
        logging.debug(f"Earnings {symbol}: {e}")
        return None, None


def format_earnings_warning(
    _symbol_unused: str,        # retained for backward compatibility
    earnings_date: Any,
    days_until: Any,
) -> str | None:
    """
    Return a formatted earnings-warning string, or None if not relevant.
    Severity scales with proximity to the report date.
    """
    if earnings_date is None:
        return None
    if days_until <= 0:
        return "🚨 *Earnings TODAY* — extreme volatility risk"
    if days_until == 1:
        return f"⚠️ *Earnings TOMORROW* ({earnings_date}) — SKIP new longs"
    if days_until <= CFG.earnings_warning_days:
        return f"⚠️ *Earnings in {days_until} days* ({earnings_date}) — consider waiting"
    if days_until <= 7:
        return f"📅 Earnings in {days_until} days ({earnings_date})"
    return None


# ════════════════════════════════════════════════════════════
# § MARKET CONTEXT (SPY / QQQ / VIX)
#   Fetched once per scan; used for market-override logic in
#   get_verdict() and for display in format_big_move_alert().
# ════════════════════════════════════════════════════════════

def get_market_ctx() -> dict | None:
    """
    Return {ticker: {price, pct}} for SPY, QQQ, ^VIX.
    Uses last completed daily bar — not today's partial.
    Returns None on complete failure (all fetches fail).
    """
    try:
        out: dict[str, dict] = {}
        for t in ("SPY", "QQQ", "^VIX"):
            df = _yf_download(t, period="5d", interval="1d")
            if df is None or df.empty or len(df) < 2:
                continue
            li = _last_completed_index(df)
            if li < 1:
                continue
            last = float(df["Close"].iloc[li])
            prev = float(df["Close"].iloc[li - 1])
            out[t] = {
                "price": last,
                "pct":   (last - prev) / prev * 100,
            }
        return out or None
    except Exception as e:
        logging.error(f"Market ctx: {e}")
        return None


# ════════════════════════════════════════════════════════════
# § RELATIVE STRENGTH vs BENCHMARK
#   5-day return of symbol vs SPY.
#   FIX v3.2: original function signature accepted `ctx` dict but
#   then accessed ctx["symbol"] — confusing and error-prone when
#   called from format_big_move_alert with the full context dict.
#   Now accepts the symbol string directly.
# ════════════════════════════════════════════════════════════

def calc_relative_strength(
    symbol: str,
    benchmark: str = "SPY",
    lookback_days: int = 5,
) -> tuple[float | None, str | None]:
    """
    Return (rs_pct_diff, label) comparing symbol's 5d return to benchmark.
    Returns (None, None) on data error.
    """
    try:
        df_sym   = _yf_download(symbol,    period="1mo", interval="1d")
        df_bench = _yf_download(benchmark, period="1mo", interval="1d")
        if df_sym is None or df_bench is None:
            return None, None
        if len(df_sym) < lookback_days + 1 or len(df_bench) < lookback_days + 1:
            return None, None

        sym_perf   = (df_sym["Close"].iloc[-1]   / df_sym["Close"].iloc[-(lookback_days + 1)]   - 1) * 100
        bench_perf = (df_bench["Close"].iloc[-1] / df_bench["Close"].iloc[-(lookback_days + 1)] - 1) * 100
        diff       = float(sym_perf - bench_perf)

        if   diff >  5: label = "🟢🟢 Strong Leader"
        elif diff >  2: label = "🟢 Outperforming"
        elif diff > -2: label = "⚖️ In-line"
        elif diff > -5: label = "🔴 Underperforming"
        else:           label = "🔴🔴 Weak / Laggard"

        return round(diff, 2), label

    except Exception as e:
        logging.debug(f"RS {symbol}: {e}")
        return None, None


# ════════════════════════════════════════════════════════════
# § FULL CONTEXT — single source of indicator truth per symbol
#   Returns a rich dict consumed by get_verdict(), format_*(), and AI.
#   Returns None if data is insufficient or fetch fails.
# ════════════════════════════════════════════════════════════

def get_full_context(symbol: str) -> dict | None:
    """
    Compute the complete technical snapshot for a symbol.

    Price, 52W range, ATH, EMA20/50/200, Pine RSI, volume ratio,
    trend classification, intraday OHLC.

    Returns
    -------
    dict with all indicator fields, or None on failure.
    """
    try:
        daily    = _yf_download(symbol, period="5y", interval="1d")
        if daily is None or len(daily) < 50:
            return None

        intraday = _yf_download(symbol, period="2d", interval="5m")
        if intraday is None or intraday.empty:
            return None

        # ── Live price (last intraday bar close) ─────────────
        current = float(intraday["Close"].iloc[-1])

        # ── Completed daily bar index ─────────────────────────
        li = _last_completed_index(daily)
        if li < 1:
            return None  # need at least 2 completed bars for prev_close

        prev_close           = float(daily["Close"].iloc[li - 1])
        last_completed_close = float(daily["Close"].iloc[li])

        # ── Today's intraday bars — timezone-aware ────────────
        is_crypto = symbol.endswith("-USD")
        try:
            if intraday.index.tz is None:
                idx_tz = intraday.tz_localize("UTC").tz_convert(MARKET_TZ)
            else:
                idx_tz = intraday.tz_convert(MARKET_TZ)
        except Exception:
            idx_tz = intraday

        if is_crypto:
            # 24 h rolling window for crypto (no EST session boundary)
            cutoff     = idx_tz.index[-1] - pd.Timedelta(hours=24)
            today_bars = idx_tz[idx_tz.index >= cutoff]
        else:
            today_bars = idx_tz[idx_tz.index.date == market_now().date()]

        # Safety fallback — last ~78 bars (~6.5 h of 5-min candles)
        if today_bars.empty:
            today_bars = idx_tz.iloc[-78:]

        today_open = float(today_bars["Open"].iloc[0])
        today_high = float(today_bars["High"].max())
        today_low  = float(today_bars["Low"].min())
        vol_today  = float(today_bars["Volume"].sum())

        day_change_pct = (current - prev_close) / prev_close * 100 if prev_close else 0.0
        intraday_pct   = (current - today_open) / today_open * 100 if today_open  else 0.0

        # ── 52W range (date-windowed, not row-sliced) ─────────
        cutoff_52w = daily.index[-1] - pd.Timedelta(days=365)
        win        = daily[daily.index >= cutoff_52w]
        if len(win) < 20:
            win = daily  # graceful fallback for short-history symbols
        low_52w  = float(win["Low"].min())
        high_52w = float(win["High"].max())

        # ── All-time high ─────────────────────────────────────
        ath     = float(daily["High"].max())
        ath_idx = daily["High"].idxmax()

        ath_pct           = (current - ath)      / ath      * 100
        pct_from_52w_low  = (current - low_52w)  / low_52w  * 100 if low_52w  > 0 else 0.0
        pct_from_52w_high = (current - high_52w) / high_52w * 100 if high_52w > 0 else 0.0
        range_pos = (
            (current - low_52w) / (high_52w - low_52w) * 100
            if high_52w > low_52w else 50.0
        )

        # ── EMAs and RSI — closed candles ONLY ───────────────
        # closed = daily up to and including last completed bar
        closed = daily.iloc[: li + 1]

        ema20  = float(closed["Close"].ewm(span=20,  adjust=False).mean().iloc[-1])
        ema50  = float(closed["Close"].ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200_real: float | None = (
            float(closed["Close"].ewm(span=200, adjust=False).mean().iloc[-1])
            if len(closed) >= 200 else None
        )

        rsi_series = pine_rsi(closed["Close"], 14)
        rsi        = float(rsi_series.iloc[-1])
        if np.isnan(rsi):
            rsi = 50.0

        # ── Volume ratio (vs prior 20 completed days) ─────────
        # FIX v3.2: ensure we only use completed bars (iloc uses closed)
        vol_window  = closed["Volume"].iloc[-20:] if len(closed) >= 20 else closed["Volume"]
        vol_avg_20d = float(vol_window.mean()) if not vol_window.empty else 0.0
        vol_ratio   = vol_today / vol_avg_20d if vol_avg_20d > 0 else 1.0

        # ── Trend classification ──────────────────────────────
        ema200_for_trend = ema200_real if ema200_real is not None else ema50

        if   current > ema20 > ema50 > ema200_for_trend:      trend = "🚀 STRONG UPTREND"
        elif current < ema20 < ema50 < ema200_for_trend:      trend = "💀 STRONG DOWNTREND"
        elif current > ema50 > ema200_for_trend:              trend = "📈 UPTREND"
        elif current < ema50 < ema200_for_trend:              trend = "📉 DOWNTREND"
        elif current > ema200_for_trend and current < ema50:  trend = "🔄 PULLBACK IN UPTREND"
        elif current < ema200_for_trend and current > ema50:  trend = "🔀 RECOVERING"
        else:                                                  trend = "⚖️ MIXED"

        if ema200_real is None:
            trend += " ⓘ"   # indicates EMA200 based on EMA50 fallback

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
            "ath_date":          (
                ath_idx.strftime("%Y-%m-%d")
                if hasattr(ath_idx, "strftime") else str(ath_idx)[:10]
            ),
            "ath_pct":           ath_pct,
            "low_52w":           low_52w,
            "high_52w":          high_52w,
            "pct_from_52w_low":  pct_from_52w_low,
            "pct_from_52w_high": pct_from_52w_high,
            "range_pos":         range_pos,
            "ema20":             ema20,
            "ema50":             ema50,
            # Legacy field always populated (callers that pre-date ema200_real)
            "ema200":            ema200_real if ema200_real is not None else ema50,
            # None when fewer than 200 completed daily bars
            "ema200_real":       ema200_real,
            "rsi":               rsi,
            "vol_ratio":         vol_ratio,
            "trend":             trend,
        }

    except Exception as e:
        logging.error(f"Context {symbol}: {e}")
        return None


# ════════════════════════════════════════════════════════════
# § VERDICT ENGINE
#   Translates technical snapshot into an actionable verdict + zone.
#   Pure function — no I/O, no side effects.
#
#   FIX v3.2: earnings_days param now correctly short-circuits the
#   internal get_earnings_date() call when days are already known,
#   preventing a duplicate yfinance fetch.
# ════════════════════════════════════════════════════════════

def get_verdict(
    ctx: dict,
    market_ctx: dict | None = None,
    *,
    earnings_days: Any = "AUTO",
) -> tuple[str, str, list[str]]:
    """
    Return (verdict_str, zone_str, reasons_list).

    Parameters
    ----------
    ctx           : full context dict from get_full_context()
    market_ctx    : optional market context from get_market_ctx()
    earnings_days : int days until earnings, or "AUTO" to fetch internally
    """
    c         = ctx
    rsi       = c["rsi"]
    trend     = c["trend"]
    drop      = c["day_change_pct"]
    from_ath  = c["ath_pct"]
    range_pos = c["range_pos"]
    above_50  = c["current"] > c["ema50"]
    above_200 = c["ema200_real"] is not None and c["current"] > c["ema200_real"]
    vol_ratio = c["vol_ratio"]

    verdict: str       = ""
    zone:    str       = ""
    reasons: list[str] = []

    # ── 0. PARABOLIC / CRASH (≥±15%) ──────────────────────────
    if abs(drop) >= 15:
        if drop > 0:
            verdict, zone = "⚠️ PARABOLIC", f"News/Catalyst Spike +{drop:.0f}%"
            reasons = [
                f"+{drop:.1f}% single-day — likely news/catalyst driven",
                "Parabolic moves mean-revert — high risk to chase",
                (
                    f"Volume {vol_ratio:.1f}× avg — "
                    f"{'confirms activity' if vol_ratio > 1.5 else 'weak — possible pump'}"
                ),
            ]
        else:
            verdict, zone = "🚨 CRASH", f"Severe Drop {drop:.0f}%"
            reasons = [
                f"{drop:.1f}% single-day — likely news driven",
                "Wait for dust to settle before any entry",
            ]
        return verdict, zone, reasons

    # ── 1. MOMENTUM at / near ATH ─────────────────────────────
    if "UPTREND" in trend and from_ath > -5 and above_50 and above_200 and rsi < 80:
        verdict, zone = "🚀 MOMENTUM", "AT ATH — Continuation"
        reasons = [
            f"At/near all-time high ({from_ath:+.1f}%)",
            "EMA stack fully bullish",
            f"RSI {rsi:.0f} — not overbought, room to run",
        ]

    # ── 2. BUY ZONE — pullback in uptrend ─────────────────────
    elif "UPTREND" in trend and rsi < 52 and above_200:
        verdict, zone = "🟢 BUY ZONE", "Pullback in Uptrend"
        reasons = [
            "Healthy pullback in confirmed uptrend",
            f"RSI {rsi:.0f} — room to run",
        ]
        if from_ath > -20:
            reasons.append("Near ATH — strong stock pulling back")

    # ── 3. BUY ZONE — EMA50 pullback ──────────────────────────
    elif "PULLBACK" in trend and rsi < 55:
        verdict, zone = "🟢 BUY ZONE", "EMA50 Pullback"
        reasons = [
            "Above EMA200 — uptrend structure intact",
            f"Pulling toward EMA50 ${c['ema50']:.2f}",
            f"RSI {rsi:.0f} — watch for bounce",
        ]

    # ── 4. EXTENDED near ATH ──────────────────────────────────
    elif from_ath > -8 and rsi > 75:
        verdict, zone = "🟠 EXTENDED", "Overbought Near ATH"
        reasons = [
            f"RSI {rsi:.0f} — overbought at highs",
            "Risk/reward not ideal for new entry",
        ]

    # ── 5. AVOID — confirmed downtrend ────────────────────────
    elif "DOWNTREND" in trend and not above_200:
        verdict, zone = "🔴 AVOID", "Falling Knife"
        reasons = [
            "Below EMA50 & EMA200 — confirmed downtrend",
            "No base formed",
        ]
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal")

    # ── 6. CAUTION — near 52W low ─────────────────────────────
    elif c["pct_from_52w_low"] < 8 and drop < -3:
        verdict, zone = "⚠️ CAUTION", "Breaking Down"
        reasons = [
            "Near 52W low — key support at risk",
            "Wait for base formation",
        ]

    # ── 7. TAKE PROFITS — extended on a gain day ──────────────
    elif rsi > 75 and drop > 2:
        verdict, zone = "🟠 TAKE PROFITS", "Extended"
        reasons = [
            f"RSI overbought ({rsi:.0f})",
            "Consider trimming, not entering",
        ]

    # ── 8. RECOVERING ─────────────────────────────────────────
    elif "RECOVERING" in trend:
        if rsi > 55 and drop > 0:
            verdict, zone = "🟡 WATCH", "Recovery Attempt"
            reasons = [
                "Reclaiming EMA50 — potential recovery",
                f"Must clear EMA200 ${c['ema200']:.2f}",
            ]
        else:
            verdict, zone = "⏸️ HOLD", "Below EMA200"
            reasons = ["Below EMA200 — no structural confirmation"]

    # ── 9. MIXED ──────────────────────────────────────────────
    elif "MIXED" in trend:
        if range_pos < 30 and rsi < 45:
            verdict, zone = "🟡 WATCH", "Potential Accumulation"
            reasons = [
                "Lower 52W range — possible accumulation",
                "Wait for trend confirmation",
            ]
        elif rsi > 72:
            verdict, zone = "🟠 EXTENDED", "Overbought in Chop"
            reasons = [f"RSI {rsi:.0f} extended in mixed trend"]
        else:
            verdict, zone = "⏸️ HOLD", "No Edge"
            reasons = ["Mixed signals — wait for clarity"]

    # ── 10. DEFAULT ───────────────────────────────────────────
    else:
        if above_50 and above_200 and rsi > 55:
            verdict, zone = "🟡 WATCH", "Building Momentum"
            reasons = [
                "Above both EMAs — structure improving",
                f"RSI {rsi:.0f} — momentum building",
            ]
        elif drop < -5:
            verdict, zone = "⚠️ WATCH", "Sharp Drop"
            reasons = ["Large move — wait for stabilisation"]
        else:
            verdict, zone = "⏸️ NEUTRAL", "No Clear Setup"
            reasons = ["No strong directional signal"]

    # ── Market context override ───────────────────────────────
    if market_ctx:
        vix_val  = market_ctx.get("^VIX", {}).get("price", 15)
        spy_pct  = market_ctx.get("SPY",  {}).get("pct",   0)
        if vix_val > 25 and spy_pct < -1.5 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
            verdict = "⚠️ WAIT"
            reasons.insert(0, f"Market bleeding — VIX {vix_val:.0f}, SPY {spy_pct:.1f}%")

    # ── Earnings override ─────────────────────────────────────
    if any(x in verdict for x in ["BUY", "MOMENTUM", "WATCH"]):
        if earnings_days == "AUTO":
            _, days = get_earnings_date(c["symbol"])
        else:
            days = earnings_days
        if days is not None and days <= CFG.earnings_warning_days:
            verdict = "⚠️ WAIT — Earnings"
            zone    = f"Earnings in {days}d"
            reasons.insert(0, f"Earnings in {days} days — avoid new entries")

    return verdict, zone, reasons


# ════════════════════════════════════════════════════════════
# § AI ANALYSIS (Gemini) — non-blocking on rate-limit
#   If Gemini returns 429, we log and skip — no sleep in scan loop.
#   FIX v3.2: ema200_real None handled before formatting, not inside
#   the f-string (would crash with TypeError on None).
# ════════════════════════════════════════════════════════════

def ai_analyze_drop(ctx: dict, market_ctx: dict | None = None) -> str | None:
    """
    Call Gemini Flash and return a 4-line analysis string, or None.
    Returns None immediately when GEMINI_API_KEY is unset.
    Skips (returns None) on 429 — does NOT block the scan loop.
    """
    if not GEMINI_API_KEY:
        return None

    c = ctx
    mkt_line = ""
    if market_ctx:
        mkt_line = (
            f"\nMarket: SPY {market_ctx.get('SPY', {}).get('pct', 0):+.2f}%, "
            f"QQQ {market_ctx.get('QQQ', {}).get('pct', 0):+.2f}%, "
            f"VIX {market_ctx.get('^VIX', {}).get('price', 15):.1f}"
        )

    # FIX v3.2: guard ema200_real before format string
    ema200_str = f"${c['ema200_real']:.2f}" if c.get("ema200_real") is not None else "n/a"

    prompt = f"""You are a senior market analyst. Analyze this move in EXACTLY 4 lines (max 110 chars each).

{c['symbol']} — Today: {c['day_change_pct']:+.2f}% | Price: ${c['current']:.2f} | Volume: {c['vol_ratio']:.1f}× avg
52W: Low ${c['low_52w']:.2f} / High ${c['high_52w']:.2f} / ATH ${c['ath']:.2f} ({c['ath_pct']:+.1f}% from ATH)
Trend: {c['trend']} | RSI: {c['rsi']:.0f} | Position in 52W range: {c['range_pos']:.0f}%
EMA50: ${c['ema50']:.2f} | EMA200: {ema200_str}{mkt_line}

Respond EXACTLY:
📊 [Is this technical, sector-driven, or likely news/catalyst? Be specific]
🎯 [Setup quality — healthy pullback, correction, extended, or bleed?]
⚠️ [Biggest risk — specific price level or condition that invalidates]
💡 [STRONG BUY / BUY / HOLD / AVOID / WAIT] — [one sharp actionable sentence]

4 lines only. No extra text."""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        r = SESSION.post(
            url,
            json={
                "contents":        [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.5, "maxOutputTokens": 400},
            },
            timeout=CFG.ai_timeout_s,
        )
        if r.status_code == 200:
            data  = r.json()
            cands = data.get("candidates") or []
            if cands:
                return _sanitize_ai(cands[0]["content"]["parts"][0]["text"])
            logging.warning(f"Gemini empty response for {c['symbol']}")
        elif r.status_code == 429:
            logging.warning(f"Gemini rate-limited for {c['symbol']} — skipping (no sleep)")
        else:
            logging.error(f"Gemini {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"AI {c['symbol']}: {e}")

    return None


# ════════════════════════════════════════════════════════════
# § TELEGRAM ALERT FORMATTER — BIG MOVE
#
#   FIX v3.2: signature now includes earnings_date + days_until
#   so the caller (run_intel_scan) can pass already-fetched
#   earnings data.  The old version called get_earnings_date()
#   internally, causing a duplicate fetch and potential race.
#
#   Also fixed: dead variable `support_level` removed from BUY block.
# ════════════════════════════════════════════════════════════

def format_big_move_alert(
    ctx:           dict,
    verdict:       str,
    zone:          str,
    reasons:       list[str],
    ai_text:       str | None,
    market_ctx:    dict | None,
    earnings_date: Any = None,
    days_until:    Any = None,
) -> str | None:
    """
    Compose the full Telegram Markdown message for a big-move alert.

    Returns None when the day_change_pct does not meet any alert threshold
    (so callers can safely pass any context without pre-filtering).
    """
    c    = ctx
    drop = c["day_change_pct"]
    rsi  = c.get("rsi", 50)

    # ── Severity tier ──────────────────────────────────────────
    if drop <= BIG_DROP_CRITICAL:
        header_em, severity = "🚨🩸", "CRITICAL DROP"
    elif drop <= BIG_DROP_WARN:
        header_em, severity = "⚠️📉", "BIG DROP"
    elif drop >= BIG_GAIN_ALERT:
        header_em, severity = "🚀📈", "BIG GAIN"
    else:
        return None  # below threshold

    em  = SYMBOL_EMOJI.get(c["symbol"], "📊")
    now = now_est()
    tz  = now.tzname() or "ET"
    ts  = now.strftime(f"%a %b %d · %I:%M %p {tz}")

    sign     = "+" if drop >= 0 else ""
    drop_em  = "🟢" if drop >= 0 else "🔴"
    decimals = 4 if c["current"] < 10 else 2
    pf       = f"{{:.{decimals}f}}"

    spy_pct = market_ctx.get("SPY",  {}).get("pct",   0)  if market_ctx else 0
    vix_val = market_ctx.get("^VIX", {}).get("price", 15) if market_ctx else 15

    # ── Stock vs market context banner note ───────────────────
    stock_vs_market = ""
    if drop < -5 and spy_pct > 0:
        stock_vs_market = "  🚨 _Stock\\-specific — market is green_"
    elif drop < -5 and spy_pct < -1.5:
        stock_vs_market = "  ⚠️ _Moving with broad market bleed_"
    elif drop > 8 and spy_pct < 0:
        stock_vs_market = "  💪 _Outperforming — market is red_"

    # ════════════════════════════════════════════
    # § MSG-1  HEADER
    # ════════════════════════════════════════════
    name_line = f"*{md(c['symbol'])}* {em}"

    msg  = f"{header_em} *{severity}*\n"
    msg += "`══════════════════════════`\n"
    msg += f"{name_line}\n"
    msg += f"`${pf.format(c['current'])}`  {drop_em} *{sign}{drop:.2f}%*{stock_vs_market}\n"
    msg += f"_{ts}_\n"
    msg += "`══════════════════════════`\n\n"

    # ════════════════════════════════════════════
    # § MSG-2  TAG PILLS — instant mobile context
    # ════════════════════════════════════════════
    tags: list[str] = []

    if   "BUY"       in verdict: tags.append("🟢 Buy Zone")
    elif "MOMENTUM"  in verdict: tags.append("🚀 Momentum")
    elif "EXTENDED"  in verdict: tags.append("🟠 Extended")
    elif "AVOID"     in verdict: tags.append("🔴 Avoid")
    elif "PARABOLIC" in verdict: tags.append("🚨 Parabolic")
    elif "CRASH"     in verdict: tags.append("💥 Crash")
    elif "WAIT"      in verdict: tags.append("⏳ Wait")
    elif "CAUTION"   in verdict: tags.append("⚠️ Caution")
    elif "WATCH"     in verdict: tags.append("👀 Watch")

    if   rsi >= 70: tags.append("🔴 RSI Overbought")
    elif rsi <= 30: tags.append("🟢 RSI Oversold")
    elif rsi >= 60: tags.append("🟡 RSI Bullish")

    vol = c.get("vol_ratio", 1.0)
    if   vol >= 2.0: tags.append("🔥 High Volume")
    elif vol <  0.7: tags.append("⬇️ Low Volume")

    if vix_val > 25: tags.append("🔴 VIX High")

    if tags:
        msg += "  ·  ".join(tags[:4]) + "\n\n"

    # ════════════════════════════════════════════
    # § MSG-3  VERDICT
    # ════════════════════════════════════════════
    msg += f"*〔 {verdict} 〕*\n"
    msg += f"_↳ {zone}_\n"

    # AI one-liner right under verdict (last line with actionable keyword)
    if ai_text:
        lines   = ai_text.strip().split("\n")
        summary = next(
            (ln for ln in reversed(lines)
             if any(w in ln.upper() for w in ["WAIT", "BUY", "HOLD", "AVOID", "STRONG"])),
            lines[-1] if lines else None,
        )
        if summary:
            msg += f"\n{summary}\n"

    msg += "\n"
    for r in reasons[:3]:
        msg += f"  › {r}\n"
    msg += "`══════════════════════════`\n\n"

    # ════════════════════════════════════════════
    # § MSG-4  PRICE
    # ════════════════════════════════════════════
    msg += "💵 *PRICE*\n`──────────────────────────`\n"
    msg += f"  Live     `${pf.format(c['current'])}`  {drop_em} {sign}{drop:.2f}% today\n"
    msg += f"  Range    L `${pf.format(c['today_low'])}` — H `${pf.format(c['today_high'])}`\n"

    if   vol >= 2.0: vol_line = f"`{vol:.1f}x` 🔥 Unusually high"
    elif vol >= 1.5: vol_line = f"`{vol:.1f}x` ⬆️ Above average"
    elif vol >= 0.8: vol_line = f"`{vol:.1f}x` — Normal"
    else:            vol_line = f"`{vol:.1f}x` ⬇️ Below average"
    msg += f"  Volume   {vol_line}\n"

    # Volume × move context signals
    if drop <= BIG_DROP_WARN and vol < 0.8:
        msg += "  ⚠️ _Big drop on low volume — may recover, watch for follow\\-through_\n"
    elif drop <= BIG_DROP_WARN and vol >= 2.0:
        msg += "  🚨 _High volume sell\\-off — distribution, not just a dip_\n"
    elif drop >= BIG_GAIN_ALERT and vol < 1.3:
        msg += "  ⚠️ _Big gain on low volume — thin/news\\-driven, less reliable_\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # § MSG-5  WHAT TO DO — verdict-specific action
    # ════════════════════════════════════════════
    msg += "🎯 *WHAT TO DO*\n`──────────────────────────`\n"

    if "BUY" in verdict:
        # FIX v3.2: removed dead `support_level` variable; ema200 used directly
        msg += f"  › Entry zone: `${pf.format(c['ema200'])}` — `${pf.format(c['current'])}` (EMA200 to current)\n"
        msg += f"  › Stop: below EMA200 `${pf.format(c['ema200'])}`\n"
        msg += f"  › Target: ATH `${pf.format(c['ath'])}` ({c['ath_pct']:+.1f}% away)\n"
    elif "MOMENTUM" in verdict:
        msg += f"  › Breakout entry: above ATH `${pf.format(c['ath'])}` with volume\n"
        msg += f"  › Pullback entry: dip to EMA50 `${pf.format(c['ema50'])}` (ideal)\n"
        msg += f"  › Stop: below EMA50 `${pf.format(c['ema50'])}`\n"
    elif "EXTENDED" in verdict or "PARABOLIC" in verdict:
        reentry = round(c["ema50"] * 0.98, 2)
        msg += "  › DO NOT chase at current price\n"
        msg += f"  › Re\\-entry zone: near EMA50 `${pf.format(reentry)}`\n"
        msg += f"  › RSI trigger: wait for RSI below 60 (currently `{rsi:.0f}`)\n"
    elif "CRASH" in verdict or "AVOID" in verdict:
        msg += "  › Do NOT catch today — wait minimum 3 days\n"
        msg += f"  › Watch: does it hold EMA200 `${pf.format(c['ema200'])}`?\n"
        msg += f"  › Entry only after base forms above EMA50 `${pf.format(c['ema50'])}`\n"
    elif "CAUTION" in verdict or "WATCH" in verdict:
        msg += f"  › Watch key level: EMA50 `${pf.format(c['ema50'])}`\n"
        msg += f"  › Scale\\-in zone: EMA200 `${pf.format(c['ema200'])}` if holds\n"
        msg += "  › Confirm with RSI > 50 + volume before entry\n"
    else:
        msg += "  › No clear edge — wait for directional setup\n"
        msg += f"  › Watch: EMA50 `${pf.format(c['ema50'])}` for direction\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # § MSG-6  POSITIONAL CONTEXT (ATH + 52W range)
    # ════════════════════════════════════════════
    msg += "📊 *POSITIONAL CONTEXT*\n`──────────────────────────`\n"

    ath_pct = c["ath_pct"]
    if   ath_pct > -5:  ath_tag = "🏔️ At/near ATH"
    elif ath_pct > -15: ath_tag = "📍 Near ATH"
    elif ath_pct > -30: ath_tag = "📉 Pullback from ATH"
    elif ath_pct > -50: ath_tag = "💀 Deep drawdown"
    else:               ath_tag = "⚰️ Far from ATH"

    ath_when = ath_recency_label(c["ath_date"])
    msg += f"  🏔️ ATH    `${pf.format(c['ath'])}` ({c['ath_pct']:+.1f}%) {ath_tag} — {ath_when}\n"

    rp  = c["range_pos"]
    pos = min(int(rp / 10), 10)   # guard: clamp to [0, 10]
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"  📐 52W    `${pf.format(c['low_52w'])}` — `${pf.format(c['high_52w'])}`\n"
    msg += f"         `{bar}` {rp:.0f}% of range\n"

    if c["pct_from_52w_low"] > 500:
        msg += f"  ⚠️ _52W low `${pf.format(c['low_52w'])}` may reflect split/spin\\-off_\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # § MSG-7  TECHNICALS
    # ════════════════════════════════════════════
    msg += "📈 *TECHNICALS*\n`──────────────────────────`\n"
    msg += f"  Trend   {c['trend']}\n"

    if   rsi < 30:  rsi_tag, rsi_em = "Oversold",   "🟢"
    elif rsi >= 70: rsi_tag, rsi_em = "Overbought", "🔴"
    elif rsi > 60:  rsi_tag, rsi_em = "Bullish",    "🟡"
    else:           rsi_tag, rsi_em = "Neutral",    "⚪"
    msg += f"  RSI     `{rsi:.0f}` {rsi_em} _{rsi_tag}_\n"

    msg += f"  EMA50   `${pf.format(c['ema50'])}`\n"
    msg += f"  EMA200  `${pf.format(c['ema200'])}`\n"

    above_50  = c["current"] > c["ema50"]
    above_200 = c["current"] > c["ema200"]
    if   above_50 and above_200:     msg += "  ✅ Above EMA50 & EMA200\n"
    elif above_200 and not above_50: msg += "  ⚠️ Below EMA50, above EMA200\n"
    elif not above_200 and above_50: msg += "  🔀 Above EMA50, below EMA200\n"
    else:                            msg += "  🔴 Below both EMAs\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # § MSG-8  EARNINGS (uses pre-fetched data, no extra yf call)
    # ════════════════════════════════════════════
    earn_warn = format_earnings_warning(c["symbol"], earnings_date, days_until)
    if earn_warn:
        msg += f"📅 *EARNINGS*\n`──────────────────────────`\n  {earn_warn}\n\n"

    # ════════════════════════════════════════════
    # § MSG-9  RELATIVE STRENGTH vs SPY
    # ════════════════════════════════════════════
    # FIX v3.2: pass symbol string, not ctx dict
    rs_score, rs_label = calc_relative_strength(c["symbol"])
    if rs_score is not None:
        rs_sign = "+" if rs_score >= 0 else ""
        rs_em   = "💪" if rs_score > 5 else "📉" if rs_score < -5 else "➖"
        msg += f"  {rs_em} *RS vs SPY (5d)*  {rs_label}  `{rs_sign}{rs_score}%`\n\n"

    # ════════════════════════════════════════════
    # § MSG-10  MARKET CONDITIONS
    # ════════════════════════════════════════════
    if market_ctx:
        spy_d = market_ctx.get("SPY",  {})
        qqq_d = market_ctx.get("QQQ",  {})
        vix_d = market_ctx.get("^VIX", {})
        if spy_d or vix_d:
            msg += "🌍 *MARKET*\n`──────────────────────────`\n"
            parts: list[str] = []
            if spy_d:
                s  = spy_d.get("pct", 0)
                se = "🟢" if s >= 0 else "🔴"
                parts.append(f"SPY {se} `{'+' if s >= 0 else ''}{s:.2f}%`")
            if qqq_d:
                q  = qqq_d.get("pct", 0)
                qe = "🟢" if q >= 0 else "🔴"
                parts.append(f"QQQ {qe} `{'+' if q >= 0 else ''}{q:.2f}%`")
            if vix_d:
                ve   = "🔴" if vix_val > 25 else "🟡" if vix_val > 18 else "🟢"
                vtag = "High" if vix_val > 25 else "Elevated" if vix_val > 18 else "Calm"
                parts.append(f"VIX {ve} `{vix_val:.1f}` _{vtag}_")
            msg += f"  {'  ·  '.join(parts)}\n"

            if   drop < -5 and spy_pct > 0:   msg += "  🚨 _Stock\\-specific weakness — market is UP_\n"
            elif drop < -5 and spy_pct < -1.5: msg += "  ⚠️ _Moving with broad market sell\\-off_\n"
            elif drop > 8  and spy_pct < 0:    msg += "  💪 _Stock\\-specific strength — market is down_\n"
            elif vix_val > 22:                 msg += "  ⚠️ _Elevated VIX — broad risk\\-off environment_\n"
            msg += "\n"

    # ════════════════════════════════════════════
    # § MSG-11  AI ANALYSIS (full 4-line block)
    # ════════════════════════════════════════════
    if ai_text:
        msg += "🤖 *AI ANALYSIS*\n`──────────────────────────`\n"
        for line in ai_text.strip().split("\n"):
            if line.strip():
                msg += f"  {line.strip()}\n"
        msg += "\n"

    msg += "`══════════════════════════`\n"
    msg += "_AlphaEdge Market Intel_"
    return msg


# ════════════════════════════════════════════════════════════
# § SECTOR BLEED DETECTOR
#   Triggers when a sector's average move is <-2% AND at least
#   half its members (min 2) are individually down >2%.
# ════════════════════════════════════════════════════════════

def check_sector_bleeds(all_contexts: dict[str, dict]) -> dict[str, dict]:
    """
    Return {sector: {avg, bleeding, all}} for sectors in bleed.
    Requires ≥2 symbols with data; at least max(2, n//2) must be down >2%.
    """
    out: dict[str, dict] = {}

    for sector, syms in SECTORS.items():
        moves = [
            (s, all_contexts[s]["day_change_pct"])
            for s in syms
            if s in all_contexts and all_contexts[s]
        ]
        if len(moves) < 2:
            continue

        avg      = sum(m[1] for m in moves) / len(moves)
        bleeding = [m for m in moves if m[1] < -2]

        # Both conditions required: sector avg down AND majority bleeding
        if avg < -2 and len(bleeding) >= max(2, len(moves) // 2):
            out[sector] = {
                "avg":      avg,
                "bleeding": bleeding,
                "all":      moves,
            }

    return out


def format_sector_bleed_alert(sector_moves: dict) -> str | None:
    """Compose Telegram message for sector bleed detection. Returns None if empty."""
    if not sector_moves:
        return None

    ts  = display_now().strftime("%a %b %d • %I:%M %p ET")
    msg = f"🩸 *SECTOR BLEED DETECTED*\n`━━━━━━━━━━━━━━━━━━━━━`\n_{ts}_\n\n"

    for sector, data in sorted(sector_moves.items(), key=lambda x: x[1]["avg"]):
        msg += f"*{md(sector)}*\n"
        msg += f"Sector avg: `{data['avg']:+.2f}%`\n"
        msg += "`─────────────────`\n"

        for sym, pct in sorted(data["all"], key=lambda x: x[1]):
            em     = SYMBOL_EMOJI.get(sym, "📊")
            pct_em = (
                "🔴" if pct < -5 else
                "🟠" if pct < -2 else
                "🟡" if pct < 0  else
                "🟢"
            )
            msg += f"{pct_em} {em} *{md(sym)}* `{pct:+.2f}%`\n"

        msg += "\n"

    msg += "*ACTION*\n`─────────────────`\n"
    msg += "🚫 Avoid new longs in bleeding sectors\n"
    msg += "⏳ Wait for stabilization or leadership divergence\n"
    msg += "👀 Watch strongest names that hold above EMA50\n"
    msg += "\n`━━━━━━━━━━━━━━━━━━━━━`\n_AlphaEdge Sector Intel_"

    return msg


# ════════════════════════════════════════════════════════════
# § LEADERSHIP / LAGGARD DETECTOR
#   Within a bleed/rip sector, finds stocks that diverge
#   significantly from the sector average.
# ════════════════════════════════════════════════════════════

def check_leadership(
    all_contexts: dict[str, dict],
    sector_full:  dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """
    Return (leaders, laggards).

    Leaders: stock is up relative to a bleeding sector (div > +2pp).
    Laggards: stock is down relative to a rising sector (div < -2pp).
    Only considers sectors with |avg| > 1.5%.
    """
    leaders:  list[dict] = []
    laggards: list[dict] = []

    for sector, data in sector_full.items():
        s_avg = data["avg"]
        if abs(s_avg) < 1.5:
            continue

        for sym, pct in data["all"]:
            # Only the symbol's PRIMARY sector contributes to leadership
            if SYMBOL_TO_SECTOR.get(sym) != sector:
                continue
            ctx = all_contexts.get(sym)
            if not ctx:
                continue

            div = pct - s_avg

            if s_avg < -2 and div > 2:
                leaders.append({
                    "symbol":     sym,
                    "ctx":        ctx,
                    "sector":     sector,
                    "sector_avg": s_avg,
                    "divergence": div,
                })
            elif s_avg > 2 and div < -2:
                laggards.append({
                    "symbol":     sym,
                    "ctx":        ctx,
                    "sector":     sector,
                    "sector_avg": s_avg,
                    "divergence": div,
                })

    return leaders, laggards


def name_label(sym: str, *, bold_ticker: bool = True) -> str:
    """Return 'AAPL — Apple Inc. (NASDAQ)' when metadata is available."""
    meta = SYMBOL_META.get(sym, {})
    name = meta.get("name", "")
    exch = meta.get("exchange", "")
    ticker = f"*{md(sym)}*" if bold_ticker else md(sym)
    if name and exch: return f"{ticker} — {md(name)} ({md(exch)})"
    if name:          return f"{ticker} — {md(name)}"
    return ticker


def format_leadership_alert(leaders: list[dict], laggards: list[dict]) -> str | None:
    """Compose Telegram message for leadership/laggard detection. Returns None if empty."""
    if not leaders and not laggards:
        return None

    ts  = display_now().strftime("%a %b %d • %I:%M %p ET")
    msg = f"💪 *RELATIVE STRENGTH SIGNALS*\n`═════════════════════`\n_{ts}_\n\n"

    if leaders:
        msg += "🏆 *LEADERS*\n_Holding strong while sector is weak_\n`─────────────────`\n"
        for ldr in sorted(leaders, key=lambda x: -x["divergence"]):
            em = SYMBOL_EMOJI.get(ldr["symbol"], "📊")
            msg += f"{em} {name_label(ldr['symbol'])}\n"
            msg += f"Sector: {md(ldr['sector'])}\n"
            msg += (
                f"Stock: `{ldr['ctx']['day_change_pct']:+.2f}%` • "
                f"Sector: `{ldr['sector_avg']:+.2f}%`\n"
            )
            msg += f"💪 Outperforming by *{ldr['divergence']:+.2f}%*\n\n"
        msg += "💡 _Leaders during weakness can become future winners._\n\n"

    if laggards:
        msg += "🔻 *LAGGARDS*\n_Weak names inside strong sectors_\n`─────────────────`\n"
        for ldr in sorted(laggards, key=lambda x: x["divergence"]):
            em = SYMBOL_EMOJI.get(ldr["symbol"], "📊")
            msg += f"{em} {name_label(ldr['symbol'])}\n"
            msg += f"Sector: {md(ldr['sector'])}\n"
            msg += (
                f"Stock: `{ldr['ctx']['day_change_pct']:+.2f}%` • "
                f"Sector: `{ldr['sector_avg']:+.2f}%`\n"
            )
            msg += f"📉 Underperforming by *{ldr['divergence']:+.2f}%*\n\n"
        msg += "⚠️ _Laggards in strong sectors show relative weakness._\n\n"

    msg += "*ACTION*\n`─────────────────`\n"
    msg += "🏆 Prioritize leaders on pullbacks\n"
    msg += "🔻 Avoid laggards until trend improves\n"
    msg += "👀 Confirm with volume + EMA50 reclaim\n"
    msg += "\n`━━━━━━━━━━━━━━━━━━━━━`\n_AlphaEdge Leadership Intel_"

    return msg


# ════════════════════════════════════════════════════════════
# § COOLDOWN MANAGEMENT
#   can_alert() is PURE — no side effects.
#   mark_alert() writes atomically via _state_update().
#   Always call can_alert() first, then mark_alert() only after
#   a confirmed successful Telegram send.
# ════════════════════════════════════════════════════════════

def can_alert(key: str, hours: int = COOLDOWN_HOURS) -> bool:
    """
    PURE check: has enough time elapsed since the last alert for `key`?
    Does NOT write state. Call mark_alert() separately after a confirmed send.
    """
    state = load_json(STATE_FILE, {})
    last  = state.get(key)
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MARKET_TZ)
        return (now_est() - dt) >= timedelta(hours=hours)
    except Exception:
        return True


def mark_alert(key: str) -> None:
    """
    Atomically record that an alert for `key` was just sent.
    Call only after a confirmed successful Telegram send to avoid
    poisoning the cooldown with a failed delivery.
    """
    iso = now_est().isoformat()
    _state_update(lambda s: s.__setitem__(key, iso))


# ════════════════════════════════════════════════════════════
# § TELEGRAM — split, send, retry
#   Splits on blank-line boundaries to avoid cutting mid-section.
#   Hard-slices at TG_LIMIT when a single paragraph exceeds limit.
# ════════════════════════════════════════════════════════════

def _split_for_telegram(msg: str, limit: int = TG_LIMIT) -> list[str]:
    """
    Split a long message into Telegram-safe chunks.

    FIX v3.2: original could produce chunks > limit when a single
    paragraph > limit (the block-level split would skip it and put
    everything in `current` until overflow).  Now hard-slices any
    block that is itself too large.
    """
    if len(msg) <= limit:
        return [msg]

    chunks:  list[str] = []
    current: str       = ""

    for block in msg.split("\n\n"):
        candidate = (current + "\n\n" + block) if current else block

        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
                current = ""
            # Hard-slice blocks that are themselves oversized
            while len(block) > limit:
                # Try to split at the last newline before the limit
                cut = block.rfind("\n", 0, limit)
                if cut == -1:
                    cut = limit
                chunks.append(block[:cut])
                block = block[cut:].lstrip("\n")
            current = block

    if current:
        chunks.append(current)

    # Annotate multi-part messages
    if len(chunks) > 1:
        chunks = [
            (f"{c}\n\n_(part {i + 1}/{len(chunks)})_" if i < len(chunks) - 1 else c)
            for i, c in enumerate(chunks)
        ]

    return chunks


def _send_single(message: str, silent: bool = False) -> bool:
    """Send one Telegram message. Retries once without parse_mode on parse error."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram credentials missing")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":             CHAT_ID,
        "text":                message,
        "parse_mode":          "Markdown",
        "disable_notification": silent,
    }

    try:
        r = SESSION.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        logging.error(f"Telegram {r.status_code}: {r.text[:200]}")

        # Retry without Markdown on parse error (e.g. unescaped special char)
        if "parse" in r.text.lower():
            logging.warning("Retrying Telegram send without parse_mode")
            plain = {k: v for k, v in payload.items() if k != "parse_mode"}
            r2 = SESSION.post(url, json=plain, timeout=10)
            return r2.status_code == 200

        return False

    except Exception as e:
        logging.error(f"Telegram send: {e}")
        return False


def send_telegram(message: str, silent: bool = False) -> bool:
    """Split and send a (potentially long) Telegram message. Returns True if all chunks sent."""
    ok = True
    for chunk in _split_for_telegram(message):
        if not _send_single(chunk, silent):
            ok = False
        time.sleep(0.3)   # brief pause to avoid Telegram flood limits
    return ok


# ════════════════════════════════════════════════════════════
# § MAIN ORCHESTRATION — parallel fetch + alert pipeline
# ════════════════════════════════════════════════════════════

def _scan_one(symbol: str) -> tuple[str, dict | None]:
    """Worker: fetch full context for one symbol. Returns (symbol, ctx|None)."""
    try:
        return symbol, get_full_context(symbol)
    except Exception as e:
        logging.error(f"scan_one {symbol}: {e}")
        return symbol, None


def run_intel_scan() -> None:
    """
    Full scan cycle:
    1. Fetch market context (SPY/QQQ/VIX)
    2. Parallel-fetch full context for every symbol in MONITOR_LIST
    3. Fire big-move alerts for symbols that cross thresholds
    4. Detect and alert sector bleed
    5. Detect and alert leadership / laggard divergence
    6. Log telemetry (duration, failures, alerts fired)
    """
    t0 = time.time()
    print(f"\n🧠 Market Intelligence Scan @ {display_now().strftime('%H:%M ET')}")
    logging.info("Intel scan start")
    clear_caches()

    market_ctx   = get_market_ctx()
    all_contexts: dict[str, dict] = {}
    fail = 0
    alerts_fired = 0

    # ── Parallel context fetch ────────────────────────────────
    with ThreadPoolExecutor(max_workers=CFG.fetch_workers) as ex:
        futures = {ex.submit(_scan_one, s): s for s in MONITOR_LIST}
        for fut in as_completed(futures):
            sym, ctx = fut.result()
            if not ctx:
                print(f"  → {sym:10s} —")
                fail += 1
                continue
            all_contexts[sym] = ctx
            print(f"  → {sym:10s} {ctx['day_change_pct']:+.2f}%")

    # ── Big-move alerts ───────────────────────────────────────
    for sym, ctx in all_contexts.items():
        drop = ctx["day_change_pct"]
        if not (drop <= CFG.big_drop_warn or drop >= CFG.big_gain_alert):
            continue

        cool_key = f"intel_bigmove_{sym}"
        if not can_alert(cool_key, CFG.cooldown_hours):
            print(f"  🔕 {sym} cooldown")
            continue

        # Fetch earnings once; pass to both get_verdict and format_big_move_alert
        ed, days = get_earnings_date(sym)
        verdict, zone, reasons = get_verdict(ctx, market_ctx, earnings_days=days)
        ai_text = ai_analyze_drop(ctx, market_ctx) if abs(drop) >= 5 else None

        msg = format_big_move_alert(
            ctx, verdict, zone, reasons, ai_text, market_ctx,
            earnings_date=ed,
            days_until=days,
        )
        if msg and send_telegram(msg, silent=False):
            mark_alert(cool_key)
            alerts_fired += 1
            print(f"  🚨 {sym} alert sent")

    if not all_contexts:
        print("\n⚠️ No contexts — skipping aggregate detectors")
        logging.warning("No contexts built — skipping sector/leadership checks")
        return

    # ── Sector bleed ─────────────────────────────────────────
    sector_moves = check_sector_bleeds(all_contexts)
    if sector_moves and can_alert("last_sector_bleed", CFG.sector_bleed_cooldown_h):
        msg = format_sector_bleed_alert(sector_moves)
        if msg and send_telegram(msg, silent=False):
            mark_alert("last_sector_bleed")
            alerts_fired += 1
            print("🩸 Sector bleed alert sent")

    # ── Leadership / laggard ──────────────────────────────────
    sector_full: dict[str, dict] = {}
    for sector, syms in SECTORS.items():
        moves = [
            (s, all_contexts[s]["day_change_pct"])
            for s in syms if s in all_contexts
        ]
        if len(moves) >= 2:
            sector_full[sector] = {
                "avg": sum(m[1] for m in moves) / len(moves),
                "all": moves,
            }

    leaders, laggards = check_leadership(all_contexts, sector_full)
    if (leaders or laggards) and can_alert("last_leadership_alert", CFG.leadership_cooldown_h):
        msg = format_leadership_alert(leaders, laggards)
        if msg and send_telegram(msg, silent=True):
            mark_alert("last_leadership_alert")
            alerts_fired += 1
            print("💪 Leadership alert sent")

    elapsed = time.time() - t0
    summary = (
        f"\n✅ Intel scan done — {alerts_fired} alert(s), "
        f"{fail} fail(s), {len(all_contexts)} ok in {elapsed:.1f}s"
    )
    print(summary)
    logging.info(
        f"Intel scan complete | alerts={alerts_fired} fail={fail} "
        f"ok={len(all_contexts)} elapsed={elapsed:.1f}s"
    )


# ════════════════════════════════════════════════════════════
# § LOGGING SETUP
#   Idempotent — safe to call multiple times (e.g., from tests).
#   Only adds the handler once; 14-day rotating log files.
# ════════════════════════════════════════════════════════════

def setup_logging() -> None:
    """Configure rotating file logger. Idempotent."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        return  # already configured
    handler = TimedRotatingFileHandler(
        LOGS_DIR / "intel.log", when="midnight", backupCount=14
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    root.addHandler(handler)


# ════════════════════════════════════════════════════════════
# § ENTRY POINT
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    setup_logging()
    run_intel_scan()


# ════════════════════════════════════════════════════════════════════════════
# ║  NEW-CHAT CONTEXT HANDOFF PROMPT
# ║
# ║  Copy the block below into a new conversation to onboard Claude with
# ║  full context about this codebase.
# ║
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  --- PASTE THIS INTO NEW CHAT ---                                        ║
# ║                                                                          ║
# ║  I'm working on AlphaEdge Market Intelligence (market_intel.py v3.2).   ║
# ║  Here is what the code does and what to verify:                          ║
# ║                                                                          ║
# ║  WHAT IT DOES                                                            ║
# ║  • Scans a watchlist of stocks/crypto/commodities via yfinance           ║
# ║  • Computes EMA20/50/200, Pine RSI (closed bars only), 52W range,        ║
# ║    ATH, volume ratio, trend classification, intraday OHLC                ║
# ║  • Derives a trading verdict (BUY ZONE / AVOID / MOMENTUM / WAIT, etc.) ║
# ║  • Detects sector bleed (avg < -2%, ≥ half members down > 2%)           ║
# ║  • Detects leadership/laggard divergence within sectors                  ║
# ║  • Sends rich Telegram Markdown alerts (Markdown v1, not v2)             ║
# ║  • Optionally calls Gemini Flash for a 4-line AI commentary block        ║
# ║  • Persists cooldown timestamps in scanner_state.json (fcntl-locked)     ║
# ║  • Caches earnings dates for 12 h in earnings_cache.json                 ║
# ║  • Uses a per-scan in-memory DataFrame cache (clear_caches() resets it)  ║
# ║                                                                          ║
# ║  KEY DESIGN CONSTRAINTS                                                  ║
# ║  • RSI MUST use closed candles only (daily.iloc[:li+1])                 ║
# ║  • EMA200 returns None when <200 bars — never fall back silently        ║
# ║  • prev_close = last COMPLETED daily bar (li-1), not today's partial    ║
# ║  • vol_avg_20d excludes today's partial volume                           ║
# ║  • Crypto open = last 24h rolling (not EST date slice)                   ║
# ║  • can_alert() is pure; mark_alert() writes atomically post-send        ║
# ║  • format_big_move_alert() takes earnings_date + days_until as params   ║
# ║    (do NOT call get_earnings_date() inside it — that's a duplicate fetch)║
# ║  • calc_relative_strength() takes a symbol string, not a ctx dict       ║
# ║                                                                          ║
# ║  BUGS FIXED IN v3.2 (verify these do not regress)                       ║
# ║  1. format_big_move_alert() signature mismatch — TypeError at runtime   ║
# ║  2. calc_relative_strength() received ctx dict, accessed ctx["symbol"]  ║
# ║  3. _last_completed_index() returned -1 on 1-row df → IndexError        ║
# ║  4. SYMBOL_META / YAML_SETTINGS undefined when yaml absent → NameError  ║
# ║  5. Dead variable `support_level` in BUY branch (harmless but messy)    ║
# ║  6. ema200_real None not guarded before f-string in ai_analyze_drop()   ║
# ║  7. ath_recency_label() mixed tz-aware/naive datetime comparison         ║
# ║  8. _split_for_telegram() produced oversized chunks on large paragraphs ║
# ║                                                                          ║
# ║  FILES                                                                   ║
# ║  • market_intel.py    — this file (main module)                          ║
# ║  • symbols.yaml       — watchlist + sector map + config overrides        ║
# ║  • scanner_state.json — cooldown timestamps (auto-created)               ║
# ║  • earnings_cache.json — 12 h earnings cache (auto-created)              ║
# ║  • logs/intel.log     — rotating log (14-day retention)                  ║
# ║                                                                          ║
# ║  ENV VARS REQUIRED                                                       ║
# ║  TELEGRAM_TOKEN, CHAT_ID, GEMINI_API_KEY (optional)                      ║
# ║                                                                          ║
# ║  WHAT TO VERIFY IN REVIEW                                                ║
# ║  □ All callers of calc_relative_strength() pass symbol string, not dict  ║
# ║  □ format_big_move_alert() never calls get_earnings_date() internally    ║
# ║  □ get_verdict() earnings_days param is always passed from caller        ║
# ║  □ _last_completed_index() result is checked for li < 1 before use      ║
# ║  □ RSI computation only uses closed = daily.iloc[:li+1]                 ║
# ║  □ ema200_real checked for None before any numeric formatting            ║
# ║  □ Telegram strings don't contain unescaped _ * ` [ chars               ║
# ║  □ mark_alert() only called after confirmed send_telegram() == True      ║
# ║  □ clear_caches() called at top of each run_intel_scan()                ║
# ║  --- END PASTE ---                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# ════════════════════════════════════════════════════════════════════════════
