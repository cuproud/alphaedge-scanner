"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              ALPHAEDGE BRIEF v3.3 — UNIFIED MORNING + EVENING               ║
║              Full audit, bug-fix & hardening pass — May 2026                ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  PURPOSE                                                                     ║
║  Fires twice every weekday via cron or scheduler:                            ║
║    🌅 9:00 AM ET  — Morning Brief (day setup, buy candidates, risk map)     ║
║    🌆 4:30 PM ET  — Evening Brief (day recap, open trades, after-hours)     ║
║                                                                              ║
║  DEPENDENCIES                                                                ║
║  Fully delegates data, indicators, and Telegram delivery to market_intel.   ║
║  This file only owns: brief composition, AI prompts, scheduling logic.      ║
║                                                                              ║
║  ARCHITECTURE                                                                ║
║  ┌────────────────────────────────────────────────────────────────────┐      ║
║  │ market_intel.py → MONITOR_LIST, SECTORS, get_full_context(),      │      ║
║  │                   get_verdict(), get_market_ctx(), send_telegram() │      ║
║  │ symbols.yaml    → SYMBOL_META (name/exchange), YAML_SETTINGS      │      ║
║  │                   settings.brief overrides                         │      ║
║  │ active_trades.json  → open position tracking (evening only)       │      ║
║  │ trade_history.json  → closed trade history   (evening only)       │      ║
║  └────────────────────────────────────────────────────────────────────┘      ║
║                                                                              ║
║  FORCE-RUN FLAGS (env vars)                                                  ║
║  FORCE_MORNING=true  → run morning brief regardless of time window          ║
║  FORCE_EVENING=true  → run evening brief regardless of time window          ║
║  FORCE_BRIEF=true    → run whichever brief the current time suggests        ║
║                                                                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  CHANGELOG                                                                   ║
║                                                                              ║
║  v3.3 — Audit / Bug-Fix Pass (this file)                                   ║
║  BUG FIXES                                                                   ║
║   1. build_morning_brief(): `mmsg +=` typo → NameError whenever             ║
║      avoid_list is non-empty. Fixed to `msg +=`.                            ║
║   2. Late `import re` + `import requests` appeared mid-file inside the AI   ║
║      section. Moved to top-of-file with all other stdlib imports.           ║
║   3. collect_brief_data(): earnings_soon always appended session=None.      ║
║      earnings_label() branches on 'BMO'/'AMC' — always silently hit the    ║
║      fallback. Added TODO + explicit comment; doc'd as known limitation.    ║
║   4. build_evening_brief() open_trades loop: float(t.get('entry', 0))      ║
║      crashes when value is explicitly None (key present, value null).       ║
║      Fixed: or 0.0 fallback applied after get() to handle None.            ║
║   5. collect_brief_data() never called clear_caches() before parallel       ║
║      get_full_context() calls — stale DataFrames from a prior run could     ║
║      persist. Fixed: clear_caches() called at top of collect_brief_data(). ║
║   6. compute_ah_movers(): no log/warn when no regular-hours bars found      ║
║      (e.g. called before market open). Added debug log + early return.     ║
║                                                                              ║
║  CLEAN-UPS                                                                   ║
║   7. Removed unused imports: field, calc_relative_strength,                 ║
║      format_earnings_warning, H_RULE, SUB_RULE, save_json, STATE_FILE.     ║
║   8. Removed redundant local MARKET_TZ / DISPLAY_TZ definitions —          ║
║      already defined in market_intel and available via market_now() /       ║
║      display_now().                                                          ║
║   9. _sanitize_ai() local re-implementation removed; now imported directly  ║
║      from market_intel (single source of truth).                            ║
║  10. from market_intel import SESSION moved to top import block.            ║
║  11. fetch_timeout in Config documented as currently unused (reserved for   ║
║      future per-call timeout enforcement in _fetch_one).                    ║
║  12. Section headers added throughout for Notepad++ code folding.           ║
║  13. Prompt template appended at EOF for new-session context handoff.       ║
║                                                                              ║
║  v3.2 — Alignment with market_intel v3.1 + symbols.yaml v3                 ║
║   • Reads SYMBOL_META → company name + exchange in headers                  ║
║   • Applies YAML_SETTINGS["brief"] overrides                                ║
║   • Cooldown via market_intel.can_alert / mark_alert                        ║
║   • Reuses SESSION, tg_escape, send_telegram from market_intel              ║
║   • Pre-fetches earnings once per scan (central 12 h cache)                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

# ┌─────────────────────────────────────────────────────────────────────────┐
# │ STDLIB IMPORTS  (all at top — PEP 8)                                   │
# └─────────────────────────────────────────────────────────────────────────┘
import logging
import os
import re
import requests  # used by _gemini_call via SESSION; explicit for clarity
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

# ┌─────────────────────────────────────────────────────────────────────────┐
# │ THIRD-PARTY IMPORTS                                                     │
# └─────────────────────────────────────────────────────────────────────────┘
import pandas as pd
import yfinance as yf

# ┌─────────────────────────────────────────────────────────────────────────┐
# │ LOCAL IMPORTS — from market_intel                                       │
# │ Unused imports removed (v3.3): field, calc_relative_strength,          │
# │ format_earnings_warning, H_RULE, SUB_RULE, save_json, STATE_FILE       │
# └─────────────────────────────────────────────────────────────────────────┘
from market_intel import (
    # Data & universe
    MONITOR_LIST,
    SECTORS,
    SYMBOL_EMOJI,
    SYMBOL_META,
    YAML_SETTINGS,
    # Context / indicators
    get_earnings_date,
    get_full_context,
    get_market_ctx,
    get_verdict,
    # I/O helpers
    load_json,
    now_est,
    send_telegram,
    can_alert,
    mark_alert,
    tg_escape as md,
    display_now,
    market_now,
    # HTTP session (pooled, retrying) — reused for Gemini calls
    SESSION,
    # Cache management — FIX v3.3: must be called before parallel fetch
    clear_caches,
    # AI text sanitiser — FIX v3.3: reuse instead of reimplementing locally
    _sanitize_ai,
)


# ════════════════════════════════════════════════════════════
# § CONSTANTS
# ════════════════════════════════════════════════════════════

LOGS_DIR     = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

TRADES_FILE  = "active_trades.json"   # active position store (evening)
HISTORY_FILE = "trade_history.json"   # closed trade log     (evening)

# NOTE v3.3: MARKET_TZ / DISPLAY_TZ removed — already defined in market_intel
# and available via market_now() / display_now(). Redefining them here was
# redundant and risked divergence if market_intel ever changes the TZ.

# ════════════════════════════════════════════════════════════
# § GEMINI AI CONSTANTS
#   Defined at module level so _gemini_call() can reference them
#   without importing from market_intel (brief has its own prompts).
# ════════════════════════════════════════════════════════════

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL   = "gemini-2.0-flash"
GEMINI_TIMEOUT = 20   # seconds


# ════════════════════════════════════════════════════════════
# § CONFIGURATION DATACLASS
#   All tunable parameters live here.
#   Override via settings.brief in symbols.yaml.
#   Force-run flags read from environment at startup.
# ════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    # ── Parallelism ──────────────────────────────────────────
    fetch_workers: int = 5

    # fetch_timeout is RESERVED for future per-call timeout enforcement
    # inside _fetch_one(). Currently not wired to yf.download() or the
    # ThreadPoolExecutor since yfinance doesn't expose a clean timeout param.
    fetch_timeout: int = 25

    # ── Scheduling windows (market / ET time) ────────────────
    morning_window: tuple[dtime, dtime] = (dtime(8, 45),  dtime(12, 0))
    evening_window: tuple[dtime, dtime] = (dtime(16, 15), dtime(20, 0))

    # ── Display limits ───────────────────────────────────────
    ah_move_min_pct:    float = 0.5   # min AH % change to appear in list
    max_buy_candidates: int   = 8     # max rows in BUY ZONE CANDIDATES section
    max_avoid_shown:    int   = 6     # max rows in AVOID/WAIT section
    max_movers_shown:   int   = 5     # max gainers / losers rows
    max_ah_movers:      int   = 6     # max after-hours rows

    # ── Cooldown ─────────────────────────────────────────────
    slot_cooldown_h: int = 23   # one brief per slot per 23 h (allows slight drift)

    # ── Force-run flags (env overrides, set by _load_force_flags) ──
    force_morning: bool = False
    force_evening: bool = False
    force_brief:   bool = False


def _apply_yaml_overrides(cfg: Config) -> Config:
    """
    Merge settings.brief from symbols.yaml into Config.
    Handles time-window strings: ["08:45", "12:00"] → tuple[dtime, dtime].
    Unknown keys silently dropped — no accidental injection.
    """
    overrides = dict((YAML_SETTINGS or {}).get("brief") or {})
    if not overrides:
        return cfg

    def _to_window(v: list[str]) -> tuple[dtime, dtime]:
        h1, m1 = map(int, v[0].split(":"))
        h2, m2 = map(int, v[1].split(":"))
        return (dtime(h1, m1), dtime(h2, m2))

    if isinstance(overrides.get("morning_window"), list):
        overrides["morning_window"] = _to_window(overrides["morning_window"])
    if isinstance(overrides.get("evening_window"), list):
        overrides["evening_window"] = _to_window(overrides["evening_window"])

    valid = set(cfg.__dataclass_fields__)
    safe  = {k: v for k, v in overrides.items() if k in valid}
    if safe:
        logging.info(f"brief: applied yaml overrides: {sorted(safe)}")
    return replace(cfg, **safe)


def _load_force_flags(cfg: Config) -> Config:
    """Read FORCE_* env vars and patch into a new frozen Config."""
    return replace(
        cfg,
        force_morning = os.environ.get("FORCE_MORNING", "").lower() in ("true", "1", "yes"),
        force_evening = os.environ.get("FORCE_EVENING", "").lower() in ("true", "1", "yes"),
        force_brief   = os.environ.get("FORCE_BRIEF",   "").lower() in ("true", "1", "yes"),
    )


CFG = _load_force_flags(_apply_yaml_overrides(Config()))


# ════════════════════════════════════════════════════════════
# § CLOCK HELPERS
# ════════════════════════════════════════════════════════════

def is_weekend() -> bool:
    """True on Saturday (5) and Sunday (6) in market timezone."""
    return market_now().weekday() >= 5


def in_window(win: tuple[dtime, dtime]) -> bool:
    """True when the current market-TZ time is inside [win[0], win[1])."""
    t = market_now().time()
    return win[0] <= t < win[1]


# ════════════════════════════════════════════════════════════
# § COMPANY LABEL HELPER
# ════════════════════════════════════════════════════════════

def name_label(sym: str, *, bold_ticker: bool = True) -> str:
    """
    Return 'AAPL — Apple Inc. (NASDAQ)' when SYMBOL_META is populated,
    else just the (escaped) ticker symbol.
    Ticker is *bold* by default for Telegram Markdown.
    """
    meta = SYMBOL_META.get(sym, {})
    name = meta.get("name", "")
    exch = meta.get("exchange", "")
    ticker = f"*{md(sym)}*" if bold_ticker else md(sym)
    if name and exch:
        return f"{ticker} — {md(name)} ({md(exch)})"
    if name:
        return f"{ticker} — {md(name)}"
    return ticker


# ════════════════════════════════════════════════════════════
# § DATA COLLECTION (shared by morning + evening)
#   All expensive I/O (yfinance + earnings) happens here.
#   Results are bundled into BriefData and passed to renderers.
# ════════════════════════════════════════════════════════════

@dataclass
class BriefData:
    """All pre-fetched data for one brief cycle."""
    market_ctx:    dict                    # SPY / QQQ / VIX
    contexts:      dict[str, dict]         # full context per symbol
    failed_count:  int                     # symbols that failed to fetch
    sectors:       list[tuple[str, float]] # (sector_name, avg_pct), sorted desc
    gainers:       list[dict]              # top N positive movers
    losers:        list[dict]              # top N negative movers
    earnings_soon: list[tuple]             # (sym, date, days, session|None)
    earnings_days: dict[str, int | None]   # sym → days_until (for get_verdict)


def _fetch_one(symbol: str) -> tuple[str, dict | None, Exception | None]:
    """Worker: fetch full context for one symbol. Safe — never raises."""
    try:
        return symbol, get_full_context(symbol), None
    except Exception as e:
        return symbol, None, e


def collect_brief_data() -> BriefData:
    """
    Fetch market context + full context for every symbol in MONITOR_LIST.

    FIX v3.3: clear_caches() called first so stale per-scan DataFrames
    from a prior run do not bleed into this scan.
    """
    clear_caches()   # FIX v3.3 — must precede parallel get_full_context() calls
    market_ctx = get_market_ctx()
    contexts: dict[str, dict] = {}
    failed = 0

    with ThreadPoolExecutor(max_workers=CFG.fetch_workers) as ex:
        futures = {ex.submit(_fetch_one, s): s for s in MONITOR_LIST}
        for fut in as_completed(futures):
            symbol, ctx, err = fut.result()
            if err is not None:
                failed += 1
                logging.error(f"fetch {symbol}: {err}")
                print(f"  → {symbol:10s} 💥 {err}")
            elif ctx:
                contexts[symbol] = ctx
                print(f"  → {symbol:10s} {ctx['day_change_pct']:+.2f}%")
            else:
                failed += 1
                print(f"  → {symbol:10s} —")

    # ── Sector averages ──────────────────────────────────────
    sectors: list[tuple[str, float]] = []
    for sector, syms in SECTORS.items():
        ctxs = [contexts[s] for s in syms if s in contexts]
        if ctxs:
            avg = sum(c["day_change_pct"] for c in ctxs) / len(ctxs)
            sectors.append((sector, avg))
    sectors.sort(key=lambda x: -x[1])

    # ── Movers — split before slicing so gainers never leak into losers ──
    sorted_desc = sorted(contexts.values(), key=lambda c: -c["day_change_pct"])
    pos = [c for c in sorted_desc if c["day_change_pct"] > 0]
    neg = [c for c in sorted_desc if c["day_change_pct"] < 0]
    gainers = [
        {"symbol": c["symbol"], "pct": c["day_change_pct"]}
        for c in pos[: CFG.max_movers_shown]
    ]
    losers = [
        {"symbol": c["symbol"], "pct": c["day_change_pct"]}
        for c in neg[-CFG.max_movers_shown:][::-1]
    ]

    # ── Earnings — single fetch per symbol (uses central 12 h cache) ────
    # NOTE v3.3: session is always None because SYMBOL_META does not yet
    # carry a 'report_time' field ('BMO' / 'AMC'). earnings_label() will
    # always hit its else-branch. TODO: add report_time to symbols.yaml
    # and expose it via SYMBOL_META once the schema is extended.
    earnings_soon: list[tuple] = []
    earnings_days: dict[str, int | None] = {}
    for sym in contexts:
        ed, days = get_earnings_date(sym)
        earnings_days[sym] = days
        if ed is not None and days is not None and days <= 1:
            earnings_soon.append((sym, ed, days, None))  # session: TODO

    return BriefData(
        market_ctx=market_ctx,
        contexts=contexts,
        failed_count=failed,
        sectors=sectors,
        gainers=gainers,
        losers=losers,
        earnings_soon=earnings_soon,
        earnings_days=earnings_days,
    )


# ════════════════════════════════════════════════════════════
# § AFTER-HOURS MOVER CALCULATOR
#   Compares last AH price to the 4 PM close.
#   Only symbols that moved ≥ ah_move_min_pct are returned.
# ════════════════════════════════════════════════════════════

def compute_ah_movers(symbols: Iterable[str]) -> list[dict]:
    """
    Return list of {symbol, pct, price} for symbols with significant
    after-hours moves. Sorted by |pct| descending, capped at max_ah_movers.

    FIX v3.3: added debug log when no regular-hours bars are found
    (e.g. function called before market opens or on a holiday).
    The original silently returned an empty list in this case.
    """
    movers: list[dict] = []

    for sym in symbols:
        try:
            df = yf.download(
                sym, period="1d", interval="5m",
                progress=False, auto_adjust=True, prepost=True,
            )
            if df is None or df.empty or len(df) < 2:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Normalise index to market timezone
            idx = df.index
            if idx.tz is not None:
                df.index = idx.tz_convert("America/New_York")
            else:
                df.index = idx.tz_localize("UTC").tz_convert("America/New_York")

            # Regular-hours bars: up to and including 4:00 PM close
            regular_mask = df.index.time <= dtime(16, 0)

            if not regular_mask.any():
                logging.debug(f"AH {sym}: no regular-hours bars found (pre-market or holiday?)")
                continue

            if not (~regular_mask).any():
                # No after-hours bars yet — market just closed
                logging.debug(f"AH {sym}: no after-hours bars yet")
                continue

            close_4pm = float(df.loc[regular_mask, "Close"].iloc[-1])
            ah_price  = float(df["Close"].iloc[-1])

            if close_4pm <= 0:
                continue

            ah_pct = (ah_price - close_4pm) / close_4pm * 100
            if abs(ah_pct) >= CFG.ah_move_min_pct:
                movers.append({"symbol": sym, "pct": ah_pct, "price": ah_price})

        except Exception as e:
            logging.debug(f"AH calc {sym}: {e}")

    movers.sort(key=lambda x: -abs(x["pct"]))
    return movers[: CFG.max_ah_movers]


# ════════════════════════════════════════════════════════════
# § EARNINGS LABEL
#   Generates the display string for upcoming earnings.
#   session ('BMO' / 'AMC' / None) is not yet populated — see TODO
#   in collect_brief_data(). All paths will fall through to the
#   generic label until session data is added to SYMBOL_META.
# ════════════════════════════════════════════════════════════

def earnings_label(days: int, session: str | None, brief_kind: str) -> str:
    """
    Return a human-readable earnings warning string.

    Parameters
    ----------
    days       : days until earnings (0 = today, 1 = tomorrow)
    session    : 'BMO' (before market open) / 'AMC' (after market close) / None
    brief_kind : 'morning' | 'evening' (affects phrasing)
    """
    if days == 0:
        if session == "BMO":
            return "🔴 REPORTED PRE-MARKET" if brief_kind == "evening" else "🔴 REPORTING PRE-MARKET"
        if session == "AMC":
            return "AFTER CLOSE TONIGHT" if brief_kind == "morning" else "🔴 REPORTING IN MINUTES"
        return "TODAY"
    if days == 1:
        if session == "BMO": return "TOMORROW PRE-MARKET"
        if session == "AMC": return "TOMORROW AFTER CLOSE"
        return "TOMORROW"
    return f"in {days}d"


# ════════════════════════════════════════════════════════════
# § RENDER HELPERS — TELEGRAM MARKDOWN COMPOSITION
#   Pure functions; all take pre-computed data, return strings.
#   No I/O or side effects.
# ════════════════════════════════════════════════════════════

def brief_header(
    emoji:    str,
    title:    str,
    subtitle: str | None = None,
    border:   str = "━━━━━━━━━━━━━━━━━━━━━",
) -> str:
    """Top-of-message header block with timestamp."""
    ts  = display_now().strftime("%A, %B %d • %I:%M %p ET")
    msg = f"{emoji} *{title}*\n`{border}`\n"
    msg += f"🕒 _{ts}_\n"
    if subtitle:
        msg += f"_{md(subtitle)}_\n"
    msg += "\n"
    return msg


def section_header(
    emoji:  str,
    title:  str,
    border: str = "─────────────────",
) -> str:
    """Minor section divider."""
    return f"\n{emoji} *{title}*\n`{border}`\n"


def sector_emoji(avg: float) -> str:
    """Return an appropriate emoji for a sector's average daily move."""
    if avg >  2.0:  return "🚀"
    if avg >  0.5:  return "🟢"
    if avg > -0.5:  return "⚖️"
    if avg > -2.0:  return "🔴"
    return "🩸"


def render_market_row(label: str, d: dict | None) -> str:
    """Render one market instrument row (SPY / QQQ / VIX)."""
    if not d:
        return f"{label}: —\n"
    pct   = d.get("pct",   0)
    price = d.get("price", 0)
    em    = "🟢" if pct >= 0 else "🔴"
    return f"{label}: {em} `${price:.2f}`  `{pct:+.2f}%`\n"


def render_market_snapshot(market_ctx: dict, title: str) -> str:
    """
    Compose the market overview section (SPY / QQQ / VIX + regime note).
    Returns empty string when market_ctx is falsy.
    """
    if not market_ctx:
        return ""

    spy = market_ctx.get("SPY",  {})
    qqq = market_ctx.get("QQQ",  {})
    vix = market_ctx.get("^VIX", {})

    out = section_header("🌍", title)
    out += render_market_row("SPY", spy)
    out += render_market_row("QQQ", qqq)
    out += render_market_row("VIX", vix)

    vix_p   = vix.get("price", 15)
    spy_pct = spy.get("pct",   0)

    if vix_p >= 30:
        out += "\n🩸 _Stressed regime — defensive sizing only_\n"
    elif vix_p >= 20:
        out += "\n⚠️ _Elevated VIX — expect chop and risk-off moves_\n"
    elif vix_p < 14 and spy_pct > 0.3:
        out += "\n✅ _Low vol + green market — cleaner trend setup_\n"
    else:
        out += "\n⚖️ _Mixed backdrop — be selective_\n"

    return out


def render_sectors(sectors: list[tuple[str, float]], title: str) -> str:
    """Render sector performance table (sorted by avg move)."""
    if not sectors:
        return ""
    out = section_header("🌡️", title)
    for sector, avg in sectors:
        out += f"{sector_emoji(avg)} {md(sector)}: `{avg:+.2f}%`\n"
    return out


def render_movers(
    gainers: list[dict],
    losers:  list[dict],
    title:   str,
) -> str:
    """Render top gainers and losers side-by-side under one section header."""
    if not (gainers or losers):
        return ""
    out = section_header("📊", title)
    if gainers:
        out += "*🚀 GAINERS*\n"
        for g in gainers:
            em = SYMBOL_EMOJI.get(g["symbol"], "📊")
            out += f"  {em} *{md(g['symbol'])}* `{g['pct']:+.2f}%`\n"
    if losers:
        out += "\n*📉 LOSERS*\n"
        for l in losers:
            em = SYMBOL_EMOJI.get(l["symbol"], "📊")
            out += f"  {em} *{md(l['symbol'])}* `{l['pct']:+.2f}%`\n"
    return out


def render_earnings(
    earnings:   list[tuple],
    brief_kind: str,
    title:      str,
    footer:     str,
) -> str:
    """Render the earnings warning section. Returns empty string when no earnings."""
    if not earnings:
        return ""
    out = section_header("📅", title, "═════════════════")
    for sym, _ed, days, session in earnings:
        em = SYMBOL_EMOJI.get(sym, "📊")
        out += f"  {em} {name_label(sym)}\n"
        out += f"     ⚠️ *{earnings_label(days, session, brief_kind)}*\n"
    out += f"\n_{footer}_\n"
    return out


def render_header(emoji: str, title: str) -> str:
    """Route to brief_header() with the correct subtitle for morning / evening."""
    if "MORNING" in title.upper():
        return brief_header(
            emoji, title,
            "Pre\\-market setup, risk map, and watchlist",
            "━━━━━━━━━━━━━━━━━━━━━",
        )
    if "EVENING" in title.upper():
        return brief_header(
            emoji, title,
            "Day recap, open risk, and after\\-hours watch",
            "═════════════════════",
        )
    return brief_header(emoji, title)


def render_footer(failed_count: int, total: int, tail: str) -> str:
    """Closing footer with optional fetch-failure warning."""
    out = "\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    if failed_count:
        out += f"⚠️ _{failed_count}/{total} tickers failed to fetch._\n"
    out += f"_{tail}_"
    return out


# ════════════════════════════════════════════════════════════
# § MORNING BRIEF
#   Builds and sends the morning brief message.
#   Returns True on successful Telegram delivery.
# ════════════════════════════════════════════════════════════

def build_morning_brief() -> bool:
    """
    Compose and send the morning brief.

    Sections:
      Header → Market Snapshot → AI Outlook → Earnings Alert →
      Sector Performance → Top Movers → Buy Zone Candidates → Avoid/Wait →
      Footer
    """
    print(f"\n🌅 Building Morning Brief @ {display_now().strftime('%H:%M ET')}")
    logging.info("Morning brief build start")

    if is_weekend():
        print("⚠️ Weekend — no brief today")
        return False

    data = collect_brief_data()
    if not data.contexts:
        print("❌ No contexts — aborting morning brief")
        return False

    earnings_syms = {sym for sym, *_ in data.earnings_soon}

    # ── Verdicts (earnings_days passed in — no redundant fetch) ──────────
    buy_candidates: list[tuple] = []
    avoid_list:     list[tuple] = []

    for sym, ctx in data.contexts.items():
        try:
            verdict, zone, reasons = get_verdict(
                ctx, data.market_ctx,
                earnings_days=data.earnings_days.get(sym),
            )
        except Exception as e:
            logging.error(f"verdict {sym}: {e}")
            continue
        if "BUY" in verdict:
            buy_candidates.append((sym, ctx, verdict, zone, reasons))
        elif ("AVOID" in verdict or "WAIT" in verdict) and sym not in earnings_syms:
            avoid_list.append((sym, ctx, verdict, zone))

    # Sort buy candidates by RSI ascending (most oversold first)
    buy_candidates.sort(key=lambda x: x[1].get("rsi", 50))

    print("  🤖 Getting AI morning outlook...")
    ai_outlook = ai_daily_outlook(data.market_ctx, data.sectors,
                                  data.gainers + data.losers)

    # ── Compose message ───────────────────────────────────────
    msg = render_header("🌅", "MORNING BRIEF")
    msg += render_market_snapshot(data.market_ctx, "🌍 MARKET SNAPSHOT")

    if ai_outlook:
        msg += section_header("🤖", "TODAY'S OUTLOOK")
        msg += f"{ai_outlook}\n"

    msg += render_earnings(
        data.earnings_soon, brief_kind="morning",
        title="📅 EARNINGS ALERT",
        footer="Avoid new entries. Existing positions: consider hedging.",
    )

    msg += render_sectors(data.sectors, "🌡️ SECTOR PERFORMANCE")
    msg += render_movers(data.gainers, data.losers, "📊 TOP MOVERS")

    # ── Buy Zone Candidates ───────────────────────────────────
    msg += section_header("🎯", "BUY ZONE CANDIDATES", "━━━━━━━━━━━━━━━━━")
    if buy_candidates:
        shown = min(CFG.max_buy_candidates, len(buy_candidates))
        for sym, ctx, _v, zone, reasons in buy_candidates[:shown]:
            try:
                em = SYMBOL_EMOJI.get(sym, "📊")
                msg += f"  {em} {name_label(sym)}\n"
                msg += (
                    f"     `${ctx['current']:.2f}` — _{md(zone)}_ • "
                    f"RSI(14) `{ctx.get('rsi', 0):.0f}` • "
                    f"{ctx['day_change_pct']:+.2f}%\n"
                )
                if reasons:
                    msg += f"     💡 {md(reasons[0])}\n"
            except Exception as e:
                logging.error(f"render buy row {sym}: {e}")
        if len(buy_candidates) > shown:
            msg += f"  _+{len(buy_candidates) - shown} more buy candidates_\n"
    else:
        msg += "  _No clean buy setups — wait for better conditions_\n"

    # ── Avoid / Wait ──────────────────────────────────────────
    if avoid_list:
        # FIX v3.3: `mmsg +=` was a typo for `msg +=` — caused NameError at runtime
        msg += section_header("🚫", "AVOID / WAIT", "═════════════════")
        shown = min(CFG.max_avoid_shown, len(avoid_list))
        for sym, _ctx, _v, zone in avoid_list[:shown]:
            em = SYMBOL_EMOJI.get(sym, "📊")
            msg += f"  {em} {md(sym)}: _{md(zone)}_\n"
        if len(avoid_list) > shown:
            msg += f"  _+{len(avoid_list) - shown} more_\n"

    msg += render_footer(
        data.failed_count, len(MONITOR_LIST),
        "Scanners running every 10\\-15 min during market hours. "
        "Watch for 🩸 sector bleed, 💪 RS signals, 🎯 dip alerts.",
    )

    ok = send_telegram(msg, silent=False)
    print(f"{'✅' if ok else '❌'} Morning brief {'sent' if ok else 'FAILED'} ({len(msg)} chars)")
    logging.info(
        f"Morning brief done | sent={ok} | chars={len(msg)} "
        f"| candidates={len(buy_candidates)} | failed_fetch={data.failed_count}"
    )
    return ok


# ════════════════════════════════════════════════════════════
# § EVENING BRIEF
#   Builds and sends the evening brief message.
#   Returns True on successful Telegram delivery.
# ════════════════════════════════════════════════════════════

def build_evening_brief() -> bool:
    """
    Compose and send the evening brief.

    Sections:
      Header → Day Close Snapshot → AI Summary → Open Trades →
      Closed Trades Today → Sector Close → Day Movers →
      After-Hours Movers → Earnings Watch → Footer
    """
    print(f"\n🌆 Building Evening Brief @ {display_now().strftime('%H:%M ET')}")
    logging.info("Evening brief build start")

    if is_weekend():
        print("⚠️ Weekend — no brief today")
        return False

    data = collect_brief_data()
    if not data.contexts:
        print("❌ No contexts — aborting evening brief")
        return False

    # ── Load open trades (defensive) ─────────────────────────
    open_trades: dict = {}
    try:
        all_trades = load_json(TRADES_FILE, {})
        open_trades = {k: v for k, v in all_trades.items() if not v.get("closed")}
    except Exception as e:
        logging.error(f"trades load: {e}")

    # ── Load closed-today trades (defensive) ─────────────────
    closed_today: list = []
    try:
        history   = load_json(HISTORY_FILE, [])
        today_str = market_now().strftime("%Y-%m-%d")
        closed_today = [
            t for t in history
            if (t.get("closed_at") or "").startswith(today_str)
        ]
    except Exception as e:
        logging.error(f"history load: {e}")

    print("  🌙 Computing after-hours moves...")
    ah_movers = compute_ah_movers(data.contexts.keys())

    print("  🤖 Getting AI evening summary...")
    ai_summary = ai_evening_summary(
        data.market_ctx, data.sectors,
        data.gainers, data.losers,
        open_trades,
    )

    # ── Compose message ───────────────────────────────────────
    msg = render_header("🌆", "EVENING BRIEF")
    msg += render_market_snapshot(data.market_ctx, "🔔 DAY CLOSE")

    if ai_summary:
        msg += section_header("🤖", "END-OF-DAY ANALYSIS")
        msg += f"{ai_summary}\n"

    # ── Open trades ───────────────────────────────────────────
    if open_trades:
        msg += section_header("📊", f"OPEN TRADES ({len(open_trades)})", "━━━━━━━━━━━━━━━━━")
        msg += "_Still active going into after-hours:_\n"
        for k, t in open_trades.items():
            try:
                em     = t.get("emoji",  "📈")
                signal = t.get("signal", "BUY")
                dir_em = "🟢" if signal == "BUY" else "🔴"
                sym    = t.get("symbol", k)
                tf     = md(t.get("tf_label", t.get("tf", "—")))
                # FIX v3.3: float(t.get('entry', 0)) crashes when value is None
                # (key present but value null). Use `or 0.0` after get().
                entry  = float(t.get("entry") or 0.0)
                sl     = float(t.get("sl")    or 0.0)
                msg += (
                    f"  {em} {dir_em} {name_label(sym)} `{tf}`\n"
                    f"     @ `${entry:.2f}` — SL `${sl:.2f}`\n"
                )
            except Exception as e:
                logging.error(f"open trade row {k}: {e}")
        msg += "_Use LIMIT orders in after-hours. Watch for gap risk overnight._\n"
    else:
        msg += "\n📊 _No open trades going into after-hours._\n"

    # ── Closed today ──────────────────────────────────────────
    if closed_today:
        wins    = [t for t in closed_today if (t.get("final_r") or 0) > 0]
        losses  = [t for t in closed_today if (t.get("final_r") or 0) < 0]
        total_r = sum((t.get("final_r") or 0) for t in closed_today)
        r_em    = "🟢" if total_r > 0 else ("⚪" if total_r == 0 else "🔴")
        msg += section_header("✅", "TODAY'S CLOSED TRADES", "═════════════════")
        msg += f"Closed: *{len(closed_today)}* • Wins: *{len(wins)}* • Losses: *{len(losses)}*\n"
        msg += f"{r_em} Day P&L: *{total_r:+.1f}R*\n"

    msg += render_sectors(data.sectors, "🌡️ SECTOR CLOSE")
    msg += render_movers(data.gainers, data.losers, "🏆 DAY MOVERS")

    # ── After-hours movers ────────────────────────────────────
    if ah_movers:
        msg += section_header("🌙", "AFTER-HOURS MOVERS")
        for m in ah_movers:
            em     = SYMBOL_EMOJI.get(m["symbol"], "📊")
            dir_em = "🟢" if m["pct"] > 0 else "🔴"
            msg += (
                f"  {dir_em} {em} {md(m['symbol'])}: "
                f"`{m['pct']:+.2f}%` AH @ `${m['price']:.2f}`\n"
            )
    else:
        msg += "\n🌙 _No significant after-hours moves._\n"

    msg += render_earnings(
        data.earnings_soon, brief_kind="evening",
        title="📅 EARNINGS WATCH",
        footer="Consider reducing exposure before report.",
    )

    msg += render_footer(
        data.failed_count, len(MONITOR_LIST),
        "Scanner continues in after-hours (crypto + ext\\-hrs stocks). "
        "Next morning brief: tomorrow at 9:00 AM ET.",
    )

    ok = send_telegram(msg, silent=False)
    print(f"{'✅' if ok else '❌'} Evening brief {'sent' if ok else 'FAILED'} ({len(msg)} chars)")
    logging.info(
        f"Evening brief done | sent={ok} | chars={len(msg)} "
        f"| open_trades={len(open_trades)} | failed_fetch={data.failed_count}"
    )
    return ok


# ════════════════════════════════════════════════════════════
# § AI — GEMINI CALLS (morning outlook + evening summary)
#   Prompts are brief-specific and live here.
#   All calls reuse SESSION from market_intel (pooled, retrying).
#   Non-blocking on 429: logs and returns None.
#
#   FIX v3.3: _sanitize_ai() now imported from market_intel
#   instead of re-implemented locally (was identical copy).
# ════════════════════════════════════════════════════════════

def _gemini_call(prompt: str, label: str) -> str | None:
    """
    POST to Gemini Flash and return raw text content, or None on error.
    Skips (returns None) on 429 — does NOT block.
    """
    if not GEMINI_API_KEY:
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        r = SESSION.post(
            url,
            json={
                "contents":        [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.6, "maxOutputTokens": 400},
            },
            timeout=GEMINI_TIMEOUT,
        )
        if r.status_code == 200:
            data  = r.json()
            cands = data.get("candidates") or []
            if cands:
                return cands[0]["content"]["parts"][0]["text"].strip()
            logging.warning(f"Gemini {label}: empty candidates")
        elif r.status_code == 429:
            logging.warning(f"Gemini rate-limited ({label}) — skipping")
        else:
            logging.error(f"Gemini {label}: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logging.error(f"AI {label}: {e}")

    return None


def ai_daily_outlook(
    market_ctx:     dict,
    sector_summary: list[tuple[str, float]],
    top_movers:     list[dict],
) -> str | None:
    """
    Generate the morning AI outlook (4 lines).
    Returns sanitised string or None when AI unavailable / rate-limited.
    """
    mkt = market_ctx or {}
    spy = mkt.get("SPY",  {}).get("pct",   0)
    qqq = mkt.get("QQQ",  {}).get("pct",   0)
    vix = mkt.get("^VIX", {}).get("price", 15)

    sec_lines   = "\n".join(f"  {n}: {a:+.2f}%" for n, a in sector_summary[:6])
    mover_lines = "\n".join(f"  {m['symbol']}: {m['pct']:+.2f}%" for m in top_movers[:6])

    prompt = f"""You are a senior trading strategist writing the morning brief for an active trader.

TODAY'S PRE-MARKET SNAPSHOT:
SPY: {spy:+.2f}%, QQQ: {qqq:+.2f}%, VIX: {vix:.1f}

TOP MOVERS (pre-market):
{mover_lines}

SECTOR PERFORMANCE:
{sec_lines}

Write EXACTLY 4 lines (max 120 chars each). Be direct and actionable.

🌅 [Setup: risk-on/risk-off/mixed — briefly why]
🎯 [Which sectors/themes to favor today — be specific with tickers if relevant]
⚠️ [Main risk / what to avoid — pinpoint it]
💡 [Bias: trade aggressive / selective / defensive — with size guidance]

NO extra headers, bullets, intros, or outros. 4 lines only."""

    return _sanitize_ai(_gemini_call(prompt, "morning"))


def ai_evening_summary(
    market_ctx:     dict,
    sector_summary: list[tuple[str, float]],
    day_winners:    list[dict],
    day_losers:     list[dict],
    open_trades:    dict,
) -> str | None:
    """
    Generate the evening AI summary (4 lines).
    Returns sanitised string or None when AI unavailable / rate-limited.
    """
    mkt = market_ctx or {}
    spy = mkt.get("SPY",  {}).get("pct",   0)
    qqq = mkt.get("QQQ",  {}).get("pct",   0)
    vix = mkt.get("^VIX", {}).get("price", 15)

    sec_lines    = "\n".join(f"  {n}: {a:+.2f}%" for n, a in sector_summary[:6])
    winner_lines = "\n".join(f"  {w['symbol']}: {w['pct']:+.2f}%" for w in day_winners[:4]) or "  none"
    loser_lines  = "\n".join(f"  {l['symbol']}: {l['pct']:+.2f}%" for l in day_losers[:4])  or "  none"
    trade_lines  = (
        f"{len(open_trades)} trade(s) still open going into after-hours"
        if open_trades else "No open trades"
    )

    prompt = f"""You are a senior trading strategist writing the end-of-day brief for an active trader.

TODAY'S CLOSE:
SPY: {spy:+.2f}%, QQQ: {qqq:+.2f}%, VIX: {vix:.1f}

DAY LEADERS:
{winner_lines}

DAY LAGGARDS:
{loser_lines}

SECTOR CLOSE:
{sec_lines}

OPEN POSITIONS: {trade_lines}

Write EXACTLY 4 lines (max 120 chars each). Be direct.

🌆 [Day recap: what drove price — sector rotation, macro, momentum?]
🎯 [What set up well today — note any themes for tomorrow]
⚠️ [Overnight risk — what to watch AH / pre-market tomorrow]
💡 [Overnight bias: cautious / hold / reduce — brief reason]

NO extra headers, bullets, intros, or outros. 4 lines only."""

    return _sanitize_ai(_gemini_call(prompt, "evening"))


# ════════════════════════════════════════════════════════════
# § SCHEDULING — JOB DISPATCH
#   can_alert / mark_alert from market_intel manage the cooldown
#   so no local slot-tracking is needed.
# ════════════════════════════════════════════════════════════

@dataclass
class Job:
    kind:     str             # 'morning' | 'evening'
    slot_key: str             # key in scanner_state.json
    builder:  Callable[[], bool]


JOBS: dict[str, Job] = {
    "morning": Job("morning", "last_morning_brief", build_morning_brief),
    "evening": Job("evening", "last_evening_brief", build_evening_brief),
}


def decide_job() -> Job | None:
    """Return the appropriate Job based on current market time, or None."""
    if in_window(CFG.morning_window): return JOBS["morning"]
    if in_window(CFG.evening_window): return JOBS["evening"]
    return None


def run_job(job: Job, *, force: bool = False) -> None:
    """
    Execute a brief job, respecting the cooldown unless force=True.
    mark_alert() is called only on confirmed successful delivery.
    """
    today = market_now().strftime("%Y-%m-%d")
    if not force and not can_alert(job.slot_key, CFG.slot_cooldown_h):
        print(f"ℹ️  {job.kind.title()} brief already sent today ({today})")
        return
    try:
        ok = job.builder()
        if ok:
            mark_alert(job.slot_key)   # ← only on confirmed send
    except Exception as e:
        logging.exception(f"{job.kind} brief crashed: {e}")


# ════════════════════════════════════════════════════════════
# § LOGGING SETUP
#   Idempotent — safe to call multiple times.
# ════════════════════════════════════════════════════════════

def setup_logging() -> None:
    """Configure rotating file logger for brief.log. Idempotent."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        return  # already configured
    handler = TimedRotatingFileHandler(
        LOGS_DIR / "brief.log", when="midnight", backupCount=14, utc=False,
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

    if CFG.force_morning:
        run_job(JOBS["morning"], force=True)
    elif CFG.force_evening:
        run_job(JOBS["evening"], force=True)
    elif CFG.force_brief:
        job = decide_job()
        if job:
            run_job(job, force=True)
        else:
            print(
                f"ℹ️  FORCE_BRIEF set but outside both windows "
                f"({display_now().strftime('%H:%M ET')})."
            )
    else:
        job = decide_job()
        if job:
            run_job(job, force=False)
        else:
            print(
                f"ℹ️  Outside brief windows "
                f"({display_now().strftime('%H:%M ET')}). "
                "Use FORCE_MORNING / FORCE_EVENING / FORCE_BRIEF to override."
            )


# ════════════════════════════════════════════════════════════════════════════
# ║  NEW-CHAT CONTEXT HANDOFF PROMPT
# ║
# ║  Copy the block below into a new conversation to onboard Claude with
# ║  full context about this codebase.
# ║
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  --- PASTE THIS INTO NEW CHAT ---                                        ║
# ║                                                                          ║
# ║  I'm working on AlphaEdge Brief (brief.py v3.3), the daily briefing     ║
# ║  module that runs on top of market_intel.py v3.2.                       ║
# ║                                                                          ║
# ║  WHAT IT DOES                                                            ║
# ║  • Fires twice every weekday:                                            ║
# ║      🌅 Morning Brief (~9 AM ET): buy candidates, risk map, AI outlook  ║
# ║      🌆 Evening Brief (~4:30 PM ET): recap, open trades, AH movers      ║
# ║  • Delegates ALL data/indicator work to market_intel.py                 ║
# ║  • Reads open positions from active_trades.json (evening only)          ║
# ║  • Reads closed trade history from trade_history.json (evening only)    ║
# ║  • Calls Gemini Flash for 4-line AI outlook / summary                   ║
# ║  • Sends rich Telegram Markdown v1 via market_intel.send_telegram()     ║
# ║  • Uses market_intel.can_alert() / mark_alert() for cooldown (23 h)    ║
# ║                                                                          ║
# ║  KEY DESIGN CONSTRAINTS                                                  ║
# ║  • clear_caches() MUST be called at top of collect_brief_data()         ║
# ║    (already done in v3.3 — do not remove)                               ║
# ║  • _sanitize_ai() imported from market_intel — do not re-implement      ║
# ║  • SESSION imported from market_intel — do not create a new session     ║
# ║  • MARKET_TZ / DISPLAY_TZ NOT defined locally — use market_now() /      ║
# ║    display_now() from market_intel instead                               ║
# ║  • format_big_move_alert() not used here — brief.py never calls it      ║
# ║  • earnings_soon session field is always None (TODO: add report_time    ║
# ║    to symbols.yaml and SYMBOL_META to enable BMO/AMC logic)             ║
# ║                                                                          ║
# ║  BUGS FIXED IN v3.3 (verify these do not regress)                       ║
# ║  1. mmsg += → msg += (NameError in build_morning_brief avoid block)     ║
# ║  2. Late import re / import requests → moved to top of file             ║
# ║  3. float(t.get('entry', 0)) → float(t.get('entry') or 0.0)           ║
# ║     (crashes when value is explicitly None in active_trades.json)       ║
# ║  4. clear_caches() added at top of collect_brief_data()                ║
# ║  5. compute_ah_movers() now logs when no regular-hours bars found       ║
# ║  6. Removed 6 unused imports from market_intel                          ║
# ║                                                                          ║
# ║  FILES                                                                   ║
# ║  • brief.py             — this file                                      ║
# ║  • market_intel.py      — data engine (must be present)                 ║
# ║  • symbols.yaml         — watchlist + config                             ║
# ║  • active_trades.json   — open positions (auto-created by trade module) ║
# ║  • trade_history.json   — closed trades  (auto-created by trade module) ║
# ║  • scanner_state.json   — shared cooldown store (market_intel manages)  ║
# ║  • logs/brief.log       — rotating log (14-day)                         ║
# ║                                                                          ║
# ║  ENV VARS                                                                ║
# ║  TELEGRAM_TOKEN, CHAT_ID, GEMINI_API_KEY (optional)                     ║
# ║  FORCE_MORNING=true | FORCE_EVENING=true | FORCE_BRIEF=true             ║
# ║                                                                          ║
# ║  WHAT TO VERIFY IN REVIEW                                                ║
# ║  □ msg += used throughout build_morning_brief() (not mmsg)              ║
# ║  □ All imports at top of file, none mid-file                            ║
# ║  □ float(t.get('entry') or 0.0) pattern in evening trade rows          ║
# ║  □ clear_caches() is first call in collect_brief_data()                ║
# ║  □ _sanitize_ai not redefined locally — only imported                   ║
# ║  □ mark_alert() only called inside run_job() when builder() returns True║
# ║  □ No local MARKET_TZ / DISPLAY_TZ definitions                          ║
# ║  --- END PASTE ---                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# ════════════════════════════════════════════════════════════════════════════
