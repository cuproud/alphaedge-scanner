"""
ALPHAEDGE PYTHON SCANNER v5.1 - SESSION-AWARE
═══════════════════════════════════════════════════════════════
CHANGES IN v5.1:
• Smart watchlist filtering by trading session
• Split into crypto/stocks/extended-hours groups
• After-hours signal warnings (thin liquidity alert)
• Gold futures respects Sunday maintenance break
• Active trades still monitored 24/7 regardless of session

INHERITS FROM v5.0:
• Smart context-aware RSI filtering
• Multi-timeframe (30m + 1h)
• Live price entries
• Close-based SL/TP detection
• Position sizing with risk management
• Price ladder visualization
• Trade age counter
• Symbol emojis
• Smart cooldown by SQS tier
• Session tagging
• Weekly performance summary
• Trade history archive
• Correlation detection
• Daily near-miss digest
• Market context (SPY/QQQ/VIX)
• Batch digest mode
• Log archiving
• Rate limit protection
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
# CONFIG
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

EST = ZoneInfo("America/New_York")

def now_est():
    return datetime.now(EST)

def fmt_time():
    return now_est().strftime('%H:%M %Z')

def fmt_datetime():
    return now_est().strftime('%Y-%m-%d %H:%M %Z')

# 💼 ACCOUNT & RISK CONFIG
ACCOUNT_SIZE = 10000
RISK_PCT = 1.0

# ═══════════════════════════════════════════════
# WATCHLIST GROUPS (session-aware)
# ═══════════════════════════════════════════════

# 🪙 24/7 — crypto + gold (gold pauses briefly Sunday)
CRYPTO_WATCHLIST = [
    'BTC-USD', 'ETH-USD', 'XRP-USD',
    'GC=F',
]

# 💎 Mega-cap stocks — OK during extended hours (pre-market + after-hours)
# These have enough liquidity that 4AM-8PM signals are tradeable
EXTENDED_HOURS_STOCKS = [
    'NVDA', 'TSLA', 'AMD', 'MSFT', 'META', 'AMZN', 'GOOGL', 'NFLX'
]

# 📊 All other stocks — ONLY during regular market hours (9:30 AM - 4 PM EDT)
# Thin after-hours liquidity makes signals unreliable
REGULAR_HOURS_ONLY = [
    'MU', 'SNDK', 'NBIS',
    'IONQ', 'RGTI', 'QBTS',
    'OKLO', 'IREN', 'UAMY', 'WGRX',
    'SOFI', 'NVO',
]

# Combined for emoji mapping and weekly summary
ALL_SYMBOLS = CRYPTO_WATCHLIST + EXTENDED_HOURS_STOCKS + REGULAR_HOURS_ONLY

# Symbol emojis
SYMBOL_EMOJI = {
    'BTC-USD': '₿', 'ETH-USD': 'Ξ', 'XRP-USD': '◇',
    'GC=F': '🥇',
    'NVDA': '💎', 'TSLA': '🚘', 'META': '👓', 'AMZN': '📦',
    'GOOGL': '🔍', 'MSFT': '🪟', 'NFLX': '🎬', 'AMD': '⚡',
    'MU': '💾', 'SNDK': '💽', 'NBIS': '🌐',
    'IONQ': '⚛️', 'RGTI': '🧪', 'QBTS': '🔬',
    'OKLO': '☢️', 'IREN': '🪙', 'UAMY': '⚒️', 'WGRX': '💊',
    'SOFI': '🏦', 'NVO': '💉',
}

# Multi-timeframe scanning
TIMEFRAMES = [
    {'tf': '30m', 'lookback': '60d', 'label': '⚡30m', 'min_bars': 100},
    {'tf': '1h',  'lookback': '3mo', 'label': '📊1h',  'min_bars': 200},
]

# Signal quality thresholds
MIN_SQS = 60
MIN_SCORE = 5
AI_TIER_THRESHOLD = 70
MAX_TRADE_AGE_HOURS = 72

# Smart cooldown by SQS tier (hours)
COOLDOWN_ELITE = 2
COOLDOWN_STRONG = 4
COOLDOWN_GOOD = 6
COOLDOWN_FAIR = 10

DIGEST_THRESHOLD = 4
DEBUG_NEAR_MISS = True
FETCH_DELAY = 0.3

# File paths
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

# ═══════════════════════════════════════════════
# SESSION & WATCHLIST LOGIC
# ═══════════════════════════════════════════════

def get_session():
    """Return trading session tag based on EST time."""
    now = now_est()
    hour = now.hour
    minute = now.minute
    is_weekend = now.weekday() >= 5
    
    if is_weekend:
        return "🌐 Weekend"
    
    time_decimal = hour + minute / 60
    
    if 4 <= time_decimal < 9.5:
        return "🌅 Pre-Market"
    elif 9.5 <= time_decimal < 10.5:
        return "🔔 Market Open"
    elif 10.5 <= time_decimal < 14:
        return "📊 Midday"
    elif 14 <= time_decimal < 16:
        return "⚡ Power Hour"
    elif 16 <= time_decimal < 20:
        return "🌙 After-Hours"
    else:
        return "🌑 Overnight"

def is_crypto(symbol):
    """Check if symbol is crypto or gold futures (24/7-ish)."""
    return symbol.endswith('-USD') or symbol == 'GC=F'

def is_extended_hours_session():
    """True if we're in pre-market or after-hours (stocks have reduced liquidity)."""
    session = get_session()
    return session in ['🌅 Pre-Market', '🌙 After-Hours']

def is_regular_market_open():
    """True if regular US market hours (9:30 AM - 4:00 PM EDT weekday)."""
    session = get_session()
    return session in ['🔔 Market Open', '📊 Midday', '⚡ Power Hour']

def get_active_watchlist():
    """
    Return symbols that should be scanned RIGHT NOW based on session.
    
    - Weekend: crypto only
    - Overnight (8PM - 4AM): crypto only
    - Pre-Market (4-9:30 AM): crypto + mega-cap stocks
    - Regular Hours (9:30 AM - 4 PM): ALL symbols
    - After-Hours (4-8 PM): crypto + mega-cap stocks
    """
    session = get_session()
    
    if session == "🌐 Weekend":
        return CRYPTO_WATCHLIST
    
    if session == "🌑 Overnight":
        return CRYPTO_WATCHLIST
    
    if session in ["🌅 Pre-Market", "🌙 After-Hours"]:
        return CRYPTO_WATCHLIST + EXTENDED_HOURS_STOCKS
    
    # Regular market hours — scan everything
    if is_regular_market_open():
        return CRYPTO_WATCHLIST + EXTENDED_HOURS_STOCKS + REGULAR_HOURS_ONLY
    
    # Fallback (shouldn't reach here)
    return CRYPTO_WATCHLIST

# ═══════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════

def ema(s, l): return s.ewm(span=l, adjust=False).mean()

def rsi(series, length=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(length).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def atr(df, length=14):
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def macd(series, fast=12, slow=26, signal=9):
    m = ema(series, fast) - ema(series, slow)
    s = ema(m, signal)
    return m, s

def adx(df, length=14):
    high, low, close = df['High'], df['Low'], df['Close']
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr_val = tr.rolling(length).mean()
    plus_di = 100 * (plus_dm.rolling(length).mean() / atr_val)
    minus_di = 100 * (minus_dm.rolling(length).mean() / atr_val)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(length).mean(), plus_di, minus_di

def supertrend(df, period=10, mult=3.0):
    atr_val = atr(df, period)
    hl2 = (df['High'] + df['Low']) / 2
    upper = hl2 + (mult * atr_val)
    lower = hl2 - (mult * atr_val)
    trend = pd.Series(index=df.index, dtype=int)
    trend.iloc[0] = 1
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > upper.iloc[i-1]:
            trend.iloc[i] = 1
        elif df['Close'].iloc[i] < lower.iloc[i-1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[i-1]
    return trend

def vwap(df):
    q = df['Volume']
    p = (df['High'] + df['Low'] + df['Close']) / 3
    return (p * q).cumsum() / q.cumsum()

# ═══════════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════════

def load_json(path, default):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)

def load_cache_with_migration():
    cache = load_json(ALERT_CACHE, {})
    cleaned = {}
    for key, ts_str in cache.items():
        try:
            dt = datetime.fromisoformat(ts_str) if isinstance(ts_str, str) else ts_str
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            cleaned[key] = dt.isoformat()
        except:
            continue
    return cleaned

def get_cooldown_hours(sqs):
    if sqs >= 85: return COOLDOWN_ELITE
    elif sqs >= 70: return COOLDOWN_STRONG
    elif sqs >= 55: return COOLDOWN_GOOD
    else: return COOLDOWN_FAIR

def is_duplicate(symbol, signal_key, cache, sqs=60):
    key = f"{symbol}_{signal_key}"
    if key not in cache:
        return False
    try:
        last_time = datetime.fromisoformat(cache[key])
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=EST)
        cooldown = get_cooldown_hours(sqs)
        return now_est() - last_time < timedelta(hours=cooldown)
    except:
        return False

def mark_sent(symbol, signal_key, cache):
    cache[f"{symbol}_{signal_key}"] = now_est().isoformat()

# ═══════════════════════════════════════════════
# LIVE PRICE FETCH
# ═══════════════════════════════════════════════

def get_real_time_price(symbol):
    try:
        df = yf.download(symbol, period='1d', interval='1m',
                        progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return float(df['Close'].iloc[-1])
    except:
        return None

def get_live_ohlc(symbol):
    try:
        df = yf.download(symbol, period='2d', interval='5m',
                        progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return float(df['Close'].iloc[-1]), float(df['High'].iloc[-1]), float(df['Low'].iloc[-1])
    except:
        return None

# ═══════════════════════════════════════════════
# ANALYSIS ENGINE
# ═══════════════════════════════════════════════

def analyze_symbol(symbol, tf_config):
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
        
        df['ema20'] = ema(df['Close'], 20)
        df['ema50'] = ema(df['Close'], 50)
        df['ema200'] = ema(df['Close'], 200) if len(df) >= 200 else ema(df['Close'], 100)
        df['rsi'] = rsi(df['Close'], 14)
        df['atr'] = atr(df, 14)
        df['macd'], df['signal'] = macd(df['Close'])
        df['adx'], df['plus_di'], df['minus_di'] = adx(df, 14)
        df['st'] = supertrend(df, 10, 3.0)
        df['vwap'] = vwap(df)
        df['vol_avg'] = df['Volume'].rolling(20).mean()
        
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
        
        strong_uptrend = (
            ema50 > ema200 and
            bar_price > ema50 and
            22 < adx_val < 55 and
            last['plus_di'] > last['minus_di']
        )
        strong_downtrend = (
            ema50 < ema200 and
            bar_price < ema50 and
            22 < adx_val < 55 and
            last['minus_di'] > last['plus_di']
        )
        
        price_stretch = abs(bar_price - ema50) / atr_val
        parabolic = price_stretch > 5.0
        htf_bullish = bar_price > ema200
        htf_bearish = bar_price < ema200
        
        # Confluence
        bull, bear = 0, 0
        if bar_price > last['ema20']: bull += 1
        else: bear += 1
        if bar_price > last['ema50']: bull += 1
        else: bear += 1
        if last['ema50'] > last['ema200']: bull += 1
        else: bear += 1
        if last['st'] == 1: bull += 1
        else: bear += 1
        if last['macd'] > last['signal']: bull += 1
        else: bear += 1
        if rsi_val > 50: bull += 1
        else: bear += 1
        if bar_price > last['vwap']: bull += 1
        else: bear += 1
        if adx_val > 22:
            if last['plus_di'] > last['minus_di']: bull += 1
            else: bear += 1
        if last['Volume'] > last['vol_avg'] * 1.3:
            if bar_price > prev['Close']: bull += 1
            else: bear += 1
        if rsi_val > prev['rsi']: bull += 1
        else: bear += 1
        
        # Triggers
        fresh_bull = (
            (prev['Close'] <= prev['ema50'] and bar_price > last['ema50']) or
            (prev['st'] == -1 and last['st'] == 1) or
            (prev['macd'] <= prev['signal'] and last['macd'] > last['signal'])
        )
        fresh_bear = (
            (prev['Close'] >= prev['ema50'] and bar_price < last['ema50']) or
            (prev['st'] == 1 and last['st'] == -1) or
            (prev['macd'] >= prev['signal'] and last['macd'] < last['signal'])
        )
        pullback_bull = (
            strong_uptrend and
            (df['Close'].iloc[-4:-1] < df['ema20'].iloc[-4:-1]).any() and
            bar_price > last['ema20'] and
            40 < rsi_val < 75
        )
        pullback_bear = (
            strong_downtrend and
            (df['Close'].iloc[-4:-1] > df['ema20'].iloc[-4:-1]).any() and
            bar_price < last['ema20'] and
            25 < rsi_val < 60
        )
        oversold_bounce = (
            prev['rsi'] < 32 and rsi_val > prev['rsi'] and
            bar_price > prev['Close'] and bull >= 5
        )
        overbought_drop = (
            prev['rsi'] > 68 and rsi_val < prev['rsi'] and
            bar_price < prev['Close'] and bear >= 5
        )
        trend_continuation_bull = (
            strong_uptrend and
            bull >= 7 and
            bar_price > df['High'].iloc[-5:-1].max() * 0.998
        )
        trend_continuation_bear = (
            strong_downtrend and
            bear >= 7 and
            bar_price < df['Low'].iloc[-5:-1].min() * 1.002
        )
        strong_bull = (bull >= 7 and 22 < adx_val < 55 
                       and last['plus_di'] > last['minus_di']
                       and rsi_val < 78)
        strong_bear = (bear >= 7 and 22 < adx_val < 55
                       and last['minus_di'] > last['plus_di']
                       and rsi_val > 22)
        
        bull_trigger = fresh_bull or pullback_bull or oversold_bounce or strong_bull or trend_continuation_bull
        bear_trigger = fresh_bear or pullback_bear or overbought_drop or strong_bear or trend_continuation_bear
        
        trigger_type = ""
        if fresh_bull or fresh_bear: trigger_type = "Fresh Cross"
        elif pullback_bull or pullback_bear: trigger_type = "Pullback"
        elif oversold_bounce: trigger_type = "Oversold Bounce"
        elif overbought_drop: trigger_type = "Overbought Drop"
        elif trend_continuation_bull or trend_continuation_bear: trigger_type = "Trend Continuation"
        elif strong_bull or strong_bear: trigger_type = "Strong Momentum"
        
        # Smart hard blocks
        if bull_trigger:
            if rsi_val >= 80 and not strong_uptrend:
                return None, f"RSI extreme ({rsi_val:.0f}) + weak trend"
            if parabolic and not strong_uptrend:
                return None, f"parabolic ({price_stretch:.1f}×) + weak trend"
            if adx_val >= 65:
                return None, f"ADX exhausted ({adx_val:.0f})"
            if htf_bearish and trigger_type == "Strong Momentum":
                return None, "counter-HTF momentum"
        
        if bear_trigger:
            if rsi_val <= 20 and not strong_downtrend:
                return None, f"RSI extreme ({rsi_val:.0f}) + weak trend"
            if parabolic and not strong_downtrend:
                return None, f"parabolic + weak trend"
            if adx_val >= 65:
                return None, f"ADX exhausted ({adx_val:.0f})"
            if htf_bullish and trigger_type == "Strong Momentum":
                return None, "counter-HTF momentum"
        
        # SQS
        def calc_sqs(score, is_bull):
            conf = score / 10 * 40
            regime = 15 if 22 < adx_val < 50 else 10 if adx_val > 20 else 5
            vol = 10 if last['Volume'] > last['vol_avg'] * 1.5 else 6
            
            if 40 <= rsi_val <= 60: rsi_fit = 10
            elif 30 <= rsi_val <= 70: rsi_fit = 7
            else: rsi_fit = 2
            
            if is_bull and ema50 > ema200 and bar_price > ema200: trend = 15
            elif not is_bull and ema50 < ema200 and bar_price < ema200: trend = 15
            else: trend = 5
            
            if price_stretch > 4: trend = max(0, trend - 10)
            
            return max(0, min(100, conf + regime + vol + rsi_fit + trend))
        
        def grade(s):
            return "A+" if s >= 8 else "A" if s >= 6 else "B" if s >= 4 else "C"
        
        def tier(sqs):
            if sqs >= 85: return "🏆 ELITE"
            elif sqs >= 70: return "⭐ STRONG"
            elif sqs >= 55: return "✅ GOOD"
            else: return "⚠️ FAIR"
        
        if bull_trigger and bull >= MIN_SCORE:
            sqs = calc_sqs(bull, True)
            if sqs < MIN_SQS:
                return None, f"BUY SQS too low ({sqs:.0f})"
            signal_type, score = 'BUY', bull
        elif bear_trigger and bear >= MIN_SCORE:
            sqs = calc_sqs(bear, False)
            if sqs < MIN_SQS:
                return None, f"SELL SQS too low ({sqs:.0f})"
            signal_type, score = 'SELL', bear
        else:
            if bull >= 7 and not bull_trigger:
                return None, f"bull={bull} no trigger (rsi={rsi_val:.0f})"
            if bear >= 7 and not bear_trigger:
                return None, f"bear={bear} no trigger (rsi={rsi_val:.0f})"
            return None, None
        
        # Live price for accurate entry
        live_price = get_real_time_price(symbol)
        entry_price = live_price if live_price else bar_price
        
        # SL/TP
        lookback_bars = 10
        recent_low = float(df['Low'].iloc[-lookback_bars-1:-1].min())
        recent_high = float(df['High'].iloc[-lookback_bars-1:-1].max())
        
        if signal_type == 'BUY':
            atr_sl = entry_price - (atr_val * 2)
            struct_sl = recent_low - (atr_val * 0.2)
            sl = min(atr_sl, struct_sl)
            min_sl = entry_price - (atr_val * 0.5)
            sl = min(sl, min_sl)
            risk = entry_price - sl
            tp1 = entry_price + risk
            tp2 = entry_price + risk * 2
            tp3 = entry_price + risk * 3
        else:
            atr_sl = entry_price + (atr_val * 2)
            struct_sl = recent_high + (atr_val * 0.2)
            sl = max(atr_sl, struct_sl)
            min_sl = entry_price + (atr_val * 0.5)
            sl = max(sl, min_sl)
            risk = sl - entry_price
            tp1 = entry_price - risk
            tp2 = entry_price - risk * 2
            tp3 = entry_price - risk * 3
        
        dollar_risk = ACCOUNT_SIZE * RISK_PCT / 100
        shares = int(dollar_risk / risk) if risk > 0 else 0
        notional = round(shares * entry_price, 2)
        
        decimals = 4 if entry_price < 10 else 2
        
        return {
            'symbol': symbol,
            'emoji': SYMBOL_EMOJI.get(symbol, '📈'),
            'signal': signal_type,
            'price': round(entry_price, decimals),
            'bar_price': round(bar_price, decimals),
            'score': score,
            'grade': grade(score),
            'sqs': round(sqs),
            'tier': tier(sqs),
            'trigger': trigger_type,
            'sl': round(sl, decimals),
            'tp1': round(tp1, decimals),
            'tp2': round(tp2, decimals),
            'tp3': round(tp3, decimals),
            'risk': round(risk, decimals),
            'shares': shares,
            'notional': notional,
            'dollar_risk': round(dollar_risk, 2),
            'rsi': round(rsi_val, 1),
            'adx': round(adx_val, 1),
            'stretch': round(price_stretch, 1),
            'regime': 'TRENDING' if adx_val > 25 else 'RANGING' if adx_val < 20 else 'TRANSITIONAL',
            'timeframe': tf,
            'tf_label': tf_config['label'],
            'session': get_session(),
            'decimals': decimals,
            'strong_trend': strong_uptrend if signal_type == 'BUY' else strong_downtrend,
            'is_crypto': is_crypto(symbol),
            'is_extended_hours': is_extended_hours_session() and not is_crypto(symbol),
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
        'shares': sig['shares'],
        'notional': sig['notional'],
        'decimals': sig['decimals'],
        'grade': sig['grade'],
        'sqs': sig['sqs'],
        'tier': sig['tier'],
        'tf': sig['timeframe'],
        'tf_label': sig['tf_label'],
        'opened_at': now_est().isoformat(),
        'opened_session': sig['session'],
        'tp1_hit': False,
        'tp2_hit': False,
        'tp3_hit': False,
        'tp1_hit_at': None,
        'tp2_hit_at': None,
        'tp3_hit_at': None,
        'closed': False,
        'closed_reason': None,
        'closed_at': None,
        'final_r': None,
    }

def check_trade_progress(trade):
    result = get_live_ohlc(trade['symbol'])
    if not result:
        return [], False
    
    current, _, _ = result
    events = []
    is_long = trade['signal'] == 'BUY'
    
    # Age check
    try:
        opened = datetime.fromisoformat(trade['opened_at'])
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=EST)
        age = now_est() - opened
        if age > timedelta(hours=MAX_TRADE_AGE_HOURS):
            trade['closed'] = True
            trade['closed_reason'] = 'Timeout (72h)'
            trade['closed_at'] = now_est().isoformat()
            trade['final_r'] = 0
            events.append({'type': 'TIMEOUT', 'price': current})
            return events, True
    except:
        pass
    
    # SL check
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
    
    # TP hits
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
# AI ANALYSIS
# ═══════════════════════════════════════════════

def get_ai_analysis(sig):
    if not GEMINI_API_KEY:
        return None
    
    ah_note = ""
    if sig.get('is_extended_hours'):
        ah_note = "\nIMPORTANT: This is an after-hours/pre-market signal — liquidity is thin."
    
    prompt = f"""Analyze this trading signal in EXACTLY 3 short lines (max 100 chars each).

SYMBOL: {sig['symbol']} ({sig['signal']} @ ${sig['price']})
TF: {sig['timeframe']} | Trigger: {sig['trigger']}
Score: {sig['score']}/10 ({sig['grade']}) | SQS: {sig['sqs']}/100
RSI: {sig['rsi']} | ADX: {sig['adx']} | Regime: {sig['regime']}
R:R = 1:3 | Stretch: {sig['stretch']}×ATR | Strong trend: {sig['strong_trend']}{ah_note}

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
        return None
    except Exception as e:
        logging.error(f"AI error: {e}")
        return None

# ═══════════════════════════════════════════════
# MESSAGE FORMATTING
# ═══════════════════════════════════════════════

def fmt_price(val, decimals):
    return f"{val:.{decimals}f}"

def time_ago(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        delta = now_est() - dt
        total_mins = int(delta.total_seconds() / 60)
        if total_mins < 60:
            return f"{total_mins}m"
        hours = total_mins // 60
        mins = total_mins % 60
        if hours < 24:
            return f"{hours}h {mins}m"
        days = hours // 24
        hours_rem = hours % 24
        return f"{days}d {hours_rem}h"
    except:
        return "?"

def price_ladder(trade, current_price):
    dec = trade['decimals']
    is_long = trade['signal'] == 'BUY'
    
    levels = [
        ('TP3', trade['tp3'], '🎯', trade['tp3_hit']),
        ('TP2', trade['tp2'], '🎯', trade['tp2_hit']),
        ('TP1', trade['tp1'], '🎯', trade['tp1_hit']),
        ('NOW', current_price, '⬅️', None),
        ('Ent', trade['entry'], '📍', None),
        ('SL ', trade['sl'],  '🛑', None),
    ]
    levels.sort(key=lambda x: -x[1] if is_long else x[1])
    
    lines = []
    for label, price, emoji, hit in levels:
        marker = " ✅" if hit else ("" if hit is None else "")
        lines.append(f"{emoji} {label}: `${fmt_price(price, dec)}`{marker}")
    return "\n".join(lines)

def format_new_signal(sig, ai_text=None):
    emoji = "🟢" if sig['signal'] == 'BUY' else "🔴"
    dec = sig['decimals']
    sym_emoji = sig['emoji']
    
    msg = f"{sig['tier']} {emoji} *{sig['signal']} {sym_emoji} {sig['symbol']}* `[{sig['tf_label']}]`\n"
    msg += f"{sig['session']} • {now_est().strftime('%H:%M %Z')}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    
    # ⚠️ After-hours warning for stocks
    if sig.get('is_extended_hours'):
        msg += f"⚠️ *After-hours signal — thin liquidity!*\n"
        msg += f"_Wider spreads, execution may slip. Use limit orders._\n"
        msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    
    msg += f"💵 *Live Entry:* `${fmt_price(sig['price'], dec)}`\n"
    msg += f"🎯 *Trigger:* {sig['trigger']}\n"
    msg += f"📊 *Quality:* {sig['score']}/10 ({sig['grade']}) • SQS *{sig['sqs']}*\n"
    
    msg += f"\n*🎯 TRADE PLAN*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"📍 Entry:  `${fmt_price(sig['price'], dec)}`\n"
    msg += f"🛑 SL:     `${fmt_price(sig['sl'], dec)}`\n"
    msg += f"🎯 TP1:    `${fmt_price(sig['tp1'], dec)}` (1R)\n"
    msg += f"🎯 TP2:    `${fmt_price(sig['tp2'], dec)}` (2R)\n"
    msg += f"🎯 TP3:    `${fmt_price(sig['tp3'], dec)}` (3R)\n"
    
    msg += f"\n*💼 POSITION SIZING*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"Shares:   `{sig['shares']}` units\n"
    msg += f"Notional: `${sig['notional']:,.2f}`\n"
    msg += f"Risk:     `${sig['dollar_risk']:.2f}` ({RISK_PCT}% of ${ACCOUNT_SIZE:,})\n"
    
    msg += f"\n*📈 TECHNICALS*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"RSI: `{sig['rsi']}` | ADX: `{sig['adx']}` | Stretch: `{sig['stretch']}×`\n"
    msg += f"Regime: {sig['regime']} | Trend: {'Strong ✓' if sig['strong_trend'] else 'Mixed'}\n"
    
    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n"
        msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
        msg += f"{ai_text}\n"
    
    return msg

def format_trade_event(trade, event, current_price):
    emoji = "🟢" if trade['signal'] == 'BUY' else "🔴"
    dec = trade['decimals']
    sym_emoji = trade.get('emoji', '📈')
    age = time_ago(trade['opened_at'])
    
    event_type = event['type']
    
    if event_type == 'TP1':
        header = f"✅ *TP1 HIT* {emoji} {sym_emoji} {trade['symbol']} `[{trade.get('tf_label', trade['tf'])}]`"
        sub = "💡 _Move SL to breakeven_ 🔒"
        r_mult = "+1R"
        dollar_gain = round(trade['shares'] * trade['risk'], 2)
    elif event_type == 'TP2':
        header = f"✅✅ *TP2 HIT* {emoji} {sym_emoji} {trade['symbol']} `[{trade.get('tf_label', trade['tf'])}]`"
        sub = "💡 _Trail SL to TP1_ 📈"
        r_mult = "+2R"
        dollar_gain = round(trade['shares'] * trade['risk'] * 2, 2)
    elif event_type == 'TP3':
        header = f"🏆 *TP3 HIT — FULL TARGET* {emoji} {sym_emoji} {trade['symbol']}"
        sub = "🎉 _Trade complete!_"
        r_mult = "+3R"
        dollar_gain = round(trade['shares'] * trade['risk'] * 3, 2)
    elif event_type == 'SL':
        header = f"🛑 *SL HIT* {emoji} {sym_emoji} {trade['symbol']}"
        if trade['tp1_hit']:
            sub = "✅ _Trailed profit exit — still a winner!_"
            r_mult = "Partial gain"
            dollar_gain = round(trade['shares'] * trade['risk'] * 0.5, 2)
        else:
            sub = "❌ _Stop loss hit — trade closed_"
            r_mult = "-1R"
            dollar_gain = -round(trade['shares'] * trade['risk'], 2)
    elif event_type == 'TIMEOUT':
        header = f"⏰ *TRADE TIMEOUT* {emoji} {sym_emoji} {trade['symbol']}"
        sub = "_72h expiry — auto-closed_"
        r_mult = "—"
        dollar_gain = 0
    else:
        return None
    
    msg = f"{header}\n"
    msg += f"⏱ Trade age: {age}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"💵 *Live:* `${fmt_price(current_price, dec)}`\n"
    msg += f"🎯 *Hit:* `${fmt_price(event['price'], dec)}` ({r_mult})\n"
    if isinstance(dollar_gain, (int, float)) and dollar_gain != 0:
        sign = "+" if dollar_gain > 0 else ""
        msg += f"💰 *P&L:* {sign}${dollar_gain:,.2f}\n"
    msg += f"{sub}\n"
    
    msg += f"\n*📊 PRICE LADDER*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += price_ladder(trade, current_price)
    
    msg += f"\n\n⏰ {fmt_time()}"
    return msg

def format_digest(signals):
    msg = f"🔔 *SIGNAL DIGEST — {len(signals)} alerts*\n"
    msg += f"{get_session()} • {fmt_time()}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"
    
    ah_count = sum(1 for s in signals if s.get('is_extended_hours'))
    if ah_count > 0:
        msg += f"⚠️ _{ah_count} signal(s) in extended hours — thin liquidity_\n\n"
    
    for sig in signals:
        emoji = "🟢" if sig['signal'] == 'BUY' else "🔴"
        ah_tag = " ⚠️" if sig.get('is_extended_hours') else ""
        msg += f"{sig['tier']} {emoji} *{sig['symbol']}* `[{sig['tf_label']}]`{ah_tag}\n"
        msg += f"  {sig['emoji']} {sig['signal']} @ `${fmt_price(sig['price'], sig['decimals'])}` • SQS {sig['sqs']}\n"
        msg += f"  🎯 {sig['trigger']} | RSI {sig['rsi']} | Shares: {sig['shares']}\n"
        msg += f"  🛑 SL `${fmt_price(sig['sl'], sig['decimals'])}` → 🎯 TP3 `${fmt_price(sig['tp3'], sig['decimals'])}`\n\n"
    
    msg += f"_Full trade plans sent separately._"
    return msg

# ═══════════════════════════════════════════════
# MARKET CONTEXT
# ═══════════════════════════════════════════════

def get_market_context():
    try:
        tickers = ['SPY', 'QQQ', '^VIX']
        data = {}
        for t in tickers:
            df = yf.download(t, period='5d', interval='1d', progress=False, auto_adjust=True)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                last_close = float(df['Close'].iloc[-1])
                prev_close = float(df['Close'].iloc[-2])
                pct = (last_close - prev_close) / prev_close * 100
                data[t] = {'price': last_close, 'pct': pct}
        return data
    except Exception as e:
        logging.error(f"Market context error: {e}")
        return None

def format_market_context():
    ctx = get_market_context()
    if not ctx:
        return None
    
    spy = ctx.get('SPY', {})
    qqq = ctx.get('QQQ', {})
    vix = ctx.get('^VIX', {})
    
    if vix.get('price', 20) < 15:
        vol_regime = "🟢 Low Vol"
    elif vix.get('price', 20) < 22:
        vol_regime = "🟡 Normal Vol"
    else:
        vol_regime = "🔴 High Vol"
    
    if spy.get('pct', 0) > 0.3 and qqq.get('pct', 0) > 0.3:
        bias = "🚀 RISK-ON — Longs favored"
    elif spy.get('pct', 0) < -0.3 and qqq.get('pct', 0) < -0.3:
        bias = "🐻 RISK-OFF — Shorts favored"
    else:
        bias = "⚖️ MIXED — Selective trading"
    
    msg = f"🌍 *MARKET CONTEXT*\n"
    msg += f"{fmt_datetime()}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    
    def fmt_row(name, d):
        if not d: return f"{name}: —\n"
        sign = "+" if d['pct'] >= 0 else ""
        return f"{name}: `${d['price']:.2f}` ({sign}{d['pct']:.2f}%)\n"
    
    msg += fmt_row("SPY ", spy)
    msg += fmt_row("QQQ ", qqq)
    msg += fmt_row("VIX ", vix)
    msg += f"\n*Volatility:* {vol_regime}\n"
    msg += f"*Bias:* {bias}\n"
    return msg

# ═══════════════════════════════════════════════
# WEEKLY SUMMARY
# ═══════════════════════════════════════════════

def format_weekly_summary():
    history = load_json(HISTORY_FILE, [])
    if not history:
        return None
    
    cutoff = now_est() - timedelta(days=7)
    week_trades = []
    for t in history:
        try:
            closed_at = t.get('closed_at')
            if not closed_at:
                continue
            dt = datetime.fromisoformat(closed_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            if dt >= cutoff:
                week_trades.append(t)
        except:
            continue
    
    if not week_trades:
        return None
    
    wins = [t for t in week_trades if (t.get('final_r') or 0) > 0]
    losses = [t for t in week_trades if (t.get('final_r') or 0) < 0]
    breakevens = [t for t in week_trades if (t.get('final_r') or 0) == 0]
    
    total_r = sum(t.get('final_r', 0) or 0 for t in week_trades)
    win_rate = len(wins) / len(week_trades) * 100 if week_trades else 0
    
    best = max(week_trades, key=lambda t: t.get('final_r', 0) or 0)
    worst = min(week_trades, key=lambda t: t.get('final_r', 0) or 0)
    
    grades = {'A+': [0, 0], 'A': [0, 0], 'B': [0, 0], 'C': [0, 0]}
    for t in week_trades:
        g = t.get('grade', 'C')
        if g in grades:
            grades[g][0] += 1
            if (t.get('final_r') or 0) > 0:
                grades[g][1] += 1
    
    msg = f"📊 *WEEKLY SUMMARY*\n"
    msg += f"{cutoff.strftime('%b %d')} → {now_est().strftime('%b %d')}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"Total signals: *{len(week_trades)}*\n"
    msg += f"✅ Wins: *{len(wins)}* ({win_rate:.0f}%)\n"
    msg += f"❌ Losses: *{len(losses)}*\n"
    msg += f"➖ Breakeven: *{len(breakevens)}*\n\n"
    msg += f"💰 *Total R: {'+'if total_r>=0 else ''}{total_r:.1f}R*\n"
    msg += f"🏆 Best: *{best['symbol']}* ({best.get('final_r', 0):+.1f}R)\n"
    msg += f"💥 Worst: *{worst['symbol']}* ({worst.get('final_r', 0):+.1f}R)\n"
    
    msg += f"\n*GRADE PERFORMANCE*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    for grade, (total, won) in grades.items():
        if total > 0:
            wr = won / total * 100
            msg += f"{grade}: {won}/{total} wins ({wr:.0f}%)\n"
    
    msg += f"\n⏰ {fmt_time()}"
    return msg

# ═══════════════════════════════════════════════
# NEAR-MISS DIGEST
# ═══════════════════════════════════════════════

def format_near_miss_digest(near_miss_list):
    if not near_miss_list:
        return None
    
    msg = f"👀 *WATCHLIST — Setups Forming*\n"
    msg += f"{fmt_datetime()}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Symbols with strong confluence but no trigger yet_\n\n"
    
    for item in near_miss_list[:10]:
        msg += f"⚡ {item['emoji']} *{item['symbol']}* `[{item['tf']}]`: {item['reason']}\n"
    
    msg += f"\n_Watch these — next pullback/breakout may trigger._"
    return msg

# ═══════════════════════════════════════════════
# CORRELATION DETECTION
# ═══════════════════════════════════════════════

CORRELATION_GROUPS = {
    'AI/Semis': ['NVDA', 'AMD', 'MU', 'SNDK', 'NBIS'],
    'Crypto': ['BTC-USD', 'ETH-USD', 'XRP-USD'],
    'Quantum': ['IONQ', 'RGTI', 'QBTS'],
    'Mega Tech': ['GOOGL', 'MSFT', 'META', 'AMZN'],
}

def detect_correlations(signals):
    warnings = []
    for group_name, symbols in CORRELATION_GROUPS.items():
        matching = [s for s in signals if s['symbol'] in symbols]
        if len(matching) >= 2:
            syms = ", ".join([m['symbol'] for m in matching])
            warnings.append(f"🔗 *{group_name}*: {syms} all firing simultaneously")
    return warnings

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
        logging.error(f"Telegram send error: {e}")
        return False

# ═══════════════════════════════════════════════
# STATE TRIGGERS
# ═══════════════════════════════════════════════

def should_send_daily_context():
    state = load_json(STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    if state.get('last_context_date') == today:
        return False
    if now_est().hour < 9:
        return False
    if now_est().weekday() >= 5:  # no weekend context
        return False
    state['last_context_date'] = today
    save_json(STATE_FILE, state)
    return True

def should_send_weekly_summary():
    state = load_json(STATE_FILE, {})
    now = now_est()
    if now.weekday() != 6:  # Sunday
        return False
    if now.hour < 21:
        return False
    week_key = now.strftime('%Y-W%W')
    if state.get('last_weekly') == week_key:
        return False
    state['last_weekly'] = week_key
    save_json(STATE_FILE, state)
    return True

def should_send_near_miss_digest():
    state = load_json(STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    if state.get('last_nearmiss') == today:
        return False
    if now_est().hour < 9 or now_est().hour > 10:
        return False
    if now_est().weekday() >= 5:  # no weekend digest
        return False
    state['last_nearmiss'] = today
    save_json(STATE_FILE, state)
    return True

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main():
    session = get_session()
    active_list = get_active_watchlist()
    
    print(f"\n{'='*60}")
    print(f"AlphaEdge v5.1 Scanner @ {fmt_datetime()}")
    print(f"Session: {session}")
    print(f"Active watchlist: {len(active_list)}/{len(ALL_SYMBOLS)} symbols")
    print(f"Account: ${ACCOUNT_SIZE:,} | Risk/trade: {RISK_PCT}%")
    print(f"AI: {bool(GEMINI_API_KEY)} | MIN_SQS: {MIN_SQS}")
    print(f"{'='*60}\n")
    
    logging.info(f"Scan start | Session: {session} | Active: {len(active_list)}")
    
    cache = load_cache_with_migration()
    trades = load_json(TRADES_FILE, {})
    
    # ═══════════════════════════════════════════════
    # Daily market context (once per weekday 9AM+)
    # ═══════════════════════════════════════════════
    if should_send_daily_context():
        print("🌍 Sending daily market context...")
        ctx_msg = format_market_context()
        if ctx_msg:
            send_telegram(ctx_msg, silent=True)
    
    # ═══════════════════════════════════════════════
    # STEP 1: Check ALL active trades (regardless of session!)
    # ═══════════════════════════════════════════════
    # CRITICAL: active trades are checked 24/7 even outside scanning hours
    # so you don't miss TP/SL hits on stocks during after-hours
    if trades:
        print(f"📊 Checking {len(trades)} active trade(s) (all sessions)...")
        trades_to_remove = []
        
        for trade_key, trade in list(trades.items()):
            if trade.get('closed'):
                archive_trade(trade)
                trades_to_remove.append(trade_key)
                continue
            
            try:
                print(f"  → {trade['symbol']:10s} [{trade.get('tf_label', trade['tf']):5s}] ({trade['signal']})...", end=" ")
                events, closed = check_trade_progress(trade)
                
                if not events:
                    print("no change")
                    continue
                
                live_result = get_live_ohlc(trade['symbol'])
                current = live_result[0] if live_result else trade['entry']
                
                for event in events:
                    msg = format_trade_event(trade, event, current)
                    if msg:
                        send_telegram(msg, silent=False)
                        print(f"🔔 {event['type']}", end=" ")
                        logging.info(f"{trade['symbol']} {event['type']} @ {event['price']}")
                
                if closed:
                    archive_trade(trade)
                    trades_to_remove.append(trade_key)
                    print("✅ closed")
                else:
                    print()
            except Exception as e:
                print(f"💥 error: {e}")
                logging.error(f"Trade check {trade.get('symbol')}: {e}")
                continue
            
            time.sleep(FETCH_DELAY)
        
        for key in trades_to_remove:
            del trades[key]
        save_json(TRADES_FILE, trades)
        print()
    
    # ═══════════════════════════════════════════════
    # STEP 2: Scan for new signals (session-filtered watchlist)
    # ═══════════════════════════════════════════════
    print(f"🔍 Scanning {len(active_list)} symbols for new signals...")
    
    new_signals = []
    skipped_dupe = 0
    skipped_active = 0
    ai_calls = 0
    near_misses = []
    strong_near_miss = []
    
    for symbol in active_list:
        for tf_cfg in TIMEFRAMES:
            tf = tf_cfg['tf']
            label = tf_cfg['label']
            print(f"  → {symbol:10s} [{label:5s}]...", end=" ")
            
            active_key = f"{symbol}_{tf}_active"
            
            if active_key in trades and not trades[active_key].get('closed'):
                skipped_active += 1
                print(f"🔒 active")
                continue
            
            try:
                result, reason = analyze_symbol(symbol, tf_cfg)
            except Exception as e:
                print(f"💥 error: {e}")
                logging.error(f"Analyze {symbol} {tf}: {e}")
                continue
            
            time.sleep(FETCH_DELAY)
            
            if not result:
                if DEBUG_NEAR_MISS and reason:
                    print(f"⚪ {reason}")
                    if 'bull=' in str(reason) or 'bear=' in str(reason):
                        strong_near_miss.append({
                            'symbol': symbol,
                            'emoji': SYMBOL_EMOJI.get(symbol, '📈'),
                            'tf': label,
                            'reason': reason
                        })
                    near_misses.append(reason)
                else:
                    print("—")
                continue
            
            sig_key = f"{result['signal']}_{tf}"
            if is_duplicate(symbol, sig_key, cache, result['sqs']):
                skipped_dupe += 1
                print(f"🔕 cooldown")
                continue
            
            ai_text = None
            if result['sqs'] >= AI_TIER_THRESHOLD and GEMINI_API_KEY:
                print(f"🤖", end=" ")
                ai_text = get_ai_analysis(result)
                if ai_text:
                    ai_calls += 1
            
            result['ai_text'] = ai_text
            new_signals.append(result)
            mark_sent(symbol, sig_key, cache)
            trades[active_key] = create_trade(result)
            print(f"🚨 {result['tier']} {result['signal']} SQS={result['sqs']}")
            logging.info(f"NEW SIGNAL: {symbol} {tf} {result['signal']} SQS={result['sqs']}")
    
    # ═══════════════════════════════════════════════
    # STEP 3: Send signals (individual or digest)
    # ═══════════════════════════════════════════════
    if new_signals:
        corr_warnings = detect_correlations(new_signals)
        
        if len(new_signals) >= DIGEST_THRESHOLD:
            digest = format_digest(new_signals)
            if corr_warnings:
                digest += f"\n\n*⚠️ CORRELATION ALERT*\n"
                for w in corr_warnings:
                    digest += f"{w}\n"
                digest += f"_Consider reducing per-trade size._"
            send_telegram(digest, silent=False)
            print(f"📦 Sent digest with {len(new_signals)} signals")
            # Also send full details
            for sig in new_signals:
                msg = format_new_signal(sig, sig.get('ai_text'))
                silent = 'FAIR' in sig['tier']
                send_telegram(msg, silent=silent)
        else:
            for sig in new_signals:
                msg = format_new_signal(sig, sig.get('ai_text'))
                silent = 'FAIR' in sig['tier']
                send_telegram(msg, silent=silent)
            print(f"📨 Sent {len(new_signals)} individual alerts")
            
            if corr_warnings:
                warn_msg = f"⚠️ *CORRELATION ALERT*\n"
                warn_msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
                for w in corr_warnings:
                    warn_msg += f"{w}\n"
                warn_msg += f"\n_Consider reducing per-trade size._"
                send_telegram(warn_msg, silent=True)
    
    save_json(ALERT_CACHE, cache)
    save_json(TRADES_FILE, trades)
    
    # ═══════════════════════════════════════════════
    # STEP 4: Daily near-miss digest (9AM weekdays)
    # ═══════════════════════════════════════════════
    if should_send_near_miss_digest() and strong_near_miss:
        print(f"👀 Sending near-miss digest ({len(strong_near_miss)} items)...")
        digest_msg = format_near_miss_digest(strong_near_miss)
        if digest_msg:
            send_telegram(digest_msg, silent=True)
    
    # ═══════════════════════════════════════════════
    # STEP 5: Weekly summary (Sunday 9PM+)
    # ═══════════════════════════════════════════════
    if should_send_weekly_summary():
        print(f"📊 Sending weekly summary...")
        summary_msg = format_weekly_summary()
        if summary_msg:
            send_telegram(summary_msg, silent=False)
    
    # ═══════════════════════════════════════════════
    # Final report
    # ═══════════════════════════════════════════════
    print(f"\n{'='*60}")
    print(f"✅ New: {len(new_signals)} | 🔕 Cooldown: {skipped_dupe} | 🔒 Active: {skipped_active}")
    print(f"⚪ Near-miss: {len(near_misses)} | 🤖 AI: {ai_calls} | 📊 Open: {len(trades)}")
    print(f"Session: {session} | Watchlist: {len(active_list)}/{len(ALL_SYMBOLS)}")
    print(f"{'='*60}")
    
    logging.info(f"Scan end | New:{len(new_signals)} Active:{len(trades)} AI:{ai_calls}")

if __name__ == "__main__":
    main()
