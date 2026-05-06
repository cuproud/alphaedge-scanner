"""
ALPHAEDGE PYTHON SCANNER v1.0
Ports core AlphaEdge logic to Python
Runs free on GitHub Actions
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
from datetime import datetime

# ═══════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID', '820394470')

# Your watchlist - ADD/REMOVE symbols here
WATCHLIST = [
    # US Stocks
    'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMD', 'META', 
    'GOOGL', 'AMZN', 'NFLX',
    # Indices
    'SPY', 'QQQ', 'DIA',
    # Indian Stocks (add .NS suffix)
    'RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'HDFCBANK.NS',
    # Crypto
    'BTC-USD', 'ETH-USD', 'SOL-USD',
]

TIMEFRAME = '1h'       # 1m, 5m, 15m, 30m, 1h, 4h, 1d
LOOKBACK = '3mo'       # How much history to fetch
MIN_SQS = 60           # Only alert if SQS >= this (0-100)
MIN_SCORE = 5          # Min confluence score (0-10)

# ═══════════════════════════════════════════════
# INDICATORS (Pure pandas - no dependencies)
# ═══════════════════════════════════════════════

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def sma(series, length):
    return series.rolling(length).mean()

def rsi(series, length=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(length).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def atr(df, length=14):
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def macd(series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line

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

def supertrend(df, period=10, multiplier=3.0):
    """Supertrend indicator"""
    atr_val = atr(df, period)
    hl2 = (df['High'] + df['Low']) / 2
    upper = hl2 + (multiplier * atr_val)
    lower = hl2 - (multiplier * atr_val)
    
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
    """Session VWAP"""
    q = df['Volume']
    p = (df['High'] + df['Low'] + df['Close']) / 3
    return (p * q).cumsum() / q.cumsum()

# ═══════════════════════════════════════════════
# ALPHAEDGE SIGNAL ENGINE (Simplified)
# ═══════════════════════════════════════════════

def analyze_symbol(symbol):
    """Core AlphaEdge analysis - returns signal dict or None"""
    try:
        df = yf.download(symbol, period=LOOKBACK, interval=TIMEFRAME, 
                         progress=False, auto_adjust=True)
        
        if df.empty or len(df) < 200:
            return None
        
        # Flatten multi-index columns if yfinance returns them
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        # ─── Calculate all indicators ───
        df['ema50'] = ema(df['Close'], 50)
        df['ema200'] = ema(df['Close'], 200)
        df['rsi'] = rsi(df['Close'], 14)
        df['atr'] = atr(df, 14)
        df['macd'], df['signal'] = macd(df['Close'])
        df['adx'], df['plus_di'], df['minus_di'] = adx(df, 14)
        df['st'] = supertrend(df, 10, 3.0)
        df['vwap'] = vwap(df)
        df['vol_avg'] = df['Volume'].rolling(20).mean()
        
        # ─── Get latest values ───
        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = last['Close']
        
        # ─── Bull/Bear Score (0-10) ───
        bull_score = 0
        bear_score = 0
        
        # 1. Trend direction (price vs EMA50)
        if price > last['ema50']: bull_score += 1
        else: bear_score += 1
        
        # 2. Supertrend
        if last['st'] == 1: bull_score += 1
        else: bear_score += 1
        
        # 3. MACD
        if last['macd'] > last['signal']: bull_score += 1
        else: bear_score += 1
        
        # 4. RSI
        if last['rsi'] > 50: bull_score += 1
        else: bear_score += 1
        
        # 5. EMA50 vs EMA200
        if last['ema50'] > last['ema200']: bull_score += 1
        else: bear_score += 1
        
        # 6. Price vs VWAP
        if price > last['vwap']: bull_score += 1
        else: bear_score += 1
        
        # 7. ADX strength with direction
        if last['adx'] > 25:
            if last['plus_di'] > last['minus_di']: bull_score += 1
            else: bear_score += 1
        
        # 8. Volume surge
        if last['Volume'] > last['vol_avg'] * 1.5:
            if price > prev['Close']: bull_score += 1
            else: bear_score += 1
        
        # 9. RSI momentum
        if last['rsi'] > prev['rsi'] and last['rsi'] > 55: bull_score += 1
        if last['rsi'] < prev['rsi'] and last['rsi'] < 45: bear_score += 1
        
        # 10. Breakout from 20-bar range
        high_20 = df['High'].iloc[-21:-1].max()
        low_20 = df['Low'].iloc[-21:-1].min()
        if price > high_20: bull_score += 1
        if price < low_20: bear_score += 1
        
        # ─── Trigger detection (just crossed a key level) ───
        bull_trigger = (
            prev['Close'] <= prev['ema50'] and price > last['ema50']
        ) or (
            prev['st'] == -1 and last['st'] == 1
        ) or (
            prev['macd'] <= prev['signal'] and last['macd'] > last['signal'] and price > last['ema50']
        )
        
        bear_trigger = (
            prev['Close'] >= prev['ema50'] and price < last['ema50']
        ) or (
            prev['st'] == 1 and last['st'] == -1
        ) or (
            prev['macd'] >= prev['signal'] and last['macd'] < last['signal'] and price < last['ema50']
        )
        
        # ─── SQS Calculation (0-100) ───
        def calc_sqs(score, is_bull):
            conf = score / 10 * 40
            regime = 15 if last['adx'] > 25 else 8 if last['adx'] > 20 else 3
            vol = 10 if last['Volume'] > last['vol_avg'] * 1.5 else 5
            rsi_fit = 10 if 30 < last['rsi'] < 70 else 5
            mtf = 25 * (score / 10) if is_bull else 25 * ((10-score)/10)
            return min(100, conf + regime + vol + rsi_fit + mtf)
        
        # ─── Grade ───
        def grade(s):
            return "A+" if s >= 8 else "A" if s >= 6 else "B" if s >= 4 else "C"
        
        # ─── Determine signal ───
        signal_type = None
        score = 0
        sqs = 0
        
        if bull_trigger and bull_score >= MIN_SCORE:
            signal_type = 'BUY'
            score = bull_score
            sqs = calc_sqs(bull_score, True)
        elif bear_trigger and bear_score >= MIN_SCORE:
            signal_type = 'SELL'
            score = bear_score
            sqs = calc_sqs(bear_score, False)
        
        if not signal_type or sqs < MIN_SQS:
            return None
        
        # ─── Calculate TP/SL ───
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
            'sl': round(sl, 2),
            'tp1': round(tp1, 2),
            'tp2': round(tp2, 2),
            'tp3': round(tp3, 2),
            'rsi': round(last['rsi'], 1),
            'adx': round(last['adx'], 1),
            'regime': 'TRENDING' if last['adx'] > 25 else 'RANGING',
            'timeframe': TIMEFRAME
        }
    
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

# ═══════════════════════════════════════════════
# TELEGRAM SENDER
# ═══════════════════════════════════════════════

def format_message(sig):
    emoji = "🟢" if sig['signal'] == 'BUY' else "🔴"
    arrow = "▲" if sig['signal'] == 'BUY' else "▼"
    
    sqs_label = "ELITE" if sig['sqs'] >= 90 else "STRONG" if sig['sqs'] >= 75 else "GOOD" if sig['sqs'] >= 60 else "FAIR"
    
    msg = f"{emoji} *{sig['signal']} {sig['symbol']}* {sig['timeframe']}\n"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"📊 Score: *{sig['score']}/10* ({sig['grade']}) • SQS {sig['sqs']} ({sqs_label})\n"
    msg += f"💰 Entry: `{sig['price']}`\n"
    msg += f"🛑 SL: `{sig['sl']}`\n"
    msg += f"🎯 TP1: `{sig['tp1']}` | TP2: `{sig['tp2']}` | TP3: `{sig['tp3']}`\n"
    msg += f"📈 RSI: {sig['rsi']} | ADX: {sig['adx']}\n"
    msg += f"🏷️ Regime: {sig['regime']}\n"
    msg += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}"
    return msg

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown'
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ═══════════════════════════════════════════════
# MAIN SCANNER
# ═══════════════════════════════════════════════

def main():
    print(f"\n{'='*50}")
    print(f"AlphaEdge Scanner starting at {datetime.now()}")
    print(f"Scanning {len(WATCHLIST)} symbols on {TIMEFRAME}")
    print(f"{'='*50}\n")
    
    signals_found = 0
    
    for symbol in WATCHLIST:
        print(f"Scanning {symbol}...", end=" ")
        result = analyze_symbol(symbol)
        
        if result:
            print(f"🚨 {result['signal']} signal! Score={result['score']}, SQS={result['sqs']}")
            msg = format_message(result)
            if send_telegram(msg):
                signals_found += 1
                print(f"   ✅ Sent to Telegram")
            else:
                print(f"   ❌ Telegram send failed")
        else:
            print("no signal")
    
    print(f"\n{'='*50}")
    print(f"Scan complete. Signals sent: {signals_found}")
    print(f"{'='*50}\n")

if __name__ == "__main__":
    main()
