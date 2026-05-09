"""
ALPHAEDGE SINGLE SCAN v2.0
═══════════════════════════════════════════════════════════════
On-demand analysis triggered by Telegram message.
Usage: python single_scan.py TSLA
       python single_scan.py BTC-USD

v2.0 IMPROVEMENTS:
• Verdict + AI summary at TOP of message
• Momentum/ATH continuation verdict (was wrongly NEUTRAL)
• Better entry guidance per verdict type
• Volume interpretation (not just raw ratio)
• Breakout entry for momentum stocks
• "What would make this a BUY" for NEUTRAL/HOLD
• Support/resistance from recent structure
• ATH recency ("set yesterday" vs "set 2 years ago")
• Cleaner layout — key facts first, details below
"""

import sys
import os
import time
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")
LOGS_DIR = Path('logs')
LOGS_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=LOGS_DIR / f'single_{datetime.now(EST).strftime("%Y-%m-%d")}.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)

from market_intel import (
    get_full_context, get_market_ctx,
    calc_relative_strength, get_earnings_date, format_earnings_warning,
    ai_analyze_drop, SYMBOL_EMOJI, send_telegram, now_est,
    EARNINGS_WARNING_DAYS
)

try:
    import yfinance as yf
    import numpy as np
    import pandas as pd
except ImportError as e:
    print(f"Missing dependency: {e}")
    sys.exit(1)


# ═══════════════════════════════════════════════
# IMPROVED VERDICT ENGINE
# ═══════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None):
    """
    Improved verdict with momentum/ATH case and better logic flow.
    Returns (verdict, zone, [reasons], [next_steps])
    """
    c = ctx
    rsi = c['rsi']
    trend = c['trend']
    drop = c['day_change_pct']
    from_ath = c['ath_pct']
    range_pos = c['range_pos']
    above_50 = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']

    reasons = []
    next_steps = []
    verdict = None
    zone = None

    # ── 1. MOMENTUM — AT/NEAR ATH in strong uptrend ──
    if ("UPTREND" in trend and from_ath > -5 and
            above_50 and above_200 and rsi < 80):
        verdict = "🚀 MOMENTUM"
        zone = "AT ATH — Continuation"
        reasons.append(f"At/near all-time high ({from_ath:+.1f}%)")
        reasons.append("EMA stack fully bullish")
        reasons.append(f"RSI {rsi:.0f} — not overbought, room to run")
        next_steps = [
            f"Breakout entry: above ATH `${c['ath']:.2f}` with volume",
            f"Pullback entry: dip to EMA50 `${c['ema50']:.2f}`",
            f"Stop: below EMA50 `${c['ema50']:.2f}`",
        ]

    # ── 2. STRONG UPTREND PULLBACK → BUY ZONE ──
    elif ("UPTREND" in trend and rsi < 52 and above_200):
        verdict = "🟢 BUY ZONE"
        zone = "Pullback in Uptrend"
        reasons.append("Healthy pullback in confirmed uptrend")
        reasons.append(f"RSI {rsi:.0f} — not oversold, room to run")
        if from_ath > -20:
            reasons.append("Near ATH — strong stock pulling back")
        next_steps = [
            f"Entry: current price `${c['current']:.2f}` or lower",
            f"Target: retest ATH `${c['ath']:.2f}`",
            f"Stop: below EMA200 `${c['ema200']:.2f}`",
        ]

    # ── 3. EMA50 PULLBACK ──
    elif "PULLBACK" in trend and rsi < 55:
        verdict = "🟢 BUY ZONE"
        zone = "EMA50 Pullback"
        reasons.append("Above EMA200 — uptrend structure intact")
        reasons.append(f"Pulling back toward EMA50 ${c['ema50']:.2f}")
        reasons.append(f"RSI {rsi:.0f} — watch for bounce")
        next_steps = [
            f"Entry: near EMA50 `${c['ema50']:.2f}`",
            f"Stop: below EMA200 `${c['ema200']:.2f}`",
            f"Target: prior highs `${c['high_52w']:.2f}`",
        ]

    # ── 4. EXTENDED NEAR ATH ──
    elif from_ath > -8 and rsi > 75:
        verdict = "🟠 EXTENDED"
        zone = "Overbought Near ATH"
        reasons.append(f"RSI {rsi:.0f} — overbought at highs")
        reasons.append("Risk/reward not ideal for new entry")
        reasons.append("Better to wait for RSI to cool")
        next_steps = [
            f"Wait for RSI to pull back to 50-60",
            f"Better entry: EMA50 `${c['ema50']:.2f}`",
            f"If already holding: trail stop, don't add",
        ]

    # ── 5. STRONG DOWNTREND ──
    elif "DOWNTREND" in trend and not above_200:
        verdict = "🔴 AVOID"
        zone = "Falling Knife"
        reasons.append("Below EMA50 & EMA200 — confirmed downtrend")
        reasons.append("No base formed — high risk of continued drop")
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal")
        next_steps = [
            f"Wait for: close above EMA50 `${c['ema50']:.2f}`",
            f"Then confirm: EMA50 > EMA200 cross",
            f"Don't buy until structure recovers",
        ]

    # ── 6. NEAR 52W LOW BREAKING DOWN ──
    elif c['pct_from_52w_low'] < 8 and drop < -3:
        verdict = "⚠️ CAUTION"
        zone = "Breaking Down"
        reasons.append("Near 52W low — key support at risk")
        reasons.append("Wait for base before entering")
        next_steps = [
            f"Watch: holds above `${c['low_52w']:.2f}` (52W low)",
            f"Only enter after 2-3 days of stabilisation",
        ]

    # ── 7. RECOVERING ──
    elif "RECOVERING" in trend:
        if rsi > 55 and drop > 0:
            verdict = "🟡 WATCH"
            zone = "Recovery Attempt"
            reasons.append("Reclaiming EMA50 — potential recovery")
            reasons.append(f"Must clear EMA200 `${c['ema200']:.2f}` to confirm")
            next_steps = [
                f"Trigger: daily close above EMA200 `${c['ema200']:.2f}`",
                f"Then: small position, stop below EMA50",
            ]
        else:
            verdict = "⏸️ HOLD"
            zone = "Below EMA200"
            reasons.append("Below EMA200 — no structural confirmation")
            next_steps = [
                f"Wait for: reclaim EMA200 `${c['ema200']:.2f}`",
            ]

    # ── 8. MIXED ──
    elif "MIXED" in trend:
        if range_pos < 35 and rsi < 45:
            verdict = "🟡 WATCH"
            zone = "Potential Base"
            reasons.append("Lower 52W range — possible accumulation")
            reasons.append("Wait for trend confirmation")
            next_steps = [
                f"Trigger: RSI > 50 + close above EMA50 `${c['ema50']:.2f}`",
            ]
        elif rsi > 72:
            verdict = "🟠 EXTENDED"
            zone = "Overbought in Chop"
            reasons.append(f"RSI {rsi:.0f} — extended in mixed trend")
            reasons.append("High risk entry point")
            next_steps = [f"Wait for RSI pullback to 50-55"]
        else:
            verdict = "⏸️ NEUTRAL"
            zone = "No Clear Edge"
            reasons.append("Mixed signals — no directional conviction")
            next_steps = [
                f"Bull trigger: close above EMA50 `${c['ema50']:.2f}` + RSI > 55",
                f"Bear trigger: close below EMA200 `${c['ema200']:.2f}`",
            ]

    # ── 9. DEFAULT ──
    else:
        if above_50 and above_200 and rsi > 55:
            verdict = "🟡 WATCH"
            zone = "Building Momentum"
            reasons.append("Above both EMAs — structure improving")
            reasons.append(f"RSI {rsi:.0f} — momentum building")
            next_steps = [
                f"Better entry: pullback to EMA50 `${c['ema50']:.2f}`",
                f"Breakout entry: new high above `${c['high_52w']:.2f}`",
            ]
        else:
            verdict = "⏸️ NEUTRAL"
            zone = "No Clear Setup"
            reasons.append("No strong directional signal")
            next_steps = [
                f"Bull trigger: above EMA50 `${c['ema50']:.2f}` + RSI > 55",
                f"Bear trigger: below EMA200 `${c['ema200']:.2f}`",
            ]

    # ── Market context override ──
    if market_ctx:
        vix = market_ctx.get('^VIX', {}).get('price', 15)
        spy_pct = market_ctx.get('SPY', {}).get('pct', 0)
        if vix > 25 and spy_pct < -1.5 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
            verdict = "⚠️ WAIT"
            reasons.insert(0, f"Market bleeding — VIX {vix:.0f}, SPY {spy_pct:.1f}%")
            next_steps = ["Wait for market to stabilise before entering"]

    # ── Earnings override ──
    if any(x in verdict for x in ["BUY", "MOMENTUM", "WATCH"]):
        _, days_until = get_earnings_date(c['symbol'])
        if days_until is not None and days_until <= EARNINGS_WARNING_DAYS:
            verdict = "⚠️ WAIT — Earnings"
            zone = f"Earnings in {days_until}d"
            reasons.insert(0, f"Earnings in {days_until} days — skip new entries")
            next_steps = [f"Re-evaluate after earnings on {days_until}d"]

    return verdict, zone, reasons, next_steps


# ═══════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════

def normalise_symbol(raw):
    s = raw.strip().upper()
    aliases = {
        'BITCOIN': 'BTC-USD', 'BTC': 'BTC-USD',
        'ETHEREUM': 'ETH-USD', 'ETH': 'ETH-USD',
        'XRP': 'XRP-USD', 'RIPPLE': 'XRP-USD',
        'GOLD': 'GC=F',
    }
    return aliases.get(s, s)

def is_crypto(sym):
    return sym.endswith('-USD') or sym == 'GC=F'

def volume_label(vol_ratio):
    if vol_ratio >= 2.0:   return f"{vol_ratio:.1f}× avg 🔥 Unusually high"
    if vol_ratio >= 1.5:   return f"{vol_ratio:.1f}× avg ⬆️ Above average"
    if vol_ratio >= 0.8:   return f"{vol_ratio:.1f}× avg — Normal"
    return f"{vol_ratio:.1f}× avg ⬇️ Below average — weak move"

def ath_recency(ath_date_str):
    """Returns how recent the ATH was in human terms."""
    try:
        ath_dt = datetime.strptime(ath_date_str[:10], '%Y-%m-%d')
        days = (datetime.now() - ath_dt).days
        if days == 0:   return "set TODAY 🔥"
        if days == 1:   return "set YESTERDAY 🔥"
        if days <= 7:   return f"set {days} days ago"
        if days <= 30:  return f"set {days // 7}w ago"
        if days <= 365: return f"set {days // 30}mo ago"
        return f"set {days // 365}y ago"
    except Exception:
        return f"on {ath_date_str}"

def quick_poc(symbol):
    try:
        df = yf.download(symbol, period='3mo', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        recent = df.iloc[-60:]
        low = float(recent['Low'].min())
        high = float(recent['High'].max())
        if high <= low:
            return None
        bins = 30
        bin_edges = np.linspace(low, high, bins + 1)
        vol_at_price = np.zeros(bins)
        for i in range(len(recent)):
            bar_low = float(recent['Low'].iloc[i])
            bar_high = float(recent['High'].iloc[i])
            bar_vol = float(recent['Volume'].iloc[i])
            if bar_vol <= 0:
                continue
            bar_range = max(bar_high - bar_low, 1e-9)
            for b in range(bins):
                overlap = max(0, min(bar_high, bin_edges[b+1]) - max(bar_low, bin_edges[b]))
                if overlap > 0:
                    vol_at_price[b] += bar_vol * (overlap / bar_range)
        if vol_at_price.sum() == 0:
            return None
        poc_idx = int(np.argmax(vol_at_price))
        poc = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2
        return round(poc, 4)
    except Exception:
        return None

def recent_structure(symbol):
    """Returns (recent_support, recent_resistance) from last 20 daily bars."""
    try:
        df = yf.download(symbol, period='1mo', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 10:
            return None, None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        recent = df.iloc[-20:]
        support = float(recent['Low'].min())
        resistance = float(recent['High'].max())
        return round(support, 2), round(resistance, 2)
    except Exception:
        return None, None


# ═══════════════════════════════════════════════
# FORMAT MESSAGE — verdict at top, details below
# ═══════════════════════════════════════════════

def format_analysis(symbol, ctx, verdict, zone, reasons, next_steps,
                    ai_text, market_ctx, rs_score, rs_label, poc,
                    support, resistance):
    em = SYMBOL_EMOJI.get(symbol, '📊')
    c = ctx
    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    decimals = 4 if c['current'] < 10 else 2
    pf = f"{{:.{decimals}f}}"

    drop = c['day_change_pct']
    drop_em = "🟢" if drop >= 0 else "🔴"
    sign = "+" if drop >= 0 else ""

    # ── HEADER — verdict first ──
    msg = f"🔍 *ON-DEMAND ANALYSIS*\n"
    msg += f"{em} *{symbol}* • {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # ── VERDICT BANNER (top) ──
    msg += f"*{verdict}*\n"
    msg += f"_Zone: {zone}_\n"
    for r in reasons[:3]:
        msg += f"  • {r}\n"

    # ── AI SUMMARY (second — most valuable insight early) ──
    if ai_text:
        # Extract just the 💡 recommendation line for the top summary
        lines = ai_text.strip().split('\n')
        summary = next((l for l in lines if '💡' in l), None)
        if summary:
            msg += f"\n{summary}\n"

    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # ── PRICE SNAPSHOT ──
    msg += f"*💵 PRICE*\n`─────────────────`\n"
    msg += f"Live: `${pf.format(c['current'])}` ({drop_em} {sign}{drop:.2f}% today)\n"
    msg += f"Range: L `${pf.format(c['today_low'])}` → H `${pf.format(c['today_high'])}`\n"
    msg += f"Volume: {volume_label(c['vol_ratio'])}\n"

    # POC
    if poc:
        diff_pct = (c['current'] - poc) / poc * 100
        if abs(diff_pct) < 0.5:
            msg += f"🎯 *AT POC* `${pf.format(poc)}` — volume magnet\n"
        elif c['current'] > poc:
            msg += f"🎯 Above POC `${pf.format(poc)}` — buyers in control\n"
        else:
            msg += f"🎯 Below POC `${pf.format(poc)}` — sellers in control\n"

    # ── TECHNICALS ──
    msg += f"\n*📈 TECHNICALS*\n`─────────────────`\n"
    msg += f"{c['trend']}\n"

    if c['rsi'] < 30:       rsi_tag = "_(oversold)_"
    elif c['rsi'] > 70:     rsi_tag = "_(overbought)_"
    elif c['rsi'] > 60:     rsi_tag = "_(bullish)_"
    else:                   rsi_tag = "_(neutral)_"
    msg += f"RSI: `{c['rsi']:.0f}` {rsi_tag}\n"
    msg += f"EMA50: `${pf.format(c['ema50'])}` • EMA200: `${pf.format(c['ema200'])}`\n"

    above_50 = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if above_50 and above_200:      msg += "✅ Above EMA50 & EMA200\n"
    elif above_200 and not above_50: msg += "⚠️ Below EMA50, above EMA200\n"
    elif not above_200 and above_50: msg += "🔀 Above EMA50, below EMA200\n"
    else:                            msg += "🔴 Below both EMAs\n"

    # ── POSITION IN RANGE ──
    msg += f"\n*📏 POSITION*\n`─────────────────`\n"
    pos = int(c['range_pos'] / 10)
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"`{bar}` {c['range_pos']:.0f}% of 52W range\n"
    msg += f"52W: `${pf.format(c['low_52w'])}` → `${pf.format(c['high_52w'])}`\n"
    ath_rec = ath_recency(c['ath_date'])
    msg += f"ATH: `${pf.format(c['ath'])}` ({c['ath_pct']:+.1f}%) — {ath_rec}\n"

    # Recent structure levels
    if support and resistance:
        msg += f"Structure: Support `${pf.format(support)}` • Resistance `${pf.format(resistance)}`\n"

    # ── RELATIVE STRENGTH ──
    if rs_score is not None:
        sign_rs = "+" if rs_score >= 0 else ""
        msg += f"\n*💪 RS vs SPY (5d):* {rs_label} `{sign_rs}{rs_score}%`\n"

    # ── EARNINGS ──
    earnings_date, days_until = get_earnings_date(symbol)
    warn = format_earnings_warning(symbol, earnings_date, days_until)
    if warn:
        msg += f"\n*📅 EARNINGS*\n`─────────────────`\n{warn}\n"

    # ── MARKET ──
    if market_ctx:
        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})
        if spy or vix:
            msg += f"\n*🌍 MARKET*\n`─────────────────`\n"
            if spy:
                spy_em = "🟢" if spy.get('pct', 0) >= 0 else "🔴"
                msg += f"SPY: {spy_em} `{spy.get('pct', 0):+.2f}%`"
            if vix:
                vix_val = vix.get('price', 0)
                vix_em = "🔴" if vix_val > 25 else "🟡" if vix_val > 18 else "🟢"
                msg += f" • VIX: {vix_em} `{vix_val:.1f}`"
            msg += "\n"

    # ── ENTRY GUIDANCE (actionable next steps) ──
    msg += f"\n*💡 WHAT TO DO*\n`─────────────────`\n"
    for step in next_steps:
        msg += f"  → {step}\n"
    if not next_steps:
        msg += f"  → No clear edge — wait for setup\n"

    # ── FULL AI ANALYSIS ──
    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n`─────────────────`\n{ai_text}\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_AlphaEdge v7.0 • On-demand scan_"
    return msg


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        send_telegram("❌ No symbol provided. Just type a ticker e.g. `TSLA`")
        sys.exit(1)

    raw_symbol = sys.argv[1]
    symbol = normalise_symbol(raw_symbol)

    print(f"\n🔍 Single scan v2.0: {symbol} @ {now_est().strftime('%H:%M %Z')}")

    # Immediate ack
    send_telegram(f"🔍 Analysing *{symbol}*... please wait ~30s", silent=True)

    # Fetch
    print(f"  → Fetching context...")
    ctx = get_full_context(symbol)
    if not ctx:
        send_telegram(
            f"❌ Could not fetch data for *{symbol}*.\n"
            f"Check the symbol is valid (e.g. `TSLA`, `BTC-USD`, `GC=F`)"
        )
        sys.exit(1)

    print(f"  → Market context...")
    market_ctx = get_market_ctx()

    print(f"  → Verdict...")
    verdict, zone, reasons, next_steps = get_verdict(ctx, market_ctx)

    print(f"  → Relative strength...")
    rs_score, rs_label = calc_relative_strength(ctx)

    print(f"  → POC...")
    poc = quick_poc(symbol)

    print(f"  → Structure levels...")
    support, resistance = recent_structure(symbol)

    print(f"  → AI analysis...")
    ai_text = ai_analyze_drop(ctx, market_ctx)
    print(f"  → AI: {'GOT RESPONSE' if ai_text else 'NO RESPONSE'}")

    print(f"  → Building message...")
    msg = format_analysis(
        symbol, ctx, verdict, zone, reasons, next_steps,
        ai_text, market_ctx, rs_score, rs_label, poc,
        support, resistance
    )

    success = send_telegram(msg, silent=False)
    if success:
        print(f"✅ Analysis sent for {symbol}")
        logging.info(f"Single scan v2.0: {symbol} | verdict={verdict}")
    else:
        print(f"❌ Failed to send for {symbol}")
        logging.error(f"Single scan send failed: {symbol}")


if __name__ == "__main__":
    main()
