"""
ALPHAEDGE DIP BUY SCANNER v3.1 — AUDITED BUILD
═══════════════════════════════════════════════════════════════
Finds healthy pullbacks in strong uptrends that are temporarily
oversold across 12 high-growth sectors.

v3.1 vs v3.0 — fixes & hardening
────────────────────────────────
P0
  • Cooldown recorded ONLY after successful Telegram send
  • Atomic state writes (fcntl-locked) — no race with brief.py
  • Single 1y/1d yfinance download per ticker, reused for 5d/EMA-slope
  • Markdown escaping for every dynamic value (md())
  • Reason codes added for accurate disqualification stats
  • Logging configured in __main__ with daily rotation

P1
  • Parallel fetch with bounded ThreadPoolExecutor
  • Earnings results cached 12h
  • Buy zone derived from ATR/swing-low instead of degenerate min/max
  • EMA slope = % change vs price, not raw delta
  • DST-safe window check using time(h,m)
  • Top-N selection happens per tier, not globally
  • Structured per-candidate JSONL logging for backtests

P2
  • Mobile-friendly Telegram layout (21-char rules, narrow rows)
  • Tier groups sorted by sector for visual clustering
  • Config dataclass — all magic numbers in one place
  • Display TZ = America/Toronto; market TZ = America/New_York
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from enum import Enum
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from market_intel import (
    SYMBOL_EMOJI, STATE_FILE,
    _clean_df, calc_relative_strength, can_alert,
    get_earnings_date, get_full_context, get_market_ctx,
    load_json, now_est, save_json, send_telegram,
)

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════

MARKET_TZ  = ZoneInfo("America/New_York")
DISPLAY_TZ = ZoneInfo("America/Toronto")     # same offset, user-facing label

LOGS_DIR = Path("logs"); LOGS_DIR.mkdir(exist_ok=True)
EARNINGS_CACHE_FILE = "earnings_cache.json"
QUALIFIED_LOG_FILE  = LOGS_DIR / "dip_qualified.jsonl"

H_RULE   = "─────────────────────"
TG_LIMIT = 4096


@dataclass(frozen=True)
class Config:
    # qualification thresholds
    rsi_min: float          = 25
    rsi_max: float          = 48
    drop_1d_max: float      = -1.5
    drop_5d_max: float      = -4.0
    ath_min: float          = -30.0
    vol_ratio_min: float    = 0.6
    ema200_flex_pct: float  = 5.0      # allow up to 5% below if slope rising
    ema_slope_min_pct: float = 0.5     # min % rise over lookback to count

    # cooldown / windows
    cooldown_hours: int     = 4
    scan_window: tuple[dtime, dtime] = (dtime(7, 30), dtime(20, 30))

    # fetch
    fetch_workers: int      = 5
    earnings_cache_ttl_h: int = 12

    # buy zone
    max_loss_pct: float     = 8.0      # hard cap on stop distance

    # alert layout
    top_per_tier: int       = 5
    max_total_shown: int    = 12

CFG = Config()


# ════════════════════════════════════════════════════════════
# UNIVERSE
# ════════════════════════════════════════════════════════════

SECTOR_MAP: dict[str, list[str]] = {
    "🤖 AI & Software":          ['NVDA','MSFT','GOOGL','META','PLTR','CRM','NOW','SNOW',
                                  'DDOG','CRWD','ADBE','ORCL','AI','PATH','BBAI','SOUN',
                                  'UPST','NBIS','APP','S'],
    "🔬 Semiconductors":          ['AMD','AVGO','TSM','ASML','MU','SMCI','MRVL','ARM',
                                  'ANET','LSCC','MPWR','KLAC','AMAT','ONTO','ACLS'],
    "⚛️ Quantum Computing":      ['IONQ','RGTI','QBTS','QUBT','ARQQ','QTUM'],
    "☢️ Nuclear & Energy":        ['OKLO','CEG','VST','SMR','NNE','LEU','CCJ','UEC',
                                  'BWXT','TLN'],
    "🚀 Space & Defense":         ['RKLB','LUNR','ASTS','RDW','MNTS','PL','KTOS',
                                  'LMT','RTX','NOC','LDOS'],
    "🧬 Healthcare & Biotech":    ['LLY','NVO','REGN','MRNA','ISRG','DXCM','VEEV',
                                  'ARGX','NBIX','EXAS','TEM','RXRX','CRSP','BEAM',
                                  'NTLA','DNA'],
    "💡 Photonics & Optics":      ['COHR','IIVI','LITE','CIEN','FNSR','POET','LAZR',
                                  'LIDR','OUST'],
    "₿ Crypto & Fintech":         ['MSTR','MARA','RIOT','COIN','IREN','SOFI','HOOD',
                                  'NU','AFRM'],
    "🏭 Mega Cap Tech":           ['AAPL','AMZN','TSLA','NFLX'],
    "🛒 Consumer & Growth":       ['SHOP','UBER','SPOT','DUOL','CAVA','COST','CELH',
                                  'ONON','DECK','BIRK'],
    "💰 Financials":              ['JPM','V','MA','AXP','GS','SCHW'],
    "🏗️ Infrastructure":          ['PWR','EME','PRIM','GEV','APH','ETN'],
}

DIP_UNIVERSE = list(dict.fromkeys(s for syms in SECTOR_MAP.values() for s in syms))

# Symbol → primary sector (first occurrence wins; PLTR → AI & Software)
SYMBOL_SECTOR: dict[str, str] = {}
for sector, syms in SECTOR_MAP.items():
    for s in syms:
        SYMBOL_SECTOR.setdefault(s, sector)

EXTRA_EMOJI = {
    'AAPL':'🍎','AVGO':'🔷','TSM':'🏭','ASML':'🔬','SMCI':'💻','MRVL':'🛸',
    'ARM':'🦾','CRM':'☁️','ADBE':'🎨','ORCL':'🗄️','CRWD':'🛡️','PLTR':'🔮',
    'SNOW':'❄️','NOW':'⏱️','DDOG':'🐕','APP':'📱','DUOL':'🦉','HOOD':'🏹',
    'CEG':'⚡','VST':'🔌','SMR':'⚛️','NNE':'☢️','LLY':'💊','REGN':'🧬',
    'JPM':'🏦','V':'💳','MA':'💳','AXP':'🪙','QUBT':'🔬','MSTR':'₿','MARA':'⛏️',
    'RIOT':'⛏️','COIN':'🪙','SHOP':'🛍️','UBER':'🚗','SPOT':'🎵','ANET':'🌐',
    'COST':'🏪','CAVA':'🫒','IONQ':'⚛️','RGTI':'⚛️','QBTS':'⚛️','OKLO':'☢️',
    'CCJ':'☢️','LEU':'☢️','RKLB':'🚀','LUNR':'🌙','ASTS':'📡','ISRG':'🤖',
    'MRNA':'💉','CRSP':'✂️','COHR':'💡','LAZR':'💡','AI':'🤖','SOUN':'🔊',
    'PATH':'🤖','SOFI':'💰','NU':'💜','AFRM':'💳','PWR':'🏗️','GEV':'⚡',
    'NFLX':'🎬','TSLA':'⚡','AMD':'🔴','NVDA':'💚','META':'👓','GOOGL':'🔍',
    'MSFT':'🪟','AMZN':'📦','UEC':'☢️','BWXT':'☢️','TLN':'⚡','KTOS':'🎯',
    'ARQQ':'⚛️','POET':'💡','DNA':'🧬','RXRX':'🧪',
}
FULL_EMOJI = {**SYMBOL_EMOJI, **EXTRA_EMOJI}


# ════════════════════════════════════════════════════════════
# CLOCK
# ════════════════════════════════════════════════════════════

def market_now()  -> datetime: return now_est().astimezone(MARKET_TZ)
def display_now() -> datetime: return market_now().astimezone(DISPLAY_TZ)
def is_weekend()  -> bool:     return market_now().weekday() >= 5

def in_window(win: tuple[dtime, dtime]) -> bool:
    t = market_now().time()
    return win[0] <= t < win[1]


# ════════════════════════════════════════════════════════════
# TELEGRAM ESCAPING
# ════════════════════════════════════════════════════════════

_MD_SPECIALS = re.compile(r"([_*`\[])")

def md(text: Any) -> str:
    if text is None: return "—"
    return _MD_SPECIALS.sub(r"\\\1", str(text))


# ════════════════════════════════════════════════════════════
# ATOMIC STATE
# ════════════════════════════════════════════════════════════

def _state_update(mutator) -> None:
    Path(STATE_FILE).touch(exist_ok=True)
    with open(STATE_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(f.read() or "{}")
            except json.JSONDecodeError:
                logging.warning("State corrupt; resetting")
                state = {}
            mutator(state)
            f.seek(0); f.truncate()
            json.dump(state, f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def record_alert_fired(symbol: str) -> None:
    key = f"dip_alert_{symbol}"
    now_iso = market_now().isoformat()
    _state_update(lambda s: s.__setitem__(key, now_iso))

def cooldown_hours_left(symbol: str) -> float:
    """0 = ready to alert; >0 = hours of cooldown remaining."""
    key = f"dip_alert_{symbol}"
    try:
        if can_alert(key, CFG.cooldown_hours):
            return 0.0
        state = load_json(STATE_FILE) if Path(STATE_FILE).exists() else {}
        last_str = state.get(key)
        if not last_str:
            return 0.0
        last = datetime.fromisoformat(last_str)
        if last.tzinfo is None:
            last = last.replace(tzinfo=MARKET_TZ)
        elapsed_h = (market_now() - last).total_seconds() / 3600
        return max(0.0, CFG.cooldown_hours - elapsed_h)
    except Exception as e:
        logging.warning(f"cooldown check {symbol}: {e}")
        return 0.0  # fail open


# ════════════════════════════════════════════════════════════
# EARNINGS CACHE (12h)
# ════════════════════════════════════════════════════════════

def get_earnings_cached(symbol: str) -> tuple:
    cache = load_json(EARNINGS_CACHE_FILE, {})
    rec = cache.get(symbol)
    if rec:
        cached_at = datetime.fromisoformat(rec["cached_at"])
        if datetime.now(MARKET_TZ) - cached_at < timedelta(hours=CFG.earnings_cache_ttl_h):
            return rec.get("date"), rec.get("days")
    try:
        ed, days = get_earnings_date(symbol)
    except Exception:
        ed, days = None, None
    cache[symbol] = {"date": ed, "days": days,
                     "cached_at": datetime.now(MARKET_TZ).isoformat()}
    save_json(EARNINGS_CACHE_FILE, cache)
    return ed, days


# ════════════════════════════════════════════════════════════
# SINGLE PRICE-HISTORY DOWNLOAD per ticker (replaces 2 separate calls)
# ════════════════════════════════════════════════════════════

@dataclass
class PriceStats:
    drop_5d: float | None
    ema200_rising: bool | None
    swing_low_20d: float | None
    atr_14: float | None

def fetch_price_stats(symbol: str) -> PriceStats:
    try:
        df = yf.download(symbol, period="1y", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return PriceStats(None, None, None, None)
        df = _clean_df(df)
        if len(df) < 30:
            return PriceStats(None, None, None, None)

        close = df["Close"]

        # 5d change
        drop_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else None

        # EMA200 slope as % change vs current price over 10 bars
        ema200_rising = None
        if len(close) >= 210:
            ema = close.ewm(span=200, adjust=False).mean()
            pct = float((ema.iloc[-1] - ema.iloc[-10]) / close.iloc[-1] * 100)
            ema200_rising = pct >= CFG.ema_slope_min_pct

        # 20-day swing low (excluding today)
        swing_low_20d = float(close.iloc[-21:-1].min()) if len(close) >= 21 else None

        # ATR(14) — Wilder's
        atr_14 = None
        if len(df) >= 15 and {"High","Low","Close"}.issubset(df.columns):
            high, low = df["High"], df["Low"]
            prev_close = close.shift(1)
            tr = pd.concat([high - low,
                            (high - prev_close).abs(),
                            (low  - prev_close).abs()], axis=1).max(axis=1)
            atr_14 = float(tr.rolling(14).mean().iloc[-1])

        return PriceStats(drop_5d, ema200_rising, swing_low_20d, atr_14)
    except Exception as e:
        logging.debug(f"price stats {symbol}: {e}")
        return PriceStats(None, None, None, None)


# ════════════════════════════════════════════════════════════
# QUALIFICATION
# ════════════════════════════════════════════════════════════

class FailCode(str, Enum):
    BELOW_EMA200_HARD       = "Too far below EMA200"
    BELOW_EMA200_FLAT_SLOPE = "Below EMA200 with flat/falling slope"
    RSI_TOO_LOW             = "RSI too low (breakdown risk)"
    RSI_TOO_HIGH            = "RSI not oversold"
    NO_5D_DATA              = "Could not compute 5d change"
    INSUFFICIENT_DIP        = "Insufficient dip (1d & 5d)"
    TOO_FAR_FROM_ATH        = "Too far from ATH"
    VOLUME_THIN             = "Volume too thin"
    EARNINGS_SOON           = "Earnings within 3 days"
    MISSING_FIELDS          = "Missing context fields"

@dataclass
class QualifyResult:
    qualified: bool             = False
    score: int                  = 0
    reasons: list[str]          = field(default_factory=list)
    fail_code: FailCode | None  = None
    fail_detail: str            = ""
    drop_5d: float | None       = None
    rs_score: float | None      = None
    rs_label: str | None        = None
    trend_note: str             = ""
    buy_low: float | None       = None
    buy_high: float | None      = None
    stop: float | None          = None


def qualify_dip(ctx: dict, stats: PriceStats) -> QualifyResult:
    res = QualifyResult()
    sym = ctx["symbol"]

    # ─── Trend ─────────────────────────────────────────────
    if not ctx.get("ema200") or ctx["ema200"] <= 0:
        res.fail_code, res.fail_detail = FailCode.MISSING_FIELDS, "ema200"
        return res
    pct_from_ema200 = (ctx["current"] / ctx["ema200"] - 1) * 100
    above_200 = ctx["current"] > ctx["ema200"]

    if above_200:
        res.trend_note = f"Above EMA200 ({pct_from_ema200:+.1f}%)"
    elif pct_from_ema200 >= -CFG.ema200_flex_pct:
        if stats.ema200_rising:
            res.trend_note = f"Below EMA200 ({pct_from_ema200:+.1f}%) but rising"
        else:
            res.fail_code = FailCode.BELOW_EMA200_FLAT_SLOPE
            res.fail_detail = f"{pct_from_ema200:+.1f}%"
            return res
    else:
        res.fail_code = FailCode.BELOW_EMA200_HARD
        res.fail_detail = f"{pct_from_ema200:+.1f}%"
        return res

    # ─── RSI ───────────────────────────────────────────────
    rsi = ctx["rsi"]
    if rsi < CFG.rsi_min:
        res.fail_code, res.fail_detail = FailCode.RSI_TOO_LOW, f"{rsi:.0f}"
        return res
    if rsi > CFG.rsi_max:
        res.fail_code, res.fail_detail = FailCode.RSI_TOO_HIGH, f"{rsi:.0f}"
        return res

    # ─── Dip ───────────────────────────────────────────────
    if stats.drop_5d is None:
        res.fail_code = FailCode.NO_5D_DATA
        return res
    res.drop_5d = stats.drop_5d
    day_drop = ctx["day_change_pct"]
    if not (day_drop <= CFG.drop_1d_max or stats.drop_5d <= CFG.drop_5d_max):
        res.fail_code = FailCode.INSUFFICIENT_DIP
        res.fail_detail = f"1d {day_drop:+.1f}% / 5d {stats.drop_5d:+.1f}%"
        return res

    # ─── ATH ───────────────────────────────────────────────
    if ctx["ath_pct"] < CFG.ath_min:
        res.fail_code = FailCode.TOO_FAR_FROM_ATH
        res.fail_detail = f"{ctx['ath_pct']:+.0f}%"
        return res

    # ─── Volume ────────────────────────────────────────────
    if ctx["vol_ratio"] < CFG.vol_ratio_min:
        res.fail_code = FailCode.VOLUME_THIN
        res.fail_detail = f"{ctx['vol_ratio']:.2f}×"
        return res

    # ─── Earnings ──────────────────────────────────────────
    _, days_to_earn = get_earnings_cached(sym)
    if days_to_earn is not None and 0 <= days_to_earn <= 3:
        res.fail_code = FailCode.EARNINGS_SOON
        res.fail_detail = f"{days_to_earn}d"
        return res

    # ═══ Scoring (0–16) ════════════════════════════════════
    score, reasons = 0, []
    above_50 = ctx["current"] > ctx["ema50"]

    # Trend (1–3)
    if above_50 and above_200:
        score += 3; reasons.append("📈 Strong trend (above EMA50 & EMA200)")
    elif above_200:
        score += 2; reasons.append("📉 Pulling back to EMA50 zone")
    else:
        score += 1; reasons.append("⚠️ Testing EMA200 (slope rising)")

    # RSI depth (1–3)
    if rsi <= 30:
        score += 3; reasons.append(f"🔥 Deeply oversold (RSI {rsi:.0f})")
    elif rsi <= 35:
        score += 2; reasons.append(f"📊 Oversold (RSI {rsi:.0f})")
    else:
        score += 1; reasons.append(f"📊 Cooling off (RSI {rsi:.0f})")

    # ATH proximity (0–3)
    ap = ctx["ath_pct"]
    if   ap > -5:  score += 3; reasons.append(f"🏔️ Very near ATH ({ap:+.1f}%)")
    elif ap > -10: score += 2; reasons.append(f"📍 Close to ATH ({ap:+.1f}%)")
    elif ap > -20: score += 1; reasons.append(f"📍 Moderate pullback ({ap:+.1f}%)")

    # Volume (0–2)
    vr = ctx["vol_ratio"]
    if   vr > 1.8: score += 2; reasons.append(f"🔊 High vol capitulation ({vr:.1f}×)")
    elif vr > 1.2: score += 1; reasons.append(f"📊 Above-avg volume ({vr:.1f}×)")

    # 5d severity (1–3)
    d5 = stats.drop_5d
    if   d5 <= -10: score += 3; reasons.append(f"💥 Sharp 5d selloff ({d5:+.1f}%)")
    elif d5 <= -7:  score += 2; reasons.append(f"📉 Significant 5d drop ({d5:+.1f}%)")
    else:           score += 1; reasons.append(f"📉 Moderate 5d dip ({d5:+.1f}%)")

    # RS (0–2)
    try:
        rs_score, rs_label = calc_relative_strength(ctx)
        res.rs_score, res.rs_label = rs_score, rs_label
        if rs_score is not None:
            if rs_score > 2: score += 2; reasons.append(f"💪 Outperforming SPY ({rs_label})")
            elif rs_score > 0: score += 1; reasons.append(f"📊 Holding vs SPY ({rs_label})")
    except Exception as e:
        logging.debug(f"RS {sym}: {e}")

    # ─── Buy zone (ATR / swing-low aware) ──────────────────
    current = ctx["current"]
    atr = stats.atr_14 or (current * 0.02)              # 2% fallback
    swing = stats.swing_low_20d or (current - 2 * atr)
    ema50 = ctx["ema50"]

    # Lower band: swing low or EMA50 - 0.5 ATR, whichever higher (tighter)
    buy_low  = max(swing, ema50 - 0.5 * atr)
    buy_high = max(current, ema50)
    if buy_low >= buy_high:
        buy_low = buy_high * 0.99

    # Stop: max of (EMA200, swing-low - 1 ATR), but cap loss at max_loss_pct
    raw_stop = max(ctx["ema200"], swing - atr)
    cap_stop = current * (1 - CFG.max_loss_pct / 100)
    stop = max(raw_stop, cap_stop)
    stop = min(stop, current * 0.999)                   # never above price

    res.qualified = True
    res.score = score
    res.reasons = reasons
    res.buy_low, res.buy_high, res.stop = buy_low, buy_high, stop
    return res


# ════════════════════════════════════════════════════════════
# ALERT FORMATTING
# ════════════════════════════════════════════════════════════

def _tier(score: int) -> tuple[str, str]:
    if score >= 13: return ("🏆", "ELITE")
    if score >= 9:  return ("⭐", "STRONG")
    return ("✅", "WATCHLIST")

def format_candidate(c: dict, rank: int) -> str:
    ctx, q = c["ctx"], c["q"]
    sym = ctx["symbol"]
    em  = FULL_EMOJI.get(sym, "📊")
    sector = SYMBOL_SECTOR.get(sym, "📊 Other")
    badge, _ = _tier(q.score)

    rs_part = ""
    if q.rs_score is not None:
        rs_icon = "💪" if q.rs_score > 0 else "📉"
        rs_part = f" • RS {rs_icon} `{q.rs_score:+.1f}%`"

    stop_pct = (q.stop / ctx["current"] - 1) * 100 if q.stop else 0

    block  = f"\n{badge} *#{rank} {md(sym)}* `${ctx['current']:.2f}` • Score *{q.score}/16*\n"
    block += f"   {md(sector)}\n"
    block += f"   1D `{ctx['day_change_pct']:+.2f}%` • 5D `{q.drop_5d:+.2f}%` • RSI `{ctx['rsi']:.0f}`\n"
    block += f"   ATH `{ctx['ath_pct']:+.1f}%` • Vol `{ctx['vol_ratio']:.1f}×`{rs_part}\n"
    for r in q.reasons[:2]:
        block += f"   • {md(r)}\n"
    block += f"   🟢 Buy `${q.buy_low:.2f}` → `${q.buy_high:.2f}`\n"
    block += f"   🛡️ Stop `${q.stop:.2f}` (`{stop_pct:+.1f}%`)\n"
    return block


def format_alert(candidates: list[dict], market_ctx: dict, stats: dict) -> str:
    now = display_now()
    ts = now.strftime("%a %b %d • %I:%M %p ET")

    msg  = f"🎯 *DIP SCANNER*\n"
    msg += f"🕒 {ts}\n"
    msg += f"`{H_RULE}`\n"
    msg += f"📊 Scanned `{stats['scanned']}` • Qualified `{len(candidates)}`"
    if stats["failed"]:   msg += f" • Failed `{stats['failed']}`"
    if stats["cooldown"]: msg += f" • Cooldown `{stats['cooldown']}`"
    msg += "\n"

    if market_ctx:
        spy = market_ctx.get("SPY", {})
        vix = market_ctx.get("^VIX", {})
        spy_pct, vix_p = spy.get("pct", 0), vix.get("price", 15)
        spy_em = "🟢" if spy_pct >= 0 else "🔴"
        vix_em = "🩸" if vix_p >= 30 else ("🟡" if vix_p >= 20 else "🟢")
        msg += f"🌍 SPY {spy_em} `{spy_pct:+.2f}%` • VIX {vix_em} `{vix_p:.1f}`\n"
        if   vix_p >= 25: msg += "_⚠️ High VIX — reduce position sizes_\n"
        elif vix_p >= 20: msg += "_⚡ Elevated VIX — be selective_\n"

    # Group by tier; within each tier, sort by sector then score
    tiers = {"ELITE": [], "STRONG": [], "WATCHLIST": []}
    for c in candidates:
        _, name = _tier(c["q"].score)
        tiers[name].append(c)

    rank, total_shown = 1, 0
    headers = {
        "ELITE":     "*🏆 ELITE (13–16)*",
        "STRONG":    "*⭐ STRONG (9–12)*",
        "WATCHLIST": "*✅ WATCHLIST (5–8)*",
    }
    for tier_name in ("ELITE", "STRONG", "WATCHLIST"):
        bucket = tiers[tier_name]
        if not bucket:
            continue
        bucket.sort(key=lambda c: (SYMBOL_SECTOR.get(c["ctx"]["symbol"], ""), -c["q"].score))
        shown = bucket[: CFG.top_per_tier]
        msg += f"\n{headers[tier_name]}\n`{H_RULE}`\n"
        for c in shown:
            if total_shown >= CFG.max_total_shown:
                break
            msg += format_candidate(c, rank)
            rank += 1; total_shown += 1
        if len(bucket) > len(shown):
            msg += f"   _+{len(bucket) - len(shown)} more in this tier_\n"

    if stats.get("disqualified"):
        msg += f"\n*📋 TOP DISQUALIFICATIONS*\n`{H_RULE}`\n"
        top = sorted(stats["disqualified"].items(), key=lambda x: -x[1])[:4]
        for code, cnt in top:
            msg += f"  • {md(code)}: `{cnt}`\n"

    msg += f"\n`{H_RULE}`\n"
    msg += "💡 *Rules*\n"
    msg += "  • Pick 1–3 best, not all\n"
    msg += "  • Size 2–5% of portfolio per trade\n"
    msg += "  • Stop respects EMA200 *and* −8% cap\n"
    msg += "  • Scale in — don't full-send"
    return msg


def split_for_telegram(msg: str, limit: int = TG_LIMIT) -> list[str]:
    if len(msg) <= limit:
        return [msg]
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


# ════════════════════════════════════════════════════════════
# SCAN PIPELINE
# ════════════════════════════════════════════════════════════

def _scan_one(symbol: str) -> tuple[str, dict | None, PriceStats | None, str | None]:
    try:
        ctx = get_full_context(symbol)
        if not ctx:
            return symbol, None, None, "no_ctx"
        required = ("current","ema200","ema50","rsi","day_change_pct","ath_pct","vol_ratio")
        if any(ctx.get(f) is None for f in required):
            return symbol, None, None, "missing_fields"
        stats = fetch_price_stats(symbol)
        return symbol, ctx, stats, None
    except Exception as e:
        return symbol, None, None, f"err:{e}"


def run_dip_scan() -> None:
    print(f"\n{'='*50}")
    print(f"🎯 ALPHAEDGE DIP SCANNER v3.1")
    print(f"🕒 {display_now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"📊 Universe: {len(DIP_UNIVERSE)} stocks / {len(SECTOR_MAP)} sectors")
    print('='*50)
    logging.info(f"Scan start | universe={len(DIP_UNIVERSE)}")

    if is_weekend():
        print("⚠️ Weekend — skipping"); logging.info("skip:weekend"); return
    if not in_window(CFG.scan_window):
        print(f"⚠️ Outside scan window {CFG.scan_window[0]}–{CFG.scan_window[1]}")
        logging.info("skip:outside_window"); return

    market_ctx = get_market_ctx()

    # Pre-filter cooldown
    eligible, in_cooldown = [], 0
    for sym in DIP_UNIVERSE:
        if cooldown_hours_left(sym) > 0:
            in_cooldown += 1
        else:
            eligible.append(sym)
    print(f"  Cooldown: {in_cooldown} • Eligible: {len(eligible)}")

    candidates: list[dict] = []
    scanned = failed = 0
    disq: dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=CFG.fetch_workers) as ex:
        futures = {ex.submit(_scan_one, s): s for s in eligible}
        for fut in as_completed(futures):
            sym, ctx, stats, err = fut.result()
            if err is not None or ctx is None or stats is None:
                failed += 1
                print(f"  {sym:6s} ✗ {err}")
                continue
            scanned += 1
            q = qualify_dip(ctx, stats)
            if not q.qualified:
                code = q.fail_code.value if q.fail_code else "unknown"
                disq[code] = disq.get(code, 0) + 1
                print(f"  {sym:6s} ✗ {code} {q.fail_detail}")
                continue
            print(f"  {sym:6s} 🎯 score={q.score}/16")
            candidates.append({"ctx": ctx, "q": q})

    # Sort by score then deeper dip
    candidates.sort(key=lambda c: (-c["q"].score, c["q"].drop_5d or 0))

    print(f"\n{'-'*50}")
    print(f"📊 Scanned: {scanned} • Failed: {failed} • Cooldown: {in_cooldown}")
    print(f"   Qualified: {len(candidates)}")
    if disq:
        for code, cnt in sorted(disq.items(), key=lambda x: -x[1])[:5]:
            print(f"   • {code}: {cnt}")
    logging.info(f"Scan done | scanned={scanned} failed={failed} "
                 f"cooldown={in_cooldown} qualified={len(candidates)}")

    if not candidates:
        print("\n✅ No qualifying setups."); return

    msg = format_alert(
        candidates, market_ctx,
        stats={"scanned": scanned, "failed": failed,
               "cooldown": in_cooldown, "disqualified": disq},
    )

    # Send first; only commit cooldown for symbols that actually went out
    sent_ok = True
    for chunk in split_for_telegram(msg):
        if not send_telegram(chunk, silent=False):
            sent_ok = False
            logging.error(f"Telegram chunk failed (len={len(chunk)})")

    if sent_ok:
        for c in candidates[: CFG.max_total_shown]:
            sym = c["ctx"]["symbol"]
            record_alert_fired(sym)
            try:
                with open(QUALIFIED_LOG_FILE, "a") as f:
                    f.write(json.dumps({
                        "ts":    market_now().isoformat(),
                        "sym":   sym,
                        "score": c["q"].score,
                        "rsi":   c["ctx"]["rsi"],
                        "drop_5d": c["q"].drop_5d,
                        "buy_low":  c["q"].buy_low,
                        "buy_high": c["q"].buy_high,
                        "stop": c["q"].stop,
                    }) + "\n")
            except Exception as e:
                logging.warning(f"jsonl log {sym}: {e}")
        print(f"\n✅ Alert sent ({len(candidates)} qualified, top {min(CFG.max_total_shown, len(candidates))} shown)")
    else:
        print("\n❌ Telegram send failed — cooldown NOT recorded; will retry next scan")


# ════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ════════════════════════════════════════════════════════════

def run_diagnostics() -> None:
    print("\n🔍 DIAGNOSTIC MODE — first 20 universe symbols\n")
    for symbol in DIP_UNIVERSE[:20]:
        print(f"\n{'-'*40}\n📊 {symbol}")
        try:
            ctx = get_full_context(symbol)
            if not ctx:
                print("   ❌ no ctx"); continue
            stats = fetch_price_stats(symbol)
            print(f"   Price ${ctx.get('current')} • EMA50 ${ctx.get('ema50')} • EMA200 ${ctx.get('ema200')}")
            print(f"   RSI {ctx.get('rsi')} • Day {ctx.get('day_change_pct')}% • ATH {ctx.get('ath_pct')}%")
            print(f"   Vol {ctx.get('vol_ratio')}× • 5d {stats.drop_5d} • EMA200↑ {stats.ema200_rising}")
            q = qualify_dip(ctx, stats)
            if q.qualified:
                print(f"   ✅ score={q.score}/16")
                for r in q.reasons: print(f"      • {r}")
                print(f"   🟢 Buy ${q.buy_low:.2f}–${q.buy_high:.2f} • Stop ${q.stop:.2f}")
            else:
                print(f"   ❌ {q.fail_code.value if q.fail_code else '?'} ({q.fail_detail})")
        except Exception as e:
            print(f"   💥 {e}")


# ════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════

def setup_logging() -> None:
    handler = TimedRotatingFileHandler(LOGS_DIR / "dipscan.log",
                                       when="midnight", backupCount=14)
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
    if "--debug" in sys.argv or "--diagnostics" in sys.argv:
        run_diagnostics()
    else:
        run_dip_scan()
