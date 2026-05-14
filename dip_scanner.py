"""
ALPHAEDGE DIP BUY SCANNER v3.2 — UNIFIED BUILD
═══════════════════════════════════════════════════════════════
Finds healthy pullbacks in strong uptrends that are temporarily oversold.

v3.2 vs v3.1 — alignment with market_intel v3.0 + symbols.yaml v3
────────────────────────────────────────────────────────────────
• Universe loaded from symbols.yaml (filter: roles ∋ "dip") —
  no more hardcoded SECTOR_MAP / EXTRA_EMOJI duplication
• Cooldown uses market_intel.mark_alert (single atomic state path)
• Earnings uses market_intel.get_earnings_date (12h cache, central)
• Price-stats fetch reuses market_intel._yf_download cache
• Markdown escape + Telegram split delegated to market_intel
• Config applies YAML settings.dip_scanner overrides at startup
• Sector taxonomy now matches brief.py + intel scan output exactly
"""

from __future__ import annotations

import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, time as dtime
from enum import Enum
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from market_intel import (
    SECTORS,
    SYMBOL_EMOJI,
    SYMBOL_META,           # NEW: per-symbol metadata from yaml
    SYMBOL_TO_SECTOR,
    YAML_SETTINGS,         # NEW: optional overrides from yaml
    _yf_download,          # cache-aware downloader
    calc_relative_strength,
    can_alert,
    display_now,
    get_earnings_date,     # already 12h-cached internally
    get_full_context,
    get_market_ctx,
    mark_alert,            # atomic, used after confirmed send
    market_now,
    send_telegram,         # already auto-splits + escapes
    tg_escape as md,
    H_RULE as _MI_HRULE,   # we render our own narrower rule for mobile
)

# ════════════════════════════════════════════════════════════
# CONFIG (with yaml overrides)
# ════════════════════════════════════════════════════════════

LOGS_DIR = Path("logs"); LOGS_DIR.mkdir(exist_ok=True)
QUALIFIED_LOG_FILE = LOGS_DIR / "dip_qualified.jsonl"

H_RULE = "─────────────────────"   # 21 chars — mobile friendly


@dataclass(frozen=True)
class Config:
    rsi_min: float           = 25
    rsi_max: float           = 48
    drop_1d_max: float       = -1.5
    drop_5d_max: float       = -4.0
    ath_min: float           = -30.0
    vol_ratio_min: float     = 0.6
    ema200_flex_pct: float   = 5.0
    ema_slope_min_pct: float = 0.5

    cooldown_hours: int      = 4
    scan_window: tuple[dtime, dtime] = (dtime(7, 30), dtime(20, 30))

    fetch_workers: int       = 5
    max_loss_pct: float      = 8.0

    top_per_tier: int        = 5
    max_total_shown: int     = 12


def _apply_yaml_overrides(cfg: Config) -> Config:
    overrides = (YAML_SETTINGS or {}).get("dip_scanner") or {}
    if not overrides:
        return cfg
    valid = {f.name for f in cfg.__dataclass_fields__.values()}
    safe  = {k: v for k, v in overrides.items() if k in valid}
    if safe:
        logging.info(f"dip_scanner: applied yaml overrides: {sorted(safe)}")
    return replace(cfg, **safe)

CFG = _apply_yaml_overrides(Config())


# ════════════════════════════════════════════════════════════
# UNIVERSE — derived from symbols.yaml (no hardcoding)
# ════════════════════════════════════════════════════════════

def _build_dip_universe() -> tuple[list[str], dict[str, str]]:
    """Symbols whose role list includes 'dip'. Falls back to all if meta empty."""
    if SYMBOL_META:
        syms = [s for s, m in SYMBOL_META.items() if "dip" in (m.get("roles") or [])]
        if not syms:                            # no symbol opted in → use all
            syms = list(SYMBOL_META.keys())
    else:
        # legacy fallback if yaml missing — use whatever market_intel exposes
        syms = list({s for ss in SECTORS.values() for s in ss})
    sym_sector = {s: SYMBOL_TO_SECTOR.get(s, "Other") for s in syms}
    return syms, sym_sector

DIP_UNIVERSE, SYMBOL_SECTOR = _build_dip_universe()
SECTOR_COUNT = len({s for s in SYMBOL_SECTOR.values()})


# ════════════════════════════════════════════════════════════
# CLOCK
# ════════════════════════════════════════════════════════════

def is_weekend() -> bool:
    return market_now().weekday() >= 5

def in_window(win: tuple[dtime, dtime]) -> bool:
    t = market_now().time()
    return win[0] <= t < win[1]


# ════════════════════════════════════════════════════════════
# COOLDOWN (uses market_intel atomic state)
# ════════════════════════════════════════════════════════════

def cooldown_key(symbol: str) -> str:
    return f"dip_alert_{symbol}"

def is_in_cooldown(symbol: str) -> bool:
    return not can_alert(cooldown_key(symbol), CFG.cooldown_hours)


# ════════════════════════════════════════════════════════════
# PRICE STATS — reuses market_intel._yf_download cache
# ════════════════════════════════════════════════════════════

@dataclass
class PriceStats:
    drop_5d: float | None
    ema200_rising: bool | None
    swing_low_20d: float | None
    atr_14: float | None

def fetch_price_stats(symbol: str) -> PriceStats:
    df = _yf_download(symbol, period="1y", interval="1d")
    if df is None or df.empty or len(df) < 30:
        return PriceStats(None, None, None, None)
    try:
        close = df["Close"]
        # 5d change
        drop_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else None

        # EMA200 slope as % of price over 10 bars
        ema200_rising = None
        if len(close) >= 210:
            ema = close.ewm(span=200, adjust=False).mean()
            pct = float((ema.iloc[-1] - ema.iloc[-10]) / close.iloc[-1] * 100)
            ema200_rising = pct >= CFG.ema_slope_min_pct

        # 20-day swing low (excludes today's partial)
        swing_low_20d = float(close.iloc[-21:-1].min()) if len(close) >= 21 else None

        # ATR(14) — Wilder
        atr_14 = None
        if len(df) >= 15 and {"High", "Low", "Close"}.issubset(df.columns):
            high, low = df["High"], df["Low"]
            prev = close.shift(1)
            tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
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

    # Use ema200_real if exposed by market_intel v3.0 — None if insufficient history
    ema200 = ctx.get("ema200_real") or ctx.get("ema200")
    if not ema200 or ema200 <= 0:
        res.fail_code, res.fail_detail = FailCode.MISSING_FIELDS, "ema200"
        return res

    pct_from_ema200 = (ctx["current"] / ema200 - 1) * 100
    above_200 = ctx["current"] > ema200

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

    # RSI
    rsi = ctx["rsi"]
    if rsi < CFG.rsi_min:
        res.fail_code, res.fail_detail = FailCode.RSI_TOO_LOW, f"{rsi:.0f}"; return res
    if rsi > CFG.rsi_max:
        res.fail_code, res.fail_detail = FailCode.RSI_TOO_HIGH, f"{rsi:.0f}"; return res

    # Dip
    if stats.drop_5d is None:
        res.fail_code = FailCode.NO_5D_DATA; return res
    res.drop_5d = stats.drop_5d
    day_drop = ctx["day_change_pct"]
    if not (day_drop <= CFG.drop_1d_max or stats.drop_5d <= CFG.drop_5d_max):
        res.fail_code = FailCode.INSUFFICIENT_DIP
        res.fail_detail = f"1d {day_drop:+.1f}% / 5d {stats.drop_5d:+.1f}%"
        return res

    # ATH
    if ctx["ath_pct"] < CFG.ath_min:
        res.fail_code = FailCode.TOO_FAR_FROM_ATH
        res.fail_detail = f"{ctx['ath_pct']:+.0f}%"; return res

    # Volume
    if ctx["vol_ratio"] < CFG.vol_ratio_min:
        res.fail_code = FailCode.VOLUME_THIN
        res.fail_detail = f"{ctx['vol_ratio']:.2f}×"; return res

    # Earnings (central cache)
    _, days_to_earn = get_earnings_date(sym)
    if days_to_earn is not None and 0 <= days_to_earn <= 3:
        res.fail_code = FailCode.EARNINGS_SOON
        res.fail_detail = f"{days_to_earn}d"; return res

    # ─── Scoring (0–16) ────────────────────────────────────
    score, reasons = 0, []
    above_50 = ctx["current"] > ctx["ema50"]

    if above_50 and above_200:
        score += 3; reasons.append("📈 Strong trend (above EMA50 & EMA200)")
    elif above_200:
        score += 2; reasons.append("📉 Pulling back to EMA50 zone")
    else:
        score += 1; reasons.append("⚠️ Testing EMA200 (slope rising)")

    if rsi <= 30:
        score += 3; reasons.append(f"🔥 Deeply oversold (RSI {rsi:.0f})")
    elif rsi <= 35:
        score += 2; reasons.append(f"📊 Oversold (RSI {rsi:.0f})")
    else:
        score += 1; reasons.append(f"📊 Cooling off (RSI {rsi:.0f})")

    ap = ctx["ath_pct"]
    if   ap > -5:  score += 3; reasons.append(f"🏔️ Very near ATH ({ap:+.1f}%)")
    elif ap > -10: score += 2; reasons.append(f"📍 Close to ATH ({ap:+.1f}%)")
    elif ap > -20: score += 1; reasons.append(f"📍 Moderate pullback ({ap:+.1f}%)")

    vr = ctx["vol_ratio"]
    if   vr > 1.8: score += 2; reasons.append(f"🔊 High vol capitulation ({vr:.1f}×)")
    elif vr > 1.2: score += 1; reasons.append(f"📊 Above-avg volume ({vr:.1f}×)")

    d5 = stats.drop_5d
    if   d5 <= -10: score += 3; reasons.append(f"💥 Sharp 5d selloff ({d5:+.1f}%)")
    elif d5 <= -7:  score += 2; reasons.append(f"📉 Significant 5d drop ({d5:+.1f}%)")
    else:           score += 1; reasons.append(f"📉 Moderate 5d dip ({d5:+.1f}%)")

    try:
        rs_score, rs_label = calc_relative_strength(ctx)
        res.rs_score, res.rs_label = rs_score, rs_label
        if rs_score is not None:
            if rs_score > 2: score += 2; reasons.append(f"💪 Outperforming SPY ({rs_label})")
            elif rs_score > 0: score += 1; reasons.append(f"📊 Holding vs SPY ({rs_label})")
    except Exception as e:
        logging.debug(f"RS {sym}: {e}")

    # Buy zone (ATR / swing-low aware)
    current = ctx["current"]
    atr     = stats.atr_14 or (current * 0.02)
    swing   = stats.swing_low_20d or (current - 2 * atr)
    ema50   = ctx["ema50"]

    buy_low  = max(swing, ema50 - 0.5 * atr)
    buy_high = max(current, ema50)
    if buy_low >= buy_high:
        buy_low = buy_high * 0.99

    raw_stop = max(ema200, swing - atr)
    cap_stop = current * (1 - CFG.max_loss_pct / 100)
    stop = max(raw_stop, cap_stop)
    stop = min(stop, current * 0.999)

    res.qualified = True
    res.score = score
    res.reasons = reasons
    res.buy_low, res.buy_high, res.stop = buy_low, buy_high, stop
    return res


# ════════════════════════════════════════════════════════════
# ALERT FORMATTING — VISUAL TELEGRAM ALERTS ONLY
# ════════════════════════════════════════════════════════════

def dip_header(emoji: str, title: str, subtitle: str | None = None,
               border: str = "━━━━━━━━━━━━━━━━━━━━━") -> str:
    msg = f"{emoji} *{title}*\n`{border}`\n"
    if subtitle:
        msg += f"_{md(subtitle)}_\n"
    msg += "\n"
    return msg


def _tier(score: int) -> tuple[str, str]:
    if score >= 13:
        return ("🏆", "ELITE")
    if score >= 9:
        return ("⭐", "STRONG")
    return ("✅", "WATCHLIST")


def _name_label(sym: str) -> str:
    """e.g. 'NVDA — NVIDIA Corp. (NASDAQ)' if metadata available."""
    meta = SYMBOL_META.get(sym, {})
    name = meta.get("name", "")
    exch = meta.get("exchange", "")

    if name and exch:
        return f"{md(sym)} — {md(name)} ({md(exch)})"
    if name:
        return f"{md(sym)} — {md(name)}"
    return md(sym)


def _rsi_mood(rsi: float) -> str:
    if rsi <= 30:
        return "🔥 Deep oversold"
    if rsi <= 35:
        return "🟢 Oversold"
    if rsi <= 42:
        return "🟡 Cooling"
    return "⚪ Mild dip"


def _risk_label(stop_pct: float) -> str:
    if stop_pct >= -3:
        return "Low risk"
    if stop_pct >= -6:
        return "Medium risk"
    return "Higher risk"


def format_candidate(c: dict, rank: int) -> str:
    ctx, q = c["ctx"], c["q"]

    sym = ctx["symbol"]
    em = SYMBOL_EMOJI.get(sym, "📊")
    sector = SYMBOL_SECTOR.get(sym, "Other")
    badge, tier_name = _tier(q.score)

    rs_part = ""
    if q.rs_score is not None:
        rs_icon = "💪" if q.rs_score > 0 else "📉"
        rs_part = f" • RS {rs_icon} `{q.rs_score:+.1f}%`"

    stop_pct = (q.stop / ctx["current"] - 1) * 100 if q.stop else 0
    risk = _risk_label(stop_pct)

    block = ""
    block += f"\n{badge} *#{rank} — {tier_name} SETUP*\n"
    block += f"`─────────────────`\n"
    block += f"{em} *{_name_label(sym)}*\n"
    block += f"Sector: _{md(sector)}_\n\n"

    block += f"*PRICE SNAPSHOT*\n"
    block += f"💵 Current: `${ctx['current']:.2f}`\n"
    block += f"📉 1D: `{ctx['day_change_pct']:+.2f}%` • 5D: `{q.drop_5d:+.2f}%`\n"
    block += f"📊 RSI: `{ctx['rsi']:.0f}` — {_rsi_mood(ctx['rsi'])}\n"
    block += f"🏔️ From ATH: `{ctx['ath_pct']:+.1f}%`\n"
    block += f"🔊 Volume: `{ctx['vol_ratio']:.1f}×`{rs_part}\n\n"

    block += f"*WHY IT QUALIFIED*\n"
    for r in q.reasons[:3]:
        block += f"• {md(r)}\n"

    block += f"\n*TRADE PLAN*\n"
    block += f"🟢 Buy zone: `${q.buy_low:.2f}` → `${q.buy_high:.2f}`\n"
    block += f"🛡️ Stop: `${q.stop:.2f}` (`{stop_pct:+.1f}%`) — {risk}\n"
    block += f"🎯 Setup score: *{q.score}/16*\n"

    return block


def format_alert(candidates: list[dict], market_ctx: dict, stats: dict) -> str:
    ts = display_now().strftime("%a %b %d • %I:%M %p ET")

    msg = dip_header(
        "🎯",
        "DIP BUY SCANNER",
        ts,
        "━━━━━━━━━━━━━━━━━━━━━"
    )

    msg += f"*SCAN SUMMARY*\n"
    msg += f"`─────────────────`\n"
    msg += f"📊 Scanned: `{stats['scanned']}`\n"
    msg += f"✅ Qualified: `{len(candidates)}`\n"

    if stats["failed"]:
        msg += f"⚠️ Failed: `{stats['failed']}`\n"

    if stats["cooldown"]:
        msg += f"🔕 Cooldown: `{stats['cooldown']}`\n"

    if market_ctx:
        spy = market_ctx.get("SPY", {})
        qqq = market_ctx.get("QQQ", {})
        vix = market_ctx.get("^VIX", {})

        spy_pct = spy.get("pct", 0)
        qqq_pct = qqq.get("pct", 0)
        vix_p = vix.get("price", 15)

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

    tiers = {
        "ELITE": [],
        "STRONG": [],
        "WATCHLIST": [],
    }

    for c in candidates:
        _, name = _tier(c["q"].score)
        tiers[name].append(c)

    rank = 1
    total_shown = 0

    tier_headers = {
        "ELITE": {
            "title": "🏆 ELITE SETUPS",
            "desc": "Best risk/reward pullbacks",
            "border": "━━━━━━━━━━━━━━━━━━━━━",
        },
        "STRONG": {
            "title": "⭐ STRONG SETUPS",
            "desc": "Good dips, still need confirmation",
            "border": "═════════════════════",
        },
        "WATCHLIST": {
            "title": "✅ WATCHLIST SETUPS",
            "desc": "Interesting, but lower conviction",
            "border": "─────────────────────",
        },
    }

    for tier_name in ("ELITE", "STRONG", "WATCHLIST"):
        bucket = tiers[tier_name]

        if not bucket:
            continue

        bucket.sort(
            key=lambda c: (
                SYMBOL_SECTOR.get(c["ctx"]["symbol"], ""),
                -c["q"].score,
            )
        )

        shown = bucket[:CFG.top_per_tier]
        h = tier_headers[tier_name]

        msg += "\n"
        msg += f"*{h['title']}*\n"
        msg += f"_{h['desc']}_\n"
        msg += f"`{h['border']}`\n"

        for c in shown:
            if total_shown >= CFG.max_total_shown:
                break

            msg += format_candidate(c, rank)
            rank += 1
            total_shown += 1

        if len(bucket) > len(shown):
            msg += f"\n_+{len(bucket) - len(shown)} more in this tier_\n"

    if stats.get("disqualified"):
        msg += f"\n*TOP DISQUALIFICATIONS*\n"
        msg += f"`─────────────────`\n"

        top = sorted(
            stats["disqualified"].items(),
            key=lambda x: -x[1],
        )[:4]

        for code, cnt in top:
            msg += f"• {md(code)}: `{cnt}`\n"

    msg += f"\n*RULES*\n"
    msg += f"`─────────────────`\n"
    msg += "✅ Pick only 1–3 best setups\n"
    msg += "📏 Size 2–5% per trade\n"
    msg += "🛡️ Respect stop — EMA200 and max-loss protected\n"
    msg += "🧱 Scale in, don’t full-send\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += "_AlphaEdge Dip Scanner_"

    return msg

# ════════════════════════════════════════════════════════════
# SCAN PIPELINE
# ════════════════════════════════════════════════════════════

def _scan_one(symbol: str) -> tuple[str, dict | None, PriceStats | None, str | None]:
    try:
        ctx = get_full_context(symbol)
        if not ctx:
            return symbol, None, None, "no_ctx"
        required = ("current", "ema50", "rsi", "day_change_pct", "ath_pct", "vol_ratio")
        if any(ctx.get(f) is None for f in required):
            return symbol, None, None, "missing_fields"
        stats = fetch_price_stats(symbol)
        return symbol, ctx, stats, None
    except Exception as e:
        return symbol, None, None, f"err:{e}"


def run_dip_scan() -> None:
    print(f"\n{'='*50}")
    print(f"🎯 ALPHAEDGE DIP SCANNER v3.2")
    print(f"🕒 {display_now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"📊 Universe: {len(DIP_UNIVERSE)} stocks / {SECTOR_COUNT} sectors")
    print('='*50)
    logging.info(f"Scan start | universe={len(DIP_UNIVERSE)}")

    if is_weekend():
        print("⚠️ Weekend — skipping"); logging.info("skip:weekend"); return
    if not in_window(CFG.scan_window):
        print(f"⚠️ Outside scan window {CFG.scan_window[0]}–{CFG.scan_window[1]}")
        logging.info("skip:outside_window"); return

    market_ctx = get_market_ctx()

    eligible, in_cooldown = [], 0
    for sym in DIP_UNIVERSE:
        if is_in_cooldown(sym):
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

    # send_telegram already handles auto-split + parse-mode fallback
    sent_ok = send_telegram(msg, silent=False)

    if sent_ok:
        for c in candidates[: CFG.max_total_shown]:
            sym = c["ctx"]["symbol"]
            mark_alert(cooldown_key(sym))     # ← atomic, only after send
            try:
                with open(QUALIFIED_LOG_FILE, "a") as f:
                    f.write(json.dumps({
                        "ts":       market_now().isoformat(),
                        "sym":      sym,
                        "score":    c["q"].score,
                        "rsi":      c["ctx"]["rsi"],
                        "drop_5d":  c["q"].drop_5d,
                        "buy_low":  c["q"].buy_low,
                        "buy_high": c["q"].buy_high,
                        "stop":     c["q"].stop,
                    }) + "\n")
            except Exception as e:
                logging.warning(f"jsonl log {sym}: {e}")
        print(f"\n✅ Alert sent ({len(candidates)} qualified, "
              f"top {min(CFG.max_total_shown, len(candidates))} shown)")
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
            if not ctx: print("   ❌ no ctx"); continue
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
