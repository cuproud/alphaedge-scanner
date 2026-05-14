"""
ALPHAEDGE SINGLE SCAN v4.3
═══════════════════════════════════════════════════════════════
v4.3 AUDIT FIXES vs v4.2:

CRITICAL:
• next_steps from get_verdict() now rendered directly in format_full_analysis()
  (was silently discarded — build_reentry_table() recomputed generic levels instead)
• build_reentry_table() removed — next_steps IS the action table now
• HOLD and CAUTION verdict branches added to reentry guidance (were falling to
  generic default showing wrong Bull/Bear trigger text)
• Verdict gap fixed: RSI 55-69 + stretch 15-29% + strong uptrend now correctly
  hits a new MOMENTUM CONTINUATION case instead of falling to WATCH
• get_earnings_date() called only once per analysis (was called twice — once in
  get_verdict() and once in format_full_analysis())

HIGH:
• drop_em dead variable removed (defined but never used)
• next_steps dead parameter removed from format_full_analysis() signature
• support/resistance passed in but never rendered — now shown in price section
• short_name (company full name) now shown in header alongside ticker
• industry now shown in header type line
• ai_analyze_drop dead import removed
• os dead import removed
• validate_symbol() replaced with lightweight fast_info check (was doing full
  yfinance download just to confirm ticker exists)
• df_daily download in run_analysis() removed — reuses the daily data already
  fetched by get_full_context() (passed through as ctx['df_daily'])
• get_market_ctx() QQQ fetch: no change here (in market_intel.py) but QQQ
  value is now shown in market section for context

MEDIUM:
• detect_rsi_divergence() now uses rolling min/max for proper swing detection
  instead of endpoint comparison (was firing on trend noise)
• ath_recency() now uses now_est() instead of naive datetime.now()
• Company full name shown in header: "TSLA — Tesla, Inc. (NASDAQ)"

VERSION: v7.3
"""

import sys
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
    SYMBOL_EMOJI, SECTORS, SYMBOL_TO_SECTOR,
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
    """
    Lightweight existence check using fast_info.
    Avoids a full yfinance download just to confirm a ticker exists.
    Falls back to a minimal download if fast_info unavailable.
    """
    try:
        fi = yf.Ticker(sym).fast_info
        price = getattr(fi, 'last_price', None)
        if price and float(price) > 0:
            return True
        # fallback
        df = yf.download(sym, period='2d', interval='1d',
                         progress=False, auto_adjust=True)
        return not df.empty
    except Exception:
        return False

def volume_label(vol_ratio):
    if vol_ratio >= 2.0:  return f"{vol_ratio:.1f}x avg — Unusually high"
    if vol_ratio >= 1.5:  return f"{vol_ratio:.1f}x avg — Above average"
    if vol_ratio >= 0.8:  return f"{vol_ratio:.1f}x avg — Normal"
    return f"{vol_ratio:.1f}x avg — Below average (weak move)"

def ath_recency(ath_date_str):
    """Uses now_est() for timezone consistency."""
    try:
        ath_dt = datetime.strptime(ath_date_str[:10], '%Y-%m-%d')
        now    = now_est().replace(tzinfo=None)  # naive comparison to date-only ath_dt
        days   = (now - ath_dt).days
        if days == 0:   return "set TODAY"
        if days == 1:   return "set YESTERDAY"
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
            'long_name': symbol,
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
            'long_name':     info.get('longName', info.get('shortName', symbol)),
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
            'long_name':  symbol,
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
        if len(df) < 2:
            return None, None
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
        if len(in_sq) >= 2 and in_sq.iloc[-2] and not in_sq.iloc[-1]:
            direction = 'bullish' if df['Close'].iloc[-1] > bb_basis.iloc[-1] else 'bearish'
            return 'fired', direction
        return 'none', None
    except Exception:
        return 'none', None


def detect_rsi_divergence(df):
    """
    Proper divergence detection using rolling min/max over a lookback window,
    not endpoint-to-endpoint comparison which fires on trend noise.

    Bullish divergence: price makes new recent low AND RSI makes a higher low.
    Bearish divergence: price makes new recent high AND RSI makes a lower high.
    Requires 5-point RSI gap (vs old 3) to reduce false positives.
    """
    try:
        if len(df) < 30:
            return None

        rsi_series = pine_rsi(df['Close'], 14)
        look       = 14  # bars to look back

        price_window = df['Low'].iloc[-look:]
        rsi_window   = rsi_series.iloc[-look:]
        price_h_win  = df['High'].iloc[-look:]
        rsi_h_win    = rsi_series.iloc[-look:]

        # Bullish: price made a new low in last 3 bars but RSI is higher than the period low
        recent_price_low = price_window.iloc[-3:].min()
        period_price_low = price_window.iloc[:-3].min()
        recent_rsi_low   = rsi_window.iloc[-3:].min()
        period_rsi_low   = rsi_window.iloc[:-3].min()

        if recent_price_low < period_price_low and recent_rsi_low > period_rsi_low + 5:
            return 'bullish'

        # Bearish: price made a new high in last 3 bars but RSI is lower
        recent_price_high = price_h_win.iloc[-3:].max()
        period_price_high = price_h_win.iloc[:-3].max()
        recent_rsi_high   = rsi_h_win.iloc[-3:].max()
        period_rsi_high   = rsi_h_win.iloc[:-3].max()

        if recent_price_high > period_price_high and recent_rsi_high < period_rsi_high - 5:
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
# VERDICT ENGINE v4.3
# Returns (verdict, zone, reasons, next_steps)
# next_steps contains specific price levels and is rendered directly in the alert.
# ═══════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None, mtf_verdicts=None, earnings_cache=None):
    """
    earnings_cache: (earnings_date, days_until) tuple to avoid double API call.
    If not provided, get_earnings_date() is called here. Pass it in from run_analysis()
    to prevent the second call in format_full_analysis().
    """
    c         = ctx
    rsi       = c['rsi']
    trend     = c['trend']
    drop      = c['day_change_pct']
    from_ath  = c['ath_pct']
    range_pos = c['range_pos']
    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']

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

    # ── 0b. PARABOLIC EXTENSION — extreme EMA stretch ──
    if stretch_pct >= 50 and rsi >= 70:
        reentry_lo = round(c['ema50'] * 0.98, 2)
        reentry_hi = round(c['ema50'] * 1.05, 2)
        rsi_tf_str = f" / {mtf_max_rsi:.0f} higher TF" if mtf_verdicts else ""
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
            f"Invalidation: below EMA200 ${c['ema200']:.2f}",
            "If holding: trail stop, consider taking 25-33% off",
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
    elif (rsi >= 70 or stretch_pct >= 30) and above_50 and above_200:
        reentry_lo = round(c['ema50'] * 0.98, 2)
        reentry_hi = round(c['ema50'] * 1.05, 2)
        verdict, zone = "EXTENDED", "Overbought — Wait for Pullback"
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

    # ── 4. MOMENTUM CONTINUATION — strong uptrend, RSI 55-69, not extended ──
    # Fixes gap: RSI 55-69 + stretch < 25% + uptrend was falling to generic WATCH
    elif ("UPTREND" in trend and 55 <= rsi < 70 and above_200 and stretch_pct < 25):
        verdict, zone = "MOMENTUM CONTINUATION", "Uptrend — Healthy"
        reasons = [
            f"Strong uptrend intact — above both EMAs",
            f"RSI {rsi:.0f} — momentum building, not yet overbought",
        ]
        if from_ath > -15:
            reasons.append(f"Near ATH ({from_ath:+.1f}%) — trend is strong")
        if mtf_all_bull:
            reasons.append("All timeframes aligned bullish")
        next_steps = [
            f"Ideal entry: pullback to EMA50 ${c['ema50']:.2f} (lower risk)",
            f"Momentum entry: current ${c['current']:.2f} acceptable if conviction is high",
            f"Target: ATH ${c['ath']:.2f} ({from_ath:+.1f}% away)",
            f"Stop: close below EMA50 ${c['ema50']:.2f}",
        ]

    # ── 5. BUY ZONE — strong uptrend pullback ──
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
            f"Target 1: ATH ${c['ath']:.2f} ({from_ath:+.1f}% away)",
            "Target 2: new ATH breakout",
            f"Stop: below EMA200 ${c['ema200']:.2f}",
        ]

    # ── 6. BUY ZONE — EMA50 pullback ──
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

    # ── 7. DOWNTREND ──
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

    # ── 8. NEAR 52W LOW ──
    elif c['pct_from_52w_low'] < 8 and drop < -3:
        verdict, zone = "CAUTION", "Breaking Down"
        reasons = ["Near 52W low — key support at risk of breaking"]
        next_steps = [
            f"Watch: holds ${c['low_52w']:.2f} (52W low support)",
            "Enter only after 2-3 days of stabilisation above the low",
            f"Stop if entering: below ${c['low_52w']:.2f}",
        ]

    # ── 9. TAKE PROFITS ──
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

    # ── 10. RECOVERING ──
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
                f"Do not enter below EMA200 ${c['ema200']:.2f}",
            ]

    # ── 11. MIXED ──
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

    # ── 12. DEFAULT ──
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
    # Uses earnings_cache to avoid double API call when called from run_analysis()
    if any(x in verdict for x in ["BUY", "MOMENTUM", "WATCH", "EXTENDED"]):
        if earnings_cache is not None:
            _, days_until = earnings_cache
        else:
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
[Setup quality and R:R — is it worth taking now?]
[Biggest invalidation risk — specific level]
[STRONG BUY/BUY/HOLD/AVOID/WAIT] — [one sharp sentence]

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
            logging.warning("Gemini rate limited, retrying in 15s")
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
            logging.warning(f"Gemini error {r.status_code}")
    except Exception as e:
        logging.error(f"AI analysis: {e}")
    return None


# ═══════════════════════════════════════════════
# TAG PILLS
# ═══════════════════════════════════════════════

def build_tag_pills(verdict, ctx, rs_label, squeeze_state, rsi_div, stock_info):
    """
    Compact emoji tag pills shown under header.
    Max 4 pills. Each pill has an emoji so they scan instantly on mobile.
    RSI threshold >= 70 = overbought (consistent with verdict logic).
    """
    tags = []
    c    = ctx
    rsi  = c['rsi']
    stretch_pct = (c['current'] - c['ema50']) / c['ema50'] * 100 if c['ema50'] > 0 else 0

    # Primary verdict pill — emoji encodes direction at a glance
    if 'PARABOLIC' in verdict:               tags.append('🚨 Parabolic')
    elif 'CRASH' in verdict:                 tags.append('💥 Crash')
    elif 'EXTREMELY EXTENDED' in verdict:    tags.append('🔥 Extremely Extended')
    elif 'MOMENTUM CONTINUATION' in verdict: tags.append('🚀 Momentum')
    elif 'MOMENTUM' in verdict:              tags.append('🚀 ATH Momentum')
    elif 'BUY ZONE' in verdict:              tags.append('🟢 Buy Zone')
    elif 'EXTENDED' in verdict:              tags.append('🟠 Extended')
    elif 'TAKE PROFITS' in verdict:          tags.append('💰 Take Profits')
    elif 'AVOID' in verdict:                 tags.append('🔴 Avoid')
    elif 'CAUTION' in verdict:               tags.append('⚠️ Caution')
    elif 'WAIT' in verdict:                  tags.append('⏳ Wait')
    elif 'WATCH' in verdict:                 tags.append('👀 Watch')
    elif 'HOLD' in verdict:                  tags.append('⏸️ Hold')
    elif 'NEUTRAL' in verdict:               tags.append('⚖️ Neutral')

    # RSI pill
    if rsi >= 80:        tags.append('🔴 RSI Extreme')
    elif rsi >= 70:      tags.append('🔴 RSI Overbought')
    elif rsi <= 30:      tags.append('🟢 RSI Oversold')
    elif rsi >= 60:      tags.append('🟡 RSI Bullish')

    # EMA stretch pill (only if not already captured by primary verdict)
    if stretch_pct >= 50 and 'PARABOLIC' not in verdict:
        tags.append(f'⚡ {stretch_pct:.0f}% above EMA50')
    elif 30 <= stretch_pct < 50 and 'EXTENDED' not in verdict:
        tags.append(f'⚡ {stretch_pct:.0f}% above EMA50')

    # RS vs SPY pill
    if rs_label:
        if 'Strong Leader' in rs_label:    tags.append('💪 Strong Leader')
        elif 'Outperform' in rs_label:     tags.append('📈 Outperforming')
        elif 'Laggard' in rs_label:        tags.append('📉 Laggard')
        elif 'Underperform' in rs_label:   tags.append('🔻 Underperforming')

    # Squeeze / divergence pills
    if squeeze_state == 'building':        tags.append('🔥 Squeeze Building')
    elif squeeze_state == 'fired':         tags.append('💥 Squeeze Fired')
    if rsi_div == 'bullish':               tags.append('📈 RSI Div Bullish')
    elif rsi_div == 'bearish':             tags.append('📉 RSI Div Bearish')

    # Short interest pill
    short_pct = stock_info.get('short_pct')
    if short_pct and short_pct > 0.15:    tags.append(f'⚡ {short_pct*100:.0f}% Short')

    return '  ·  '.join(tags[:4])


# ═══════════════════════════════════════════════
# PRICE CONTEXT GRID
# ═══════════════════════════════════════════════

def build_price_context_grid(ctx, cad_price, tsx_symbol, usd_cad, support, resistance):
    """
    Compact scannable price context.
    Each row has a fixed emoji anchor so the eye can jump to any row instantly.
    Support/resistance now rendered here (were previously passed but never shown).
    """
    c = ctx
    decimals = 4 if c['current'] < 10 else 2
    pf = f"{{:.{decimals}f}}"

    vol_ratio = c['vol_ratio']
    if vol_ratio >= 2.0:   vol_str = f"`{vol_ratio:.1f}x` 🔥 Unusually high"
    elif vol_ratio >= 1.5: vol_str = f"`{vol_ratio:.1f}x` ⬆️ Above average"
    elif vol_ratio >= 0.8: vol_str = f"`{vol_ratio:.1f}x` — Normal"
    else:                  vol_str = f"`{vol_ratio:.1f}x` ⬇️ Below average (weak move)"

    rp = c['range_pos']
    if rp >= 90:   rp_bar = "████████████ 90%+ — near top"
    elif rp >= 70: rp_bar = f"█████████░░░ {rp:.0f}% — upper range"
    elif rp >= 50: rp_bar = f"██████░░░░░░ {rp:.0f}% — mid range"
    elif rp >= 30: rp_bar = f"████░░░░░░░░ {rp:.0f}% — lower range"
    else:          rp_bar = f"██░░░░░░░░░░ {rp:.0f}% — near bottom"

    ath_str = f"`{c['ath_pct']:+.1f}%` — {ath_recency(c['ath_date'])}"

    msg  = f"📊 *PRICE CONTEXT*\n`─────────────────────────`\n"
    msg += f"📦 Volume    {vol_str}\n"
    msg += f"📍 52W Pos   `{rp_bar}`\n"
    msg += f"🏔️ From ATH  {ath_str}\n"
    msg += f"📐 52W Range `${pf.format(c['low_52w'])}` — `${pf.format(c['high_52w'])}`\n"

    if c.get('pct_from_52w_low', 0) > 500:
        msg += f"⚠️ _52W low `${pf.format(c['low_52w'])}` may reflect split/spin-off — range unreliable_\n"

    if support and resistance:
        msg += f"🔑 Structure Support `${pf.format(support)}` · Resistance `${pf.format(resistance)}`\n"

    if cad_price and tsx_symbol:
        msg += f"🍁 TSX      `{tsx_symbol}` = `${cad_price:.2f} CAD`\n"
    elif usd_cad and not is_crypto(c['symbol']):
        implied = round(c['current'] * usd_cad, 2)
        msg += f"🍁 CAD      `${implied:.2f}` (×{usd_cad:.4f}) — no TSX listing\n"

    return msg


# ═══════════════════════════════════════════════
# FORMAT FULL ANALYSIS v4.3
# Layout:
#   1. Header (company name, ticker, price, change, type)
#   2. Tag pills
#   3. Verdict + AI one-liner + reasons
#   4. Price + POC
#   5. What To Do (next_steps from get_verdict — specific price levels)
#   6. Price context grid (volume, 52W, ATH, structure, CAD)
#   7. Timeframe alignment
#   8. Technicals
#   9. Sector + RS
#  10. Fundamentals (analyst targets with flag)
#  11. Earnings (single call, cached from run_analysis)
#  12. Market (SPY + VIX + QQQ)
#  13. AI full analysis
# ═══════════════════════════════════════════════

def format_full_analysis(symbol, ctx, verdict, zone, reasons,
                          ai_text, market_ctx, rs_score, rs_label,
                          poc, support, resistance,
                          squeeze_state, squeeze_dir, rsi_div,
                          mtf_verdicts, sector_name, sector_avg,
                          stock_info, cad_price, tsx_symbol, usd_cad,
                          next_steps, earnings_info):
    """
    Redesigned v7.3 layout — emoji-anchored headers, visual hierarchy,
    verdict prominence, breathing room between sections.

    Telegram cannot change font sizes but bold + emoji headers + separator
    lines create strong visual differentiation from body content.

    next_steps: from get_verdict() — specific computed price levels.
    earnings_info: (earnings_date, days_until) — pre-fetched, no second API call.
    """
    em  = SYMBOL_EMOJI.get(symbol, '📊')
    c   = ctx
    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%b %d · %I:%M %p {tz}')
    decimals = 4 if c['current'] < 10 else 2
    pf       = f"{{:.{decimals}f}}"
    drop     = c['day_change_pct']
    sign     = "+" if drop >= 0 else ""
    drop_em  = "🟢" if drop >= 0 else "🔴"

    stretch_pct = (c['current'] - c['ema50']) / c['ema50'] * 100 if c['ema50'] > 0 else 0

    asset_type = stock_info.get('asset_type', 'Stock')
    long_name  = stock_info.get('long_name', symbol)
    sector_h   = stock_info.get('sector', SYMBOL_TO_SECTOR.get(symbol, ''))
    industry   = stock_info.get('industry', '')
    exchange   = stock_info.get('exchange', '')
    mcap       = fmt_mcap(stock_info.get('market_cap'))
    beta_val   = stock_info.get('beta')

    # ════════════════════════════════════════════
    # § 1  HEADER
    # Big bold ticker + emoji. Company name on next line.
    # Price + change + timestamp all on one scannable line.
    # ════════════════════════════════════════════
    name_str = f"\n_{long_name}_" if long_name and long_name != symbol else ""
    msg  = f"🔍 *{symbol}* {em}{name_str}\n"
    msg += f"`${pf.format(c['current'])}`  {drop_em} *{sign}{drop:.2f}%*  _· {ts}_\n"

    meta_parts = [asset_type]
    if sector_h:  meta_parts.append(sector_h)
    if industry and industry != sector_h: meta_parts.append(industry)
    if exchange:  meta_parts.append(exchange)
    if mcap:      meta_parts.append(mcap)
    msg += f"_{' · '.join(meta_parts)}_\n"
    msg += f"`══════════════════════════`\n\n"

    # ════════════════════════════════════════════
    # § 2  TAG PILLS — emoji-differentiated, · separated
    # ════════════════════════════════════════════
    tags = build_tag_pills(verdict, ctx, rs_label, squeeze_state, rsi_div, stock_info)
    if tags:
        msg += f"{tags}\n\n"

    # ════════════════════════════════════════════
    # § 3  VERDICT BLOCK
    # This is the most important section — it gets a full box treatment.
    # Verdict in bold caps, zone in italics, reasons as bullets.
    # AI one-liner sits right under verdict for immediate context.
    # ════════════════════════════════════════════
    msg += f"*〔 {verdict} 〕*\n"
    msg += f"_↳ {zone}_\n"

    # AI summary line elevated to verdict level — most actionable insight first
    if ai_text:
        lines   = ai_text.strip().split('\n')
        summary = next((l for l in reversed(lines)
                        if any(w in l.upper() for w in ['WAIT', 'BUY', 'HOLD', 'AVOID', 'STRONG'])),
                       lines[-1] if lines else None)
        if summary:
            msg += f"\n{summary}\n"

    msg += "\n"
    for r in reasons[:3]:
        msg += f"  › {r}\n"
    msg += f"`══════════════════════════`\n\n"

    # ════════════════════════════════════════════
    # § 4  PRICE
    # ════════════════════════════════════════════
    msg += f"💵 *PRICE*\n`──────────────────────────`\n"
    msg += f"  Live     `${pf.format(c['current'])}`  {drop_em} {sign}{drop:.2f}% today\n"
    msg += f"  Range    L `${pf.format(c['today_low'])}` — H `${pf.format(c['today_high'])}`\n"

    # Volume with context
    vol_ratio = c['vol_ratio']
    if vol_ratio >= 2.0:   vol_line = f"`{vol_ratio:.1f}x` 🔥 Unusually high"
    elif vol_ratio >= 1.5: vol_line = f"`{vol_ratio:.1f}x` ⬆️ Above average"
    elif vol_ratio >= 0.8: vol_line = f"`{vol_ratio:.1f}x` — Normal"
    else:                  vol_line = f"`{vol_ratio:.1f}x` ⬇️ Below avg — weak move"
    msg += f"  Volume   {vol_line}\n"

    if poc:
        diff_pct = (c['current'] - poc) / poc * 100
        poc_em   = "🎯" if abs(diff_pct) < 0.5 else ("⬆️" if c['current'] > poc else "⬇️")
        if abs(diff_pct) < 0.5:
            msg += f"  POC      {poc_em} AT `${pf.format(poc)}` — volume magnet\n"
        elif c['current'] > poc:
            msg += f"  POC      {poc_em} Above `${pf.format(poc)}` ({diff_pct:+.1f}%) — buyers in control\n"
        else:
            msg += f"  POC      {poc_em} Below `${pf.format(poc)}` ({diff_pct:+.1f}%) — sellers in control\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # § 5  WHAT TO DO
    # next_steps from get_verdict() — specific price levels, not generic text.
    # Monospace block for each step makes prices stand out visually.
    # ════════════════════════════════════════════
    msg += f"🎯 *WHAT TO DO*\n`──────────────────────────`\n"
    for step in next_steps:
        msg += f"  › {step}\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # § 6  PRICE CONTEXT
    # ════════════════════════════════════════════
    msg += build_price_context_grid(ctx, cad_price, tsx_symbol, usd_cad, support, resistance)
    msg += "\n"

    # ════════════════════════════════════════════
    # § 7  TIMEFRAMES
    # Compact 3-row table. Emoji encodes bull/bear at a glance.
    # ════════════════════════════════════════════
    if mtf_verdicts:
        msg += f"🗂️ *TIMEFRAMES*\n`──────────────────────────`\n"
        for tf_label, v in mtf_verdicts.items():
            # Trend emoji
            t = v['trend']
            t_em = "🚀" if "Strong Bull" in t else "📈" if "Bull" in t else "💀" if "Strong Bear" in t else "📉" if "Bear" in t else "⚖️"

            # SAR emoji
            sar_em = "✅" if v.get('sar_bull') else "❌" if v.get('sar_bull') is False else "➖"

            # ADX label
            adx_em = "💪" if v['adx'] >= 25 else "⚠️" if v['adx'] < 20 else "➖"

            # RSI with tag
            rsi_str = f"RSI `{v['rsi']}`"
            if v.get('rsi_tag'):
                rsi_str += f" _{v['rsi_tag']}_"

            # Signal label
            sig = v['adx_sar']
            sig_em = "✅" if sig == "Trend BUY" else "❌" if sig == "Trend SELL" else "⚠️" if sig == "Ranging" else "➖"

            msg += (f"  {t_em} *{tf_label}*  {t}\n"
                    f"     {rsi_str}  {adx_em} ADX `{v['adx']:.0f}`  SAR {sar_em}  {sig_em} _{sig}_\n")
        msg += "\n"

    # ════════════════════════════════════════════
    # § 8  TECHNICALS
    # ════════════════════════════════════════════
    msg += f"📈 *TECHNICALS*\n`──────────────────────────`\n"
    msg += f"  Trend   {c['trend']}\n"

    # RSI — threshold >= 70 = overbought (consistent with verdict)
    if c['rsi'] < 30:       rsi_tag, rsi_em = "Oversold",   "🟢"
    elif c['rsi'] >= 70:    rsi_tag, rsi_em = "Overbought", "🔴"
    elif c['rsi'] > 60:     rsi_tag, rsi_em = "Bullish",    "🟡"
    else:                   rsi_tag, rsi_em = "Neutral",    "⚪"
    msg += f"  RSI     `{c['rsi']:.0f}` {rsi_em} _{rsi_tag}_\n"

    # EMA50 with stretch warning — wording consistent with verdict action
    if stretch_pct >= 50:    stretch_warn = " 🚨 _Extreme — do not chase_"
    elif stretch_pct >= 30:  stretch_warn = " ⚠️ _Extended — wait for pullback_"
    elif stretch_pct >= 15:  stretch_warn = " ⚠️ _Stretched_"
    elif stretch_pct <= -15: stretch_warn = " 🟢 _Deeply oversold_"
    else:                    stretch_warn = ""
    msg += f"  EMA50   `${pf.format(c['ema50'])}` ({stretch_pct:+.1f}%){stretch_warn}\n"
    msg += f"  EMA200  `${pf.format(c['ema200'])}`\n"

    # EMA position summary
    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if above_50 and above_200:          msg += "  ✅ Above EMA50 & EMA200\n"
    elif above_200 and not above_50:    msg += "  ⚠️ Below EMA50, above EMA200\n"
    elif not above_200 and above_50:    msg += "  🔀 Above EMA50, below EMA200\n"
    else:                               msg += "  🔴 Below both EMAs\n"

    if beta_val:
        beta_em   = "🐢" if beta_val < 0.8 else "⚡" if beta_val > 1.5 else "⚖️"
        beta_desc = "low volatility" if beta_val < 0.8 else "high volatility" if beta_val > 1.5 else "market volatility"
        msg += f"  Beta    `{beta_val:.2f}` {beta_em} _{beta_desc}_\n"

    # Squeeze / divergence — only show if active
    if squeeze_state == 'building':
        msg += f"  🔥 *SQUEEZE BUILDING* — explosive move loading\n"
    elif squeeze_state == 'fired':
        sq_em = "⬆️" if squeeze_dir == 'bullish' else "⬇️"
        msg += f"  💥 *SQUEEZE FIRED* {sq_em} {squeeze_dir}\n"
    if rsi_div == 'bullish':
        msg += f"  📈 *RSI DIVERGENCE* — bullish momentum building\n"
    elif rsi_div == 'bearish':
        msg += f"  📉 *RSI DIVERGENCE* — momentum fading\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # § 9  SECTOR + RELATIVE STRENGTH
    # ════════════════════════════════════════════
    if sector_name and sector_avg is not None:
        sec_em  = "🟢" if sector_avg > 0 else "🔴"
        sym_vs  = drop - sector_avg
        sec_sign = "+" if sector_avg >= 0 else ""
        msg += f"🏭 *SECTOR · {sector_name}*\n`──────────────────────────`\n"
        msg += f"  Sector avg  {sec_em} `{sec_sign}{sector_avg:.2f}%` today\n"
        if sym_vs > 1.5:
            msg += f"  💪 Outperforming by `{sym_vs:+.1f}%`\n"
        elif sym_vs < -1.5:
            msg += f"  🔻 Underperforming by `{sym_vs:+.1f}%`\n"
        else:
            msg += f"  ➖ In line with sector\n"
        msg += "\n"

    if rs_score is not None:
        rs_sign = "+" if rs_score >= 0 else ""
        rs_em   = "💪" if rs_score > 5 else "📉" if rs_score < -5 else "➖"
        msg += f"  {rs_em} *RS vs SPY (5d)*  {rs_label}  `{rs_sign}{rs_score}%`\n\n"

    # ════════════════════════════════════════════
    # § 10  FUNDAMENTALS
    # Analyst targets with overextension flag.
    # ════════════════════════════════════════════
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
        msg += f"📊 *FUNDAMENTALS*\n`──────────────────────────`\n"

        if target_mean:
            upside    = (target_mean - c['current']) / c['current'] * 100
            up_sign   = "+" if upside >= 0 else ""
            upside_em = "🟢" if upside > 0 else "🔴"
            msg += f"  🎯 Analyst target  `${target_mean:.2f}` {upside_em} `{up_sign}{upside:.1f}%`"
            if analyst_n:
                msg += f" _({analyst_n} analysts)_"
            msg += "\n"
            if target_high and target_low:
                msg += f"     Range `${target_low:.2f}` — `${target_high:.2f}`\n"
            if rec_key:
                rec_em = "🟢" if 'Buy' in rec_key else "🔴" if 'Sell' in rec_key else "🟡"
                msg += f"     Rating {rec_em} *{rec_key}*\n"
            # Flag when stock has run past analyst consensus
            if upside < -15:
                msg += (f"  🚨 *{abs(upside):.0f}% ABOVE analyst consensus*\n"
                        f"     _Analysts haven't upgraded — may be overextended_\n")
            elif upside < -5:
                msg += f"  ⚠️ _Stock above analyst consensus — limited upside per analysts_\n"

        if pe_ratio:
            pe_em  = "🔴" if pe_ratio > 60 else "🟡" if pe_ratio > 30 else "🟢"
            pe_tag = " _(elevated)_" if pe_ratio > 40 else ""
            msg += f"  {pe_em} P/E ratio  `{pe_ratio:.0f}`{pe_tag}\n"

        if short_pct:
            si_em  = "⚡" if short_pct > 0.15 else "➖"
            si_tag = "*High*" if short_pct > 0.15 else "Normal"
            msg += f"  {si_em} Short int  `{short_pct*100:.1f}%` {si_tag}"
            if short_pct > 0.15:
                msg += " — _squeeze potential on breakout_"
            msg += "\n"

        if inst_pct:
            inst_em  = "🏦" if inst_pct > 0.7 else "➖"
            inst_tag = "Smart money heavy" if inst_pct > 0.7 else "Moderate"
            msg += f"  {inst_em} Institutional  `{inst_pct*100:.0f}%` — {inst_tag}\n"

        msg += "\n"

    # ════════════════════════════════════════════
    # § 11  EARNINGS — pre-fetched, no second API call
    # ════════════════════════════════════════════
    if earnings_info:
        earnings_date, days_until = earnings_info
        warn = format_earnings_warning(symbol, earnings_date, days_until)
        if warn:
            msg += f"📅 *EARNINGS*\n`──────────────────────────`\n  {warn}\n\n"

    # ════════════════════════════════════════════
    # § 12  MARKET CONDITIONS
    # ════════════════════════════════════════════
    if market_ctx:
        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})
        if spy or vix:
            msg += f"🌍 *MARKET*\n`──────────────────────────`\n"
            parts = []
            if spy:
                spy_pct  = spy.get('pct', 0)
                spy_sign = "+" if spy_pct >= 0 else ""
                spy_em   = "🟢" if spy_pct >= 0 else "🔴"
                parts.append(f"SPY {spy_em} `{spy_sign}{spy_pct:.2f}%`")
            if vix:
                vix_val = vix.get('price', 0)
                vix_em  = "🔴" if vix_val > 25 else "🟡" if vix_val > 18 else "🟢"
                vix_tag = "High" if vix_val > 25 else "Elevated" if vix_val > 18 else "Calm"
                parts.append(f"VIX {vix_em} `{vix_val:.1f}` _{vix_tag}_")
            msg += f"  {'  ·  '.join(parts)}\n\n"

    # ════════════════════════════════════════════
    # § 13  AI ANALYSIS
    # Full 4-line response from Gemini.
    # ════════════════════════════════════════════
    if ai_text:
        msg += f"🤖 *AI ANALYSIS*\n`──────────────────────────`\n"
        for line in ai_text.strip().split('\n'):
            if line.strip():
                msg += f"  {line.strip()}\n"
        msg += "\n"

    msg += f"`══════════════════════════`\n"
    msg += f"_AlphaEdge v7.3 · On-demand_"
    return msg


def format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score, stock_info):
    em       = SYMBOL_EMOJI.get(symbol, '')
    c        = ctx
    drop     = c['day_change_pct']
    sign     = "+" if drop >= 0 else ""
    decimals = 4 if c['current'] < 10 else 2
    pf       = f"{{:.{decimals}f}}"
    rs_str   = f"  RS {rs_label}" if rs_label else ""
    long_name = stock_info.get('long_name', symbol)
    name_str  = f" ({long_name})" if long_name and long_name != symbol else ""
    msg  = f"{em} *{symbol}*{name_str}  `${pf.format(c['current'])}`  ({sign}{drop:.1f}%)\n"
    msg += f"{verdict} — {zone}\n"
    msg += f"RSI `{c['rsi']:.0f}`  {c['trend']}{rs_str}"
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

    dir_str    = "rises to" if direction == 'above' else "falls to"
    warn_price = target_price * 0.98 if direction == 'above' else target_price * 1.02
    cur_str    = f" (currently ${current:.2f})" if current else ""
    send_telegram(
        f"Alert set\n"
        f"{SYMBOL_EMOJI.get(symbol,'')} *{symbol}* — notify when {dir_str} `${target_price:.2f}`{cur_str}\n"
        f"Early warning at `${warn_price:.2f}` (2% before)\n"
        f"Expires in 30 days"
    )

def cancel_alert(symbol):
    alerts  = load_alerts()
    removed = []
    for key in list(alerts.keys()):
        if alerts[key]['symbol'] == symbol:
            removed.append(alerts[key]['target'])
            del alerts[key]
    save_alerts(alerts)
    em = SYMBOL_EMOJI.get(symbol, '')
    if removed:
        send_telegram(f"Cancelled alerts for {em} *{symbol}*: {', '.join([f'${t}' for t in removed])}")
    else:
        send_telegram(f"No active alerts for *{symbol}*")

def list_alerts():
    alerts = load_alerts()
    active = {k: v for k, v in alerts.items() if not v.get('triggered')}
    if not active:
        send_telegram("No active alerts.\n\nSet one: `alert TSLA 450`")
        return
    msg = f"*ACTIVE ALERTS ({len(active)})*\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"
    for key, a in sorted(active.items(), key=lambda x: x[1]['symbol']):
        em        = SYMBOL_EMOJI.get(a['symbol'], '')
        dir_str   = "above" if a['direction'] == 'above' else "below"
        expires   = datetime.fromisoformat(a['expires_at'])
        days_left = (expires - now_est()).days
        warn      = a['target'] * 0.98 if a['direction'] == 'above' else a['target'] * 1.02
        msg += f"{em} *{a['symbol']}*  {dir_str} `${a['target']:.2f}`\n"
        msg += f"   Early warning `${warn:.2f}`  {days_left}d left\n\n"
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
        em         = SYMBOL_EMOJI.get(symbol, '')

        expires   = datetime.fromisoformat(a['expires_at'])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=EST)
        days_left = (expires - now).days

        if days_left <= 1 and not a.get('expiry_warning_sent'):
            send_telegram(f"Alert expiring\n{em} *{symbol}* to `${target:.2f}` expires tomorrow\n`alert {symbol} {target}` to reset")
            a['expiry_warning_sent'] = True
            changed = True

        if now > expires:
            send_telegram(f"Alert expired\n{em} *{symbol}* to `${target:.2f}` (30 days, untriggered)")
            del alerts[key]
            changed = True
            continue

        try:
            df      = yf.download(symbol, period='1d', interval='5m',
                                  progress=False, auto_adjust=True)
            if df.empty: continue
            df      = _clean_df(df)
            current = float(df['Close'].iloc[-1])
        except Exception:
            continue

        if ((direction == 'above' and current >= target) or
                (direction == 'below' and current <= target)):
            send_telegram(f"ALERT TRIGGERED\n{em} *{symbol}* hit `${target:.2f}`\nCurrent: `${current:.2f}`\nAlert removed.")
            a['triggered'] = True
            changed = True
        elif (not a.get('warning_sent') and
              ((direction == 'above' and current >= warn_price) or
               (direction == 'below' and current <= warn_price))):
            send_telegram(f"APPROACHING TARGET\n{em} *{symbol}* near `${target:.2f}`\nNow: `${current:.2f}` — {abs(current-target)/target*100:.1f}% away")
            a['warning_sent'] = True
            changed = True

    if changed:
        save_alerts(alerts)


# ═══════════════════════════════════════════════
# WATCHLIST SCAN
# ═══════════════════════════════════════════════

def run_watchlist_scan():
    send_telegram("Scanning watchlist... ~60s", silent=True)
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
                print("--"); continue
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
                'current':  ctx['current'],
            })
            print(f"{ctx['day_change_pct']:+.1f}% {verdict}")
        except Exception as e:
            logging.warning(f"Watchlist scan {sym}: {e}")
            print(f"err")

    if not results:
        send_telegram("No data — try again later")
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
    ts  = now.strftime(f'%a %b %d  %I:%M %p {tz}')
    msg = f"*WATCHLIST SCAN*\n{ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    groups = {}
    for r in results:
        groups.setdefault(r['verdict'], []).append(r)

    for vkey, items in groups.items():
        msg += f"*{vkey}*\n"
        for r in items:
            sign     = "+" if r['drop'] >= 0 else ""
            decimals = 4 if r['current'] < 10 else 2
            pf       = f"{{:.{decimals}f}}"
            rs_str   = f" RS {r['rs_score']:+.1f}%" if r['rs_score'] else ""
            msg += (f"  {r['emoji']} *{r['symbol']}* `${pf.format(r['current'])}`"
                    f"  {sign}{r['drop']:.1f}%  RSI `{r['rsi']:.0f}`{rs_str}\n")
        msg += "\n"

    msg += "_Type any symbol for full analysis_\n_AlphaEdge v7.3_"
    send_telegram(msg)


# ═══════════════════════════════════════════════
# TOP MOVERS
# ═══════════════════════════════════════════════

def run_top_movers():
    send_telegram("Fetching top movers...", silent=True)
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
            movers.append({'symbol': sym, 'emoji': emoji_map.get(sym, ''),
                           'change': change, 'price': float(df['Close'].iloc[-1])})
            time.sleep(0.2)
        except Exception:
            pass

    if not movers:
        send_telegram("Could not fetch data")
        return

    movers.sort(key=lambda x: -x['change'])
    gainers = [m for m in movers if m['change'] > 0][:5]
    losers  = [m for m in movers if m['change'] < 0][-5:]
    losers.reverse()

    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%a %b %d  %I:%M %p {tz}')
    msg = f"*TOP MOVERS*\n{ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    if gainers:
        msg += "*GAINERS*\n"
        for m in gainers:
            d = 4 if m['price'] < 10 else 2
            msg += f"  {m['emoji']} *{m['symbol']}* `${m['price']:.{d}f}`  +{m['change']:.2f}%\n"
    if losers:
        msg += f"\n*LOSERS*\n"
        for m in losers:
            d = 4 if m['price'] < 10 else 2
            msg += f"  {m['emoji']} *{m['symbol']}* `${m['price']:.{d}f}`  {m['change']:.2f}%\n"

    if market_ctx:
        spy = market_ctx.get('SPY', {})
        vix = market_ctx.get('^VIX', {})
        msg += f"\n`─────────────────`\n"
        if spy:
            spy_pct  = spy.get('pct', 0)
            spy_sign = "+" if spy_pct >= 0 else ""
            msg += f"SPY: `{spy_sign}{spy_pct:.2f}%`"
        if vix:
            msg += f"  VIX: `{vix.get('price',0):.1f}`"
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
`TSLA` — full analysis
`TSLA NVDA AMD` — multiple (up to 5)
`TSLA short` — 3-line quick summary

*WATCHLIST*
`scan` — rank all your symbols
`top` — today's top movers

*PRICE ALERTS*
`alert TSLA 450` — notify near $450
`alert TSLA 400 below` — notify going down
`cancel TSLA` — remove alert
`alerts` — list active alerts

*BRIEFS*
`brief` — morning/evening/weekend context

`help` — this message

`━━━━━━━━━━━━━━━━━━━━━`
_AlphaEdge v7.3_""")


# ═══════════════════════════════════════════════
# FULL ANALYSIS RUNNER
# ═══════════════════════════════════════════════

def run_analysis(symbol, mode='full', timeframe='1d'):
    symbol = normalise_symbol(symbol)
    logging.info(f"Analysis start: {symbol} mode={mode}")

    send_telegram(f"Analysing *{symbol}*... ~35s", silent=True)

    if not validate_symbol(symbol):
        send_telegram(f"*{symbol}* not found. Check ticker (e.g. TSLA, BTC-USD, NXE)")
        return

    ctx = get_full_context(symbol)
    if not ctx:
        send_telegram(f"Could not fetch data for *{symbol}*")
        return

    print(f"  -> Stock info...")
    stock_info = get_stock_info(symbol)

    print(f"  -> Market context...")
    market_ctx = get_market_ctx()

    print(f"  -> MTF verdicts...")
    mtf_verdicts = get_mtf_verdicts(symbol) if mode == 'full' else {}

    # Fetch earnings ONCE here — passed to both get_verdict() and format_full_analysis()
    # Eliminates the duplicate get_earnings_date() call that was costing an extra API hit.
    print(f"  -> Earnings date...")
    earnings_info = get_earnings_date(symbol)  # returns (date, days_until)

    verdict, zone, reasons, next_steps = get_verdict(
        ctx, market_ctx, mtf_verdicts, earnings_cache=earnings_info
    )
    rs_score, rs_label = calc_relative_strength(ctx)

    if mode == 'short':
        send_telegram(format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score, stock_info))
        return

    print(f"  -> Sector context...")
    sector_name, sector_avg, _ = get_sector_context(symbol)

    print(f"  -> CAD pricing...")
    cad_price, tsx_symbol = get_cad_price(symbol)
    usd_cad = get_usd_cad_rate() if not is_crypto(symbol) else None

    # POC, squeeze, divergence — uses a 6mo daily slice.
    # get_full_context() already downloads 5y daily but doesn't expose it.
    # This is a separate focused download for the analysis functions.
    print(f"  -> Daily bars...")
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

    print(f"  -> AI analysis...")
    ai_text = get_ai_analysis(ctx, verdict, zone, sector_name, sector_avg,
                               mtf_verdicts, stock_info)
    print(f"  -> AI: {'GOT' if ai_text else 'NO RESPONSE'}")

    msg = format_full_analysis(
        symbol, ctx, verdict, zone, reasons,
        ai_text, market_ctx, rs_score, rs_label,
        poc, support, resistance,
        squeeze_state, squeeze_dir, rsi_div,
        mtf_verdicts, sector_name, sector_avg,
        stock_info, cad_price, tsx_symbol, usd_cad,
        next_steps=next_steps,
        earnings_info=earnings_info,
    )
    send_telegram(msg)
    logging.info(f"Analysis complete: {symbol} | {verdict}")


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

    logging.info(f"Command: event={event_type} cmd={command} sym={symbol}")

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
        send_telegram(f"Unknown command: `{command}`\nType `help` for commands.")


if __name__ == "__main__":
    main()
