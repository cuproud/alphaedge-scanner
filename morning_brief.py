"""
ALPHAEDGE BRIEF v3.2 — UNIFIED BUILD
═══════════════════════════════════════════════════════════════
Fires twice every weekday:
  🌅 9:00 AM ET  — Morning Brief (day setup)
  🌆 4:30 PM ET  — Evening Brief (day recap + after-hours watch)

v3.2 vs v3.1 — alignment with market_intel v3.1 + symbols.yaml v3
────────────────────────────────────────────────────────────────
• Reads SYMBOL_META → company name + exchange in every header
  (e.g. "AAPL — Apple Inc. (NASDAQ)") — fulfils original spec
• Applies YAML_SETTINGS["brief"] overrides at startup
• Cooldown unified through market_intel.can_alert / mark_alert
  (drops local claim_slot/release_slot, drops local earnings cache)
• Reuses market_intel SESSION, tg_escape, send_telegram (auto-split)
• Pre-fetches earnings once per scan (uses central 12h cache)
• Display TZ = America/Toronto; market TZ = America/New_York
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, time as dtime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from market_intel import (
    MONITOR_LIST, SECTORS, STATE_FILE, SYMBOL_EMOJI,
    SYMBOL_META, YAML_SETTINGS,
    calc_relative_strength, format_earnings_warning,
    get_earnings_date,
    get_full_context, get_market_ctx, get_verdict,
    load_json, now_est, save_json, send_telegram,
    can_alert, mark_alert,
    tg_escape as md,
    display_now, market_now,
    H_RULE, SUB_RULE,
)

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════

MARKET_TZ  = ZoneInfo("America/New_York")
DISPLAY_TZ = ZoneInfo("America/Toronto")

LOGS_DIR     = Path("logs"); LOGS_DIR.mkdir(exist_ok=True)
TRADES_FILE  = "active_trades.json"
HISTORY_FILE = "trade_history.json"


@dataclass(frozen=True)
class Config:
    fetch_workers:  int = 5
    fetch_timeout:  int = 25

    morning_window: tuple[dtime, dtime] = (dtime(8, 45),  dtime(12, 0))
    evening_window: tuple[dtime, dtime] = (dtime(16, 15), dtime(20, 0))

    ah_move_min_pct:    float = 0.5
    max_buy_candidates: int   = 8
    max_avoid_shown:    int   = 6
    max_movers_shown:   int   = 5
    max_ah_movers:      int   = 6

    # Slot cooldown — one brief per 24h per slot key
    slot_cooldown_h:    int   = 23

    # Force flags (read at startup from env)
    force_morning: bool = False
    force_evening: bool = False
    force_brief:   bool = False


def _apply_yaml_overrides(cfg: Config) -> Config:
    """Apply settings.brief from symbols.yaml, converting time strings."""
    overrides = dict((YAML_SETTINGS or {}).get("brief") or {})
    if not overrides:
        return cfg

    def _to_window(v):
        h1, m1 = map(int, v[0].split(":"))
        h2, m2 = map(int, v[1].split(":"))
        return (dtime(h1, m1), dtime(h2, m2))

    if isinstance(overrides.get("morning_window"), list):
        overrides["morning_window"] = _to_window(overrides["morning_window"])
    if isinstance(overrides.get("evening_window"), list):
        overrides["evening_window"] = _to_window(overrides["evening_window"])

    valid = {f.name for f in cfg.__dataclass_fields__.values()}
    safe  = {k: v for k, v in overrides.items() if k in valid}
    if safe:
        logging.info(f"brief: applied yaml overrides: {sorted(safe)}")
    return replace(cfg, **safe)


def _load_force_flags(cfg: Config) -> Config:
    return replace(
        cfg,
        force_morning = os.environ.get("FORCE_MORNING", "").lower() in ("true", "1", "yes"),
        force_evening = os.environ.get("FORCE_EVENING", "").lower() in ("true", "1", "yes"),
        force_brief   = os.environ.get("FORCE_BRIEF",   "").lower() in ("true", "1", "yes"),
    )

CFG = _load_force_flags(_apply_yaml_overrides(Config()))


# ════════════════════════════════════════════════════════════
# CLOCK HELPERS
# ════════════════════════════════════════════════════════════

def is_weekend() -> bool:
    return market_now().weekday() >= 5

def in_window(win: tuple[dtime, dtime]) -> bool:
    t = market_now().time()
    return win[0] <= t < win[1]


# ════════════════════════════════════════════════════════════
# COMPANY NAME / EXCHANGE LABEL
# ════════════════════════════════════════════════════════════

def name_label(sym: str, *, bold_ticker: bool = True) -> str:
    """
    Returns 'AAPL — Apple Inc. (NASDAQ)' if metadata available,
    else just the (escaped) ticker. Ticker is bolded by default.
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
# DATA COLLECTION (shared by morning + evening)
# ════════════════════════════════════════════════════════════

@dataclass
class BriefData:
    market_ctx:     dict
    contexts:       dict[str, dict]
    failed_count:   int
    sectors:        list[tuple[str, float]]
    gainers:        list[dict]
    losers:         list[dict]
    earnings_soon:  list[tuple]   # (sym, date, days, session)
    earnings_days:  dict[str, int | None]


def _fetch_one(symbol: str) -> tuple[str, dict | None, Exception | None]:
    try:
        return symbol, get_full_context(symbol), None
    except Exception as e:
        return symbol, None, e


def collect_brief_data() -> BriefData:
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

    # Sectors
    sectors: list[tuple[str, float]] = []
    for sector, syms in SECTORS.items():
        ctxs = [contexts[s] for s in syms if s in contexts]
        if ctxs:
            avg = sum(c["day_change_pct"] for c in ctxs) / len(ctxs)
            sectors.append((sector, avg))
    sectors.sort(key=lambda x: -x[1])

    # Movers — split BEFORE slicing so we never leak gainers into losers
    sorted_desc = sorted(contexts.values(), key=lambda c: -c["day_change_pct"])
    pos = [c for c in sorted_desc if c["day_change_pct"] > 0]
    neg = [c for c in sorted_desc if c["day_change_pct"] < 0]
    gainers = [{"symbol": c["symbol"], "pct": c["day_change_pct"]}
               for c in pos[: CFG.max_movers_shown]]
    losers  = [{"symbol": c["symbol"], "pct": c["day_change_pct"]}
               for c in neg[-CFG.max_movers_shown:][::-1]]

    # Earnings — single fetch per symbol (uses central 12h cache)
    earnings_soon: list[tuple] = []
    earnings_days: dict[str, int | None] = {}
    for sym in contexts:
        ed, days = get_earnings_date(sym)
        earnings_days[sym] = days
        if ed and days is not None and days <= 1:
            # session metadata not yet exposed by market_intel — pass None
            earnings_soon.append((sym, ed, days, None))

    return BriefData(
        market_ctx=market_ctx, contexts=contexts, failed_count=failed,
        sectors=sectors, gainers=gainers, losers=losers,
        earnings_soon=earnings_soon, earnings_days=earnings_days,
    )


# ════════════════════════════════════════════════════════════
# AFTER-HOURS — TIMESTAMP-BASED (not bar count)
# ════════════════════════════════════════════════════════════

def compute_ah_movers(symbols: Iterable[str]) -> list[dict]:
    movers = []
    for sym in symbols:
        try:
            df = yf.download(sym, period="1d", interval="5m",
                             progress=False, auto_adjust=True, prepost=True)
            if df is None or df.empty or len(df) < 2:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            idx = df.index
            df.index = (idx.tz_convert(MARKET_TZ) if idx.tz is not None
                        else idx.tz_localize("UTC").tz_convert(MARKET_TZ))

            regular_mask = df.index.time <= dtime(16, 0)
            if not regular_mask.any() or not (~regular_mask).any():
                continue  # no AH bars yet OR no regular bars

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
# EARNINGS LABELING (BMO/AMC + brief-kind aware)
# ════════════════════════════════════════════════════════════

def earnings_label(days: int, session: str | None, brief_kind: str) -> str:
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
# RENDER HELPERS — VISUAL TELEGRAM BRIEFS ONLY
# ════════════════════════════════════════════════════════════

def brief_header(emoji: str, title: str, subtitle: str | None = None,
                 border: str = "━━━━━━━━━━━━━━━━━━━━━") -> str:
    ts = display_now().strftime("%A, %B %d • %I:%M %p ET")
    msg = f"{emoji} *{title}*\n`{border}`\n"
    msg += f"🕒 _{ts}_\n"
    if subtitle:
        msg += f"_{md(subtitle)}_\n"
    msg += "\n"
    return msg


def section_header(emoji: str, title: str,
                   border: str = "─────────────────") -> str:
    return f"\n{emoji} *{title}*\n`{border}`\n"


def sector_emoji(avg: float) -> str:
    if avg > 2:
        return "🚀"
    if avg > 0.5:
        return "🟢"
    if avg > -0.5:
        return "⚖️"
    if avg > -2:
        return "🔴"
    return "🩸"


def render_market_row(label: str, d: dict | None) -> str:
    if not d:
        return f"{label}: —\n"

    pct = d.get("pct", 0)
    price = d.get("price", 0)

    em = "🟢" if pct >= 0 else "🔴"
    return f"{label}: {em} `${price:.2f}`  `{pct:+.2f}%`\n"


def render_market_snapshot(market_ctx: dict, title: str) -> str:
    if not market_ctx:
        return ""

    spy = market_ctx.get("SPY", {})
    qqq = market_ctx.get("QQQ", {})
    vix = market_ctx.get("^VIX", {})

    out = section_header("🌍", title)

    out += render_market_row("SPY", spy)
    out += render_market_row("QQQ", qqq)
    out += render_market_row("VIX", vix)

    vix_p = vix.get("price", 15)
    spy_pct = spy.get("pct", 0)

    if vix_p >= 30:
        out += "\n🩸 _Stressed regime — defensive sizing only_\n"
    elif vix_p >= 20:
        out += "\n⚠️ _Elevated VIX — expect chop and risk-off moves_\n"
    elif vix_p < 14 and spy_pct > 0.3:
        out += "\n✅ _Low vol + green market — cleaner trend setup_\n"
    else:
        out += "\n⚖️ _Mixed backdrop — be selective_\n"

    return out


def render_sectors(sectors, title: str) -> str:
    if not sectors:
        return ""

    out = section_header("🌡️", title)

    for sector, avg in sectors:
        out += f"{sector_emoji(avg)} {md(sector)}: `{avg:+.2f}%`\n"

    return out


def render_movers(gainers, losers, title: str) -> str:
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


def render_earnings(earnings, brief_kind: str, title: str, footer: str) -> str:
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
    if "MORNING" in title.upper():
        return brief_header(
            emoji,
            title,
            "Pre-market setup, risk map, and watchlist",
            "━━━━━━━━━━━━━━━━━━━━━",
        )

    if "EVENING" in title.upper():
        return brief_header(
            emoji,
            title,
            "Day recap, open risk, and after-hours watch",
            "═════════════════════",
        )

    return brief_header(emoji, title)


def render_footer(failed_count: int, total: int, tail: str) -> str:
    out = f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"

    if failed_count:
        out += f"⚠️ _{failed_count}/{total} tickers failed to fetch._\n"

    out += f"_{tail}_"
    return out

# ════════════════════════════════════════════════════════════
# MORNING BRIEF
# ════════════════════════════════════════════════════════════

def build_morning_brief() -> bool:
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

    # Verdicts (earnings_days passed in — no redundant fetch)
    buy_candidates, avoid_list = [], []
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
    buy_candidates.sort(key=lambda x: x[1].get("rsi", 50))

    print("  🤖 Getting AI morning outlook...")
    ai_outlook = ai_daily_outlook(data.market_ctx, data.sectors,
                                  data.gainers + data.losers)

    # ── compose ──
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

    # Buy candidates — with company name + exchange
    msg += section_header("🎯", "BUY ZONE CANDIDATES", "━━━━━━━━━━━━━━━━━")
    if buy_candidates:
        shown = min(CFG.max_buy_candidates, len(buy_candidates))
        for sym, ctx, _v, zone, reasons in buy_candidates[:shown]:
            try:
                em = SYMBOL_EMOJI.get(sym, "📊")
                msg += f"  {em} {name_label(sym)}\n"
                msg += (f"     `${ctx['current']:.2f}` — _{md(zone)}_ • "
                        f"RSI(14) `{ctx.get('rsi', 0):.0f}` • "
                        f"{ctx['day_change_pct']:+.2f}%\n")
                if reasons:
                    msg += f"     💡 {md(reasons[0])}\n"
            except Exception as e:
                logging.error(f"render buy row {sym}: {e}")
        if len(buy_candidates) > shown:
            msg += f"  _+{len(buy_candidates) - shown} more buy candidates_\n"
    else:
        msg += "  _No clean buy setups — wait for better conditions_\n"

    # Avoid
    if avoid_list:
        mmsg += section_header("🚫", "AVOID / WAIT", "═════════════════")
        shown = min(CFG.max_avoid_shown, len(avoid_list))
        for sym, _ctx, _v, zone in avoid_list[:shown]:
            em = SYMBOL_EMOJI.get(sym, "📊")
            msg += f"  {em} {md(sym)}: _{md(zone)}_\n"
        if len(avoid_list) > shown:
            msg += f"  _+{len(avoid_list) - shown} more_\n"

    msg += render_footer(
        data.failed_count, len(MONITOR_LIST),
        "Scanners running every 10–15 min during market hours. "
        "Watch for 🩸 sector bleed, 💪 RS signals, 🎯 dip alerts.",
    )

    ok = send_telegram(msg, silent=False)
    print(f"{'✅' if ok else '❌'} Morning brief {'sent' if ok else 'FAILED'} ({len(msg)} chars)")
    logging.info(f"Morning brief done | sent={ok} | chars={len(msg)} "
                 f"| candidates={len(buy_candidates)} | failed_fetch={data.failed_count}")
    return ok


# ════════════════════════════════════════════════════════════
# EVENING BRIEF
# ════════════════════════════════════════════════════════════

def build_evening_brief() -> bool:
    print(f"\n🌆 Building Evening Brief @ {display_now().strftime('%H:%M ET')}")
    logging.info("Evening brief build start")

    if is_weekend():
        print("⚠️ Weekend — no brief today")
        return False

    data = collect_brief_data()
    if not data.contexts:
        print("❌ No contexts — aborting evening brief")
        return False

    # Open trades — defensive load
    open_trades: dict = {}
    try:
        all_trades = load_json(TRADES_FILE, {})
        open_trades = {k: v for k, v in all_trades.items() if not v.get("closed")}
    except Exception as e:
        logging.error(f"trades load: {e}")

    # Closed-today
    closed_today: list = []
    try:
        history = load_json(HISTORY_FILE, [])
        today_str = market_now().strftime("%Y-%m-%d")
        closed_today = [t for t in history if (t.get("closed_at") or "").startswith(today_str)]
    except Exception as e:
        logging.error(f"history load: {e}")

    print("  🌙 Computing after-hours moves...")
    ah_movers = compute_ah_movers(data.contexts.keys())

    print("  🤖 Getting AI evening summary...")
    ai_summary = ai_evening_summary(
        data.market_ctx, data.sectors, data.gainers, data.losers, open_trades,
    )

    # ── compose ──
    msg = render_header("🌆", "EVENING BRIEF")
    msg += render_market_snapshot(data.market_ctx, "🔔 DAY CLOSE")

    if ai_summary:
        msg += section_header("🤖", "END-OF-DAY ANALYSIS")
        msg += f"{ai_summary}\n"

    # Open trades — per-row try/except
    if open_trades:
        msg += section_header("📊", f"OPEN TRADES ({len(open_trades)})", "━━━━━━━━━━━━━━━━━")
        msg += "_Still active going into after-hours:_\n"
        for k, t in open_trades.items():
            try:
                em      = t.get("emoji", "📈")
                signal  = t.get("signal", "BUY")
                dir_em  = "🟢" if signal == "BUY" else "🔴"
                sym     = t.get("symbol", k)
                tf      = md(t.get("tf_label", t.get("tf", "—")))
                entry   = float(t.get("entry", 0))
                sl      = float(t.get("sl", 0))
                msg += (f"  {em} {dir_em} {name_label(sym)} `{tf}`\n"
                        f"     @ `${entry:.2f}` — SL `${sl:.2f}`\n")
            except Exception as e:
                logging.error(f"open trade row {k}: {e}")
        msg += "_Use LIMIT orders in after-hours. Watch for gap risk overnight._\n"
    else:
        msg += "\n📊 _No open trades going into after-hours._\n"

    # Closed today summary
    if closed_today:
        wins   = [t for t in closed_today if (t.get("final_r") or 0) > 0]
        losses = [t for t in closed_today if (t.get("final_r") or 0) < 0]
        total_r = sum((t.get("final_r") or 0) for t in closed_today)
        r_em = "🟢" if total_r > 0 else ("⚪" if total_r == 0 else "🔴")
        msg += section_header("✅", "TODAY'S CLOSED TRADES", "═════════════════")
        msg += f"Closed: *{len(closed_today)}* • Wins: *{len(wins)}* • Losses: *{len(losses)}*\n"
        msg += f"{r_em} Day P&L: *{total_r:+.1f}R*\n"

    msg += render_sectors(data.sectors, "🌡️ SECTOR CLOSE")
    msg += render_movers(data.gainers, data.losers, "🏆 DAY MOVERS")

    # AH movers
    if ah_movers:
        msg += section_header("🌙", "AFTER-HOURS MOVERS")
        for m in ah_movers:
            em     = SYMBOL_EMOJI.get(m["symbol"], "📊")
            dir_em = "🟢" if m["pct"] > 0 else "🔴"
            msg += (f"  {dir_em} {em} {md(m['symbol'])}: "
                    f"`{m['pct']:+.2f}%` AH @ `${m['price']:.2f}`\n")
    else:
        msg += "\n🌙 _No significant after-hours moves._\n"

    msg += render_earnings(
        data.earnings_soon, brief_kind="evening",
        title="📅 EARNINGS WATCH",
        footer="Consider reducing exposure before report.",
    )

    msg += render_footer(
        data.failed_count, len(MONITOR_LIST),
        "Scanner continues in after-hours (crypto + ext-hrs stocks). "
        "Next morning brief: tomorrow at 9:00 AM ET.",
    )

    ok = send_telegram(msg, silent=False)
    print(f"{'✅' if ok else '❌'} Evening brief {'sent' if ok else 'FAILED'} ({len(msg)} chars)")
    logging.info(f"Evening brief done | sent={ok} | chars={len(msg)} "
                 f"| open_trades={len(open_trades)} | failed_fetch={data.failed_count}")
    return ok


# ════════════════════════════════════════════════════════════
# AI — MORNING OUTLOOK + EVENING SUMMARY
# (kept in this file because prompts are brief-specific)
# ════════════════════════════════════════════════════════════

import re
import requests
from market_intel import SESSION  # reuse pooled session w/ retry

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL   = "gemini-2.0-flash"
GEMINI_TIMEOUT = 20

_AI_STRIP = re.compile(r"[`*_\[\]()]")

def _sanitize_ai(text: str | None, max_lines: int = 4, max_line_len: int = 140) -> str | None:
    if not text:
        return None
    cleaned = _AI_STRIP.sub("", text).strip()
    lines = [ln.strip()[:max_line_len] for ln in cleaned.splitlines() if ln.strip()]
    return "\n".join(lines[:max_lines]) or None


def _gemini_call(prompt: str, label: str) -> str | None:
    if not GEMINI_API_KEY:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    try:
        r = SESSION.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.6, "maxOutputTokens": 400},
            },
            timeout=GEMINI_TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json()
            cands = data.get("candidates") or []
            if cands:
                return cands[0]["content"]["parts"][0]["text"].strip()
            logging.warning(f"Gemini {label}: empty candidates")
        elif r.status_code == 429:
            logging.warning(f"Gemini rate-limited ({label})")
        else:
            logging.error(f"Gemini {label}: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logging.error(f"AI {label}: {e}")
    return None


def ai_daily_outlook(market_ctx, sector_summary, top_movers) -> str | None:
    mkt = market_ctx or {}
    spy = mkt.get("SPY", {}).get("pct", 0)
    qqq = mkt.get("QQQ", {}).get("pct", 0)
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


def ai_evening_summary(market_ctx, sector_summary, day_winners, day_losers, open_trades) -> str | None:
    mkt = market_ctx or {}
    spy = mkt.get("SPY", {}).get("pct", 0)
    qqq = mkt.get("QQQ", {}).get("pct", 0)
    vix = mkt.get("^VIX", {}).get("price", 15)
    sec_lines    = "\n".join(f"  {n}: {a:+.2f}%" for n, a in sector_summary[:6])
    winner_lines = "\n".join(f"  {w['symbol']}: {w['pct']:+.2f}%" for w in day_winners[:4]) or "  none"
    loser_lines  = "\n".join(f"  {l['symbol']}: {l['pct']:+.2f}%" for l in day_losers[:4])  or "  none"
    trade_lines  = (f"{len(open_trades)} trade(s) still open going into after-hours"
                    if open_trades else "No open trades")

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
# DISPATCH
# ════════════════════════════════════════════════════════════

@dataclass
class Job:
    kind:     str                        # 'morning' | 'evening'
    slot_key: str
    builder:  Callable[[], bool]

JOBS = {
    "morning": Job("morning", "last_morning_brief", build_morning_brief),
    "evening": Job("evening", "last_evening_brief", build_evening_brief),
}

def decide_job() -> Job | None:
    if in_window(CFG.morning_window): return JOBS["morning"]
    if in_window(CFG.evening_window): return JOBS["evening"]
    return None

def run_job(job: Job, *, force: bool = False) -> None:
    today = market_now().strftime("%Y-%m-%d")
    if not force and not can_alert(job.slot_key, CFG.slot_cooldown_h):
        print(f"ℹ️  {job.kind.title()} brief already sent today ({today})")
        return
    try:
        ok = job.builder()
        if ok:
            mark_alert(job.slot_key)        # ← only on confirmed send
    except Exception as e:
        logging.exception(f"{job.kind} brief crashed: {e}")


def setup_logging() -> None:
    handler = TimedRotatingFileHandler(
        LOGS_DIR / "brief.log", when="midnight", backupCount=14, utc=False,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


# ════════════════════════════════════════════════════════════
# MAIN
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
            print(f"ℹ️  FORCE_BRIEF set but outside both windows ({display_now().strftime('%H:%M ET')}).")
    else:
        job = decide_job()
        if job:
            run_job(job, force=False)
        else:
            print(f"ℹ️  Outside brief windows ({display_now().strftime('%H:%M ET')}). "
                  "Use FORCE_MORNING / FORCE_EVENING / FORCE_BRIEF to override.")
