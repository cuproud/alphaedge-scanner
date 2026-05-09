"""
ALPHAEDGE SINGLE SCAN v3.0 — FULL COMMAND ROUTER
═══════════════════════════════════════════════════════════════
Handles all bot commands dispatched from Pipedream:

  analyze_symbol  → full/short analysis, daily/weekly/monthly
  bot_command     → alert, cancel_alert, list_alerts,
                    scan, top, brief, help

v3.0 NEW:
  • Multi-timeframe verdict (daily/weekly/monthly row)
  • Squeeze detection
  • RSI divergence warning
  • Sector health context
  • Smarter AI prompt (pre-interpreted)
  • Catalyst detection in AI
  • Price alert system (set/cancel/list/check)
  • 2-stage proximity warnings (2% before target)
  • Alert expiry after 30 days + 1-day warning
  • Watchlist scan (ranked by momentum)
  • Top movers from symbols.yaml
  • On-demand brief (auto morning/evening/weekend)
  • Symbol validation before dispatch
  • Help command
"""

import sys
import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
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
    ai_analyze_drop, SYMBOL_EMOJI, SECTORS, SYMBOL_TO_SECTOR,
    send_telegram, now_est, load_json, save_json,
    EARNINGS_WARNING_DAYS
)

try:
    import yfinance as yf
    import numpy as np
    import pandas as pd
except ImportError as e:
    print(f"Missing dependency: {e}")
    sys.exit(1)

# ── File paths ──
ALERTS_FILE = 'price_alerts.json'
SYMBOLS_YAML = 'symbols.yaml'


# ═══════════════════════════════════════════════
# UNIVERSE LOADER
# ═══════════════════════════════════════════════

def load_universe():
    """Load symbols from symbols.yaml."""
    try:
        import yaml
        with open(SYMBOLS_YAML, 'r') as f:
            raw = yaml.safe_load(f) or {}
        all_syms = []
        emoji_map = {}
        for bucket in ('crypto', 'extended_hours', 'regular_hours'):
            for item in (raw.get(bucket) or []):
                sym = item['symbol']
                all_syms.append(sym)
                emoji_map[sym] = item.get('emoji', '📊')
        return all_syms, emoji_map
    except Exception as e:
        logging.error(f"Universe load: {e}")
        return [], {}


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

def validate_symbol(sym):
    """Returns True if yfinance can fetch this symbol."""
    try:
        df = yf.download(sym, period='5d', interval='1d',
                         progress=False, auto_adjust=True)
        return not df.empty
    except Exception:
        return False

def volume_label(vol_ratio):
    if vol_ratio >= 2.0:  return f"{vol_ratio:.1f}× avg 🔥 Unusually high"
    if vol_ratio >= 1.5:  return f"{vol_ratio:.1f}× avg ⬆️ Above average"
    if vol_ratio >= 0.8:  return f"{vol_ratio:.1f}× avg — Normal"
    return f"{vol_ratio:.1f}× avg ⬇️ Below average — weak move"

def ath_recency(ath_date_str):
    try:
        ath_dt = datetime.strptime(ath_date_str[:10], '%Y-%m-%d')
        days = (datetime.now() - ath_dt).days
        if days == 0:   return "set TODAY 🔥"
        if days == 1:   return "set YESTERDAY 🔥"
        if days <= 7:   return f"set {days}d ago"
        if days <= 30:  return f"set {days // 7}w ago"
        if days <= 365: return f"set {days // 30}mo ago"
        return f"set {days // 365}y ago"
    except Exception:
        return f"on {ath_date_str}"

def fmt_price(val, decimals=2):
    try:
        return f"${float(val):.{decimals}f}"
    except Exception:
        return str(val)


# ═══════════════════════════════════════════════
# PINE INDICATORS (for squeeze/divergence)
# ═══════════════════════════════════════════════

def _clean_df(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def sma(s, length):
    return s.rolling(length).mean()

def rma(series, length):
    return series.ewm(alpha=1.0 / length, adjust=False).mean()

def pine_rsi(src, length=14):
    delta = src.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = rma(gain, length) / rma(loss, length).replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def pine_atr(df, length=14):
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return rma(tr, length)

def detect_squeeze(df):
    """Returns ('building'|'fired'|'none', direction_hint)."""
    try:
        if len(df) < 30:
            return 'none', None
        bb_basis = sma(df['Close'], 20)
        bb_dev = df['Close'].rolling(20).std()
        bb_up = bb_basis + 2.0 * bb_dev
        bb_lo = bb_basis - 2.0 * bb_dev
        kc_mid = ema(df['Close'], 20)
        kc_rng = pine_atr(df, 20)
        kc_up = kc_mid + 1.5 * kc_rng
        kc_lo = kc_mid - 1.5 * kc_rng
        in_squeeze = (bb_up < kc_up) & (bb_lo > kc_lo)
        # Last bar still in squeeze = building
        if in_squeeze.iloc[-1]:
            return 'building', None
        # Just fired (was in squeeze, now out)
        if in_squeeze.iloc[-2] and not in_squeeze.iloc[-1]:
            direction = 'bullish' if df['Close'].iloc[-1] > bb_basis.iloc[-1] else 'bearish'
            return 'fired', direction
        return 'none', None
    except Exception:
        return 'none', None

def detect_rsi_divergence(df):
    """Returns ('bullish'|'bearish'|None)."""
    try:
        if len(df) < 30:
            return None
        rsi = pine_rsi(df['Close'], 14)
        look = 10
        price_lows = df['Low'].iloc[-look:]
        rsi_lows = rsi.iloc[-look:]
        price_highs = df['High'].iloc[-look:]
        rsi_highs = rsi.iloc[-look:]
        # Bullish: price lower low, RSI higher low
        if (price_lows.iloc[-1] < price_lows.iloc[0] and
                rsi_lows.iloc[-1] > rsi_lows.iloc[0] + 3):
            return 'bullish'
        # Bearish: price higher high, RSI lower high
        if (price_highs.iloc[-1] > price_highs.iloc[0] and
                rsi_highs.iloc[-1] < rsi_highs.iloc[0] - 3):
            return 'bearish'
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════
# MTF VERDICT
# ═══════════════════════════════════════════════

def get_mtf_verdicts(symbol):
    """Returns dict of {timeframe_label: (trend, rsi)} for D/W/M."""
    results = {}
    tf_map = {
        'Daily':   ('6mo', '1d'),
        'Weekly':  ('2y',  '1wk'),
        'Monthly': ('5y',  '1mo'),
    }
    for label, (period, interval) in tf_map.items():
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 20:
                continue
            df = _clean_df(df)
            c = float(df['Close'].iloc[-1])
            e50 = float(ema(df['Close'], min(50, len(df))).iloc[-1])
            e200 = float(ema(df['Close'], min(200, len(df))).iloc[-1])
            rsi_val = float(pine_rsi(df['Close'], 14).iloc[-1])

            if c > e50 > e200:      trend = "🚀 Strong Bull"
            elif c > e200:          trend = "📈 Bull"
            elif c < e50 < e200:    trend = "💀 Strong Bear"
            elif c < e200:          trend = "📉 Bear"
            else:                   trend = "⚖️ Mixed"

            results[label] = (trend, round(rsi_val, 1))
            time.sleep(0.2)
        except Exception as e:
            logging.debug(f"MTF {symbol} {label}: {e}")
    return results


# ═══════════════════════════════════════════════
# SECTOR CONTEXT
# ═══════════════════════════════════════════════

def get_sector_context(symbol):
    """Returns sector name + avg sector performance today."""
    sector = SYMBOL_TO_SECTOR.get(symbol)
    if not sector:
        return None, None, None
    syms = SECTORS.get(sector, [])
    if not syms or len(syms) < 2:
        return sector, None, None
    changes = []
    for s in syms:
        if s == symbol:
            continue
        try:
            df = yf.download(s, period='5d', interval='1d',
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            df = _clean_df(df)
            chg = (float(df['Close'].iloc[-1]) - float(df['Close'].iloc[-2])) / float(df['Close'].iloc[-2]) * 100
            changes.append(chg)
            time.sleep(0.15)
        except Exception:
            pass
    if not changes:
        return sector, None, None
    avg = sum(changes) / len(changes)
    return sector, round(avg, 2), syms


# ═══════════════════════════════════════════════
# POC + STRUCTURE
# ═══════════════════════════════════════════════

def quick_poc(df_daily):
    try:
        recent = df_daily.iloc[-60:]
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
        return round((bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2, 4)
    except Exception:
        return None

def recent_structure(df_daily):
    try:
        recent = df_daily.iloc[-20:]
        return round(float(recent['Low'].min()), 2), round(float(recent['High'].max()), 2)
    except Exception:
        return None, None


# ═══════════════════════════════════════════════
# IMPROVED VERDICT ENGINE
# ═══════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None):
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

    # 0. PARABOLIC SPIKE
    if abs(drop) >= 15:
        if drop > 0:
            verdict = "⚠️ PARABOLIC"
            zone = f"News/Catalyst Spike +{drop:.0f}%"
            reasons = [
                f"+{drop:.1f}% single-day — likely news driven",
                "Parabolic moves mean-revert — high risk to chase",
                f"Volume {c['vol_ratio']:.1f}× avg confirms institutional activity",
            ]
            next_steps = [
                "DO NOT chase at current price",
                "Wait for 3-5 day consolidation",
                f"Re-entry: first pullback to EMA50 `${c['ema50']:.2f}`",
                "If holding: consider partial profits",
            ]
        else:
            verdict = "🚨 CRASH"
            zone = f"Severe Drop {drop:.0f}%"
            reasons = [
                f"{drop:.1f}% single-day drop — likely news driven",
                "Wait for dust to settle before any entry",
                "Could bounce or continue lower — too early to know",
            ]
            next_steps = [
                "Do NOT catch this today",
                "Wait minimum 3 days for stabilisation",
                f"Watch: does it hold EMA200 `${c['ema200']:.2f}`?",
            ]
        return verdict, zone, reasons, next_steps

    # 1. MOMENTUM — at/near ATH
    if ("UPTREND" in trend and from_ath > -5 and above_50 and above_200 and rsi < 80):
        verdict = "🚀 MOMENTUM"
        zone = "AT ATH — Continuation"
        reasons = [
            f"At/near all-time high ({from_ath:+.1f}%)",
            "EMA stack fully bullish",
            f"RSI {rsi:.0f} — not overbought, room to run",
        ]
        next_steps = [
            f"Breakout entry: above ATH `${c['ath']:.2f}` with volume",
            f"Pullback entry: dip to EMA50 `${c['ema50']:.2f}`",
            f"Stop: below EMA50 `${c['ema50']:.2f}`",
        ]

    # 2. STRONG UPTREND PULLBACK
    elif "UPTREND" in trend and rsi < 52 and above_200:
        verdict = "🟢 BUY ZONE"
        zone = "Pullback in Uptrend"
        reasons = [
            "Healthy pullback in confirmed uptrend",
            f"RSI {rsi:.0f} — room to run",
        ]
        if from_ath > -20:
            reasons.append("Near ATH — strong stock pulling back")
        next_steps = [
            f"Entry: `${c['current']:.2f}` or lower",
            f"Target: retest ATH `${c['ath']:.2f}`",
            f"Stop: below EMA200 `${c['ema200']:.2f}`",
        ]

    # 3. EMA50 PULLBACK
    elif "PULLBACK" in trend and rsi < 55:
        verdict = "🟢 BUY ZONE"
        zone = "EMA50 Pullback"
        reasons = [
            "Above EMA200 — uptrend structure intact",
            f"Pulling toward EMA50 `${c['ema50']:.2f}`",
            f"RSI {rsi:.0f} — watch for bounce",
        ]
        next_steps = [
            f"Entry: near EMA50 `${c['ema50']:.2f}`",
            f"Stop: below EMA200 `${c['ema200']:.2f}`",
            f"Target: prior highs `${c['high_52w']:.2f}`",
        ]

    # 4. EXTENDED NEAR ATH
    elif from_ath > -8 and rsi > 75:
        verdict = "🟠 EXTENDED"
        zone = "Overbought Near ATH"
        reasons = [
            f"RSI {rsi:.0f} — overbought at highs",
            "Risk/reward not ideal for new entry",
        ]
        next_steps = [
            "Wait for RSI to cool to 50-60",
            f"Better entry: EMA50 `${c['ema50']:.2f}`",
            "If holding: trail stop, don't add",
        ]

    # 5. STRONG DOWNTREND
    elif "DOWNTREND" in trend and not above_200:
        verdict = "🔴 AVOID"
        zone = "Falling Knife"
        reasons = [
            "Below EMA50 & EMA200 — confirmed downtrend",
            "No base formed — high continuation risk",
        ]
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal")
        next_steps = [
            f"Wait for: close above EMA50 `${c['ema50']:.2f}`",
            "Then confirm EMA50 > EMA200 cross",
        ]

    # 6. NEAR 52W LOW
    elif c['pct_from_52w_low'] < 8 and drop < -3:
        verdict = "⚠️ CAUTION"
        zone = "Breaking Down"
        reasons = [
            "Near 52W low — key support at risk",
            "Wait for base formation",
        ]
        next_steps = [
            f"Watch: holds above `${c['low_52w']:.2f}` (52W low)",
            "Only enter after 2-3 days of stabilisation",
        ]

    # 7. RECOVERING
    elif "RECOVERING" in trend:
        if rsi > 55 and drop > 0:
            verdict = "🟡 WATCH"
            zone = "Recovery Attempt"
            reasons = [
                "Reclaiming EMA50 — potential recovery",
                f"Must clear EMA200 `${c['ema200']:.2f}` to confirm",
            ]
            next_steps = [
                f"Trigger: daily close above EMA200 `${c['ema200']:.2f}`",
                "Then: small position, stop below EMA50",
            ]
        else:
            verdict = "⏸️ HOLD"
            zone = "Below EMA200"
            reasons = ["Below EMA200 — no structural confirmation"]
            next_steps = [f"Wait for: reclaim EMA200 `${c['ema200']:.2f}`"]

    # 8. MIXED
    elif "MIXED" in trend:
        if range_pos < 35 and rsi < 45:
            verdict = "🟡 WATCH"
            zone = "Potential Base"
            reasons = ["Lower 52W range — possible accumulation"]
            next_steps = [
                f"Trigger: RSI > 50 + close above EMA50 `${c['ema50']:.2f}`",
            ]
        elif rsi > 72:
            verdict = "🟠 EXTENDED"
            zone = "Overbought in Chop"
            reasons = [f"RSI {rsi:.0f} extended in mixed trend"]
            next_steps = ["Wait for RSI pullback to 50-55"]
        else:
            verdict = "⏸️ NEUTRAL"
            zone = "No Clear Edge"
            reasons = ["Mixed signals — no directional conviction"]
            next_steps = [
                f"Bull trigger: close above EMA50 `${c['ema50']:.2f}` + RSI > 55",
                f"Bear trigger: close below EMA200 `${c['ema200']:.2f}`",
            ]

    # 9. DEFAULT
    else:
        if above_50 and above_200 and rsi > 55:
            verdict = "🟡 WATCH"
            zone = "Building Momentum"
            reasons = ["Above both EMAs — structure improving"]
            next_steps = [
                f"Better entry: pullback to EMA50 `${c['ema50']:.2f}`",
                f"Breakout: new high above `${c['high_52w']:.2f}`",
            ]
        else:
            verdict = "⏸️ NEUTRAL"
            zone = "No Clear Setup"
            reasons = ["No strong directional signal"]
            next_steps = [
                f"Bull trigger: above EMA50 `${c['ema50']:.2f}` + RSI > 55",
                f"Bear trigger: below EMA200 `${c['ema200']:.2f}`",
            ]

    # Market context override
    if market_ctx:
        vix = market_ctx.get('^VIX', {}).get('price', 15)
        spy_pct = market_ctx.get('SPY', {}).get('pct', 0)
        if vix > 25 and spy_pct < -1.5 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
            verdict = "⚠️ WAIT"
            reasons.insert(0, f"Market bleeding — VIX {vix:.0f}, SPY {spy_pct:.1f}%")
            next_steps = ["Wait for market to stabilise"]

    # Earnings override
    if any(x in verdict for x in ["BUY", "MOMENTUM", "WATCH"]):
        _, days_until = get_earnings_date(c['symbol'])
        if days_until is not None and days_until <= EARNINGS_WARNING_DAYS:
            verdict = "⚠️ WAIT — Earnings"
            zone = f"Earnings in {days_until}d"
            reasons.insert(0, f"Earnings in {days_until} days — skip new entries")
            next_steps = [f"Re-evaluate after earnings"]

    return verdict, zone, reasons, next_steps


# ═══════════════════════════════════════════════
# SMARTER AI PROMPT
# ═══════════════════════════════════════════════

def get_ai_analysis(ctx, verdict, zone, sector_name, sector_avg, mtf_verdicts):
    """Smarter AI prompt — sends pre-interpreted context."""
    from market_intel import GEMINI_API_KEY
    if not GEMINI_API_KEY:
        return None

    c = ctx
    mtf_str = "\n".join([f"  {tf}: {trend} (RSI {rsi})"
                         for tf, (trend, rsi) in mtf_verdicts.items()]) if mtf_verdicts else "  N/A"

    sector_str = f"{sector_name}: {sector_avg:+.1f}% avg" if sector_name and sector_avg else "Unknown sector"

    prompt = f"""You are a senior trading analyst. Analyze this setup in EXACTLY 4 lines (max 110 chars each).

SETUP SUMMARY:
Symbol: {c['symbol']} | Verdict: {zone}
Price: ${c['current']:.2f} | Day: {c['day_change_pct']:+.1f}% | Volume: {c['vol_ratio']:.1f}× avg
Trend: {c['trend']} | RSI: {c['rsi']:.0f} | ATH: {c['ath_pct']:+.1f}%
Position: {c['range_pos']:.0f}% of 52W range (L ${c['low_52w']:.2f} / H ${c['high_52w']:.2f})
EMA50: ${c['ema50']:.2f} | EMA200: ${c['ema200']:.2f}

MULTI-TIMEFRAME:
{mtf_str}

SECTOR: {sector_str}

Respond EXACTLY in this format:
📊 [Is this move technical, sector-driven, or likely news/catalyst? Be specific]
🎯 [Setup quality: is the risk/reward good here? Any hidden risks?]
⚠️ [Biggest risk to watch — price level or condition that invalidates]
💡 [STRONG BUY / BUY / HOLD / AVOID / WAIT] — [one sharp actionable sentence]

4 lines only. No extra text."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        r = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 400}
        }, timeout=20)
        if r.status_code == 200:
            data = r.json()
            if data.get('candidates'):
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
        elif r.status_code == 429:
            print("  → Gemini RATE LIMITED — retrying in 15s")
            time.sleep(15)
            r2 = requests.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.5, "maxOutputTokens": 400}
            }, timeout=20)
            if r2.status_code == 200:
                data2 = r2.json()
                if data2.get('candidates'):
                    return data2['candidates'][0]['content']['parts'][0]['text'].strip()
        else:
            print(f"  → Gemini ERROR {r.status_code}")
    except Exception as e:
        logging.error(f"AI analysis: {e}")
    return None

import requests  # needed for get_ai_analysis


# ═══════════════════════════════════════════════
# FORMAT FULL ANALYSIS MESSAGE
# ═══════════════════════════════════════════════

def format_full_analysis(symbol, ctx, verdict, zone, reasons, next_steps,
                          ai_text, market_ctx, rs_score, rs_label,
                          poc, support, resistance,
                          squeeze_state, squeeze_dir,
                          rsi_div, mtf_verdicts,
                          sector_name, sector_avg):
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

    # ── HEADER + VERDICT ──
    msg = f"🔍 *ON-DEMAND ANALYSIS*\n"
    msg += f"{em} *{symbol}* • {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"
    msg += f"*{verdict}*\n"
    msg += f"_Zone: {zone}_\n"
    for r in reasons[:3]:
        msg += f"  • {r}\n"

    # Squeeze / divergence flags right under verdict
    if squeeze_state == 'building':
        msg += f"\n🔥 *SQUEEZE BUILDING* — explosive move loading, direction unknown\n"
    elif squeeze_state == 'fired':
        dir_em = "⬆️" if squeeze_dir == 'bullish' else "⬇️"
        msg += f"\n💥 *SQUEEZE FIRED* {dir_em} {squeeze_dir} — momentum expanding\n"
    if rsi_div == 'bullish':
        msg += f"📈 *RSI DIVERGENCE* — price weak but momentum building (bullish)\n"
    elif rsi_div == 'bearish':
        msg += f"📉 *RSI DIVERGENCE* — price strong but momentum fading (bearish)\n"

    # AI summary line at top
    if ai_text:
        lines = ai_text.strip().split('\n')
        summary = next((l for l in lines if '💡' in l), None)
        if summary:
            msg += f"\n{summary}\n"

    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # ── PRICE ──
    msg += f"*💵 PRICE*\n`─────────────────`\n"
    msg += f"Live: `${pf.format(c['current'])}` ({drop_em} {sign}{drop:.2f}% today)\n"
    msg += f"Range: L `${pf.format(c['today_low'])}` → H `${pf.format(c['today_high'])}`\n"
    msg += f"Volume: {volume_label(c['vol_ratio'])}\n"
    if poc:
        diff_pct = (c['current'] - poc) / poc * 100
        if abs(diff_pct) < 0.5:
            msg += f"🎯 *AT POC* `${pf.format(poc)}` — volume magnet\n"
        elif c['current'] > poc:
            msg += f"🎯 Above POC `${pf.format(poc)}` — buyers in control\n"
        else:
            msg += f"🎯 Below POC `${pf.format(poc)}` — sellers in control\n"

    # ── MULTI-TIMEFRAME ──
    if mtf_verdicts:
        msg += f"\n*🗂️ TIMEFRAME ALIGNMENT*\n`─────────────────`\n"
        for tf_label, (tf_trend, tf_rsi) in mtf_verdicts.items():
            msg += f"{tf_label:7s}: {tf_trend} (RSI {tf_rsi})\n"

    # ── TECHNICALS ──
    msg += f"\n*📈 TECHNICALS*\n`─────────────────`\n"
    msg += f"{c['trend']}\n"
    rsi_tag = "_(oversold)_" if c['rsi'] < 30 else "_(overbought)_" if c['rsi'] > 70 else "_(bullish)_" if c['rsi'] > 60 else "_(neutral)_"
    msg += f"RSI: `{c['rsi']:.0f}` {rsi_tag}\n"
    msg += f"EMA50: `${pf.format(c['ema50'])}` • EMA200: `${pf.format(c['ema200'])}`\n"
    above_50 = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if above_50 and above_200:       msg += "✅ Above EMA50 & EMA200\n"
    elif above_200 and not above_50: msg += "⚠️ Below EMA50, above EMA200\n"
    elif not above_200 and above_50: msg += "🔀 Above EMA50, below EMA200\n"
    else:                             msg += "🔴 Below both EMAs\n"

    # ── POSITION ──
    msg += f"\n*📏 POSITION*\n`─────────────────`\n"
    pos = int(c['range_pos'] / 10)
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"`{bar}` {c['range_pos']:.0f}% of 52W range\n"
    msg += f"52W: `${pf.format(c['low_52w'])}` → `${pf.format(c['high_52w'])}`\n"
    msg += f"ATH: `${pf.format(c['ath'])}` ({c['ath_pct']:+.1f}%) — {ath_recency(c['ath_date'])}\n"
    if support and resistance:
        msg += f"Structure: Support `${pf.format(support)}` • Resistance `${pf.format(resistance)}`\n"

    # ── SECTOR ──
    if sector_name and sector_avg is not None:
        sec_em = "🟢" if sector_avg > 0 else "🔴"
        msg += f"\n*🏭 SECTOR ({sector_name})*\n`─────────────────`\n"
        msg += f"Sector avg today: {sec_em} `{sector_avg:+.2f}%`\n"
        sym_vs_sector = drop - sector_avg
        if sym_vs_sector > 1.5:
            msg += f"💪 {symbol} outperforming sector by `{sym_vs_sector:+.1f}%`\n"
        elif sym_vs_sector < -1.5:
            msg += f"⚠️ {symbol} underperforming sector by `{sym_vs_sector:+.1f}%`\n"
        else:
            msg += f"➖ Moving in line with sector\n"

    # ── RS ──
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

    # ── WHAT TO DO ──
    msg += f"\n*💡 WHAT TO DO*\n`─────────────────`\n"
    for step in next_steps:
        msg += f"  → {step}\n"

    # ── FULL AI ──
    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n`─────────────────`\n{ai_text}\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_AlphaEdge v7.0 • On-demand_"
    return msg


def format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score):
    """3-line quick summary."""
    em = SYMBOL_EMOJI.get(symbol, '📊')
    c = ctx
    drop = c['day_change_pct']
    drop_em = "🟢" if drop >= 0 else "🔴"
    sign = "+" if drop >= 0 else ""
    decimals = 4 if c['current'] < 10 else 2
    pf = f"{{:.{decimals}f}}"
    rs_str = f" • RS {rs_label}" if rs_label else ""
    msg = f"🔍 {em} *{symbol}* `${pf.format(c['current'])}` ({drop_em}{sign}{drop:.1f}%)\n"
    msg += f"{verdict} — _{zone}_\n"
    msg += f"RSI `{c['rsi']:.0f}` • {c['trend']}{rs_str}"
    return msg


# ═══════════════════════════════════════════════
# PRICE ALERT SYSTEM
# ═══════════════════════════════════════════════

def load_alerts():
    return load_json(ALERTS_FILE, {})

def save_alerts(alerts):
    save_json(ALERTS_FILE, alerts)

def set_alert(symbol, target_price, direction='auto'):
    """Set a price alert. Direction auto-detected from current price."""
    alerts = load_alerts()

    # Get current price to auto-detect direction
    try:
        df = yf.download(symbol, period='1d', interval='1m',
                         progress=False, auto_adjust=True)
        df = _clean_df(df)
        current = float(df['Close'].iloc[-1]) if not df.empty else None
    except Exception:
        current = None

    if direction == 'auto' and current:
        direction = 'above' if target_price > current else 'below'

    alert_key = f"{symbol}_{target_price}"
    alerts[alert_key] = {
        'symbol': symbol,
        'target': target_price,
        'direction': direction,
        'set_at': now_est().isoformat(),
        'expires_at': (now_est() + timedelta(days=30)).isoformat(),
        'warning_sent': False,
        'expiry_warning_sent': False,
        'triggered': False,
    }
    save_alerts(alerts)

    dir_str = "rises to" if direction == 'above' else "falls to"
    warn_price = target_price * 0.98 if direction == 'above' else target_price * 1.02
    current_str = f" (currently `${current:.2f}`)" if current else ""
    msg = (f"✅ *Alert set!*\n"
           f"{SYMBOL_EMOJI.get(symbol, '📊')} *{symbol}* — notify when price {dir_str} `${target_price:.2f}`{current_str}\n"
           f"⚡ Early warning at `${warn_price:.2f}` (2% before target)\n"
           f"⏰ Expires in 30 days")
    send_telegram(msg)

def cancel_alert(symbol):
    alerts = load_alerts()
    removed = []
    for key in list(alerts.keys()):
        if alerts[key]['symbol'] == symbol:
            removed.append(alerts[key]['target'])
            del alerts[key]
    save_alerts(alerts)

    em = SYMBOL_EMOJI.get(symbol, '📊')
    if removed:
        targets = ', '.join([f"${t}" for t in removed])
        send_telegram(f"🗑️ Cancelled alert(s) for {em} *{symbol}*: {targets}")
    else:
        send_telegram(f"❌ No active alerts found for *{symbol}*")

def list_alerts():
    alerts = load_alerts()
    active = {k: v for k, v in alerts.items() if not v.get('triggered')}
    if not active:
        send_telegram("📋 *No active price alerts.*\n\nSet one: `alert TSLA 450`")
        return

    msg = f"📋 *ACTIVE PRICE ALERTS ({len(active)})*\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"
    for key, a in sorted(active.items(), key=lambda x: x[1]['symbol']):
        em = SYMBOL_EMOJI.get(a['symbol'], '📊')
        dir_em = "⬆️" if a['direction'] == 'above' else "⬇️"
        expires = datetime.fromisoformat(a['expires_at'])
        days_left = (expires - now_est()).days
        warn_price = a['target'] * 0.98 if a['direction'] == 'above' else a['target'] * 1.02
        msg += f"{em} *{a['symbol']}* {dir_em} `${a['target']:.2f}`\n"
        msg += f"   ⚡ Warning at `${warn_price:.2f}` • ⏰ {days_left}d left\n\n"
    send_telegram(msg)

def check_alerts():
    """Check all active alerts — call this from scanner every run during market hours."""
    alerts = load_alerts()
    if not alerts:
        return

    changed = False
    now = now_est()

    for key, a in list(alerts.items()):
        if a.get('triggered'):
            continue

        symbol = a['symbol']
        target = a['target']
        direction = a['direction']
        warn_price = target * 0.98 if direction == 'above' else target * 1.02
        em = SYMBOL_EMOJI.get(symbol, '📊')

        # Check expiry
        expires = datetime.fromisoformat(a['expires_at'])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=EST)
        days_left = (expires - now).days

        # Expiry warning (1 day before)
        if days_left <= 1 and not a.get('expiry_warning_sent'):
            send_telegram(
                f"⏰ *Alert expiring soon!*\n"
                f"{em} *{symbol}* → `${target:.2f}` expires tomorrow\n"
                f"Send `alert {symbol} {target}` to reset"
            )
            a['expiry_warning_sent'] = True
            changed = True

        # Expired
        if now > expires:
            send_telegram(
                f"🗑️ *Alert expired*\n"
                f"{em} *{symbol}* → `${target:.2f}` (30 days, untriggered)"
            )
            del alerts[key]
            changed = True
            continue

        # Get current price
        try:
            df = yf.download(symbol, period='1d', interval='5m',
                             progress=False, auto_adjust=True)
            if df.empty:
                continue
            df = _clean_df(df)
            current = float(df['Close'].iloc[-1])
        except Exception:
            continue

        # Stage 2 — target hit
        if ((direction == 'above' and current >= target) or
                (direction == 'below' and current <= target)):
            send_telegram(
                f"🎯 *PRICE ALERT TRIGGERED!*\n"
                f"{em} *{symbol}* hit `${target:.2f}`\n"
                f"Current: `${current:.2f}`\n"
                f"_Alert has been removed._"
            )
            a['triggered'] = True
            changed = True

        # Stage 1 — proximity warning (2% away)
        elif (not a.get('warning_sent') and
              ((direction == 'above' and current >= warn_price) or
               (direction == 'below' and current <= warn_price))):
            send_telegram(
                f"⚡ *APPROACHING TARGET!*\n"
                f"{em} *{symbol}* is near your `${target:.2f}` alert\n"
                f"Current: `${current:.2f}` — {abs(current - target) / target * 100:.1f}% away\n"
                f"_Get ready!_"
            )
            a['warning_sent'] = True
            changed = True

    if changed:
        save_alerts(alerts)


# ═══════════════════════════════════════════════
# WATCHLIST SCAN
# ═══════════════════════════════════════════════

def run_watchlist_scan():
    """Scan all symbols.yaml symbols and rank by momentum."""
    send_telegram("🔍 *Scanning your watchlist...* please wait ~60s", silent=True)

    all_syms, emoji_map = load_universe()
    if not all_syms:
        send_telegram("❌ Could not load symbols.yaml")
        return

    market_ctx = get_market_ctx()
    results = []

    for sym in all_syms:
        try:
            print(f"  → {sym}...", end=" ", flush=True)
            ctx = get_full_context(sym)
            time.sleep(0.3)
            if not ctx:
                print("—")
                continue
            verdict, zone, _, _ = get_verdict(ctx, market_ctx)
            rs_score, rs_label = calc_relative_strength(ctx)
            results.append({
                'symbol': sym,
                'emoji': emoji_map.get(sym, '📊'),
                'verdict': verdict,
                'zone': zone,
                'drop': ctx['day_change_pct'],
                'rsi': ctx['rsi'],
                'rs_score': rs_score or 0,
                'trend': ctx['trend'],
                'current': ctx['current'],
                'ath_pct': ctx['ath_pct'],
            })
            print(f"{ctx['day_change_pct']:+.1f}% {verdict}")
        except Exception as e:
            print(f"💥 {e}")

    if not results:
        send_telegram("❌ No data fetched — try again later")
        return

    # Sort: MOMENTUM first, then BUY ZONE, then by day change
    def sort_key(r):
        v = r['verdict']
        if 'MOMENTUM' in v:   return (0, -r['drop'])
        if 'BUY' in v:        return (1, -r['drop'])
        if 'WATCH' in v:      return (2, -r['drop'])
        if 'NEUTRAL' in v:    return (3, -r['drop'])
        if 'EXTENDED' in v:   return (4, -r['drop'])
        if 'AVOID' in v:      return (5, -r['drop'])
        return (6, -r['drop'])

    results.sort(key=sort_key)

    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    msg = f"📊 *WATCHLIST SCAN*\n🕒 {ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # Group by verdict
    groups = {}
    for r in results:
        v = r['verdict']
        groups.setdefault(v, []).append(r)

    for verdict_key, items in groups.items():
        msg += f"*{verdict_key}*\n"
        for r in items:
            drop_em = "🟢" if r['drop'] >= 0 else "🔴"
            sign = "+" if r['drop'] >= 0 else ""
            decimals = 4 if r['current'] < 10 else 2
            pf = f"{{:.{decimals}f}}"
            rs_str = f" RS {r['rs_score']:+.1f}%" if r['rs_score'] else ""
            msg += (f"  {r['emoji']} *{r['symbol']}* `${pf.format(r['current'])}` "
                    f"{drop_em}{sign}{r['drop']:.1f}% RSI `{r['rsi']:.0f}`{rs_str}\n")
        msg += "\n"

    msg += f"_Type any symbol for full analysis_\n"
    msg += f"_AlphaEdge v7.0_"
    send_telegram(msg)


# ═══════════════════════════════════════════════
# TOP MOVERS
# ═══════════════════════════════════════════════

def run_top_movers():
    """Show today's top gainers and losers from watchlist."""
    send_telegram("📊 *Fetching top movers...* please wait", silent=True)

    all_syms, emoji_map = load_universe()
    market_ctx = get_market_ctx()
    movers = []

    for sym in all_syms:
        try:
            df = yf.download(sym, period='5d', interval='1d',
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            df = _clean_df(df)
            change = (float(df['Close'].iloc[-1]) - float(df['Close'].iloc[-2])) / float(df['Close'].iloc[-2]) * 100
            movers.append({
                'symbol': sym,
                'emoji': emoji_map.get(sym, '📊'),
                'change': change,
                'price': float(df['Close'].iloc[-1]),
            })
            time.sleep(0.2)
        except Exception:
            pass

    if not movers:
        send_telegram("❌ Could not fetch mover data")
        return

    movers.sort(key=lambda x: -x['change'])
    gainers = [m for m in movers if m['change'] > 0][:5]
    losers = [m for m in movers if m['change'] < 0][-5:]
    losers.reverse()

    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    msg = f"📊 *TOP MOVERS*\n🕒 {ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    if gainers:
        msg += "*🚀 GAINERS*\n"
        for m in gainers:
            decimals = 4 if m['price'] < 10 else 2
            msg += f"  {m['emoji']} *{m['symbol']}* `${m['price']:.{decimals}f}` 🟢 +{m['change']:.2f}%\n"

    if losers:
        msg += f"\n*📉 LOSERS*\n"
        for m in losers:
            decimals = 4 if m['price'] < 10 else 2
            msg += f"  {m['emoji']} *{m['symbol']}* `${m['price']:.{decimals}f}` 🔴 {m['change']:.2f}%\n"

    if market_ctx:
        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})
        msg += f"\n`─────────────────`\n"
        if spy:
            spy_em = "🟢" if spy.get('pct', 0) >= 0 else "🔴"
            msg += f"SPY: {spy_em} `{spy.get('pct', 0):+.2f}%`"
        if vix:
            msg += f" • VIX: `{vix.get('price', 0):.1f}`"
        msg += "\n"

    msg += f"\n_Type any symbol for full analysis_"
    send_telegram(msg)


# ═══════════════════════════════════════════════
# ON-DEMAND BRIEF
# ═══════════════════════════════════════════════

def run_brief():
    """Auto-detect morning/evening/weekend and fire the right brief."""
    now = now_est()
    hour = now.hour + now.minute / 60
    weekday = now.weekday()

    try:
        if weekday >= 5:
            # Weekend — import and fire weekly summary
            from scanner import format_weekly_summary, load_json, HISTORY_FILE
            ws = format_weekly_summary()
            if ws:
                send_telegram(ws)
            else:
                send_telegram("📊 No trade history yet for weekly summary.")
        elif hour < 12:
            # Morning
            from morning_brief import build_morning_brief, should_send_morning, mark_morning_sent
            send_telegram("🌅 _Building morning brief..._", silent=True)
            success = build_morning_brief()
            if success:
                mark_morning_sent()
        else:
            # Afternoon/Evening
            from morning_brief import build_evening_brief, should_send_evening, mark_evening_sent
            send_telegram("🌆 _Building evening brief..._", silent=True)
            success = build_evening_brief()
            if success:
                mark_evening_sent()
    except Exception as e:
        logging.error(f"Brief command: {e}")
        send_telegram(f"❌ Brief failed: {e}")


# ═══════════════════════════════════════════════
# HELP MESSAGE
# ═══════════════════════════════════════════════

def send_help():
    msg = """🤖 *ALPHAEDGE BOT COMMANDS*
`━━━━━━━━━━━━━━━━━━━━━`

*📊 ANALYSIS*
`TSLA` — full analysis
`TSLA NVDA AMD` — multiple (up to 5)
`TSLA short` — 3-line quick summary
`TSLA week` — weekly timeframe

*📋 WATCHLIST*
`scan` — rank all your symbols
`top` — today's top movers

*🔔 PRICE ALERTS*
`alert TSLA 450` — notify near $450
`alert TSLA 400 below` — notify going down
`cancel TSLA` — remove alert
`alerts` — list active alerts

*📰 BRIEFS*
`brief` — morning/evening context now

*❓ OTHER*
`help` — show this message

`━━━━━━━━━━━━━━━━━━━━━`
_AlphaEdge v7.0 • Always watching_ 👁️"""
    send_telegram(msg)


# ═══════════════════════════════════════════════
# FULL ANALYSIS RUNNER
# ═══════════════════════════════════════════════

def run_analysis(symbol, mode='full', timeframe='1d'):
    symbol = normalise_symbol(symbol)
    print(f"\n🔍 Analysis: {symbol} | mode={mode} | tf={timeframe}")

    # Validate symbol
    send_telegram(f"🔍 Analysing *{symbol}*... please wait ~30s", silent=True)
    if not validate_symbol(symbol):
        send_telegram(
            f"❌ *{symbol}* not found.\n"
            f"Check the ticker is valid (e.g. `TSLA`, `BTC-USD`, `GC=F`)"
        )
        return

    # Fetch context
    ctx = get_full_context(symbol)
    if not ctx:
        send_telegram(f"❌ Could not fetch data for *{symbol}*")
        return

    market_ctx = get_market_ctx()
    verdict, zone, reasons, next_steps = get_verdict(ctx, market_ctx)
    rs_score, rs_label = calc_relative_strength(ctx)

    # Short mode — just send quick summary
    if mode == 'short':
        msg = format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score)
        send_telegram(msg)
        return

    # Full mode — fetch all extras
    print("  → MTF verdicts...")
    mtf_verdicts = get_mtf_verdicts(symbol)

    print("  → Sector context...")
    sector_name, sector_avg, _ = get_sector_context(symbol)

    print("  → Daily bars for POC/structure/squeeze...")
    try:
        df_daily = yf.download(symbol, period='6mo', interval='1d',
                               progress=False, auto_adjust=True)
        df_daily = _clean_df(df_daily)
        poc = quick_poc(df_daily)
        support, resistance = recent_structure(df_daily)
        squeeze_state, squeeze_dir = detect_squeeze(df_daily)
        rsi_div = detect_rsi_divergence(df_daily)
    except Exception:
        poc = support = resistance = None
        squeeze_state, squeeze_dir, rsi_div = 'none', None, None

    print("  → AI analysis...")
    ai_text = get_ai_analysis(ctx, verdict, zone, sector_name, sector_avg, mtf_verdicts)
    print(f"  → AI: {'GOT RESPONSE' if ai_text else 'NO RESPONSE'}")

    msg = format_full_analysis(
        symbol, ctx, verdict, zone, reasons, next_steps,
        ai_text, market_ctx, rs_score, rs_label,
        poc, support, resistance,
        squeeze_state, squeeze_dir, rsi_div,
        mtf_verdicts, sector_name, sector_avg
    )
    send_telegram(msg)
    logging.info(f"Analysis sent: {symbol} | {verdict}")


# ═══════════════════════════════════════════════
# MAIN — command router
# ═══════════════════════════════════════════════

def main():
    payload = {}
    if len(sys.argv) > 1:
        try:
            payload = json.loads(sys.argv[1])
        except Exception:
            # Legacy: just a symbol string
            payload = {"symbol": sys.argv[1], "mode": "full", "timeframe": "1d"}

    event_type = payload.get('event_type', 'analyze_symbol')
    command = payload.get('command', '')
    symbol = payload.get('symbol', '')
    mode = payload.get('mode', 'full')
    timeframe = payload.get('timeframe', '1d')

    print(f"\n🤖 Bot command: event={event_type} command={command} symbol={symbol}")

    if event_type == 'analyze_symbol' or command == '':
        if symbol:
            run_analysis(symbol, mode, timeframe)
        else:
            send_help()

    elif command == 'alert':
        price = payload.get('price')
        direction = payload.get('direction', 'auto')
        if symbol and price:
            set_alert(symbol, float(price), direction)
        else:
            send_telegram("❌ Usage: `alert TSLA 450`")

    elif command == 'cancel_alert':
        if symbol:
            cancel_alert(symbol)

    elif command == 'list_alerts':
        list_alerts()

    elif command == 'check_alerts':
        check_alerts()

    elif command == 'scan':
        run_watchlist_scan()

    elif command == 'top':
        run_top_movers()

    elif command == 'brief':
        run_brief()

    elif command == 'help':
        send_help()

    else:
        send_telegram(f"❓ Unknown command: `{command}`\nType `help` for commands.")


if __name__ == "__main__":
    main()
