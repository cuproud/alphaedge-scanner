"""
ALPHAEDGE PYTHON SCANNER v2.0
- Better signal detection
- Anti-spam (dedupe per symbol)
- Priority tiers
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID', '820394470')

WATCHLIST = [
    'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMD', 'META', 
    'GOOGL', 'AMZN', 'NFLX',
    'SPY', 'QQQ', 'DIA',
    'RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'HDFCBANK.NS',
    'BTC-USD', 'ETH-USD', 'SOL-USD',
]

TIMEFRAME = '1h'
LOOKBACK = '3mo'
MIN_SQS = 55          # Slightly relaxed
MIN_SCORE = 5

# ═══════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════

def ema(s, l): return s.ewm(span=l, adjust=False).mean()
def sma(s, l): return s.rolling(l).mean()

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
# ANTI-SPAM (stores last alert per symbol in file)
# ═══════════════════════════════════════════════

CACHE_FILE = 'alert_cache.json'
COOLDOWN_HOURS = 4  # Don't repeat same signal within 4 hours

def load_cache():
    try:
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)

def is_duplicate(symbol, signal_type, cache):
    key = f"{symbol}_{signal_type}"
    if key not in cache:
        return False
    last_time = datetime.fromisoformat(cache[key])
    return datetime.now() - last_time < timedelta(hours=COOLDOWN_HOURS)

def mark_sent(symbol, signal_type, cache):
    key = f"{symbol}_{signal_type}"
    cache[key] = datetime.now().isoformat()

# ═══════════════════════════════════════════════
# SIGNAL ENGINE
# ═══════════════════════════════════════════════

def analyze_symbol(symbol):
    try:
        df = yf.download(symbol, period=LOOKBACK, interval=TIMEFRAME, 
                         progress=False, auto_adjust=True)
        
        if df.empty or len(df) < 200:
            return None
        
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        df['ema20'] = ema(df['Close'], 20)
        df['ema50'] = ema(df['Close'], 50)
        df['ema200'] = ema(df['Close'], 200)
        df['rsi'] = rsi(df['Close'], 14)
        df['atr'] = atr(df, 14)
        df['macd'], df['signal'] = macd(df['Close'])
        df['adx'], df['plus_di'], df['minus_di'] = adx(df, 14)
        df['st'] = supertrend(df, 10, 3.0)
        df['vwap'] = vwap(df)
        df['vol_avg'] = df['Volume'].rolling(20).mean()
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]
        price = last['Close']
        
        # ─── Confluence Scores ───
        bull = 0
        bear = 0
        
        if price > last['ema20']: bull += 1
        else: bear += 1
        
        if price > last['ema50']: bull += 1
        else: bear += 1
        
        if last['ema50'] > last['ema200']: bull += 1
        else: bear += 1
        
        if last['st'] == 1: bull += 1
        else: bear += 1
        
        if last['macd'] > last['signal']: bull += 1
        else: bear += 1
        
        if last['rsi'] > 50: bull += 1
        else: bear += 1
        
        if price > last['vwap']: bull += 1
        else: bear += 1
        
        if last['adx'] > 22:
            if last['plus_di'] > last['minus_di']: bull += 1
            else: bear += 1
        
        if last['Volume'] > last['vol_avg'] * 1.3:
            if price > prev['Close']: bull += 1
            else: bear += 1
        
        # Momentum
        if last['rsi'] > prev['rsi']: bull += 1
        else: bear += 1
        
        # ─── Trigger Detection (multiple types) ───
        
        # Fresh crossovers
        fresh_bull_cross = (
            (prev['Close'] <= prev['ema50'] and price > last['ema50']) or
            (prev['st'] == -1 and last['st'] == 1) or
            (prev['macd'] <= prev['signal'] and last['macd'] > last['signal'])
        )
        fresh_bear_cross = (
            (prev['Close'] >= prev['ema50'] and price < last['ema50']) or
            (prev['st'] == 1 and last['st'] == -1) or
            (prev['macd'] >= prev['signal'] and last['macd'] < last['signal'])
        )
        
        # Pullback continuation (price bounces from EMA20 in trend)
        pullback_bull = (
            last['ema50'] > last['ema200'] and
            prev['Close'] < prev['ema20'] and price > last['ema20'] and
            last['rsi'] > 45
        )
        pullback_bear = (
            last['ema50'] < last['ema200'] and
            prev['Close'] > prev['ema20'] and price < last['ema20'] and
            last['rsi'] < 55
        )
        
        # Oversold/Overbought bounce
        oversold_bounce = (
            prev['rsi'] < 32 and last['rsi'] > prev['rsi'] and
            price > prev['Close'] and bull >= 5
        )
        overbought_drop = (
            prev['rsi'] > 68 and last['rsi'] < prev['rsi'] and
            price < prev['Close'] and bear >= 5
        )
        
        # Strong momentum (already trending hard)
        strong_bull = bull >= 8 and last['adx'] > 25 and last['plus_di'] > last['minus_di']
        strong_bear = bear >= 8 and last['adx'] > 25 and last['minus_di'] > last['plus_di']
        
        # Combine
        bull_trigger = fresh_bull_cross or pullback_bull or oversold_bounce or strong_bull
        bear_trigger = fresh_bear_cross or pullback_bear or overbought_drop or strong_bear
        
        # Detect trigger type
        trigger_type = ""
        if fresh_bull_cross or fresh_bear_cross: trigger_type = "Fresh Cross"
        elif pullback_bull or pullback_bear: trigger_type = "Pullback"
        elif oversold_bounce: trigger_type = "Oversold Bounce"
        elif overbought_drop: trigger_type = "Overbought Drop"
        elif strong_bull or strong_bear: trigger_type = "Strong Momentum"
        
        # ─── SQS ───
        def calc_sqs(score, is_bull):
            conf = score / 10 * 40
            regime = 15 if last['adx'] > 25 else 10 if last['adx'] > 20 else 5
            vol = 10 if last['Volume'] > last['vol_avg'] * 1.5 else 6
            rsi_fit = 10 if 30 < last['rsi'] < 70 else 5
            trend = 15 if (is_bull and last['ema50'] > last['ema200']) or (not is_bull and last['ema50'] < last['ema200']) else 5
            return min(100, conf + regime + vol + rsi_fit + trend)
        
        def grade(s):
            return "A+" if s >= 8 else "A" if s >= 6 else "B" if s >= 4 else "C"
        
        # Priority tier
        def tier(sqs):
            if sqs >= 85: return "🏆 ELITE"
            elif sqs >= 70: return "⭐ STRONG"
            elif sqs >= 55: return "✅ GOOD"
            else: return "⚠️ FAIR"
        
        # ─── Decide signal ───
        if bull_trigger and bull >= MIN_SCORE:
            sqs = calc_sqs(bull, True)
            if sqs < MIN_SQS: return None
            signal_type = 'BUY'
            score = bull
        elif bear_trigger and bear >= MIN_SCORE:
            sqs = calc_sqs(bear, False)
            if sqs < MIN_SQS: return None
            signal_type = 'SELL'
            score = bear
        else:
            return None
        
        # ─── TP/SL ───
        atr_val = last['atr']
        if signal_type == 'BUY':
            sl = price - (atr_val * 2)
            risk = price - sl
            tp1 = price + risk * 1
            tp2 = price + risk * 2
            tp3 = price + risk * 3
        else:
            sl = price + (atr_val * 2)
            risk = sl - price
            tp1 = price - risk * 1
            tp2 = price - risk * 2
            tp3 = price - risk * 3
        
        return {
            'symbol': symbol,
            'signal': signal_type,
            'price': round(price, 2),
            'score': score,
            'grade': grade(score),
            'sqs': round(sqs),
            'tier': tier(sqs),
            'trigger': trigger_type,
            'sl': round(sl, 2),
            'tp1': round(tp1, 2),
            'tp2': round(tp2, 2),
            'tp3': round(tp3, 2),
            'rsi': round(last['rsi'], 1),
            'adx': round(last['adx'], 1),
            'regime': 'TRENDING' if last['adx'] > 25 else 'RANGING' if last['adx'] < 20 else 'TRANSITIONAL',
            'timeframe': TIMEFRAME
        }
    
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

# ═══════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════

def format_message(sig):
    emoji = "🟢" if sig['signal'] == 'BUY' else "🔴"
    
    msg = f"{sig['tier']} {emoji} *{sig['signal']} {sig['symbol']}* • {sig['timeframe']}\n"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"🎯 *{sig['trigger']}*\n"
    msg += f"📊 Score: *{sig['score']}/10* ({sig['grade']}) • SQS *{sig['sqs']}*\n\n"
    msg += f"💰 Entry: `{sig['price']}`\n"
    msg += f"🛑 SL: `{sig['sl']}`\n"
    msg += f"🎯 TP1: `{sig['tp1']}`\n"
    msg += f"🎯 TP2: `{sig['tp2']}`\n"
    msg += f"🎯 TP3: `{sig['tp3']}`\n\n"
    msg += f"📈 RSI: {sig['rsi']} | ADX: {sig['adx']}\n"
    msg += f"🏷️ Regime: {sig['regime']}\n"
    msg += f"⏰ {datetime.now().strftime('%H:%M UTC')}"
    return msg

def send_telegram(message, silent=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown',
            'disable_notification': silent
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main():
    print(f"\n{'='*50}")
    print(f"AlphaEdge v2 Scanner @ {datetime.now()}")
    print(f"Scanning {len(WATCHLIST)} symbols on {TIMEFRAME}")
    print(f"{'='*50}\n")
    
    cache = load_cache()
    sent = 0
    skipped_dupes = 0
    
    for symbol in WATCHLIST:
        print(f"→ {symbol}...", end=" ")
        result = analyze_symbol(symbol)
        
        if not result:
            print("no signal")
            continue
        
        # Check dupes
        if is_duplicate(symbol, result['signal'], cache):
            skipped_dupes += 1
            print(f"🔕 duplicate ({result['signal']}, cooldown active)")
            continue
        
        # Silent for FAIR tier
        silent = 'FAIR' in result['tier']
        
        print(f"🚨 {result['tier']} {result['signal']} | SQS={result['sqs']} | {result['trigger']}")
        
        if send_telegram(format_message(result), silent=silent):
            mark_sent(symbol, result['signal'], cache)
            sent += 1
            print(f"   ✅ Sent")
        else:
            print(f"   ❌ Failed")
    
    save_cache(cache)
    
    print(f"\n{'='*50}")
    print(f"Sent: {sent} | Dupes skipped: {skipped_dupes}")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
