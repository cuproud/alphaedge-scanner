"""
ALPHAEDGE MARKET INTELLIGENCE MODULE v2.2
═══════════════════════════════════════════════════════════════
Provides CONTEXT, not just signals:
• Big move detection (±5%, ±10%)
• ATH / 52W / position in range
• AI-powered "why is this moving?" analysis
• Clear BUY ZONE / HOLD / AVOID verdicts
• Sector bleed detection
• Leadership / laggard detection (relative strength)
• Earnings calendar check
• Wilder's RMA RSI (matches scanner.py)

v2.2 CHANGES vs v2.1:
• Fixed TAKE PROFITS entry guidance (was "no edge" — now trim/re-entry levels)
• Added PARABOLIC verdict case for big single-day moves
• ATH recency label ("set YESTERDAY 🔥" vs "set 2y ago")
• Abnormal 52W range detection (spin-offs, corporate actions)
• Volume context for big gains (low volume = thin/news-driven warning)
• Fixed verdict engine — added momentum/ATH continuation case
• Smarter AI prompt with pre-interpreted context
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
import time
import logging
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

warnings.filterwarnings('ignore', category=FutureWarning)

# ═══════════════════════════════════════════════
# GLOBALS
# ═══════════════════════════════════════════════
EST = ZoneInfo("America/New_York")

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

STATE_FILE = 'scanner_state.json'
SYMBOLS_YAML = 'symbols.yaml'
LOGS_DIR = Path('logs')
LOGS_DIR.mkdir(exist_ok=True)

def _setup_logger():
    logging.basicConfig(
        filename=LOGS_DIR / f'intel_{datetime.now(EST).strftime("%Y-%m-%d")}.log',
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s'
    )
_setup_logger()

# ═══════════════════════════════════════════════
# THRESHOLDS
# ═══════════════════════════════════════════════
BIG_DROP_WARN     = -5.0
BIG_DROP_CRITICAL = -10.0
BIG_GAIN_ALERT    = 8.0
NEAR_52W_LOW_PCT  = 10.0
ATH_PULLBACK_ALERT = -15.0

COOLDOWN_HOURS         = 4
SECTOR_BLEED_COOLDOWN  = 4
LEADERSHIP_COOLDOWN    = 3

EARNINGS_WARNING_DAYS  = 3
FETCH_DELAY            = 0.3


# ═══════════════════════════════════════════════
# SYMBOLS — loaded from symbols.yaml with hardcoded fallback
# ═══════════════════════════════════════════════

def _load_from_yaml():
    yaml_path = Path(SYMBOLS_YAML)
    if not yaml_path.exists():
        return None
    try:
        import yaml
        with open(yaml_path, 'r', encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}

        emoji_map  = {}
        sector_map = {}
        all_syms   = []

        for bucket in ('crypto', 'extended_hours', 'regular_hours'):
            for item in (raw.get(bucket) or []):
                sym = item['symbol']
                all_syms.append(sym)
                emoji_map[sym]  = item.get('emoji', '📊')
                sector_map[sym] = item.get('sector', 'Other')

        sectors = {}
        for sym, sec in sector_map.items():
            sectors.setdefault(sec, []).append(sym)

        return all_syms, sectors, emoji_map
    except Exception as e:
        logging.warning(f"symbols.yaml load failed: {e} — using hardcoded fallback")
        return None


_yaml_result = _load_from_yaml()

if _yaml_result:
    MONITOR_LIST, SECTORS, SYMBOL_EMOJI = _yaml_result
    logging.info(f"market_intel: loaded {len(MONITOR_LIST)} symbols from symbols.yaml")
else:
    SECTORS = {
        'AI/Semis':       ['NVDA', 'AMD', 'MU', 'SNDK', 'NBIS'],
        'Crypto':         ['BTC-USD', 'ETH-USD', 'XRP-USD'],
        'Crypto-Adj':     ['IREN', 'SOFI'],
        'Quantum':        ['IONQ', 'RGTI', 'QBTS'],
        'Nuclear/Energy': ['OKLO', 'UAMY'],
        'Mega Tech':      ['GOOGL', 'MSFT', 'META', 'AMZN', 'AAPL'],
        'EV/Auto':        ['TSLA'],
        'Fintech':        ['SOFI'],
        'Biotech':        ['NVO', 'WGRX'],
        'Streaming':      ['NFLX'],
        'Safe Haven':     ['GC=F'],
    }
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
    logging.info("market_intel: symbols.yaml not found — using hardcoded fallback")

# Reverse lookup
SYMBOL_TO_SECTOR = {}
for _sector, _syms in SECTORS.items():
    for _sym in _syms:
        SYMBOL_TO_SECTOR[_sym] = _sector


# ═══════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════

def now_est():
    return datetime.now(EST)

def load_json(path, default):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)

def _clean_df(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def rma(series, length):
    return series.ewm(alpha=1.0 / length, adjust=False).mean()

def pine_rsi(src, length=14):
    delta   = src.diff()
    gain    = delta.clip(lower=0)
    loss    = -delta.clip(upper=0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def ath_recency_label(ath_date_str):
    """Human-readable label for how recent the ATH was."""
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


# ═══════════════════════════════════════════════
# EARNINGS CALENDAR
# ═══════════════════════════════════════════════

def get_earnings_date(symbol):
    if symbol.endswith('-USD') or symbol == 'GC=F':
        return None, None
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is None:
            return None, None

        earnings_date = None
        if isinstance(cal, dict):
            ed = cal.get('Earnings Date')
            if ed:
                earnings_date = ed[0] if isinstance(ed, list) and len(ed) > 0 else ed
        elif hasattr(cal, 'loc'):
            try:
                if 'Earnings Date' in cal.index:
                    earnings_date = cal.loc['Earnings Date'].iloc[0]
            except Exception:
                pass

        if earnings_date is None:
            return None, None

        if isinstance(earnings_date, str):
            earnings_date = datetime.fromisoformat(earnings_date.split('T')[0])
        elif hasattr(earnings_date, 'to_pydatetime'):
            earnings_date = earnings_date.to_pydatetime()
        if hasattr(earnings_date, 'date'):
            earnings_date = earnings_date.date()

        today = now_est().date()
        days_until = (earnings_date - today).days
        if days_until < 0 or days_until > 60:
            return None, None
        return earnings_date, days_until
    except Exception as e:
        logging.debug(f"Earnings {symbol}: {e}")
        return None, None

def format_earnings_warning(symbol, earnings_date, days_until):
    if earnings_date is None:
        return None
    if days_until <= 0:
        return "🚨 *Earnings TODAY* — extreme volatility risk"
    if days_until == 1:
        return f"⚠️ *Earnings TOMORROW* ({earnings_date}) — SKIP new longs"
    if days_until <= EARNINGS_WARNING_DAYS:
        return f"⚠️ *Earnings in {days_until} days* ({earnings_date}) — consider waiting"
    if days_until <= 7:
        return f"📅 Earnings in {days_until} days ({earnings_date})"
    return None


# ═══════════════════════════════════════════════
# MARKET CONTEXT
# ═══════════════════════════════════════════════

def get_market_ctx():
    try:
        data = {}
        for t in ['SPY', 'QQQ', '^VIX']:
            df = yf.download(t, period='5d', interval='1d',
                             progress=False, auto_adjust=True)
            if df.empty:
                continue
            df = _clean_df(df)
            last = float(df['Close'].iloc[-1])
            prev = float(df['Close'].iloc[-2])
            data[t] = {'price': last, 'pct': (last - prev) / prev * 100}
        return data
    except Exception as e:
        logging.error(f"Market ctx: {e}")
        return None


# ═══════════════════════════════════════════════
# RELATIVE STRENGTH
# ═══════════════════════════════════════════════

def calc_relative_strength(ctx, benchmark='SPY', lookback_days=5):
    try:
        df_sym   = yf.download(ctx['symbol'], period='1mo', interval='1d',
                               progress=False, auto_adjust=True)
        df_bench = yf.download(benchmark, period='1mo', interval='1d',
                               progress=False, auto_adjust=True)
        if df_sym.empty or df_bench.empty:
            return None, None
        df_sym   = _clean_df(df_sym)
        df_bench = _clean_df(df_bench)

        if len(df_sym) < lookback_days + 1 or len(df_bench) < lookback_days + 1:
            return None, None

        sym_perf   = (df_sym['Close'].iloc[-1]   / df_sym['Close'].iloc[-(lookback_days + 1)]   - 1) * 100
        bench_perf = (df_bench['Close'].iloc[-1] / df_bench['Close'].iloc[-(lookback_days + 1)] - 1) * 100
        diff = float(sym_perf - bench_perf)

        if diff > 5:     label = "🟢🟢 Strong Leader"
        elif diff > 2:   label = "🟢 Outperforming"
        elif diff > -2:  label = "⚖️ In-line"
        elif diff > -5:  label = "🔴 Underperforming"
        else:            label = "🔴🔴 Weak / Laggard"

        return round(diff, 2), label
    except Exception as e:
        logging.debug(f"RS {ctx['symbol']}: {e}")
        return None, None


# ═══════════════════════════════════════════════
# FULL CONTEXT
# ═══════════════════════════════════════════════

def get_full_context(symbol):
    try:
        daily = yf.download(symbol, period='5y', interval='1d',
                            progress=False, auto_adjust=True)
        if daily.empty or len(daily) < 50:
            return None
        daily = _clean_df(daily)

        intraday = yf.download(symbol, period='2d', interval='5m',
                               progress=False, auto_adjust=True)
        if intraday.empty:
            return None
        intraday = _clean_df(intraday)

        current    = float(intraday['Close'].iloc[-1])
        prev_close = float(daily['Close'].iloc[-2])

        today_date = now_est().date()
        try:
            if intraday.index.tz is None:
                intraday_tz = intraday.tz_localize('UTC').tz_convert(EST)
            else:
                intraday_tz = intraday.tz_convert(EST)
            today_bars = intraday_tz[intraday_tz.index.date == today_date]
            if today_bars.empty:
                today_bars = intraday.iloc[-78:]
        except Exception:
            today_bars = intraday.iloc[-78:]

        today_open = float(today_bars['Open'].iloc[0])
        today_high = float(today_bars['High'].max())
        today_low  = float(today_bars['Low'].min())
        vol_today  = float(today_bars['Volume'].sum())

        day_change_pct = (current - prev_close) / prev_close * 100
        intraday_pct   = (current - today_open) / today_open * 100

        ath      = float(daily['High'].max())
        ath_date = daily['High'].idxmax()
        low_52w  = float(daily['Low'].iloc[-252:].min())  if len(daily) >= 252 else float(daily['Low'].min())
        high_52w = float(daily['High'].iloc[-252:].max()) if len(daily) >= 252 else float(daily['High'].max())

        ath_pct          = (current - ath) / ath * 100
        pct_from_52w_low  = (current - low_52w)  / low_52w  * 100 if low_52w  > 0 else 0
        pct_from_52w_high = (current - high_52w) / high_52w * 100 if high_52w > 0 else 0
        range_pos         = ((current - low_52w) / (high_52w - low_52w) * 100) if high_52w > low_52w else 50

        ema20  = float(daily['Close'].ewm(span=20,  adjust=False).mean().iloc[-1])
        ema50  = float(daily['Close'].ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200 = float(daily['Close'].ewm(span=200, adjust=False).mean().iloc[-1]) if len(daily) >= 200 else ema50

        rsi_series = pine_rsi(daily['Close'], 14)
        rsi = float(rsi_series.iloc[-1])

        vol_avg_20d = float(daily['Volume'].iloc[-20:].mean())
        vol_ratio   = vol_today / vol_avg_20d if vol_avg_20d > 0 else 1.0

        if   current > ema20 > ema50 > ema200: trend = "🚀 STRONG UPTREND"
        elif current < ema20 < ema50 < ema200: trend = "💀 STRONG DOWNTREND"
        elif current > ema50 > ema200:          trend = "📈 UPTREND"
        elif current < ema50 < ema200:          trend = "📉 DOWNTREND"
        elif current > ema200 and current < ema50: trend = "🔄 PULLBACK IN UPTREND"
        elif current < ema200 and current > ema50: trend = "🔀 RECOVERING"
        else:                                   trend = "⚖️ MIXED"

        return {
            'symbol':           symbol,
            'current':          current,
            'prev_close':       prev_close,
            'today_open':       today_open,
            'today_high':       today_high,
            'today_low':        today_low,
            'day_change_pct':   day_change_pct,
            'intraday_pct':     intraday_pct,
            'ath':              ath,
            'ath_date':         ath_date.strftime('%Y-%m-%d') if hasattr(ath_date, 'strftime') else str(ath_date)[:10],
            'ath_pct':          ath_pct,
            'low_52w':          low_52w,
            'high_52w':         high_52w,
            'pct_from_52w_low': pct_from_52w_low,
            'pct_from_52w_high':pct_from_52w_high,
            'range_pos':        range_pos,
            'ema20':            ema20,
            'ema50':            ema50,
            'ema200':           ema200,
            'rsi':              rsi if not np.isnan(rsi) else 50,
            'vol_ratio':        vol_ratio,
            'trend':            trend,
        }
    except Exception as e:
        logging.error(f"Context {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════
# VERDICT ENGINE v2.2 — improved logic
# ═══════════════════════════════════════════════

def get_verdict(ctx, market_ctx=None):
    """Returns (verdict, zone, [reasons])."""
    c         = ctx
    rsi       = c['rsi']
    trend     = c['trend']
    drop      = c['day_change_pct']
    from_ath  = c['ath_pct']
    range_pos = c['range_pos']
    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']

    reasons = []
    verdict = None
    zone    = None

    # ── 0. PARABOLIC single-day spike ──
    if abs(drop) >= 15:
        if drop > 0:
            verdict = "⚠️ PARABOLIC"
            zone = f"News/Catalyst Spike +{drop:.0f}%"
            reasons = [
                f"+{drop:.1f}% single-day — likely news/catalyst driven",
                "Parabolic moves mean-revert — high risk to chase",
                f"Volume {c['vol_ratio']:.1f}× avg — confirms activity",
            ]
        else:
            verdict = "🚨 CRASH"
            zone = f"Severe Drop {drop:.0f}%"
            reasons = [
                f"{drop:.1f}% single-day — likely news driven",
                "Wait for dust to settle before any entry",
            ]
        return verdict, zone, reasons

    # ── 1. MOMENTUM — at/near ATH in strong uptrend ──
    if ("UPTREND" in trend and from_ath > -5 and above_50 and above_200 and rsi < 80):
        verdict = "🚀 MOMENTUM"
        zone    = "AT ATH — Continuation"
        reasons = [
            f"At/near all-time high ({from_ath:+.1f}%)",
            "EMA stack fully bullish",
            f"RSI {rsi:.0f} — not overbought, room to run",
        ]

    # ── 2. STRONG UPTREND PULLBACK → BUY ZONE ──
    elif "UPTREND" in trend and rsi < 52 and above_200:
        verdict = "🟢 BUY ZONE"
        zone    = "Pullback in Uptrend"
        reasons = ["Healthy pullback in confirmed uptrend", f"RSI {rsi:.0f} — room to run"]
        if from_ath > -20:
            reasons.append("Near ATH — strong stock pulling back")

    # ── 3. EMA50 PULLBACK ──
    elif "PULLBACK" in trend and rsi < 55:
        verdict = "🟢 BUY ZONE"
        zone    = "EMA50 Pullback"
        reasons = [
            "Above EMA200 — uptrend structure intact",
            f"Pulling toward EMA50 ${c['ema50']:.2f}",
            f"RSI {rsi:.0f} — watch for bounce",
        ]

    # ── 4. EXTENDED NEAR ATH ──
    elif from_ath > -8 and rsi > 75:
        verdict = "🟠 EXTENDED"
        zone    = "Overbought Near ATH"
        reasons = [f"RSI {rsi:.0f} — overbought at highs", "Risk/reward not ideal for new entry"]

    # ── 5. STRONG DOWNTREND ──
    elif "DOWNTREND" in trend and not above_200:
        verdict = "🔴 AVOID"
        zone    = "Falling Knife"
        reasons = ["Below EMA50 & EMA200 — confirmed downtrend", "No base formed"]
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal")

    # ── 6. NEAR 52W LOW ──
    elif c['pct_from_52w_low'] < 8 and drop < -3:
        verdict = "⚠️ CAUTION"
        zone    = "Breaking Down"
        reasons = ["Near 52W low — key support at risk", "Wait for base formation"]

    # ── 7. OVERBOUGHT (non-ATH) ──
    elif rsi > 75 and drop > 2:
        verdict = "🟠 TAKE PROFITS"
        zone    = "Extended"
        reasons = [f"RSI overbought ({rsi:.0f})", "Consider trimming, not entering"]

    # ── 8. RECOVERING ──
    elif "RECOVERING" in trend:
        if rsi > 55 and drop > 0:
            verdict = "🟡 WATCH"
            zone    = "Recovery Attempt"
            reasons = ["Reclaiming EMA50 — potential recovery", f"Must clear EMA200 ${c['ema200']:.2f}"]
        else:
            verdict = "⏸️ HOLD"
            zone    = "Below EMA200"
            reasons = ["Below EMA200 — no structural confirmation"]

    # ── 9. MIXED ──
    elif "MIXED" in trend:
        if range_pos < 30 and rsi < 45:
            verdict = "🟡 WATCH"
            zone    = "Potential Accumulation"
            reasons = ["Lower 52W range — possible accumulation", "Wait for trend confirmation"]
        elif rsi > 72:
            verdict = "🟠 EXTENDED"
            zone    = "Overbought in Chop"
            reasons = [f"RSI {rsi:.0f} extended in mixed trend"]
        else:
            verdict = "⏸️ HOLD"
            zone    = "No Edge"
            reasons = ["Mixed signals — wait for clarity"]

    # ── 10. DEFAULT ──
    else:
        if above_50 and above_200 and rsi > 55:
            verdict = "🟡 WATCH"
            zone    = "Building Momentum"
            reasons = ["Above both EMAs — structure improving", f"RSI {rsi:.0f} — momentum building"]
        elif drop < -5:
            verdict = "⚠️ WATCH"
            zone    = "Sharp Drop"
            reasons = ["Large move — wait for stabilisation"]
        else:
            verdict = "⏸️ NEUTRAL"
            zone    = "No Clear Setup"
            reasons = ["No strong directional signal"]

    # ── Market context override ──
    if market_ctx:
        vix     = market_ctx.get('^VIX', {}).get('price', 15)
        spy_pct = market_ctx.get('SPY',  {}).get('pct', 0)
        if vix > 25 and spy_pct < -1.5 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
            verdict = "⚠️ WAIT"
            reasons.insert(0, f"Market bleeding — VIX {vix:.0f}, SPY {spy_pct:.1f}%")

    # ── Earnings override ──
    if any(x in verdict for x in ["BUY", "MOMENTUM", "WATCH"]):
        _, days_until = get_earnings_date(c['symbol'])
        if days_until is not None and days_until <= EARNINGS_WARNING_DAYS:
            verdict = "⚠️ WAIT — Earnings"
            zone    = f"Earnings in {days_until}d"
            reasons.insert(0, f"Earnings in {days_until} days — avoid new entries")

    return verdict, zone, reasons


# ═══════════════════════════════════════════════
# AI DROP ANALYSIS — smarter prompt
# ═══════════════════════════════════════════════

def ai_analyze_drop(ctx, market_ctx=None):
    if not GEMINI_API_KEY:
        return None

    c = ctx
    mkt_str = ""
    if market_ctx:
        spy_pct = market_ctx.get('SPY', {}).get('pct', 0)
        qqq_pct = market_ctx.get('QQQ', {}).get('pct', 0)
        vix     = market_ctx.get('^VIX', {}).get('price', 15)
        mkt_str = f"\nMarket: SPY {spy_pct:+.2f}%, QQQ {qqq_pct:+.2f}%, VIX {vix:.1f}"

    prompt = f"""You are a senior market analyst. Analyze this move in EXACTLY 4 lines (max 110 chars each).

{c['symbol']} — Today: {c['day_change_pct']:+.2f}% | Price: ${c['current']:.2f} | Volume: {c['vol_ratio']:.1f}× avg
52W: Low ${c['low_52w']:.2f} / High ${c['high_52w']:.2f} / ATH ${c['ath']:.2f} ({c['ath_pct']:+.1f}% from ATH)
Trend: {c['trend']} | RSI: {c['rsi']:.0f} | Position in 52W range: {c['range_pos']:.0f}%
EMA50: ${c['ema50']:.2f} | EMA200: ${c['ema200']:.2f}{mkt_str}

Respond EXACTLY:
📊 [Is this technical, sector-driven, or likely news/catalyst? Be specific]
🎯 [Setup quality — healthy pullback, correction, extended, or bleed?]
⚠️ [Biggest risk — specific price level or condition that invalidates]
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
                text = data['candidates'][0]['content']['parts'][0]['text'].strip()
                print(f"  → Gemini got {len(text)} chars")
                return text
            else:
                print(f"  → Gemini no candidates: {data}")
                return None
        elif r.status_code == 429:
            print("  → Gemini RATE LIMITED — retrying in 15s")
            logging.warning(f"Gemini rate-limited for {c['symbol']}")
            time.sleep(15)
            try:
                r2 = requests.post(url, json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.5, "maxOutputTokens": 400}
                }, timeout=20)
                if r2.status_code == 200:
                    data2 = r2.json()
                    if data2.get('candidates'):
                        text = data2['candidates'][0]['content']['parts'][0]['text'].strip()
                        print(f"  → Gemini retry succeeded ({len(text)} chars)")
                        return text
                print(f"  → Gemini retry failed: {r2.status_code}")
            except Exception as e2:
                print(f"  → Gemini retry error: {e2}")
        else:
            print(f"  → Gemini ERROR {r.status_code}: {r.text[:300]}")
            logging.error(f"Gemini {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"AI drop analysis {c['symbol']}: {e}")
    return None


# ═══════════════════════════════════════════════
# ALERT FORMATTER v2.2
# ═══════════════════════════════════════════════

def format_big_move_alert(ctx, verdict, zone, reasons, ai_text, market_ctx):
    c   = ctx
    em  = SYMBOL_EMOJI.get(c['symbol'], '📊')
    drop = c['day_change_pct']

    if drop <= BIG_DROP_CRITICAL:
        header_emoji, severity = "🚨🩸", "CRITICAL DROP"
    elif drop <= BIG_DROP_WARN:
        header_emoji, severity = "⚠️📉", "BIG DROP"
    elif drop >= BIG_GAIN_ALERT:
        header_emoji, severity = "🚀📈", "BIG GAIN"
    else:
        return None

    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%a %b %d • %I:%M %p {tz}')

    msg  = f"{header_emoji} *{severity}* — {em} *{c['symbol']}*\n"
    msg += f"🕒 {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"

    sign    = "+" if drop >= 0 else ""
    drop_em = "🔴" if drop < 0 else "🟢"
    msg += f"💵 *Price:* `${c['current']:.2f}` ({drop_em} {sign}{drop:.2f}% today)\n"
    msg += f"📊 *Range:* L `${c['today_low']:.2f}` → H `${c['today_high']:.2f}`\n"

    # Volume with interpretation
    vol_str = f"{c['vol_ratio']:.1f}× average"
    if c['vol_ratio'] >= 2.0:   vol_str += " 🔥 Unusually high"
    elif c['vol_ratio'] >= 1.5: vol_str += " ⬆️ Above average"
    elif c['vol_ratio'] < 0.8:  vol_str += " ⬇️ Below average"
    msg += f"📈 *Volume:* {vol_str}\n"

    # Low volume warning on big gains
    if drop >= BIG_GAIN_ALERT and c['vol_ratio'] < 1.3:
        msg += f"⚠️ _Low volume on big gain — thin/news-driven, less reliable_\n"

    # ── VERDICT ──
    msg += f"\n*🎯 VERDICT: {verdict}*\n"
    msg += f"_Zone: {zone}_\n"
    for r in reasons[:3]:
        msg += f"  • {r}\n"

    # AI summary line early
    if ai_text:
        lines   = ai_text.strip().split('\n')
        summary = next((l for l in lines if '💡' in l), None)
        if summary:
            msg += f"\n{summary}\n"

    # ── POSITIONAL CONTEXT ──
    msg += f"\n*📏 POSITIONAL CONTEXT*\n`─────────────────`\n"

    ath_pct = c['ath_pct']
    if ath_pct > -5:     ath_tag = "🏔️ AT/NEAR ATH"
    elif ath_pct > -15:  ath_tag = "📍 Near ATH"
    elif ath_pct > -30:  ath_tag = "📉 Pullback from ATH"
    elif ath_pct > -50:  ath_tag = "💀 Deep drawdown"
    else:                ath_tag = "⚰️ Far from ATH"

    ath_when = ath_recency_label(c['ath_date'])
    msg += f"🏔️ *ATH:* `${c['ath']:.2f}` ({c['ath_pct']:+.1f}%) {ath_tag} — {ath_when}\n"

    pos = int(c['range_pos'] / 10)
    bar = "█" * pos + "░" * (10 - pos)
    msg += f"📊 *52W Range:* `${c['low_52w']:.2f}` → `${c['high_52w']:.2f}`\n"
    msg += f"   `{bar}` {c['range_pos']:.0f}% of range\n"
    msg += f"   From low: {c['pct_from_52w_low']:+.1f}% • From high: {c['pct_from_52w_high']:+.1f}%\n"

    # Flag abnormal 52W range (spin-offs, corporate actions)
    if c['pct_from_52w_low'] > 1000:
        msg += f"   ⚠️ _Extreme range — likely corporate action or spin-off_\n"
        msg += f"   _52W context may not be meaningful_\n"

    # ── TREND & TECHNICALS ──
    msg += f"\n*📈 TREND & TECHNICALS*\n`─────────────────`\n"
    msg += f"Trend: {c['trend']}\n"

    if c['rsi'] < 30:   rsi_tag = " _(oversold)_"
    elif c['rsi'] > 70: rsi_tag = " _(overbought)_"
    else:               rsi_tag = " _(neutral)_"
    msg += f"RSI (Daily): `{c['rsi']:.0f}`{rsi_tag}\n"
    msg += f"EMA50: `${c['ema50']:.2f}` • EMA200: `${c['ema200']:.2f}`\n"

    above_50  = c['current'] > c['ema50']
    above_200 = c['current'] > c['ema200']
    if above_50 and above_200:
        ma_status = "✅ Above EMA50 & EMA200 (bullish structure)"
    elif above_200 and not above_50:
        ma_status = "⚠️ Below EMA50, above EMA200 (pullback)"
    elif not above_200 and above_50:
        ma_status = "🔀 Above EMA50, below EMA200 (recovery)"
    else:
        ma_status = "🔴 Below EMA50 & EMA200 (bearish)"
    msg += f"{ma_status}\n"

    # ── EARNINGS ──
    earnings_date, days_until = get_earnings_date(c['symbol'])
    earn_warning = format_earnings_warning(c['symbol'], earnings_date, days_until)
    if earn_warning:
        msg += f"\n*📅 EARNINGS*\n`─────────────────`\n{earn_warning}\n"

    # ── RELATIVE STRENGTH ──
    rs_score, rs_label = calc_relative_strength(c)
    if rs_score is not None:
        sign_rs = "+" if rs_score >= 0 else ""
        msg += f"\n*💪 RELATIVE STRENGTH (5d vs SPY)*\n`─────────────────`\n"
        msg += f"{rs_label}: `{sign_rs}{rs_score}%` vs SPY\n"

    # ── MARKET ──
    if market_ctx:
        spy = market_ctx.get('SPY', {}).get('pct', 0)
        vix = market_ctx.get('^VIX', {}).get('price', 15)
        msg += f"\n*🌍 MARKET*\n`─────────────────`\n"
        spy_em = "🔴" if spy < 0 else "🟢"
        vix_em = "🔴" if vix > 25 else "🟡" if vix > 18 else "🟢"
        msg += f"SPY: {spy_em} `{spy:+.2f}%` • VIX: {vix_em} `{vix:.1f}`\n"
        if vix > 22:
            msg += f"⚠️ _Elevated VIX — broad risk-off_\n"
        elif spy < -1 and drop < -5:
            msg += f"⚠️ _Moving with market bleed_\n"
        elif spy > 0 and drop < -5:
            msg += f"🚨 _Stock-specific weakness — market is UP_\n"

    # ── FULL AI ──
    if ai_text:
        msg += f"\n*🤖 AI ANALYSIS*\n`─────────────────`\n{ai_text}\n"

    # ── ENTRY GUIDANCE — verdict-specific ──
    msg += f"\n*💡 ENTRY GUIDANCE*\n`─────────────────`\n"
    if "BUY" in verdict:
        support1 = min(c['ema50'], c['low_52w'] * 1.03)
        msg += f"🟢 *Buy Zone:* `${support1:.2f}` – `${c['current']:.2f}`\n"
        msg += f"🛡️ *Support:* `${c['ema200']:.2f}` (EMA200)\n"
        msg += f"🚪 *Invalidation:* Below `${c['ema200']:.2f}`\n"
    elif "MOMENTUM" in verdict:
        msg += f"🚀 *Breakout entry:* Above ATH `${c['ath']:.2f}` with volume\n"
        msg += f"🔄 *Pullback entry:* Dip to EMA50 `${c['ema50']:.2f}`\n"
        msg += f"🛡️ *Stop:* Below EMA50 `${c['ema50']:.2f}`\n"
    elif "TAKE PROFITS" in verdict or "EXTENDED" in verdict:
        msg += f"🟠 *If holding:* Consider trimming 25-33% here\n"
        msg += f"🔄 *Re-entry zone:* Pullback to EMA50 `${c['ema50']:.2f}`\n"
        msg += f"🛡️ *Trail stop:* `${c['ema50'] * 0.97:.2f}` (3% below EMA50)\n"
        msg += f"🚫 *Don't add* to position at these levels\n"
    elif "PARABOLIC" in verdict:
        msg += f"🚫 *DO NOT chase* at current levels\n"
        msg += f"⏳ *Wait for:* 3-5 day consolidation\n"
        msg += f"🔄 *Re-entry:* First pullback to EMA50 `${c['ema50']:.2f}`\n"
    elif "CRASH" in verdict:
        msg += f"🚫 *Do NOT catch this today*\n"
        msg += f"⏳ *Wait minimum* 3 days for stabilisation\n"
        msg += f"👀 *Watch:* Does it hold EMA200 `${c['ema200']:.2f}`?\n"
    elif "AVOID" in verdict or "WAIT" in verdict:
        msg += f"🚫 *Don't enter now*\n"
        msg += f"⏳ *Wait for:* Base above `${c['ema200']:.2f}`\n"
        msg += f"👀 *Trigger:* RSI reversal + reclaim EMA50 `${c['ema50']:.2f}`\n"
    elif "CAUTION" in verdict or "WATCH" in verdict:
        msg += f"👀 *Watch key level:* `${c['ema50']:.2f}` (EMA50)\n"
        msg += f"🟡 *Scale-in zone:* `${c['ema200']:.2f}` if holds\n"
    else:
        msg += f"⏸️ *No clear edge* — wait for directional setup\n"
        msg += f"👀 *Watch:* EMA50 `${c['ema50']:.2f}` for direction\n"

    return msg


# ═══════════════════════════════════════════════
# SECTOR BLEED DETECTOR
# ═══════════════════════════════════════════════

def check_sector_bleeds(all_contexts):
    sector_moves = {}
    for sector, symbols in SECTORS.items():
        moves = [(s, all_contexts[s]['day_change_pct'])
                 for s in symbols if s in all_contexts and all_contexts[s]]
        if len(moves) >= 2:
            avg      = sum(m[1] for m in moves) / len(moves)
            bleeding = [m for m in moves if m[1] < -2]
            if avg < -2 and len(bleeding) >= max(2, len(moves) // 2):
                sector_moves[sector] = {'avg': avg, 'bleeding': bleeding, 'all': moves}
    return sector_moves

def format_sector_bleed_alert(sector_moves):
    if not sector_moves:
        return None
    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%I:%M %p {tz}')
    msg = f"🩸 *SECTOR BLEED DETECTED*\n🕒 {ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    for sector, data in sorted(sector_moves.items(), key=lambda x: x[1]['avg']):
        msg += f"\n🔻 *{sector}* (avg {data['avg']:+.2f}%)\n"
        for sym, pct in sorted(data['all'], key=lambda x: x[1]):
            em = SYMBOL_EMOJI.get(sym, '📊')
            if pct < -5:    pct_em = "🔴"
            elif pct < -2:  pct_em = "🟠"
            elif pct < 0:   pct_em = "🟡"
            else:           pct_em = "🟢"
            msg += f"  {em} {sym}: {pct_em} `{pct:+.2f}%`\n"
    msg += f"\n💡 _Avoid longs in bleeding sectors. Wait for stabilization._"
    return msg


# ═══════════════════════════════════════════════
# LEADERSHIP / LAGGARD DETECTOR
# ═══════════════════════════════════════════════

def check_leadership(all_contexts, sector_full):
    leaders  = []
    laggards = []
    for sector, data in sector_full.items():
        sector_avg = data['avg']
        if abs(sector_avg) < 1.5:
            continue
        for sym, pct in data['all']:
            if SYMBOL_TO_SECTOR.get(sym) != sector:
                continue
            ctx = all_contexts.get(sym)
            if not ctx:
                continue
            divergence = pct - sector_avg
            if sector_avg < -2 and divergence > 2:
                leaders.append({'symbol': sym, 'ctx': ctx, 'sector': sector,
                                 'sector_avg': sector_avg, 'divergence': divergence})
            elif sector_avg > 2 and divergence < -2:
                laggards.append({'symbol': sym, 'ctx': ctx, 'sector': sector,
                                  'sector_avg': sector_avg, 'divergence': divergence})
    return leaders, laggards

def format_leadership_alert(leaders, laggards):
    if not leaders and not laggards:
        return None
    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f'%I:%M %p {tz}')
    msg = f"💪 *RELATIVE STRENGTH SIGNALS*\n🕒 {ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    if leaders:
        msg += f"\n🏆 *LEADERS* — holding up while sector bleeds\n`─────────────────`\n"
        for l in sorted(leaders, key=lambda x: -x['divergence']):
            em = SYMBOL_EMOJI.get(l['symbol'], '📊')
            msg += f"  {em} *{l['symbol']}* ({l['sector']})\n"
            msg += f"     Stock: `{l['ctx']['day_change_pct']:+.2f}%` • Sector avg: `{l['sector_avg']:+.2f}%`\n"
            msg += f"     💪 Outperforming by *{l['divergence']:+.2f}%*\n"
        msg += f"\n💡 _Leaders during weakness = future winners. Watch for entry._\n"
    if laggards:
        msg += f"\n🔻 *LAGGARDS* — weak vs strong sector\n`─────────────────`\n"
        for l in sorted(laggards, key=lambda x: x['divergence']):
            em = SYMBOL_EMOJI.get(l['symbol'], '📊')
            msg += f"  {em} *{l['symbol']}* ({l['sector']})\n"
            msg += f"     Stock: `{l['ctx']['day_change_pct']:+.2f}%` • Sector avg: `{l['sector_avg']:+.2f}%`\n"
            msg += f"     📉 Underperforming by *{l['divergence']:+.2f}%*\n"
        msg += f"\n⚠️ _Laggards in strong sectors = relative weakness._\n"
    return msg


# ═══════════════════════════════════════════════
# COOLDOWN MANAGER
# ═══════════════════════════════════════════════

def can_alert(key, hours=COOLDOWN_HOURS):
    state = load_json(STATE_FILE, {})
    last  = state.get(key)
    if last:
        try:
            dt = datetime.fromisoformat(last)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            if now_est() - dt < timedelta(hours=hours):
                return False
        except Exception:
            pass
    state[key] = now_est().isoformat()
    save_json(STATE_FILE, state)
    return True


# ═══════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════

def send_telegram(message, silent=False):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram credentials missing")
        return False

    if len(message) > 4000:
        parts   = []
        current = ""
        for line in message.split('\n'):
            if len(current) + len(line) + 1 > 3900:
                parts.append(current)
                current = line + '\n'
            else:
                current += line + '\n'
        if current:
            parts.append(current)
        success = True
        for part in parts:
            if not _send_single(part, silent):
                success = False
            time.sleep(0.3)
        return success
    return _send_single(message, silent)

def _send_single(message, silent=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id':              CHAT_ID,
            'text':                 message,
            'parse_mode':           'Markdown',
            'disable_notification': silent
        }, timeout=10)
        if r.status_code != 200:
            logging.error(f"Telegram {r.status_code}: {r.text[:200]}")
            if "can't parse" in r.text.lower() or 'parse' in r.text.lower():
                logging.warning("Retrying without parse_mode")
                r = requests.post(url, json={
                    'chat_id': CHAT_ID, 'text': message,
                    'disable_notification': silent,
                }, timeout=10)
                return r.status_code == 200
        return r.status_code == 200
    except Exception as e:
        logging.error(f"Telegram send: {e}")
        return False


# ═══════════════════════════════════════════════
# MAIN ORCHESTRATION
# ═══════════════════════════════════════════════

def run_intel_scan():
    print(f"\n🧠 Market Intelligence Scan @ {now_est().strftime('%H:%M %Z')}")
    logging.info("Intel scan start")

    market_ctx   = get_market_ctx()
    all_contexts = {}
    alerts_fired = 0

    for symbol in MONITOR_LIST:
        try:
            print(f"  → {symbol:10s}...", end=" ", flush=True)
            ctx = get_full_context(symbol)
            time.sleep(FETCH_DELAY)
            if not ctx:
                print("—")
                continue
            all_contexts[symbol] = ctx

            drop     = ctx['day_change_pct']
            big_move = (drop <= BIG_DROP_WARN or drop >= BIG_GAIN_ALERT)

            if big_move:
                cool_key = f"intel_bigmove_{symbol}"
                if not can_alert(cool_key, COOLDOWN_HOURS):
                    print(f"{drop:+.2f}% 🔕 cooldown")
                    continue

                verdict, zone, reasons = get_verdict(ctx, market_ctx)
                ai  = ai_analyze_drop(ctx, market_ctx) if abs(drop) >= 5 else None
                msg = format_big_move_alert(ctx, verdict, zone, reasons, ai, market_ctx)

                if msg:
                    send_telegram(msg, silent=False)
                    alerts_fired += 1
                    print(f"{drop:+.2f}% 🚨 ALERT SENT")
                else:
                    print(f"{drop:+.2f}%")
            else:
                print(f"{drop:+.2f}%")

        except Exception as e:
            print(f"💥 {e}")
            logging.error(f"Intel {symbol}: {e}")

    if not all_contexts:
        print("\n⚠️ No contexts fetched — skipping sector/leadership checks")
        logging.warning("No contexts — skipping aggregate detectors")
        return

    # Sector bleed
    sector_moves = check_sector_bleeds(all_contexts)
    if sector_moves:
        if can_alert('last_sector_bleed', SECTOR_BLEED_COOLDOWN):
            sector_msg = format_sector_bleed_alert(sector_moves)
            if sector_msg:
                send_telegram(sector_msg, silent=False)
                alerts_fired += 1
                print("🩸 Sector bleed alert sent")
        else:
            print("🩸 Sector bleed — 🔕 cooldown")

    # Leadership / laggard
    sector_full = {}
    for sector, symbols in SECTORS.items():
        moves = [(s, all_contexts[s]['day_change_pct'])
                 for s in symbols if s in all_contexts]
        if len(moves) >= 2:
            avg = sum(m[1] for m in moves) / len(moves)
            sector_full[sector] = {'avg': avg, 'all': moves}

    leaders, laggards = check_leadership(all_contexts, sector_full)
    if leaders or laggards:
        if can_alert('last_leadership_alert', LEADERSHIP_COOLDOWN):
            rs_msg = format_leadership_alert(leaders, laggards)
            if rs_msg:
                send_telegram(rs_msg, silent=True)
                alerts_fired += 1
                print("💪 Leadership alert sent")
        else:
            print("💪 Leadership — 🔕 cooldown")

    print(f"\n✅ Intel scan done — {alerts_fired} alert(s) fired")
    logging.info(f"Intel scan | Alerts: {alerts_fired}")


if __name__ == "__main__":
    run_intel_scan()
