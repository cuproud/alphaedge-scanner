"""
ALPHAEDGE PYTHON SCANNER v4.1 - PRODUCTION
- Proper EST/EDT handling (auto DST via zoneinfo)
- Balanced signal filters (quality without being too strict)
- Active trade tracking with TP/SL hit notifications  
- Clean organized Telegram messages with live price
- Near-miss diagnostic logging
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ═══════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Auto EST/EDT handling via IANA timezone
EST = ZoneInfo("America/New_York")

def now_est():
    return datetime.now(EST)

def fmt_time():
    return now_est().strftime('%H:%M %Z')

def fmt_datetime():
    return now_est().strftime('%Y-%m-%d %H:%M %Z')

# 👇 YOUR WATCHLIST 👇
WATCHLIST = [
    'BTC-USD', 'ETH-USD', 'XRP-USD',
    'GC=F',
    'GOOGL', 'TSLA', 'AMD', 'NVDA', 'MSFT',
    'META', 'AMZN', 'NFLX',
    'MU', 'SNDK', 'NBIS', 'DRAM',
    'IONQ', 'RGTI', 'QBTS',
    'OKLO', 'IREN', 'UAMY', 'WGRX',
    'SOFI', 'NVO',
]

TIMEFRAME = '1h'
LOOKBACK = '3mo'

# ⚙️ BALANCED SETTINGS (v4.1)
MIN_SQS = 60               # was 65 — still quality, catches more
MIN_SCORE = 5              # was 6 — matches original good setups  
AI_TIER_THRESHOLD = 70     # AI on signals 70+
COOLDOWN_HOURS = 6         # was 4 — less re-alerting noise
MAX_TRADE_AGE_HOURS = 72   # auto-close trades older than 3 days

# 🔬 DEBUG: Show near-miss signals in logs
DEBUG_NEAR_MISS = True

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

ALERT_CACHE = 'alert_cache.json'
TRADES_FILE = 'active_trades.json'

def load_json(path, default):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)

def is_duplicate(symbol, signal_type, cache):
    key = f"{symbol}_{signal_type}"
    if key not in cache:
        return False
    try:
        last_time = datetime.fromisoformat(cache[key])
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=EST)
        return now_est() - last_time < timedelta(hours=COOLDOWN_HOURS)
    except:
        return False

def mark_sent(symbol, signal_type, cache):
    cache[f"{symbol}_{signal_type}"] = now_est().isoformat()

# ═══════════════════════════════════════════════
# ANALYSIS ENGINE
# ═══════════════════════════════════════════════

def analyze_symbol(symbol):
    try:
        df = yf.download(symbol, period=LOOKBACK, interval=TIMEFRAME,
                         progress=False, auto_adjust=True)
        
        if df.empty or len(df) < 200:
            return None, "insufficient data"
        
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
        price = float(last['Close'])
        atr_val = float(last['atr'])
        
        if atr_val <= 0 or pd.isna(atr_val):
            return None, "invalid ATR"
        
        # ══════════════════════════════════════════
        # QUALITY GATES (relaxed v4.1)
        # ══════════════════════════════════════════
        rsi_val = float(last['rsi'])
        adx_val = float(last['adx'])
        
        # Only EXTREME RSI blocks now (75/25 instead of 70/30)
        rsi_extreme_high = rsi_val >= 75
        rsi_extreme_low = rsi_val <= 25
        adx_exhausted = adx_val >= 60
        
        price_stretch = abs(price - float(last['ema50'])) / atr_val
        parabolic = price_stretch > 5.0
        
        htf_bullish = price > float(last['ema200'])
        htf_bearish = price < float(last['ema200'])
        
        # ══════════════════════════════════════════
        # CONFLUENCE SCORING
        # ══════════════════════════════════════════
        bull, bear = 0, 0
        
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
        if rsi_val > 50: bull += 1
        else: bear += 1
        if price > last['vwap']: bull += 1
        else: bear += 1
        if adx_val > 22:
            if last['plus_di'] > last['minus_di']: bull += 1
            else: bear += 1
        if last['Volume'] > last['vol_avg'] * 1.3:
            if price > prev['Close']: bull += 1
            else: bear += 1
        if rsi_val > prev['rsi']: bull += 1
        else: bear += 1
        
        # ══════════════════════════════════════════
        # TRIGGER DETECTION (slightly relaxed)
        # ══════════════════════════════════════════
        fresh_bull = (
            (prev['Close'] <= prev['ema50'] and price > last['ema50']) or
            (prev['st'] == -1 and last['st'] == 1) or
            (prev['macd'] <= prev['signal'] and last['macd'] > last['signal'])
        )
        fresh_bear = (
            (prev['Close'] >= prev['ema50'] and price < last['ema50']) or
            (prev['st'] == 1 and last['st'] == -1) or
            (prev['macd'] >= prev['signal'] and last['macd'] < last['signal'])
        )
        # Relaxed pullback: check last 3 bars, wider RSI range
        pullback_bull = (
            last['ema50'] > last['ema200'] and
            (df['Close'].iloc[-4:-1] < df['ema20'].iloc[-4:-1]).any() and
            price > last['ema20'] and
            45 < rsi_val < 70
        )
        pullback_bear = (
            last['ema50'] < last['ema200'] and
            (df['Close'].iloc[-4:-1] > df['ema20'].iloc[-4:-1]).any() and
            price < last['ema20'] and
            30 < rsi_val < 55
        )
        oversold_bounce = (
            prev['rsi'] < 32 and rsi_val > prev['rsi'] and
            price > prev['Close'] and bull >= 5
        )
        overbought_drop = (
            prev['rsi'] > 68 and rsi_val < prev['rsi'] and
            price < prev['Close'] and bear >= 5
        )
        strong_bull = (bull >= 7 and 22 < adx_val < 55 
                       and last['plus_di'] > last['minus_di']
                       and rsi_val < 72)
        strong_bear = (bear >= 7 and 22 < adx_val < 55
                       and last['minus_di'] > last['plus_di']
                       and rsi_val > 28)
        
        bull_trigger = fresh_bull or pullback_bull or oversold_bounce or strong_bull
        bear_trigger = fresh_bear or pullback_bear or overbought_drop or strong_bear
        
        trigger_type = ""
        if fresh_bull or fresh_bear: trigger_type = "Fresh Cross"
        elif pullback_bull or pullback_bear: trigger_type = "Pullback"
        elif oversold_bounce: trigger_type = "Oversold Bounce"
        elif overbought_drop: trigger_type = "Overbought Drop"
        elif strong_bull or strong_bear: trigger_type = "Strong Momentum"
        
        # ══════════════════════════════════════════
        # HARD BLOCKS (relaxed — only extremes)
        # ══════════════════════════════════════════
        if bull_trigger:
            if rsi_extreme_high and trigger_type != "Pullback":
                return None, f"RSI too hot ({rsi_val:.0f})"
            if adx_exhausted:
                return None, f"ADX exhausted ({adx_val:.0f})"
            if parabolic and trigger_type == "Strong Momentum":
                return None, f"parabolic ({price_stretch:.1f}×ATR)"
            # Only block HTF-counter on momentum chases
            if htf_bearish and trigger_type == "Strong Momentum":
                return None, "counter-HTF momentum"
        
        if bear_trigger:
            if rsi_extreme_low and trigger_type != "Pullback":
                return None, f"RSI too cold ({rsi_val:.0f})"
            if adx_exhausted:
                return None, f"ADX exhausted ({adx_val:.0f})"
            if parabolic and trigger_type == "Strong Momentum":
                return None, f"parabolic ({price_stretch:.1f}×ATR)"
            if htf_bullish and trigger_type == "Strong Momentum":
                return None, "counter-HTF momentum"
        
        # ══════════════════════════════════════════
        # SQS SCORING
        # ══════════════════════════════════════════
        def calc_sqs(score, is_bull):
            conf = score / 10 * 40
            
            if 22 < adx_val < 50:
                regime = 15
            elif adx_val > 20:
                regime = 10
            else:
                regime = 5
            
            vol = 10 if last['Volume'] > last['vol_avg'] * 1.5 else 6
            
            if 40 <= rsi_val <= 60:
                rsi_fit = 10
            elif 30 <= rsi_val <= 70:
                rsi_fit = 7
            else:
                rsi_fit = 2
            
            if is_bull and last['ema50'] > last['ema200'] and price > last['ema200']:
                trend = 15
            elif not is_bull and last['ema50'] < last['ema200'] and price < last['ema200']:
                trend = 15
            else:
                trend = 5
            
            if price_stretch > 4:
                trend = max(0, trend - 10)
            
            return max(0, min(100, conf + regime + vol + rsi_fit + trend))
        
        def grade(s):
            return "A+" if s >= 8 else "A" if s >= 6 else "B" if s >= 4 else "C"
        
        def tier(sqs):
            if sqs >= 85: return "🏆 ELITE"
            elif sqs >= 70: return "⭐ STRONG"
            elif sqs >= 55: return "✅ GOOD"
            else: return "⚠️ FAIR"
        
        # Determine signal
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
            # Near-miss reporting
            if bull >= 7 and not bull_trigger:
                return None, f"bull={bull} no trigger (rsi={rsi_val:.0f})"
            if bear >= 7 and not bear_trigger:
                return None, f"bear={bear} no trigger (rsi={rsi_val:.0f})"
            return None, None
        
        # ══════════════════════════════════════════
        # STRUCTURE-BASED SL/TP
        # ══════════════════════════════════════════
        lookback = 10
        recent_low = float(df['Low'].iloc[-lookback-1:-1].min())
        recent_high = float(df['High'].iloc[-lookback-1:-1].max())
        
        if signal_type == 'BUY':
            atr_sl = price - (atr_val * 2)
            struct_sl = recent_low - (atr_val * 0.2)
            sl = min(atr_sl, struct_sl)
            min_sl = price - (atr_val * 0.5)
            sl = min(sl, min_sl)
            risk = price - sl
            tp1 = price + risk * 1
            tp2 = price + risk * 2
            tp3 = price + risk * 3
        else:
            atr_sl = price + (atr_val * 2)
            struct_sl = recent_high + (atr_val * 0.2)
            sl = max(atr_sl, struct_sl)
            min_sl = price + (atr_val * 0.5)
            sl = max(sl, min_sl)
            risk = sl - price
            tp1 = price - risk * 1
            tp2 = price - risk * 2
            tp3 = price - risk * 3
        
        decimals = 4 if price < 10 else 2
        
        return {
            'symbol': symbol,
            'signal': signal_type,
            'price': round(price, decimals),
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
            'rsi': round(rsi_val, 1),
            'adx': round(adx_val, 1),
            'stretch': round(price_stretch, 1),
            'regime': 'TRENDING' if adx_val > 25 else 'RANGING' if adx_val < 20 else 'TRANSITIONAL',
            'timeframe': TIMEFRAME,
            'decimals': decimals
        }, None
    
    except Exception as e:
        return None, f"error: {e}"

# ═══════════════════════════════════════════════
# LIVE PRICE FETCH
# ═══════════════════════════════════════════════

def get_live_price(symbol):
    """Fetch latest price for trade monitoring. Returns (close, high, low) from most recent 5m bar."""
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
# TRADE TRACKING
# ═══════════════════════════════════════════════

def create_trade(sig):
    return {
        'symbol': sig['symbol'],
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
        'opened_at': now_est().isoformat(),
        'tp1_hit': False,
        'tp2_hit': False,
        'tp3_hit': False,
        'closed': False,
        'closed_reason': None,
        'closed_at': None
    }

def check_trade_progress(trade):
    """Returns (events, is_closed)."""
    result = get_live_price(trade['symbol'])
    if not result:
        return [], False
    
    current, high, low = result
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
            events.append({'type': 'TIMEOUT', 'price': current})
            return events, True
    except:
        pass
    
    # SL hit (pessimistic — check first)
    if is_long and low <= trade['sl']:
        trade['closed'] = True
        trade['closed_reason'] = 'SL Hit'
        trade['closed_at'] = now_est().isoformat()
        events.append({'type': 'SL', 'price': trade['sl']})
        return events, True
    if not is_long and high >= trade['sl']:
        trade['closed'] = True
        trade['closed_reason'] = 'SL Hit'
        trade['closed_at'] = now_est().isoformat()
        events.append({'type': 'SL', 'price': trade['sl']})
        return events, True
    
    # TP hits
    if is_long:
        if not trade['tp1_hit'] and high >= trade['tp1']:
            trade['tp1_hit'] = True
            events.append({'type': 'TP1', 'price': trade['tp1']})
        if not trade['tp2_hit'] and high >= trade['tp2']:
            trade['tp2_hit'] = True
            events.append({'type': 'TP2', 'price': trade['tp2']})
        if not trade['tp3_hit'] and high >= trade['tp3']:
            trade['tp3_hit'] = True
            events.append({'type': 'TP3', 'price': trade['tp3']})
            trade['closed'] = True
            trade['closed_reason'] = 'TP3 Hit (Full Target)'
            trade['closed_at'] = now_est().isoformat()
            return events, True
    else:
        if not trade['tp1_hit'] and low <= trade['tp1']:
            trade['tp1_hit'] = True
            events.append({'type': 'TP1', 'price': trade['tp1']})
        if not trade['tp2_hit'] and low <= trade['tp2']:
            trade['tp2_hit'] = True
            events.append({'type': 'TP2', 'price': trade['tp2']})
        if not trade['tp3_hit'] and low <= trade['tp3']:
            trade['tp3_hit'] = True
            events.append({'type': 'TP3', 'price': trade['tp3']})
            trade['closed'] = True
            trade['closed_reason'] = 'TP3 Hit (Full Target)'
            trade['closed_at'] = now_est().isoformat()
            return events, True
    
    return events, False

# ═══════════════════════════════════════════════
# AI ANALYSIS
# ═══════════════════════════════════════════════

def get_ai_analysis(sig):
    if not GEMINI_API_KEY:
        return None
    
    prompt = f"""You are an expert trading analyst. Analyze this signal in EXACTLY 3 short lines (max 100 chars each).

SYMBOL: {sig['symbol']} ({sig['signal']} @ ${sig['price']})
TRIGGER: {sig['trigger']} | Score: {sig['score']}/10 | SQS: {sig['sqs']}/100
RSI: {sig['rsi']} | ADX: {sig['adx']} | Regime: {sig['regime']}
Risk/Reward: 1:3 | Stretch: {sig['stretch']}×ATR

Respond EXACTLY in this format (no extra text):
📝 {sig['symbol']}: [setup quality in one line]
⚠️ Risk: [main concern]
💡 Verdict: [STRONG BUY/BUY/NEUTRAL/CAUTION/AVOID]"""
    
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
        print(f"  AI error: {e}")
        return None

# ═══════════════════════════════════════════════
# MESSAGE FORMATTING
# ═══════════════════════════════════════════════

def fmt_price(val, decimals):
    return f"{val:.{decimals}f}"

def format_new_signal(sig, ai_text=None):
    emoji = "🟢" if sig['signal'] == 'BUY' else "🔴"
    dec = sig['decimals']
    
    msg = f"{sig['tier']} {emoji} *{sig['signal']} {sig['symbol']}*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"💵 *Live Price:* `${fmt_price(sig['price'], dec)}`\n"
    msg += f"🎯 *Trigger:* {sig['trigger']}\n"
    msg += f"📊 *Quality:* {sig['score']}/10 ({sig['grade']}) • SQS *{sig['sqs']}*\n"
    msg += f"\n"
    msg += f"*🎯 TRADE PLAN*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"📍 Entry:  `${fmt_price(sig['price'], dec)}`\n"
    msg += f"🛑 SL:     `${fmt_price(sig['sl'], dec)}`\n"
    msg += f"🎯 TP1:    `${fmt_price(sig['tp1'], dec)}` (1R)\n"
    msg += f"🎯 TP2:    `${fmt_price(sig['tp2'], dec)}` (2R)\n"
    msg += f"🎯 TP3:    `${fmt_price(sig['tp3'], dec)}` (3R)\n"
    msg += f"💰 Risk:   `${fmt_price(sig['risk'], dec)}` | R:R = 1:3\n"
    msg += f"\n"
    msg += f"*📈 TECHNICALS*\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"RSI: `{sig['rsi']}` | ADX: `{sig['adx']}` | Stretch: `{sig['stretch']}×`\n"
    msg += f"Regime: {sig['regime']} | TF: {sig['timeframe']}\n"
    
    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n"
        msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
        msg += f"{ai_text}\n"
    
    msg += f"\n⏰ {fmt_time()}"
    return msg

def format_trade_event(trade, event, current_price):
    emoji = "🟢" if trade['signal'] == 'BUY' else "🔴"
    dec = trade['decimals']
    
    tp1_mark = "✅" if trade['tp1_hit'] else "⭕"
    tp2_mark = "✅" if trade['tp2_hit'] else "⭕"
    tp3_mark = "✅" if trade['tp3_hit'] else "⭕"
    
    event_type = event['type']
    
    if event_type == 'TP1':
        header = f"✅ *TP1 HIT* {emoji} {trade['symbol']}"
        sub = "Move SL to breakeven 🔒"
        r_mult = "+1R"
    elif event_type == 'TP2':
        header = f"✅✅ *TP2 HIT* {emoji} {trade['symbol']}"
        sub = "Trail SL to TP1 📈"
        r_mult = "+2R"
    elif event_type == 'TP3':
        header = f"🏆 *TP3 HIT — FULL TARGET* {emoji} {trade['symbol']}"
        sub = "Trade complete! 🎉"
        r_mult = "+3R"
    elif event_type == 'SL':
        header = f"🛑 *SL HIT* {emoji} {trade['symbol']}"
        if trade['tp1_hit']:
            sub = "Trailed profit exit"
            r_mult = "Partial gain"
        else:
            sub = "Stop loss hit — trade closed"
            r_mult = "-1R"
    elif event_type == 'TIMEOUT':
        header = f"⏰ *TRADE TIMEOUT* {emoji} {trade['symbol']}"
        sub = "72h expiry — auto-closed"
        r_mult = "—"
    else:
        return None
    
    msg = f"{header}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"💵 *Live:* `${fmt_price(current_price, dec)}`\n"
    msg += f"🎯 *Hit:* `${fmt_price(event['price'], dec)}` ({r_mult})\n"
    msg += f"💡 {sub}\n"
    msg += f"\n"
    msg += f"*TRADE PROGRESS*\n"
    msg += f"Entry:  `${fmt_price(trade['entry'], dec)}`\n"
    msg += f"{tp1_mark} TP1: `${fmt_price(trade['tp1'], dec)}`\n"
    msg += f"{tp2_mark} TP2: `${fmt_price(trade['tp2'], dec)}`\n"
    msg += f"{tp3_mark} TP3: `${fmt_price(trade['tp3'], dec)}`\n"
    msg += f"\n⏰ {fmt_time()}"
    
    return msg

# ═══════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════

def send_telegram(message, silent=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown',
            'disable_notification': silent
        }, timeout=10)
        if r.status_code != 200:
            print(f"  Telegram error {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"  Telegram error: {e}")
        return False

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main():
    print(f"\n{'='*55}")
    print(f"AlphaEdge v4.1 Scanner @ {fmt_datetime()}")
    print(f"Scanning {len(WATCHLIST)} symbols on {TIMEFRAME}")
    print(f"AI: {bool(GEMINI_API_KEY)} | MIN_SQS: {MIN_SQS} | MIN_SCORE: {MIN_SCORE}")
    print(f"{'='*55}\n")
    
    cache = load_json(ALERT_CACHE, {})
    trades = load_json(TRADES_FILE, {})
    
    # ═══════════════════════════════════════════════
    # STEP 1: Check active trades
    # ═══════════════════════════════════════════════
    if trades:
        print(f"📊 Checking {len(trades)} active trade(s)...")
        trades_to_remove = []
        
        for trade_key, trade in trades.items():
            if trade.get('closed'):
                trades_to_remove.append(trade_key)
                continue
            
            print(f"  → {trade['symbol']} ({trade['signal']})...", end=" ")
            events, closed = check_trade_progress(trade)
            
            if not events:
                print("no change")
                continue
            
            live_result = get_live_price(trade['symbol'])
            current = live_result[0] if live_result else trade['entry']
            
            for event in events:
                msg = format_trade_event(trade, event, current)
                if msg:
                    send_telegram(msg, silent=False)
                    print(f"🔔 {event['type']}", end=" ")
            
            if closed:
                trades_to_remove.append(trade_key)
                print("✅ closed")
            else:
                print()
        
        for key in trades_to_remove:
            del trades[key]
        
        save_json(TRADES_FILE, trades)
        print()
    
    # ═══════════════════════════════════════════════
    # STEP 2: Scan for new signals
    # ═══════════════════════════════════════════════
    print(f"🔍 Scanning for new signals...")
    sent = 0
    skipped_dupe = 0
    skipped_active = 0
    ai_calls = 0
    near_misses = 0
    
    for symbol in WATCHLIST:
        print(f"  → {symbol:12s}...", end=" ")
        
        active_key = f"{symbol}_active"
        if active_key in trades and not trades[active_key].get('closed'):
            skipped_active += 1
            print(f"🔒 active trade")
            continue
        
        result, reason = analyze_symbol(symbol)
        
        if not result:
            if DEBUG_NEAR_MISS and reason:
                print(f"⚪ {reason}")
                near_misses += 1
            else:
                print("no signal")
            continue
        
        if is_duplicate(symbol, result['signal'], cache):
            skipped_dupe += 1
            print(f"🔕 cooldown")
            continue
        
        ai_text = None
        if result['sqs'] >= AI_TIER_THRESHOLD and GEMINI_API_KEY:
            print(f"🤖", end=" ")
            ai_text = get_ai_analysis(result)
            if ai_text:
                ai_calls += 1
        
        silent = 'FAIR' in result['tier']
        print(f"🚨 {result['tier']} {result['signal']} SQS={result['sqs']}")
        
        msg = format_new_signal(result, ai_text)
        if send_telegram(msg, silent=silent):
            mark_sent(symbol, result['signal'], cache)
            trades[active_key] = create_trade(result)
            sent += 1
            print(f"     ✅ Alert sent + trade tracked")
        else:
            print(f"     ❌ Send failed")
    
    save_json(ALERT_CACHE, cache)
    save_json(TRADES_FILE, trades)
    
    print(f"\n{'='*55}")
    print(f"✅ New: {sent} | 🔕 Cooldown: {skipped_dupe} | 🔒 Active: {skipped_active} | ⚪ Near-miss: {near_misses}")
    print(f"🤖 AI calls: {ai_calls} | 📊 Open trades: {len(trades)}")
    print(f"{'='*55}")

if __name__ == "__main__":
    main()
