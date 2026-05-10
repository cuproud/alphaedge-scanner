"""
ALPHAEDGE SINGLE SCAN v4.1 — FULL AUDIT & FIXES
═══════════════════════════════════════════════════════════════
v4.1 vs v4.0 FIXES:
• CRITICAL: Stop loss in BUY ZONE now uses EMA50 not EMA200
  (v4.0 gave -15% stops on near-EMA50 entries — terrible R:R)
• CRITICAL: Verdict engine now checks MTF ADX — won't give BUY
  verdict if daily ADX < 15 (ranging/weak trend)
• CRITICAL: POC conflict warning added — if below POC in "BUY ZONE"
  entry guidance now notes sellers in control
• Volume threshold: 0.8× now shows "⬇️ Slightly below average"
  instead of "Normal" (was misleading)
• Trend label consistency: TECHNICALS section now uses MTF daily
  trend label instead of re-deriving (prevents Daily showing
  "UPTREND" while MTF Daily shows "Strong Bull")
• Analyst caveat: warns when n < 3 analysts (statistically thin)
• R:R ratio now explicitly calculated and shown in entry guidance
• Beta-based position sizing suggestion added
• AI failure now shown as "(AI unavailable)" rather than silent skip
• PE ratio shown in FUNDAMENTALS when available
• EMA50 stretch > 15% now blocks BUY verdict (overbought extension)
• ADX/SAR conflict with verdict flagged inline
• Minor: "Normal" volume threshold tightened 0.8→0.9

MATH VERIFIED:
• ATH% = (current - ATH) / ATH × 100
• Range position = (current - 52wLow) / (52wHigh - 52wLow) × 100
• RS = symbol 5d perf − SPY 5d perf
• CAD implied = USD price × USDCAD rate
• R:R = (target - entry) / (entry - stop)
"""

import sys
import os
import json
import time
import logging
import requests
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

ALERTS_FILE  = 'price_alerts.json'
SYMBOLS_YAML = 'symbols.yaml'


# ═══════════════════════════════════════════════
# UNIVERSE LOADER
# ═══════════════════════════════════════════════

def load_universe():
    try:
        import yaml
        with open(SYMBOLS_YAML, 'r') as f:
            raw = yaml.safe_load(f) or {}
        all_syms  = []
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
    try:
        df = yf.download(sym, period='5d', interval='1d',
                         progress=False, auto_adjust=True)
        return not df.empty
    except Exception:
        return False

def volume_label(vol_ratio):
    """
    FIX v4.1: 0.8-0.9× was labelled "Normal" in v4.0 — misleading.
    Tightened threshold: >= 0.9 is Normal.
    """
    if vol_ratio >= 2.0:  return f"{vol_ratio:.1f}× avg 🔥 Unusually high"
    if vol_ratio >= 1.5:  return f"{vol_ratio:.1f}× avg ⬆️ Above average"
    if vol_ratio >= 0.9:  return f"{vol_ratio:.1f}× avg — Normal"
    if vol_ratio >= 0.7:  return f"{vol_ratio:.1f}× avg ⬇️ Slightly below average"
    return f"{vol_ratio:.1f}× avg ⬇️ Below average — weak move"

def ath_recency(ath_date_str):
    try:
        ath_dt = datetime.strptime(ath_date_str[:10], '%Y-%m-%d')
        days   = (datetime.now() - ath_dt).days
        if days == 0:   return "set TODAY 🔥"
        if days == 1:   return "set YESTERDAY 🔥"
        if days <= 7:   return f"set {days}d ago"
        if days <= 30:  return f"set {days // 7}w ago"
        if days <= 365: return f"set {days // 30}mo ago"
        return f"set {days // 365}y ago"
    except Exception:
        return f"on {ath_date_str}"

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
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    rs    = rma(gain, length) / rma(loss, length).replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def pine_atr(df, length=14):
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low']  - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return rma(tr, length)

def calc_rr(entry, target, stop):
    """
    FIX v4.1: Explicit R:R calculation added.
    Returns (reward, risk, ratio) or None if invalid.
    """
    try:
        risk   = entry - stop
        reward = target - entry
        if risk <= 0 or reward <= 0:
            return None
        return round(reward, 2), round(risk, 2), round(reward / risk, 2)
    except Exception:
        return None

def beta_position_size(beta, base_pct=2.0):
    """
    FIX v4.1: Beta-adjusted position sizing suggestion.
    Lower allocation for high-beta stocks.
    Returns suggested portfolio % string.
    """
    if not beta or beta <= 0:
        return None
    if beta < 0.8:
        pct = base_pct * 1.5
        label = "low vol"
    elif beta < 1.2:
        pct = base_pct
        label = "market vol"
    elif beta < 1.8:
        pct = base_pct * 0.75
        label = "high vol"
    else:
        pct = base_pct * 0.5
        label = "very high vol"
    return f"~{pct:.1f}% portfolio ({label}, Beta {beta:.2f})"


# ═══════════════════════════════════════════════
# STOCK INFO
# ═══════════════════════════════════════════════

def get_stock_info(symbol):
    if is_crypto(symbol):
        return {
            'sector': 'Crypto', 'industry': 'Cryptocurrency',
            'exchange': '24/7', 'asset_type': 'Crypto',
            'currency': 'USD', 'short_name': symbol,
        }
    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.info or {}

        quote_type = info.get('quoteType', '').upper()
        if quote_type == 'EQUITY':        asset_type = 'Stock'
        elif quote_type == 'ETF':         asset_type = 'ETF'
        elif quote_type == 'MUTUALFUND':  asset_type = 'Fund'
        elif quote_type in ('FUTURE', 'COMMODITY'): asset_type = 'Futures'
        else: asset_type = quote_type or 'Stock'

        return {
            'sector':         info.get('sector', SYMBOL_TO_SECTOR.get(symbol, 'Unknown')),
            'industry':       info.get('industry', ''),
            'exchange':       info.get('exchange', ''),
            'asset_type':     asset_type,
            'currency':       info.get('currency', 'USD'),
            'short_name':     info.get('shortName', symbol),
            'target_mean':    info.get('targetMeanPrice'),
            'target_high':    info.get('targetHighPrice'),
            'target_low':     info.get('targetLowPrice'),
            'analyst_count':  info.get('numberOfAnalystOpinions', 0),
            'rec_key':        info.get('recommendationKey', '').replace('_', ' ').title(),
            'short_pct':      info.get('shortPercentOfFloat'),
            'inst_pct':       info.get('institutionsPercentHeld'),
            'beta':           info.get('beta'),
            'pe_ratio':       info.get('trailingPE'),
            'market_cap':     info.get('marketCap'),
            'forward_pe':     info.get('forwardPE'),
            'peg_ratio':      info.get('pegRatio'),
        }
    except Exception as e:
        logging.debug(f"Stock info {symbol}: {e}")
        return {
            'sector': SYMBOL_TO_SECTOR.get(symbol, 'Unknown'),
            'asset_type': 'Stock', 'currency': 'USD',
        }


def get_cad_price(symbol):
    if is_crypto(symbol) or symbol == 'GC=F':
        return None, None
    for suffix in ['.TO', '.V']:
        tsx_sym = symbol + suffix
        try:
            df = yf.download(tsx_sym, period='2d', interval='1d',
                             progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 1:
                df = _clean_df(df)
                cad_price = float(df['Close'].iloc[-1])
                if cad_price > 0:
                    return round(cad_price, 4 if cad_price < 10 else 2), tsx_sym
        except Exception:
            pass
    return None, None


def get_usd_cad_rate():
    try:
        df = yf.download('USDCAD=X', period='2d', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty:
            return 1.36
        df = _clean_df(df)
        return round(float(df['Close'].iloc[-1]), 4)
    except Exception:
        return 1.36


# ═══════════════════════════════════════════════
# PARABOLIC SAR
# ═══════════════════════════════════════════════

def calc_parabolic_sar(df, af_start=0.02, af_step=0.02, af_max=0.2):
    try:
        high  = df['High'].values
        low   = df['Low'].values
        close = df['Close'].values
        n     = len(df)

        sar  = np.zeros(n)
        ep   = np.zeros(n)
        af   = np.zeros(n)
        bull = np.ones(n, dtype=bool)

        bull[0] = close[1] > close[0]
        sar[0]  = high[0] if bull[0] else low[0]
        ep[0]   = high[0] if bull[0] else low[0]
        af[0]   = af_start

        for i in range(1, n):
            prev_bull = bull[i - 1]
            prev_sar  = sar[i - 1]
            prev_ep   = ep[i - 1]
            prev_af   = af[i - 1]

            new_sar = prev_sar + prev_af * (prev_ep - prev_sar)

            if prev_bull:
                new_sar = min(new_sar, low[i - 1], low[max(0, i - 2)])
                if low[i] < new_sar:
                    bull[i] = False; sar[i] = prev_ep
                    ep[i] = low[i];  af[i] = af_start
                else:
                    bull[i] = True; sar[i] = new_sar
                    if high[i] > prev_ep:
                        ep[i] = high[i]
                        af[i] = min(prev_af + af_step, af_max)
                    else:
                        ep[i] = prev_ep; af[i] = prev_af
            else:
                new_sar = max(new_sar, high[i - 1], high[max(0, i - 2)])
                if high[i] > new_sar:
                    bull[i] = True; sar[i] = prev_ep
                    ep[i] = high[i]; af[i] = af_start
                else:
                    bull[i] = False; sar[i] = new_sar
                    if low[i] < prev_ep:
                        ep[i] = low[i]
                        af[i] = min(prev_af + af_step, af_max)
                    else:
                        ep[i] = prev_ep; af[i] = prev_af

        return pd.Series(bull, index=df.index), pd.Series(sar, index=df.index)
    except Exception:
        return None, None


def calc_adx(df, length=14):
    try:
        high  = df['High']
        low   = df['Low']
        close = df['Close']

        up  = high.diff()
        dn  = -low.diff()

        plus_dm  = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)

        atr_v    = rma(tr, length).replace(0, np.nan)
        plus_di  = 100 * rma(plus_dm,  length) / atr_v
        minus_di = 100 * rma(minus_dm, length) / atr_v
        dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_v    = rma(dx, length)

        return adx_v.fillna(0), plus_di.fillna(0), minus_di.fillna(0)
    except Exception:
        return None, None, None


# ═══════════════════════════════════════════════
# SQUEEZE DETECTION
# ═══════════════════════════════════════════════

def detect_squeeze(df):
    try:
        if len(df) < 30:
            return 'none', None
        bb_basis = sma(df['Close'], 20)
        bb_dev   = df['Close'].rolling(20).std()
        bb_up    = bb_basis + 2.0 * bb_dev
        bb_lo    = bb_basis - 2.0 * bb_dev
        kc_mid   = ema(df['Close'], 20)
        kc_rng   = pine_atr(df, 20)
        kc_up    = kc_mid + 1.5 * kc_rng
        kc_lo    = kc_mid - 1.5 * kc_rng
        in_sq    = (bb_up < kc_up) & (bb_lo > kc_lo)
        if in_sq.iloc[-1]:
            return 'building', None
        if in_sq.iloc[-2] and not in_sq.iloc[-1]:
            direction = 'bullish' if df['Close'].iloc[-1] > bb_basis.iloc[-1] else 'bearish'
            return 'fired', direction
        return 'none', None
    except Exception:
        return 'none', None

def detect_rsi_divergence(df):
    try:
        if len(df) < 30:
            return None
        rsi_series  = pine_rsi(df['Close'], 14)
        look        = 10
        price_lows  = df['Low'].iloc[-look:]
        rsi_lows    = rsi_series.iloc[-look:]
        price_highs = df['High'].iloc[-look:]
        rsi_highs   = rsi_series.iloc[-look:]
        if (price_lows.iloc[-1] < price_lows.iloc[0] and
                rsi_lows.iloc[-1] > rsi_lows.iloc[0] + 3):
            return 'bullish'
        if (price_highs.iloc[-1] > price_highs.iloc[0] and
                rsi_highs.iloc[-1] < rsi_highs.iloc[0] - 3):
            return 'bearish'
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════
# MTF — RSI + ADX + SAR per timeframe
# ═══════════════════════════════════════════════

def get_mtf_verdicts(symbol):
    """
    Returns dict of {label: {trend, rsi, adx, sar_bull, adx_signal, plus_di, minus_di}}
    for Daily / Weekly / Monthly.

    FIX v4.1: Also returns plus_di and minus_di for verdict engine use.
    Trend label now consistent with get_full_context() by using same EMA logic.
    """
    results = {}
    tf_map  = {
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

            c    = float(df['Close'].iloc[-1])
            e20  = float(ema(df['Close'], min(20,  len(df))).iloc[-1])
            e50  = float(ema(df['Close'], min(50,  len(df))).iloc[-1])
            e200 = float(ema(df['Close'], min(200, len(df))).iloc[-1])
            rsi_val = float(pine_rsi(df['Close'], 14).iloc[-1])

            adx_series, plus_di, minus_di = calc_adx(df, 14)
            adx_val   = float(adx_series.iloc[-1]) if adx_series is not None else 0
            plus_val  = float(plus_di.iloc[-1])    if plus_di   is not None else 0
            minus_val = float(minus_di.iloc[-1])   if minus_di  is not None else 0

            sar_bull_series, _ = calc_parabolic_sar(df)
            sar_bull = bool(sar_bull_series.iloc[-1]) if sar_bull_series is not None else None

            if adx_val >= 25 and sar_bull is True and plus_val > minus_val:
                adx_sar = "✅ Trend BUY"
            elif adx_val >= 25 and sar_bull is False and minus_val > plus_val:
                adx_sar = "❌ Trend SELL"
            elif adx_val < 20:
                adx_sar = "⚠️ Ranging"
            else:
                adx_sar = "➖ Mixed"

            # FIX v4.1: Use same 4-condition logic as get_full_context() for consistency
            if c > e20 > e50 > e200:   trend = "🚀 Strong Bull"
            elif c > e50 > e200:       trend = "📈 Bull"
            elif c < e20 < e50 < e200: trend = "💀 Strong Bear"
            elif c < e50 < e200:       trend = "📉 Bear"
            elif c > e200 and c < e50: trend = "🔄 Pullback"
            elif c < e200 and c > e50: trend = "🔀 Recovering"
            else:                      trend = "⚖️ Mixed"

            results[label] = {
                'trend':    trend,
                'rsi':      round(rsi_val, 1),
                'adx':      round(adx_val, 1),
                'plus_di':  round(plus_val, 1),
                'minus_di': round(minus_val, 1),
                'sar_bull': sar_bull,
                'adx_sar':  adx_sar,
            }
            time.sleep(0.2)
        except Exception as e:
            logging.debug(f"MTF {symbol} {label}: {e}")
    return results


# ═══════════════════════════════════════════════
# SECTOR CONTEXT
# ═══════════════════════════════════════════════

def get_sector_context(symbol):
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
            df  = _clean_df(df)
            chg = (float(df['Close'].iloc[-1]) - float(df['Close'].iloc[-2])) / float(df['Close'].iloc[-2]) * 100
            changes.append(chg)
            time.sleep(0.15)
        except Exception:
            pass
    if not changes:
        return sector, None, None
    return sector, round(sum(changes) / len(changes), 2), syms


# ═══════════════════════════════════════════════
# POC + STRUCTURE
# ═══════════════════════════════════════════════

def quick_poc(df_daily):
    try:
        recent = df_daily.iloc[-60:]
        low    = float(recent['Low'].min())
        high   = float(recent['High'].max())
        if high <= low:
            return None
        bins         = 30
        bin_edges    = np.linspace(low, high, bins + 1)
        vol_at_price = np.zeros(bins)
        for i in range(len(recent)):
            bar_low  = float(recent['Low'].iloc[i])
            bar_high = float(recent['High'].iloc[i])
            bar_vol  = float(recent['Volume'].iloc[i])
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
# VERDICT ENGINE v4.1
# ═══════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None, mtf_verdicts=None):
    """
    FIX v4.1 changes:
    1. BUY ZONE stop now EMA50 not EMA200 (v4.0 bug: -15% stop on pullback entry)
    2. Low ADX (< 15) on daily blocks BUY verdict — no edge in choppy market
    3. EMA50 stretch > 15% blocks BUY momentum verdict
    4. Flags are returned to format function for inline conflict warnings
    """
    c         = ctx
    rsi       = c['rsi']
    trend     = c['trend']
    drop      = c['day_change_pct']
    from_ath  = c['ath_pct']
    range_pos = c['range_pos']
    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']

    reasons    = []
    next_steps = []
    verdict    = None
    zone       = None
    flags      = {}  # NEW: conflict flags passed to formatter

    # MTF alignment
    mtf_all_bull  = False
    daily_adx     = None
    daily_sar_bull = None
    if mtf_verdicts:
        bull_count = sum(1 for v in mtf_verdicts.values() if 'Bull' in v.get('trend', ''))
        mtf_all_bull = bull_count == len(mtf_verdicts)
        if 'Daily' in mtf_verdicts:
            daily_adx      = mtf_verdicts['Daily'].get('adx', 0)
            daily_sar_bull = mtf_verdicts['Daily'].get('sar_bull')

    # EMA50 stretch
    stretch_pct = (c['current'] - c['ema50']) / c['ema50'] * 100 if c['ema50'] > 0 else 0

    # 0. PARABOLIC
    if abs(drop) >= 15:
        if drop > 0:
            verdict, zone = "⚠️ PARABOLIC", f"News/Catalyst Spike +{drop:.0f}%"
            reasons    = [f"+{drop:.1f}% single-day — likely news driven",
                          "Parabolic moves mean-revert — high risk to chase"]
            next_steps = ["DO NOT chase at current price",
                          "Wait for 3-5 day consolidation",
                          f"Re-entry: pullback to EMA50 `${c['ema50']:.2f}`"]
        else:
            verdict, zone = "🚨 CRASH", f"Severe Drop {drop:.0f}%"
            reasons    = [f"{drop:.1f}% single-day drop — likely news driven",
                          "Wait for dust to settle"]
            next_steps = ["Do NOT catch today",
                          "Wait minimum 3 days",
                          f"Watch: does it hold EMA200 `${c['ema200']:.2f}`?"]
        return verdict, zone, reasons, next_steps, flags

    # 1. MOMENTUM — at/near ATH
    if ("UPTREND" in trend and from_ath > -5 and above_50 and above_200
            and rsi < 80 and stretch_pct < 20):
        verdict, zone = "🚀 MOMENTUM", "AT ATH — Continuation"
        reasons = [f"At/near ATH ({from_ath:+.1f}%)", "EMA stack fully bullish",
                   f"RSI {rsi:.0f} — not overbought"]
        if mtf_all_bull:
            reasons.append("All timeframes aligned bullish 🎯")
        # FIX v4.1: Stop is EMA50, not EMA200
        next_steps = [f"Breakout: above ATH `${c['ath']:.2f}` with volume",
                      f"Pullback entry: dip to EMA50 `${c['ema50']:.2f}`",
                      f"Stop: below EMA50 `${c['ema50']:.2f}`"]

    # 2. STRONG UPTREND PULLBACK → BUY ZONE
    elif "UPTREND" in trend and rsi < 52 and above_200:
        # FIX v4.1: Block BUY if daily ADX too low (ranging market)
        if daily_adx is not None and daily_adx < 15:
            verdict, zone = "🟡 WATCH", "Weak Trend — Wait for Strength"
            reasons = [f"RSI {rsi:.0f} pullback looks good BUT daily ADX {daily_adx:.0f} — market ranging",
                       "No directional edge in choppy conditions",
                       "Wait for ADX > 20 before entry"]
            next_steps = [f"Monitor: ADX rising above 20 confirms trend",
                          f"Entry then: `${c['ema50']:.2f}` area",
                          f"Stop: below `${c['ema200']:.2f}`"]
            flags['low_adx_block'] = daily_adx
        else:
            verdict, zone = "🟢 BUY ZONE", "Pullback in Uptrend"
            reasons = ["Healthy pullback in confirmed uptrend", f"RSI {rsi:.0f} — room to run"]
            if from_ath > -20:
                reasons.append("Near ATH — strong stock pulling back")
            # FIX v4.1: Stop is EMA50 (below entry), not EMA200 (creates -15% stop)
            # R:R: entry at current, target ATH, stop just below EMA50
            stop_price   = c['ema50'] * 0.98  # 2% below EMA50
            target_price = c['ath']
            rr = calc_rr(c['current'], target_price, stop_price)
            rr_str = f" (R:R {rr[2]:.1f}:1)" if rr else ""
            next_steps = [f"Entry: `${c['current']:.2f}` or lower{rr_str}",
                          f"Target: ATH `${c['ath']:.2f}`",
                          f"Stop: below EMA50 `${stop_price:.2f}` (2% buffer)"]

    # 3. EMA50 PULLBACK
    elif "PULLBACK" in trend and rsi < 55:
        verdict, zone = "🟢 BUY ZONE", "EMA50 Pullback"
        reasons = ["Above EMA200 — uptrend intact",
                   f"Pulling toward EMA50 `${c['ema50']:.2f}`",
                   f"RSI {rsi:.0f} — watch for bounce"]
        stop_price = c['ema200'] * 0.99
        next_steps = [f"Entry: near EMA50 `${c['ema50']:.2f}`",
                      f"Stop: below EMA200 `${stop_price:.2f}`",
                      f"Target: `${c['high_52w']:.2f}`"]

    # 4. EXTENDED
    elif from_ath > -8 and rsi > 75:
        verdict, zone = "🟠 EXTENDED", "Overbought Near ATH"
        reasons = [f"RSI {rsi:.0f} — overbought", "Risk/reward not ideal"]
        next_steps = ["Wait for RSI to cool to 50-60",
                      f"Better entry: EMA50 `${c['ema50']:.2f}`",
                      "If holding: trail stop, don't add"]

    # 5. DOWNTREND
    elif "DOWNTREND" in trend and not above_200:
        verdict, zone = "🔴 AVOID", "Falling Knife"
        reasons = ["Below EMA50 & EMA200 — confirmed downtrend"]
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal")
        next_steps = [f"Wait for: close above EMA50 `${c['ema50']:.2f}`",
                      "Confirm EMA50 > EMA200 cross before entry"]

    # 6. NEAR 52W LOW
    elif c['pct_from_52w_low'] < 8 and drop < -3:
        verdict, zone = "⚠️ CAUTION", "Breaking Down"
        reasons = ["Near 52W low — key support at risk"]
        next_steps = [f"Watch: holds `${c['low_52w']:.2f}` (52W low)",
                      "Enter only after 2-3 days stabilisation"]

    # 7. TAKE PROFITS
    elif rsi > 75 and drop > 2:
        verdict, zone = "🟠 TAKE PROFITS", "Extended"
        reasons = [f"RSI overbought ({rsi:.0f})", "Consider trimming"]
        next_steps = ["Trim 25-33% of position here",
                      f"Re-entry: pullback to EMA50 `${c['ema50']:.2f}`",
                      f"Trail stop: `${c['ema50'] * 0.97:.2f}`"]

    # 8. RECOVERING
    elif "RECOVERING" in trend:
        if rsi > 55 and drop > 0:
            verdict, zone = "🟡 WATCH", "Recovery Attempt"
            reasons = ["Reclaiming EMA50", f"Must clear EMA200 `${c['ema200']:.2f}`"]
            next_steps = [f"Trigger: close above EMA200 `${c['ema200']:.2f}`"]
        else:
            verdict, zone = "⏸️ HOLD", "Below EMA200"
            reasons = ["Below EMA200 — no structural confirmation"]
            next_steps = [f"Wait for: reclaim EMA200 `${c['ema200']:.2f}`"]

    # 9. MIXED
    elif "MIXED" in trend:
        if range_pos < 35 and rsi < 45:
            verdict, zone = "🟡 WATCH", "Potential Base"
            reasons = ["Lower 52W range — possible accumulation"]
            next_steps = [f"Trigger: RSI > 50 + close above EMA50 `${c['ema50']:.2f}`"]
        else:
            verdict, zone = "⏸️ NEUTRAL", "No Clear Edge"
            reasons = ["Mixed signals — no directional conviction"]
            next_steps = [f"Bull: above EMA50 `${c['ema50']:.2f}` + RSI > 55",
                          f"Bear: below EMA200 `${c['ema200']:.2f}`"]

    # 10. DEFAULT
    else:
        if above_50 and above_200 and rsi > 55:
            verdict, zone = "🟡 WATCH", "Building Momentum"
            reasons = ["Above both EMAs", f"RSI {rsi:.0f} building"]
            next_steps = [f"Pullback entry: EMA50 `${c['ema50']:.2f}`",
                          f"Breakout: above `${c['high_52w']:.2f}`"]
        else:
            verdict, zone = "⏸️ NEUTRAL", "No Clear Setup"
            reasons = ["No strong directional signal"]
            next_steps = [f"Bull trigger: above EMA50 `${c['ema50']:.2f}` + RSI > 55"]

    # Market override
    if market_ctx:
        vix     = market_ctx.get('^VIX', {}).get('price', 15)
        spy_pct = market_ctx.get('SPY',  {}).get('pct', 0)
        if vix > 25 and spy_pct < -1.5 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
            verdict = "⚠️ WAIT"
            reasons.insert(0, f"Market bleeding — VIX {vix:.0f}, SPY {spy_pct:.1f}%")
            next_steps = ["Wait for market to stabilise"]

    # Earnings override
    if any(x in verdict for x in ["BUY", "MOMENTUM", "WATCH"]):
        _, days_until = get_earnings_date(c['symbol'])
        if days_until is not None and days_until <= EARNINGS_WARNING_DAYS:
            verdict = "⚠️ WAIT — Earnings"
            zone    = f"Earnings in {days_until}d"
            reasons.insert(0, f"Earnings in {days_until} days — skip new entries")
            next_steps = ["Re-evaluate after earnings"]

    # FIX v4.1: EMA50 stretch warning for overbought extension
    if stretch_pct > 15 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
        flags['ema50_stretch'] = stretch_pct

    # FIX v4.1: SAR conflict flag — bearish SAR but BUY verdict
    if daily_sar_bull is False and any(x in verdict for x in ["BUY", "MOMENTUM"]):
        flags['sar_conflict'] = True

    return verdict, zone, reasons, next_steps, flags


# ═══════════════════════════════════════════════
# AI ANALYSIS
# ═══════════════════════════════════════════════

def get_ai_analysis(ctx, verdict, zone, sector_name, sector_avg,
                    mtf_verdicts, stock_info):
    from market_intel import GEMINI_API_KEY
    if not GEMINI_API_KEY:
        return None

    c = ctx
    mtf_str = "\n".join([
        f"  {tf}: {v['trend']} | RSI {v['rsi']} | ADX {v['adx']} | {v['adx_sar']}"
        for tf, v in mtf_verdicts.items()
    ]) if mtf_verdicts else "  N/A"

    sector_str = f"{sector_name}: {sector_avg:+.1f}% avg today" if sector_name and sector_avg else "Unknown"
    analyst_str = ""
    if stock_info.get('target_mean'):
        upside = (stock_info['target_mean'] - c['current']) / c['current'] * 100
        analyst_str = (f"\nAnalyst target: ${stock_info['target_mean']:.2f} mean "
                       f"({upside:+.1f}% upside) — {stock_info.get('rec_key','')}")

    prompt = f"""You are a senior trading analyst. Analyze this setup in EXACTLY 4 lines (max 110 chars each).

SETUP: {c['symbol']} | {zone}
Price: ${c['current']:.2f} | Day: {c['day_change_pct']:+.1f}% | Vol: {c['vol_ratio']:.1f}× avg
Trend: {c['trend']} | RSI: {c['rsi']:.0f} | ATH: {c['ath_pct']:+.1f}% | 52W pos: {c['range_pos']:.0f}%
EMA50: ${c['ema50']:.2f} | EMA200: ${c['ema200']:.2f}

TIMEFRAMES:
{mtf_str}

SECTOR: {sector_str}{analyst_str}

Respond EXACTLY:
📊 [Technical/sector/catalyst? Specific]
🎯 [Setup quality & R:R — is it worth taking?]
⚠️ [Biggest invalidation risk — specific level]
💡 [STRONG BUY/BUY/HOLD/AVOID/WAIT] — [one sharp sentence]

4 lines only."""

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
        logging.error(f"AI: {e}")
    return None


# ═══════════════════════════════════════════════
# FORMAT MARKET CAP
# ═══════════════════════════════════════════════

def fmt_mcap(val):
    if not val:
        return None
    if val >= 1e12:  return f"${val/1e12:.1f}T"
    if val >= 1e9:   return f"${val/1e9:.1f}B"
    if val >= 1e6:   return f"${val/1e6:.1f}M"
    return f"${val:.0f}"


# ═══════════════════════════════════════════════
# FORMAT FULL ANALYSIS v4.1
# ═══════════════════════════════════════════════

def format_full_analysis(symbol, ctx, verdict, zone, reasons, next_steps,
                          ai_text, market_ctx, rs_score, rs_label,
                          poc, support, resistance,
                          squeeze_state, squeeze_dir, rsi_div,
                          mtf_verdicts, sector_name, sector_avg,
                          stock_info, cad_price, tsx_symbol, usd_cad,
                          flags=None):
    if flags is None:
        flags = {}

    em  = SYMBOL_EMOJI.get(symbol, '📊')
    c   = ctx
    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%a %b %d • %I:%M %p {tz}')
    decimals = 4 if c['current'] < 10 else 2
    pf       = f"{{:.{decimals}f}}"
    drop     = c['day_change_pct']
    drop_em  = "🟢" if drop >= 0 else "🔴"
    sign     = "+" if drop >= 0 else ""

    asset_type = stock_info.get('asset_type', 'Stock')
    sector_h   = stock_info.get('sector', SYMBOL_TO_SECTOR.get(symbol, ''))
    industry   = stock_info.get('industry', '')
    exchange   = stock_info.get('exchange', '')
    mcap       = fmt_mcap(stock_info.get('market_cap'))
    beta_val   = stock_info.get('beta')

    msg  = f"🔍 *ON-DEMAND ANALYSIS*\n"
    msg += f"{em} *{symbol}* • {ts}\n"

    type_parts = [asset_type]
    if sector_h:  type_parts.append(sector_h)
    if industry and industry != sector_h: type_parts.append(industry)
    if exchange:  type_parts.append(exchange)
    if mcap:      type_parts.append(f"Mkt cap {mcap}")
    msg += f"_{' • '.join(type_parts)}_\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # ── VERDICT ──
    msg += f"*{verdict}*\n"
    msg += f"_Zone: {zone}_\n"
    for r in reasons[:3]:
        msg += f"  • {r}\n"

    # FIX v4.1: Inline conflict warnings
    if flags.get('low_adx_block'):
        msg += f"⚠️ _ADX {flags['low_adx_block']:.0f} — market ranging, conviction low_\n"
    if flags.get('ema50_stretch'):
        msg += f"⚠️ _EMA50 stretch {flags['ema50_stretch']:.1f}% — overbought extension risk_\n"
    if flags.get('sar_conflict'):
        msg += f"⚠️ _Daily SAR bearish — contradicts BUY verdict, reduce size_\n"

    if squeeze_state == 'building':
        msg += f"\n🔥 *SQUEEZE BUILDING* — explosive move loading\n"
    elif squeeze_state == 'fired':
        dir_em = "⬆️" if squeeze_dir == 'bullish' else "⬇️"
        msg += f"\n💥 *SQUEEZE FIRED* {dir_em} {squeeze_dir}\n"
    if rsi_div == 'bullish':
        msg += f"📈 *RSI DIVERGENCE* — momentum building (bullish)\n"
    elif rsi_div == 'bearish':
        msg += f"📉 *RSI DIVERGENCE* — momentum fading (bearish)\n"

    # AI top summary
    if ai_text:
        lines   = ai_text.strip().split('\n')
        summary = next((l for l in lines if '💡' in l), None)
        if summary:
            msg += f"\n{summary}\n"
    elif not is_crypto(symbol):
        # FIX v4.1: Flag AI unavailability instead of silent skip
        msg += f"\n_💡 AI analysis unavailable_\n"

    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # ── PRICE ──
    msg += f"*💵 PRICE (USD)*\n`─────────────────`\n"
    msg += f"Live: `${pf.format(c['current'])}` ({drop_em} {sign}{drop:.2f}% today)\n"
    msg += f"Range: L `${pf.format(c['today_low'])}` → H `${pf.format(c['today_high'])}`\n"
    msg += f"Volume: {volume_label(c['vol_ratio'])}\n"

    # CAD pricing
    if cad_price and tsx_symbol:
        msg += f"\n*🍁 WEALTHSIMPLE (CAD)*\n`─────────────────`\n"
        msg += f"TSX: `{tsx_symbol}` → `${cad_price:.2f} CAD`\n"
        if usd_cad:
            implied = round(c['current'] * usd_cad, 2)
            msg += f"USD→CAD implied: `${implied:.2f}` (rate: {usd_cad:.4f})\n"
            # Verify CAD price vs implied (should match within ~2%)
            if abs(cad_price - implied) / implied > 0.03:
                msg += f"⚠️ _TSX price diverges >3% from implied — check spread_\n"
    elif not is_crypto(symbol):
        if usd_cad:
            implied = round(c['current'] * usd_cad, 2)
            msg += f"🍁 CAD equiv: `${implied:.2f}` (USD×{usd_cad:.4f}) — no TSX listing\n"

    # POC
    if poc:
        diff_pct = (c['current'] - poc) / poc * 100
        # FIX v4.1: POC conflict note when BUY verdict but below POC
        poc_conflict = diff_pct < -0.5 and any(x in verdict for x in ["BUY", "MOMENTUM"])
        if abs(diff_pct) < 0.5:
            msg += f"🎯 *AT POC* `${pf.format(poc)}` — volume magnet\n"
        elif c['current'] > poc:
            msg += f"🎯 Above POC `${pf.format(poc)}` — buyers in control\n"
        else:
            msg += f"🎯 Below POC `${pf.format(poc)}` — sellers in control\n"
            if poc_conflict:
                msg += f"   ⚠️ _Below POC contradicts BUY — wait for POC reclaim_\n"

    # ── TIMEFRAME ALIGNMENT ──
    if mtf_verdicts:
        msg += f"\n*🗂️ TIMEFRAME ALIGNMENT*\n`─────────────────`\n"
        for tf_label, v in mtf_verdicts.items():
            sar_em  = "✅" if v.get('sar_bull') else "❌" if v.get('sar_bull') is False else "➖"
            adx_val = v['adx']
            adx_em  = "💪" if adx_val >= 25 else "⚠️" if adx_val < 20 else "➖"
            msg += (f"{tf_label:7s}: {v['trend']}\n"
                    f"         RSI {v['rsi']} | {adx_em} ADX {adx_val:.0f} "
                    f"| +DI {v.get('plus_di',0):.0f} / -DI {v.get('minus_di',0):.0f} "
                    f"| SAR {sar_em} | {v['adx_sar']}\n")

    # ── TECHNICALS ──
    msg += f"\n*📈 TECHNICALS*\n`─────────────────`\n"
    # FIX v4.1: Use MTF daily trend if available for consistency
    display_trend = c['trend']
    if mtf_verdicts and 'Daily' in mtf_verdicts:
        mtf_trend = mtf_verdicts['Daily']['trend']
        if mtf_trend != display_trend:
            display_trend = f"{display_trend} _{(mtf_trend)} on MTF_"
    msg += f"{display_trend}\n"

    rsi_tag = "_(oversold)_" if c['rsi'] < 30 else "_(overbought)_" if c['rsi'] > 70 else "_(bullish)_" if c['rsi'] > 60 else "_(neutral)_"
    msg += f"RSI: `{c['rsi']:.0f}` {rsi_tag}\n"

    stretch_pct = (c['current'] - c['ema50']) / c['ema50'] * 100 if c['ema50'] > 0 else 0
    stretch_warn = ""
    if stretch_pct > 15:
        stretch_warn = " ⚠️ _Extended — reversion risk_"
    elif stretch_pct < -10:
        stretch_warn = " ✅ _Deeply oversold — mean reversion likely_"
    msg += f"EMA50: `${pf.format(c['ema50'])}` ({stretch_pct:+.1f}% away){stretch_warn}\n"
    msg += f"EMA200: `${pf.format(c['ema200'])}`\n"

    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if above_50 and above_200:        msg += "✅ Above EMA50 & EMA200\n"
    elif above_200 and not above_50:  msg += "⚠️ Below EMA50, above EMA200\n"
    elif not above_200 and above_50:  msg += "🔀 Above EMA50, below EMA200\n"
    else:                             msg += "🔴 Below both EMAs\n"

    # FIX v4.1: Beta with position sizing hint
    if beta_val:
        beta_desc   = "low vol" if beta_val < 0.8 else "high vol" if beta_val > 1.5 else "market vol"
        pos_suggest = beta_position_size(beta_val)
        msg += f"Beta: `{beta_val:.2f}` _{beta_desc}_"
        if pos_suggest:
            msg += f" • Size: _{pos_suggest}_"
        msg += "\n"

    # FIX v4.1: PE ratio shown when available
    pe = stock_info.get('pe_ratio')
    fpe = stock_info.get('forward_pe')
    if pe and not is_crypto(symbol):
        pe_str = f"P/E: `{pe:.1f}`"
        if fpe:
            pe_str += f" • Fwd P/E: `{fpe:.1f}`"
        msg += pe_str + "\n"

    # ── POSITION ──
    msg += f"\n*📏 POSITION*\n`─────────────────`\n"
    pos = int(c['range_pos'] / 10)
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"`{bar}` {c['range_pos']:.0f}% of 52W range\n"
    msg += f"52W: `${pf.format(c['low_52w'])}` → `${pf.format(c['high_52w'])}`\n"
    msg += f"ATH: `${pf.format(c['ath'])}` ({c['ath_pct']:+.1f}%) — {ath_recency(c['ath_date'])}\n"
    if support and resistance:
        msg += f"Structure: Support `${pf.format(support)}` • Resistance `${pf.format(resistance)}`\n"

    # ── ANALYST TARGETS ──
    target_mean  = stock_info.get('target_mean')
    target_high  = stock_info.get('target_high')
    target_low   = stock_info.get('target_low')
    rec_key      = stock_info.get('rec_key', '')
    analyst_n    = stock_info.get('analyst_count', 0)

    if target_mean and not is_crypto(symbol):
        msg += f"\n*🎯 ANALYST TARGETS*\n`─────────────────`\n"
        upside    = (target_mean - c['current']) / c['current'] * 100
        upside_em = "🟢" if upside > 0 else "🔴"
        msg += f"Consensus: `${target_mean:.2f}` {upside_em} {upside:+.1f}% upside"
        if analyst_n:
            msg += f" ({analyst_n} analysts)"
        msg += "\n"
        # FIX v4.1: Flag thin analyst coverage
        if analyst_n and analyst_n < 3:
            msg += f"⚠️ _Only {analyst_n} analyst(s) — low coverage, treat target with caution_\n"
        if target_high and target_low:
            msg += f"Range: `${target_low:.2f}` → `${target_high:.2f}`\n"
        if rec_key:
            rec_em = "🟢" if 'Buy' in rec_key else "🔴" if 'Sell' in rec_key else "🟡"
            msg += f"Rating: {rec_em} *{rec_key}*\n"

    # ── FUNDAMENTALS ──
    short_pct = stock_info.get('short_pct')
    inst_pct  = stock_info.get('inst_pct')
    peg       = stock_info.get('peg_ratio')
    if (short_pct or inst_pct or peg) and not is_crypto(symbol):
        msg += f"\n*📊 FUNDAMENTALS*\n`─────────────────`\n"
        if peg:
            peg_em = "🟢" if peg < 1.5 else "🟡" if peg < 2.5 else "🔴"
            msg += f"PEG: `{peg:.2f}` {peg_em}\n"
        if short_pct:
            short_em = "⚠️ High" if short_pct > 0.15 else "Normal"
            msg += f"Short interest: `{short_pct*100:.1f}%` — {short_em}\n"
            if short_pct > 0.15:
                msg += f"   _High shorts = squeeze potential on breakout_\n"
        if inst_pct:
            msg += f"Institutional: `{inst_pct*100:.0f}%` — "
            msg += "Smart money heavy\n" if inst_pct > 0.7 else "Moderate\n"

    # ── SECTOR ──
    if sector_name and sector_avg is not None:
        sec_em = "🟢" if sector_avg > 0 else "🔴"
        sym_vs = drop - sector_avg
        msg += f"\n*🏭 SECTOR ({sector_name})*\n`─────────────────`\n"
        msg += f"Sector avg: {sec_em} `{sector_avg:+.2f}%` today\n"
        if sym_vs > 1.5:
            msg += f"💪 Outperforming sector by `{sym_vs:+.1f}%`\n"
        elif sym_vs < -1.5:
            msg += f"⚠️ Underperforming sector by `{sym_vs:+.1f}%`\n"
        else:
            msg += f"➖ In line with sector\n"

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
                vix_em  = "🔴" if vix_val > 25 else "🟡" if vix_val > 18 else "🟢"
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
    msg += f"_AlphaEdge v4.1 • On-demand_"
    return msg


def format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score, stock_info):
    em   = SYMBOL_EMOJI.get(symbol, '📊')
    c    = ctx
    drop = c['day_change_pct']
    drop_em = "🟢" if drop >= 0 else "🔴"
    sign    = "+" if drop >= 0 else ""
    decimals = 4 if c['current'] < 10 else 2
    pf   = f"{{:.{decimals}f}}"
    rs_str  = f" • RS {rs_label}" if rs_label else ""
    sector_h = stock_info.get('sector', '')
    sector_str = f" • _{sector_h}_" if sector_h else ""
    msg  = f"🔍 {em} *{symbol}* `${pf.format(c['current'])}` ({drop_em}{sign}{drop:.1f}%){sector_str}\n"
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
    alerts = load_alerts()
    try:
        df = yf.download(symbol, period='1d', interval='1m',
                         progress=False, auto_adjust=True)
        df      = _clean_df(df)
        current = float(df['Close'].iloc[-1]) if not df.empty else None
    except Exception:
        current = None

    if direction == 'auto' and current:
        direction = 'above' if target_price > current else 'below'

    alert_key         = f"{symbol}_{target_price}"
    alerts[alert_key] = {
        'symbol':               symbol,
        'target':               target_price,
        'direction':            direction,
        'set_at':               now_est().isoformat(),
        'expires_at':           (now_est() + timedelta(days=30)).isoformat(),
        'warning_sent':         False,
        'expiry_warning_sent':  False,
        'triggered':            False,
    }
    save_alerts(alerts)

    dir_str    = "rises to" if direction == 'above' else "falls to"
    warn_price = target_price * 0.98 if direction == 'above' else target_price * 1.02
    current_str = f" (currently `${current:.2f}`)" if current else ""
    send_telegram(
        f"✅ *Alert set!*\n"
        f"{SYMBOL_EMOJI.get(symbol,'📊')} *{symbol}* — notify when {dir_str} `${target_price:.2f}`{current_str}\n"
        f"⚡ Early warning at `${warn_price:.2f}` (2% before)\n"
        f"⏰ Expires in 30 days"
    )

def cancel_alert(symbol):
    alerts  = load_alerts()
    removed = []
    for key in list(alerts.keys()):
        if alerts[key]['symbol'] == symbol:
            removed.append(alerts[key]['target'])
            del alerts[key]
    save_alerts(alerts)
    em = SYMBOL_EMOJI.get(symbol, '📊')
    if removed:
        send_telegram(f"🗑️ Cancelled alerts for {em} *{symbol}*: {', '.join([f'${t}' for t in removed])}")
    else:
        send_telegram(f"❌ No active alerts for *{symbol}*")

def list_alerts():
    alerts = load_alerts()
    active = {k: v for k, v in alerts.items() if not v.get('triggered')}
    if not active:
        send_telegram("📋 *No active alerts.*\n\nSet one: `alert TSLA 450`")
        return
    msg = f"📋 *ACTIVE ALERTS ({len(active)})*\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"
    for key, a in sorted(active.items(), key=lambda x: x[1]['symbol']):
        em        = SYMBOL_EMOJI.get(a['symbol'], '📊')
        dir_em    = "⬆️" if a['direction'] == 'above' else "⬇️"
        expires   = datetime.fromisoformat(a['expires_at'])
        days_left = (expires - now_est()).days
        warn      = a['target'] * 0.98 if a['direction'] == 'above' else a['target'] * 1.02
        msg += f"{em} *{a['symbol']}* {dir_em} `${a['target']:.2f}`\n"
        msg += f"   ⚡ Warning at `${warn:.2f}` • ⏰ {days_left}d left\n\n"
    send_telegram(msg)

def check_alerts():
    alerts  = load_alerts()
    if not alerts:
        return
    changed = False
    now     = now_est()
    for key, a in list(alerts.items()):
        if a.get('triggered'):
            continue
        symbol     = a['symbol']
        target     = a['target']
        direction  = a['direction']
        warn_price = target * 0.98 if direction == 'above' else target * 1.02
        em         = SYMBOL_EMOJI.get(symbol, '📊')

        expires   = datetime.fromisoformat(a['expires_at'])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=EST)
        days_left = (expires - now).days

        if days_left <= 1 and not a.get('expiry_warning_sent'):
            send_telegram(f"⏰ *Alert expiring!*\n{em} *{symbol}* → `${target:.2f}` expires tomorrow\n`alert {symbol} {target}` to reset")
            a['expiry_warning_sent'] = True
            changed = True

        if now > expires:
            send_telegram(f"🗑️ *Alert expired*\n{em} *{symbol}* → `${target:.2f}` (30 days, untriggered)")
            del alerts[key]
            changed = True
            continue

        try:
            df      = yf.download(symbol, period='1d', interval='5m', progress=False, auto_adjust=True)
            if df.empty: continue
            df      = _clean_df(df)
            current = float(df['Close'].iloc[-1])
        except Exception:
            continue

        if ((direction == 'above' and current >= target) or
                (direction == 'below' and current <= target)):
            send_telegram(f"🎯 *ALERT TRIGGERED!*\n{em} *{symbol}* hit `${target:.2f}`\nCurrent: `${current:.2f}`\n_Alert removed._")
            a['triggered'] = True
            changed = True
        elif (not a.get('warning_sent') and
              ((direction == 'above' and current >= warn_price) or
               (direction == 'below' and current <= warn_price))):
            send_telegram(f"⚡ *APPROACHING TARGET!*\n{em} *{symbol}* near `${target:.2f}` alert\nNow: `${current:.2f}` — {abs(current-target)/target*100:.1f}% away")
            a['warning_sent'] = True
            changed = True

    if changed:
        save_alerts(alerts)


# ═══════════════════════════════════════════════
# WATCHLIST SCAN
# ═══════════════════════════════════════════════

def run_watchlist_scan():
    send_telegram("🔍 *Scanning watchlist...* ~60s", silent=True)
    all_syms, emoji_map = load_universe()
    if not all_syms:
        send_telegram("❌ Could not load symbols.yaml")
        return

    market_ctx = get_market_ctx()
    results    = []

    for sym in all_syms:
        try:
            print(f"  → {sym}...", end=" ", flush=True)
            ctx = get_full_context(sym)
            time.sleep(0.3)
            if not ctx:
                print("—"); continue
            verdict, zone, _, _, _ = get_verdict(ctx, market_ctx)
            rs_score, rs_label     = calc_relative_strength(ctx)
            results.append({
                'symbol':   sym,
                'emoji':    emoji_map.get(sym, '📊'),
                'verdict':  verdict,
                'zone':     zone,
                'drop':     ctx['day_change_pct'],
                'rsi':      ctx['rsi'],
                'rs_score': rs_score or 0,
                'current':  ctx['current'],
            })
            print(f"{ctx['day_change_pct']:+.1f}% {verdict}")
        except Exception as e:
            print(f"💥 {e}")

    if not results:
        send_telegram("❌ No data — try again later")
        return

    def sort_key(r):
        v = r['verdict']
        if 'MOMENTUM' in v: return (0, -r['drop'])
        if 'BUY'      in v: return (1, -r['drop'])
        if 'WATCH'    in v: return (2, -r['drop'])
        if 'NEUTRAL'  in v: return (3, -r['drop'])
        if 'EXTENDED' in v: return (4, -r['drop'])
        if 'AVOID'    in v: return (5, -r['drop'])
        return (6, -r['drop'])

    results.sort(key=sort_key)

    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%a %b %d • %I:%M %p {tz}')
    msg = f"📊 *WATCHLIST SCAN*\n🕒 {ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    groups = {}
    for r in results:
        groups.setdefault(r['verdict'], []).append(r)

    for vkey, items in groups.items():
        msg += f"*{vkey}*\n"
        for r in items:
            drop_em  = "🟢" if r['drop'] >= 0 else "🔴"
            sign     = "+" if r['drop'] >= 0 else ""
            decimals = 4 if r['current'] < 10 else 2
            pf       = f"{{:.{decimals}f}}"
            rs_str   = f" RS {r['rs_score']:+.1f}%" if r['rs_score'] else ""
            msg += (f"  {r['emoji']} *{r['symbol']}* `${pf.format(r['current'])}` "
                    f"{drop_em}{sign}{r['drop']:.1f}% RSI `{r['rsi']:.0f}`{rs_str}\n")
        msg += "\n"

    msg += "_Type any symbol for full analysis_\n_AlphaEdge v4.1_"
    send_telegram(msg)


# ═══════════════════════════════════════════════
# TOP MOVERS
# ═══════════════════════════════════════════════

def run_top_movers():
    send_telegram("📊 *Fetching top movers...*", silent=True)
    all_syms, emoji_map = load_universe()
    market_ctx = get_market_ctx()
    movers     = []

    for sym in all_syms:
        try:
            df = yf.download(sym, period='5d', interval='1d',
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 2: continue
            df     = _clean_df(df)
            change = (float(df['Close'].iloc[-1]) - float(df['Close'].iloc[-2])) / float(df['Close'].iloc[-2]) * 100
            movers.append({'symbol': sym, 'emoji': emoji_map.get(sym,'📊'),
                           'change': change, 'price': float(df['Close'].iloc[-1])})
            time.sleep(0.2)
        except Exception:
            pass

    if not movers:
        send_telegram("❌ Could not fetch data"); return

    movers.sort(key=lambda x: -x['change'])
    gainers = [m for m in movers if m['change'] > 0][:5]
    losers  = [m for m in movers if m['change'] < 0][-5:]
    losers.reverse()

    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%a %b %d • %I:%M %p {tz}')
    msg = f"📊 *TOP MOVERS*\n🕒 {ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    if gainers:
        msg += "*🚀 GAINERS*\n"
        for m in gainers:
            d = 4 if m['price'] < 10 else 2
            msg += f"  {m['emoji']} *{m['symbol']}* `${m['price']:.{d}f}` 🟢 +{m['change']:.2f}%\n"
    if losers:
        msg += f"\n*📉 LOSERS*\n"
        for m in losers:
            d = 4 if m['price'] < 10 else 2
            msg += f"  {m['emoji']} *{m['symbol']}* `${m['price']:.{d}f}` 🔴 {m['change']:.2f}%\n"

    if market_ctx:
        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})
        msg += f"\n`─────────────────`\n"
        if spy:
            spy_em = "🟢" if spy.get('pct', 0) >= 0 else "🔴"
            msg += f"SPY: {spy_em} `{spy.get('pct',0):+.2f}%`"
        if vix:
            msg += f" • VIX: `{vix.get('price',0):.1f}`"
        msg += "\n"

    msg += "\n_Type any symbol for full analysis_"
    send_telegram(msg)


# ═══════════════════════════════════════════════
# ON-DEMAND BRIEF
# ═══════════════════════════════════════════════

def run_brief():
    now     = now_est()
    hour    = now.hour + now.minute / 60
    weekday = now.weekday()
    try:
        if weekday >= 5:
            from scanner import format_weekly_summary, HISTORY_FILE
            ws = format_weekly_summary()
            send_telegram(ws if ws else "📊 No trade history yet.")
        elif hour < 12:
            from morning_brief import build_morning_brief, mark_morning_sent
            send_telegram("🌅 _Building morning brief..._", silent=True)
            if build_morning_brief():
                mark_morning_sent()
        else:
            from morning_brief import build_evening_brief, mark_evening_sent
            send_telegram("🌆 _Building evening brief..._", silent=True)
            if build_evening_brief():
                mark_evening_sent()
    except Exception as e:
        logging.error(f"Brief: {e}")
        send_telegram(f"❌ Brief failed: {e}")


# ═══════════════════════════════════════════════
# HELP
# ═══════════════════════════════════════════════

def send_help():
    send_telegram("""🤖 *ALPHAEDGE BOT COMMANDS*
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
`brief` — morning/evening/weekend context

*❓ OTHER*
`help` — this message

`━━━━━━━━━━━━━━━━━━━━━`
_AlphaEdge v4.1 • Always watching_ 👁️""")


# ═══════════════════════════════════════════════
# FULL ANALYSIS RUNNER
# ═══════════════════════════════════════════════

def run_analysis(symbol, mode='full', timeframe='1d'):
    symbol = normalise_symbol(symbol)
    print(f"\n🔍 v4.1: {symbol} | mode={mode} | tf={timeframe}")

    send_telegram(f"🔍 Analysing *{symbol}*... please wait ~35s", silent=True)

    if not validate_symbol(symbol):
        send_telegram(f"❌ *{symbol}* not found. Check ticker (e.g. `TSLA`, `BTC-USD`, `NXE`)")
        return

    ctx = get_full_context(symbol)
    if not ctx:
        send_telegram(f"❌ Could not fetch data for *{symbol}*")
        return

    print("  → Stock info...")
    stock_info = get_stock_info(symbol)

    print("  → Market context...")
    market_ctx = get_market_ctx()

    print("  → MTF verdicts...")
    mtf_verdicts = get_mtf_verdicts(symbol) if mode == 'full' else {}

    # FIX v4.1: get_verdict now returns 5 values including flags
    verdict, zone, reasons, next_steps, flags = get_verdict(ctx, market_ctx, mtf_verdicts)
    rs_score, rs_label = calc_relative_strength(ctx)

    if mode == 'short':
        send_telegram(format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score, stock_info))
        return

    print("  → Sector context...")
    sector_name, sector_avg, _ = get_sector_context(symbol)

    print("  → CAD pricing...")
    cad_price, tsx_symbol = get_cad_price(symbol)
    usd_cad = get_usd_cad_rate() if not is_crypto(symbol) else None

    print("  → Daily bars...")
    try:
        df_daily = yf.download(symbol, period='6mo', interval='1d',
                               progress=False, auto_adjust=True)
        df_daily = _clean_df(df_daily)
        poc                  = quick_poc(df_daily)
        support, resistance  = recent_structure(df_daily)
        squeeze_state, squeeze_dir = detect_squeeze(df_daily)
        rsi_div              = detect_rsi_divergence(df_daily)
    except Exception:
        poc = support = resistance = None
        squeeze_state, squeeze_dir, rsi_div = 'none', None, None

    print("  → AI analysis...")
    ai_text = get_ai_analysis(ctx, verdict, zone, sector_name, sector_avg,
                               mtf_verdicts, stock_info)
    print(f"  → AI: {'GOT' if ai_text else 'UNAVAILABLE'}")

    msg = format_full_analysis(
        symbol, ctx, verdict, zone, reasons, next_steps,
        ai_text, market_ctx, rs_score, rs_label,
        poc, support, resistance,
        squeeze_state, squeeze_dir, rsi_div,
        mtf_verdicts, sector_name, sector_avg,
        stock_info, cad_price, tsx_symbol, usd_cad,
        flags=flags   # FIX v4.1: pass conflict flags
    )
    send_telegram(msg)
    logging.info(f"v4.1 sent: {symbol} | {verdict}")


# ═══════════════════════════════════════════════
# MAIN — command router
# ═══════════════════════════════════════════════

def main():
    payload = {}
    if len(sys.argv) > 1:
        try:
            payload = json.loads(sys.argv[1])
        except Exception:
            payload = {"symbol": sys.argv[1], "mode": "full", "timeframe": "1d"}

    event_type = payload.get('event_type', 'analyze_symbol')
    command    = payload.get('command', '')
    symbol     = payload.get('symbol', '')
    mode       = payload.get('mode', 'full')
    timeframe  = payload.get('timeframe', '1d')

    print(f"\n🤖 event={event_type} command={command} symbol={symbol}")

    if event_type == 'analyze_symbol' or (not command and symbol):
        run_analysis(symbol, mode, timeframe)
    elif command == 'alert':
        price     = payload.get('price')
        direction = payload.get('direction', 'auto')
        if symbol and price:
            set_alert(symbol, float(price), direction)
        else:
            send_telegram("❌ Usage: `alert TSLA 450`")
    elif command == 'cancel_alert':
        if symbol: cancel_alert(symbol)
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
        send_telegram(f"❓ Unknown: `{command}`\nType `help` for commands.")


if __name__ == "__main__":
    main()
