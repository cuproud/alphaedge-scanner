"""
ALPHAEDGE DIP BUY SCANNER v3.0 — FULLY AUDITED
═══════════════════════════════════════════════════════════════
Finds HEALTHY PULLBACKS in strong uptrends that are temporarily
oversold. Multi-sector coverage across high-growth themes.

SCAN LOGIC:
  1. Confirm uptrend (price > EMA200 OR within 5% below with rising slope)
  2. Detect oversold (RSI 25–48, widened from v2's too-narrow 28–45)
  3. Confirm dip (today ≤ -1.5% OR 5-day ≤ -4%)
  4. Quality filter (within 30% of ATH, volume present, no earnings)
  5. Score & rank (0–16 scale)
  6. Cooldown per symbol (4h) to prevent spam

v3.0 CHANGES vs v2.0:
  • WIDENED RSI window (25–48) — v2 was too narrow, only ANET qualified
  • RELAXED dip thresholds (-1.5% daily / -4% weekly)
  • ADDED uptrend flexibility (allow 5% below EMA200 if slope positive)
  • FIXED 5d change calculation (proper business-day indexing)
  • FIXED cooldown — now properly records alert timestamps
  • EXPANDED universe to ~120 stocks across 12 sectors
  • IMPROVED scoring (0–16 scale, properly documented)
  • BETTER alert format (sector tags, clearer buy zones)
  • ADDED sector grouping in output
  • ADDED pre-scan diagnostics logging
"""

import os
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import yfinance as yf
import pandas as pd
import numpy as np

from market_intel import (
    get_full_context, get_earnings_date, calc_relative_strength,
    get_market_ctx, SYMBOL_EMOJI, send_telegram,
    now_est, load_json, save_json, STATE_FILE, can_alert,
    _clean_df
)

EST = ZoneInfo("America/New_York")
LOGS_DIR = Path('logs')
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / f'dipscan_{now_est().strftime("%Y-%m-%d")}.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)

# ═══════════════════════════════════════════════════════════════
# SECTOR-ORGANIZED UNIVERSE (~120 high-growth stocks)
# ═══════════════════════════════════════════════════════════════

SECTOR_MAP = {
    "🤖 AI & Software": [
        'NVDA', 'MSFT', 'GOOGL', 'META', 'PLTR', 'CRM', 'NOW', 'SNOW',
        'DDOG', 'CRWD', 'ADBE', 'ORCL', 'AI', 'PATH', 'BBAI', 'SOUN',
        'UPST', 'NBIS', 'APP', 'S',
    ],
    "🔬 Semiconductors": [
        'AMD', 'AVGO', 'TSM', 'ASML', 'MU', 'SMCI', 'MRVL', 'ARM',
        'ANET', 'LSCC', 'MPWR', 'KLAC', 'AMAT', 'ONTO', 'ACLS',
    ],
    "⚛️ Quantum Computing": [
        'IONQ', 'RGTI', 'QBTS', 'QUBT', 'ARQQ', 'QTUM',
    ],
    "☢️ Nuclear & Energy": [
        'OKLO', 'CEG', 'VST', 'SMR', 'NNE', 'LEU', 'CCJ', 'UEC',
        'BWXT', 'TLN',
    ],
    "🚀 Space & Defense": [
        'RKLB', 'LUNR', 'ASTS', 'RDW', 'MNTS', 'PL', 'KTOS',
        'LMT', 'RTX', 'NOC', 'LDOS', 'PLTR',
    ],
    "🧬 Healthcare & Biotech": [
        'LLY', 'NVO', 'REGN', 'MRNA', 'ISRG', 'DXCM', 'VEEV',
        'ARGX', 'NBIX', 'EXAS', 'TEM', 'RXRX', 'CRSP', 'BEAM',
        'NTLA', 'DNA',
    ],
    "💡 Photonics & Optics": [
        'COHR', 'IIVI', 'LITE', 'CIEN', 'FNSR', 'POET', 'LAZR',
        'LIDR', 'OUST',
    ],
    "₿ Crypto & Fintech": [
        'MSTR', 'MARA', 'RIOT', 'COIN', 'IREN', 'SOFI', 'HOOD',
        'NU', 'AFRM',
    ],
    "🏭 Mega Cap Tech": [
        'AAPL', 'AMZN', 'TSLA', 'NFLX',
    ],
    "🛒 Consumer & Growth": [
        'SHOP', 'UBER', 'SPOT', 'DUOL', 'CAVA', 'COST', 'CELH',
        'ONON', 'DECK', 'BIRK',
    ],
    "💰 Financials": [
        'JPM', 'V', 'MA', 'AXP', 'GS', 'SCHW',
    ],
    "🏗️ Infrastructure & Industrial": [
        'PWR', 'EME', 'PRIM', 'GEV', 'APH', 'ETN',
    ],
}

# Flatten universe (deduplicated)
DIP_UNIVERSE = list(dict.fromkeys(
    sym for stocks in SECTOR_MAP.values() for sym in stocks
))

# Reverse lookup: symbol → sector
SYMBOL_SECTOR = {}
for sector, symbols in SECTOR_MAP.items():
    for sym in symbols:
        if sym not in SYMBOL_SECTOR:
            SYMBOL_SECTOR[sym] = sector

# Emoji map
EXTRA_EMOJI = {
    'AAPL': '🍎', 'AVGO': '🔷', 'TSM': '🏭', 'ASML': '🔬', 'SMCI': '💻',
    'MRVL': '🛸', 'ARM': '🦾', 'CRM': '☁️', 'ADBE': '🎨', 'ORCL': '🗄️',
    'CRWD': '🛡️', 'PLTR': '🔮', 'SNOW': '❄️', 'NOW': '⏱️', 'DDOG': '🐕',
    'APP': '📱', 'DUOL': '🦉', 'HOOD': '🏹', 'CEG': '⚡', 'VST': '🔌',
    'SMR': '⚛️', 'NNE': '☢️', 'LLY': '💊', 'REGN': '🧬', 'JPM': '🏦',
    'V': '💳', 'MA': '💳', 'AXP': '🪙', 'QUBT': '🔬', 'MSTR': '₿',
    'MARA': '⛏️', 'RIOT': '⛏️', 'COIN': '🪙', 'SHOP': '🛍️', 'UBER': '🚗',
    'SPOT': '🎵', 'ANET': '🌐', 'COST': '🏪', 'CAVA': '🫒', 'IONQ': '⚛️',
    'RGTI': '⚛️', 'QBTS': '⚛️', 'OKLO': '☢️', 'CCJ': '☢️', 'LEU': '☢️',
    'RKLB': '🚀', 'LUNR': '🌙', 'ASTS': '📡', 'ISRG': '🤖', 'MRNA': '💉',
    'CRSP': '✂️', 'COHR': '💡', 'LAZR': '💡', 'AI': '🤖', 'SOUN': '🔊',
    'PATH': '🤖', 'SOFI': '💰', 'NU': '💜', 'AFRM': '💳', 'PWR': '🏗️',
    'GEV': '⚡', 'NFLX': '🎬', 'TSLA': '⚡', 'AMD': '🔴', 'NVDA': '💚',
    'META': '👓', 'GOOGL': '🔍', 'MSFT': '🪟', 'AMZN': '📦',
    'UEC': '☢️', 'BWXT': '☢️', 'TLN': '⚡', 'KTOS': '🎯',
    'ARQQ': '⚛️', 'POET': '💡', 'DNA': '🧬', 'RXRX': '🧪',
}
FULL_EMOJI = {**SYMBOL_EMOJI, **EXTRA_EMOJI}

# ═══════════════════════════════════════════════════════════════
# THRESHOLDS (v3.0 — WIDENED from v2.0)
# ═══════════════════════════════════════════════════════════════
DIP_RSI_MIN = 25          # was 28 — allow deeper oversold
DIP_RSI_MAX = 48          # was 45 — catch more early pullbacks
DIP_MIN_DROP_1D = -1.5    # was -2.0 — less restrictive
DIP_MIN_DROP_5D = -4.0    # was -5.0 — catch moderate pullbacks
DIP_MAX_FROM_ATH = -30.0  # was -25.0 — allow slightly more beaten names
DIP_MIN_VOL_RATIO = 0.6   # was 0.8 — don't exclude low-vol dip days
EMA200_FLEX_PCT = 5.0     # NEW: allow up to 5% below EMA200 if slope rising

PER_SYMBOL_COOLDOWN_HOURS = 4  # was 6 — faster re-alerts for fast movers
FETCH_DELAY = 0.2

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def get_5d_change(symbol):
    """Returns 5-day % change using proper business day indexing."""
    try:
        df = yf.download(symbol, period='1mo', interval='1d',
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        df = _clean_df(df)
        if len(df) < 6:
            return None
        # Use last row vs 5 trading days ago (index -1 vs -6)
        current_close = float(df['Close'].iloc[-1])
        past_close = float(df['Close'].iloc[-6])
        if past_close == 0:
            return None
        return ((current_close / past_close) - 1) * 100
    except Exception as e:
        logging.debug(f"5d change {symbol}: {e}")
        return None


def get_ema_slope(symbol, period=200, lookback=10):
    """Check if EMA is rising over last `lookback` days. Returns slope direction."""
    try:
        df = yf.download(symbol, period='1y', interval='1d',
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < period + lookback:
            return None
        df = _clean_df(df)
        ema = df['Close'].ewm(span=period, adjust=False).mean()
        if len(ema) < lookback:
            return None
        slope = float(ema.iloc[-1] - ema.iloc[-lookback])
        return slope > 0  # True = rising
    except Exception:
        return None


def record_alert_fired(symbol):
    """Record that we fired an alert for this symbol (for cooldown tracking)."""
    try:
        state = load_json(STATE_FILE) if STATE_FILE.exists() else {}
        key = f"dip_alert_{symbol}"
        state[key] = now_est().isoformat()
        save_json(STATE_FILE, state)
    except Exception as e:
        logging.error(f"Failed to record alert state for {symbol}: {e}")


def check_cooldown(symbol):
    """Check if symbol is in cooldown. Returns (is_cooled_down: bool, hours_remaining: float)."""
    key = f"dip_alert_{symbol}"
    try:
        if not can_alert(key, PER_SYMBOL_COOLDOWN_HOURS):
            # Calculate remaining time
            state = load_json(STATE_FILE) if STATE_FILE.exists() else {}
            last_str = state.get(key)
            if last_str:
                last_time = datetime.fromisoformat(last_str)
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=EST)
                elapsed = (now_est() - last_time).total_seconds() / 3600
                remaining = PER_SYMBOL_COOLDOWN_HOURS - elapsed
                return True, max(0, remaining)
            return True, 0
        return False, 0
    except Exception:
        return False, 0  # On error, allow alert


# ═══════════════════════════════════════════════════════════════
# QUALIFY DIP — CORE LOGIC (v3.0 IMPROVED)
# ═══════════════════════════════════════════════════════════════

def qualify_dip(ctx):
    """
    Evaluates whether a stock qualifies as a dip buy opportunity.

    Returns dict:
      'qualified': bool
      'reason': str (failure reason if not qualified)
      'score': int (0–16 if qualified)
      'reasons': list[str] (scoring breakdown)
      'drop_5d': float
      'rs_score': float or None
      'rs_label': str or None
      'trend_note': str (trend context)
    """
    c = ctx
    symbol = c['symbol']
    result = {
        'qualified': False, 'reason': '', 'score': 0,
        'reasons': [], 'drop_5d': None, 'rs_score': None,
        'rs_label': None, 'trend_note': ''
    }

    # ─── STEP 1: UPTREND CHECK (with flexibility) ───
    above_200 = c['current'] > c['ema200']
    pct_from_ema200 = ((c['current'] / c['ema200']) - 1) * 100 if c['ema200'] > 0 else 0

    if above_200:
        result['trend_note'] = f"✅ Above EMA200 (+{pct_from_ema200:.1f}%)"
    elif pct_from_ema200 >= -EMA200_FLEX_PCT:
        # Allow slightly below EMA200 if slope is rising
        ema_rising = get_ema_slope(symbol, period=200, lookback=10)
        if ema_rising:
            result['trend_note'] = f"⚠️ Slightly below EMA200 ({pct_from_ema200:+.1f}%) but slope rising"
        else:
            result['reason'] = f"Below EMA200 ({pct_from_ema200:+.1f}%) with flat/falling slope"
            return result
    else:
        result['reason'] = f"Too far below EMA200 ({pct_from_ema200:+.1f}%)"
        return result

    # ─── STEP 2: RSI ZONE ───
    rsi = c['rsi']
    if rsi < DIP_RSI_MIN:
        result['reason'] = f"RSI {rsi:.0f} too low (<{DIP_RSI_MIN}) — possible breakdown"
        return result
    if rsi > DIP_RSI_MAX:
        result['reason'] = f"RSI {rsi:.0f} not oversold (>{DIP_RSI_MAX})"
        return result

    # ─── STEP 3: DIP CONFIRMATION ───
    drop_5d = get_5d_change(symbol)
    if drop_5d is None:
        result['reason'] = "Could not calculate 5d change"
        return result
    result['drop_5d'] = drop_5d

    day_drop = c['day_change_pct']
    is_daily_dip = day_drop <= DIP_MIN_DROP_1D
    is_weekly_dip = drop_5d <= DIP_MIN_DROP_5D

    if not (is_daily_dip or is_weekly_dip):
        result['reason'] = (
            f"Insufficient dip: 1d={day_drop:+.1f}% (need ≤{DIP_MIN_DROP_1D}%) "
            f"| 5d={drop_5d:+.1f}% (need ≤{DIP_MIN_DROP_5D}%)"
        )
        return result

    # ─── STEP 4: ATH PROXIMITY ───
    if c['ath_pct'] < DIP_MAX_FROM_ATH:
        result['reason'] = f"Too far from ATH ({c['ath_pct']:+.0f}%, limit {DIP_MAX_FROM_ATH}%)"
        return result

    # ─── STEP 5: VOLUME CHECK ───
    if c['vol_ratio'] < DIP_MIN_VOL_RATIO:
        result['reason'] = f"Volume too thin ({c['vol_ratio']:.2f}× avg, need ≥{DIP_MIN_VOL_RATIO}×)"
        return result

    # ─── STEP 6: EARNINGS EXCLUSION ───
    try:
        _, days_to_earn = get_earnings_date(symbol)
        if days_to_earn is not None and 0 <= days_to_earn <= 3:
            result['reason'] = f"Earnings in {days_to_earn} day(s) — avoid"
            return result
    except Exception:
        pass  # If earnings check fails, proceed

    # ═══════════════════════════════════════════════
    # SCORING (0–16 max)
    # ═══════════════════════════════════════════════
    score = 0
    reasons = []

    # Trend structure (0–3)
    above_50 = c['current'] > c['ema50']
    if above_50 and above_200:
        score += 3
        reasons.append("📈 Strong trend (above EMA50 & EMA200)")
    elif above_200:
        score += 2
        reasons.append("📉 Pulling back to EMA50 zone")
    else:
        score += 1
        reasons.append("⚠️ Testing EMA200 support")

    # RSI depth (0–3)
    if rsi <= 30:
        score += 3
        reasons.append(f"🔥 Deeply oversold (RSI {rsi:.0f})")
    elif rsi <= 35:
        score += 2
        reasons.append(f"📊 Oversold (RSI {rsi:.0f})")
    else:
        score += 1
        reasons.append(f"📊 Cooling off (RSI {rsi:.0f})")

    # ATH proximity (0–3)
    if c['ath_pct'] > -5:
        score += 3
        reasons.append(f"🏔️ Very near ATH ({c['ath_pct']:+.1f}%)")
    elif c['ath_pct'] > -10:
        score += 2
        reasons.append(f"📍 Close to ATH ({c['ath_pct']:+.1f}%)")
    elif c['ath_pct'] > -20:
        score += 1
        reasons.append(f"📍 Moderate pullback from ATH ({c['ath_pct']:+.1f}%)")

    # Volume signal (0–2)
    if c['vol_ratio'] > 1.8:
        score += 2
        reasons.append(f"🔊 High volume capitulation ({c['vol_ratio']:.1f}× avg)")
    elif c['vol_ratio'] > 1.2:
        score += 1
        reasons.append(f"📊 Above-avg volume ({c['vol_ratio']:.1f}× avg)")

    # Drop severity (0–3)
    if drop_5d <= -10:
        score += 3
        reasons.append(f"💥 Sharp 5d selloff ({drop_5d:+.1f}%)")
    elif drop_5d <= -7:
        score += 2
        reasons.append(f"📉 Significant 5d drop ({drop_5d:+.1f}%)")
    elif drop_5d <= -4:
        score += 1
        reasons.append(f"📉 Moderate 5d dip ({drop_5d:+.1f}%)")

    # Relative strength (0–2)
    try:
        rs_score, rs_label = calc_relative_strength(c)
        result['rs_score'] = rs_score
        result['rs_label'] = rs_label
        if rs_score is not None and rs_score > 2:
            score += 2
            reasons.append(f"💪 Outperforming SPY ({rs_label})")
        elif rs_score is not None and rs_score > 0:
            score += 1
            reasons.append(f"📊 Holding vs SPY ({rs_label})")
    except Exception:
        pass

    result.update({
        'qualified': True,
        'score': score,
        'reasons': reasons,
    })
    return result


# ═══════════════════════════════════════════════════════════════
# ALERT FORMATTING
# ═══════════════════════════════════════════════════════════════

def format_alert(candidates, market_ctx, scan_stats):
    """Build clean, organized Telegram alert message."""
    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    # Use variable to avoid triple-backtick conflict with Python strings
    code_block = "\u0060\u0060\u0060"

    msg = f"{code_block}\n"
    msg += "╔══════════════════════════════════╗\n"
    msg += "║   🎯 ALPHAEDGE DIP SCANNER v3   ║\n"
    msg += "╚══════════════════════════════════╝\n"
    msg += f"{code_block}\n"
    msg += f"🕒 *{ts}*\n"
    msg += f"📊 Scanned `{scan_stats['total']}` | Qualified `{len(candidates)}`\n"

    # Market context
    if market_ctx:
        spy_data = market_ctx.get('SPY', {})
        vix_data = market_ctx.get('^VIX', {})
        spy_pct = spy_data.get('pct', 0)
        vix_price = vix_data.get('price', 15)

        spy_icon = "🟢" if spy_pct >= 0 else "🔴"
        vix_icon = "🟡" if vix_price > 20 else "🟢"

        msg += f"\n*Market:* {spy_icon} SPY `{spy_pct:+.2f}%` | {vix_icon} VIX `{vix_price:.1f}`"
        if vix_price > 25:
            msg += "\n⚠️ _High VIX — reduce position sizes_"
        elif vix_price > 20:
            msg += "\n⚡ _Elevated VIX — be selective_"
        msg += "\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    # Group by tier
    elite = [c for c in candidates if c['score'] >= 12]
    strong = [c for c in candidates if 8 <= c['score'] < 12]
    good = [c for c in candidates if c['score'] < 8]

    def format_candidate(c, rank):
        ctx = c['ctx']
        sym = ctx['symbol']
        em = FULL_EMOJI.get(sym, '📊')
        sector = SYMBOL_SECTOR.get(sym, '📊 Other')

        # Tier badge
        if c['score'] >= 12:
            badge = "🏆"
        elif c['score'] >= 8:
            badge = "⭐"
        else:
            badge = "✅"

        block = f"\n{badge} *#{rank} — {em} {sym}* `${ctx['current']:.2f}`\n"
        block += f"   {sector}\n"
        block += f"   📉 1D: `{ctx['day_change_pct']:+.2f}%` | 5D: `{c['drop_5d']:+.2f}%`\n"
        block += f"   📏 ATH: `{ctx['ath_pct']:+.1f}%` | RSI: `{ctx['rsi']:.0f}` | Vol: `{ctx['vol_ratio']:.1f}x`\n"
        block += f"   🎯 Score: *{c['score']}/16*"

        if c['rs_score'] is not None:
            rs_icon = "💪" if c['rs_score'] > 0 else "📉"
            block += f" | RS: {rs_icon} `{c['rs_score']:+.1f}%`"
        block += "\n"

        # Top 2 reasons only for brevity
        for r in c['reasons'][:2]:
            block += f"      • {r}\n"

        # Buy zone
        buy_low = min(ctx['ema50'], ctx['current'])
        buy_high = max(ctx['ema50'], ctx['current'])
        block += f"   🟢 Buy: `${buy_low:.2f}` - `${buy_high:.2f}`\n"
        block += f"   🛡️ Stop: `${ctx['ema200']:.2f}` (EMA200)\n"

        return block

    rank = 1
    top_shown = candidates[:10]

    if elite:
        msg += "\n*🏆 ELITE SETUPS (Score 12+):*\n"
        for c in [x for x in top_shown if x['score'] >= 12]:
            msg += format_candidate(c, rank)
            rank += 1

    if strong:
        msg += "\n*⭐ STRONG SETUPS (Score 8-11):*\n"
        for c in [x for x in top_shown if 8 <= x['score'] < 12]:
            msg += format_candidate(c, rank)
            rank += 1

    if good:
        msg += "\n*✅ WATCHLIST (Score <8):*\n"
        for c in [x for x in top_shown if x['score'] < 8]:
            msg += format_candidate(c, rank)
            rank += 1

    if len(candidates) > 10:
        msg += f"\n_...+{len(candidates) - 10} more (showing top 10)_\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "💡 *Rules:*\n"
    msg += "  • Pick 1-3 best setups, not all\n"
    msg += "  • Size: 2-5% of portfolio per trade\n"
    msg += "  • Stop below EMA200 or -8% max\n"
    msg += "  • Scale in - don't full-send\n"

    return msg


# ═══════════════════════════════════════════════════════════════
# MAIN SCAN
# ═══════════════════════════════════════════════════════════════

def run_dip_scan():
    now = now_est()
    print(f"\n{'=' * 50}")
    print(f"🎯 ALPHAEDGE DIP SCANNER v3.0")
    print(f"🕒 {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"📊 Universe: {len(DIP_UNIVERSE)} stocks across {len(SECTOR_MAP)} sectors")
    print(f"{'=' * 50}")
    logging.info(f"Dip scan start | Universe: {len(DIP_UNIVERSE)} stocks")

    # ─── Session check ───
    if now.weekday() >= 5:
        print("⚠️  Weekend — skipping (stale data)")
        logging.info("Skipped: weekend")
        return

    hour_dec = now.hour + now.minute / 60
    if hour_dec < 7.5 or hour_dec > 20.5:
        print("⚠️  Outside market-aware hours (7:30 AM - 8:30 PM ET)")
        logging.info("Skipped: outside hours")
        return

    # ─── Fetch market context ───
    market_ctx = get_market_ctx()

    # ─── Scan loop ───
    candidates = []
    scanned = 0
    failed = 0
    skipped_cooldown = 0
    disqualified_reasons = {}

    for i, symbol in enumerate(DIP_UNIVERSE):
        try:
            progress = f"[{i + 1}/{len(DIP_UNIVERSE)}]"
            print(f"  {progress} {symbol:6s}", end=" ", flush=True)

            # Check cooldown FIRST (saves API calls)
            is_cooled, hours_left = check_cooldown(symbol)
            if is_cooled:
                print(f"🔕 cooldown ({hours_left:.1f}h left)")
                skipped_cooldown += 1
                continue

            ctx = get_full_context(symbol)
            time.sleep(FETCH_DELAY)
            scanned += 1

            if not ctx:
                print("— no data")
                failed += 1
                continue

            # Validate required fields exist
            required_fields = [
                'current', 'ema200', 'ema50', 'rsi',
                'day_change_pct', 'ath_pct', 'vol_ratio'
            ]
            missing = [f for f in required_fields if f not in ctx or ctx[f] is None]
            if missing:
                print(f"— missing: {missing}")
                failed += 1
                continue

            result = qualify_dip(ctx)

            if not result['qualified']:
                # Track failure reasons for diagnostics
                reason_key = result['reason'].split('(')[0].strip()
                disqualified_reasons[reason_key] = disqualified_reasons.get(reason_key, 0) + 1
                print(f"✗ {result['reason'][:60]}")
                continue

            # Record cooldown
            record_alert_fired(symbol)

            candidates.append({
                'ctx': ctx,
                'score': result['score'],
                'reasons': result['reasons'],
                'drop_5d': result['drop_5d'],
                'rs_score': result['rs_score'],
                'rs_label': result['rs_label'],
                'trend_note': result['trend_note'],
            })
            print(f"🎯 QUALIFIED (score={result['score']}/16)")

        except Exception as e:
            print(f"💥 ERROR: {e}")
            failed += 1
            logging.error(f"Scan error {symbol}: {e}", exc_info=True)

    # ─── Summary ───
    print(f"\n{'-' * 50}")
    print(f"📊 SCAN COMPLETE:")
    print(f"   Scanned: {scanned} | Failed: {failed} | Cooldown: {skipped_cooldown}")
    print(f"   Qualified: {len(candidates)}")

    if disqualified_reasons:
        print(f"\n📋 Top disqualification reasons:")
        sorted_reasons = sorted(disqualified_reasons.items(), key=lambda x: -x[1])[:5]
        for reason, count in sorted_reasons:
            print(f"   • {reason}: {count}")

    logging.info(
        f"Scan complete | Scanned:{scanned} Failed:{failed} "
        f"Cooldown:{skipped_cooldown} Qualified:{len(candidates)}"
    )

    if not candidates:
        print("\n✅ No qualifying dip setups right now")
        logging.info("No candidates")
        return

    # ─── Sort by score ───
    candidates.sort(key=lambda c: (-c['score'], c['drop_5d']))

    # ─── Build & send alert ───
    scan_stats = {'total': scanned, 'failed': failed, 'cooldown': skipped_cooldown}
    msg = format_alert(candidates, market_ctx, scan_stats)

    # Split long messages (Telegram limit ~4096 chars)
    if len(msg) > 4000:
        parts = []
        current_part = ""
        for line in msg.split('\n'):
            if len(current_part) + len(line) + 1 > 3900:
                parts.append(current_part)
                current_part = line + '\n'
            else:
                current_part += line + '\n'
        if current_part:
            parts.append(current_part)

        success = True
        for i, part in enumerate(parts):
            if not send_telegram(part, silent=(i > 0)):
                success = False
            time.sleep(0.5)
    else:
        success = send_telegram(msg, silent=False)

    if success:
        print(f"\n✅ Alert sent! ({len(candidates)} candidates, top {min(10, len(candidates))} shown)")
    else:
        print("\n❌ Failed to send Telegram alert")
        logging.error("Telegram send failed")


# ═══════════════════════════════════════════════════════════════
# DIAGNOSTICS MODE — run with --debug to see why stocks fail
# ═══════════════════════════════════════════════════════════════

def run_diagnostics():
    """Run scan with verbose output showing exactly why each stock passes/fails."""
    print("\n🔍 DIAGNOSTIC MODE — showing all qualification details\n")

    for symbol in DIP_UNIVERSE[:20]:
        print(f"\n{'-' * 40}")
        print(f"📊 {symbol}")

        ctx = get_full_context(symbol)
        if not ctx:
            print("   ❌ No data returned from get_full_context")
            continue

        print(f"   Price: ${ctx.get('current', 'N/A')}")
        print(f"   EMA50: ${ctx.get('ema50', 'N/A')}")
        print(f"   EMA200: ${ctx.get('ema200', 'N/A')}")
        print(f"   RSI: {ctx.get('rsi', 'N/A')}")
        print(f"   Day Change: {ctx.get('day_change_pct', 'N/A')}%")
        print(f"   ATH%: {ctx.get('ath_pct', 'N/A')}%")
        print(f"   Vol Ratio: {ctx.get('vol_ratio', 'N/A')}x")

        above_200 = ctx['current'] > ctx['ema200'] if ctx.get('ema200') else None
        print(f"   Above EMA200: {above_200}")

        result = qualify_dip(ctx)
        if result['qualified']:
            print(f"   ✅ QUALIFIED — Score: {result['score']}/16")
            for r in result['reasons']:
                print(f"      • {r}")
        else:
            print(f"   ❌ FAILED — {result['reason']}")

        time.sleep(FETCH_DELAY)


if __name__ == "__main__":
    import sys
    if '--debug' in sys.argv or '--diagnostics' in sys.argv:
        run_diagnostics()
    else:
        run_dip_scan()
