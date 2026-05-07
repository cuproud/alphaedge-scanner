"""
ALPHAEDGE DIP BUY SCANNER
═══════════════════════════════════════════════════════════════
Proactively finds HEALTHY PULLBACKS — stocks in strong uptrends
that are temporarily oversold on daily RSI.

Universe: curated "high-quality" list (expand as needed)
Criteria:
  • Price above daily EMA50 AND EMA200 (uptrend confirmed)
  • Daily RSI between 30-45 (oversold in uptrend = dip)
  • Today down >-2% OR -5d down >-5%
  • Within 25% of 52W high
  • NOT in earnings window (≤3 days)
  • Volume confirmation (>0.8× avg)
"""

import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import yfinance as yf
import pandas as pd
import numpy as np

from market_intel import (
    get_full_context, get_earnings_date, calc_relative_strength,
    get_market_ctx, SYMBOL_EMOJI, send_telegram,
    now_est, load_json, save_json, STATE_FILE
)

logging.basicConfig(
    filename=f'logs/dipscan_{now_est().strftime("%Y-%m-%d")}.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
Path('logs').mkdir(exist_ok=True)

# ═══════════════════════════════════════════════
# DIP UNIVERSE — Expand as needed
# ═══════════════════════════════════════════════
DIP_UNIVERSE = [
    # Mega caps
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA',
    # Semis
    'AMD', 'AVGO', 'TSM', 'ASML', 'MU', 'SMCI', 'MRVL', 'ARM',
    # Growth/Tech
    'NFLX', 'CRM', 'ADBE', 'ORCL', 'CRWD', 'PLTR', 'SNOW', 'NOW', 'DDOG',
    # AI/Data
    'NBIS', 'APP', 'DUOL', 'HOOD', 'COIN',
    # Nuclear / Energy
    'OKLO', 'CEG', 'VST', 'SMR', 'NNE',
    # Biotech
    'LLY', 'NVO', 'REGN',
    # Financial
    'JPM', 'V', 'MA', 'SOFI', 'AXP',
    # Quantum / Exotic
    'IONQ', 'RGTI', 'QBTS', 'QUBT',
    # Crypto-adj
    'MSTR', 'IREN', 'MARA', 'RIOT',
    # Others
    'SHOP', 'UBER', 'SPOT', 'ANET', 'COST', 'CAVA',
]

# Dip criteria thresholds
DIP_RSI_MIN = 28
DIP_RSI_MAX = 45
DIP_MIN_DROP_1D = -2.0    # or
DIP_MIN_DROP_5D = -5.0
DIP_MAX_FROM_ATH = -25.0  # Within 25% of ATH
DIP_MIN_VOL_RATIO = 0.8

# ═══════════════════════════════════════════════
# FIVE DAY DROP
# ═══════════════════════════════════════════════
def get_5d_change(symbol):
    try:
        df = yf.download(symbol, period='2wk', interval='1d',
                        progress=False, auto_adjust=True)
        if df.empty or len(df) < 6:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return float((df['Close'].iloc[-1] / df['Close'].iloc[-6] - 1) * 100)
    except:
        return None

# ═══════════════════════════════════════════════
# QUALIFY DIP
# ═══════════════════════════════════════════════
def qualify_dip(ctx):
    """Returns (score, reasons) or (None, None) if doesn't qualify."""
    c = ctx
    reasons = []
    score = 0

    # Must be in uptrend
    above_50 = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if not above_200:
        return None, "below EMA200 (not uptrend)"

    # RSI oversold zone
    if not (DIP_RSI_MIN <= c['rsi'] <= DIP_RSI_MAX):
        return None, f"RSI {c['rsi']:.0f} outside dip zone"

    # Today down OR 5d down
    drop_5d = get_5d_change(c['symbol'])
    if drop_5d is None:
        return None, "no 5d data"
    is_dip = (c['day_change_pct'] <= DIP_MIN_DROP_1D) or (drop_5d <= DIP_MIN_DROP_5D)
    if not is_dip:
        return None, "not dropping enough"

    # Not too far from ATH
    if c['ath_pct'] < DIP_MAX_FROM_ATH:
        return None, f"{c['ath_pct']:.0f}% from ATH (too far)"

    # Volume check
    if c['vol_ratio'] < DIP_MIN_VOL_RATIO:
        return None, f"low volume {c['vol_ratio']:.1f}×"

    # Earnings exclusion
    _, days_to_earn = get_earnings_date(c['symbol'])
    if days_to_earn is not None and days_to_earn <= 3:
        return None, f"earnings in {days_to_earn}d"

    # Scoring
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
        reasons.append(f"Sharp 5d drop ({drop_5d:.1f}%)")
    elif drop_5d < -5:
        score += 1

    # RS check
    rs_score, rs_label = calc_relative_strength(c)
    if rs_score and rs_score > 0:
        score += 2
        reasons.append(f"{rs_label}")

    return score, reasons, drop_5d, rs_score, rs_label

# ═══════════════════════════════════════════════
# MAIN DIP SCAN
# ═══════════════════════════════════════════════
def run_dip_scan():
    print(f"\n🎯 Dip Scanner @ {now_est().strftime('%H:%M %Z')}")
    market_ctx = get_market_ctx()

    candidates = []
    for symbol in DIP_UNIVERSE:
        try:
            print(f"  → {symbol:8s}...", end=" ")
            ctx = get_full_context(symbol)
            time.sleep(0.25)
            if not ctx:
                print("—")
                continue

            result = qualify_dip(ctx)
            if result[0] is None:
                print(f"✗ {result[1]}")
                continue

            score, reasons, drop_5d, rs_score, rs_label = result
            candidates.append({
                'ctx': ctx,
                'score': score,
                'reasons': reasons,
                'drop_5d': drop_5d,
                'rs_score': rs_score,
                'rs_label': rs_label,
            })
            print(f"🎯 DIP score={score}")
        except Exception as e:
            print(f"💥 {e}")

    if not candidates:
        print("\n✅ No dips today")
        return

    candidates.sort(key=lambda c: -c['score'])

    # ═══ BUILD ALERT ═══
    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    msg = f"🎯 *DIP BUY OPPORTUNITIES*\n"
    msg += f"🕒 {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Oversold pullbacks in strong uptrends_\n\n"

    top = candidates[:10]
    for c in top:
        ctx = c['ctx']
        em = SYMBOL_EMOJI.get(ctx['symbol'], '📊')
        tier = "🏆 ELITE" if c['score'] >= 10 else "⭐ STRONG" if c['score'] >= 7 else "✅ GOOD"

        msg += f"{tier} {em} *{ctx['symbol']}* @ `${ctx['current']:.2f}`\n"
        msg += f"  📉 Today: `{ctx['day_change_pct']:+.2f}%` • 5d: `{c['drop_5d']:+.2f}%`\n"
        msg += f"  📏 {ctx['ath_pct']:+.1f}% from ATH • RSI `{ctx['rsi']:.0f}`\n"
        msg += f"  🎯 Score: *{c['score']}/14*\n"
        for r in c['reasons'][:3]:
            msg += f"     • {r}\n"
        msg += f"  🟢 *Buy Zone:* `${ctx['ema50']:.2f}` – `${ctx['current']:.2f}`\n"
        msg += f"  🛡️ *Support:* `${ctx['ema200']:.2f}` (EMA200)\n\n"

    if len(candidates) > 10:
        msg += f"_+{len(candidates) - 10} more candidates (top 10 shown)_\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"💡 _Quality > quantity. Pick 1-2, size small._"

    send_telegram(msg, silent=False)
    print(f"\n✅ Sent {len(candidates)} dip candidates")
    logging.info(f"Dip scan | Candidates: {len(candidates)}")

if __name__ == "__main__":
    run_dip_scan()
