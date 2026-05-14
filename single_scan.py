"""
ALPHAEDGE SINGLE SCAN v4.2
═══════════════════════════════════════════════════════════════
v4.2 FIXES vs v4.0 (doc4 base):
• get_verdict() — EXTENDED case now catches RSI >= 70 OR stretch >= 30%
  (was missing — RSI 74 was falling through to WATCH "Building Momentum")
• get_verdict() — reason text built dynamically so RSI label in verdict
  matches technicals section (no more "overbought" vs "bullish" conflict)
• get_verdict() — new PARABOLIC EXTENSION case (stretch >= 50% + RSI >= 70)
• get_verdict() — MTF RSI extreme detection (any TF >= 85 adds warning)
• get_verdict() — stretch_pct gates BUY/MOMENTUM cases to prevent chasing
• get_verdict() — computed re-entry price levels in every next_steps
• get_verdict() — earnings override expanded to include EXTENDED verdict
• get_mtf_verdicts() — rsi_tag field added (EXTREME/Overbought/Oversold)
• quick_poc() — price-anchored window (+-30% of current price)
  prevents stale histogram on parabolic movers
• build_reentry_table() — analyst target row removed (was duplicating
  the FUNDAMENTALS section)
• build_reentry_table() — WATCH case fixed (was showing MOMENTUM language)
• build_tag_pills() — RSI threshold corrected (>= 70 = overbought)
• format_full_analysis() — MTF RSI tag displayed inline
• format_full_analysis() — analyst flag shown when stock > consensus
• format_full_analysis() — 52W corporate action warning in price grid
• format_full_analysis() — stretch warning consistent with verdict
• version footer bumped to v7.2
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
    if vol_ratio >= 2.0:  return f"{vol_ratio:.1f}x avg 🔥 Unusually high"
    if vol_ratio >= 1.5:  return f"{vol_ratio:.1f}x avg - Above average"
    if vol_ratio >= 0.8:  return f"{vol_ratio:.1f}x avg - Normal"
    return f"{vol_ratio:.1f}x avg - Below average (weak move)"

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

def fmt_mcap(val):
    if not val:
        return None
    if val >= 1e12:  return f"${val/1e12:.1f}T"
    if val >= 1e9:   return f"${val/1e9:.1f}B"
    if val >= 1e6:   return f"${val/1e6:.1f}M"
    return f"${val:.0f}"


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
        if quote_type == 'EQUITY':                   asset_type = 'Stock'
        elif quote_type == 'ETF':                    asset_type = 'ETF'
        elif quote_type == 'MUTUALFUND':             asset_type = 'Fund'
        elif quote_type in ('FUTURE', 'COMMODITY'):  asset_type = 'Futures'
        else:                                        asset_type = quote_type or 'Stock'

        return {
            'sector':        info.get('sector', SYMBOL_TO_SECTOR.get(symbol, 'Unknown')),
            'industry':      info.get('industry', ''),
            'exchange':      info.get('exchange', ''),
            'asset_type':    asset_type,
            'currency':      info.get('currency', 'USD'),
            'short_name':    info.get('shortName', symbol),
            'target_mean':   info.get('targetMeanPrice'),
            'target_high':   info.get('targetHighPrice'),
            'target_low':    info.get('targetLowPrice'),
            'analyst_count': info.get('numberOfAnalystOpinions', 0),
            'rec_key':       info.get('recommendationKey', '').replace('_', ' ').title(),
            'short_pct':     info.get('shortPercentOfFloat'),
            'inst_pct':      info.get('institutionsPercentHeld'),
            'beta':          info.get('beta'),
            'pe_ratio':      info.get('trailingPE'),
            'market_cap':    info.get('marketCap'),
        }
    except Exception as e:
        logging.debug(f"Stock info {symbol}: {e}")
        return {
            'sector':     SYMBOL_TO_SECTOR.get(symbol, 'Unknown'),
            'asset_type': 'Stock',
            'currency':   'USD',
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
                if cad_price > 0.50:
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
            new_sar   = prev_sar + prev_af * (prev_ep - prev_sar)

            if prev_bull:
                new_sar = min(new_sar, low[i - 1], low[max(0, i - 2)])
                if low[i] < new_sar:
                    bull[i] = False; sar[i] = prev_ep; ep[i] = low[i]; af[i] = af_start
                else:
                    bull[i] = True; sar[i] = new_sar
                    if high[i] > prev_ep:
                        ep[i] = high[i]; af[i] = min(prev_af + af_step, af_max)
                    else:
                        ep[i] = prev_ep; af[i] = prev_af
            else:
                new_sar = max(new_sar, high[i - 1], high[max(0, i - 2)])
                if high[i] > new_sar:
                    bull[i] = True; sar[i] = prev_ep; ep[i] = high[i]; af[i] = af_start
                else:
                    bull[i] = False; sar[i] = new_sar
                    if low[i] < prev_ep:
                        ep[i] = low[i]; af[i] = min(prev_af + af_step, af_max)
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
# SQUEEZE + DIVERGENCE
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
# MTF VERDICTS — RSI + ADX + SAR with extreme tags
# ═══════════════════════════════════════════════

def get_mtf_verdicts(symbol):
    """
    Returns dict of {label: {trend, rsi, rsi_tag, adx, sar_bull, adx_sar}}
    for Daily / Weekly / Monthly.
    rsi_tag shows EXTREME / Overbought / Oversold inline in alert.
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

            c       = float(df['Close'].iloc[-1])
            e50     = float(ema(df['Close'], min(50,  len(df))).iloc[-1])
            e200    = float(ema(df['Close'], min(200, len(df))).iloc[-1])
            rsi_val = float(pine_rsi(df['Close'], 14).iloc[-1])

            # RSI extreme tag
            if rsi_val >= 90:    rsi_tag = "EXTREME"
            elif rsi_val >= 80:  rsi_tag = "Overbought"
            elif rsi_val <= 20:  rsi_tag = "EXTREME"
            elif rsi_val <= 30:  rsi_tag = "Oversold"
            else:                rsi_tag = ""

            adx_series, plus_di, minus_di = calc_adx(df, 14)
            adx_val   = float(adx_series.iloc[-1]) if adx_series is not None else 0
            plus_val  = float(plus_di.iloc[-1])    if plus_di   is not None else 0
            minus_val = float(minus_di.iloc[-1])   if minus_di  is not None else 0

            sar_bull_series, _ = calc_parabolic_sar(df)
            sar_bull = bool(sar_bull_series.iloc[-1]) if sar_bull_series is not None else None

            if adx_val >= 25 and sar_bull is True and plus_val > minus_val:
                adx_sar = "Trend BUY"
            elif adx_val >= 25 and sar_bull is False and minus_val > plus_val:
                adx_sar = "Trend SELL"
            elif adx_val < 20:
                adx_sar = "Ranging"
            else:
                adx_sar = "Mixed"

            if c > e50 > e200:   trend = "Strong Bull"
            elif c > e200:       trend = "Bull"
            elif c < e50 < e200: trend = "Strong Bear"
            elif c < e200:       trend = "Bear"
            else:                trend = "Mixed"

            results[label] = {
                'trend':    trend,
                'rsi':      round(rsi_val, 1),
                'rsi_tag':  rsi_tag,
                'adx':      round(adx_val, 1),
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
# POC — price-anchored window
# ═══════════════════════════════════════════════

def quick_poc(df_daily):
    """
    Compute POC using only bars within +/-30% of current price.
    Prevents stale low-price history dominating on parabolic movers.
    Returns None if fewer than 5 qualifying bars.
    """
    try:
        price_now = float(df_daily['Close'].iloc[-1])
        lo_bound  = price_now * 0.70
        hi_bound  = price_now * 1.30
        mask      = (df_daily['High'] >= lo_bound) & (df_daily['Low'] <= hi_bound)
        recent    = df_daily[mask].iloc[-60:]

        if len(recent) < 5:
            return None

        low  = float(recent['Low'].min())
        high = float(recent['High'].max())
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
                overlap = max(0, min(bar_high, bin_edges[b + 1]) - max(bar_low, bin_edges[b]))
                if overlap > 0:
                    vol_at_price[b] += bar_vol * (overlap / bar_range)

        if vol_at_price.sum() == 0:
            return None

        poc_idx = int(np.argmax(vol_at_price))
        poc     = round((bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2, 4)

        # Sanity check: POC must be within 40% of current price
        if abs(poc - price_now) / price_now > 0.40:
            return None

        return poc
    except Exception:
        return None


def recent_structure(df_daily):
    try:
        recent = df_daily.iloc[-20:]
        return round(float(recent['Low'].min()), 2), round(float(recent['High'].max()), 2)
    except Exception:
        return None, None


# ═══════════════════════════════════════════════
# VERDICT ENGINE v4.2
# ═══════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None, mtf_verdicts=None):
    c         = ctx
    rsi       = c['rsi']
    trend     = c['trend']
    drop      = c['day_change_pct']
    from_ath  = c['ath_pct']
    range_pos = c['range_pos']
    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']

    # EMA50 stretch — pre-computed, used in multiple cases
    stretch_pct = (c['current'] - c['ema50']) / c['ema50'] * 100 if c['ema50'] > 0 else 0

    reasons    = []
    next_steps = []
    verdict    = None
    zone       = None

    # MTF alignment + RSI extreme detection
    mtf_all_bull    = False
    mtf_rsi_extreme = False
    mtf_max_rsi     = 0
    if mtf_verdicts and len(mtf_verdicts) >= 2:
        bull_count      = sum(1 for v in mtf_verdicts.values() if 'Bull' in v.get('trend', ''))
        mtf_all_bull    = bull_count == len(mtf_verdicts)
        mtf_rsi_extreme = any(
            v.get('rsi', 50) >= 85 or v.get('rsi', 50) <= 15
            for v in mtf_verdicts.values()
        )
        mtf_max_rsi = max(v.get('rsi', 0) for v in mtf_verdicts.values())

    # ── 0. PARABOLIC single-day spike ──
    if abs(drop) >= 15:
        if drop > 0:
            verdict, zone = "PARABOLIC SPIKE", f"Single-day +{drop:.0f}%"
            reasons = [
                f"+{drop:.1f}% in one day — likely news/catalyst driven",
                "Parabolic spikes mean-revert — chasing is high risk",
            ]
            next_steps = [
                "DO NOT chase at current price",
                "Wait for 3-5 day consolidation",
                f"Re-entry zone: near EMA50 ${c['ema50']:.2f} on pullback",
                f"Invalidation: below EMA200 ${c['ema200']:.2f}",
            ]
        else:
            verdict, zone = "CRASH", f"Single-day {drop:.0f}%"
            reasons = [
                f"{drop:.1f}% single-day drop — likely news driven",
                "Wait for dust to settle — no entry today",
            ]
            next_steps = [
                "Do NOT catch today",
                "Wait minimum 3 days for stabilisation",
                f"Key level to watch: EMA200 ${c['ema200']:.2f}",
            ]
        return verdict, zone, reasons, next_steps

    # ── 0b. PARABOLIC EXTENSION — multi-day run, extreme EMA stretch ──
    if stretch_pct >= 50 and rsi >= 70:
        reentry_lo  = round(c['ema50'] * 0.98, 2)
        reentry_hi  = round(c['ema50'] * 1.05, 2)
        rsi_tf_str  = f" / {mtf_max_rsi:.0f} higher TF" if mtf_verdicts else ""
        verdict, zone = "PARABOLIC EXTENSION", f"{stretch_pct:.0f}% Above EMA50"
        reasons = [
            f"Price is {stretch_pct:.0f}% above EMA50 — statistically extreme",
            f"RSI {rsi:.0f} daily{rsi_tf_str} — overbought across timeframes",
        ]
        if mtf_rsi_extreme:
            reasons.append("Weekly/Monthly RSI at extremes — broad overextension")
        next_steps = [
            "DO NOT enter — mean reversion is the highest-probability outcome",
            f"Re-entry zone: ${reentry_lo} - ${reentry_hi} (near EMA50)",
            f"RSI trigger: wait for RSI to reset below 60 (currently {rsi:.0f})",
            f"Stop / invalidation: below EMA200 ${c['ema200']:.2f}",
            "If holding: trail stop tightly, consider taking 25-33% off",
        ]
        return verdict, zone, reasons, next_steps

    # ── 1. MOMENTUM — at/near ATH, not overextended ──
    if ("UPTREND" in trend and from_ath > -5 and above_50 and above_200
            and rsi < 75 and stretch_pct < 25):
        verdict, zone = "MOMENTUM", "AT ATH — Continuation"
        reasons = [
            f"At/near all-time high ({from_ath:+.1f}%)",
            "EMA stack fully bullish",
            f"RSI {rsi:.0f} — elevated but not overbought",
        ]
        if mtf_all_bull:
            reasons.append("All timeframes aligned bullish")
        next_steps = [
            f"Breakout entry: above ATH ${c['ath']:.2f} with volume",
            f"Pullback entry: dip to EMA50 ${c['ema50']:.2f} (ideal)",
            f"Stop: below EMA50 ${c['ema50']:.2f}",
            "Target: new ATH territory",
        ]

    # ── 2. EXTREMELY EXTENDED — multi-TF overbought ──
    elif (rsi >= 70 or stretch_pct >= 30) and above_50 and above_200 and mtf_rsi_extreme:
        reentry_lo = round(c['ema50'] * 0.98, 2)
        reentry_hi = round(c['ema50'] * 1.05, 2)
        verdict, zone = "EXTREMELY EXTENDED", "Multi-TF Overbought"
        reasons = [
            f"RSI {rsi:.0f} daily — overbought",
            f"Weekly/Monthly RSI also extreme (max: {mtf_max_rsi:.0f})",
            f"Price {stretch_pct:.0f}% above EMA50 — unsustainable",
        ]
        next_steps = [
            "DO NOT enter — pullback is highest-probability outcome",
            f"Re-entry zone: ${reentry_lo} - ${reentry_hi} (EMA50 area)",
            f"RSI trigger: wait for RSI below 60 (currently {rsi:.0f})",
            "If holding: consider trimming 33-50% of position",
        ]

    # ── 3. EXTENDED — RSI >= 70 OR stretch >= 30% ──
    # KEY FIX: this case was missing in v4.0.
    # RSI 74 was falling through to WATCH "Building Momentum" in old code.
    elif (rsi >= 70 or stretch_pct >= 30) and above_50 and above_200:
        reentry_lo = round(c['ema50'] * 0.98, 2)
        reentry_hi = round(c['ema50'] * 1.05, 2)
        verdict, zone = "EXTENDED", "Overbought — Wait for Pullback"
        # Dynamic reasons so RSI label matches the technicals section
        reasons = []
        if rsi >= 70:
            reasons.append(f"RSI {rsi:.0f} — overbought, momentum stretched")
        else:
            reasons.append(f"RSI {rsi:.0f} — elevated but not yet overbought")
        if stretch_pct >= 30:
            reasons.append(f"Price {stretch_pct:.0f}% above EMA50 — extended, poor R:R")
        elif stretch_pct >= 15:
            reasons.append(f"Price {stretch_pct:.0f}% above EMA50 — moderately stretched")
        reasons.append("Better setups come on pullbacks — not ideal entry now")
        next_steps = [
            f"Better entry: pullback to EMA50 zone ${reentry_lo} - ${reentry_hi}",
            f"RSI trigger: wait for RSI to cool below 60 (currently {rsi:.0f})",
            f"Stop if entering now: below EMA50 ${c['ema50']:.2f}",
            "If holding: trail stop, do not add to position",
        ]

    # ── 4. BUY ZONE — strong uptrend pullback ──
    elif "UPTREND" in trend and rsi < 55 and above_200 and stretch_pct < 20:
        verdict, zone = "BUY ZONE", "Pullback in Uptrend"
        reasons = [
            "Healthy pullback in a confirmed uptrend",
            f"RSI {rsi:.0f} — cooled down, room to run",
        ]
        if from_ath > -20:
            reasons.append("Near ATH — strong stock pulling back")
        if mtf_all_bull:
            reasons.append("All timeframes remain aligned bullish")
        next_steps = [
            f"Entry: ${c['current']:.2f} or lower — current level is reasonable",
            f"Target 1: ${c['ath']:.2f} (prior ATH)",
            "Target 2: new ATH breakout",
            f"Stop: below EMA200 ${c['ema200']:.2f}",
        ]

    # ── 5. BUY ZONE — EMA50 pullback ──
    elif "PULLBACK" in trend and rsi < 58 and stretch_pct < 15:
        verdict, zone = "BUY ZONE", "EMA50 Pullback"
        reasons = [
            "Pulling back toward EMA50 — uptrend structure intact",
            f"Above EMA200 ${c['ema200']:.2f} — structural support holds",
            f"RSI {rsi:.0f} — watch for bounce",
        ]
        next_steps = [
            f"Entry: near EMA50 ${c['ema50']:.2f} (ideal) or current ${c['current']:.2f}",
            f"Target: prior high ${c['high_52w']:.2f}",
            f"Stop: close below EMA200 ${c['ema200']:.2f}",
        ]

    # ── 6. DOWNTREND ──
    elif "DOWNTREND" in trend and not above_200:
        verdict, zone = "AVOID", "Falling Knife"
        reasons = ["Below EMA50 & EMA200 — confirmed downtrend, no base formed"]
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal yet")
        next_steps = [
            f"Do NOT enter — wait for close above EMA50 ${c['ema50']:.2f}",
            "Confirm EMA50 > EMA200 crossover before buying",
            f"EMA200 ${c['ema200']:.2f} is first target if reversal forms",
        ]

    # ── 7. NEAR 52W LOW ──
    elif c['pct_from_52w_low'] < 8 and drop < -3:
        verdict, zone = "CAUTION", "Breaking Down"
        reasons = ["Near 52W low — key support at risk of breaking"]
        next_steps = [
            f"Watch: holds ${c['low_52w']:.2f} (52W low support)",
            "Enter only after 2-3 days of stabilisation above the low",
            f"Stop if entering: below ${c['low_52w']:.2f}",
        ]

    # ── 8. TAKE PROFITS ──
    elif rsi > 73 and drop > 2:
        reentry_lo = round(c['ema50'] * 0.98, 2)
        reentry_hi = round(c['ema50'] * 1.05, 2)
        verdict, zone = "TAKE PROFITS", "Extended — Trim Here"
        reasons = [
            f"RSI {rsi:.0f} — overbought, momentum stretched",
            "Better risk/reward on a pullback",
        ]
        next_steps = [
            "Trim 25-33% of position at current levels",
            f"Re-entry zone: ${reentry_lo} - ${reentry_hi} (EMA50 area)",
            f"Trail stop: ${round(c['ema50'] * 0.97, 2):.2f} (3% below EMA50)",
        ]

    # ── 9. RECOVERING ──
    elif "RECOVERING" in trend:
        if rsi > 55 and drop > 0:
            verdict, zone = "WATCH", "Recovery Attempt"
            reasons = [
                "Reclaiming EMA50 — potential recovery in progress",
                f"Must clear EMA200 ${c['ema200']:.2f} to confirm",
            ]
            next_steps = [
                f"Trigger: confirmed close above EMA200 ${c['ema200']:.2f}",
                f"Entry on breakout: ${round(c['ema200'] * 1.01, 2):.2f} (1% above EMA200)",
                f"Stop: back below EMA50 ${c['ema50']:.2f}",
            ]
        else:
            verdict, zone = "HOLD", "Below EMA200"
            reasons = ["Below EMA200 — no structural confirmation of recovery"]
            next_steps = [
                f"Wait for: reclaim EMA200 ${c['ema200']:.2f}",
                "Then enter on confirmation with RSI > 50",
            ]

    # ── 10. MIXED ──
    elif "MIXED" in trend:
        if range_pos < 35 and rsi < 45:
            verdict, zone = "WATCH", "Potential Base"
            reasons = ["Lower 52W range — possible accumulation phase"]
            next_steps = [
                f"Trigger: RSI > 50 AND close above EMA50 ${c['ema50']:.2f}",
                f"Entry: ${round(c['ema50'] * 1.01, 2):.2f} on breakout",
                f"Stop: below recent low ${c['low_52w']:.2f}",
            ]
        else:
            verdict, zone = "NEUTRAL", "No Clear Edge"
            reasons = ["Mixed signals — no directional conviction"]
            next_steps = [
                f"Bull trigger: close above EMA50 ${c['ema50']:.2f} + RSI > 55",
                f"Bear trigger: close below EMA200 ${c['ema200']:.2f}",
                "No position until one of these confirms",
            ]

    # ── 11. DEFAULT ──
    else:
        if above_50 and above_200 and rsi > 55 and stretch_pct < 25:
            verdict, zone = "WATCH", "Building Momentum"
            reasons = [
                "Above both EMAs — structure is bullish",
                f"RSI {rsi:.0f} — momentum building, not yet extended",
            ]
            next_steps = [
                f"Ideal entry: pullback to EMA50 ${c['ema50']:.2f}",
                f"Breakout entry: above 52W high ${c['high_52w']:.2f} with volume",
                f"Stop: below EMA50 ${c['ema50']:.2f}",
            ]
        elif above_50 and above_200 and stretch_pct >= 25:
            reentry_lo = round(c['ema50'] * 0.98, 2)
            reentry_hi = round(c['ema50'] * 1.03, 2)
            verdict, zone = "EXTENDED", "Stretched — Wait"
            reasons = [
                f"RSI {rsi:.0f} + {stretch_pct:.0f}% above EMA50 — not ideal entry",
                "Risk/reward is poor at current levels",
            ]
            next_steps = [
                f"Wait for pullback to EMA50 ${reentry_lo} - ${reentry_hi}",
                "Do not chase at current levels",
            ]
        else:
            verdict, zone = "NEUTRAL", "No Clear Setup"
            reasons = ["No strong directional signal currently"]
            next_steps = [
                f"Bull trigger: above EMA50 ${c['ema50']:.2f} + RSI > 55",
                "No position until signal confirms",
            ]

    # ── MTF RSI extreme addendum ──
    if mtf_rsi_extreme and verdict not in (
        "PARABOLIC EXTENSION", "EXTREMELY EXTENDED",
        "PARABOLIC SPIKE", "CRASH"
    ):
        reasons.append(f"Higher-TF RSI extreme (max {mtf_max_rsi:.0f}) — elevated reversion risk")

    # ── Market override ──
    if market_ctx:
        vix     = market_ctx.get('^VIX', {}).get('price', 15)
        spy_pct = market_ctx.get('SPY',  {}).get('pct', 0)
        if vix > 25 and spy_pct < -1.5 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
            verdict = "WAIT — Market"
            reasons.insert(0, f"Market bleeding — VIX {vix:.0f}, SPY {spy_pct:.1f}% — defer entry")
            next_steps = [
                "Wait for market to stabilise (VIX < 20, SPY positive)",
                f"Re-entry zone when market calms: near EMA50 ${c['ema50']:.2f}",
            ]

    # ── Earnings override — includes EXTENDED ──
    if any(x in verdict for x in ["BUY", "MOMENTUM", "WATCH", "EXTENDED"]):
        _, days_until = get_earnings_date(c['symbol'])
        if days_until is not None and days_until <= EARNINGS_WARNING_DAYS:
            verdict = "WAIT — Earnings"
            zone    = f"Earnings in {days_until}d"
            reasons.insert(0, f"Earnings in {days_until} days — skip new entries before binary event")
            next_steps = [
                "Re-evaluate after earnings report",
                f"If bullish post-earnings: entry near EMA50 ${c['ema50']:.2f}",
            ]

    return verdict, zone, reasons, next_steps


# ═══════════════════════════════════════════════
# AI ANALYSIS
# ═══════════════════════════════════════════════

def get_ai_analysis(ctx, verdict, zone, sector_name, sector_avg,
                    mtf_verdicts, stock_info):
    from market_intel import GEMINI_API_KEY
    if not GEMINI_API_KEY:
        return None

    c           = ctx
    stretch_pct = (c['current'] - c['ema50']) / c['ema50'] * 100 if c['ema50'] > 0 else 0

    mtf_str = "\n".join([
        f"  {tf}: {v['trend']} | RSI {v['rsi']} | ADX {v['adx']} | {v['adx_sar']}"
        for tf, v in mtf_verdicts.items()
    ]) if mtf_verdicts else "  N/A"

    sector_str  = f"{sector_name}: {sector_avg:+.1f}% avg today" if sector_name and sector_avg else "Unknown"
    analyst_str = ""
    if stock_info.get('target_mean'):
        upside      = (stock_info['target_mean'] - c['current']) / c['current'] * 100
        analyst_str = (f"\nAnalyst target: ${stock_info['target_mean']:.2f} mean "
                       f"({upside:+.1f}% upside) — {stock_info.get('rec_key','')}")

    prompt = f"""You are a senior trading analyst. Analyze this setup in EXACTLY 4 lines (max 110 chars each).

SETUP: {c['symbol']} | {zone}
Price: ${c['current']:.2f} | Day: {c['day_change_pct']:+.1f}% | Vol: {c['vol_ratio']:.1f}x avg
Trend: {c['trend']} | RSI: {c['rsi']:.0f} | ATH: {c['ath_pct']:+.1f}% | EMA50 stretch: {stretch_pct:+.1f}%
EMA50: ${c['ema50']:.2f} | EMA200: ${c['ema200']:.2f} | 52W pos: {c['range_pos']:.0f}%

TIMEFRAMES:
{mtf_str}

SECTOR: {sector_str}{analyst_str}

Respond EXACTLY:
[Technical/sector/catalyst? Specific]
[Setup quality & R:R -- is it worth taking now?]
[Biggest invalidation risk -- specific level]
[STRONG BUY/BUY/HOLD/AVOID/WAIT] -- [one sharp sentence]

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
            print("  --> Gemini RATE LIMITED -- retrying in 15s")
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
            print(f"  --> Gemini ERROR {r.status_code}")
    except Exception as e:
        logging.error(f"AI: {e}")
    return None


# ═══════════════════════════════════════════════
# TAG PILLS
# ═══════════════════════════════════════════════

def build_tag_pills(verdict, ctx, rs_label, squeeze_state, rsi_div, stock_info):
    """
    Compact tag line shown under header. Max 4 pills.
    RSI threshold fixed: >= 70 = overbought (consistent with verdict).
    """
    tags = []
    c    = ctx
    rsi  = c['rsi']
    stretch_pct = (c['current'] - c['ema50']) / c['ema50'] * 100 if c['ema50'] > 0 else 0

    # Primary verdict tag
    if 'PARABOLIC' in verdict:            tags.append('Parabolic')
    elif 'CRASH' in verdict:              tags.append('Crash')
    elif 'EXTREMELY EXTENDED' in verdict: tags.append('Extremely Extended')
    elif 'MOMENTUM' in verdict:           tags.append('Momentum')
    elif 'BUY ZONE' in verdict:           tags.append('Buy Zone')
    elif 'EXTENDED' in verdict:           tags.append('Extended')
    elif 'TAKE PROFITS' in verdict:       tags.append('Take Profits')
    elif 'AVOID' in verdict:              tags.append('Avoid')
    elif 'CAUTION' in verdict:            tags.append('Caution')
    elif 'WAIT' in verdict:               tags.append('Wait')
    elif 'WATCH' in verdict:              tags.append('Watch')
    elif 'NEUTRAL' in verdict:            tags.append('Neutral')

    # RSI tag — threshold >= 70 matches verdict EXTENDED trigger
    if rsi >= 80:        tags.append('RSI Extreme')
    elif rsi >= 70:      tags.append('RSI Overbought')
    elif rsi <= 30:      tags.append('RSI Oversold')
    elif rsi >= 60:      tags.append('RSI Bullish')

    # Stretch tag (only if not already in primary verdict)
    if stretch_pct >= 50 and 'PARABOLIC' not in verdict:
        tags.append(f'{stretch_pct:.0f}% above EMA50')
    elif 30 <= stretch_pct < 50 and 'EXTENDED' not in verdict:
        tags.append(f'{stretch_pct:.0f}% above EMA50')

    # RS vs SPY
    if rs_label:
        if 'Strong Leader' in rs_label:   tags.append('Strong Leader')
        elif 'Outperform' in rs_label:    tags.append('Outperforming')
        elif 'Laggard' in rs_label:       tags.append('Laggard')
        elif 'Underperform' in rs_label:  tags.append('Underperforming')

    # Squeeze / divergence
    if squeeze_state == 'building':       tags.append('Squeeze Building')
    elif squeeze_state == 'fired':        tags.append('Squeeze Fired')
    if rsi_div == 'bullish':              tags.append('RSI Div Bullish')
    elif rsi_div == 'bearish':            tags.append('RSI Div Bearish')

    # High short interest
    short_pct = stock_info.get('short_pct')
    if short_pct and short_pct > 0.15:   tags.append(f'{short_pct*100:.0f}% Short')

    return ' | '.join(tags[:4])


# ═══════════════════════════════════════════════
# RE-ENTRY TABLE
# ═══════════════════════════════════════════════

def build_reentry_table(verdict, ctx, mtf_verdicts, stock_info):
    """
    Decision table with exact price levels.
    Analyst target removed — lives in FUNDAMENTALS to avoid duplication.
    WATCH case fixed — no longer shows MOMENTUM language.
    """
    c = ctx
    decimals = 4 if c['current'] < 10 else 2
    pf = f"{{:.{decimals}f}}"

    lines = []

    if any(x in verdict for x in ['PARABOLIC', 'EXTREMELY EXTENDED']):
        ema50_lo = round(c['ema50'] * 0.97, decimals)
        ema50_hi = round(c['ema50'] * 1.03, decimals)
        lines.append(f"{'DO NOT enter':16s} Mean reversion is highest-probability")
        lines.append(f"{'Re-entry zone':16s} ${pf.format(ema50_lo)} - ${pf.format(ema50_hi)} (EMA50)")
        lines.append(f"{'RSI trigger':16s} Wait for RSI below 60, then bounce")
        lines.append(f"{'Invalidation':16s} Below EMA200 ${pf.format(c['ema200'])}")

    elif 'EXTENDED' in verdict or 'TAKE PROFITS' in verdict:
        ema50_lo = round(c['ema50'] * 0.98, decimals)
        ema50_hi = round(c['ema50'] * 1.05, decimals)
        lines.append(f"{'Not ideal entry':16s} Poor R:R at current price — wait")
        lines.append(f"{'Better entry':16s} ${pf.format(ema50_lo)} - ${pf.format(ema50_hi)} (EMA50)")
        lines.append(f"{'RSI trigger':16s} Wait for RSI below 60 (now {c['rsi']:.0f})")
        lines.append(f"{'If holding':16s} Trail stop, do not add to position")

    elif 'MOMENTUM' in verdict:
        lines.append(f"{'Breakout entry':16s} Above ATH ${pf.format(c['ath'])} with volume")
        lines.append(f"{'Pullback entry':16s} Dip to EMA50 ${pf.format(c['ema50'])} (ideal)")
        lines.append(f"{'Target':16s} New ATH territory")
        lines.append(f"{'Stop':16s} Close below EMA50 ${pf.format(c['ema50'])}")

    elif 'BUY ZONE' in verdict:
        lines.append(f"{'Entry':16s} ${pf.format(c['current'])} or lower — good R:R here")
        lines.append(f"{'Target':16s} ATH ${pf.format(c['ath'])} ({c['ath_pct']:+.1f}% away)")
        lines.append(f"{'Stop':16s} Close below EMA200 ${pf.format(c['ema200'])}")
        lines.append(f"{'Add on dip':16s} EMA50 ${pf.format(c['ema50'])} is ideal add zone")

    elif 'WATCH' in verdict:
        # WATCH is not a buy — wait for confirmation
        pullback_lo = round(c['ema50'] * 0.99, decimals)
        pullback_hi = round(c['ema50'] * 1.03, decimals)
        lines.append(f"{'Not a buy yet':16s} Wait for confirmation before entering")
        lines.append(f"{'Best entry':16s} Pullback to ${pf.format(pullback_lo)} - ${pf.format(pullback_hi)}")
        lines.append(f"{'Breakout entry':16s} Above ${pf.format(c['high_52w'])} with volume")
        lines.append(f"{'Stop':16s} Close below EMA200 ${pf.format(c['ema200'])}")

    elif any(x in verdict for x in ['AVOID', 'CRASH']):
        lines.append(f"{'Do NOT enter':16s} Confirmed downtrend — no base formed")
        lines.append(f"{'Watch for':16s} Close above EMA50 ${pf.format(c['ema50'])}")
        lines.append(f"{'Confirm with':16s} RSI > 50 + volume > 1.5x")
        lines.append(f"{'Then target':16s} EMA200 ${pf.format(c['ema200'])}")

    else:
        # Neutral / Hold / Wait
        lines.append(f"{'Bull trigger':16s} Close above EMA50 ${pf.format(c['ema50'])}")
        lines.append(f"{'Bear trigger':16s} Close below EMA200 ${pf.format(c['ema200'])}")
        lines.append(f"{'RSI watch':16s} Above 55 for bull, below 45 for bear")
        lines.append(f"{'Best action':16s} Wait for clear directional break")

    msg = "*WHAT TO DO*\n`─────────────────────────`\n"
    for line in lines:
        msg += f"`{line}`\n"
    return msg


# ═══════════════════════════════════════════════
# PRICE CONTEXT GRID
# ═══════════════════════════════════════════════

def build_price_context_grid(ctx, cad_price, tsx_symbol, usd_cad):
    """Compact scannable price context block."""
    c = ctx
    decimals = 4 if c['current'] < 10 else 2
    pf = f"{{:.{decimals}f}}"

    vol_ratio = c['vol_ratio']
    if vol_ratio >= 2.0:   vol_str = f"{vol_ratio:.1f}x Unusual"
    elif vol_ratio >= 1.5: vol_str = f"{vol_ratio:.1f}x Above avg"
    elif vol_ratio >= 0.8: vol_str = f"{vol_ratio:.1f}x Normal"
    else:                  vol_str = f"{vol_ratio:.1f}x Weak"

    rp = c['range_pos']
    if rp >= 90:   rp_str = f"{rp:.0f}% — near top of range"
    elif rp >= 70: rp_str = f"{rp:.0f}% — upper range"
    elif rp >= 50: rp_str = f"{rp:.0f}% — mid range"
    elif rp >= 30: rp_str = f"{rp:.0f}% — lower range"
    else:          rp_str = f"{rp:.0f}% — near bottom"

    ath_str = f"{c['ath_pct']:+.1f}% — {ath_recency(c['ath_date'])}"

    msg  = "*PRICE CONTEXT*\n`─────────────────────────`\n"
    msg += f"`{'Volume':12s}` {vol_str}\n"
    msg += f"`{'52W Position':12s}` {rp_str}\n"
    msg += f"`{'From ATH':12s}` {ath_str}\n"
    msg += f"`{'52W Range':12s}` ${pf.format(c['low_52w'])} to ${pf.format(c['high_52w'])}\n"

    # Corporate action / abnormal range warning
    if c.get('pct_from_52w_low', 0) > 500:
        msg += f"52W low ${pf.format(c['low_52w'])} suggests split/spin-off — range unreliable\n"

    if cad_price and tsx_symbol:
        msg += f"`{'TSX (CAD)':12s}` {tsx_symbol} = ${cad_price:.2f} CAD\n"
    elif usd_cad and not is_crypto(c['symbol']):
        implied = round(c['current'] * usd_cad, 2)
        msg += f"`{'CAD equiv':12s}` ${implied:.2f} (x{usd_cad:.4f}) — no TSX listing\n"

    return msg


# ═══════════════════════════════════════════════
# FORMAT FULL ANALYSIS v4.2
# Layout:
#   1. Header (ticker, price, change, type)
#   2. Tag pills
#   3. Verdict + AI one-liner + reasons
#   4. Price + POC
#   5. What To Do (re-entry table)
#   6. Price context grid
#   7. Timeframe alignment
#   8. Technicals
#   9. Sector + RS
#  10. Fundamentals (analyst targets with overextension flag)
#  11. Earnings
#  12. Market
#  13. Full AI analysis
# ═══════════════════════════════════════════════

def format_full_analysis(symbol, ctx, verdict, zone, reasons, next_steps,
                          ai_text, market_ctx, rs_score, rs_label,
                          poc, support, resistance,
                          squeeze_state, squeeze_dir, rsi_div,
                          mtf_verdicts, sector_name, sector_avg,
                          stock_info, cad_price, tsx_symbol, usd_cad):
    em  = SYMBOL_EMOJI.get(symbol, '')
    c   = ctx
    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%a %b %d  %I:%M %p {tz}')
    decimals = 4 if c['current'] < 10 else 2
    pf       = f"{{:.{decimals}f}}"
    drop     = c['day_change_pct']
    drop_em  = "+" if drop >= 0 else ""
    sign     = "+" if drop >= 0 else ""

    stretch_pct = (c['current'] - c['ema50']) / c['ema50'] * 100 if c['ema50'] > 0 else 0

    asset_type = stock_info.get('asset_type', 'Stock')
    sector_h   = stock_info.get('sector', SYMBOL_TO_SECTOR.get(symbol, ''))
    exchange   = stock_info.get('exchange', '')
    mcap       = fmt_mcap(stock_info.get('market_cap'))
    beta_val   = stock_info.get('beta')

    # ─────────────────────────────────────────────
    # 1  HEADER
    # ─────────────────────────────────────────────
    msg  = f"*{symbol}* {em}  `${pf.format(c['current'])}`  {sign}{drop:.2f}%\n"
    type_parts = [asset_type]
    if sector_h:  type_parts.append(sector_h)
    if exchange:  type_parts.append(exchange)
    if mcap:      type_parts.append(mcap)
    msg += f"_{' · '.join(type_parts)}_\n"
    msg += f"_{ts}_\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # ─────────────────────────────────────────────
    # 2  TAG PILLS
    # ─────────────────────────────────────────────
    tags = build_tag_pills(verdict, ctx, rs_label, squeeze_state, rsi_div, stock_info)
    if tags:
        msg += f"{tags}\n\n"

    # ─────────────────────────────────────────────
    # 3  VERDICT + AI ONE-LINER + REASONS
    # ─────────────────────────────────────────────
    msg += f"*{verdict}*\n"
    msg += f"_{zone}_\n"

    if ai_text:
        lines   = ai_text.strip().split('\n')
        summary = next((l for l in lines if 'WAIT' in l.upper() or
                        'BUY' in l.upper() or 'HOLD' in l.upper() or
                        'AVOID' in l.upper()), lines[-1] if lines else None)
        if summary:
            msg += f"{summary}\n"

    msg += "\n"
    for r in reasons[:3]:
        msg += f"  - {r}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # ─────────────────────────────────────────────
    # 4  PRICE + POC
    # ─────────────────────────────────────────────
    msg += f"*PRICE*\n`─────────────────────────`\n"
    msg += f"Live:   `${pf.format(c['current'])}`  {sign}{drop:.2f}% today\n"
    msg += f"Range:  L `${pf.format(c['today_low'])}` to H `${pf.format(c['today_high'])}`\n"
    msg += f"Volume: {volume_label(c['vol_ratio'])}\n"
    if poc:
        diff_pct = (c['current'] - poc) / poc * 100
        if abs(diff_pct) < 0.5:
            msg += f"POC:    AT `${pf.format(poc)}` — volume magnet\n"
        elif c['current'] > poc:
            msg += f"POC:    Above `${pf.format(poc)}` ({diff_pct:+.1f}%) — buyers in control\n"
        else:
            msg += f"POC:    Below `${pf.format(poc)}` ({diff_pct:+.1f}%) — sellers in control\n"
    msg += "\n"

    # ─────────────────────────────────────────────
    # 5  WHAT TO DO
    # ─────────────────────────────────────────────
    msg += build_reentry_table(verdict, ctx, mtf_verdicts, stock_info)
    msg += "\n"

    # ─────────────────────────────────────────────
    # 6  PRICE CONTEXT GRID
    # ─────────────────────────────────────────────
    msg += build_price_context_grid(ctx, cad_price, tsx_symbol, usd_cad)
    msg += "\n"

    # ─────────────────────────────────────────────
    # 7  TIMEFRAME ALIGNMENT
    # ─────────────────────────────────────────────
    if mtf_verdicts:
        msg += f"*TIMEFRAMES*\n`─────────────────────────`\n"
        for tf_label, v in mtf_verdicts.items():
            sar_em  = "SAR OK" if v.get('sar_bull') else "SAR X" if v.get('sar_bull') is False else "SAR -"
            adx_em  = "ADX strong" if v['adx'] >= 25 else "ADX weak" if v['adx'] < 20 else "ADX ok"
            rsi_str = f"RSI {v['rsi']}"
            if v.get('rsi_tag'):
                rsi_str += f" ({v['rsi_tag']})"
            msg += (f"`{tf_label:7s}` {v['trend']}\n"
                    f"         {rsi_str}  {adx_em} {v['adx']:.0f}  {sar_em}  {v['adx_sar']}\n")
        msg += "\n"

    # ─────────────────────────────────────────────
    # 8  TECHNICALS
    # ─────────────────────────────────────────────
    msg += f"*TECHNICALS*\n`─────────────────────────`\n"
    msg += f"{c['trend']}\n"

    # RSI label consistent with verdict (>= 70 = overbought)
    if c['rsi'] < 30:       rsi_tag = "(oversold)"
    elif c['rsi'] >= 70:    rsi_tag = "(overbought)"
    elif c['rsi'] > 60:     rsi_tag = "(bullish)"
    else:                   rsi_tag = "(neutral)"
    msg += f"RSI:    `{c['rsi']:.0f}` {rsi_tag}\n"

    # Stretch warning consistent with verdict action
    if stretch_pct >= 50:    stretch_warn = " -- EXTREME, do not chase"
    elif stretch_pct >= 30:  stretch_warn = " -- extended, wait for pullback"
    elif stretch_pct >= 15:  stretch_warn = " -- moderately stretched"
    elif stretch_pct <= -15: stretch_warn = " -- deeply oversold"
    else:                    stretch_warn = ""
    msg += f"EMA50:  `${pf.format(c['ema50'])}` ({stretch_pct:+.1f}%){stretch_warn}\n"
    msg += f"EMA200: `${pf.format(c['ema200'])}`\n"

    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if above_50 and above_200:          msg += "Above EMA50 & EMA200\n"
    elif above_200 and not above_50:    msg += "Below EMA50, above EMA200\n"
    elif not above_200 and above_50:    msg += "Above EMA50, below EMA200\n"
    else:                               msg += "Below both EMAs\n"

    if beta_val:
        beta_desc = "low vol" if beta_val < 0.8 else "high vol" if beta_val > 1.5 else "market vol"
        msg += f"Beta:   `{beta_val:.2f}` ({beta_desc})\n"
    msg += "\n"

    # ─────────────────────────────────────────────
    # 9  SECTOR + RELATIVE STRENGTH
    # ─────────────────────────────────────────────
    if sector_name and sector_avg is not None:
        sec_sign = "+" if sector_avg >= 0 else ""
        sym_vs   = drop - sector_avg
        msg += f"*SECTOR ({sector_name})*\n`─────────────────────────`\n"
        msg += f"Sector avg: `{sec_sign}{sector_avg:.2f}%` today\n"
        if sym_vs > 1.5:
            msg += f"Outperforming sector by `{sym_vs:+.1f}%`\n"
        elif sym_vs < -1.5:
            msg += f"Underperforming sector by `{sym_vs:+.1f}%`\n"
        else:
            msg += f"In line with sector\n"
        msg += "\n"

    if rs_score is not None:
        rs_sign = "+" if rs_score >= 0 else ""
        msg += f"*RS vs SPY (5d):* {rs_label}  `{rs_sign}{rs_score}%`\n\n"

    # ─────────────────────────────────────────────
    # 10  FUNDAMENTALS — analyst targets with flag + short + inst
    # ─────────────────────────────────────────────
    short_pct   = stock_info.get('short_pct')
    inst_pct    = stock_info.get('inst_pct')
    target_mean = stock_info.get('target_mean')
    target_high = stock_info.get('target_high')
    target_low  = stock_info.get('target_low')
    rec_key     = stock_info.get('rec_key', '')
    analyst_n   = stock_info.get('analyst_count', 0)
    pe_ratio    = stock_info.get('pe_ratio')

    has_fundamentals = (target_mean or short_pct or inst_pct or pe_ratio) and not is_crypto(symbol)
    if has_fundamentals:
        msg += f"*FUNDAMENTALS*\n`─────────────────────────`\n"

        if target_mean:
            upside    = (target_mean - c['current']) / c['current'] * 100
            up_sign   = "+" if upside >= 0 else ""
            msg += f"Analyst target: `${target_mean:.2f}`  {up_sign}{upside:.1f}%"
            if analyst_n:
                msg += f" ({analyst_n} analysts)"
            msg += "\n"
            if target_high and target_low:
                msg += f"Range: `${target_low:.2f}` to `${target_high:.2f}`\n"
            if rec_key:
                msg += f"Rating: *{rec_key}*\n"
            # Flag when stock has run past analyst consensus
            if upside < -15:
                msg += (f"STOCK IS {abs(upside):.0f}% ABOVE analyst consensus\n"
                        f"Analysts haven't upgraded — stock may be overextended\n")
            elif upside < -5:
                msg += f"Stock above analyst consensus — limited upside per analysts\n"

        if pe_ratio:
            pe_tag = " (elevated)" if pe_ratio > 40 else ""
            msg += f"P/E: `{pe_ratio:.0f}`{pe_tag}\n"

        if short_pct:
            short_tag = "High" if short_pct > 0.15 else "Normal"
            msg += f"Short int: `{short_pct*100:.1f}%`  {short_tag}"
            if short_pct > 0.15:
                msg += " — squeeze potential on breakout"
            msg += "\n"

        if inst_pct:
            inst_tag = "Smart money heavy" if inst_pct > 0.7 else "Moderate"
            msg += f"Institutional: `{inst_pct*100:.0f}%` — {inst_tag}\n"

        msg += "\n"

    # ─────────────────────────────────────────────
    # 11  EARNINGS
    # ─────────────────────────────────────────────
    earnings_date, days_until = get_earnings_date(symbol)
    warn = format_earnings_warning(symbol, earnings_date, days_until)
    if warn:
        msg += f"*EARNINGS*\n`─────────────────────────`\n{warn}\n\n"

    # ─────────────────────────────────────────────
    # 12  MARKET
    # ─────────────────────────────────────────────
    if market_ctx:
        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})
        if spy or vix:
            msg += f"*MARKET*\n`─────────────────────────`\n"
            if spy:
                spy_pct = spy.get('pct', 0)
                spy_sign = "+" if spy_pct >= 0 else ""
                msg += f"SPY: `{spy_sign}{spy_pct:.2f}%`"
            if vix:
                vix_val = vix.get('price', 0)
                vix_tag = "High" if vix_val > 25 else "Elevated" if vix_val > 18 else "Calm"
                msg += f"  VIX: `{vix_val:.1f}` ({vix_tag})"
            msg += "\n\n"

    # ─────────────────────────────────────────────
    # 13  FULL AI ANALYSIS
    # ─────────────────────────────────────────────
    if ai_text:
        msg += f"*AI ANALYSIS*\n`─────────────────────────`\n{ai_text}\n\n"

    msg += f"`━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_AlphaEdge v7.2 · On-demand_"
    return msg


def format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score, stock_info):
    em       = SYMBOL_EMOJI.get(symbol, '')
    c        = ctx
    drop     = c['day_change_pct']
    sign     = "+" if drop >= 0 else ""
    decimals = 4 if c['current'] < 10 else 2
    pf       = f"{{:.{decimals}f}}"
    rs_str   = f"  RS {rs_label}" if rs_label else ""
    sector_h = stock_info.get('sector', '')
    sec_str  = f"  {sector_h}" if sector_h else ""
    msg  = f"{em} *{symbol}*  `${pf.format(c['current'])}`  ({sign}{drop:.1f}%){sec_str}\n"
    msg += f"{verdict} — {zone}\n"
    msg += f"RSI `{c['rsi']:.0f}`  {c['trend']}{rs_str}"
    return msg


# ═══════════════════════════════════════════════
# PRICE ALERT SYSTEM — Enhanced Layout v7.2
# ═══════════════════════════════════════════════

def alert_header(emoji, title, border='━━━━━━━━━━━━━━━━━━━━━'):
    return f"{emoji} *{title}*\n`{border}`\n\n"


def load_alerts():
    return load_json(ALERTS_FILE, {})

def save_alerts(alerts):
    save_json(ALERTS_FILE, alerts)


def set_alert(symbol, target_price, direction='auto'):
    alerts = load_alerts()

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
        'symbol':              symbol,
        'target':              target_price,
        'direction':           direction,
        'set_at':              now_est().isoformat(),
        'expires_at':          (now_est() + timedelta(days=30)).isoformat(),
        'warning_sent':        False,
        'expiry_warning_sent': False,
        'triggered':           False,
    }

    save_alerts(alerts)

    em = SYMBOL_EMOJI.get(symbol, '📈')

    dir_str = "breaks above" if direction == 'above' else "falls below"
    dir_em  = "📈" if direction == 'above' else "📉"

    warn_price = (
        target_price * 0.98
        if direction == 'above'
        else target_price * 1.02
    )

    cur_str = f"${current:.2f}" if current else "N/A"

    msg  = alert_header("🔔", "PRICE ALERT CREATED")
    msg += f"{em} *{symbol}*\n"
    msg += f"_30-day active alert_\n\n"

    msg += f"🎯 *Target*\n"
    msg += f"`${target_price:.2f}`\n\n"

    msg += f"{dir_em} *Condition*\n"
    msg += f"{dir_str.title()}\n\n"

    msg += f"💵 *Current Price*\n"
    msg += f"`{cur_str}`\n\n"

    msg += f"⚠️ *Early Warning*\n"
    msg += f"`${warn_price:.2f}`\n\n"

    msg += f"⏳ Expires in *30 days*\n\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_AlphaEdge will notify before trigger._"

    send_telegram(msg)


def cancel_alert(symbol):
    alerts = load_alerts()

    removed = []

    for key in list(alerts.keys()):
        if alerts[key]['symbol'] == symbol:
            removed.append(alerts[key]['target'])
            del alerts[key]

    save_alerts(alerts)

    em = SYMBOL_EMOJI.get(symbol, '📈')

    if removed:

        targets = "\n".join(
            [f"• `${t:.2f}`" for t in sorted(removed)]
        )

        msg  = alert_header("🗑️", "ALERT CANCELLED", "═════════════════════")
        msg += f"{em} *{symbol}*\n\n"
        msg += f"🎯 *Removed Targets*\n"
        msg += f"{targets}\n\n"
        msg += "✅ No more active alerts for this symbol."

        send_telegram(msg)

    else:

        send_telegram(
            alert_header("📭", "NO ACTIVE ALERTS", "─────────────────────") +
            f"{em} *{symbol}*\n\n"
            f"No active alerts found."
        )


def list_alerts():
    alerts = load_alerts()

    active = {
        k: v for k, v in alerts.items()
        if not v.get('triggered')
    }

    if not active:
        send_telegram(
            alert_header("📭", "NO ACTIVE ALERTS", "─────────────────────") +
            "Set one with:\n"
            "`alert TSLA 450`"
        )
        return

    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%a %b %d · %I:%M %p {tz}')

    msg  = alert_header("📋", f"ACTIVE ALERTS ({len(active)})")
    msg += f"_{ts}_\n\n"

    for key, a in sorted(active.items(), key=lambda x: x[1]['symbol']):

        em = SYMBOL_EMOJI.get(a['symbol'], '📈')

        dir_str = "Above" if a['direction'] == 'above' else "Below"
        dir_em  = "📈" if a['direction'] == 'above' else "📉"

        expires = datetime.fromisoformat(a['expires_at'])
        days_left = (expires - now_est()).days

        warn = (
            a['target'] * 0.98
            if a['direction'] == 'above'
            else a['target'] * 1.02
        )

        msg += (
            f"{em} *{a['symbol']}*\n"
            f"{dir_em} {dir_str}: `${a['target']:.2f}`\n"
            f"⚠️ Warning: `${warn:.2f}`\n"
            f"⏳ {days_left}d remaining\n\n"
        )

    msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += "_AlphaEdge v7.2_"

    send_telegram(msg)


def check_alerts():
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

        warn_price = (
            target * 0.98
            if direction == 'above'
            else target * 1.02
        )

        em = SYMBOL_EMOJI.get(symbol, '📈')

        expires = datetime.fromisoformat(a['expires_at'])

        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=EST)

        days_left = (expires - now).days

        # ─────────────────────────────────────
        # Expiring Tomorrow
        # ─────────────────────────────────────
        if days_left <= 1 and not a.get('expiry_warning_sent'):

            send_telegram(
                alert_header("⏳", "ALERT EXPIRING SOON", "─────────────────────") +
                f"{em} *{symbol}*\n\n"
                f"🎯 Target: `${target:.2f}`\n\n"
                f"This alert expires *tomorrow*.\n\n"
                f"🔁 Reset with:\n"
                f"`alert {symbol} {target}`"
            )

            a['expiry_warning_sent'] = True
            changed = True

        # ─────────────────────────────────────
        # Expired
        # ─────────────────────────────────────
        if now > expires:

            send_telegram(
                alert_header("🕒", "ALERT EXPIRED", "═════════════════════") +
                f"{em} *{symbol}*\n\n"
                f"🎯 Target: `${target:.2f}`\n\n"
                f"No trigger within 30 days.\n\n"
                f"❌ Alert removed."
            )

            del alerts[key]
            changed = True
            continue

        try:
            df = yf.download(symbol, period='1d', interval='5m',
                             progress=False, auto_adjust=True)

            if df.empty:
                continue

            df = _clean_df(df)

            current = float(df['Close'].iloc[-1])

        except Exception:
            continue

        # ─────────────────────────────────────
        # Triggered
        # ─────────────────────────────────────
        if (
            (direction == 'above' and current >= target) or
            (direction == 'below' and current <= target)
        ):

            move_pct = ((current - target) / target) * 100

            move_em = "🚀" if move_pct > 0 else "📉"

            msg  = alert_header("🚨", "ALERT TRIGGERED", "━━━━━━━━━━━━━━━━━━━━━")
            msg += f"{em} *{symbol}*\n\n"

            msg += f"🎯 *Target Hit*\n"
            msg += f"`${target:.2f}`\n\n"

            msg += f"💵 *Current Price*\n"
            msg += f"`${current:.2f}`\n\n"

            msg += f"{move_em} *Beyond Target*\n"
            msg += f"`{move_pct:+.2f}%`\n\n"

            msg += "✅ Alert completed and removed."

            send_telegram(msg)

            a['triggered'] = True
            changed = True

        # ─────────────────────────────────────
        # Approaching
        # ─────────────────────────────────────
        elif (
            not a.get('warning_sent') and
            (
                (direction == 'above' and current >= warn_price) or
                (direction == 'below' and current <= warn_price)
            )
        ):

            away_pct = abs(current - target) / target * 100

            msg  = alert_header("🟡", "APPROACHING TARGET", "═════════════════════")
            msg += f"{em} *{symbol}*\n\n"

            msg += f"💵 *Current*\n"
            msg += f"`${current:.2f}`\n\n"

            msg += f"🎯 *Target*\n"
            msg += f"`${target:.2f}`\n\n"

            msg += f"📏 *Distance Remaining*\n"
            msg += f"`{away_pct:.1f}% away`\n\n"

            msg += "👀 Watch closely — alert not triggered yet."

            send_telegram(msg)

            a['warning_sent'] = True
            changed = True

    if changed:
        save_alerts(alerts)


# ═══════════════════════════════════════════════
# WATCHLIST SCAN — Enhanced Layout v7.2
# ═══════════════════════════════════════════════

def run_watchlist_scan():

    send_telegram("🔎 Scanning watchlist... ~60s", silent=True)

    all_syms, emoji_map = load_universe()

    if not all_syms:
        send_telegram("Could not load symbols.yaml")
        return

    market_ctx = get_market_ctx()
    results    = []

    for sym in all_syms:

        try:

            print(f"  -> {sym}...", end=" ", flush=True)

            ctx = get_full_context(sym)

            time.sleep(0.3)

            if not ctx:
                print("--")
                continue

            verdict, zone, _, _ = get_verdict(ctx, market_ctx)
            rs_score, rs_label  = calc_relative_strength(ctx)

            results.append({
                'symbol':   sym,
                'emoji':    emoji_map.get(sym, ''),
                'verdict':  verdict,
                'zone':     zone,
                'drop':     ctx['day_change_pct'],
                'rsi':      ctx['rsi'],
                'rs_score': rs_score or 0,
                'rs_label': rs_label or '',
                'current':  ctx['current'],
            })

            print(f"{ctx['day_change_pct']:+.1f}% {verdict}")

        except Exception as e:
            print(f"ERROR {e}")

    if not results:
        send_telegram("No data — try again later")
        return

    def sort_key(r):

        v = r['verdict']

        if 'MOMENTUM' in v:
            return (0, -r['drop'])

        if 'BUY' in v:
            return (1, -r['drop'])

        if 'WATCH' in v:
            return (2, -r['drop'])

        if 'NEUTRAL' in v:
            return (3, -r['drop'])

        if 'EXTENDED' in v:
            return (4, -r['drop'])

        if 'AVOID' in v:
            return (5, -r['drop'])

        return (6, -r['drop'])

    results.sort(key=sort_key)

    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%a %b %d · %I:%M %p {tz}')

    msg  = "📋 *WATCHLIST SCAN*\n"
    msg += f"_{ts}_\n"
    msg += "`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    groups = {}

    for r in results:
        groups.setdefault(r['verdict'], []).append(r)

    for vkey, items in groups.items():

        if 'MOMENTUM' in vkey:
            v_em = '🚀'
        elif 'BUY' in vkey:
            v_em = '🟢'
        elif 'WATCH' in vkey:
            v_em = '🟡'
        elif 'EXTENDED' in vkey:
            v_em = '🟠'
        elif 'AVOID' in vkey:
            v_em = '🔴'
        else:
            v_em = '⚪'

        msg += f"{v_em} *{vkey}*\n"

        for r in items:

            sign     = "+" if r['drop'] >= 0 else ""
            drop_em  = "🟢" if r['drop'] >= 0 else "🔴"

            decimals = 4 if r['current'] < 10 else 2
            pf       = f"{{:.{decimals}f}}"

            rs_line = ""

            if r['rs_score']:
                rs_line = f" · RS `{r['rs_score']:+.1f}%`"

            rsi_tag = (
                "🔴" if r['rsi'] >= 70 else
                "🟢" if r['rsi'] <= 30 else
                "🟡"
            )

            msg += (
                f"{r['emoji']} *{r['symbol']}* "
                f"`{pf.format(r['current'])}`\n"
                f"   {drop_em} {sign}{r['drop']:.1f}%"
                f" · {rsi_tag} RSI `{r['rsi']:.0f}`"
                f"{rs_line}\n"
            )

        msg += "\n"

    # Market context footer
    if market_ctx:

        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})

        spy_pct = spy.get('pct', 0)
        vix_val = vix.get('price', 0)

        spy_sign = "+" if spy_pct >= 0 else ""
        spy_em   = "🟢" if spy_pct >= 0 else "🔴"

        vix_tag = (
            "🔴 High"
            if vix_val > 25 else
            "🟡 Elevated"
            if vix_val > 18 else
            "🟢 Calm"
        )

        msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
        msg += f"🌍 SPY {spy_em} `{spy_sign}{spy_pct:.2f}%`"
        msg += f" · VIX `{vix_val:.1f}` {vix_tag}\n\n"

    msg += "_Type any symbol for full analysis_\n"
    msg += "_AlphaEdge v7.2_"

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
            from scanner import format_weekly_summary
            ws = format_weekly_summary()
            send_telegram(ws if ws else "No trade history yet.")
        elif hour < 12:
            from morning_brief import build_morning_brief, mark_morning_sent
            send_telegram("Building morning brief...", silent=True)
            if build_morning_brief():
                mark_morning_sent()
        else:
            from morning_brief import build_evening_brief, mark_evening_sent
            send_telegram("Building evening brief...", silent=True)
            if build_evening_brief():
                mark_evening_sent()
    except Exception as e:
        logging.error(f"Brief: {e}")
        send_telegram(f"Brief failed: {e}")


# ═══════════════════════════════════════════════
# HELP
# ═══════════════════════════════════════════════

def send_help():
    send_telegram("""*ALPHAEDGE BOT COMMANDS*
`━━━━━━━━━━━━━━━━━━━━━`

*ANALYSIS*
`TSLA` -- full analysis
`TSLA NVDA AMD` -- multiple (up to 5)
`TSLA short` -- 3-line quick summary

*WATCHLIST*
`scan` -- rank all your symbols
`top` -- today's top movers

*PRICE ALERTS*
`alert TSLA 450` -- notify near $450
`alert TSLA 400 below` -- notify going down
`cancel TSLA` -- remove alert
`alerts` -- list active alerts

*BRIEFS*
`brief` -- morning/evening/weekend context

`help` -- this message

`━━━━━━━━━━━━━━━━━━━━━`
_AlphaEdge v7.2_""")


# ═══════════════════════════════════════════════
# FULL ANALYSIS RUNNER
# ═══════════════════════════════════════════════

def run_analysis(symbol, mode='full', timeframe='1d'):
    symbol = normalise_symbol(symbol)
    print(f"\n v4.2: {symbol} | mode={mode} | tf={timeframe}")

    send_telegram(f"Analysing *{symbol}*... ~35s", silent=True)

    if not validate_symbol(symbol):
        send_telegram(f"*{symbol}* not found. Check ticker (e.g. TSLA, BTC-USD, NXE)")
        return

    ctx = get_full_context(symbol)
    if not ctx:
        send_telegram(f"Could not fetch data for *{symbol}*")
        return

    print("  -> Stock info...")
    stock_info = get_stock_info(symbol)

    print("  -> Market context...")
    market_ctx = get_market_ctx()

    print("  -> MTF verdicts...")
    mtf_verdicts = get_mtf_verdicts(symbol) if mode == 'full' else {}

    verdict, zone, reasons, next_steps = get_verdict(ctx, market_ctx, mtf_verdicts)
    rs_score, rs_label = calc_relative_strength(ctx)

    if mode == 'short':
        send_telegram(format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score, stock_info))
        return

    print("  -> Sector context...")
    sector_name, sector_avg, _ = get_sector_context(symbol)

    print("  -> CAD pricing...")
    cad_price, tsx_symbol = get_cad_price(symbol)
    usd_cad = get_usd_cad_rate() if not is_crypto(symbol) else None

    print("  -> Daily bars...")
    try:
        df_daily = yf.download(symbol, period='6mo', interval='1d',
                               progress=False, auto_adjust=True)
        df_daily = _clean_df(df_daily)
        poc                        = quick_poc(df_daily)
        support, resistance        = recent_structure(df_daily)
        squeeze_state, squeeze_dir = detect_squeeze(df_daily)
        rsi_div                    = detect_rsi_divergence(df_daily)
    except Exception:
        poc = support = resistance = None
        squeeze_state, squeeze_dir, rsi_div = 'none', None, None

    print("  -> AI analysis...")
    ai_text = get_ai_analysis(ctx, verdict, zone, sector_name, sector_avg,
                               mtf_verdicts, stock_info)
    print(f"  -> AI: {'GOT' if ai_text else 'NO RESPONSE'}")

    msg = format_full_analysis(
        symbol, ctx, verdict, zone, reasons, next_steps,
        ai_text, market_ctx, rs_score, rs_label,
        poc, support, resistance,
        squeeze_state, squeeze_dir, rsi_div,
        mtf_verdicts, sector_name, sector_avg,
        stock_info, cad_price, tsx_symbol, usd_cad
    )
    send_telegram(msg)
    logging.info(f"v4.2 sent: {symbol} | {verdict}")


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

    print(f"\n event={event_type} command={command} symbol={symbol}")

    if event_type == 'analyze_symbol' or (not command and symbol):
        run_analysis(symbol, mode, timeframe)
    elif command == 'alert':
        price     = payload.get('price')
        direction = payload.get('direction', 'auto')
        if symbol and price:
            set_alert(symbol, float(price), direction)
        else:
            send_telegram("Usage: `alert TSLA 450`")
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
        send_telegram(f"Unknown: `{command}`\nType `help` for commands.")


if __name__ == "__main__":
    main()
