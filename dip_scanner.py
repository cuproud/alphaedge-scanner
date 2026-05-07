"""
ALPHAEDGE DIP BUY SCANNER v2.0 — AUDITED
═══════════════════════════════════════════════════════════════
Finds HEALTHY PULLBACKS — stocks in strong uptrends that are
temporarily oversold on daily RSI.

Universe: curated ~50 high-quality names (expandable)

Qualification criteria:
  • Above daily EMA200 (uptrend confirmed)
  • Daily RSI 28-45 (oversold but not dead)
  • Today down >-2% OR 5d down >-5%
  • Within 25% of ATH (not broken stocks)
  • NOT in earnings window (≤3 days)
  • Volume ≥ 0.8× avg
  • Not in dip-alert cooldown (6h per symbol)

Outputs: ranked top-10 with score, RS, buy zones.

v2.0 FIXES:
• Consistent return tuples from qualify_dip
• Per-symbol cooldown (6h) prevents repeat alerts
• Auto-split for long Telegram messages
• Session awareness — skip if overnight
• Better error handling
• Added MSTR/MARA/RIOT/COIN emojis
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
    _clean_df  # reused helper
)

EST = ZoneInfo("America/New_York")
LOGS_DIR = Path('logs')
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / f'dipscan_{now_est().strftime("%Y-%m-%d")}.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)

# ═══════════════════════════════════════════════
# DIP UNIVERSE (expandable)
# ═══════════════════════════════════════════════
DIP_UNIVERSE = [
    # Mega caps
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA',
    # Semis
    'AMD', 'AVGO', 'TSM', 'ASML', 'MU', 'SMCI', 'MRVL', 'ARM', 'SNDK',
    # Growth/SaaS
    'NFLX', 'CRM', 'ADBE', 'ORCL', 'CRWD', 'PLTR', 'SNOW', 'NOW', 'DDOG',
    # AI / Data
    'NBIS', 'APP', 'DUOL', 'HOOD',
    # Nuclear / Energy
    'OKLO', 'CEG', 'VST', 'SMR', 'NNE',
    # Biotech
    'LLY', 'NVO', 'REGN',
    # Financial
    'JPM', 'V', 'MA', 'SOFI', 'AXP',
    # Quantum
    'IONQ', 'RGTI', 'QBTS', 'QUBT',
    # Crypto-adj
    'MSTR', 'IREN', 'MARA', 'RIOT', 'COIN',
    # Other quality
    'SHOP', 'UBER', 'SPOT', 'ANET', 'COST', 'CAVA',
]

# Extend emoji for universe-only symbols
EXTRA_EMOJI = {
    'AAPL': '🍎', 'AVGO': '🔷', 'TSM': '🏭', 'ASML': '🔬', 'SMCI': '💻',
    'MRVL': '🛸', 'ARM': '🦾', 'CRM': '☁️', 'ADBE': '🎨', 'ORCL': '🗄️',
    'CRWD': '🛡️', 'PLTR': '🔮', 'SNOW': '❄️', 'NOW': '⏱️', 'DDOG': '🐕',
    'APP': '📱', 'DUOL': '🦉', 'HOOD': '🏹', 'CEG': '⚡', 'VST': '🔌',
    'SMR': '⚛️', 'NNE': '☢️', 'LLY': '💊', 'REGN': '🧬', 'JPM': '🏦',
    'V': '💳', 'MA': '💳', 'AXP': '🪙', 'QUBT': '🔬', 'MSTR': '₿',
    'MARA': '⛏️', 'RIOT': '⛏️', 'COIN': '🪙', 'SHOP': '🛍️', 'UBER': '🚗',
    'SPOT': '🎵', 'ANET': '🌐', 'COST': '🏪', 'CAVA': '🫒',
}
FULL_EMOJI = {**SYMBOL_EMOJI, **EXTRA_EMOJI}

# ═══════════════════════════════════════════════
# THRESHOLDS
# ═══════════════════════════════════════════════
DIP_RSI_MIN = 28
DIP_RSI_MAX = 45
DIP_MIN_DROP_1D = -2.0
DIP_MIN_DROP_5D = -5.0
DIP_MAX_FROM_ATH = -25.0
DIP_MIN_VOL_RATIO = 0.8
DIP_ABOVE_EMA200_REQUIRED = True

PER_SYMBOL_COOLDOWN_HOURS = 6
FETCH_DELAY = 0.25

# ═══════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════

def get_5d_change(symbol):
    """Returns 5d % change or None."""
    try:
        df = yf.download(symbol, period='1mo', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 6:
            return None
        df = _clean_df(df)
        return float((df['Close'].iloc[-1] / df['Close'].iloc[-6] - 1) * 100)
    except Exception as e:
        logging.debug(f"5d change {symbol}: {e}")
        return None

# ═══════════════════════════════════════════════
# QUALIFY DIP — returns consistent dict
# ═══════════════════════════════════════════════

def qualify_dip(ctx):
    """
    Returns dict with keys:
      'qualified': bool
      'reason': str (why failed if not qualified)
      'score': int (if qualified)
      'reasons': list[str] (if qualified)
      'drop_5d': float
      'rs_score': float or None
      'rs_label': str or None
    """
    c = ctx
    result = {'qualified': False, 'reason': '', 'score': 0,
              'reasons': [], 'drop_5d': None, 'rs_score': None, 'rs_label': None}

    # Must be in uptrend
    above_200 = c['current'] > c['ema200']
    if DIP_ABOVE_EMA200_REQUIRED and not above_200:
        result['reason'] = "below EMA200 (not uptrend)"
        return result

    # RSI zone
    if not (DIP_RSI_MIN <= c['rsi'] <= DIP_RSI_MAX):
        result['reason'] = f"RSI {c['rsi']:.0f} outside {DIP_RSI_MIN}-{DIP_RSI_MAX}"
        return result

    # Dip check
    drop_5d = get_5d_change(c['symbol'])
    if drop_5d is None:
        result['reason'] = "no 5d data"
        return result
    result['drop_5d'] = drop_5d

    is_dip = (c['day_change_pct'] <= DIP_MIN_DROP_1D) or (drop_5d <= DIP_MIN_DROP_5D)
    if not is_dip:
        result['reason'] = f"not dropping (1d={c['day_change_pct']:+.1f}%, 5d={drop_5d:+.1f}%)"
        return result

    # ATH check
    if c['ath_pct'] < DIP_MAX_FROM_ATH:
        result['reason'] = f"{c['ath_pct']:+.0f}% from ATH (>25%)"
        return result

    # Volume check
    if c['vol_ratio'] < DIP_MIN_VOL_RATIO:
        result['reason'] = f"low volume {c['vol_ratio']:.1f}×"
        return result

    # Earnings exclusion
    _, days_to_earn = get_earnings_date(c['symbol'])
    if days_to_earn is not None and days_to_earn <= 3:
        result['reason'] = f"earnings in {days_to_earn}d"
        return result

    # ═══ Scoring ═══
    score = 0
    reasons = []

    above_50 = c['current'] > c['ema50']
    if above_50 and above_200:
        score += 3
        reasons.append("Strong structure (above EMA50 & 200)")
    elif above_200:
        score += 2
        reasons.append("Pullback to EMA50")

    if c['rsi'] < 35:
        score += 3
        reasons.append(f"Deep oversold RSI {c['rsi']:.0f}")
    else:
        score += 2
        reasons.append(f"Oversold RSI {c['rsi']:.0f}")

    if c['ath_pct'] > -10:
        score += 2
        reasons.append(f"Near ATH ({c['ath_pct']:+.1f}%)")
    elif c['ath_pct'] > -15:
        score += 1

    if c['vol_ratio'] > 1.5:
        score += 2
        reasons.append(f"Heavy volume {c['vol_ratio']:.1f}×")
    elif c['vol_ratio'] > 1.0:
        score += 1

    if drop_5d < -8:
        score += 2
        reasons.append(f"Sharp 5d drop ({drop_5d:+.1f}%)")
    elif drop_5d < -5:
        score += 1

    # RS check
    rs_score, rs_label = calc_relative_strength(c)
    if rs_score is not None and rs_score > 0:
        score += 2
        reasons.append(rs_label)

    result.update({
        'qualified': True,
        'score': score,
        'reasons': reasons,
        'rs_score': rs_score,
        'rs_label': rs_label,
    })
    return result

# ═══════════════════════════════════════════════
# MAIN SCAN
# ═══════════════════════════════════════════════

def run_dip_scan():
    print(f"\n🎯 Dip Scanner @ {now_est().strftime('%H:%M %Z')}")
    logging.info("Dip scan start")

    # Session awareness — skip if overnight (stale data, no use)
    now = now_est()
    if now.weekday() >= 5:
        print("⚠️ Weekend — dip scan skipped (stale stock data)")
        return
    hour_dec = now.hour + now.minute / 60
    if hour_dec < 8 or hour_dec > 20:
        print("⚠️ Outside trading-aware hours — skipped")
        return

    market_ctx = get_market_ctx()
    candidates = []
    scanned = 0
    failed = 0

    for symbol in DIP_UNIVERSE:
        try:
            print(f"  → {symbol:8s}...", end=" ", flush=True)
            ctx = get_full_context(symbol)
            time.sleep(FETCH_DELAY)
            scanned += 1

            if not ctx:
                print("— no data")
                failed += 1
                continue

            result = qualify_dip(ctx)

            if not result['qualified']:
                print(f"✗ {result['reason']}")
                continue

            # Per-symbol cooldown
            cool_key = f"dip_alert_{symbol}"
            if not can_alert(cool_key, PER_SYMBOL_COOLDOWN_HOURS):
                print(f"🔕 cooldown (score={result['score']})")
                continue

            candidates.append({
                'ctx': ctx,
                'score': result['score'],
                'reasons': result['reasons'],
                'drop_5d': result['drop_5d'],
                'rs_score': result['rs_score'],
                'rs_label': result['rs_label'],
            })
            print(f"🎯 DIP score={result['score']}")
        except Exception as e:
            print(f"💥 {e}")
            failed += 1
            logging.error(f"Dip scan {symbol}: {e}")

    print(f"\n📊 Scanned: {scanned} | Failed: {failed} | Qualified: {len(candidates)}")

    if not candidates:
        print("✅ No dips today worth alerting")
        logging.info("Dip scan: no candidates")
        return

    # Sort by score desc
    candidates.sort(key=lambda c: -c['score'])

    # ═══ BUILD ALERT ═══
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    msg = f"🎯 *DIP BUY OPPORTUNITIES* ({len(candidates)})\n"
    msg += f"🕒 {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Oversold pullbacks in strong uptrends_\n"

    # Market context line
    if market_ctx:
        spy = market_ctx.get('SPY', {}).get('pct', 0)
        vix = market_ctx.get('^VIX', {}).get('price', 15)
        spy_em = "🟢" if spy >= 0 else "🔴"
        msg += f"\n🌍 SPY {spy_em} `{spy:+.2f}%` • VIX `{vix:.1f}`"
        if vix > 22:
            msg += " ⚠️ _risk-off — size smaller_"
        msg += "\n"

    top = candidates[:10]
    for c in top:
        ctx = c['ctx']
        em = FULL_EMOJI.get(ctx['symbol'], '📊')
        tier = "🏆 ELITE" if c['score'] >= 10 else "⭐ STRONG" if c['score'] >= 7 else "✅ GOOD"

        msg += f"\n{tier} {em} *{ctx['symbol']}* @ `${ctx['current']:.2f}`\n"
        msg += f"  📉 Today: `{ctx['day_change_pct']:+.2f}%` • 5d: `{c['drop_5d']:+.2f}%`\n"
        msg += f"  📏 {ctx['ath_pct']:+.1f}% from ATH • RSI `{ctx['rsi']:.0f}`\n"
        msg += f"  🎯 *Score: {c['score']}/14*"
        if c['rs_score'] is not None:
            sign = "+" if c['rs_score'] >= 0 else ""
            msg += f" • RS vs SPY: `{sign}{c['rs_score']}%`"
        msg += "\n"
        for r in c['reasons'][:3]:
            msg += f"     • {r}\n"
        msg += f"  🟢 *Buy Zone:* `${ctx['ema50']:.2f}` – `${ctx['current']:.2f}`\n"
        msg += f"  🛡️ *Support:* `${ctx['ema200']:.2f}` (EMA200)\n"

    if len(candidates) > 10:
        msg += f"\n_+{len(candidates) - 10} more candidates (top 10 shown)_\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"💡 _Quality > quantity. Pick 1-2, size small._"

    # send_telegram auto-splits if >4000 chars
    if send_telegram(msg, silent=False):
        print(f"✅ Sent {len(top)} dip candidates to Telegram")
        logging.info(f"Dip alert sent | Candidates: {len(candidates)}")
    else:
        print("❌ Failed to send")

if __name__ == "__main__":
    run_dip_scan()
