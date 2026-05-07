"""
ALPHAEDGE MARKET INTELLIGENCE MODULE
═══════════════════════════════════════════════════════════════
Provides context, not just signals:
• Big move detection (±5%, ±10%)
• ATH / 52W / position in range
• AI-powered "why is this dropping?" analysis
• Clear Buy Zone / Sell Zone / Hold verdicts
• Sector bleed detection
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

EST = ZoneInfo("America/New_York")

# Reuse scanner.py credentials + state
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

STATE_FILE = 'scanner_state.json'

# ═══════════════════════════════════════════════
# THRESHOLDS
# ═══════════════════════════════════════════════
BIG_DROP_WARN = -5.0          # % intraday drop for warning
BIG_DROP_CRITICAL = -10.0     # % intraday drop for critical
BIG_GAIN_ALERT = 8.0          # % intraday gain worth alerting
NEAR_52W_LOW_PCT = 10.0       # within 10% of 52W low
ATH_PULLBACK_ALERT = -15.0    # more than 15% from ATH

COOLDOWN_HOURS = 4            # Don't re-alert same symbol within 4h

# Sector mapping (expand as needed)
SECTORS = {
    'AI/Semis': ['NVDA', 'AMD', 'MU', 'SNDK', 'NBIS'],
    'Crypto': ['BTC-USD', 'ETH-USD', 'XRP-USD'],
    'Crypto-Adj': ['IREN', 'COIN', 'MSTR'],
    'Quantum': ['IONQ', 'RGTI', 'QBTS'],
    'Nuclear/Energy': ['OKLO', 'UAMY'],
    'Mega Tech': ['GOOGL', 'MSFT', 'META', 'AMZN', 'AAPL'],
    'EV/Auto': ['TSLA'],
    'Fintech': ['SOFI'],
    'Biotech': ['NVO', 'WGRX'],
    'Streaming': ['NFLX'],
    'Safe Haven': ['GC=F'],
}

# Symbols to monitor (superset of scanner watchlist)
MONITOR_LIST = [
    'BTC-USD', 'ETH-USD', 'XRP-USD', 'GC=F',
    'NVDA', 'TSLA', 'AMD', 'MSFT', 'META', 'AMZN', 'GOOGL', 'NFLX', 'AAPL',
    'MU', 'SNDK', 'NBIS', 'IONQ', 'RGTI', 'QBTS',
    'OKLO', 'IREN', 'UAMY', 'WGRX', 'SOFI', 'NVO',
]

SYMBOL_EMOJI = {
    'BTC-USD': '₿', 'ETH-USD': 'Ξ', 'XRP-USD': '◇', 'GC=F': '🥇',
    'NVDA': '💎', 'TSLA': '🚘', 'META': '👓', 'AMZN': '📦',
    'GOOGL': '🔍', 'MSFT': '🪟', 'NFLX': '🎬', 'AMD': '⚡', 'AAPL': '🍎',
    'MU': '💾', 'SNDK': '💽', 'NBIS': '🌐',
    'IONQ': '⚛️', 'RGTI': '🧪', 'QBTS': '🔬',
    'OKLO': '☢️', 'IREN': '🪙', 'UAMY': '⚒️', 'WGRX': '💊',
    'SOFI': '🏦', 'NVO': '💉',
}

def now_est(): return datetime.now(EST)

def load_json(path, default):
    try:
        with open(path, 'r') as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, 'w') as f: json.dump(data, f, indent=2, default=str)

# ═══════════════════════════════════════════════
# DATA FETCHERS
# ═══════════════════════════════════════════════

def get_full_context(symbol):
    """Fetches all needed data in one pass — daily + intraday."""
    try:
        # Daily for ATH / 52W / trend
        daily = yf.download(symbol, period='5y', interval='1d',
                           progress=False, auto_adjust=True)
        if daily.empty or len(daily) < 50:
            return None
        if isinstance(daily.columns, pd.MultiIndex):
            daily.columns = daily.columns.get_level_values(0)

        # Intraday for today's move
        intraday = yf.download(symbol, period='2d', interval='5m',
                              progress=False, auto_adjust=True)
        if intraday.empty:
            return None
        if isinstance(intraday.columns, pd.MultiIndex):
            intraday.columns = intraday.columns.get_level_values(0)

        current = float(intraday['Close'].iloc[-1])
        prev_close = float(daily['Close'].iloc[-2])
        today_open = float(intraday['Open'].iloc[0])
        today_high = float(intraday['High'].max())
        today_low = float(intraday['Low'].min())

        # % moves
        day_change_pct = (current - prev_close) / prev_close * 100
        intraday_pct = (current - today_open) / today_open * 100

        # ATH + 52W
        ath = float(daily['High'].max())
        ath_date = daily['High'].idxmax()
        low_52w = float(daily['Low'].iloc[-252:].min()) if len(daily) >= 252 else float(daily['Low'].min())
        high_52w = float(daily['High'].iloc[-252:].max()) if len(daily) >= 252 else float(daily['High'].max())

        ath_pct = (current - ath) / ath * 100
        pct_from_52w_low = (current - low_52w) / low_52w * 100
        pct_from_52w_high = (current - high_52w) / high_52w * 100

        # Position in 52W range (0-100)
        range_pos = ((current - low_52w) / (high_52w - low_52w) * 100) if high_52w > low_52w else 50

        # Trend indicators (daily)
        ema20 = daily['Close'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = daily['Close'].ewm(span=50, adjust=False).mean().iloc[-1]
        ema200 = daily['Close'].ewm(span=200, adjust=False).mean().iloc[-1] if len(daily) >= 200 else ema50

        # RSI daily
        delta = daily['Close'].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100/(1+rs)).iloc[-1]

        # Volume vs avg
        vol_today = float(intraday['Volume'].sum())
        vol_avg_20d = float(daily['Volume'].iloc[-20:].mean())
        vol_ratio = vol_today / vol_avg_20d if vol_avg_20d > 0 else 1.0

        # Daily trend classification
        if current > ema20 > ema50 > ema200:
            trend = "🚀 STRONG UPTREND"
        elif current > ema50 > ema200:
            trend = "📈 UPTREND"
        elif current > ema200 and current < ema50:
            trend = "🔄 PULLBACK IN UPTREND"
        elif current < ema200 and current > ema50:
            trend = "🔀 RECOVERING"
        elif current < ema50 < ema200:
            trend = "📉 DOWNTREND"
        elif current < ema20 < ema50 < ema200:
            trend = "💀 STRONG DOWNTREND"
        else:
            trend = "⚖️ MIXED"

        return {
            'symbol': symbol,
            'current': current,
            'prev_close': prev_close,
            'today_open': today_open,
            'today_high': today_high,
            'today_low': today_low,
            'day_change_pct': day_change_pct,
            'intraday_pct': intraday_pct,
            'ath': ath,
            'ath_date': ath_date.strftime('%Y-%m-%d') if hasattr(ath_date, 'strftime') else str(ath_date)[:10],
            'ath_pct': ath_pct,
            'low_52w': low_52w,
            'high_52w': high_52w,
            'pct_from_52w_low': pct_from_52w_low,
            'pct_from_52w_high': pct_from_52w_high,
            'range_pos': range_pos,
            'ema20': ema20, 'ema50': ema50, 'ema200': ema200,
            'rsi': float(rsi) if not pd.isna(rsi) else 50,
            'vol_ratio': vol_ratio,
            'trend': trend,
        }
    except Exception as e:
        logging.error(f"Context fetch {symbol}: {e}")
        return None

# ═══════════════════════════════════════════════
# VERDICT ENGINE
# ═══════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None):
    """Returns a clear BUY ZONE / HOLD / AVOID verdict with reasoning."""
    c = ctx
    rsi = c['rsi']
    trend = c['trend']
    drop = c['day_change_pct']
    from_ath = c['ath_pct']
    range_pos = c['range_pos']

    reasons = []
    verdict = None
    zone = None

    # Strong Uptrend + RSI oversold + pullback = good buy
    if "UPTREND" in trend and rsi < 40 and drop < 0:
        verdict = "🟢 BUY ZONE"
        zone = "Accumulation"
        reasons.append("Healthy pullback in uptrend")
        reasons.append(f"Daily RSI oversold ({rsi:.0f})")
        if from_ath > -20:
            reasons.append(f"Near ATH — strong stock pulling back")

    # Strong downtrend + falling = avoid
    elif "DOWNTREND" in trend and drop < -3:
        verdict = "🔴 AVOID"
        zone = "Falling Knife"
        reasons.append("Continuation of downtrend")
        reasons.append(f"Below EMA50 and EMA200")
        if rsi < 30:
            reasons.append("Oversold but no reversal signal yet")

    # Near 52W low + any trend = cautious
    elif c['pct_from_52w_low'] < 5 and drop < -5:
        verdict = "⚠️ CAUTION"
        zone = "Breaking Down"
        reasons.append("Near 52W low — support at risk")
        reasons.append("Wait for base formation before buying")

    # Healthy range trade
    elif "MIXED" in trend or "RECOVERING" in trend:
        if range_pos < 30 and rsi < 45:
            verdict = "🟡 WATCH"
            zone = "Potential accumulation"
            reasons.append("Lower end of 52W range")
            reasons.append("Wait for trend confirmation")
        else:
            verdict = "⏸️ HOLD"
            zone = "No edge"
            reasons.append("Mixed signals — wait for clarity")

    # Pullback in uptrend (sweet spot)
    elif "PULLBACK" in trend and rsi < 50:
        verdict = "🟢 BUY ZONE"
        zone = "Pullback"
        reasons.append("Above EMA200, pulling back to EMA50")
        reasons.append(f"RSI {rsi:.0f} — room to run")

    # Overbought
    elif rsi > 75 and drop > 2:
        verdict = "🟠 TAKE PROFITS"
        zone = "Extended"
        reasons.append(f"RSI overbought ({rsi:.0f})")
        reasons.append("Consider trimming, not entering")

    # Default
    else:
        if drop < -5:
            verdict = "⚠️ WATCH"
            zone = "Sharp drop — needs context"
            reasons.append("Large move — wait for stabilization")
        else:
            verdict = "⏸️ NEUTRAL"
            zone = "No clear setup"
            reasons.append("Wait for better entry")

    # Market context override
    if market_ctx:
        vix = market_ctx.get('^VIX', {}).get('price', 15)
        spy_pct = market_ctx.get('SPY', {}).get('pct', 0)
        if vix > 25 and spy_pct < -1.5:
            if "BUY" in verdict:
                verdict = "⚠️ WAIT"
                reasons.insert(0, f"Market bleeding — VIX {vix:.0f}, SPY {spy_pct:.1f}%")

    return verdict, zone, reasons

# ═══════════════════════════════════════════════
# AI CONTEXT ANALYZER
# ═══════════════════════════════════════════════

def ai_analyze_drop(ctx, market_ctx=None):
    """Asks Gemini WHY the drop is happening + is it a buy."""
    if not GEMINI_API_KEY:
        return None

    c = ctx
    mkt_str = ""
    if market_ctx:
        spy_pct = market_ctx.get('SPY', {}).get('pct', 0)
        qqq_pct = market_ctx.get('QQQ', {}).get('pct', 0)
        vix = market_ctx.get('^VIX', {}).get('price', 15)
        mkt_str = f"\nMarket: SPY {spy_pct:+.2f}%, QQQ {qqq_pct:+.2f}%, VIX {vix:.1f}"

    prompt = f"""You're a senior market analyst. Analyze this stock's move in EXACTLY 4 short lines (max 110 chars each).

{c['symbol']} — Today: {c['day_change_pct']:+.2f}% • Price: ${c['current']:.2f}
Range: 52W Low ${c['low_52w']:.2f} / High ${c['high_52w']:.2f} / ATH ${c['ath']:.2f} ({c['ath_pct']:+.1f}% from ATH on {c['ath_date']})
Trend: {c['trend']} | RSI: {c['rsi']:.0f} | Position in 52W range: {c['range_pos']:.0f}%
Volume: {c['vol_ratio']:.1f}× avg{mkt_str}

Respond EXACTLY:
📊 [Context: why likely moving — sector/market/company/technical]
🎯 [Is this healthy pullback, correction, or bleed? Be specific]
💡 [Entry advice: buy zone, wait for support, avoid — with price levels]
🔮 [Short-term outlook 1-5 days]

Do NOT add bullet points or extra headers. 4 lines only."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        r = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 400}
        }, timeout=20)
        if r.status_code == 200:
            data = r.json()
            if 'candidates' in data and len(data['candidates']) > 0:
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        logging.error(f"AI drop analysis: {e}")
    return None

# ═══════════════════════════════════════════════
# MARKET CONTEXT
# ═══════════════════════════════════════════════

def get_market_ctx():
    try:
        data = {}
        for t in ['SPY', 'QQQ', '^VIX']:
            df = yf.download(t, period='5d', interval='1d', progress=False, auto_adjust=True)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                last = float(df['Close'].iloc[-1])
                prev = float(df['Close'].iloc[-2])
                data[t] = {'price': last, 'pct': (last - prev) / prev * 100}
        return data
    except: return None

# ═══════════════════════════════════════════════
# BIG MOVE ALERT FORMATTER
# ═══════════════════════════════════════════════

def format_big_move_alert(ctx, verdict, zone, reasons, ai_text, market_ctx):
    """The main alert when a stock drops/pops significantly."""
    c = ctx
    em = SYMBOL_EMOJI.get(c['symbol'], '📊')
    drop = c['day_change_pct']

    # Severity
    if drop <= BIG_DROP_CRITICAL:
        header_emoji = "🚨🩸"
        severity = "CRITICAL DROP"
    elif drop <= BIG_DROP_WARN:
        header_emoji = "⚠️📉"
        severity = "BIG DROP"
    elif drop >= BIG_GAIN_ALERT:
        header_emoji = "🚀📈"
        severity = "BIG GAIN"
    else:
        return None

    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    msg = f"{header_emoji} *{severity}* — {em} *{c['symbol']}*\n"
    msg += f"🕒 {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"

    # Price + move
    sign = "+" if drop >= 0 else ""
    drop_em = "🔴" if drop < 0 else "🟢"
    msg += f"💵 *Price:* `${c['current']:.2f}` ({drop_em} {sign}{drop:.2f}% today)\n"
    msg += f"📊 *Range:* L `${c['today_low']:.2f}` → H `${c['today_high']:.2f}`\n"
    msg += f"📈 *Volume:* {c['vol_ratio']:.1f}× average\n"

    # ═══ VERDICT (the star of the show) ═══
    msg += f"\n*🎯 VERDICT: {verdict}*\n"
    msg += f"_Zone: {zone}_\n"
    for r in reasons[:3]:
        msg += f"  • {r}\n"

    # ═══ CONTEXT SECTION ═══
    msg += f"\n*📏 POSITIONAL CONTEXT*\n"
    msg += f"`─────────────────`\n"

    # ATH distance with visual
    ath_pct = c['ath_pct']
    if ath_pct > -5:
        ath_tag = "🏔️ AT/NEAR ATH"
    elif ath_pct > -15:
        ath_tag = "📍 Near ATH"
    elif ath_pct > -30:
        ath_tag = "📉 Pullback from ATH"
    elif ath_pct > -50:
        ath_tag = "💀 Deep drawdown from ATH"
    else:
        ath_tag = "⚰️ Far from ATH"

    msg += f"🏔️ *ATH:* `${c['ath']:.2f}` ({c['ath_pct']:+.1f}%) {ath_tag}\n"
    msg += f"   _Set on {c['ath_date']}_\n"

    # 52W range visual bar
    pos = int(c['range_pos'] / 10)
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"📊 *52W Range:* `${c['low_52w']:.2f}` → `${c['high_52w']:.2f}`\n"
    msg += f"   `{bar}` {c['range_pos']:.0f}% of range\n"
    msg += f"   From low: {c['pct_from_52w_low']:+.1f}% • From high: {c['pct_from_52w_high']:+.1f}%\n"

    # Trend + technicals
    msg += f"\n*📈 TREND & TECHNICALS*\n"
    msg += f"`─────────────────`\n"
    msg += f"Trend: {c['trend']}\n"
    msg += f"RSI (Daily): `{c['rsi']:.0f}`"
    if c['rsi'] < 30: msg += " _(oversold)_\n"
    elif c['rsi'] > 70: msg += " _(overbought)_\n"
    else: msg += " _(neutral)_\n"
    msg += f"EMA50: `${c['ema50']:.2f}` • EMA200: `${c['ema200']:.2f}`\n"

    # Price vs key MAs
    above_50 = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    ma_status = ""
    if above_50 and above_200: ma_status = "✅ Above both EMA50 & EMA200 (bullish structure)"
    elif above_200 and not above_50: ma_status = "⚠️ Below EMA50, above EMA200 (pullback)"
    elif not above_200 and above_50: ma_status = "🔀 Above EMA50, below EMA200 (recovery attempt)"
    else: ma_status = "🔴 Below EMA50 & EMA200 (bearish structure)"
    msg += f"{ma_status}\n"

    # Market context
    if market_ctx:
        spy = market_ctx.get('SPY', {}).get('pct', 0)
        vix = market_ctx.get('^VIX', {}).get('price', 15)
        msg += f"\n*🌍 MARKET*\n"
        msg += f"`─────────────────`\n"
        spy_em = "🔴" if spy < 0 else "🟢"
        msg += f"SPY: {spy_em} `{spy:+.2f}%` • VIX: `{vix:.1f}`\n"
        if vix > 22:
            msg += f"⚠️ _Elevated VIX — broad risk-off_\n"
        elif spy < -1 and drop < -5:
            msg += f"⚠️ _Moving with market bleed_\n"
        elif spy > 0 and drop < -5:
            msg += f"🚨 _Stock-specific weakness — market is UP_\n"

    # AI analysis
    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n"
        msg += f"`─────────────────`\n"
        msg += f"{ai_text}\n"

    # Entry guidance
    msg += f"\n*💡 ENTRY GUIDANCE*\n"
    msg += f"`─────────────────`\n"
    if "BUY" in verdict:
        support1 = min(c['ema50'], c['low_52w'] * 1.03)
        support2 = c['ema200']
        msg += f"🟢 *Buy Zone:* `${support1:.2f}` – `${c['current']:.2f}`\n"
        msg += f"🛡️ *Support:* `${support2:.2f}` (EMA200)\n"
        msg += f"🚪 *Invalidation:* Below `${c['low_52w']:.2f}` (52W low)\n"
    elif "AVOID" in verdict or "WAIT" in verdict:
        msg += f"🚫 *Don't enter now*\n"
        msg += f"⏳ *Wait for:* Base formation above `${c['ema200']:.2f}`\n"
        msg += f"👀 *Trigger:* RSI reversal + price reclaiming EMA50 `${c['ema50']:.2f}`\n"
    elif "CAUTION" in verdict or "WATCH" in verdict:
        msg += f"👀 *Watch key level:* `${c['ema50']:.2f}` (EMA50)\n"
        msg += f"🟡 *Scale-in zone:* `${c['ema200']:.2f}` if holds\n"
    else:
        msg += f"⏸️ *No edge here — wait for cleaner setup*\n"

    return msg

# ═══════════════════════════════════════════════
# SECTOR BLEED DETECTOR
# ═══════════════════════════════════════════════

def check_sector_bleeds(all_contexts):
    """Detects when an entire sector is bleeding together."""
    sector_moves = {}
    for sector, symbols in SECTORS.items():
        moves = []
        for sym in symbols:
            if sym in all_contexts and all_contexts[sym]:
                moves.append((sym, all_contexts[sym]['day_change_pct']))
        if len(moves) >= 2:
            avg = sum(m[1] for m in moves) / len(moves)
            bleeding = [m for m in moves if m[1] < -2]
            if avg < -2 and len(bleeding) >= max(2, len(moves) // 2):
                sector_moves[sector] = {
                    'avg': avg,
                    'bleeding': bleeding,
                    'all': moves,
                }
    return sector_moves

def format_sector_bleed_alert(sector_moves):
    if not sector_moves:
        return None

    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%I:%M %p {tz}')

    msg = f"🩸 *SECTOR BLEED DETECTED*\n"
    msg += f"🕒 {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"

    for sector, data in sorted(sector_moves.items(), key=lambda x: x[1]['avg']):
        msg += f"\n🔻 *{sector}* (avg {data['avg']:+.2f}%)\n"
        for sym, pct in sorted(data['all'], key=lambda x: x[1]):
            em = SYMBOL_EMOJI.get(sym, '📊')
            pct_em = "🔴" if pct < -5 else "🟠" if pct < -2 else "🟡" if pct < 0 else "🟢"
            msg += f"  {em} {sym}: {pct_em} `{pct:+.2f}%`\n"

    msg += f"\n💡 _Avoid longs in bleeding sectors. Wait for stabilization._"
    return msg

# ═══════════════════════════════════════════════
# COOLDOWN
# ═══════════════════════════════════════════════

def can_alert(symbol, alert_type='big_move'):
    state = load_json(STATE_FILE, {})
    key = f"intel_{alert_type}_{symbol}"
    last = state.get(key)
    if last:
        try:
            dt = datetime.fromisoformat(last)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=EST)
            if now_est() - dt < timedelta(hours=COOLDOWN_HOURS):
                return False
        except: pass
    state[key] = now_est().isoformat()
    save_json(STATE_FILE, state)
    return True

# ═══════════════════════════════════════════════
# SEND TELEGRAM
# ═══════════════════════════════════════════════

def send_telegram(msg, silent=False):
    if not TELEGRAM_TOKEN or not CHAT_ID: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': CHAT_ID,
            'text': msg,
            'parse_mode': 'Markdown',
            'disable_notification': silent
        }, timeout=10)
        return r.status_code == 200
    except: return False

# ═══════════════════════════════════════════════
# MAIN INTEL SCAN
# ═══════════════════════════════════════════════

def run_intel_scan():
    print(f"\n🧠 Market Intelligence Scan @ {now_est().strftime('%H:%M %Z')}")
    market_ctx = get_market_ctx()
    all_contexts = {}
    alerts_fired = 0

    for symbol in MONITOR_LIST:
        try:
            print(f"  → {symbol:10s}...", end=" ")
            ctx = get_full_context(symbol)
            time.sleep(0.3)
            if not ctx:
                print("—")
                continue
            all_contexts[symbol] = ctx

            drop = ctx['day_change_pct']

            # Trigger conditions
            big_move = (drop <= BIG_DROP_WARN or drop >= BIG_GAIN_ALERT)

            if big_move:
                if not can_alert(symbol, 'big_move'):
                    print(f"{drop:+.1f}% 🔕 cooldown")
                    continue

                verdict, zone, reasons = get_verdict(ctx, market_ctx)
                ai = ai_analyze_drop(ctx, market_ctx) if abs(drop) >= 5 else None
                msg = format_big_move_alert(ctx, verdict, zone, reasons, ai, market_ctx)

                if msg:
                    send_telegram(msg, silent=False)
                    alerts_fired += 1
                    print(f"{drop:+.2f}% 🚨 ALERT SENT")
            else:
                print(f"{drop:+.2f}%")

        except Exception as e:
            print(f"💥 {e}")
            logging.error(f"Intel {symbol}: {e}")

    # Sector bleed
    sector_moves = check_sector_bleeds(all_contexts)
    if sector_moves:
        state = load_json(STATE_FILE, {})
        last_sector = state.get('last_sector_bleed')
        send = True
        if last_sector:
            try:
                dt = datetime.fromisoformat(last_sector)
                if dt.tzinfo is None: dt = dt.replace(tzinfo=EST)
                if now_est() - dt < timedelta(hours=4): send = False
            except: pass
        if send:
            sector_msg = format_sector_bleed_alert(sector_moves)
            if sector_msg:
                send_telegram(sector_msg, silent=False)
                alerts_fired += 1
                state['last_sector_bleed'] = now_est().isoformat()
                save_json(STATE_FILE, state)
                print(f"🩸 Sector bleed alert sent")

    print(f"\n✅ Intel scan done — {alerts_fired} alerts fired")
    logging.info(f"Intel scan | Alerts: {alerts_fired}")

if __name__ == "__main__":
    logging.basicConfig(
        filename=f'logs/intel_{now_est().strftime("%Y-%m-%d")}.log',
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s'
    )
    Path('logs').mkdir(exist_ok=True)
