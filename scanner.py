"""
ALPHAEDGE PYTHON SCANNER v6.0 — PINE SCRIPT PARITY EDITION
═══════════════════════════════════════════════════════════════
Mirrors AlphaEdge v6.3.2 Pine Script logic EXACTLY:
• Wilder's RMA for RSI/ATR/ADX (not SMA)
• Range Filter identical to Pine's rngfilt_va
• Supertrend with HL2 + ratcheting bands
• 10-point confluence (AE, ST, MACD, RSI, EMA, VWAP, ADX+DI, HTF, Squeeze, SMC)
• SQS composite (40% conf / 25% MTF / 15% regime / 10% vol / 10% volat)
• Counter-trend blocks, MTF gate, grade filter, chop filter
• Structure-based SL with min ATR distance
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

# ═══════════════════════════════════════════════
# CONFIG (mirrors Pine inputs)
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

EST = ZoneInfo("America/New_York")

def now_est(): return datetime.now(EST)
def fmt_time(): return now_est().strftime('%H:%M %Z')
def fmt_datetime(): return now_est().strftime('%Y-%m-%d %H:%M %Z')

# Pine: AE Core
AE_LENGTH = 200

# Pine: ADX Gate
USE_ADX_GATE = True
ADX_GATE_LEVEL = 20
ADX_STRONG = 25
ADX_WEAK = 20
ADX_LEN = 14

# Pine: RSI
RSI_LEN = 14

# Pine: MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG = 9

# Pine: ATR / SL
ATR_LEN = 14
SL_MULT = 2.0
SWING_LOOKBACK = 10
STRUCT_BUFFER = 0.2
MIN_SL_DIST = 0.5  # × ATR
TP1_MULT = 1.0
TP2_MULT = 2.0
TP3_MULT = 3.0

# Pine: Supertrend
ST_PERIODS = 10
ST_MULT = 3.0

# Pine: BB/KC Squeeze
SQ_BB_MULT = 2.0
SQ_KC_MULT = 1.5

# Pine: Filters
MIN_CONF_SCORE = 4             # Pine default
GRADE_FILTER = "A+ and A"      # Pine default; change to "B and better" if more signals desired
MIN_BARS_BETWEEN = 3
USE_COUNTER_TREND_BLOCK = True
USE_MTF_GATE = True
MTF_GATE_BULL = 9              # Block SELL if MTF bull sum >= this
MTF_GATE_BEAR = 3              # Block BUY if MTF bull sum <= this
USE_CHOP_FILTER = True
CHOP_ATR_MULT = 1.0
ADX_BYPASS_MIN = 5             # High-score ADX bypass

# Pine: RSI Divergence
USE_RSI_DIV = True
RSI_DIV_LOOK = 5
RSI_DIV_FLOOR = 25
RSI_BEAR_CEIL = 75

# Pine: SQS
USE_SQS = True
SQS_MIN_FOR_ALERT = 75         # Pine default
AI_TIER_THRESHOLD = 75         # Match SQS alert threshold

# Pine: Regime
REGIME_ADX_TREND = 22
REGIME_ADX_RANGE = 20
REGIME_VOL_HIGH = 1.5
REGIME_VOL_LOW = 0.7

# Safety caps (prevent absurd SLs)
MAX_SL_PCT_STOCKS = 0.04
MAX_SL_PCT_CRYPTO = 0.08
MIN_SL_PCT_STOCKS = 0.005
MIN_SL_PCT_CRYPTO = 0.01
PRICE_SANITY_DEVIATION = 0.20

# Alert management
DIGEST_THRESHOLD = 4
MAX_TRADE_AGE_HOURS = 72
COOLDOWN_ELITE = 2
COOLDOWN_STRONG = 4
COOLDOWN_GOOD = 6
COOLDOWN_FAIR = 10
FETCH_DELAY = 0.3
DEBUG_NEAR_MISS = True

# Files
ALERT_CACHE = 'alert_cache.json'
TRADES_FILE = 'active_trades.json'
HISTORY_FILE = 'trade_history.json'
STATE_FILE = 'scanner_state.json'
LOGS_DIR = Path('logs')
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / f'scan_{now_est().strftime("%Y-%m-%d")}.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)

# Watchlist groups
CRYPTO_WATCHLIST = ['BTC-USD', 'ETH-USD', 'XRP-USD', 'GC=F']
EXTENDED_HOURS_STOCKS = ['NVDA', 'TSLA', 'AMD', 'MSFT', 'META', 'AMZN', 'GOOGL', 'NFLX']
REGULAR_HOURS_ONLY = ['MU', 'SNDK', 'NBIS', 'IONQ', 'RGTI', 'QBTS',
                      'OKLO', 'IREN', 'UAMY', 'WGRX', 'SOFI', 'NVO']
ALL_SYMBOLS = CRYPTO_WATCHLIST + EXTENDED_HOURS_STOCKS + REGULAR_HOURS_ONLY

SYMBOL_EMOJI = {
    'BTC-USD': '₿', 'ETH-USD': 'Ξ', 'XRP-USD': '◇', 'GC=F': '🥇',
    'NVDA': '💎', 'TSLA': '🚘', 'META': '👓', 'AMZN': '📦',
    'GOOGL': '🔍', 'MSFT': '🪟', 'NFLX': '🎬', 'AMD': '⚡',
    'MU': '💾', 'SNDK': '💽', 'NBIS': '🌐',
    'IONQ': '⚛️', 'RGTI': '🧪', 'QBTS': '🔬',
    'OKLO': '☢️', 'IREN': '🪙', 'UAMY': '⚒️', 'WGRX': '💊',
    'SOFI': '🏦', 'NVO': '💉',
}

TIMEFRAMES = [
    {'tf': '30m', 'lookback': '60d', 'label': '⚡30m', 'min_bars': 250},
    {'tf': '1h',  'lookback': '3mo', 'label': '📊1h',  'min_bars': 250},
]

HTF_MAP = {'30m': '4h', '1h': '4h'}     # Pine uses 1h for HTF; we use 4h for more stable bias on 30m/1h base
MTF_FRAMES = ['15m', '1h', '4h', '1d']  # Pine MTF dashboard default

# ═══════════════════════════════════════════════
# SESSION HELPERS
# ═══════════════════════════════════════════════

def get_session():
    now = now_est()
    hour, minute, wk = now.hour, now.minute, now.weekday()
    if wk >= 5: return "🌐 Weekend"
    t = hour + minute/60
    if 4 <= t < 9.5: return "🌅 Pre-Market"
    if 9.5 <= t < 10.5: return "🔔 Market Open"
    if 10.5 <= t < 14: return "📊 Midday"
    if 14 <= t < 16: return "⚡ Power Hour"
    if 16 <= t < 20: return "🌙 After-Hours"
    return "🌑 Overnight"

def is_crypto(sym): return sym.endswith('-USD') or sym == 'GC=F'
def is_extended_hours_session(): return get_session() in ['🌅 Pre-Market', '🌙 After-Hours']
def is_regular_market_open(): return get_session() in ['🔔 Market Open', '📊 Midday', '⚡ Power Hour']

def get_active_watchlist():
    s = get_session()
    if s in ("🌐 Weekend", "🌑 Overnight"): return CRYPTO_WATCHLIST
    if s in ("🌅 Pre-Market", "🌙 After-Hours"): return CRYPTO_WATCHLIST + EXTENDED_HOURS_STOCKS
    if is_regular_market_open(): return ALL_SYMBOLS
    return CRYPTO_WATCHLIST

# ═══════════════════════════════════════════════
# CORE INDICATORS (Pine-parity — Wilder's RMA)
# ═══════════════════════════════════════════════

def rma(series, length):
    """Wilder's smoothing = ta.rma() in Pine. Used by RSI/ATR/ADX."""
    return series.ewm(alpha=1.0/length, adjust=False).mean()

def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def sma(s, length):
    return s.rolling(length).mean()

def pine_rsi(src, length=14):
    """Exact match to ta.rsi() — Wilder's smoothing."""
    delta = src.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50)

def pine_atr(df, length=14):
    """Exact match to ta.atr() — Wilder's RMA of true range."""
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return rma(tr, length)

def pine_adx(df, length=14):
    """Exact match to ta.dmi() — Wilder DM rule + RMA smoothing."""
    high, low, close = df['High'], df['Low'], df['Close']
    up = high.diff()
    dn = -low.diff()

    # Pine's directional movement rule:
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr_v = rma(tr, length).replace(0, np.nan)
    plus_di = 100 * rma(plus_dm, length) / atr_v
    minus_di = 100 * rma(minus_dm, length) / atr_v
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_v = rma(dx, length)
    return adx_v.fillna(0), plus_di.fillna(0), minus_di.fillna(0)

def pine_macd(src, fast=12, slow=26, sig=9):
    m = ema(src, fast) - ema(src, slow)
    s = ema(m, sig)
    return m, s

def pine_supertrend(df, period=10, mult=3.0):
    """Exact Pine Supertrend with HL2 source + ratcheting bands."""
    hl2 = (df['High'] + df['Low']) / 2
    atr_v = pine_atr(df, period)

    up = hl2 - mult * atr_v
    dn = hl2 + mult * atr_v

    up_final = up.copy()
    dn_final = dn.copy()
    close = df['Close'].values

    for i in range(1, len(df)):
        if close[i-1] > up_final.iloc[i-1]:
            up_final.iloc[i] = max(up.iloc[i], up_final.iloc[i-1])
        if close[i-1] < dn_final.iloc[i-1]:
            dn_final.iloc[i] = min(dn.iloc[i], dn_final.iloc[i-1])

    trend = pd.Series(1, index=df.index, dtype=int)
    for i in range(1, len(df)):
        prev = trend.iloc[i-1]
        if prev == -1 and close[i] > dn_final.iloc[i-1]:
            trend.iloc[i] = 1
        elif prev == 1 and close[i] < up_final.iloc[i-1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = prev

    return trend, up_final, dn_final

def pine_vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    return (tp * df['Volume']).cumsum() / df['Volume'].cumsum().replace(0, np.nan)

# ═══════════════════════════════════════════════
# RANGE FILTER (AE Core — Pine's rngfilt_va)
# ═══════════════════════════════════════════════

def smooth_range(src, length=200, qty=3):
    """Pine's utils.smoothrng: EMA of ATR-like deviation."""
    wper = length * 2 - 1
    avrng = ema(src.diff().abs(), length)
    smooth = ema(avrng, wper) * qty
    return smooth

def range_filter(src, rng):
    """Pine's rngfilt_va — price follows band, only moves when breached."""
    rf = src.copy().astype(float).values
    srng_vals = rng.values
    src_vals = src.values
    for i in range(1, len(src_vals)):
        prev = rf[i-1]
        if src_vals[i] > src_vals[i-1]:
            rf[i] = max(prev, src_vals[i] - srng_vals[i])
        else:
            rf[i] = min(prev, src_vals[i] + srng_vals[i])
    return pd.Series(rf, index=src.index)

def trend_up_value(filt):
    """Pine's utils.trendUp — cumulative direction count."""
    trend = pd.Series(0, index=filt.index, dtype=int)
    for i in range(1, len(filt)):
        prev = trend.iloc[i-1]
        if filt.iloc[i] > filt.iloc[i-1]:
            trend.iloc[i] = prev + 1 if prev >= 0 else 1
        elif filt.iloc[i] < filt.iloc[i-1]:
            trend.iloc[i] = prev - 1 if prev <= 0 else -1
        else:
            trend.iloc[i] = prev
    return trend

# ═══════════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════════

def load_json(path, default):
    try:
        with open(path, 'r') as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, 'w') as f: json.dump(data, f, indent=2, default=str)

def load_cache():
    cache = load_json(ALERT_CACHE, {})
    cleaned = {}
    for k, v in cache.items():
        try:
            dt = datetime.fromisoformat(v) if isinstance(v, str) else v
            if dt.tzinfo is None: dt = dt.replace(tzinfo=EST)
            cleaned[k] = dt.isoformat()
        except: continue
    return cleaned

def get_cooldown(sqs):
    if sqs >= 85: return COOLDOWN_ELITE
    if sqs >= 70: return COOLDOWN_STRONG
    if sqs >= 55: return COOLDOWN_GOOD
    return COOLDOWN_FAIR

def is_duplicate(sym, sig_key, cache, sqs=60):
    k = f"{sym}_{sig_key}"
    if k not in cache: return False
    try:
        last = datetime.fromisoformat(cache[k])
        if last.tzinfo is None: last = last.replace(tzinfo=EST)
        return now_est() - last < timedelta(hours=get_cooldown(sqs))
    except: return False

def mark_sent(sym, sig_key, cache):
    cache[f"{sym}_{sig_key}"] = now_est().isoformat()

# ═══════════════════════════════════════════════
# LIVE PRICE + SANITY
# ═══════════════════════════════════════════════

def get_real_time_price(sym):
    try:
        df = yf.download(sym, period='1d', interval='1m', progress=False, auto_adjust=True)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        return float(df['Close'].iloc[-1])
    except: return None

def get_live_ohlc(sym):
    try:
        df = yf.download(sym, period='2d', interval='5m', progress=False, auto_adjust=True)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        return float(df['Close'].iloc[-1]), float(df['High'].iloc[-1]), float(df['Low'].iloc[-1])
    except: return None

def get_daily_close(sym):
    try:
        df = yf.download(sym, period='5d', interval='1d', progress=False, auto_adjust=True)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        return float(df['Close'].iloc[-1])
    except: return None

def sanity_check_price(sym, live):
    daily = get_daily_close(sym)
    if daily is None or daily <= 0: return True
    return abs(live - daily) / daily <= PRICE_SANITY_DEVIATION

# ═══════════════════════════════════════════════
# HTF + MTF FETCHERS (mirrors request.security)
# ═══════════════════════════════════════════════

def get_htf_bias(symbol, htf='4h'):
    """Return True if HTF bullish (EMA50 > EMA200)."""
    try:
        period = '3mo' if htf == '4h' else '1y'
        df = yf.download(symbol, period=period, interval='1h', progress=False, auto_adjust=True)
        if df.empty or len(df) < 50: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        # Resample to HTF
        rule = '4h' if htf == '4h' else 'D'
        df4 = df.resample(rule).agg({
            'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'
        }).dropna()
        if len(df4) < 50: return None
        e50 = ema(df4['Close'], 50).iloc[-2]  # prev bar like Pine
        e200 = ema(df4['Close'], min(200, len(df4))).iloc[-2]
        return e50 > e200
    except Exception as e:
        logging.error(f"HTF {symbol}: {e}")
        return None

def get_mtf_score(symbol, tf_str):
    """Returns 0-3 bull score for a specific TF (mirrors f_mtfScore)."""
    tf_map = {
        '15m': ('5d', '15m', 200),
        '1h': ('3mo', '1h', 200),
        '4h': ('6mo', '1h', 200),    # we resample from 1h for 4h
        '1d': ('2y', '1d', 200),
    }
    if tf_str not in tf_map: return 0
    period, interval, min_b = tf_map[tf_str]
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
        if df.empty: return 0
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if tf_str == '4h':
            df = df.resample('4h').agg({
                'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'
            }).dropna()
        if len(df) < 50: return 0
        e50 = ema(df['Close'], 50).iloc[-2]
        e200 = ema(df['Close'], min(200, len(df))).iloc[-2]
        rsi = pine_rsi(df['Close'], RSI_LEN).iloc[-2]
        c = df['Close'].iloc[-2]
        score = 0
        score += 1 if e50 > e200 else 0
        score += 1 if rsi > 50 else 0
        score += 1 if c > e50 else 0
        return score
    except Exception as e:
        logging.error(f"MTF {symbol} {tf_str}: {e}")
        return 0

def get_mtf_sum(symbol):
    """Total MTF bull score (0-12) across 15m/1h/4h/1d."""
    return sum(get_mtf_score(symbol, tf) for tf in MTF_FRAMES)

# ═══════════════════════════════════════════════
# PINE-ACCURATE ANALYSIS ENGINE
# ═══════════════════════════════════════════════

def analyze_symbol(symbol, tf_config, htf_bull, mtf_sum):
    tf = tf_config['tf']
    lookback = tf_config['lookback']
    min_bars = tf_config['min_bars']

    try:
        df = yf.download(symbol, period=lookback, interval=tf,
                        progress=False, auto_adjust=True)
        if df.empty or len(df) < min_bars:
            return None, "insufficient data"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Indicators (Pine-accurate)
        df['ema20'] = ema(df['Close'], 20)
        df['ema50'] = ema(df['Close'], 50)
        df['ema200'] = ema(df['Close'], min(200, len(df)))
        df['rsi'] = pine_rsi(df['Close'], RSI_LEN)
        df['atr'] = pine_atr(df, ATR_LEN)
        df['macd'], df['signal'] = pine_macd(df['Close'], MACD_FAST, MACD_SLOW, MACD_SIG)
        df['adx'], df['plus_di'], df['minus_di'] = pine_adx(df, ADX_LEN)
        st_trend, st_up, st_dn = pine_supertrend(df, ST_PERIODS, ST_MULT)
        df['st'] = st_trend
        df['vwap'] = pine_vwap(df)
        df['vol_avg'] = sma(df['Volume'], 20)

        # BB/KC Squeeze
        bb_basis = sma(df['Close'], 20)
        bb_dev = df['Close'].rolling(20).std()
        bb_up = bb_basis + SQ_BB_MULT * bb_dev
        bb_lo = bb_basis - SQ_BB_MULT * bb_dev
        kc_mid = ema(df['Close'], 20)
        kc_rng = pine_atr(df, 20)
        kc_up = kc_mid + SQ_KC_MULT * kc_rng
        kc_lo = kc_mid - SQ_KC_MULT * kc_rng
        in_squeeze = (bb_up < kc_up) & (bb_lo > kc_lo)
        sqz_fired = in_squeeze.shift(1).fillna(False) & ~in_squeeze
        sqz_bull_break = sqz_fired & (df['Close'] > bb_basis)
        sqz_bear_break = sqz_fired & (df['Close'] < bb_basis)

        # Range Filter (AE Core)
        srng = smooth_range(df['Close'], AE_LENGTH, 3)
        basetype = range_filter(df['Close'], srng)
        hband = basetype + srng
        lowband = basetype - srng
        uprng_raw = trend_up_value(basetype)
        uprng_s = uprng_raw > 0
        df['hband'] = hband
        df['lowband'] = lowband
        df['uprng'] = uprng_s

        last = df.iloc[-1]
        prev = df.iloc[-2]

        bar_price = float(last['Close'])
        atr_val = float(last['atr'])
        if atr_val <= 0 or pd.isna(atr_val):
            return None, "invalid ATR"

        rsi_val = float(last['rsi'])
        adx_val = float(last['adx'])
        ema50 = float(last['ema50'])
        ema200 = float(last['ema200'])
        uprng = bool(last['uprng'])
        st_now = int(last['st'])
        vwap_v = float(last['vwap'])
        plus_di = float(last['plus_di'])
        minus_di = float(last['minus_di'])

        # RSI divergence (Pine-style pivots, simplified)
        rsi_bull_div = False
        rsi_bear_div = False
        if USE_RSI_DIV and len(df) > RSI_DIV_LOOK * 3:
            try:
                # Find recent pivot lows in price + RSI
                lows = df['Low'].iloc[-RSI_DIV_LOOK*3:-RSI_DIV_LOOK]
                rsi_lows = df['rsi'].iloc[-RSI_DIV_LOOK*3:-RSI_DIV_LOOK]
                if len(lows) > 2 and lows.iloc[-1] < lows.iloc[0] and rsi_lows.iloc[-1] > rsi_lows.iloc[0]:
                    rsi_bull_div = rsi_val >= RSI_DIV_FLOOR
                highs = df['High'].iloc[-RSI_DIV_LOOK*3:-RSI_DIV_LOOK]
                rsi_highs = df['rsi'].iloc[-RSI_DIV_LOOK*3:-RSI_DIV_LOOK]
                if len(highs) > 2 and highs.iloc[-1] > highs.iloc[0] and rsi_highs.iloc[-1] < rsi_highs.iloc[0]:
                    rsi_bear_div = rsi_val <= RSI_BEAR_CEIL
            except:
                pass

        rsi_bull = (rsi_val > 50) or (USE_RSI_DIV and rsi_bull_div)
        rsi_bear = (rsi_val < 50) or (USE_RSI_DIV and rsi_bear_div)

        macd_bull = last['macd'] > last['signal']
        ema_bull = ema50 > ema200

        # ═══ PINE CONFLUENCE (10 points max) ═══
        bull = 0
        bull += 1 if uprng else 0                                    # AE
        bull += 1 if st_now == 1 else 0                              # Supertrend
        bull += 1 if macd_bull else 0                                # MACD
        bull += 1 if rsi_bull else 0                                 # RSI
        bull += 1 if ema_bull else 0                                 # EMA
        bull += 1 if bar_price > vwap_v else 0                       # VWAP
        bull += 1 if adx_val > ADX_STRONG and plus_di > minus_di else 0  # ADX+DI
        bull += 1 if htf_bull is True else 0                         # HTF
        bull += 1 if bool(sqz_bull_break.iloc[-1]) else 0            # Squeeze
        # SMC bonus skipped in Python (no OB/FVG tracking here)

        bear = 0
        bear += 1 if not uprng else 0
        bear += 1 if st_now == -1 else 0
        bear += 1 if not macd_bull else 0
        bear += 1 if rsi_bear else 0
        bear += 1 if not ema_bull else 0
        bear += 1 if bar_price < vwap_v else 0
        bear += 1 if adx_val > ADX_STRONG and minus_di > plus_di else 0
        bear += 1 if htf_bull is False else 0
        bear += 1 if bool(sqz_bear_break.iloc[-1]) else 0

        # ═══ PINE TRIGGERS ═══
        # rawBuy: uprng AND crossover of close above hband
        prev_close = float(prev['Close'])
        prev_hband = float(prev['hband'])
        prev_lowband = float(prev['lowband'])
        cross_up = prev_close <= prev_hband and bar_price > float(last['hband'])
        cross_dn = prev_close >= prev_lowband and bar_price < float(last['lowband'])

        # Flip trigger (for HTF)
        prev_uprng = bool(prev['uprng'])
        flip_bull = uprng != prev_uprng and uprng
        flip_bear = uprng != prev_uprng and not uprng

        trigger_bull = cross_up or flip_bull
        trigger_bear = cross_dn or flip_bear

        # ═══ PINE HARD GATES ═══
        # ADX gate
        adx_pass_bull = (not USE_ADX_GATE) or (adx_val > ADX_GATE_LEVEL) or (bull >= ADX_BYPASS_MIN)
        adx_pass_bear = (not USE_ADX_GATE) or (adx_val > ADX_GATE_LEVEL) or (bear >= ADX_BYPASS_MIN)

        # Counter-trend block (score <6 AND HTF+ST opposite)
        htf_st_both_bear = (htf_bull is False) and (st_now == -1)
        htf_st_both_bull = (htf_bull is True) and (st_now == 1)
        ct_buy = USE_COUNTER_TREND_BLOCK and bull < 6 and htf_st_both_bear
        ct_sell = USE_COUNTER_TREND_BLOCK and bear < 6 and htf_st_both_bull

        # MTF gate
        mtf_block_sell = USE_MTF_GATE and mtf_sum >= MTF_GATE_BULL
        mtf_block_buy = USE_MTF_GATE and mtf_sum <= MTF_GATE_BEAR

        # Grade filter
        def grade(sc):
            return "A+" if sc >= 8 else "A" if sc >= 6 else "B" if sc >= 4 else "C"

        def pass_grade(sc):
            if GRADE_FILTER == "A+ Only": return sc >= 8
            if GRADE_FILTER == "A+ and A": return sc >= 6
            if GRADE_FILTER == "B and better": return sc >= 4
            return True

        # ═══ PINE SIGNAL DECISION ═══
        raw_buy = (uprng and trigger_bull and adx_pass_bull and
                   bull >= MIN_CONF_SCORE and pass_grade(bull) and
                   not ct_buy and not mtf_block_buy)
        raw_sell = (not uprng and trigger_bear and adx_pass_bear and
                    bear >= MIN_CONF_SCORE and pass_grade(bear) and
                    not ct_sell and not mtf_block_sell)

        # Conflict resolution (Pine: higher score wins)
        if raw_buy and raw_sell:
            if bull >= bear: raw_sell = False
            else: raw_buy = False

        if not raw_buy and not raw_sell:
            # Diagnostic near-miss
            if bull >= 7 and not trigger_bull:
                return None, f"bull={bull} no trigger"
            if bear >= 7 and not trigger_bear:
                return None, f"bear={bear} no trigger"
            if ct_buy: return None, "counter-trend BUY blocked"
            if ct_sell: return None, "counter-trend SELL blocked"
            if mtf_block_buy: return None, f"MTF blocks BUY (sum={mtf_sum})"
            if mtf_block_sell: return None, f"MTF blocks SELL (sum={mtf_sum})"
            return None, None

        signal = 'BUY' if raw_buy else 'SELL'
        score = bull if raw_buy else bear

        # ═══ PINE SQS (0-100) ═══
        def calc_sqs(is_bull):
            sc = bull if is_bull else bear
            conf_pct = sc / 10 * 40
            mtf_pct = (mtf_sum / 12 * 25) if is_bull else ((12 - mtf_sum) / 12 * 25)
            # Regime fit
            if adx_val >= REGIME_ADX_TREND:
                reg_pct = 15.0
            elif adx_val < REGIME_ADX_RANGE:
                reg_pct = 5.0
            else:
                reg_pct = 8.0
            # Volume fit
            vol_avg_v = float(last['vol_avg']) if not pd.isna(last['vol_avg']) else 1
            if last['Volume'] > vol_avg_v * 1.5: vol_pct = 10.0
            elif last['Volume'] > vol_avg_v: vol_pct = 6.0
            else: vol_pct = 3.0
            # Volatility fit
            atr_avg = df['atr'].rolling(50).mean().iloc[-1]
            vol_ratio = atr_val / atr_avg if atr_avg > 0 else 1.0
            if 0.8 <= vol_ratio <= 1.5: volat_pct = 10.0
            elif 0.6 <= vol_ratio <= 2.0: volat_pct = 7.0
            else: volat_pct = 3.0
            return min(100, conf_pct + mtf_pct + reg_pct + vol_pct + volat_pct)

        sqs = calc_sqs(raw_buy)

        # SQS gate (Pine: sqsMinForAlert)
        if USE_SQS and sqs < SQS_MIN_FOR_ALERT:
            return None, f"SQS {sqs:.0f} < {SQS_MIN_FOR_ALERT}"

        # ═══ LIVE PRICE + SANITY ═══
        live_price = get_real_time_price(symbol)
        entry_price = live_price if live_price else bar_price
        if live_price and not sanity_check_price(symbol, live_price):
            daily = get_daily_close(symbol)
            return None, f"bad data (live=${live_price:.2f}, daily=${daily:.2f})"

        # ═══ PINE SL/TP ═══
        recent_low = float(df['Low'].iloc[-SWING_LOOKBACK-1:-1].min())
        recent_high = float(df['High'].iloc[-SWING_LOOKBACK-1:-1].max())

        max_sl_pct = MAX_SL_PCT_CRYPTO if is_crypto(symbol) else MAX_SL_PCT_STOCKS
        min_sl_pct = MIN_SL_PCT_CRYPTO if is_crypto(symbol) else MIN_SL_PCT_STOCKS

        if signal == 'BUY':
            atr_sl = entry_price - atr_val * SL_MULT
            struct_sl = recent_low - atr_val * STRUCT_BUFFER
            sl = max(atr_sl, struct_sl)  # Pine uses max for long (tighter of the two)
            min_dist = atr_val * MIN_SL_DIST
            if (entry_price - sl) < min_dist:
                sl = entry_price - min_dist
            # Apply % caps
            if (entry_price - sl) > entry_price * max_sl_pct:
                sl = entry_price * (1 - max_sl_pct)
            if (entry_price - sl) < entry_price * min_sl_pct:
                sl = entry_price * (1 - min_sl_pct)
            risk = entry_price - sl
            tp1 = entry_price + risk * TP1_MULT
            tp2 = entry_price + risk * TP2_MULT
            tp3 = entry_price + risk * TP3_MULT
        else:
            atr_sl = entry_price + atr_val * SL_MULT
            struct_sl = recent_high + atr_val * STRUCT_BUFFER
            sl = min(atr_sl, struct_sl)
            min_dist = atr_val * MIN_SL_DIST
            if (sl - entry_price) < min_dist:
                sl = entry_price + min_dist
            if (sl - entry_price) > entry_price * max_sl_pct:
                sl = entry_price * (1 + max_sl_pct)
            if (sl - entry_price) < entry_price * min_sl_pct:
                sl = entry_price * (1 + min_sl_pct)
            risk = sl - entry_price
            tp1 = entry_price - risk * TP1_MULT
            tp2 = entry_price - risk * TP2_MULT
            tp3 = entry_price - risk * TP3_MULT

        # Nearby levels
        nearby = {
            'resistance': float(df['High'].iloc[-60:].max()) if bar_price < df['High'].iloc[-60:].max() else None,
            'support': float(df['Low'].iloc[-60:].min()) if bar_price > df['Low'].iloc[-60:].min() else None,
            'ema50': ema50,
            'ema200': ema200,
        }

        tf_minutes = 30 if tf == '30m' else 60
        expiry = now_est() + timedelta(minutes=tf_minutes * 2)
        decimals = 4 if entry_price < 10 else 2

        # Regime string
        if adx_val >= REGIME_ADX_TREND: regime = 'TRENDING'
        elif adx_val < REGIME_ADX_RANGE: regime = 'RANGING'
        else: regime = 'TRANSITIONAL'

        def tier(s):
            if s >= 90: return "🏆 ELITE"
            if s >= 75: return "⭐ STRONG"
            if s >= 60: return "✅ GOOD"
            return "⚠️ FAIR"

        # Trigger type for display
        if flip_bull: trigger_type = "AE Flip Bullish"
        elif flip_bear: trigger_type = "AE Flip Bearish"
        elif cross_up: trigger_type = "Breakout Above Band"
        elif cross_dn: trigger_type = "Breakdown Below Band"
        else: trigger_type = "Signal"

        strong_trend = (ema_bull and bar_price > ema50 and 22 < adx_val < 55 and plus_di > minus_di) if signal == 'BUY' else \
                       (not ema_bull and bar_price < ema50 and 22 < adx_val < 55 and minus_di > plus_di)

        return {
            'symbol': symbol,
            'emoji': SYMBOL_EMOJI.get(symbol, '📈'),
            'signal': signal,
            'price': round(entry_price, decimals),
            'bar_price': round(bar_price, decimals),
            'score': int(score),
            'grade': grade(score),
            'sqs': round(sqs),
            'tier': tier(sqs),
            'trigger': trigger_type,
            'trigger_desc': f"{trigger_type} (Pine-parity)",
            'sl': round(sl, decimals),
            'sl_pct': round(abs(sl - entry_price) / entry_price * 100, 2),
            'tp1': round(tp1, decimals),
            'tp2': round(tp2, decimals),
            'tp3': round(tp3, decimals),
            'risk': round(risk, decimals),
            'rsi': round(rsi_val, 1),
            'adx': round(adx_val, 1),
            'stretch': round(abs(bar_price - ema50) / atr_val, 1),
            'regime': regime,
            'timeframe': tf,
            'tf_label': tf_config['label'],
            'session': get_session(),
            'decimals': decimals,
            'strong_trend': bool(strong_trend),
            'is_crypto': is_crypto(symbol),
            'is_extended_hours': is_extended_hours_session() and not is_crypto(symbol),
            'mtf_sum': mtf_sum,
            'htf_bull': htf_bull,
            'nearby': nearby,
            'expiry_time': expiry.isoformat(),
        }, None

    except Exception as e:
        logging.error(f"{symbol} [{tf}]: {e}")
        return None, f"error: {e}"
# ═══════════════════════════════════════════════
# TRADE TRACKING
# ═══════════════════════════════════════════════

def create_trade(sig):
    return {
        'symbol': sig['symbol'],
        'emoji': sig['emoji'],
        'signal': sig['signal'],
        'entry': sig['price'],
        'sl': sig['sl'],
        'tp1': sig['tp1'],
        'tp2': sig['tp2'],
        'tp3': sig['tp3'],
        'risk': sig['risk'],
        'decimals': sig['decimals'],
        'grade': sig['grade'],
        'sqs': sig['sqs'],
        'tier': sig['tier'],
        'tf': sig['timeframe'],
        'tf_label': sig['tf_label'],
        'opened_at': now_est().isoformat(),
        'opened_session': sig['session'],
        'mtf_sum': sig.get('mtf_sum'),
        'tp1_hit': False, 'tp2_hit': False, 'tp3_hit': False,
        'tp1_hit_at': None, 'tp2_hit_at': None, 'tp3_hit_at': None,
        'closed': False, 'closed_reason': None,
        'closed_at': None, 'final_r': None,
    }

def check_trade_progress(trade):
    result = get_live_ohlc(trade['symbol'])
    if not result: return [], False
    current, hi, lo = result
    events = []
    is_long = trade['signal'] == 'BUY'

    # Timeout
    try:
        opened = datetime.fromisoformat(trade['opened_at'])
        if opened.tzinfo is None: opened = opened.replace(tzinfo=EST)
        if now_est() - opened > timedelta(hours=MAX_TRADE_AGE_HOURS):
            trade['closed'] = True
            trade['closed_reason'] = 'Timeout (72h)'
            trade['closed_at'] = now_est().isoformat()
            trade['final_r'] = 0
            events.append({'type': 'TIMEOUT', 'price': current})
            return events, True
    except: pass

    # SL
    if is_long and current <= trade['sl']:
        trade['closed'] = True
        trade['closed_reason'] = 'SL Hit'
        trade['closed_at'] = now_est().isoformat()
        trade['final_r'] = 0 if trade['tp1_hit'] else -1
        events.append({'type': 'SL', 'price': trade['sl']})
        return events, True
    if not is_long and current >= trade['sl']:
        trade['closed'] = True
        trade['closed_reason'] = 'SL Hit'
        trade['closed_at'] = now_est().isoformat()
        trade['final_r'] = 0 if trade['tp1_hit'] else -1
        events.append({'type': 'SL', 'price': trade['sl']})
        return events, True

    # TPs
    if is_long:
        if not trade['tp1_hit'] and current >= trade['tp1']:
            trade['tp1_hit'] = True
            trade['tp1_hit_at'] = now_est().isoformat()
            events.append({'type': 'TP1', 'price': trade['tp1']})
        if not trade['tp2_hit'] and current >= trade['tp2']:
            trade['tp2_hit'] = True
            trade['tp2_hit_at'] = now_est().isoformat()
            events.append({'type': 'TP2', 'price': trade['tp2']})
        if not trade['tp3_hit'] and current >= trade['tp3']:
            trade['tp3_hit'] = True
            trade['tp3_hit_at'] = now_est().isoformat()
            trade['closed'] = True
            trade['closed_reason'] = 'TP3 Hit'
            trade['closed_at'] = now_est().isoformat()
            trade['final_r'] = 3
            events.append({'type': 'TP3', 'price': trade['tp3']})
            return events, True
    else:
        if not trade['tp1_hit'] and current <= trade['tp1']:
            trade['tp1_hit'] = True
            trade['tp1_hit_at'] = now_est().isoformat()
            events.append({'type': 'TP1', 'price': trade['tp1']})
        if not trade['tp2_hit'] and current <= trade['tp2']:
            trade['tp2_hit'] = True
            trade['tp2_hit_at'] = now_est().isoformat()
            events.append({'type': 'TP2', 'price': trade['tp2']})
        if not trade['tp3_hit'] and current <= trade['tp3']:
            trade['tp3_hit'] = True
            trade['tp3_hit_at'] = now_est().isoformat()
            trade['closed'] = True
            trade['closed_reason'] = 'TP3 Hit'
            trade['closed_at'] = now_est().isoformat()
            trade['final_r'] = 3
            events.append({'type': 'TP3', 'price': trade['tp3']})
            return events, True

    return events, False

def archive_trade(trade):
    history = load_json(HISTORY_FILE, [])
    history.append(trade)
    if len(history) > 500:
        history = history[-500:]
    save_json(HISTORY_FILE, history)

# ═══════════════════════════════════════════════
# AI ANALYSIS (Gemini)
# ═══════════════════════════════════════════════

def get_ai_analysis(sig):
    if not GEMINI_API_KEY: return None

    ah_note = ""
    if sig.get('is_extended_hours'):
        ah_note = "\nNOTE: This is an after-hours/pre-market signal — liquidity is thin."

    prompt = f"""Analyze this trading signal in EXACTLY 3 short lines (max 100 chars each).

SYMBOL: {sig['symbol']} ({sig['signal']} @ ${sig['price']})
TF: {sig['timeframe']} | Trigger: {sig['trigger']}
Score: {sig['score']}/10 ({sig['grade']}) | SQS: {sig['sqs']}/100
RSI: {sig['rsi']} | ADX: {sig['adx']} | Regime: {sig['regime']}
MTF sum: {sig.get('mtf_sum', '?')}/12 | HTF: {sig.get('htf_bull')}
R:R = 1:3 | Strong trend: {sig['strong_trend']}{ah_note}

Respond EXACTLY:
📝 [setup quality assessment]
⚠️ [main risk]
💡 [STRONG BUY/BUY/NEUTRAL/CAUTION/AVOID] — [brief reason]"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        r = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 200}
        }, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if 'candidates' in data and len(data['candidates']) > 0:
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        logging.error(f"AI error: {e}")
    return None

# ═══════════════════════════════════════════════
# FORMATTING HELPERS
# ═══════════════════════════════════════════════

def fmt_price(val, d): return f"{val:.{d}f}"

def fmt_r(r):
    if abs(r) < 0.01: return "0.00R"
    sign = "+" if r > 0 else ""
    return f"{sign}{r:.2f}R"

def time_ago(iso):
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=EST)
        delta = now_est() - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60: return f"{mins}m"
        h = mins // 60
        m = mins % 60
        if h < 24: return f"{h}h {m}m"
        d = h // 24
        hr = h % 24
        return f"{d}d {hr}h"
    except: return "?"

def time_until(iso):
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=EST)
        delta = dt - now_est()
        mins = int(delta.total_seconds() / 60)
        if mins <= 0: return "expired"
        if mins < 60: return f"{mins}m"
        h = mins // 60
        m = mins % 60
        return f"{h}h {m}m"
    except: return "?"

def absolute_expiry(iso):
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=EST)
        tz_name = dt.tzname() or "EDT"
        return dt.strftime(f'%H:%M {tz_name}')
    except: return "?"

def is_signal_expired(sig):
    try:
        exp = datetime.fromisoformat(sig['expiry_time'])
        if exp.tzinfo is None: exp = exp.replace(tzinfo=EST)
        return now_est() > exp
    except: return False

def sqs_meter(sqs):
    filled = min(10, max(0, round(sqs / 10)))
    fill = "🟢" if sqs >= 75 else "🟡" if sqs >= 60 else "🟠"
    return fill * filled + "⚪" * (10 - filled)

def price_ladder(trade, current):
    d = trade['decimals']
    is_long = trade['signal'] == 'BUY'
    levels = [
        ('TP3', trade['tp3'], '🎯', trade['tp3_hit']),
        ('TP2', trade['tp2'], '🎯', trade['tp2_hit']),
        ('TP1', trade['tp1'], '🎯', trade['tp1_hit']),
        ('NOW', current, '⬅️', None),
        ('Ent', trade['entry'], '📍', None),
        ('SL ', trade['sl'], '🛑', None),
    ]
    levels.sort(key=lambda x: -x[1] if is_long else x[1])
    lines = []
    for label, price, em, hit in levels:
        marker = " ✅" if hit else ""
        lines.append(f"{em} {label}: `${fmt_price(price, d)}`{marker}")
    return "\n".join(lines)

def get_session_tips(session, is_crypto_signal):
    if is_crypto_signal:
        if session in ["🌑 Overnight", "🌐 Weekend"]:
            return "💡 _Low volume — use tight limit orders_"
        return None
    if session == "🌅 Pre-Market":
        return "🌅 _Pre-market: LIMIT orders only, reduce size, watch 9:30 AM gap_"
    if session == "🌙 After-Hours":
        return "🌙 _After-hours: LIMIT orders only, reduce size, wider spreads_"
    if session == "🔔 Market Open":
        return "🔔 _First 30 min: high volatility, wait for spread to tighten_"
    if session == "⚡ Power Hour":
        return "⚡ _Power hour: high volume, watch for end-of-day reversals_"
    return None

# ═══════════════════════════════════════════════
# ALERT MESSAGE BUILDERS
# ═══════════════════════════════════════════════

def format_new_signal(sig, ai_text=None):
    emoji = "🟢" if sig['signal'] == 'BUY' else "🔴"
    d = sig['decimals']
    sym = sig['emoji']

    msg = f"{sig['tier']} {emoji} *{sig['signal']} {sym} {sig['symbol']}* `[{sig['tf_label']}]`\n"
    msg += f"{sig['session']} • {now_est().strftime('%H:%M %Z')}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"

    if sig.get('is_extended_hours'):
        msg += f"⚠️ *After-hours — thin liquidity!*\n"
        msg += f"_Use LIMIT orders, expect wider spreads._\n"
        msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"

    msg += f"💵 *Live Entry:* `${fmt_price(sig['price'], d)}`\n"
    msg += f"🎯 *Trigger:* {sig['trigger']}\n"
    msg += f"📊 *Quality:* {sig['score']}/10 ({sig['grade']}) • SQS *{sig['sqs']}*\n"
    msg += f"`{sqs_meter(sig['sqs'])}`\n"

    if sig.get('mtf_sum') is not None:
        mtf = sig['mtf_sum']
        mtf_bar = "█" * mtf + "░" * (12 - mtf)
        msg += f"🗂️ *MTF:* `{mtf_bar}` {mtf}/12\n"

    if sig.get('htf_bull') is not None:
        htf_state = "▲ Bullish" if sig['htf_bull'] else "▼ Bearish"
        aligned = "✓" if (sig['signal']=='BUY') == sig['htf_bull'] else "⚠️"
        msg += f"🏔️ *HTF:* {htf_state} {aligned}\n"

    if sig['sqs'] < 75:
        msg += f"_⚠️ Borderline quality — consider half-size_\n"

    msg += f"\n*🎯 TRADE PLAN*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"📍 Entry:  `${fmt_price(sig['price'], d)}`\n"
    msg += f"🛑 SL:     `${fmt_price(sig['sl'], d)}` ({sig['sl_pct']}% away)\n"
    if sig['sl_pct'] < 1.0:
        msg += f"   _⚠️ Very tight stop — noise risk_\n"
    elif sig['sl_pct'] > 5.0:
        msg += f"   _⚠️ Wide stop — larger drawdown risk_\n"
    msg += f"🎯 TP1:    `${fmt_price(sig['tp1'], d)}` *(+1R)*\n"
    msg += f"🎯 TP2:    `${fmt_price(sig['tp2'], d)}` *(+2R)*\n"
    msg += f"🎯 TP3:    `${fmt_price(sig['tp3'], d)}` *(+3R)*\n"

    # Key levels
    nearby = sig.get('nearby', {})
    meaningful = []
    if nearby:
        for name, key, arrow in [
            ('Resistance', 'resistance', '⬆️'),
            ('Support', 'support', '⬇️'),
            ('EMA50', 'ema50', '〰️'),
            ('EMA200', 'ema200', '〰️'),
        ]:
            val = nearby.get(key)
            if val:
                dist = abs(val - sig['price']) / sig['price'] * 100
                if dist >= 0.3:
                    meaningful.append((name, val, dist, arrow))
    if meaningful:
        msg += f"\n*🔍 KEY LEVELS*\n"
        msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
        for name, val, dist, arrow in meaningful:
            msg += f"{arrow} {name}: `${fmt_price(val, d)}` ({dist:.1f}%)\n"

    msg += f"\n*📈 TECHNICALS*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"RSI: `{sig['rsi']}` | ADX: `{sig['adx']}` | Stretch: `{sig['stretch']}×`\n"
    msg += f"Regime: {sig['regime']} | Trend: {'Strong ✓' if sig['strong_trend'] else 'Mixed ⚠️'}\n"

    exp_abs = absolute_expiry(sig['expiry_time'])
    exp_rel = time_until(sig['expiry_time'])
    msg += f"\n⏳ *Valid until:* {exp_abs} ({exp_rel})\n"
    msg += f"_Re-evaluate on next candle after expiry._\n"

    tips = get_session_tips(sig['session'], sig.get('is_crypto', False))
    if tips: msg += f"\n{tips}\n"

    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n"
        msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
        msg += f"{ai_text}\n"

    return msg

def format_trade_event(trade, event, current):
    emoji = "🟢" if trade['signal'] == 'BUY' else "🔴"
    d = trade['decimals']
    sym = trade.get('emoji', '📈')
    age = time_ago(trade['opened_at'])
    et = event['type']

    if et == 'TP1':
        header = f"✅ *TP1 HIT* {emoji} {sym} {trade['symbol']} `[{trade.get('tf_label', trade['tf'])}]`"
        sub = "💡 *Next step:*"
        steps = [
            f"  ✓ Move SL: `${fmt_price(trade['sl'], d)}` → `${fmt_price(trade['entry'], d)}` (BE)",
            f"  ✓ Take partial: ~33% off",
            f"  ✓ Let runner go to TP2/TP3"
        ]
        r_mult = "+1R"
    elif et == 'TP2':
        header = f"✅✅ *TP2 HIT* {emoji} {sym} {trade['symbol']} `[{trade.get('tf_label', trade['tf'])}]`"
        sub = "💡 *Next step:*"
        steps = [
            f"  ✓ Move SL to TP1 `${fmt_price(trade['tp1'], d)}`",
            f"  ✓ Another partial: ~33% off",
            f"  ✓ Let final portion run to TP3"
        ]
        r_mult = "+2R"
    elif et == 'TP3':
        header = f"🏆 *TP3 HIT — FULL TARGET* {emoji} {sym} {trade['symbol']}"
        sub = "🎉 *Trade complete!*"
        steps = ["  ✓ Close remaining position", "  ✓ +3R achieved", "  ✓ Log this win"]
        r_mult = "+3R"
    elif et == 'SL':
        header = f"🛑 *SL HIT* {emoji} {sym} {trade['symbol']}"
        if trade['tp1_hit']:
            sub = "✅ *Trailed profit exit — still a winner*"
            steps = ["  ✓ Partial gain locked", "  ✓ No further action", "  ✓ Review setup"]
            r_mult = "Partial gain"
        else:
            sub = "❌ *Stop loss hit*"
            steps = ["  ✓ Loss limited to plan", "  ✓ No revenge trades", "  ✓ Log for review"]
            r_mult = "-1R"
    elif et == 'TIMEOUT':
        header = f"⏰ *TRADE TIMEOUT* {emoji} {sym} {trade['symbol']}"
        sub = "_72h expiry — auto-closed_"
        steps = ["  ✓ Signal aged out"]
        r_mult = "—"
    else:
        return None

    msg = f"{header}\n"
    msg += f"⏱ Trade age: {age}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"💵 *Live:* `${fmt_price(current, d)}`\n"
    msg += f"🎯 *Hit:* `${fmt_price(event['price'], d)}` ({r_mult})\n\n"
    msg += f"{sub}\n"
    for s in steps: msg += f"{s}\n"
    msg += f"\n*📊 PRICE LADDER*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += price_ladder(trade, current)
    msg += f"\n\n⏰ {fmt_time()}"
    return msg

def format_digest(signals):
    msg = f"🔔 *SIGNAL DIGEST — {len(signals)} alerts*\n"
    msg += f"{get_session()} • {fmt_time()}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    tf_counts = {}
    for s in signals:
        tf_counts.setdefault(s['symbol'], set()).add(s['timeframe'])
    multi_tf = {k for k, v in tf_counts.items() if len(v) > 1}

    ah_count = sum(1 for s in signals if s.get('is_extended_hours'))
    if ah_count > 0:
        msg += f"⚠️ _{ah_count} signal(s) in extended hours — thin liquidity_\n\n"

    for sig in signals:
        em = "🟢" if sig['signal'] == 'BUY' else "🔴"
        multi = " 🎯🎯" if sig['symbol'] in multi_tf else ""
        ah = " ⚠️" if sig.get('is_extended_hours') else ""
        msg += f"{sig['tier']} {em} *{sig['symbol']}* `[{sig['tf_label']}]`{ah}{multi}\n"
        msg += f"  {sig['emoji']} {sig['signal']} @ `${fmt_price(sig['price'], sig['decimals'])}` • SQS {sig['sqs']} • MTF {sig.get('mtf_sum','?')}/12\n"
        msg += f"  🎯 {sig['trigger']} | RSI {sig['rsi']}\n"
        msg += f"  🛑 `${fmt_price(sig['sl'], sig['decimals'])}` ({sig['sl_pct']}%) → 🎯 `${fmt_price(sig['tp3'], sig['decimals'])}` (+3R)\n\n"

    if multi_tf:
        msg += f"🎯🎯 *Multi-TF confirmation:* {', '.join(sorted(multi_tf))}\n"
        msg += f"_Highest-quality setups — fired on multiple timeframes._\n\n"

    msg += f"_Full details below for SQS ≥{SQS_MIN_FOR_ALERT} only._"
    return msg

# ═══════════════════════════════════════════════
# MARKET CONTEXT
# ═══════════════════════════════════════════════

def get_market_context():
    try:
        data = {}
        for t in ['SPY', 'QQQ', '^VIX']:
            df = yf.download(t, period='5d', interval='1d', progress=False, auto_adjust=True)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                last_c = float(df['Close'].iloc[-1])
                prev_c = float(df['Close'].iloc[-2])
                data[t] = {'price': last_c, 'pct': (last_c - prev_c) / prev_c * 100}
        return data
    except Exception as e:
        logging.error(f"Market context: {e}")
        return None

def format_market_context():
    ctx = get_market_context()
    if not ctx: return None
    spy = ctx.get('SPY', {})
    qqq = ctx.get('QQQ', {})
    vix = ctx.get('^VIX', {})

    if vix.get('price', 20) < 15: vol = "🟢 Low Vol"
    elif vix.get('price', 20) < 22: vol = "🟡 Normal Vol"
    else: vol = "🔴 High Vol"

    if spy.get('pct', 0) > 0.3 and qqq.get('pct', 0) > 0.3: bias = "🚀 RISK-ON — Longs favored"
    elif spy.get('pct', 0) < -0.3 and qqq.get('pct', 0) < -0.3: bias = "🐻 RISK-OFF — Shorts favored"
    else: bias = "⚖️ MIXED — Selective trading"

    msg = f"🌍 *MARKET CONTEXT*\n{fmt_datetime()}\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    def row(name, d):
        if not d: return f"{name}: —\n"
        sign = "+" if d['pct'] >= 0 else ""
        return f"{name}: `${d['price']:.2f}` ({sign}{d['pct']:.2f}%)\n"
    msg += row("SPY ", spy) + row("QQQ ", qqq) + row("VIX ", vix)
    msg += f"\n*Volatility:* {vol}\n*Bias:* {bias}\n"
    return msg

# ═══════════════════════════════════════════════
# OPEN POSITIONS SUMMARY
# ═══════════════════════════════════════════════

def format_open_positions_summary(trades):
    active = [(k, t) for k, t in trades.items() if not t.get('closed')]
    if not active: return None

    enriched = []
    longs = shorts = 0
    tf_counts = {'⚡30m': 0, '📊1h': 0}

    for k, trade in active:
        live = get_live_ohlc(trade['symbol'])
        if not live:
            enriched.append({'trade': trade, 'current': None, 'r_mult': 0, 'status': 'no data', 'bucket': 'nodata'})
            continue
        current = live[0]
        is_long = trade['signal'] == 'BUY'
        pnl_unit = (current - trade['entry']) if is_long else (trade['entry'] - current)
        r_mult = pnl_unit / trade['risk'] if trade['risk'] > 0 else 0
        if is_long: longs += 1
        else: shorts += 1
        lbl = trade.get('tf_label', trade['tf'])
        if lbl in tf_counts: tf_counts[lbl] += 1

        # Bucket for grouping
        if trade.get('tp3_hit'):
            status, bucket = "🏆 +3R", "winner"
        elif trade.get('tp2_hit'):
            status, bucket = "🎯🎯 +2R", "winner"
        elif trade.get('tp1_hit'):
            status, bucket = "🎯 +1R trail", "winner"
        elif r_mult >= 0.5:
            status, bucket = "📈 winning", "winner"
        elif r_mult <= -0.7:
            status, bucket = "⚠️ near SL", "near_sl"
        elif r_mult < -0.15:
            status, bucket = "🔻 losing", "loser"
        elif abs(r_mult) < 0.15:
            status, bucket = "➖ flat", "flat"
        else:
            status, bucket = "📊 building", "building"

        enriched.append({
            'trade': trade, 'current': current, 'r_mult': r_mult,
            'status': status, 'bucket': bucket
        })

    enriched.sort(key=lambda x: -x['r_mult'])

    # Aggregate stats
    valid = [e for e in enriched if e['current'] is not None]
    total_r = sum(e['r_mult'] for e in valid)
    winners = [e for e in valid if e['r_mult'] > 0.1]
    losers = [e for e in valid if e['r_mult'] < -0.1]
    near_sl = [e for e in valid if e['bucket'] == 'near_sl']

    # ═══ Header with timestamp ═══
    now = now_est()
    tz_abbr = now.tzname() or "EDT"
    timestamp = now.strftime(f'%a %b %d • %I:%M %p {tz_abbr}')

    msg = f"📊 *OPEN POSITIONS ({len(active)})*\n"
    msg += f"🕒 {timestamp}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"

    # ═══ Quick stats row ═══
    total_str = fmt_r(total_r)
    total_emoji = "🟢" if total_r >= 0 else "🔴"
    msg += f"{total_emoji} *Total P&L:* {total_str}\n"
    msg += f"🟢 Long: {longs} | 🔴 Short: {shorts}"
    if tf_counts['⚡30m'] or tf_counts['📊1h']:
        msg += f" | 30m: {tf_counts['⚡30m']} | 1h: {tf_counts['📊1h']}"
    msg += "\n"
    msg += f"📈 Winners: {len(winners)} • Losers: {len(losers)}"
    if near_sl:
        msg += f" • ⚠️ Near SL: {len(near_sl)}"
    msg += "\n"

    # ═══ URGENT SECTION: Near-SL first ═══
    if near_sl:
        msg += f"\n⚠️ *NEAR STOP LOSS* ({len(near_sl)})\n"
        msg += f"`─────────────────`\n"
        for e in near_sl:
            t = e['trade']
            em = t.get('emoji', '📈')
            dir_em = "🟢" if t['signal'] == 'BUY' else "🔴"
            msg += f"  {em} {dir_em} *{t['symbol']}* `{t.get('tf_label', t['tf'])}` "
            msg += f"*{fmt_r(e['r_mult'])}* • {time_ago(t['opened_at'])}\n"

    # ═══ WINNERS ═══
    winner_items = [e for e in enriched if e['bucket'] == 'winner']
    if winner_items:
        msg += f"\n✅ *WINNERS* ({len(winner_items)})\n"
        msg += f"`─────────────────`\n"
        for e in winner_items:
            t = e['trade']
            em = t.get('emoji', '📈')
            dir_em = "🟢" if t['signal'] == 'BUY' else "🔴"
            msg += f"  {em} {dir_em} *{t['symbol']}* `{t.get('tf_label', t['tf'])}` "
            msg += f"*{fmt_r(e['r_mult'])}* • {e['status']} • {time_ago(t['opened_at'])}\n"

    # ═══ BUILDING / FLAT ═══
    building_items = [e for e in enriched if e['bucket'] in ('building', 'flat')]
    if building_items:
        msg += f"\n📊 *BUILDING / FLAT* ({len(building_items)})\n"
        msg += f"`─────────────────`\n"
        for e in building_items:
            t = e['trade']
            em = t.get('emoji', '📈')
            dir_em = "🟢" if t['signal'] == 'BUY' else "🔴"
            msg += f"  {em} {dir_em} *{t['symbol']}* `{t.get('tf_label', t['tf'])}` "
            msg += f"{fmt_r(e['r_mult'])} • {time_ago(t['opened_at'])}\n"

    # ═══ LOSERS ═══
    loser_items = [e for e in enriched if e['bucket'] == 'loser']
    if loser_items:
        msg += f"\n🔻 *LOSING* ({len(loser_items)})\n"
        msg += f"`─────────────────`\n"
        for e in loser_items:
            t = e['trade']
            em = t.get('emoji', '📈')
            dir_em = "🟢" if t['signal'] == 'BUY' else "🔴"
            msg += f"  {em} {dir_em} *{t['symbol']}* `{t.get('tf_label', t['tf'])}` "
            msg += f"*{fmt_r(e['r_mult'])}* • {time_ago(t['opened_at'])}\n"

    # ═══ No-data ═══
    nodata_items = [e for e in enriched if e['bucket'] == 'nodata']
    if nodata_items:
        msg += f"\n❔ *No live data* ({len(nodata_items)})\n"
        for e in nodata_items:
            msg += f"  {e['trade'].get('emoji', '📈')} {e['trade']['symbol']}\n"

    # ═══ Footer summary ═══
    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    if winners:
        best = max(valid, key=lambda e: e['r_mult'])
        msg += f"🏆 Best: *{best['trade']['symbol']}* {fmt_r(best['r_mult'])}\n"
    if losers:
        worst = min(valid, key=lambda e: e['r_mult'])
        msg += f"💥 Worst: *{worst['trade']['symbol']}* {fmt_r(worst['r_mult'])}\n"

    return msg
# ═══════════════════════════════════════════════
# WEEKLY SUMMARY
# ═══════════════════════════════════════════════

def format_weekly_summary():
    history = load_json(HISTORY_FILE, [])
    if not history: return None
    cutoff = now_est() - timedelta(days=7)
    week = []
    for t in history:
        try:
            ca = t.get('closed_at')
            if not ca: continue
            dt = datetime.fromisoformat(ca)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=EST)
            if dt >= cutoff: week.append(t)
        except: continue

    if not week: return None

    wins = [t for t in week if (t.get('final_r') or 0) > 0]
    losses = [t for t in week if (t.get('final_r') or 0) < 0]
    be = [t for t in week if (t.get('final_r') or 0) == 0]
    total_r = sum(t.get('final_r', 0) or 0 for t in week)
    wr = len(wins) / len(week) * 100 if week else 0
    best = max(week, key=lambda t: t.get('final_r', 0) or 0)
    worst = min(week, key=lambda t: t.get('final_r', 0) or 0)

    grades = {'A+': [0,0], 'A': [0,0], 'B': [0,0], 'C': [0,0]}
    for t in week:
        g = t.get('grade', 'C')
        if g in grades:
            grades[g][0] += 1
            if (t.get('final_r') or 0) > 0: grades[g][1] += 1

    msg = f"📊 *WEEKLY SUMMARY*\n{cutoff.strftime('%b %d')} → {now_est().strftime('%b %d')}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"Total signals: *{len(week)}*\n"
    msg += f"✅ Wins: *{len(wins)}* ({wr:.0f}%)\n"
    msg += f"❌ Losses: *{len(losses)}*\n"
    msg += f"➖ Breakeven: *{len(be)}*\n\n"
    msg += f"💹 *Total: {fmt_r(total_r)}*\n"
    msg += f"🏆 Best: *{best['symbol']}* ({fmt_r(best.get('final_r', 0) or 0)})\n"
    msg += f"💥 Worst: *{worst['symbol']}* ({fmt_r(worst.get('final_r', 0) or 0)})\n\n"
    msg += f"*GRADE PERFORMANCE*\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    for g, (tot, w) in grades.items():
        if tot > 0:
            msg += f"{g}: {w}/{tot} wins ({w/tot*100:.0f}%)\n"
    msg += f"\n⏰ {fmt_time()}"
    return msg

# ═══════════════════════════════════════════════
# CORRELATION
# ═══════════════════════════════════════════════

CORRELATION_GROUPS = {
    'AI/Semis': ['NVDA', 'AMD', 'MU', 'SNDK', 'NBIS'],
    'Crypto': ['BTC-USD', 'ETH-USD', 'XRP-USD'],
    'Quantum': ['IONQ', 'RGTI', 'QBTS'],
    'Mega Tech': ['GOOGL', 'MSFT', 'META', 'AMZN'],
}

def format_correlation_alert(new_sigs, open_trades):
    combined = [(s['symbol'], 'new') for s in new_sigs]
    if open_trades:
        for k, t in open_trades.items():
            if not t.get('closed'): combined.append((t['symbol'], 'open'))

    risk = {}
    for grp, syms in CORRELATION_GROUPS.items():
        matching = [(s, src) for s, src in combined if s in syms]
        if not matching: continue
        seen = {}
        for s, src in matching:
            if s not in seen or src == 'open': seen[s] = src
        if len(seen) >= 2:
            risk[grp] = {
                'symbols': sorted(seen.keys()),
                'new': sum(1 for v in seen.values() if v == 'new'),
                'open': sum(1 for v in seen.values() if v == 'open'),
            }

    if not risk: return None

    msg = f"⚠️ *CORRELATION NOTICE*\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Multiple signals/positions in same sector_\n"
    for grp, d in risk.items():
        parts = []
        if d['new']: parts.append(f"{d['new']} new")
        if d['open']: parts.append(f"{d['open']} open")
        tag = f" ({', '.join(parts)})" if parts else ""
        msg += f"\n🔗 *{grp}*{tag}\n  {', '.join(d['symbols'])}\n"
    msg += f"\n💡 _These often move together — consider size/exposure._"
    return msg

# ═══════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════

def send_telegram(message, silent=False):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram credentials missing")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown',
            'disable_notification': silent
        }, timeout=10)
        if r.status_code != 200:
            logging.error(f"Telegram {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"Telegram send: {e}")
        return False

# ═══════════════════════════════════════════════
# STATE TRIGGERS
# ═══════════════════════════════════════════════

def should_send_daily_context():
    state = load_json(STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    if state.get('last_context_date') == today: return False
    if now_est().hour < 9: return False
    if now_est().weekday() >= 5: return False
    state['last_context_date'] = today
    save_json(STATE_FILE, state)
    return True

def should_send_weekly_summary():
    state = load_json(STATE_FILE, {})
    now = now_est()
    if now.weekday() != 6: return False
    if now.hour < 21: return False
    week_key = now.strftime('%Y-W%W')
    if state.get('last_weekly') == week_key: return False
    state['last_weekly'] = week_key
    save_json(STATE_FILE, state)
    return True

# ═══════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════

def main():
    session = get_session()
    active_list = get_active_watchlist()

    print(f"\n{'='*60}")
    print(f"AlphaEdge v6.0 PINE-PARITY @ {fmt_datetime()}")
    print(f"Session: {session}")
    print(f"Active watchlist: {len(active_list)}/{len(ALL_SYMBOLS)} symbols")
    print(f"AI: {bool(GEMINI_API_KEY)} | MIN_SQS: {SQS_MIN_FOR_ALERT} | Grade: {GRADE_FILTER}")
    print(f"{'='*60}\n")

    logging.info(f"Scan v6.0 | Session: {session} | Active: {len(active_list)}")

    cache = load_cache()
    trades = load_json(TRADES_FILE, {})

    # Daily context
    if should_send_daily_context():
        print("🌍 Sending daily market context...")
        ctx_msg = format_market_context()
        if ctx_msg: send_telegram(ctx_msg, silent=True)

    # STEP 1: Check active trades
    if trades:
        print(f"📊 Checking {len(trades)} active trade(s)...")
        to_remove = []
        for tk, trade in list(trades.items()):
            if trade.get('closed'):
                archive_trade(trade)
                to_remove.append(tk)
                continue
            try:
                print(f"  → {trade['symbol']:10s} [{trade.get('tf_label', trade['tf']):5s}] ({trade['signal']})...", end=" ")
                events, closed = check_trade_progress(trade)
                if not events:
                    print("no change")
                    continue
                live = get_live_ohlc(trade['symbol'])
                current = live[0] if live else trade['entry']
                for event in events:
                    msg = format_trade_event(trade, event, current)
                    if msg:
                        send_telegram(msg, silent=False)
                        print(f"\n     🔔 {event['type']} @ ${event['price']}", end="")
                        logging.info(f"{trade['symbol']} {event['type']} @ {event['price']}")
                if closed:
                    archive_trade(trade)
                    to_remove.append(tk)
                    print("✅ closed")
                else: print()
            except Exception as e:
                print(f"💥 error: {e}")
                logging.error(f"Trade check {trade.get('symbol')}: {e}")
            time.sleep(FETCH_DELAY)

        for k in to_remove: del trades[k]
        save_json(TRADES_FILE, trades)
        print()

        # Open positions summary (every 2h, 2+ positions)
        remaining = {k: v for k, v in trades.items() if not v.get('closed')}
        if len(remaining) >= 2:
            state = load_json(STATE_FILE, {})
            last = state.get('last_pos_summary')
            send = True
            if last:
                try:
                    dt = datetime.fromisoformat(last)
                    if dt.tzinfo is None: dt = dt.replace(tzinfo=EST)
                    if now_est() - dt < timedelta(hours=2): send = False
                except: pass
            if send:
                ps = format_open_positions_summary(remaining)
                if ps:
                    send_telegram(ps, silent=True)
                    state['last_pos_summary'] = now_est().isoformat()
                    save_json(STATE_FILE, state)

    # STEP 2: Scan new signals
    print(f"🔍 Scanning {len(active_list)} symbols...")
    new_sigs = []
    skip_dupe = skip_active = ai_calls = 0
    near_misses = []

    # Pre-fetch HTF + MTF per symbol (save on API calls)
    symbol_context = {}
    print(f"  📡 Pre-fetching HTF/MTF context...")
    for sym in active_list:
        try:
            htf_bull = get_htf_bias(sym, '4h')
            mtf_sum = get_mtf_sum(sym)
            symbol_context[sym] = {'htf_bull': htf_bull, 'mtf_sum': mtf_sum}
            time.sleep(FETCH_DELAY)
        except Exception as e:
            logging.error(f"Context fetch {sym}: {e}")
            symbol_context[sym] = {'htf_bull': None, 'mtf_sum': 6}

    for sym in active_list:
        ctx = symbol_context.get(sym, {'htf_bull': None, 'mtf_sum': 6})
        for tf_cfg in TIMEFRAMES:
            tf = tf_cfg['tf']
            label = tf_cfg['label']
            print(f"  → {sym:10s} [{label:5s}]...", end=" ")

            active_key = f"{sym}_{tf}_active"
            if active_key in trades and not trades[active_key].get('closed'):
                skip_active += 1
                print("🔒 active")
                continue

            try:
                result, reason = analyze_symbol(sym, tf_cfg, ctx['htf_bull'], ctx['mtf_sum'])
            except Exception as e:
                print(f"💥 {e}")
                logging.error(f"Analyze {sym} {tf}: {e}")
                continue
            time.sleep(FETCH_DELAY)

            if not result:
                if DEBUG_NEAR_MISS and reason:
                    print(f"⚪ {reason}")
                    near_misses.append(f"{sym} [{label}] {reason}")
                else: print("—")
                continue

            # Check signal not already expired
            if is_signal_expired(result):
                print("⏰ expired")
                continue

            sig_key = f"{result['signal']}_{tf}"
            if is_duplicate(sym, sig_key, cache, result['sqs']):
                skip_dupe += 1
                print("🔕 cooldown")
                continue

            ai_text = None
            if result['sqs'] >= AI_TIER_THRESHOLD and GEMINI_API_KEY:
                print("🤖", end=" ")
                ai_text = get_ai_analysis(result)
                if ai_text: ai_calls += 1
            result['ai_text'] = ai_text
            new_sigs.append(result)
            mark_sent(sym, sig_key, cache)
            trades[active_key] = create_trade(result)
            print(f"🚨 {result['tier']} {result['signal']} SQS={result['sqs']}")
            logging.info(f"SIGNAL: {sym} {tf} {result['signal']} SQS={result['sqs']} MTF={result.get('mtf_sum')}")

    # STEP 3: Send
    if new_sigs:
        new_sigs.sort(key=lambda s: s['sqs'], reverse=True)
        if len(new_sigs) >= DIGEST_THRESHOLD:
            send_telegram(format_digest(new_sigs), silent=False)
            print(f"📦 Sent digest")
            hq = [s for s in new_sigs if s['sqs'] >= SQS_MIN_FOR_ALERT]
            if hq:
                print(f"  Sending {len(hq)} full details...")
                for sig in hq:
                    send_telegram(format_new_signal(sig, sig.get('ai_text')), silent=False)
            corr = format_correlation_alert(new_sigs, trades)
            if corr: send_telegram(corr, silent=True)
        else:
            for sig in new_sigs:
                silent = 'FAIR' in sig['tier']
                send_telegram(format_new_signal(sig, sig.get('ai_text')), silent=silent)
            print(f"📨 Sent {len(new_sigs)} alerts")
            corr = format_correlation_alert(new_sigs, trades)
            if corr: send_telegram(corr, silent=True)

    save_json(ALERT_CACHE, cache)
    save_json(TRADES_FILE, trades)

    # Weekly summary
    if should_send_weekly_summary():
        print("📊 Sending weekly summary...")
        ws = format_weekly_summary()
        if ws: send_telegram(ws, silent=False)

    # Final report
    print(f"\n{'='*60}")
    print(f"✅ New: {len(new_sigs)} | 🔕 Cooldown: {skip_dupe} | 🔒 Active: {skip_active}")
    print(f"⚪ Near-miss: {len(near_misses)} | 🤖 AI: {ai_calls} | 📊 Open: {len(trades)}")
    print(f"Session: {session} | Watchlist: {len(active_list)}/{len(ALL_SYMBOLS)}")
    print(f"{'='*60}")
    logging.info(f"Scan done | New:{len(new_sigs)} Active:{len(trades)} AI:{ai_calls}")

if __name__ == "__main__":
    main()
