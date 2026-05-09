"""
ALPHAEDGE SINGLE SCAN v1.0
═══════════════════════════════════════════════════════════════
On-demand analysis triggered by Telegram message.
Usage: python single_scan.py TSLA
       python single_scan.py BTC-USD

Sends a full analysis alert to Telegram:
• Live price + day change
• Trend + technicals (RSI, EMA, ATH, 52W)
• Verdict (BUY ZONE / AVOID / WAIT etc.)
• Volume profile / POC
• Earnings warning
• Relative strength vs SPY
• AI analysis (Gemini)
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

# ── imports from existing modules ──
from market_intel import (
    get_full_context, get_verdict, get_market_ctx,
    calc_relative_strength, get_earnings_date, format_earnings_warning,
    ai_analyze_drop, SYMBOL_EMOJI, send_telegram, now_est
)

try:
    import yfinance as yf
    import numpy as np
    import pandas as pd
except ImportError as e:
    print(f"Missing dependency: {e}")
    sys.exit(1)


# ═══════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════

def normalise_symbol(raw):
    """Clean up user input: tsla → TSLA, bitcoin → BTC-USD, etc."""
    s = raw.strip().upper()
    aliases = {
        'BITCOIN': 'BTC-USD',
        'BTC':     'BTC-USD',
        'ETHEREUM': 'ETH-USD',
        'ETH':     'ETH-USD',
        'XRP':     'XRP-USD',
        'RIPPLE':  'XRP-USD',
        'GOLD':    'GC=F',
    }
    return aliases.get(s, s)

def is_crypto(sym):
    return sym.endswith('-USD') or sym == 'GC=F'


# ═══════════════════════════════════════════════
# POC (reused from scanner.py logic)
# ═══════════════════════════════════════════════

def quick_poc(symbol):
    """Fetch daily bars and compute a simple POC for context."""
    try:
        import numpy as np
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


# ═══════════════════════════════════════════════
# FORMAT ANALYSIS MESSAGE
# ═══════════════════════════════════════════════

def format_analysis(symbol, ctx, verdict, zone, reasons, ai_text,
                    market_ctx, rs_score, rs_label, poc):
    em = SYMBOL_EMOJI.get(symbol, '📊')
    c = ctx
    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    decimals = 4 if c['current'] < 10 else 2
    price_fmt = f"{{:.{decimals}f}}"

    drop = c['day_change_pct']
    drop_em = "🟢" if drop >= 0 else "🔴"
    sign = "+" if drop >= 0 else ""

    msg = f"🔍 *ON-DEMAND ANALYSIS*\n"
    msg += f"{em} *{symbol}* • {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # Price snapshot
    msg += f"*💵 PRICE*\n`─────────────────`\n"
    msg += f"Live: `${price_fmt.format(c['current'])}` "
    msg += f"({drop_em} {sign}{drop:.2f}% today)\n"
    msg += f"Range: L `${price_fmt.format(c['today_low'])}` → H `${price_fmt.format(c['today_high'])}`\n"
    msg += f"Volume: {c['vol_ratio']:.1f}× average\n"

    # POC
    if poc:
        diff_pct = (c['current'] - poc) / poc * 100
        if abs(diff_pct) < 0.5:
            poc_line = f"🎯 *AT POC* `${price_fmt.format(poc)}` — volume magnet"
        elif c['current'] > poc:
            poc_line = f"🎯 *Above POC* `${price_fmt.format(poc)}` — buyers in control"
        else:
            poc_line = f"🎯 *Below POC* `${price_fmt.format(poc)}` — sellers in control"
        msg += f"{poc_line}\n"

    # Verdict
    msg += f"\n*🎯 VERDICT: {verdict}*\n"
    msg += f"_Zone: {zone}_\n"
    for r in reasons[:3]:
        msg += f"  • {r}\n"

    # Trend & technicals
    msg += f"\n*📈 TREND & TECHNICALS*\n`─────────────────`\n"
    msg += f"{c['trend']}\n"

    if c['rsi'] < 30:       rsi_tag = "_(oversold)_"
    elif c['rsi'] > 70:     rsi_tag = "_(overbought)_"
    else:                   rsi_tag = "_(neutral)_"
    msg += f"RSI: `{c['rsi']:.0f}` {rsi_tag}\n"
    msg += f"EMA50: `${price_fmt.format(c['ema50'])}` • EMA200: `${price_fmt.format(c['ema200'])}`\n"

    above_50 = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if above_50 and above_200:
        msg += "✅ Above EMA50 & EMA200\n"
    elif above_200 and not above_50:
        msg += "⚠️ Below EMA50, above EMA200\n"
    elif not above_200 and above_50:
        msg += "🔀 Above EMA50, below EMA200\n"
    else:
        msg += "🔴 Below both EMAs\n"

    # Positional context
    msg += f"\n*📏 POSITION IN RANGE*\n`─────────────────`\n"
    pos = int(c['range_pos'] / 10)
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"`{bar}` {c['range_pos']:.0f}% of 52W range\n"
    msg += f"52W: `${price_fmt.format(c['low_52w'])}` → `${price_fmt.format(c['high_52w'])}`\n"
    msg += f"ATH: `${price_fmt.format(c['ath'])}` ({c['ath_pct']:+.1f}%) on {c['ath_date']}\n"

    # Relative strength
    if rs_score is not None:
        sign_rs = "+" if rs_score >= 0 else ""
        msg += f"\n*💪 RS vs SPY (5d):* {rs_label} `{sign_rs}{rs_score}%`\n"

    # Earnings
    earnings_date, days_until = get_earnings_date(symbol)
    warn = format_earnings_warning(symbol, earnings_date, days_until)
    if warn:
        msg += f"\n*📅 EARNINGS*\n`─────────────────`\n{warn}\n"

    # Market context
    if market_ctx:
        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})
        if spy or vix:
            msg += f"\n*🌍 MARKET*\n`─────────────────`\n"
            if spy:
                spy_em = "🟢" if spy.get('pct', 0) >= 0 else "🔴"
                msg += f"SPY: {spy_em} `{spy.get('pct', 0):+.2f}%`"
            if vix:
                msg += f" • VIX: `{vix.get('price', 0):.1f}`"
            msg += "\n"

    # AI
    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n`─────────────────`\n{ai_text}\n"

    # Entry guidance
    msg += f"\n*💡 ENTRY GUIDANCE*\n`─────────────────`\n"
    if "BUY" in verdict:
        support = max(c['ema200'], c['current'] * 0.95)
        msg += f"🟢 *Buy Zone:* `${price_fmt.format(support)}` – `${price_fmt.format(c['current'])}`\n"
        msg += f"🛡️ *Support:* `${price_fmt.format(c['ema200'])}` (EMA200)\n"
        msg += f"🚪 *Invalidation:* Below `${price_fmt.format(c['ema200'] * 0.97)}`\n"
    elif "AVOID" in verdict or "WAIT" in verdict:
        msg += f"🚫 *Don't enter now*\n"
        msg += f"👀 *Watch:* Reclaim EMA50 `${price_fmt.format(c['ema50'])}`\n"
    elif "CAUTION" in verdict or "WATCH" in verdict:
        msg += f"👀 *Watch:* `${price_fmt.format(c['ema50'])}` (EMA50)\n"
        msg += f"🟡 *Scale-in:* `${price_fmt.format(c['ema200'])}` if holds\n"
    else:
        msg += f"⏸️ *No edge — wait for cleaner setup*\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Powered by AlphaEdge v7.0_"
    return msg


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        err = "❌ No symbol provided.\nUsage: `python single_scan.py TSLA`"
        send_telegram(err)
        sys.exit(1)

    raw_symbol = sys.argv[1]
    symbol = normalise_symbol(raw_symbol)

    print(f"\n🔍 Single scan: {symbol} @ {now_est().strftime('%H:%M %Z')}")

    # Acknowledge receipt immediately
    ack = f"🔍 Analysing *{symbol}*... please wait ~30s"
    send_telegram(ack, silent=True)

    # Fetch everything
    print(f"  → Fetching context...")
    ctx = get_full_context(symbol)
    if not ctx:
        err = f"❌ Could not fetch data for *{symbol}*.\nCheck the symbol is valid (e.g. TSLA, BTC-USD, GC=F)."
        send_telegram(err)
        sys.exit(1)

    print(f"  → Market context...")
    market_ctx = get_market_ctx()

    print(f"  → Verdict...")
    verdict, zone, reasons = get_verdict(ctx, market_ctx)

    print(f"  → Relative strength...")
    rs_score, rs_label = calc_relative_strength(ctx)

    print(f"  → POC...")
    poc = quick_poc(symbol)

    print(f"  → AI analysis...")
    ai_text = ai_analyze_drop(ctx, market_ctx)

    print(f"  → Building message...")
    msg = format_analysis(
        symbol, ctx, verdict, zone, reasons,
        ai_text, market_ctx, rs_score, rs_label, poc
    )

    success = send_telegram(msg, silent=False)
    if success:
        print(f"✅ Analysis sent for {symbol}")
        logging.info(f"Single scan sent: {symbol} | verdict={verdict}")
    else:
        print(f"❌ Failed to send analysis for {symbol}")
        logging.error(f"Single scan send failed: {symbol}")


if __name__ == "__main__":
    main()
