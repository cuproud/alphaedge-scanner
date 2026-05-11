"""
═══════════════════════════════════════════════════════════════════════════════
  ALPHAEDGE SCANNER v7.0 — PINE PARITY + ADVANCED INTELLIGENCE
═══════════════════════════════════════════════════════════════════════════════

A self-contained signal scanner that mirrors TradingView Pine Script v6.3.2
exactly (Wilder's RMA, volume-driven Range Filter, ratcheting Supertrend, etc.)
with an advanced intelligence layer.

────────────────────────────────────────────────────────────────────────────────
WHAT'S NEW IN v7.0 vs v6.1:
────────────────────────────────────────────────────────────────────────────────
BUG FIXES:
  ✅ Signal bar = last CLOSED bar (iloc[-2]), not forming bar → no repaint
  ✅ rsi_bull / rsi_bear mutually exclusive (was double-counting)
  ✅ Flip detection looks at last 2 bars (was missing flips 10m after)
  ✅ Cache auto-cleans entries older than 48h (no unbounded growth)
  ✅ Markdown-safe escaping for symbols/prices (no broken alerts)
  ✅ Crossover uses both sides correctly (cleaner logic)
  ✅ SQS denominator corrected to /9 (was /10)
  ✅ Single data fetch per symbol per TF (was fetching 3×)
  ✅ get_htf_bias uses iloc[-2] consistently
  ✅ Log file rotation per-day (no mega-file)

NEW FEATURES:
  🆕 External symbols.yaml — add stocks without touching code
  🆕 SQS quality trending ("NVDA: 68 → 74 → 82 improving")
  🆕 VIX regime filter (blocks longs when VIX > 30 & spiking)
  🆕 Volume Profile / POC context ("Price above POC — strong hands")
  🆕 Dynamic SQS threshold (tightens if B-grade win rate drops)
  🆕 Plain-English R:R ("Risk $2.00 → Make $6.00 — 3× reward")
  🆕 Urgency emoji prefixes (🚨🔥 for elite, ⭐ for solid, etc.)

────────────────────────────────────────────────────────────────────────────────
FILE STRUCTURE (one mega-script):
────────────────────────────────────────────────────────────────────────────────
  §1  Imports & env
  §2  Config block (tune everything here)
  §3  Universe loader (reads symbols.yaml)
  §4  Logging
  §5  Session & time helpers
  §6  State/JSON helpers
  §7  Formatting (Markdown, R:R, urgency)
  §8  Pine indicators
  §9  Volume Profile / POC
  §10 VIX regime filter
  §11 SQS trending
  §12 Dynamic threshold
  §13 Data fetchers (live, HTF, MTF)
  §14 Signal analysis engine
  §15 Trade tracking & progress
  §16 AI enrichment (Gemini)
  §17 Alert builders (new signal, events, digest, positions, weekly)
  §18 Telegram transport
  §19 Correlation detector
  §20 Main orchestration

────────────────────────────────────────────────────────────────────────────────
REQUIRED FILES:
────────────────────────────────────────────────────────────────────────────────
  symbols.yaml       — watchlist config (see README for format)
  requirements.txt   — add: pyyaml

────────────────────────────────────────────────────────────────────────────────
ENVIRONMENT VARIABLES:
────────────────────────────────────────────────────────────────────────────────
  TELEGRAM_TOKEN    (required)
  CHAT_ID           (required)
  GEMINI_API_KEY    (optional — enables AI analysis)

═══════════════════════════════════════════════════════════════════════════════
"""

# ═══════════════════════════════════════════════════════════════════════════════
# §1  IMPORTS & ENVIRONMENT
# ═══════════════════════════════════════════════════════════════════════════════
import os
import json
import time
import logging
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

try:
    import yaml
except ImportError:
    raise ImportError("pyyaml required — run: pip install pyyaml")

warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=UserWarning)


# ═══════════════════════════════════════════════════════════════════════════════
# §2  CONFIG BLOCK — ALL TUNABLES IN ONE PLACE
# ═══════════════════════════════════════════════════════════════════════════════
EST = ZoneInfo("America/New_York")

# ── Credentials ──
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# ── Pine indicator params ──
AE_LENGTH = 200
ADX_LEN = 14
ADX_GATE_LEVEL = 20
ADX_STRONG = 25
ADX_BYPASS_MIN = 5           # conf ≥ this bypasses ADX gate
RSI_LEN = 14
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
ATR_LEN = 14
ST_PERIODS = 10
ST_MULT = 3.0
SQ_BB_MULT = 2.0
SQ_KC_MULT = 1.5

# ── Stop loss / take profit ──
SL_MULT = 2.0                # ATR multiple for initial SL distance
SWING_LOOKBACK = 10          # bars back for structure SL
STRUCT_BUFFER = 0.2          # ATR buffer around swing
MIN_SL_DIST = 0.5            # minimum ATR distance for SL
TP1_MULT, TP2_MULT, TP3_MULT = 1.0, 2.0, 3.0

# ── Safety caps (as % of entry price) ──
MAX_SL_PCT_STOCKS = 0.04
MAX_SL_PCT_CRYPTO = 0.08
MIN_SL_PCT_STOCKS = 0.005
MIN_SL_PCT_CRYPTO = 0.01
PRICE_SANITY_DEVIATION = 0.20  # reject if live vs daily > 20% off

# ── Signal gates ──
MIN_CONF_SCORE = 4
GRADE_FILTER = "A+ and A"    # "A+ Only" | "A+ and A" | "B and better" | "All"
USE_COUNTER_TREND_BLOCK = True
USE_MTF_GATE = True
MTF_GATE_BULL = 9
MTF_GATE_BEAR = 3
USE_CHOP_FILTER = True
CHOP_ATR_MULT = 1.0
USE_RSI_DIV = True
RSI_DIV_LOOK = 5
RSI_DIV_FLOOR = 25
RSI_BEAR_CEIL = 75

# ── SQS (composite quality score) ──
USE_SQS = True
SQS_BASE_THRESHOLD = 75      # minimum SQS to alert (may be adjusted dynamically)
SQS_DYNAMIC_ENABLED = True
SQS_DYNAMIC_MIN = 70
SQS_DYNAMIC_MAX = 85
AI_TIER_THRESHOLD = 75       # SQS ≥ this triggers AI analysis

# ── Regime classification ──
REGIME_ADX_TREND = 22
REGIME_ADX_RANGE = 20

# ── Alert management ──
DIGEST_THRESHOLD = 4         # collapse to digest if ≥N signals fire at once
MAX_TRADE_AGE_HOURS = 72     # auto-close trades after this long
COOLDOWN_ELITE = 2           # hours between duplicate alerts (SQS ≥ 85)
COOLDOWN_STRONG = 4          # SQS ≥ 70
COOLDOWN_GOOD = 6            # SQS ≥ 55
COOLDOWN_FAIR = 10           # below
POSITION_SUMMARY_HOURS = 2   # min hours between open-positions summaries
CACHE_MAX_AGE_HOURS = 48     # cleanup old cache entries

# ── VIX regime filter ──
VIX_BLOCK_LONGS_ENABLED = True
VIX_EXTREME_LEVEL = 35       # always block longs
VIX_SPIKE_LEVEL = 25         # block if + spiking
VIX_SPIKE_PCT = 15           # % above 5d avg = "spiking"

# ── Performance ──
FETCH_DELAY = 0.3            # sleep between yf calls (rate-limit friendly)
DEBUG_NEAR_MISS = True       # log reasons signals were rejected

# ── Timeframes to scan ──
TIMEFRAMES = [
    {'tf': '30m', 'lookback': '60d', 'label': '⚡30m', 'min_bars': 250},
    {'tf': '1h',  'lookback': '3mo', 'label': '📊1h',  'min_bars': 250},
]
MTF_FRAMES = ['15m', '1h', '4h', '1d']

# ── File paths ──
ALERT_CACHE = 'alert_cache.json'
TRADES_FILE = 'active_trades.json'
HISTORY_FILE = 'trade_history.json'
STATE_FILE = 'scanner_state.json'
SQS_HISTORY_FILE = 'sqs_history.json'
DYNAMIC_THRESHOLD_FILE = 'dynamic_threshold.json'
SYMBOLS_YAML = 'symbols.yaml'
LOGS_DIR = Path('logs')
LOGS_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# §3  UNIVERSE LOADER — reads symbols.yaml (single source of truth)
# ═══════════════════════════════════════════════════════════════════════════════

class Universe:
    """Loads and exposes symbol/sector config from symbols.yaml."""

    def __init__(self, path=SYMBOLS_YAML):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"{path} not found. Create it (see README) before running."
            )
        with open(self.path, 'r', encoding='utf-8') as f:
            self._raw = yaml.safe_load(f) or {}

    def _syms(self, bucket):
        return [x['symbol'] for x in (self._raw.get(bucket) or [])]

    @property
    def crypto(self):         return self._syms('crypto')
    @property
    def extended_hours(self): return self._syms('extended_hours')
    @property
    def regular_hours(self):  return self._syms('regular_hours')
    @property
    def dip_extras(self):     return self._syms('dip_extras')
    @property
    def all_symbols(self):    return self.crypto + self.extended_hours + self.regular_hours

    @property
    def emoji_map(self):
        out = {}
        for bucket in ('crypto', 'extended_hours', 'regular_hours', 'dip_extras'):
            for item in (self._raw.get(bucket) or []):
                out[item['symbol']] = item.get('emoji', '📈')
        return out

    @property
    def sector_map(self):
        out = {}
        for bucket in ('crypto', 'extended_hours', 'regular_hours', 'dip_extras'):
            for item in (self._raw.get(bucket) or []):
                out[item['symbol']] = item.get('sector', 'Other')
        return out

    @property
    def correlation_groups(self):
        """{sector: [symbols]} — only sectors with 2+ core symbols."""
        core = set(self.all_symbols)
        groups = {}
        for sym, sector in self.sector_map.items():
            if sym in core:
                groups.setdefault(sector, []).append(sym)
        return {k: v for k, v in groups.items() if len(v) >= 2}

    def setting(self, module, key, default=None):
        return (self._raw.get('settings') or {}).get(module, {}).get(key, default)

    def summary(self):
        return (f"{len(self.all_symbols)} core symbols "
                f"({len(self.crypto)} crypto, {len(self.extended_hours)} ext-hrs, "
                f"{len(self.regular_hours)} reg-hrs)")


U = Universe()
CRYPTO_WATCHLIST = U.crypto
EXTENDED_HOURS_STOCKS = U.extended_hours
REGULAR_HOURS_ONLY = U.regular_hours
ALL_SYMBOLS = U.all_symbols
SYMBOL_EMOJI = U.emoji_map
CORRELATION_GROUPS = U.correlation_groups

# Allow YAML to override config defaults
SQS_BASE_THRESHOLD = U.setting('scanner', 'sqs_min_for_alert', SQS_BASE_THRESHOLD)
GRADE_FILTER = U.setting('scanner', 'grade_filter', GRADE_FILTER)


# ═══════════════════════════════════════════════════════════════════════════════
# §4  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_logger():
    logfile = LOGS_DIR / f'scan_{datetime.now(EST).strftime("%Y-%m-%d")}.log'
    # Clear existing handlers to avoid duplicate logs when module is re-imported
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    logging.basicConfig(
        filename=logfile,
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-7s | %(message)s'
    )

_setup_logger()


# ═══════════════════════════════════════════════════════════════════════════════
# §5  SESSION & TIME HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def now_est():
    return datetime.now(EST)

def fmt_time():
    return now_est().strftime('%H:%M %Z')

def fmt_datetime():
    return now_est().strftime('%Y-%m-%d %H:%M %Z')

def get_session():
    """Returns human-readable session label."""
    n = now_est()
    wk = n.weekday()
    if wk >= 5:
        return "🌐 Weekend"
    t = n.hour + n.minute / 60
    if 4 <= t < 9.5:    return "🌅 Pre-Market"
    if 9.5 <= t < 10.5: return "🔔 Market Open"
    if 10.5 <= t < 14:  return "📊 Midday"
    if 14 <= t < 16:    return "⚡ Power Hour"
    if 16 <= t < 20:    return "🌙 After-Hours"
    return "🌑 Overnight"

def is_crypto(sym):
    return sym.endswith('-USD') or sym == 'GC=F'

def is_extended_hours_session():
    return get_session() in ('🌅 Pre-Market', '🌙 After-Hours')

def is_regular_market_open():
    return get_session() in ('🔔 Market Open', '📊 Midday', '⚡ Power Hour')

def get_active_watchlist():
    """Return only symbols tradable in current session."""
    s = get_session()
    if s in ("🌐 Weekend", "🌑 Overnight"):
        return CRYPTO_WATCHLIST
    if s in ("🌅 Pre-Market", "🌙 After-Hours"):
        return CRYPTO_WATCHLIST + EXTENDED_HOURS_STOCKS
    if is_regular_market_open():
        return ALL_SYMBOLS
    return CRYPTO_WATCHLIST

def time_ago(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        delta = now_est() - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60: return f"{mins}m"
        h, m = divmod(mins, 60)
        if h < 24: return f"{h}h {m}m"
        d, hr = divmod(h, 24)
        return f"{d}d {hr}h"
    except Exception:
        return "?"

def time_until(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        delta = dt - now_est()
        mins = int(delta.total_seconds() / 60)
        if mins <= 0: return "expired"
        if mins < 60: return f"{mins}m"
        h, m = divmod(mins, 60)
        return f"{h}h {m}m"
    except Exception:
        return "?"

def absolute_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        return dt.strftime('%H:%M %Z')
    except Exception:
        return "?"


# ═══════════════════════════════════════════════════════════════════════════════
# §6  JSON STATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def load_json(path, default):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logging.error(f"save_json {path}: {e}")


def load_cache():
    """Load alert cache and cleanup entries older than CACHE_MAX_AGE_HOURS."""
    cache = load_json(ALERT_CACHE, {})
    cleaned = {}
    cutoff = now_est() - timedelta(hours=CACHE_MAX_AGE_HOURS)
    for k, v in cache.items():
        try:
            if k.endswith('_info'):
                # info entries store JSON with a 'ts' field
                info = v if isinstance(v, dict) else json.loads(v)
                ts = datetime.fromisoformat(info['ts'])
            else:
                ts = datetime.fromisoformat(v) if isinstance(v, str) else v
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=EST)
            if ts >= cutoff:
                cleaned[k] = v
        except Exception:
            continue
    return cleaned

def get_cooldown_hours(sqs):
    if sqs >= 85: return COOLDOWN_ELITE
    if sqs >= 70: return COOLDOWN_STRONG
    if sqs >= 55: return COOLDOWN_GOOD
    return COOLDOWN_FAIR

def is_duplicate(sym, sig_key, cache, sqs=60):
    k = f"{sym}_{sig_key}"
    if k not in cache:
        return False
    try:
        last = datetime.fromisoformat(cache[k])
        if last.tzinfo is None:
            last = last.replace(tzinfo=EST)
        return now_est() - last < timedelta(hours=get_cooldown_hours(sqs))
    except Exception:
        return False

def mark_sent(sym, sig_key, cache):
    cache[f"{sym}_{sig_key}"] = now_est().isoformat()

def get_last_signal_info(cache, symbol, tf):
    """Returns last {price, atr, ts} within 24h for chop filter."""
    for direction in ('BUY', 'SELL'):
        key = f"{symbol}_{direction}_{tf}_info"
        if key not in cache:
            continue
        try:
            info = cache[key]
            if isinstance(info, str):
                info = json.loads(info)
            ts = datetime.fromisoformat(info['ts'])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=EST)
            if now_est() - ts < timedelta(hours=24):
                return info
        except Exception:
            continue
    return None

def save_signal_info(cache, symbol, tf, direction, price, atr):
    cache[f"{symbol}_{direction}_{tf}_info"] = {
        'price': price, 'atr': atr, 'ts': now_est().isoformat()
    }

def can_alert_key(key, hours):
    """Generic cooldown check + set in state file."""
    state = load_json(STATE_FILE, {})
    last = state.get(key)
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


# ═══════════════════════════════════════════════════════════════════════════════
# §7  FORMATTING — Markdown-safe, plain-English R:R, urgency emojis
# ═══════════════════════════════════════════════════════════════════════════════

def md_escape(text):
    """Escape Telegram Markdown (legacy) special chars in user data."""
    if text is None:
        return ""
    s = str(text)
    for ch in ('\\', '_', '*', '`', '[', ']'):
        s = s.replace(ch, '\\' + ch)
    return s

def safe_sym(sym):
    """Symbols like BRK.B or GC=F — just escape underscores."""
    return str(sym).replace('_', r'\_')

def fmt_price(val, decimals=2):
    try:
        return f"{float(val):.{decimals}f}"
    except Exception:
        return str(val)

def fmt_r(r, plain=False):
    """
    R multiple formatter.
      plain=True  → "+2.5× your risk"
      plain=False → "+2.50R"
    """
    try:
        r = float(r)
    except Exception:
        return "—"
    if abs(r) < 0.01:
        return "breakeven" if plain else "0.00R"
    sign = "+" if r > 0 else ""
    if plain:
        return f"{sign}{r:.2f}× your risk"
    return f"{sign}{r:.2f}R"

def fmt_risk_reward_line(risk_dollars, reward_dollars):
    """Plain-English R:R: 'Risk $2.00 → Make $6.00 (3.0× reward)'"""
    try:
        ratio = reward_dollars / risk_dollars if risk_dollars > 0 else 0
    except Exception:
        ratio = 0
    return (f"Risk `${risk_dollars:.2f}` → Make `${reward_dollars:.2f}` "
            f"(*{ratio:.1f}× reward*)")

def tp_line(tp_num, entry, target, risk):
    """Human-readable TP line."""
    profit = abs(target - entry)
    r_mult = profit / risk if risk > 0 else 0
    if r_mult < 1.1:
        desc = "matches your risk"
    elif r_mult < 2.1:
        desc = f"{r_mult:.1f}× your risk"
    else:
        desc = f"{r_mult:.1f}× your risk (big win)"
    return f"TP{tp_num}: `${fmt_price(target)}` — _+${profit:.2f} profit ({desc})_"

def urgency_prefix(sqs, strong_trend=False, vix_warn=False):
    """Stacking urgency emoji for headline."""
    if vix_warn:
        return "⚠️🌋 "
    if sqs >= 92 and strong_trend:
        return "🚨🔥🔥 "
    if sqs >= 88:
        return "🚨🔥 "
    if sqs >= 80:
        return "🚨 "
    if sqs >= 72:
        return "⭐ "
    return ""

def tier_label(sqs):
    if sqs >= 90: return "🏆 ELITE"
    if sqs >= 80: return "⭐ STRONG"
    if sqs >= 70: return "✅ GOOD"
    if sqs >= 60: return "⚠️ FAIR"
    return "🔹 LOW"

def grade_label(score):
    if score >= 8: return "A+"
    if score >= 6: return "A"
    if score >= 4: return "B"
    return "C"

def grade_passes(score):
    if GRADE_FILTER == "A+ Only":      return score >= 8
    if GRADE_FILTER == "A+ and A":     return score >= 6
    if GRADE_FILTER == "B and better": return score >= 4
    return True

def sqs_meter(sqs):
    filled = min(10, max(0, round(sqs / 10)))
    em = "🟢" if sqs >= 75 else "🟡" if sqs >= 60 else "🟠"
    return em * filled + "⚪" * (10 - filled)


# ═══════════════════════════════════════════════════════════════════════════════
# §8  PINE INDICATORS (exact parity with TradingView)
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_df(df):
    """Flatten yfinance MultiIndex columns."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def rma(series, length):
    """Wilder's RMA — identical to Pine's ta.rma()."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()

def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def sma(s, length):
    return s.rolling(length).mean()

def pine_rsi(src, length=14):
    delta = src.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def pine_atr(df, length=14):
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return rma(tr, length)

def pine_adx(df, length=14):
    high, low, close = df['High'], df['Low'], df['Close']
    up = high.diff()
    dn = -low.diff()

    plus_dm = pd.Series(
        np.where((up > dn) & (up > 0), up, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index
    )

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
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
    """Pine Supertrend with ratcheting bands — numpy-based for pandas 2.x safety."""
    hl2 = ((df['High'] + df['Low']) / 2).values
    atr_v = pine_atr(df, period).values
    close = df['Close'].values
    n = len(df)

    up = hl2 - mult * atr_v
    dn = hl2 + mult * atr_v
    up_final = up.copy()
    dn_final = dn.copy()

    for i in range(1, n):
        if close[i - 1] > up_final[i - 1]:
            up_final[i] = max(up[i], up_final[i - 1])
        if close[i - 1] < dn_final[i - 1]:
            dn_final[i] = min(dn[i], dn_final[i - 1])

    trend = np.ones(n, dtype=int)
    for i in range(1, n):
        prev = trend[i - 1]
        if prev == -1 and close[i] > dn_final[i - 1]:
            trend[i] = 1
        elif prev == 1 and close[i] < up_final[i - 1]:
            trend[i] = -1
        else:
            trend[i] = prev

    return (pd.Series(trend, index=df.index),
            pd.Series(up_final, index=df.index),
            pd.Series(dn_final, index=df.index))

def pine_vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    return (tp * df['Volume']).cumsum() / df['Volume'].cumsum().replace(0, np.nan)

def smooth_range(src, length=200, qty=3):
    """Pine's utils.smoothrng: EMA(EMA(abs diff, length), 2*length-1) * qty."""
    wper = (length * 2) - 1
    avrng = ema(src.diff().abs().fillna(0), length)
    return ema(avrng, wper) * qty

def range_filter(src, teth, rng):
    """Pine's rngfilt_va — volume-direction driven range filter."""
    n = len(src)
    rf = np.zeros(n, dtype=float)
    src_vals = src.values
    teth_vals = teth.values
    rng_vals = rng.values
    rf[0] = src_vals[0]
    for i in range(1, n):
        prev = rf[i - 1]
        if teth_vals[i] > teth_vals[i - 1]:
            candidate = src_vals[i] - rng_vals[i]
            rf[i] = prev if candidate < prev else candidate
        else:
            candidate = src_vals[i] + rng_vals[i]
            rf[i] = prev if candidate > prev else candidate
    return pd.Series(rf, index=src.index)

def trend_up_value(filt):
    """Cumulative direction counter (numpy-based)."""
    n = len(filt)
    v = filt.values
    trend = np.zeros(n, dtype=int)
    for i in range(1, n):
        prev = trend[i - 1]
        if v[i] > v[i - 1]:
            trend[i] = prev + 1 if prev >= 0 else 1
        elif v[i] < v[i - 1]:
            trend[i] = prev - 1 if prev <= 0 else -1
        else:
            trend[i] = prev
    return pd.Series(trend, index=filt.index)


# ═══════════════════════════════════════════════════════════════════════════════
# §9  VOLUME PROFILE / POC
# ═══════════════════════════════════════════════════════════════════════════════

def compute_poc(df, bins=30, lookback_bars=200):
    """
    Computes Point of Control (POC) + Value Area High/Low (70% of volume).
    Returns {'poc', 'vah', 'val'} or None.
    """
    if df is None or df.empty or len(df) < 20:
        return None

    recent = df.iloc[-lookback_bars:] if len(df) > lookback_bars else df
    low = float(recent['Low'].min())
    high = float(recent['High'].max())
    if high <= low:
        return None

    bin_edges = np.linspace(low, high, bins + 1)
    vol_at_price = np.zeros(bins)

    for idx in range(len(recent)):
        bar_low = float(recent['Low'].iloc[idx])
        bar_high = float(recent['High'].iloc[idx])
        bar_vol = float(recent['Volume'].iloc[idx])
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
    poc = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2

    # Expand value area from POC outward
    target = vol_at_price.sum() * 0.70
    lo, hi = poc_idx, poc_idx
    accum = vol_at_price[poc_idx]
    while accum < target and (lo > 0 or hi < bins - 1):
        nl = vol_at_price[lo - 1] if lo > 0 else -1
        nh = vol_at_price[hi + 1] if hi < bins - 1 else -1
        if nh >= nl:
            hi += 1; accum += nh
        else:
            lo -= 1; accum += nl

    return {
        'poc': round(poc, 4),
        'vah': round(bin_edges[hi + 1], 4),
        'val': round(bin_edges[lo], 4),
    }

def format_poc_line(current_price, poc_data):
    if not poc_data:
        return None
    poc, vah, val = poc_data['poc'], poc_data['vah'], poc_data['val']
    diff_pct = abs(current_price - poc) / poc * 100 if poc else 0
    if diff_pct < 0.3:
        return f"🎯 *AT POC* `${poc:.2f}` — volume magnet / decision zone"
    if current_price > vah:
        return f"🎯 *ABOVE Value Area* (POC `${poc:.2f}`) — strong hands, premium zone"
    if current_price < val:
        return f"🎯 *BELOW Value Area* (POC `${poc:.2f}`) — weak, discount zone"
    if current_price > poc:
        return f"🎯 *Above POC* `${poc:.2f}` — buyers in control"
    return f"🎯 *Below POC* `${poc:.2f}` — sellers in control"


# ═══════════════════════════════════════════════════════════════════════════════
# §10  VIX REGIME FILTER
# ═══════════════════════════════════════════════════════════════════════════════

_vix_cache = {'ts': None, 'data': None}

def get_vix_regime(cache_minutes=10):
    """
    Returns VIX regime dict or None.
    Result: {'vix', 'vix_5d_avg', 'spike_pct', 'regime',
             'blocks_longs', 'warning'}
    """
    if _vix_cache['ts'] and (now_est() - _vix_cache['ts']).total_seconds() < cache_minutes * 60:
        return _vix_cache['data']
    try:
        df = yf.download('^VIX', period='10d', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 5:
            return None
        df = _clean_df(df)
        vix_now = float(df['Close'].iloc[-1])
        vix_5d_avg = float(df['Close'].iloc[-5:].mean())
        spike_pct = (vix_now - vix_5d_avg) / vix_5d_avg * 100 if vix_5d_avg > 0 else 0

        if vix_now >= VIX_EXTREME_LEVEL:
            regime = 'extreme'
        elif vix_now >= VIX_SPIKE_LEVEL or (vix_now >= 22 and spike_pct > VIX_SPIKE_PCT):
            regime = 'spike'
        elif vix_now >= 20:
            regime = 'elevated'
        else:
            regime = 'calm'

        blocks = VIX_BLOCK_LONGS_ENABLED and (
            regime == 'extreme' or (regime == 'spike' and spike_pct > 20)
        )

        warning = None
        if regime == 'extreme':
            warning = f"🚨 VIX {vix_now:.1f} EXTREME — panic regime"
        elif regime == 'spike':
            warning = f"⚠️ VIX {vix_now:.1f} spiking (+{spike_pct:.0f}% vs 5d avg)"
        elif regime == 'elevated':
            warning = f"🟡 VIX {vix_now:.1f} elevated"

        result = {
            'vix': round(vix_now, 2),
            'vix_5d_avg': round(vix_5d_avg, 2),
            'spike_pct': round(spike_pct, 1),
            'regime': regime,
            'blocks_longs': blocks,
            'warning': warning,
        }
        _vix_cache['ts'] = now_est()
        _vix_cache['data'] = result
        return result
    except Exception as e:
        logging.error(f"VIX regime: {e}")
        return None

def vix_blocks(signal_direction):
    """Returns (bool blocked, str reason)."""
    vix = get_vix_regime()
    if not vix:
        return False, None
    if signal_direction == 'BUY' and vix['blocks_longs']:
        return True, f"VIX {vix['vix']} {vix['regime']} — longs blocked"
    return False, None


# ═══════════════════════════════════════════════════════════════════════════════
# §11  SQS QUALITY TRENDING
# ═══════════════════════════════════════════════════════════════════════════════

def record_sqs(symbol, sqs, keep_last=10):
    """Record SQS for a symbol. Keep last N entries."""
    history = load_json(SQS_HISTORY_FILE, {})
    entries = history.get(symbol, [])
    entries.append({'sqs': int(sqs), 'ts': now_est().isoformat()})
    entries = sorted(entries, key=lambda e: e['ts'])[-keep_last:]
    history[symbol] = entries
    save_json(SQS_HISTORY_FILE, history)

def get_sqs_trend(symbol, min_points=3):
    """Returns trend dict with values, delta, and label."""
    history = load_json(SQS_HISTORY_FILE, {})
    entries = history.get(symbol, [])
    if len(entries) < min_points:
        return {'trend': 'insufficient', 'arrow_str': '', 'delta': 0}
    recent = entries[-min_points:]
    values = [e['sqs'] for e in recent]
    delta = values[-1] - values[0]
    slope = np.polyfit(np.arange(len(values)), values, 1)[0] if len(values) > 1 else 0

    if slope > 2.5 and delta >= 5:
        trend = 'improving'
    elif slope < -2.5 and delta <= -5:
        trend = 'declining'
    else:
        trend = 'stable'

    return {
        'trend': trend,
        'values': values,
        'delta': int(delta),
        'arrow_str': ' → '.join(str(v) for v in values),
    }

def format_sqs_trend_note(symbol):
    t = get_sqs_trend(symbol)
    if t['trend'] == 'insufficient':
        return ""
    if t['trend'] == 'improving':
        return f"📈 _Quality improving: {t['arrow_str']} (+{t['delta']})_"
    if t['trend'] == 'declining':
        return f"📉 _Quality declining: {t['arrow_str']} ({t['delta']})_"
    return f"➖ _Quality stable: {t['arrow_str']}_"


# ═══════════════════════════════════════════════════════════════════════════════
# §12  DYNAMIC SQS THRESHOLD
# ═══════════════════════════════════════════════════════════════════════════════

def compute_grade_stats(lookback_days=30):
    """Read trade history → per-grade win rate & avg R."""
    history = load_json(HISTORY_FILE, [])
    cutoff = now_est() - timedelta(days=lookback_days)
    stats = {g: {'total': 0, 'wins': 0, 'r_sum': 0.0} for g in ('A+', 'A', 'B', 'C')}
    for t in history:
        try:
            ca = t.get('closed_at')
            if not ca: continue
            dt = datetime.fromisoformat(ca)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            if dt < cutoff:
                continue
            g = t.get('grade', 'C')
            if g not in stats:
                continue
            r = t.get('final_r', 0) or 0
            stats[g]['total'] += 1
            stats[g]['r_sum'] += r
            if r > 0:
                stats[g]['wins'] += 1
        except Exception:
            continue
    out = {}
    for g, s in stats.items():
        out[g] = {
            'total': s['total'],
            'wins': s['wins'],
            'winrate': s['wins'] / s['total'] if s['total'] else None,
            'avg_r': s['r_sum'] / s['total'] if s['total'] else None,
        }
    return out

def compute_dynamic_threshold():
    """Adjust SQS threshold based on recent grade performance."""
    if not SQS_DYNAMIC_ENABLED:
        return {'threshold': SQS_BASE_THRESHOLD, 'reason': 'dynamic disabled'}

    stats = compute_grade_stats()
    b = stats.get('B', {})
    a = stats.get('A', {})
    total_sample = sum(s['total'] for s in stats.values())

    if total_sample < 10:
        result = {
            'threshold': SQS_BASE_THRESHOLD,
            'reason': f'insufficient data ({total_sample}/10)',
            'stats': stats,
            'computed_at': now_est().isoformat(),
        }
        save_json(DYNAMIC_THRESHOLD_FILE, result)
        return result

    threshold = SQS_BASE_THRESHOLD
    reason = "baseline"
    b_wr = b.get('winrate') or 0
    a_wr = a.get('winrate') or 0

    if b.get('total', 0) >= 5 and b_wr < 0.35:
        threshold = min(SQS_DYNAMIC_MAX, SQS_BASE_THRESHOLD + 5)
        reason = f"B winrate {b_wr:.0%} weak — tighten"
    elif b.get('total', 0) >= 5 and b_wr < 0.45:
        threshold = min(SQS_DYNAMIC_MAX, SQS_BASE_THRESHOLD + 3)
        reason = f"B winrate {b_wr:.0%} subpar — tighten slightly"
    elif a.get('total', 0) >= 5 and a_wr > 0.65:
        threshold = max(SQS_DYNAMIC_MIN, SQS_BASE_THRESHOLD - 3)
        reason = f"A winrate {a_wr:.0%} strong — relax"

    result = {
        'threshold': int(threshold),
        'reason': reason,
        'stats': stats,
        'computed_at': now_est().isoformat(),
    }
    save_json(DYNAMIC_THRESHOLD_FILE, result)
    return result

def get_effective_threshold():
    """Returns current threshold (cached, recomputed daily)."""
    cached = load_json(DYNAMIC_THRESHOLD_FILE, None)
    if cached:
        try:
            dt = datetime.fromisoformat(cached.get('computed_at', ''))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            if now_est() - dt < timedelta(hours=24):
                return cached.get('threshold', SQS_BASE_THRESHOLD)
        except Exception:
            pass
    # Recompute
    return compute_dynamic_threshold()['threshold']


# ═══════════════════════════════════════════════════════════════════════════════
# §13  DATA FETCHERS — live price, HTF, MTF
# ═══════════════════════════════════════════════════════════════════════════════

def get_real_time_price(sym):
    """Fetch latest 1m close."""
    try:
        df = yf.download(sym, period='1d', interval='1m',
                         progress=False, auto_adjust=True)
        if df.empty: return None
        df = _clean_df(df)
        return float(df['Close'].iloc[-1])
    except Exception:
        return None

def get_live_ohlc(sym):
    """Latest 5m OHLC — used for trade checks."""
    try:
        df = yf.download(sym, period='2d', interval='5m',
                         progress=False, auto_adjust=True)
        if df.empty: return None
        df = _clean_df(df)
        return (float(df['Close'].iloc[-1]),
                float(df['High'].iloc[-1]),
                float(df['Low'].iloc[-1]))
    except Exception:
        return None

def get_daily_close(sym):
    try:
        df = yf.download(sym, period='5d', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty: return None
        df = _clean_df(df)
        return float(df['Close'].iloc[-1])
    except Exception:
        return None

def sanity_check_price(sym, live, daily_close=None):
    daily = daily_close if daily_close is not None else get_daily_close(sym)
    if daily is None or daily <= 0:
        return True
    return abs(live - daily) / daily <= PRICE_SANITY_DEVIATION

def get_htf_bias(symbol):
    """4h EMA50 > EMA200 = bullish. Returns True/False/None."""
    try:
        df = yf.download(symbol, period='3mo', interval='1h',
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            return None
        df = _clean_df(df)
        df4 = df.resample('4h').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min',
            'Close': 'last', 'Volume': 'sum'
        }).dropna()
        if len(df4) < 50:
            return None
        # Use last CLOSED bar (iloc[-2])
        e50 = ema(df4['Close'], 50).iloc[-2]
        e200 = ema(df4['Close'], min(200, len(df4))).iloc[-2]
        return bool(e50 > e200)
    except Exception as e:
        logging.error(f"HTF {symbol}: {e}")
        return None

def get_mtf_score(symbol, tf_str):
    """Returns 0-3 bull score for a single TF."""
    tf_map = {
        '15m': ('5d', '15m'),
        '1h':  ('3mo', '1h'),
        '4h':  ('6mo', '1h'),
        '1d':  ('2y', '1d'),
    }
    if tf_str not in tf_map:
        return 0
    period, interval = tf_map[tf_str]
    try:
        df = yf.download(symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df.empty: return 0
        df = _clean_df(df)
        if tf_str == '4h':
            df = df.resample('4h').agg({
                'Open': 'first', 'High': 'max', 'Low': 'min',
                'Close': 'last', 'Volume': 'sum'
            }).dropna()
        if len(df) < 50:
            return 0
        e50 = ema(df['Close'], 50).iloc[-2]
        e200 = ema(df['Close'], min(200, len(df))).iloc[-2]
        rsi = pine_rsi(df['Close'], RSI_LEN).iloc[-2]
        c = df['Close'].iloc[-2]
        score = 0
        if e50 > e200: score += 1
        if rsi > 50:   score += 1
        if c > e50:    score += 1
        return score
    except Exception as e:
        logging.error(f"MTF {symbol} {tf_str}: {e}")
        return 0

def get_mtf_sum(symbol):
    return sum(get_mtf_score(symbol, tf) for tf in MTF_FRAMES)

# ═══════════════════════════════════════════════════════════════════════════════
# §14  SIGNAL ANALYSIS ENGINE — Pine parity + all bug fixes
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_symbol(symbol, tf_config, htf_bull, mtf_sum, last_signal_info=None):
    """
    Core signal engine.

    ✅ FIX: Uses iloc[-2] as signal bar (last CLOSED bar) to prevent repainting.
    ✅ FIX: Flip detection looks at last 2 bars (catches recent flips).
    ✅ FIX: rsi_bull / rsi_bear mutually exclusive.
    ✅ FIX: Single data fetch per call.

    Returns: (result_dict, reason_or_None)
    """
    tf = tf_config['tf']
    lookback = tf_config['lookback']
    min_bars = tf_config['min_bars']

    try:
        df = yf.download(symbol, period=lookback, interval=tf,
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < min_bars:
            return None, "insufficient data"
        df = _clean_df(df)

        # ─── Indicators ───
        df['ema20'] = ema(df['Close'], 20)
        df['ema50'] = ema(df['Close'], 50)
        df['ema200'] = ema(df['Close'], min(200, len(df)))
        df['rsi'] = pine_rsi(df['Close'], RSI_LEN)
        df['atr'] = pine_atr(df, ATR_LEN)
        df['macd'], df['signal'] = pine_macd(df['Close'], MACD_FAST, MACD_SLOW, MACD_SIG)
        df['adx'], df['plus_di'], df['minus_di'] = pine_adx(df, ADX_LEN)
        st_trend, _, _ = pine_supertrend(df, ST_PERIODS, ST_MULT)
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

        # AE Range Filter (volume-driven, Pine parity)
        srng = smooth_range(df['Close'], AE_LENGTH, 3)
        basetype = range_filter(df['Close'], df['Volume'], srng)
        hband = basetype + srng
        lowband = basetype - srng
        uprng_raw = trend_up_value(basetype)
        df['hband'] = hband
        df['lowband'] = lowband
        df['uprng'] = uprng_raw > 0

        # ═══ USE LAST CLOSED BAR FOR SIGNALS (v7.0 fix) ═══
        if len(df) < 4:
            return None, "not enough bars for signal logic"
        last = df.iloc[-2]       # last CLOSED bar
        prev = df.iloc[-3]       # bar before that
        bar2 = df.iloc[-4]       # for flip detection window

        bar_price = float(last['Close'])
        atr_val = float(last['atr'])
        if atr_val <= 0 or pd.isna(atr_val):
            return None, "invalid ATR"

        rsi_val = float(last['rsi'])
        adx_val = float(last['adx'])
        ema50_v = float(last['ema50'])
        ema200_v = float(last['ema200'])
        uprng = bool(last['uprng'])
        st_now = int(last['st'])
        vwap_v = float(last['vwap'])
        plus_di = float(last['plus_di'])
        minus_di = float(last['minus_di'])

        # ─── RSI Divergence (simplified) ───
        rsi_bull_div = False
        rsi_bear_div = False
        if USE_RSI_DIV and len(df) > RSI_DIV_LOOK * 3:
            try:
                lows = df['Low'].iloc[-RSI_DIV_LOOK * 3:-RSI_DIV_LOOK]
                rsi_lows = df['rsi'].iloc[-RSI_DIV_LOOK * 3:-RSI_DIV_LOOK]
                if len(lows) > 2 and lows.iloc[-1] < lows.iloc[0] and rsi_lows.iloc[-1] > rsi_lows.iloc[0]:
                    rsi_bull_div = rsi_val >= RSI_DIV_FLOOR
                highs = df['High'].iloc[-RSI_DIV_LOOK * 3:-RSI_DIV_LOOK]
                rsi_highs = df['rsi'].iloc[-RSI_DIV_LOOK * 3:-RSI_DIV_LOOK]
                if len(highs) > 2 and highs.iloc[-1] > highs.iloc[0] and rsi_highs.iloc[-1] < rsi_highs.iloc[0]:
                    rsi_bear_div = rsi_val <= RSI_BEAR_CEIL
            except Exception:
                pass

        # ═══ v7.0 FIX: Mutually exclusive rsi_bull / rsi_bear ═══
        if rsi_val > 50:
            rsi_bull, rsi_bear = True, False
        elif rsi_val < 50:
            rsi_bull, rsi_bear = False, True
        else:
            rsi_bull = rsi_bear = False
        # Divergence overrides
        if USE_RSI_DIV and rsi_bull_div and rsi_val >= 40:
            rsi_bull, rsi_bear = True, False
        elif USE_RSI_DIV and rsi_bear_div and rsi_val <= 60:
            rsi_bull, rsi_bear = False, True

        macd_bull = last['macd'] > last['signal']
        ema_bull = ema50_v > ema200_v

        # ─── Confluence (9 points total) ───
        bull = 0
        bull += 1 if uprng else 0
        bull += 1 if st_now == 1 else 0
        bull += 1 if macd_bull else 0
        bull += 1 if rsi_bull else 0
        bull += 1 if ema_bull else 0
        bull += 1 if bar_price > vwap_v else 0
        bull += 1 if adx_val > ADX_STRONG and plus_di > minus_di else 0
        bull += 1 if htf_bull is True else 0
        bull += 1 if bool(sqz_bull_break.iloc[-2]) else 0

        bear = 0
        bear += 1 if not uprng else 0
        bear += 1 if st_now == -1 else 0
        bear += 1 if not macd_bull else 0
        bear += 1 if rsi_bear else 0
        bear += 1 if not ema_bull else 0
        bear += 1 if bar_price < vwap_v else 0
        bear += 1 if adx_val > ADX_STRONG and minus_di > plus_di else 0
        bear += 1 if htf_bull is False else 0
        bear += 1 if bool(sqz_bear_break.iloc[-2]) else 0

        # ─── Triggers ───
        prev_close = float(prev['Close'])
        prev_hband = float(prev['hband'])
        prev_lowband = float(prev['lowband'])
        cross_up = prev_close <= prev_hband and bar_price > float(last['hband'])
        cross_dn = prev_close >= prev_lowband and bar_price < float(last['lowband'])

        # ═══ v7.0 FIX: Flip window = last 2 bars ═══
        flip_bull = (not bool(bar2['uprng'])) and uprng
        flip_bear = bool(bar2['uprng']) and (not uprng)

        trigger_bull = cross_up or flip_bull
        trigger_bear = cross_dn or flip_bear

        # ─── Hard gates ───
        adx_pass_bull = (adx_val > ADX_GATE_LEVEL) or (bull >= ADX_BYPASS_MIN)
        adx_pass_bear = (adx_val > ADX_GATE_LEVEL) or (bear >= ADX_BYPASS_MIN)

        htf_st_both_bear = (htf_bull is False) and (st_now == -1)
        htf_st_both_bull = (htf_bull is True) and (st_now == 1)
        ct_buy = USE_COUNTER_TREND_BLOCK and bull < 6 and htf_st_both_bear
        ct_sell = USE_COUNTER_TREND_BLOCK and bear < 6 and htf_st_both_bull

        mtf_block_sell = USE_MTF_GATE and mtf_sum >= MTF_GATE_BULL
        mtf_block_buy = USE_MTF_GATE and mtf_sum <= MTF_GATE_BEAR

        # Chop filter
        chop_ok = True
        if USE_CHOP_FILTER and last_signal_info:
            try:
                prev_price = last_signal_info.get('price')
                prev_atr = last_signal_info.get('atr', atr_val)
                if prev_price and prev_atr:
                    if abs(bar_price - prev_price) < prev_atr * CHOP_ATR_MULT:
                        chop_ok = False
            except Exception:
                pass

        # ═══ Signal decision ═══
        raw_buy = (uprng and trigger_bull and adx_pass_bull and
                   bull >= MIN_CONF_SCORE and grade_passes(bull) and
                   not ct_buy and not mtf_block_buy and chop_ok)
        raw_sell = (not uprng and trigger_bear and adx_pass_bear and
                    bear >= MIN_CONF_SCORE and grade_passes(bear) and
                    not ct_sell and not mtf_block_sell and chop_ok)

        # Resolve conflicts
        if raw_buy and raw_sell:
            if bull >= bear: raw_sell = False
            else: raw_buy = False

        if not raw_buy and not raw_sell:
            if bull >= 7 and not trigger_bull: return None, f"bull={bull} no trigger"
            if bear >= 7 and not trigger_bear: return None, f"bear={bear} no trigger"
            if ct_buy:         return None, "counter-trend BUY blocked"
            if ct_sell:        return None, "counter-trend SELL blocked"
            if mtf_block_buy:  return None, f"MTF blocks BUY (sum={mtf_sum})"
            if mtf_block_sell: return None, f"MTF blocks SELL (sum={mtf_sum})"
            if not chop_ok:    return None, "chop filter"
            return None, None

        signal = 'BUY' if raw_buy else 'SELL'
        score = bull if raw_buy else bear

        # ═══ v7.0 FEATURE: VIX blocks longs in panic ═══
        vix_blocked, vix_reason = vix_blocks(signal)
        if vix_blocked:
            return None, vix_reason

        # ─── SQS calc ───
        def calc_sqs(is_bull):
            sc = bull if is_bull else bear
            conf_pct = sc / 9 * 40   # /9 since we have 9 confluence points
            mtf_pct = (mtf_sum / 12 * 25) if is_bull else ((12 - mtf_sum) / 12 * 25)
            if adx_val >= REGIME_ADX_TREND: reg_pct = 15.0
            elif adx_val < REGIME_ADX_RANGE: reg_pct = 5.0
            else: reg_pct = 8.0
            vol_avg_v = float(last['vol_avg']) if not pd.isna(last['vol_avg']) else 1
            cur_vol = float(last['Volume'])
            if cur_vol > vol_avg_v * 1.5: vol_pct = 10.0
            elif cur_vol > vol_avg_v: vol_pct = 6.0
            else: vol_pct = 3.0
            atr_avg = df['atr'].rolling(50).mean().iloc[-2]
            vol_ratio = atr_val / atr_avg if atr_avg and atr_avg > 0 else 1.0
            if 0.8 <= vol_ratio <= 1.5: volat_pct = 10.0
            elif 0.6 <= vol_ratio <= 2.0: volat_pct = 7.0
            else: volat_pct = 3.0
            return min(100, conf_pct + mtf_pct + reg_pct + vol_pct + volat_pct)

        sqs = calc_sqs(raw_buy)
        effective_threshold = get_effective_threshold()

        if USE_SQS and sqs < effective_threshold:
            return None, f"SQS {sqs:.0f} < {effective_threshold}"

        # ─── Live price & sanity ───
        live_price = get_real_time_price(symbol)
        entry_price = live_price if live_price else bar_price
        daily_close = get_daily_close(symbol)
        if live_price and not sanity_check_price(symbol, live_price, daily_close):
            return None, f"bad data (live=${live_price:.2f})"

        # ─── SL/TP ───
        recent_low = float(df['Low'].iloc[-SWING_LOOKBACK - 1:-1].min())
        recent_high = float(df['High'].iloc[-SWING_LOOKBACK - 1:-1].max())
        max_sl_pct = MAX_SL_PCT_CRYPTO if is_crypto(symbol) else MAX_SL_PCT_STOCKS
        min_sl_pct = MIN_SL_PCT_CRYPTO if is_crypto(symbol) else MIN_SL_PCT_STOCKS

        if signal == 'BUY':
            atr_sl = entry_price - atr_val * SL_MULT
            struct_sl = recent_low - atr_val * STRUCT_BUFFER
            sl = max(atr_sl, struct_sl)
            min_dist = atr_val * MIN_SL_DIST
            if (entry_price - sl) < min_dist:
                sl = entry_price - min_dist
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

        # ─── Nearby levels ───
        nearby_high = float(df['High'].iloc[-60:].max())
        nearby_low = float(df['Low'].iloc[-60:].min())
        nearby = {
            'resistance': nearby_high if bar_price < nearby_high else None,
            'support':    nearby_low if bar_price > nearby_low else None,
            'ema50':      ema50_v,
            'ema200':     ema200_v,
        }

        # ─── v7.0 FEATURE: POC ───
        lookback = 260 if tf == '30m' else 130
        poc_data = compute_poc(df, bins=30, lookback_bars=lookback)

        # ─── Meta ───
        tf_minutes = 30 if tf == '30m' else 60
        expiry = now_est() + timedelta(minutes=tf_minutes * 2)
        decimals = 4 if entry_price < 10 else 2

        if adx_val >= REGIME_ADX_TREND: regime = 'TRENDING'
        elif adx_val < REGIME_ADX_RANGE: regime = 'RANGING'
        else: regime = 'TRANSITIONAL'

        if flip_bull:   trigger_type = "AE Flip Bullish"
        elif flip_bear: trigger_type = "AE Flip Bearish"
        elif cross_up:  trigger_type = "Breakout Above Band"
        elif cross_dn:  trigger_type = "Breakdown Below Band"
        else:           trigger_type = "Signal"

        if signal == 'BUY':
            strong_trend = (ema_bull and bar_price > ema50_v and
                            22 < adx_val < 55 and plus_di > minus_di)
        else:
            strong_trend = (not ema_bull and bar_price < ema50_v and
                            22 < adx_val < 55 and minus_di > plus_di)

        return {
            'symbol': symbol,
            'emoji': SYMBOL_EMOJI.get(symbol, '📈'),
            'signal': signal,
            'price': round(entry_price, decimals),
            'bar_price': round(bar_price, decimals),
            'atr': round(atr_val, decimals),
            'score': int(score),
            'grade': grade_label(score),
            'sqs': round(sqs),
            'tier': tier_label(sqs),
            'trigger': trigger_type,
            'sl': round(sl, decimals),
            'sl_pct': round(abs(sl - entry_price) / entry_price * 100, 2),
            'tp1': round(tp1, decimals),
            'tp2': round(tp2, decimals),
            'tp3': round(tp3, decimals),
            'risk': round(risk, decimals),
            'rsi': round(rsi_val, 1),
            'adx': round(adx_val, 1),
            'stretch': round(abs(bar_price - ema50_v) / atr_val, 1),
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
            'poc_data': poc_data,
            'expiry_time': expiry.isoformat(),
            'effective_threshold': effective_threshold,
        }, None

    except Exception as e:
        logging.error(f"{symbol} [{tf}]: {e}")
        return None, f"error: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# §15  TRADE TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

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
        'atr_at_entry': sig.get('atr'),
        'decimals': sig['decimals'],
        'grade': sig['grade'],
        'sqs': sig['sqs'],
        'tier': sig['tier'],
        'tf': sig['timeframe'],
        'tf_label': sig['tf_label'],
        'trigger': sig.get('trigger'),
        'ai_text_at_entry': sig.get('ai_text'),
        'opened_at': now_est().isoformat(),
        'opened_session': sig['session'],
        'mtf_sum': sig.get('mtf_sum'),
        'htf_bull': sig.get('htf_bull'),
        'tp1_hit': False, 'tp2_hit': False, 'tp3_hit': False,
        'tp1_hit_at': None, 'tp2_hit_at': None, 'tp3_hit_at': None,
        'closed': False, 'closed_reason': None,
        'closed_at': None, 'final_r': None,
    }

def check_trade_progress(trade):
    """Returns (events_list, is_closed)."""
    live = get_live_ohlc(trade['symbol'])
    if not live:
        return [], False
    current, _, _ = live
    events = []
    is_long = trade['signal'] == 'BUY'

    # Timeout
    try:
        opened = datetime.fromisoformat(trade['opened_at'])
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=EST)
        if now_est() - opened > timedelta(hours=MAX_TRADE_AGE_HOURS):
            trade['closed'] = True
            trade['closed_reason'] = 'Timeout (72h)'
            trade['closed_at'] = now_est().isoformat()
            trade['final_r'] = 0
            events.append({'type': 'TIMEOUT', 'price': current})
            return events, True
    except Exception:
        pass

    # SL
    sl_hit = (is_long and current <= trade['sl']) or (not is_long and current >= trade['sl'])
    if sl_hit:
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


# ═══════════════════════════════════════════════════════════════════════════════
# §16  AI ENRICHMENT (Gemini)
# ═══════════════════════════════════════════════════════════════════════════════

def get_ai_analysis(sig):
    if not GEMINI_API_KEY:
        return None

    ah_note = ""
    if sig.get('is_extended_hours'):
        ah_note = "\nNOTE: After-hours/pre-market signal — liquidity thin."

    prompt = f"""Analyze this trading signal in EXACTLY 3 short lines (max 100 chars each).

SYMBOL: {sig['symbol']} ({sig['signal']} @ ${sig['price']})
TF: {sig['timeframe']} | Trigger: {sig['trigger']}
Score: {sig['score']}/9 ({sig['grade']}) | SQS: {sig['sqs']}/100
RSI: {sig['rsi']} | ADX: {sig['adx']} | Regime: {sig['regime']}
MTF sum: {sig.get('mtf_sum', '?')}/12 | HTF: {sig.get('htf_bull')}
Reward ratio: 1:3 | Strong trend: {sig['strong_trend']}{ah_note}

Respond EXACTLY (no extra lines):
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
            if data.get('candidates'):
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        logging.error(f"AI error: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# §17  ALERT BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

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
        if session in ("🌑 Overnight", "🌐 Weekend"):
            return "💡 _Low volume — use tight limit orders_"
        return None
    tips = {
        "🌅 Pre-Market":  "🌅 _Pre-market: LIMIT orders only, reduce size, watch 9:30 AM gap_",
        "🌙 After-Hours": "🌙 _After-hours: LIMIT orders only, wider spreads_",
        "🔔 Market Open": "🔔 _First 30 min: high volatility, wait for spread to tighten_",
        "⚡ Power Hour":  "⚡ _Power hour: watch for end-of-day reversals_",
    }
    return tips.get(session)

def format_new_signal(sig, ai_text=None):
    """
    Full-detail signal alert with:
    • Urgency emoji prefix (v7.0)
    • Plain-English R:R (v7.0)
    • POC context (v7.0)
    • SQS trending (v7.0)
    • VIX warning (v7.0)
    • Markdown-safe (v7.0)
    """
    emoji = "🟢" if sig['signal'] == 'BUY' else "🔴"
    d = sig['decimals']
    sym_safe = safe_sym(sig['symbol'])
    sym_em = sig['emoji']

    # VIX context for header
    vix = get_vix_regime()
    vix_warn = bool(vix and vix.get('regime') in ('spike', 'extreme'))

    # Urgency prefix
    prefix = urgency_prefix(sig['sqs'], sig['strong_trend'], vix_warn)

    # Header
    msg = f"{prefix}{sig['tier']} {emoji} *{sig['signal']} {sym_em} {sym_safe}* `[{sig['tf_label']}]`\n"
    msg += f"{sig['session']} • {fmt_time()}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"

    # VIX warning banner
    if vix and vix.get('warning') and vix['regime'] in ('spike', 'extreme'):
        msg += f"{vix['warning']}\n"
        msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"

    if sig.get('is_extended_hours'):
        msg += "⚠️ *After-hours — thin liquidity!*\n"
        msg += "_Use LIMIT orders, expect wider spreads._\n"
        msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"

    # Price + quality
    msg += f"💵 *Live Entry:* `${fmt_price(sig['price'], d)}`\n"
    msg += f"🎯 *Trigger:* {sig['trigger']}\n"
    msg += f"📊 *Quality:* {sig['score']}/9 ({sig['grade']}) • SQS *{sig['sqs']}*\n"
    msg += f"`{sqs_meter(sig['sqs'])}`\n"

    # SQS trend (v7.0)
    trend_note = format_sqs_trend_note(sig['symbol'])
    if trend_note:
        msg += f"{trend_note}\n"

    # MTF / HTF
    if sig.get('mtf_sum') is not None:
        mtf = sig['mtf_sum']
        mtf_bar = "█" * mtf + "░" * (12 - mtf)
        msg += f"🗂️ *Multi-TF:* `{mtf_bar}` {mtf}/12\n"
    if sig.get('htf_bull') is not None:
        htf_state = "▲ Bullish" if sig['htf_bull'] else "▼ Bearish"
        aligned = "✓" if (sig['signal'] == 'BUY') == sig['htf_bull'] else "⚠️"
        msg += f"🏔️ *Higher TF:* {htf_state} {aligned}\n"

    if sig['sqs'] < 75:
        msg += "_⚠️ Borderline quality — consider half-size_\n"

    # ═══ Plain-English TRADE PLAN ═══
    msg += "\n*🎯 TRADE PLAN*\n"
    msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
    risk_dollars = abs(sig['price'] - sig['sl'])
    reward_dollars = abs(sig['tp3'] - sig['price'])
    msg += f"📍 Entry:  `${fmt_price(sig['price'], d)}`\n"
    msg += f"🛑 Stop:   `${fmt_price(sig['sl'], d)}` ({sig['sl_pct']}% away)\n"
    if sig['sl_pct'] < 1.0:
        msg += "   _⚠️ Very tight stop — noise risk_\n"
    elif sig['sl_pct'] > 5.0:
        msg += "   _⚠️ Wide stop — larger drawdown risk_\n"

    # Plain-English R:R
    msg += f"💰 {fmt_risk_reward_line(risk_dollars, reward_dollars)}\n\n"
    msg += f"  • {tp_line(1, sig['price'], sig['tp1'], sig['risk'])}\n"
    msg += f"  • {tp_line(2, sig['price'], sig['tp2'], sig['risk'])}\n"
    msg += f"  • {tp_line(3, sig['price'], sig['tp3'], sig['risk'])}\n"

    # ─── POC line (v7.0) ───
    poc_line = format_poc_line(sig['price'], sig.get('poc_data'))
    if poc_line:
        msg += f"\n{poc_line}\n"

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
        msg += "\n*🔍 KEY LEVELS*\n"
        msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
        for name, val, dist, arrow in meaningful:
            msg += f"{arrow} {name}: `${fmt_price(val, d)}` ({dist:.1f}%)\n"

    # Technicals
    msg += "\n*📈 TECHNICALS*\n"
    msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"RSI: `{sig['rsi']}` | ADX: `{sig['adx']}` | Stretch: `{sig['stretch']}×`\n"
    msg += f"Regime: {sig['regime']} | Trend: {'Strong ✓' if sig['strong_trend'] else 'Mixed ⚠️'}\n"

    # Expiry
    exp_abs = absolute_time(sig['expiry_time'])
    exp_rel = time_until(sig['expiry_time'])
    msg += f"\n⏳ *Valid until:* {exp_abs} ({exp_rel})\n"
    msg += "_Re-evaluate on next candle after expiry._\n"

    # Session tips
    tips = get_session_tips(sig['session'], sig.get('is_crypto', False))
    if tips:
        msg += f"\n{tips}\n"

    # AI
    if ai_text:
        msg += "\n*🤖 AI ANALYSIS*\n"
        msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
        msg += f"{ai_text}\n"

    return msg

def format_trade_event(trade, event, current):
    emoji = "🟢" if trade['signal'] == 'BUY' else "🔴"
    d = trade['decimals']
    sym_em = trade.get('emoji', '📈')
    sym_safe = safe_sym(trade['symbol'])
    age = time_ago(trade['opened_at'])
    et = event['type']
    risk = trade.get('risk', 0)

    def profit_phrase(price):
        if not risk:
            return ""
        r_mult = abs(price - trade['entry']) / risk
        profit = abs(price - trade['entry'])
        return f"+${profit:.2f} ({r_mult:.1f}× your risk)"

    if et == 'TP1':
        header = f"✅ *TARGET 1 HIT* {emoji} {sym_em} {sym_safe} `[{trade.get('tf_label', trade['tf'])}]`"
        sub = "💡 *Recommended next step:*"
        steps = [
            f"  ✓ Move stop: `${fmt_price(trade['sl'], d)}` → `${fmt_price(trade['entry'], d)}` (breakeven)",
            f"  ✓ Take ~33% profits off the table",
            f"  ✓ Let the rest run to Target 2 & 3",
        ]
        profit_str = profit_phrase(trade['tp1'])
    elif et == 'TP2':
        header = f"✅✅ *TARGET 2 HIT* {emoji} {sym_em} {sym_safe} `[{trade.get('tf_label', trade['tf'])}]`"
        sub = "💡 *Recommended next step:*"
        steps = [
            f"  ✓ Move stop to Target 1 `${fmt_price(trade['tp1'], d)}`",
            f"  ✓ Another ~33% off",
            f"  ✓ Final portion rides to Target 3",
        ]
        profit_str = profit_phrase(trade['tp2'])
    elif et == 'TP3':
        header = f"🏆 *ALL TARGETS HIT* {emoji} {sym_em} {sym_safe}"
        sub = "🎉 *Trade complete!*"
        steps = ["  ✓ Close remaining position", "  ✓ Full reward achieved", "  ✓ Log this win"]
        profit_str = profit_phrase(trade['tp3'])
    elif et == 'SL':
        header = f"🛑 *STOP HIT* {emoji} {sym_em} {sym_safe}"
        if trade['tp1_hit']:
            sub = "✅ *Trailed profit exit — still a winner*"
            steps = ["  ✓ Partial gain locked in", "  ✓ No further action", "  ✓ Review setup"]
            profit_str = "Partial gain kept"
        else:
            sub = "❌ *Trade stopped out at plan*"
            steps = ["  ✓ Loss limited to plan", "  ✓ No revenge trades", "  ✓ Log for review"]
            loss = abs(trade['sl'] - trade['entry'])
            profit_str = f"-${loss:.2f} (1× your risk — as planned)"
    elif et == 'TIMEOUT':
        header = f"⏰ *TRADE TIMED OUT* {emoji} {sym_em} {sym_safe}"
        sub = "_72h expiry — auto-closed_"
        steps = ["  ✓ Signal aged out"]
        profit_str = "—"
    else:
        return None

    msg = f"{header}\n"
    msg += f"⏱ Trade age: {age}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"💵 *Current price:* `${fmt_price(current, d)}`\n"
    msg += f"🎯 *Hit at:* `${fmt_price(event['price'], d)}` — {profit_str}\n\n"
    msg += f"{sub}\n"
    for s in steps:
        msg += f"{s}\n"
    msg += "\n*📊 PRICE LADDER*\n"
    msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += price_ladder(trade, current)
    msg += f"\n\n⏰ {fmt_time()}"
    return msg

def format_digest(signals):
    msg = f"🔔 *SIGNAL DIGEST — {len(signals)} alerts*\n"
    msg += f"{get_session()} • {fmt_time()}\n"
    msg += "`━━━━━━━━━━━━━━━━━━━━━`\n\n"

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
        prefix = urgency_prefix(sig['sqs'], sig['strong_trend'])
        sym = safe_sym(sig['symbol'])
        msg += f"{prefix}{sig['tier']} {em} *{sym}* `[{sig['tf_label']}]`{ah}{multi}\n"
        msg += f"  {sig['emoji']} {sig['signal']} @ `${fmt_price(sig['price'], sig['decimals'])}` • SQS {sig['sqs']} • MTF {sig.get('mtf_sum','?')}/12\n"
        msg += f"  🎯 {sig['trigger']} | RSI {sig['rsi']}\n"

        risk_dollars = abs(sig['price'] - sig['sl'])
        reward_dollars = abs(sig['tp3'] - sig['price'])
        msg += f"  💰 Risk `${risk_dollars:.2f}` → Make `${reward_dollars:.2f}`\n\n"

    if multi_tf:
        msg += f"🎯🎯 *Multi-TF confirmation:* {', '.join(sorted(multi_tf))}\n"
        msg += "_Fired on multiple timeframes — highest conviction._\n\n"

    msg += f"_Full details for each below._"
    return msg

def format_open_positions_summary(trades):
    active = [(k, t) for k, t in trades.items() if not t.get('closed')]
    if not active:
        return None

    enriched = []
    longs = shorts = 0
    tf_counts = {'⚡30m': 0, '📊1h': 0}

    for k, trade in active:
        live = get_live_ohlc(trade['symbol'])
        if not live:
            enriched.append({'trade': trade, 'current': None, 'r_mult': 0,
                             'status': 'no data', 'bucket': 'nodata'})
            continue
        current = live[0]
        is_long = trade['signal'] == 'BUY'
        pnl = (current - trade['entry']) if is_long else (trade['entry'] - current)
        r_mult = pnl / trade['risk'] if trade['risk'] > 0 else 0
        if is_long: longs += 1
        else: shorts += 1
        lbl = trade.get('tf_label', trade['tf'])
        if lbl in tf_counts:
            tf_counts[lbl] += 1

        if trade.get('tp3_hit'):   status, bucket = "🏆 3× risk", "winner"
        elif trade.get('tp2_hit'): status, bucket = "🎯🎯 2× risk", "winner"
        elif trade.get('tp1_hit'): status, bucket = "🎯 1× risk, trailing", "winner"
        elif r_mult >= 0.5:        status, bucket = "📈 winning", "winner"
        elif r_mult <= -0.7:       status, bucket = "⚠️ near stop", "near_sl"
        elif r_mult < -0.15:       status, bucket = "🔻 losing", "loser"
        elif abs(r_mult) < 0.15:   status, bucket = "➖ flat", "flat"
        else:                      status, bucket = "📊 building", "building"

        enriched.append({'trade': trade, 'current': current, 'r_mult': r_mult,
                         'status': status, 'bucket': bucket})

    enriched.sort(key=lambda x: -x['r_mult'])
    valid = [e for e in enriched if e['current'] is not None]
    total_r = sum(e['r_mult'] for e in valid)
    winners = [e for e in valid if e['r_mult'] > 0.1]
    losers = [e for e in valid if e['r_mult'] < -0.1]
    near_sl = [e for e in valid if e['bucket'] == 'near_sl']

    now = now_est()
    tz_abbr = now.tzname() or "EDT"
    ts = now.strftime(f'%a %b %d • %I:%M %p {tz_abbr}')

    msg = f"📊 *OPEN POSITIONS ({len(active)})*\n"
    msg += f"🕒 {ts}\n"
    msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
    total_str = fmt_r(total_r, plain=True)
    total_em = "🟢" if total_r >= 0 else "🔴"
    msg += f"{total_em} *Overall P&L:* {total_str}\n"
    msg += f"🟢 Long: {longs} | 🔴 Short: {shorts}"
    if tf_counts['⚡30m'] or tf_counts['📊1h']:
        msg += f" | 30m: {tf_counts['⚡30m']} | 1h: {tf_counts['📊1h']}"
    msg += "\n"
    msg += f"📈 Winners: {len(winners)} • Losers: {len(losers)}"
    if near_sl:
        msg += f" • ⚠️ Near stop: {len(near_sl)}"
    msg += "\n"

    def _row(e, emph=False):
        t = e['trade']
        em = t.get('emoji', '📈')
        dir_em = "🟢" if t['signal'] == 'BUY' else "🔴"
        r_str = fmt_r(e['r_mult'], plain=True)
        r_str = f"*{r_str}*" if emph else r_str
        sym = safe_sym(t['symbol'])
        line = f"  {em} {dir_em} *{sym}* `{t.get('tf_label', t['tf'])}` {r_str}"
        line += f" • {time_ago(t['opened_at'])}"
        if emph and e.get('status'):
            line += f" • {e['status']}"
        return line + "\n"

    if near_sl:
        msg += f"\n⚠️ *NEAR STOP* ({len(near_sl)})\n`─────────────────`\n"
        for e in near_sl: msg += _row(e, emph=True)

    win_items = [e for e in enriched if e['bucket'] == 'winner']
    if win_items:
        msg += f"\n✅ *WINNERS* ({len(win_items)})\n`─────────────────`\n"
        for e in win_items: msg += _row(e, emph=True)

    build_items = [e for e in enriched if e['bucket'] in ('building', 'flat')]
    if build_items:
        msg += f"\n📊 *BUILDING / FLAT* ({len(build_items)})\n`─────────────────`\n"
        for e in build_items: msg += _row(e, emph=False)

    lose_items = [e for e in enriched if e['bucket'] == 'loser']
    if lose_items:
        msg += f"\n🔻 *LOSING* ({len(lose_items)})\n`─────────────────`\n"
        for e in lose_items: msg += _row(e, emph=True)

    nodata_items = [e for e in enriched if e['bucket'] == 'nodata']
    if nodata_items:
        msg += f"\n❔ *No live data* ({len(nodata_items)})\n"
        for e in nodata_items:
            msg += f"  {e['trade'].get('emoji', '📈')} {safe_sym(e['trade']['symbol'])}\n"

    msg += "\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    if winners:
        best = max(valid, key=lambda e: e['r_mult'])
        msg += f"🏆 Best: *{safe_sym(best['trade']['symbol'])}* {fmt_r(best['r_mult'], plain=True)}\n"
    if losers:
        worst = min(valid, key=lambda e: e['r_mult'])
        msg += f"💥 Worst: *{safe_sym(worst['trade']['symbol'])}* {fmt_r(worst['r_mult'], plain=True)}\n"

    return msg

def format_weekly_summary():
    history = load_json(HISTORY_FILE, [])
    if not history:
        return None
    cutoff = now_est() - timedelta(days=7)
    week = []
    for t in history:
        try:
            ca = t.get('closed_at')
            if not ca: continue
            dt = datetime.fromisoformat(ca)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            if dt >= cutoff:
                week.append(t)
        except Exception:
            continue

    if not week:
        return None

    wins = [t for t in week if (t.get('final_r') or 0) > 0]
    losses = [t for t in week if (t.get('final_r') or 0) < 0]
    be = [t for t in week if (t.get('final_r') or 0) == 0]
    total_r = sum((t.get('final_r') or 0) for t in week)
    wr = len(wins) / len(week) * 100 if week else 0
    best = max(week, key=lambda t: t.get('final_r', 0) or 0)
    worst = min(week, key=lambda t: t.get('final_r', 0) or 0)

    grades = {'A+': [0, 0], 'A': [0, 0], 'B': [0, 0], 'C': [0, 0]}
    for t in week:
        g = t.get('grade', 'C')
        if g in grades:
            grades[g][0] += 1
            if (t.get('final_r') or 0) > 0:
                grades[g][1] += 1

    msg = f"📊 *WEEKLY SUMMARY*\n{cutoff.strftime('%b %d')} → {now_est().strftime('%b %d')}\n"
    msg += "`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"Total signals: *{len(week)}*\n"
    msg += f"✅ Wins: *{len(wins)}* ({wr:.0f}%)\n"
    msg += f"❌ Losses: *{len(losses)}*\n"
    msg += f"➖ Breakeven: *{len(be)}*\n\n"
    msg += f"💹 *Total: {fmt_r(total_r, plain=True)}*\n"
    msg += f"🏆 Best: *{safe_sym(best['symbol'])}* ({fmt_r(best.get('final_r', 0) or 0, plain=True)})\n"
    msg += f"💥 Worst: *{safe_sym(worst['symbol'])}* ({fmt_r(worst.get('final_r', 0) or 0, plain=True)})\n\n"
    msg += "*GRADE PERFORMANCE*\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    for g, (tot, w) in grades.items():
        if tot > 0:
            msg += f"{g}: {w}/{tot} wins ({w / tot * 100:.0f}%)\n"

    # Dynamic threshold context
    dyn = load_json(DYNAMIC_THRESHOLD_FILE, {})
    if dyn:
        msg += f"\n*⚙️ Adaptive Threshold: {dyn.get('threshold', SQS_BASE_THRESHOLD)}*\n"
        msg += f"_{dyn.get('reason', 'baseline')}_\n"

    msg += f"\n⏰ {fmt_time()}"
    return msg


def analyze_single_symbol(symbol: str):
    symbol = symbol.upper().strip()

    # basic validation (IMPORTANT)
    if not symbol or len(symbol) > 10:
        return "❌ Invalid symbol format"

    try:
        tf_cfg = TIMEFRAMES[0]  # use 30m or your default TF

        ctx = {
            'htf_bull': get_htf_bias(symbol),
            'mtf_sum': get_mtf_sum(symbol)
        }

        result, reason = analyze_symbol(
            symbol,
            tf_cfg,
            ctx['htf_bull'],
            ctx['mtf_sum'],
            None
        )

        if not result:
            return f"⚠️ No valid setup found for {symbol}"

        ai_text = None
        if result['sqs'] >= AI_TIER_THRESHOLD and GEMINI_API_KEY:
            ai_text = get_ai_analysis(result)

        result['ai_text'] = ai_text

        return format_new_signal(result, ai_text)

    except Exception as e:
        return f"❌ Error analyzing {symbol}: {str(e)[:50]}"

# ═══════════════════════════════════════════════════════════════════════════════
# §18  TELEGRAM TRANSPORT
# ═══════════════════════════════════════════════════════════════════════════════

def send_telegram(message, silent=False):
    """Send Telegram message, auto-splitting if > 4000 chars."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram credentials missing")
        return False

    if len(message) > 4000:
        parts = []
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
        for i, part in enumerate(parts):
            hdr = f"_(part {i+1}/{len(parts)})_\n" if len(parts) > 1 else ""
            if not _tg_send(hdr + part, silent):
                success = False
            time.sleep(0.3)
        return success
    return _tg_send(message, silent)
# ═══════════════════════════════════════════════════════════════════════════════
# New addition > telegram analysis >  CORRELATION DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════
def _tg_send(message, silent=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown',
            'disable_notification': silent,
            'disable_web_page_preview': True,
        }, timeout=10)
        if r.status_code != 200:
            logging.error(f"Telegram {r.status_code}: {r.text[:200]}")
            # Retry without Markdown if parse error
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


# ═══════════════════════════════════════════════════════════════════════════════
# §19  CORRELATION DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════

def format_correlation_alert(new_sigs, open_trades):
    combined = [(s['symbol'], 'new') for s in new_sigs]
    if open_trades:
        for k, t in open_trades.items():
            if not t.get('closed'):
                combined.append((t['symbol'], 'open'))

    risk = {}
    for grp, syms in CORRELATION_GROUPS.items():
        matching = [(s, src) for s, src in combined if s in syms]
        if not matching:
            continue
        seen = {}
        for s, src in matching:
            if s not in seen or src == 'open':
                seen[s] = src
        if len(seen) >= 2:
            risk[grp] = {
                'symbols': sorted(seen.keys()),
                'new': sum(1 for v in seen.values() if v == 'new'),
                'open': sum(1 for v in seen.values() if v == 'open'),
            }

    if not risk:
        return None

    msg = "⚠️ *CORRELATION NOTICE*\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += "_Multiple signals/positions in same sector_\n"
    for grp, d in risk.items():
        parts = []
        if d['new']: parts.append(f"{d['new']} new")
        if d['open']: parts.append(f"{d['open']} open")
        tag = f" ({', '.join(parts)})" if parts else ""
        safe_list = ', '.join(safe_sym(s) for s in d['symbols'])
        msg += f"\n🔗 *{grp}*{tag}\n  {safe_list}\n"
    msg += "\n💡 _These often move together — manage overall exposure._"
    return msg


# ═══════════════════════════════════════════════════════════════════════════════
# §20  MAIN ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════════

def should_send_weekly_summary():
    state = load_json(STATE_FILE, {})
    now = now_est()
    if now.weekday() != 6 or now.hour < 21:
        return False
    week_key = now.strftime('%Y-W%W')
    if state.get('last_weekly') == week_key:
        return False
    state['last_weekly'] = week_key
    save_json(STATE_FILE, state)
    return True

def is_signal_expired(sig):
    try:
        exp = datetime.fromisoformat(sig['expiry_time'])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=EST)
        return now_est() > exp
    except Exception:
        return False

def main():
    session = get_session()
    active_list = get_active_watchlist()

    # Compute/refresh dynamic threshold once per run
    if SQS_DYNAMIC_ENABLED:
        compute_dynamic_threshold()
    eff_threshold = get_effective_threshold()

    print(f"\n{'=' * 70}")
    print(f"AlphaEdge Scanner v7.0 @ {fmt_datetime()}")
    print(f"Session: {session}")
    print(f"Watchlist: {len(active_list)}/{len(ALL_SYMBOLS)} symbols ({U.summary()})")
    print(f"AI: {bool(GEMINI_API_KEY)} | Threshold: {eff_threshold} (base {SQS_BASE_THRESHOLD}) | Grade: {GRADE_FILTER}")

    # VIX regime at top
    vix = get_vix_regime()
    if vix:
        print(f"VIX: {vix['vix']} ({vix['regime']}) | Blocks longs: {vix['blocks_longs']}")
    print(f"{'=' * 70}\n")

    logging.info(f"Scan v7.0 | Session: {session} | Active: {len(active_list)} | "
                 f"Threshold: {eff_threshold}")

    cache = load_cache()   # auto-cleaned
    trades = load_json(TRADES_FILE, {})

    # ─── STEP 1: Check active trades ───
    if trades:
        print(f"📊 Checking {len(trades)} active trade(s)...")
        to_remove = []
        for tk, trade in list(trades.items()):
            if trade.get('closed'):
                archive_trade(trade)
                to_remove.append(tk)
                continue
            try:
                sym = trade['symbol']
                print(f"  → {sym:10s} [{trade.get('tf_label', trade['tf']):5s}] ({trade['signal']})...", end=" ")
                events, closed = check_trade_progress(trade)
                if not events:
                    print("no change")
                    continue
                live = get_live_ohlc(sym)
                current = live[0] if live else trade['entry']
                for event in events:
                    msg = format_trade_event(trade, event, current)
                    if msg:
                        send_telegram(msg, silent=False)
                        print(f"\n     🔔 {event['type']} @ ${event['price']}", end="")
                        logging.info(f"{sym} {event['type']} @ {event['price']}")
                if closed:
                    archive_trade(trade)
                    to_remove.append(tk)
                    print(" ✅ closed")
                else:
                    print()
            except Exception as e:
                print(f"💥 error: {e}")
                logging.error(f"Trade check {trade.get('symbol')}: {e}")
            time.sleep(FETCH_DELAY)

        for k in to_remove:
            del trades[k]
        save_json(TRADES_FILE, trades)
        print()

        # Open positions summary (every 2h, 2+ positions)
        remaining = {k: v for k, v in trades.items() if not v.get('closed')}
        if len(remaining) >= 2:
            if can_alert_key('last_pos_summary', POSITION_SUMMARY_HOURS):
                ps = format_open_positions_summary(remaining)
                if ps:
                    send_telegram(ps, silent=True)

    # ─── STEP 2: Scan new signals ───
    print(f"🔍 Scanning {len(active_list)} symbols...")
    new_sigs = []
    skip_dupe = skip_active = ai_calls = 0
    near_misses = []

    # Pre-fetch HTF + MTF (one pass)
    symbol_context = {}
    print("  📡 Pre-fetching HTF/MTF context...")
    for sym in active_list:
        try:
            htf_bull = get_htf_bias(sym)
            mtf_sum = get_mtf_sum(sym)
            symbol_context[sym] = {'htf_bull': htf_bull, 'mtf_sum': mtf_sum}
            time.sleep(FETCH_DELAY)
        except Exception as e:
            logging.error(f"Context {sym}: {e}")
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

            last_sig_info = get_last_signal_info(cache, sym, tf)

            try:
                result, reason = analyze_symbol(
                    sym, tf_cfg, ctx['htf_bull'], ctx['mtf_sum'], last_sig_info
                )
            except Exception as e:
                print(f"💥 {e}")
                logging.error(f"Analyze {sym} {tf}: {e}")
                continue
            time.sleep(FETCH_DELAY)

            if not result:
                if DEBUG_NEAR_MISS and reason:
                    print(f"⚪ {reason}")
                    near_misses.append(f"{sym} [{label}] {reason}")
                else:
                    print("—")
                continue

            if is_signal_expired(result):
                print("⏰ expired")
                continue

            sig_key = f"{result['signal']}_{tf}"
            if is_duplicate(sym, sig_key, cache, result['sqs']):
                skip_dupe += 1
                print("🔕 cooldown")
                continue

            # Record SQS for trending (v7.0)
            record_sqs(sym, result['sqs'])

            ai_text = None
            if result['sqs'] >= AI_TIER_THRESHOLD and GEMINI_API_KEY:
                print("🤖", end=" ")
                ai_text = get_ai_analysis(result)
                if ai_text:
                    ai_calls += 1
            result['ai_text'] = ai_text

            new_sigs.append(result)
            mark_sent(sym, sig_key, cache)
            save_signal_info(cache, sym, tf, result['signal'], result['price'], result['atr'])
            trades[active_key] = create_trade(result)
            print(f"🚨 {result['tier']} {result['signal']} SQS={result['sqs']}")
            logging.info(f"SIGNAL: {sym} {tf} {result['signal']} "
                         f"SQS={result['sqs']} MTF={result.get('mtf_sum')}")

    # ─── STEP 3: Deliver alerts ───
    if new_sigs:
        new_sigs.sort(key=lambda s: s['sqs'], reverse=True)
        if len(new_sigs) >= DIGEST_THRESHOLD:
            send_telegram(format_digest(new_sigs), silent=False)
            print("📦 Sent digest")
            # Full details for signals at/above threshold
            hq = [s for s in new_sigs if s['sqs'] >= eff_threshold]
            if hq:
                print(f"  Sending {len(hq)} full details...")
                for sig in hq:
                    send_telegram(format_new_signal(sig, sig.get('ai_text')), silent=False)
            corr = format_correlation_alert(new_sigs, trades)
            if corr:
                send_telegram(corr, silent=True)
        else:
            for sig in new_sigs:
                silent = 'FAIR' in sig['tier'] or 'LOW' in sig['tier']
                send_telegram(format_new_signal(sig, sig.get('ai_text')), silent=silent)
            print(f"📨 Sent {len(new_sigs)} alert(s)")
            corr = format_correlation_alert(new_sigs, trades)
            if corr:
                send_telegram(corr, silent=True)

    save_json(ALERT_CACHE, cache)
    save_json(TRADES_FILE, trades)

    # Weekly summary (Sun 9 PM)
    if should_send_weekly_summary():
        print("📊 Sending weekly summary...")
        ws = format_weekly_summary()
        if ws:
            send_telegram(ws, silent=False)

    # ─── Final report ───
    print(f"\n{'=' * 70}")
    print(f"✅ New: {len(new_sigs)} | 🔕 Cooldown: {skip_dupe} | 🔒 Active: {skip_active}")
    print(f"⚪ Near-miss: {len(near_misses)} | 🤖 AI: {ai_calls} | 📊 Open: {len(trades)}")
    print(f"Session: {session} | Threshold: {eff_threshold}")
    print(f"{'=' * 70}")
    logging.info(f"Scan done | New:{len(new_sigs)} Active:{len(trades)} AI:{ai_calls}")


if __name__ == "__main__":
    main()
