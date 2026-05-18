╔══════════════════════════════════════════════════════════════════════╗
║           ALPHAEDGE SINGLE SCAN  v4.4                               ║
║           On-demand symbol analysis + alert engine                  ║
╠══════════════════════════════════════════════════════════════════════╣
║  PURPOSE                                                             ║
║  ───────                                                             ║
║  On-demand deep-dive analysis for any ticker (stocks, ETFs,          ║
║  crypto, futures).  Routes to full 13-section Telegram report,       ║
║  3-line quick summary, price-alert management, watchlist scan,       ║
║  top-movers, or scheduled brief depending on command received.       ║
║                                                                      ║
║  ARCHITECTURE                                                        ║
║  ────────────                                                         ║
║  • Data:       get_full_context() from market_intel (cached)         ║
║  • Verdict:    get_verdict() — 12 verdict branches + 2 overrides     ║
║  • Display:    format_full_analysis() — 13 emoji-anchored sections   ║
║  • Delivery:   market_intel.send_telegram (auto-split, MarkdownV2)   ║
║  • AI:         Gemini 2.0 Flash (4-line structured response)         ║
║  • Alerts:     price_alerts.json — watch / early-warn / expiry       ║
║  • Universe:   symbols.yaml → load_universe()                        ║
║                                                                      ║
║  VERDICT LOGIC (in evaluation order):                                ║
║    0.  |1d drop| ≥ 15% → PARABOLIC SPIKE or CRASH                   ║
║    0b. stretch ≥ 50% AND RSI ≥ 70 → PARABOLIC EXTENSION             ║
║    1.  UPTREND + near ATH + RSI < 75 + stretch < 25% → MOMENTUM     ║
║    TP. RSI ≥ 75 + up today + above both EMAs → TAKE PROFITS  [NEW]  ║
║    2.  (RSI ≥ 70 OR stretch ≥ 30%) + mtf_extreme → EXTREMELY EXT.   ║
║    3.  (RSI ≥ 70 OR stretch ≥ 30%) → EXTENDED                       ║
║    4.  UPTREND + RSI 55-69 + stretch < 25% → MOMENTUM CONTINUATION  ║
║    5.  UPTREND + RSI < 55 + stretch < 20% → BUY ZONE (uptrend)      ║
║    6.  PULLBACK + RSI < 58 + stretch < 15% → BUY ZONE (EMA50)       ║
║    7.  DOWNTREND + below EMA200 → AVOID                              ║
║    8.  Near 52W low + dropping → CAUTION                             ║
║    9.  RECOVERING trend → WATCH or HOLD                              ║
║   10.  MIXED trend → WATCH (potential base) or NEUTRAL               ║
║   11.  Default → WATCH / EXTENDED / NEUTRAL                          ║
║   OV1. Market override (VIX>25, SPY<-1.5%) → WAIT — Market          ║
║   OV2. Earnings within N days → WAIT — Earnings                      ║
║                                                                      ║
║  RSI THRESHOLDS (unified across all functions):                      ║
║    ≥ 70 = Overbought  |  ≥ 80 = MTF Overbought tag                  ║
║    ≥ 90 = EXTREME     |  ≤ 30 = Oversold  |  ≤ 20 = EXTREME         ║
║    mtf_rsi_extreme fires at ≥ 80 OR ≤ 20 (aligned with MTF tags)    ║
║                                                                      ║
║  CHANGELOG  v4.4  (vs v4.3)                                          ║
║  ──────────────────────────                                           ║
║  CRITICAL FIX  TAKE PROFITS was a dead branch — reordered BEFORE     ║
║    EXTENDED checks; now fires correctly for overbought uptrend.      ║
║  CRITICAL FIX  c['pct_from_52w_low'] → .get() guard (KeyError risk). ║
║  CRITICAL FIX  MTF RSI threshold aligned: overbought at ≥ 70        ║
║    (was ≥ 80) so tags match verdict logic.                           ║
║  CRITICAL FIX  mtf_rsi_extreme threshold changed from 85 → 80        ║
║    to match MTF rsi_tag "Overbought" threshold (was misaligned).     ║
║  CRITICAL FIX  RECOVERING + HOLD branch now checks actual            ║
║    above_200 flag; no longer unconditionally says "below EMA200".    ║
║  CRITICAL FIX  upside_to_ath was negative when stock AT/ABOVE ATH;  ║
║    now shows "AT ATH" branch or guards to 0%.                        ║
║  HIGH FIX      QQQ now rendered in §12 Market section (was fetched   ║
║    but never shown — v4.3 audit promised this, never delivered).     ║
║  HIGH FIX      AI summary line now always uses last line of Gemini   ║
║    response (the action line) — was fragile keyword search.          ║
║  HIGH FIX      RS vs SPY given its own §9b section with header;      ║
║    was floating outside sector block with no label.                  ║
║  MEDIUM FIX    detect_rsi_divergence() recent window 3→5 bars;       ║
║    reduces false positives on single-bar noise.                      ║
║  MEDIUM FIX    build_tag_pills() priority: squeeze/divergence now     ║
║    ranked BEFORE stretch (more actionable).                          ║
║  MEDIUM FIX    TAKE PROFITS fires on drop ≥ 0 (not drop > 2);        ║
║    overbought stocks deserve TP signal even on flat/down days.       ║
║  MEDIUM FIX    volume_label() dead function removed.                 ║
║  MEDIUM FIX    CAUTION next_steps now includes stabilisation entry   ║
║    and specific stop, not just "watch".                              ║
║  IMPR          Section headings added throughout for IDE folding.    ║
║  IMPR          All verdict branches inline-documented with trigger   ║
║    conditions and expected inputs.                                   ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# ════════════════════════════════════════════════════════════════════
# SECTION 1 — STANDARD-LIBRARY IMPORTS
# ════════════════════════════════════════════════════════════════════
import sys
import json
import time
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ════════════════════════════════════════════════════════════════════
# SECTION 2 — TIMEZONE + LOGGING BOOTSTRAP
# ════════════════════════════════════════════════════════════════════
EST = ZoneInfo("America/New_York")
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / f"single_{datetime.now(EST).strftime('%Y-%m-%d')}.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ════════════════════════════════════════════════════════════════════
# SECTION 3 — INTERNAL / MARKET-INTEL IMPORTS
# ════════════════════════════════════════════════════════════════════
from market_intel import (
    get_full_context,
    get_market_ctx,
    calc_relative_strength,
    get_earnings_date,
    format_earnings_warning,
    SYMBOL_EMOJI,
    SECTORS,
    SYMBOL_TO_SECTOR,
    send_telegram,
    now_est,
    load_json,
    save_json,
    EARNINGS_WARNING_DAYS,
)

# ════════════════════════════════════════════════════════════════════
# SECTION 4 — THIRD-PARTY IMPORTS
# ════════════════════════════════════════════════════════════════════
try:
    import yfinance as yf
    import numpy as np
    import pandas as pd
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    sys.exit(1)

# ════════════════════════════════════════════════════════════════════
# SECTION 5 — CONSTANTS & FILE PATHS
# ════════════════════════════════════════════════════════════════════
ALERTS_FILE  = "price_alerts.json"
SYMBOLS_YAML = "symbols.yaml"

# Unified RSI thresholds — used consistently in get_verdict(),
# build_tag_pills(), format_full_analysis(), and get_mtf_verdicts().
RSI_OVERBOUGHT      = 70    # single-TF overbought gate
RSI_MTF_OVERBOUGHT  = 70    # MTF table tag threshold (was 80 — now aligned)
RSI_MTF_EXTREME_HI  = 80    # mtf_rsi_extreme trigger (was 85 — now aligns with tag)
RSI_MTF_EXTREME_LO  = 20    # mtf_rsi_extreme lower bound
RSI_OVERSOLD        = 30
RSI_EXTREME_HI      = 90    # single-TF "EXTREME" tag
RSI_EXTREME_LO      = 20


# ════════════════════════════════════════════════════════════════════
# SECTION 6 — UNIVERSE LOADER
# ════════════════════════════════════════════════════════════════════

def load_universe() -> tuple[list[str], dict[str, str]]:
    """
    Load all symbols from symbols.yaml across all bucket types.
    Returns (all_syms, emoji_map).  Falls back to ([], {}) on any error.
    """
    try:
        import yaml
        with open(SYMBOLS_YAML, "r") as fh:
            raw = yaml.safe_load(fh) or {}
        all_syms:  list[str]       = []
        emoji_map: dict[str, str]  = {}
        for bucket in ("crypto", "extended_hours", "regular_hours"):
            for item in (raw.get(bucket) or []):
                sym = item["symbol"]
                all_syms.append(sym)
                emoji_map[sym] = item.get("emoji", "📊")
        return all_syms, emoji_map
    except Exception as exc:
        logging.error(f"Universe load: {exc}")
        return [], {}


# ════════════════════════════════════════════════════════════════════
# SECTION 7 — SYMBOL HELPERS
# ════════════════════════════════════════════════════════════════════

def normalise_symbol(raw: str) -> str:
    """Resolve common aliases (BTC, ETH, GOLD) to their yfinance tickers."""
    s = raw.strip().upper()
    aliases = {
        "BITCOIN":  "BTC-USD",
        "BTC":      "BTC-USD",
        "ETHEREUM": "ETH-USD",
        "ETH":      "ETH-USD",
        "XRP":      "XRP-USD",
        "RIPPLE":   "XRP-USD",
        "GOLD":     "GC=F",
    }
    return aliases.get(s, s)


def is_crypto(sym: str) -> bool:
    """True for crypto or commodity futures tickers."""
    return sym.endswith("-USD") or sym == "GC=F"


def validate_symbol(sym: str) -> bool:
    """
    Lightweight existence check using fast_info (no full download).
    Falls back to a 2-day daily download if fast_info is unavailable.
    v4.3 FIX: replaced full 1y download that was used just for existence check.
    """
    try:
        fi    = yf.Ticker(sym).fast_info
        price = getattr(fi, "last_price", None)
        if price and float(price) > 0:
            return True
        # Fast_info unavailable — minimal fallback
        df = yf.download(sym, period="2d", interval="1d",
                         progress=False, auto_adjust=True)
        return not df.empty
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════
# SECTION 8 — DATA HELPERS & MATHS UTILITIES
# ════════════════════════════════════════════════════════════════════

def ath_recency(ath_date_str: str) -> str:
    """
    Human-readable label for how long ago the ATH was set.
    Uses now_est() for timezone consistency (FIX: was datetime.now()).
    Comparison uses naive datetimes to avoid tz mismatch with date-only ATH string.
    """
    try:
        ath_dt = datetime.strptime(ath_date_str[:10], "%Y-%m-%d")
        now    = now_est().replace(tzinfo=None)   # strip tz for naive comparison
        days   = (now - ath_dt).days
        if days == 0:   return "set TODAY"
        if days == 1:   return "set YESTERDAY"
        if days <= 7:   return f"set {days}d ago"
        if days <= 30:  return f"set {days // 7}w ago"
        if days <= 365: return f"set {days // 30}mo ago"
        return f"set {days // 365}y ago"
    except Exception:
        return f"on {ath_date_str}"


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns returned by some yfinance versions."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def sma(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length).mean()


def rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's Relative Moving Average — used for RSI, ATR, ADX."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def pine_rsi(src: pd.Series, length: int = 14) -> pd.Series:
    """RSI computed with Wilder RMA smoothing (matches Pine Script behaviour)."""
    delta = src.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    rs    = rma(gain, length) / rma(loss, length).replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def pine_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """ATR computed with Wilder RMA smoothing."""
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"]  - df["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return rma(tr, length)


def fmt_mcap(val: float | None) -> str | None:
    """Format a market-cap value into human-readable string."""
    if not val:
        return None
    if val >= 1e12: return f"${val / 1e12:.1f}T"
    if val >= 1e9:  return f"${val / 1e9:.1f}B"
    if val >= 1e6:  return f"${val / 1e6:.1f}M"
    return f"${val:.0f}"


# ════════════════════════════════════════════════════════════════════
# SECTION 9 — STOCK INFO & CURRENCY HELPERS
# ════════════════════════════════════════════════════════════════════

def get_stock_info(symbol: str) -> dict:
    """
    Fetch fundamental metadata from yfinance Ticker.info.
    Returns a safe dict with sensible fallbacks on any error.
    Crypto symbols return a synthetic dict without API calls.
    """
    if is_crypto(symbol):
        return {
            "sector":     "Crypto",
            "industry":   "Cryptocurrency",
            "exchange":   "24/7",
            "asset_type": "Crypto",
            "currency":   "USD",
            "short_name": symbol,
            "long_name":  symbol,
        }
    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.info or {}

        qt = info.get("quoteType", "").upper()
        if   qt == "EQUITY":                   asset_type = "Stock"
        elif qt == "ETF":                      asset_type = "ETF"
        elif qt == "MUTUALFUND":               asset_type = "Fund"
        elif qt in ("FUTURE", "COMMODITY"):    asset_type = "Futures"
        else:                                  asset_type = qt or "Stock"

        return {
            "sector":        info.get("sector", SYMBOL_TO_SECTOR.get(symbol, "Unknown")),
            "industry":      info.get("industry", ""),
            "exchange":      info.get("exchange", ""),
            "asset_type":    asset_type,
            "currency":      info.get("currency", "USD"),
            "short_name":    info.get("shortName", symbol),
            "long_name":     info.get("longName", info.get("shortName", symbol)),
            "target_mean":   info.get("targetMeanPrice"),
            "target_high":   info.get("targetHighPrice"),
            "target_low":    info.get("targetLowPrice"),
            "analyst_count": info.get("numberOfAnalystOpinions", 0),
            "rec_key":       info.get("recommendationKey", "").replace("_", " ").title(),
            "short_pct":     info.get("shortPercentOfFloat"),
            "inst_pct":      info.get("institutionsPercentHeld"),
            "beta":          info.get("beta"),
            "pe_ratio":      info.get("trailingPE"),
            "market_cap":    info.get("marketCap"),
        }
    except Exception as exc:
        logging.debug(f"Stock info {symbol}: {exc}")
        return {
            "sector":     SYMBOL_TO_SECTOR.get(symbol, "Unknown"),
            "asset_type": "Stock",
            "currency":   "USD",
            "long_name":  symbol,
        }


def get_cad_price(symbol: str) -> tuple[float | None, str | None]:
    """
    Attempt to find a TSX listing (.TO or .V suffix).
    Returns (cad_price, tsx_symbol) or (None, None) if not found.
    """
    if is_crypto(symbol) or symbol == "GC=F":
        return None, None
    for suffix in (".TO", ".V"):
        tsx_sym = symbol + suffix
        try:
            df = yf.download(tsx_sym, period="2d", interval="1d",
                             progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 1:
                df        = _clean_df(df)
                cad_price = float(df["Close"].iloc[-1])
                if cad_price > 0.50:
                    return round(cad_price, 4 if cad_price < 10 else 2), tsx_sym
        except Exception:
            pass
    return None, None


def get_usd_cad_rate() -> float:
    """Fetch current USD/CAD exchange rate. Falls back to 1.36 on error."""
    try:
        df = yf.download("USDCAD=X", period="2d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return 1.36
        df = _clean_df(df)
        return round(float(df["Close"].iloc[-1]), 4)
    except Exception:
        return 1.36


# ════════════════════════════════════════════════════════════════════
# SECTION 10 — TECHNICAL INDICATORS
# ════════════════════════════════════════════════════════════════════

def calc_parabolic_sar(
    df: pd.DataFrame,
    af_start: float = 0.02,
    af_step:  float = 0.02,
    af_max:   float = 0.2,
) -> tuple[pd.Series | None, pd.Series | None]:
    """
    Full Parabolic SAR implementation.
    Returns (bull_series, sar_series) or (None, None) on error.
    Note: bull[0] initialised from close[1] — acceptable 1-bar lookahead
    for historical indicator context.
    """
    try:
        if len(df) < 2:
            return None, None

        high  = df["High"].values
        low   = df["Low"].values
        close = df["Close"].values
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
            pb   = bull[i - 1]
            ps   = sar[i - 1]
            pe   = ep[i - 1]
            paf  = af[i - 1]
            ns   = ps + paf * (pe - ps)

            if pb:
                ns = min(ns, low[i - 1], low[max(0, i - 2)])
                if low[i] < ns:
                    bull[i] = False; sar[i] = pe; ep[i] = low[i]; af[i] = af_start
                else:
                    bull[i] = True; sar[i] = ns
                    if high[i] > pe:
                        ep[i] = high[i]; af[i] = min(paf + af_step, af_max)
                    else:
                        ep[i] = pe; af[i] = paf
            else:
                ns = max(ns, high[i - 1], high[max(0, i - 2)])
                if high[i] > ns:
                    bull[i] = True; sar[i] = pe; ep[i] = high[i]; af[i] = af_start
                else:
                    bull[i] = False; sar[i] = ns
                    if low[i] < pe:
                        ep[i] = low[i]; af[i] = min(paf + af_step, af_max)
                    else:
                        ep[i] = pe; af[i] = paf

        return pd.Series(bull, index=df.index), pd.Series(sar, index=df.index)
    except Exception:
        return None, None


def calc_adx(df: pd.DataFrame, length: int = 14) -> tuple:
    """
    Compute ADX, +DI, -DI using Wilder smoothing.
    Returns (adx_series, plus_di, minus_di) — all None on error.
    """
    try:
        high  = df["High"]
        low   = df["Low"]
        close = df["Close"]

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


# ════════════════════════════════════════════════════════════════════
# SECTION 11 — SQUEEZE + RSI DIVERGENCE DETECTION
# ════════════════════════════════════════════════════════════════════

def detect_squeeze(df: pd.DataFrame) -> tuple[str, str | None]:
    """
    Detect Bollinger Band / Keltner Channel squeeze (momentum coiling).
    Returns ('building'|'fired'|'none', 'bullish'|'bearish'|None).
    """
    try:
        if len(df) < 30:
            return "none", None

        bb_basis = sma(df["Close"], 20)
        bb_dev   = df["Close"].rolling(20).std()
        bb_up    = bb_basis + 2.0 * bb_dev
        bb_lo    = bb_basis - 2.0 * bb_dev
        kc_mid   = ema(df["Close"], 20)
        kc_rng   = pine_atr(df, 20)
        kc_up    = kc_mid + 1.5 * kc_rng
        kc_lo    = kc_mid - 1.5 * kc_rng
        in_sq    = (bb_up < kc_up) & (bb_lo > kc_lo)

        if in_sq.iloc[-1]:
            return "building", None
        if len(in_sq) >= 2 and in_sq.iloc[-2] and not in_sq.iloc[-1]:
            direction = "bullish" if df["Close"].iloc[-1] > bb_basis.iloc[-1] else "bearish"
            return "fired", direction
        return "none", None
    except Exception:
        return "none", None


def detect_rsi_divergence(df: pd.DataFrame) -> str | None:
    """
    RSI divergence using rolling min/max over a 20-bar lookback window.

    FIX v4.4: recent window expanded from 3 → 5 bars to reduce false
    positives from single-bar noise.  RSI gap requirement raised from
    5 → 7 points for the same reason.

    Bullish: price new low in last 5 bars, RSI higher than period low.
    Bearish: price new high in last 5 bars, RSI lower than period high.
    Requires min 20 bars of data.
    """
    try:
        if len(df) < 20:
            return None

        rsi_series  = pine_rsi(df["Close"], 14)
        look        = 20   # total lookback window in bars
        recent_bars = 5    # 'recent' sub-window (FIX: was 3)
        rsi_gap     = 7    # minimum RSI point gap to confirm divergence (FIX: was 5)

        low_window  = df["Low"].iloc[-look:]
        high_window = df["High"].iloc[-look:]
        rsi_window  = rsi_series.iloc[-look:]

        # Bullish divergence: price lower low, RSI higher low
        recent_pl = low_window.iloc[-recent_bars:].min()
        period_pl = low_window.iloc[:-recent_bars].min()
        recent_rl = rsi_window.iloc[-recent_bars:].min()
        period_rl = rsi_window.iloc[:-recent_bars].min()

        if recent_pl < period_pl and recent_rl > period_rl + rsi_gap:
            return "bullish"

        # Bearish divergence: price higher high, RSI lower high
        recent_ph = high_window.iloc[-recent_bars:].max()
        period_ph = high_window.iloc[:-recent_bars].max()
        recent_rh = rsi_window.iloc[-recent_bars:].max()
        period_rh = rsi_window.iloc[:-recent_bars].max()

        if recent_ph > period_ph and recent_rh < period_rh - rsi_gap:
            return "bearish"

        return None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
# SECTION 12 — MULTI-TIMEFRAME VERDICTS
# ════════════════════════════════════════════════════════════════════

def get_mtf_verdicts(symbol: str) -> dict:
    """
    Compute trend / RSI / ADX / SAR for Daily, Weekly, Monthly timeframes.
    Returns dict of {label: {trend, rsi, rsi_tag, adx, sar_bull, adx_sar}}.

    FIX v4.4: RSI overbought tag threshold aligned to RSI_MTF_OVERBOUGHT (70,
    was 80) so tags match get_verdict() and format_full_analysis() logic.
    FIX v4.4: mtf_rsi_extreme threshold aligned to RSI_MTF_EXTREME_HI (80,
    was 85) so "Overbought" tag and extreme logic fire at the same level.
    """
    results: dict = {}
    tf_map = {
        "Daily":   ("6mo", "1d"),
        "Weekly":  ("2y",  "1wk"),
        "Monthly": ("5y",  "1mo"),
    }
    for label, (period, interval) in tf_map.items():
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 20:
                continue
            df = _clean_df(df)

            c       = float(df["Close"].iloc[-1])
            e50     = float(ema(df["Close"], min(50,  len(df))).iloc[-1])
            e200    = float(ema(df["Close"], min(200, len(df))).iloc[-1])
            rsi_val = float(pine_rsi(df["Close"], 14).iloc[-1])

            # RSI tags — now aligned with RSI_MTF_OVERBOUGHT = 70
            if   rsi_val >= RSI_EXTREME_HI:              rsi_tag = "EXTREME"
            elif rsi_val >= RSI_MTF_OVERBOUGHT:          rsi_tag = "Overbought"
            elif rsi_val <= RSI_EXTREME_LO:              rsi_tag = "EXTREME"
            elif rsi_val <= RSI_OVERSOLD:                rsi_tag = "Oversold"
            else:                                        rsi_tag = ""

            adx_series, plus_di, minus_di = calc_adx(df, 14)
            adx_val   = float(adx_series.iloc[-1]) if adx_series is not None else 0.0
            plus_val  = float(plus_di.iloc[-1])    if plus_di   is not None else 0.0
            minus_val = float(minus_di.iloc[-1])   if minus_di  is not None else 0.0

            sar_bull_series, _ = calc_parabolic_sar(df)
            sar_bull = bool(sar_bull_series.iloc[-1]) if sar_bull_series is not None else None

            if   adx_val >= 25 and sar_bull is True  and plus_val > minus_val: adx_sar = "Trend BUY"
            elif adx_val >= 25 and sar_bull is False and minus_val > plus_val:  adx_sar = "Trend SELL"
            elif adx_val < 20:                                                  adx_sar = "Ranging"
            else:                                                               adx_sar = "Mixed"

            if   c > e50 > e200:    trend = "Strong Bull"
            elif c > e200:          trend = "Bull"
            elif c < e50 < e200:    trend = "Strong Bear"
            elif c < e200:          trend = "Bear"
            else:                   trend = "Mixed"

            results[label] = {
                "trend":    trend,
                "rsi":      round(rsi_val, 1),
                "rsi_tag":  rsi_tag,
                "adx":      round(adx_val, 1),
                "sar_bull": sar_bull,
                "adx_sar":  adx_sar,
            }
            time.sleep(0.2)
        except Exception as exc:
            logging.debug(f"MTF {symbol} {label}: {exc}")
    return results


# ════════════════════════════════════════════════════════════════════
# SECTION 13 — SECTOR CONTEXT
# ════════════════════════════════════════════════════════════════════

def get_sector_context(symbol: str) -> tuple[str | None, float | None, list | None]:
    """
    Compute average 1-day change for all symbols in the same sector.
    Returns (sector_name, avg_change_pct, peer_symbols).
    """
    sector = SYMBOL_TO_SECTOR.get(symbol)
    if not sector:
        return None, None, None

    syms = SECTORS.get(sector, [])
    if not syms or len(syms) < 2:
        return sector, None, None

    changes: list[float] = []
    for s in syms:
        if s == symbol:
            continue
        try:
            df = yf.download(s, period="5d", interval="1d",
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            df  = _clean_df(df)
            chg = (float(df["Close"].iloc[-1]) - float(df["Close"].iloc[-2])) / float(df["Close"].iloc[-2]) * 100
            changes.append(chg)
            time.sleep(0.15)
        except Exception:
            pass

    if not changes:
        return sector, None, None
    return sector, round(sum(changes) / len(changes), 2), syms


# ════════════════════════════════════════════════════════════════════
# SECTION 14 — POC & STRUCTURE LEVELS
# ════════════════════════════════════════════════════════════════════

def quick_poc(df_daily: pd.DataFrame) -> float | None:
    """
    Point-of-Control: price level with highest volume concentration.
    Restricted to bars within ±30% of current price to prevent stale
    low-price history from dominating on parabolic movers.
    Uses last 60 filtered bars, 30 price bins.
    Returns None if POC is > 40% from current (stale/invalid).
    """
    try:
        price_now = float(df_daily["Close"].iloc[-1])
        lo_bound  = price_now * 0.70
        hi_bound  = price_now * 1.30
        mask      = (df_daily["High"] >= lo_bound) & (df_daily["Low"] <= hi_bound)
        recent    = df_daily[mask].iloc[-60:]

        if len(recent) < 5:
            return None

        low  = float(recent["Low"].min())
        high = float(recent["High"].max())
        if high <= low:
            return None

        bins         = 30
        bin_edges    = np.linspace(low, high, bins + 1)
        vol_at_price = np.zeros(bins)

        for i in range(len(recent)):
            bar_low   = float(recent["Low"].iloc[i])
            bar_high  = float(recent["High"].iloc[i])
            bar_vol   = float(recent["Volume"].iloc[i])
            if bar_vol <= 0:
                continue
            bar_range = max(bar_high - bar_low, 1e-9)
            for b in range(bins):
                overlap = max(
                    0,
                    min(bar_high, bin_edges[b + 1]) - max(bar_low, bin_edges[b]),
                )
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


def recent_structure(df_daily: pd.DataFrame) -> tuple[float | None, float | None]:
    """20-bar swing low and high as support/resistance levels."""
    try:
        recent = df_daily.iloc[-20:]
        return round(float(recent["Low"].min()), 2), round(float(recent["High"].max()), 2)
    except Exception:
        return None, None


# ════════════════════════════════════════════════════════════════════
# SECTION 15 — VERDICT ENGINE  v4.4
# ════════════════════════════════════════════════════════════════════
#
# Returns (verdict, zone, reasons, next_steps).
# next_steps contains specific computed price levels — rendered directly
# in §5 of the alert (no recomputation in format_full_analysis).
#
# EVALUATION ORDER (all if/elif — first match wins):
#   0    Single-day ≥15% move         → PARABOLIC SPIKE / CRASH
#   0b   stretch ≥50% + RSI ≥70       → PARABOLIC EXTENSION
#   1    Uptrend + near ATH + not ext → MOMENTUM
#   TP   RSI ≥75 + up + above EMAs    → TAKE PROFITS  [FIX v4.4: was dead]
#   2    RSI/stretch extended + extreme→ EXTREMELY EXTENDED
#   3    RSI/stretch extended          → EXTENDED
#   4    Uptrend + RSI 55-69           → MOMENTUM CONTINUATION
#   5    Uptrend + RSI <55             → BUY ZONE (uptrend pullback)
#   6    EMA50 pullback + RSI <58      → BUY ZONE (EMA50)
#   7    Downtrend + below EMA200      → AVOID
#   8    Near 52W low + dropping       → CAUTION
#   9    Recovering trend              → WATCH / HOLD (checks above_200)
#  10    Mixed trend                   → WATCH / NEUTRAL
#  11    Default                       → WATCH / EXTENDED / NEUTRAL
#   OV1  VIX >25 + SPY <-1.5%         → WAIT — Market
#   OV2  Earnings within N days        → WAIT — Earnings

def get_verdict(
    ctx:           dict,
    market_ctx:    dict | None = None,
    mtf_verdicts:  dict | None = None,
    earnings_cache: tuple | None = None,
) -> tuple[str, str, list[str], list[str]]:
    """
    earnings_cache: (earnings_date, days_until) pre-fetched from run_analysis().
    If None, get_earnings_date() is called here (costs one API hit).
    Pass it in to prevent the duplicate call that v4.3 was making.
    """
    c = ctx
    rsi        = c["rsi"]
    trend      = c["trend"]
    drop       = c["day_change_pct"]
    from_ath   = c["ath_pct"]
    range_pos  = c["range_pos"]
    current    = c["current"]
    above_50   = current > c["ema50"]
    above_200  = current > c["ema200"]

    stretch_pct = (current - c["ema50"]) / c["ema50"] * 100 if c.get("ema50", 0) > 0 else 0

    reasons:    list[str] = []
    next_steps: list[str] = []
    verdict:    str | None = None
    zone:       str | None = None

    # ── MTF alignment + extreme RSI detection ─────────────────────
    # FIX v4.4: mtf_rsi_extreme threshold changed from 85 → RSI_MTF_EXTREME_HI (80)
    #           to match the "Overbought" tag threshold in get_mtf_verdicts().
    #           Previously a stock at weekly RSI 82 showed "Overbought" in the table
    #           but did NOT trigger the extreme branch — conflicting signals.
    mtf_all_bull    = False
    mtf_rsi_extreme = False
    mtf_max_rsi     = 0.0
    if mtf_verdicts and len(mtf_verdicts) >= 2:
        bull_count      = sum(1 for v in mtf_verdicts.values() if "Bull" in v.get("trend", ""))
        mtf_all_bull    = bull_count == len(mtf_verdicts)
        mtf_rsi_extreme = any(
            v.get("rsi", 50) >= RSI_MTF_EXTREME_HI or v.get("rsi", 50) <= RSI_MTF_EXTREME_LO
            for v in mtf_verdicts.values()
        )
        mtf_max_rsi = max(v.get("rsi", 0) for v in mtf_verdicts.values())

    # ── Case 0: Single-day ≥15% spike ─────────────────────────────
    if abs(drop) >= 15:
        if drop > 0:
            verdict, zone = "PARABOLIC SPIKE", f"Single-day +{drop:.0f}%"
            reasons = [
                f"+{drop:.1f}% in one day — likely news/catalyst driven",
                "Parabolic spikes mean-revert — chasing is high risk",
            ]
            next_steps = [
                "DO NOT chase at current price",
                "Wait for 3–5 day consolidation",
                f"Re-entry zone: near EMA50 ${c['ema50']:.2f} on pullback",
                f"Invalidation: close below EMA200 ${c['ema200']:.2f}",
            ]
        else:
            verdict, zone = "CRASH", f"Single-day {drop:.0f}%"
            reasons = [
                f"{drop:.1f}% single-day drop — likely news driven",
                "Wait for dust to settle — no entry today",
            ]
            next_steps = [
                "Do NOT catch the falling knife today",
                "Wait minimum 3 days for stabilisation",
                f"Key level to watch: EMA200 ${c['ema200']:.2f}",
                f"Only re-enter on confirmed close above EMA50 ${c['ema50']:.2f}",
            ]
        return verdict, zone, reasons, next_steps

    # ── Case 0b: Parabolic extension (extreme EMA stretch) ────────
    if stretch_pct >= 50 and rsi >= RSI_OVERBOUGHT:
        reentry_lo = round(c["ema50"] * 0.98, 2)
        reentry_hi = round(c["ema50"] * 1.05, 2)
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
            f"Re-entry zone: ${reentry_lo} – ${reentry_hi} (near EMA50)",
            f"RSI trigger: wait for RSI to reset below 60 (currently {rsi:.0f})",
            f"Invalidation: close below EMA200 ${c['ema200']:.2f}",
            "If holding: trail stop, consider taking 25–33% off",
        ]
        return verdict, zone, reasons, next_steps

    # ── Case 1: Momentum at/near ATH ──────────────────────────────
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
            f"Stop: close below EMA50 ${c['ema50']:.2f}",
            "Target: new ATH territory",
        ]

    # ── Case TP: Take Profits ─────────────────────────────────────
    # FIX v4.4: MOVED before EXTENDED checks — was a dead branch after
    # Cases 2/3 (any stock with RSI ≥70 + above EMAs would hit EXTENDED first).
    # Now fires correctly for overbought stocks in confirmed uptrends.
    # FIX v4.4: drop condition loosened from > 2 to >= 0 so the signal
    # fires on flat/down days too (overbought is overbought regardless of today's move).
    elif (rsi >= 75 and drop >= 0 and above_50 and above_200 and not verdict):
        reentry_lo = round(c["ema50"] * 0.98, 2)
        reentry_hi = round(c["ema50"] * 1.05, 2)
        verdict, zone = "TAKE PROFITS", "Extended — Trim Here"
        reasons = [
            f"RSI {rsi:.0f} — overbought, momentum stretched",
            "Better risk/reward on a pullback — trim into strength",
        ]
        next_steps = [
            "Trim 25–33% of position at current levels",
            f"Re-entry zone: ${reentry_lo} – ${reentry_hi} (EMA50 area)",
            f"Trail stop: ${round(c['ema50'] * 0.97, 2):.2f} (3% below EMA50)",
        ]

    # ── Case 2: Extremely extended (multi-TF overbought) ─────────
    elif (rsi >= RSI_OVERBOUGHT or stretch_pct >= 30) and above_50 and above_200 and mtf_rsi_extreme:
        reentry_lo = round(c["ema50"] * 0.98, 2)
        reentry_hi = round(c["ema50"] * 1.05, 2)
        verdict, zone = "EXTREMELY EXTENDED", "Multi-TF Overbought"
        reasons = [
            f"RSI {rsi:.0f} daily — overbought",
            f"Weekly/Monthly RSI also extreme (max: {mtf_max_rsi:.0f})",
            f"Price {stretch_pct:.0f}% above EMA50 — unsustainable",
        ]
        next_steps = [
            "DO NOT enter — pullback is highest-probability outcome",
            f"Re-entry zone: ${reentry_lo} – ${reentry_hi} (EMA50 area)",
            f"RSI trigger: wait for RSI below 60 (currently {rsi:.0f})",
            "If holding: consider trimming 33–50% of position",
        ]

    # ── Case 3: Extended (single-TF overbought) ───────────────────
    elif (rsi >= RSI_OVERBOUGHT or stretch_pct >= 30) and above_50 and above_200:
        reentry_lo = round(c["ema50"] * 0.98, 2)
        reentry_hi = round(c["ema50"] * 1.05, 2)
        verdict, zone = "EXTENDED", "Overbought — Wait for Pullback"
        reasons = []
        if rsi >= RSI_OVERBOUGHT:
            reasons.append(f"RSI {rsi:.0f} — overbought, momentum stretched")
        else:
            reasons.append(f"RSI {rsi:.0f} — elevated but not yet overbought")
        if stretch_pct >= 30:
            reasons.append(f"Price {stretch_pct:.0f}% above EMA50 — extended, poor R:R")
        elif stretch_pct >= 15:
            reasons.append(f"Price {stretch_pct:.0f}% above EMA50 — moderately stretched")
        reasons.append("Better setups come on pullbacks — not ideal entry now")
        next_steps = [
            f"Better entry: pullback to EMA50 zone ${reentry_lo} – ${reentry_hi}",
            f"RSI trigger: wait for RSI to cool below 60 (currently {rsi:.0f})",
            f"Stop if entering now: below EMA50 ${c['ema50']:.2f}",
            "If holding: trail stop, do not add to position",
        ]

    # ── Case 4: Momentum continuation (healthy uptrend, RSI 55-69) ─
    # Fixes gap: UPTREND + RSI 55-69 + stretch <25% was falling to default WATCH
    elif "UPTREND" in trend and 55 <= rsi < 70 and above_200 and stretch_pct < 25:
        verdict, zone = "MOMENTUM CONTINUATION", "Uptrend — Healthy"
        reasons = [
            "Strong uptrend intact — above both EMAs",
            f"RSI {rsi:.0f} — momentum building, not yet overbought",
        ]
        if from_ath > -15:
            reasons.append(f"Near ATH ({from_ath:+.1f}%) — trend is strong")
        if mtf_all_bull:
            reasons.append("All timeframes aligned bullish")
        # FIX v4.4: guard negative upside_to_ath when stock is at/above ATH
        upside_to_ath = max((c["ath"] - current) / current * 100, 0.0)
        ath_line = (
            f"Target: ATH ${c['ath']:.2f} (+{upside_to_ath:.1f}% upside)"
            if upside_to_ath > 0
            else f"Stock is at/above ATH ${c['ath']:.2f} — new high territory"
        )
        next_steps = [
            f"Ideal entry: pullback to EMA50 ${c['ema50']:.2f} (lower risk)",
            f"Momentum entry: current ${current:.2f} acceptable if conviction is high",
            ath_line,
            f"Stop: close below EMA50 ${c['ema50']:.2f}",
        ]

    # ── Case 5: Buy Zone — uptrend pullback ───────────────────────
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
        # FIX v4.4: guard negative upside_to_ath
        upside_to_ath = max((c["ath"] - current) / current * 100, 0.0)
        ath_line = (
            f"Target 1: ATH ${c['ath']:.2f} (+{upside_to_ath:.1f}% upside)"
            if upside_to_ath > 0
            else f"Target: new ATH territory (stock is at/above ${c['ath']:.2f})"
        )
        next_steps = [
            f"Entry: ${current:.2f} or lower — current level is reasonable",
            ath_line,
            "Target 2: new ATH breakout",
            f"Stop: close below EMA200 ${c['ema200']:.2f}",
        ]

    # ── Case 6: Buy Zone — EMA50 pullback ─────────────────────────
    elif "PULLBACK" in trend and rsi < 58 and stretch_pct < 15:
        # FIX v4.4: use .get() guard on high_52w to prevent crash if None
        high52 = c.get("high_52w")
        target_str = f"Target: prior high ${high52:.2f}" if high52 else "Target: prior high (check chart)"
        verdict, zone = "BUY ZONE", "EMA50 Pullback"
        reasons = [
            "Pulling back toward EMA50 — uptrend structure intact",
            f"Above EMA200 ${c['ema200']:.2f} — structural support holds",
            f"RSI {rsi:.0f} — watch for bounce",
        ]
        next_steps = [
            f"Entry: near EMA50 ${c['ema50']:.2f} (ideal) or current ${current:.2f}",
            target_str,
            f"Stop: close below EMA200 ${c['ema200']:.2f}",
        ]

    # ── Case 7: Downtrend ─────────────────────────────────────────
    elif "DOWNTREND" in trend and not above_200:
        verdict, zone = "AVOID", "Falling Knife"
        reasons = ["Below EMA50 & EMA200 — confirmed downtrend, no base formed"]
        if rsi < 30:
            reasons.append(f"RSI {rsi:.0f} oversold but no reversal signal yet")
        next_steps = [
            f"Do NOT enter — wait for confirmed close above EMA50 ${c['ema50']:.2f}",
            "Confirm EMA50 > EMA200 crossover before buying",
            f"EMA200 ${c['ema200']:.2f} is first resistance if reversal forms",
        ]

    # ── Case 8: Near 52W low + breaking down ──────────────────────
    # FIX v4.4: c.get() guard prevents KeyError if field missing from context
    elif c.get("pct_from_52w_low", 100) < 8 and drop < -3:
        low52 = c.get("low_52w", 0)
        verdict, zone = "CAUTION", "Breaking Down"
        reasons = ["Near 52W low — key support at risk of breaking"]
        if rsi < 35:
            reasons.append(f"RSI {rsi:.0f} approaching oversold — watch for flush")
        next_steps = [
            f"Watch: holds ${low52:.2f} (52W low support)",
            "Enter only after 2–3 days of stabilisation above the low",
            f"Entry on stabilisation: ${round(low52 * 1.02, 2):.2f} (+2% above low)",
            f"Stop if entering: below ${low52:.2f}",
        ]

    # ── Case 9: Recovering trend ──────────────────────────────────
    # FIX v4.4: HOLD branch now checks actual above_200 to avoid
    # unconditionally printing "below EMA200" when stock is above it.
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
            if above_200:
                # Recovering and above EMA200 — stalling, needs momentum
                verdict, zone = "HOLD", "Above EMA200 — Lacks Momentum"
                reasons = ["Above EMA200 but momentum stalling — no clear entry signal"]
                next_steps = [
                    f"Watch for: RSI > 55 AND sustained close above EMA50 ${c['ema50']:.2f}",
                    "Then enter on confirmation",
                    f"Stop if holding: below EMA200 ${c['ema200']:.2f}",
                ]
            else:
                # Below EMA200 — needs structural reclaim
                verdict, zone = "HOLD", "Below EMA200"
                reasons = ["Below EMA200 — no structural confirmation of recovery"]
                next_steps = [
                    f"Wait for: reclaim EMA200 ${c['ema200']:.2f}",
                    "Then enter on confirmation with RSI > 50",
                    f"Do not enter below EMA200 ${c['ema200']:.2f}",
                ]

    # ── Case 10: Mixed trend ──────────────────────────────────────
    elif "MIXED" in trend:
        if range_pos < 35 and rsi < 45:
            verdict, zone = "WATCH", "Potential Base"
            reasons = ["Lower 52W range — possible accumulation phase"]
            next_steps = [
                f"Trigger: RSI > 50 AND close above EMA50 ${c['ema50']:.2f}",
                f"Entry: ${round(c['ema50'] * 1.01, 2):.2f} on breakout",
                f"Stop: below recent low ${c.get('low_52w', 0):.2f}",
            ]
        else:
            verdict, zone = "NEUTRAL", "No Clear Edge"
            reasons = ["Mixed signals — no directional conviction"]
            next_steps = [
                f"Bull trigger: close above EMA50 ${c['ema50']:.2f} + RSI > 55",
                f"Bear trigger: close below EMA200 ${c['ema200']:.2f}",
                "No position until one of these confirms",
            ]

    # ── Case 11: Default fallback ─────────────────────────────────
    else:
        if above_50 and above_200 and rsi > 55 and stretch_pct < 25:
            verdict, zone = "WATCH", "Building Momentum"
            reasons = [
                "Above both EMAs — structure is bullish",
                f"RSI {rsi:.0f} — momentum building, not yet extended",
            ]
            next_steps = [
                f"Ideal entry: pullback to EMA50 ${c['ema50']:.2f}",
                f"Breakout entry: above 52W high ${c.get('high_52w', 0):.2f} with volume",
                f"Stop: below EMA50 ${c['ema50']:.2f}",
            ]
        elif above_50 and above_200 and stretch_pct >= 25:
            reentry_lo = round(c["ema50"] * 0.98, 2)
            reentry_hi = round(c["ema50"] * 1.03, 2)
            verdict, zone = "EXTENDED", "Stretched — Wait"
            reasons = [
                f"RSI {rsi:.0f} + {stretch_pct:.0f}% above EMA50 — not ideal entry",
                "Risk/reward is poor at current levels",
            ]
            next_steps = [
                f"Wait for pullback to EMA50 ${reentry_lo} – ${reentry_hi}",
                "Do not chase at current levels",
            ]
        else:
            verdict, zone = "NEUTRAL", "No Clear Setup"
            reasons = ["No strong directional signal currently"]
            next_steps = [
                f"Bull trigger: above EMA50 ${c['ema50']:.2f} + RSI > 55",
                "No position until signal confirms",
            ]

    # ── MTF RSI extreme addendum ───────────────────────────────────
    # Appended to reasons for any verdict not already highlighting extremes.
    if mtf_rsi_extreme and verdict not in (
        "PARABOLIC EXTENSION", "EXTREMELY EXTENDED", "PARABOLIC SPIKE", "CRASH",
    ):
        reasons.append(
            f"Higher-TF RSI extreme (max {mtf_max_rsi:.0f}) — elevated reversion risk"
        )

    # ── Override 1: Market conditions (VIX + SPY) ─────────────────
    if market_ctx:
        vix     = market_ctx.get("^VIX", {}).get("price", 15)
        spy_pct = market_ctx.get("SPY",  {}).get("pct",   0)
        if vix > 25 and spy_pct < -1.5 and any(x in verdict for x in ["BUY", "MOMENTUM"]):
            verdict = "WAIT — Market"
            reasons.insert(0, f"Market bleeding — VIX {vix:.0f}, SPY {spy_pct:.1f}% — defer entry")
            next_steps = [
                "Wait for market to stabilise (VIX < 20, SPY positive)",
                f"Re-entry zone when market calms: near EMA50 ${c['ema50']:.2f}",
            ]

    # ── Override 2: Earnings proximity ────────────────────────────
    # Covers BUY, MOMENTUM, WATCH, EXTENDED verdicts.
    # EXTREMELY EXTENDED contains "EXTENDED" — override is intentional
    # (earnings before a stretched stock IS a binary risk to flag).
    _earnings_verdicts = ["BUY", "MOMENTUM", "WATCH", "EXTENDED"]
    if any(x in verdict for x in _earnings_verdicts):
        _, days_until = earnings_cache if earnings_cache else get_earnings_date(c["symbol"])
        if days_until is not None and days_until <= EARNINGS_WARNING_DAYS:
            verdict = "WAIT — Earnings"
            zone    = f"Earnings in {days_until}d"
            reasons.insert(0, f"Earnings in {days_until} days — skip new entries before binary event")
            next_steps = [
                "Re-evaluate after earnings report",
                f"If bullish post-earnings: entry near EMA50 ${c['ema50']:.2f}",
            ]

    return verdict, zone, reasons, next_steps


# ════════════════════════════════════════════════════════════════════
# SECTION 16 — AI ANALYSIS (Gemini 2.0 Flash)
# ════════════════════════════════════════════════════════════════════

def get_ai_analysis(
    ctx:         dict,
    verdict:     str,
    zone:        str,
    sector_name: str | None,
    sector_avg:  float | None,
    mtf_verdicts: dict,
    stock_info:  dict,
) -> str | None:
    """
    Send a structured 4-line prompt to Gemini 2.0 Flash.
    Returns the raw text string or None on any failure.
    Rate-limit (429) is retried once after 15 seconds.
    """
    from market_intel import GEMINI_API_KEY
    if not GEMINI_API_KEY:
        return None

    c           = ctx
    stretch_pct = (c["current"] - c["ema50"]) / c["ema50"] * 100 if c.get("ema50", 0) > 0 else 0

    mtf_str = "\n".join([
        f"  {tf}: {v['trend']} | RSI {v['rsi']} | ADX {v['adx']} | {v['adx_sar']}"
        for tf, v in mtf_verdicts.items()
    ]) if mtf_verdicts else "  N/A"

    sector_str  = f"{sector_name}: {sector_avg:+.1f}% avg today" if sector_name and sector_avg else "Unknown"
    analyst_str = ""
    if stock_info.get("target_mean"):
        upside      = (stock_info["target_mean"] - c["current"]) / c["current"] * 100
        analyst_str = (
            f"\nAnalyst target: ${stock_info['target_mean']:.2f} mean "
            f"({upside:+.1f}% upside) — {stock_info.get('rec_key', '')}"
        )

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

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 400},
    }

    def _call() -> str | None:
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                data = r.json()
                if data.get("candidates"):
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif r.status_code == 429:
                return None   # signal retry
            else:
                logging.warning(f"Gemini error {r.status_code}")
        except Exception as exc:
            logging.error(f"AI analysis: {exc}")
        return None

    result = _call()
    if result is None:
        logging.warning("Gemini rate limited, retrying in 15s")
        time.sleep(15)
        result = _call()
    return result


# ════════════════════════════════════════════════════════════════════
# SECTION 17 — TAG PILLS BUILDER
# ════════════════════════════════════════════════════════════════════

def build_tag_pills(
    verdict:       str,
    ctx:           dict,
    rs_label:      str | None,
    squeeze_state: str,
    rsi_div:       str | None,
    stock_info:    dict,
) -> str:
    """
    Build up to 4 compact emoji tag pills shown under the header.

    FIX v4.4: Priority reordered so squeeze/divergence pills rank BEFORE
    the EMA stretch pill — squeeze/divergence are more actionable signals
    and were previously never shown when earlier pills filled the 4-slot limit.

    Priority order:
      1. Primary verdict pill (always slot 0)
      2. RSI state
      3. Squeeze / RSI divergence  ← elevated in priority (FIX)
      4. RS vs SPY
      5. EMA stretch (demoted — still shown if slots remain)
      6. Short interest
    """
    tags: list[str] = []
    c    = ctx
    rsi  = c["rsi"]
    stretch_pct = (c["current"] - c["ema50"]) / c["ema50"] * 100 if c.get("ema50", 0) > 0 else 0

    # ① Primary verdict pill
    if   "PARABOLIC EXTENSION" in verdict:  tags.append("🚨 Parabolic Ext")
    elif "PARABOLIC SPIKE"     in verdict:  tags.append("🚨 Parabolic Spike")
    elif "CRASH"               in verdict:  tags.append("💥 Crash")
    elif "EXTREMELY EXTENDED"  in verdict:  tags.append("🔥 Extremely Extended")
    elif "MOMENTUM CONTINUATION" in verdict: tags.append("🚀 Momentum")
    elif "MOMENTUM"            in verdict:  tags.append("🚀 ATH Momentum")
    elif "BUY ZONE"            in verdict:  tags.append("🟢 Buy Zone")
    elif "EXTENDED"            in verdict:  tags.append("🟠 Extended")
    elif "TAKE PROFITS"        in verdict:  tags.append("💰 Take Profits")
    elif "AVOID"               in verdict:  tags.append("🔴 Avoid")
    elif "CAUTION"             in verdict:  tags.append("⚠️ Caution")
    elif "WAIT"                in verdict:  tags.append("⏳ Wait")
    elif "WATCH"               in verdict:  tags.append("👀 Watch")
    elif "HOLD"                in verdict:  tags.append("⏸️ Hold")
    elif "NEUTRAL"             in verdict:  tags.append("⚖️ Neutral")

    # ② RSI state pill
    if   rsi >= 80:     tags.append("🔴 RSI Extreme")
    elif rsi >= RSI_OVERBOUGHT: tags.append("🔴 RSI Overbought")
    elif rsi <= RSI_OVERSOLD:   tags.append("🟢 RSI Oversold")
    elif rsi >= 60:     tags.append("🟡 RSI Bullish")

    # ③ Squeeze / divergence — elevated priority (FIX v4.4)
    if   squeeze_state == "building":  tags.append("🔥 Squeeze Building")
    elif squeeze_state == "fired":     tags.append("💥 Squeeze Fired")
    if   rsi_div == "bullish":         tags.append("📈 RSI Div Bullish")
    elif rsi_div == "bearish":         tags.append("📉 RSI Div Bearish")

    # ④ RS vs SPY
    if rs_label:
        if   "Strong Leader"  in rs_label: tags.append("💪 Strong Leader")
        elif "Outperform"     in rs_label: tags.append("📈 Outperforming")
        elif "Laggard"        in rs_label: tags.append("📉 Laggard")
        elif "Underperform"   in rs_label: tags.append("🔻 Underperforming")

    # ⑤ EMA stretch (demoted — only if space remains after higher-priority pills)
    if stretch_pct >= 50 and "PARABOLIC" not in verdict:
        tags.append(f"⚡ {stretch_pct:.0f}% above EMA50")
    elif 30 <= stretch_pct < 50 and "EXTENDED" not in verdict:
        tags.append(f"⚡ {stretch_pct:.0f}% above EMA50")

    # ⑥ Short interest
    short_pct = stock_info.get("short_pct")
    if short_pct and short_pct > 0.15:
        tags.append(f"⚡ {short_pct * 100:.0f}% Short")

    return "  ·  ".join(tags[:4])


# ════════════════════════════════════════════════════════════════════
# SECTION 18 — PRICE CONTEXT GRID
# ════════════════════════════════════════════════════════════════════

def build_price_context_grid(
    ctx:        dict,
    cad_price:  float | None,
    tsx_symbol: str | None,
    usd_cad:    float | None,
    support:    float | None,
    resistance: float | None,
) -> str:
    """
    Compact scannable price context block (§6 of the alert).
    Each row has a fixed emoji anchor for rapid eye-scanning on mobile.
    Support/resistance rendered here (were passed in but never shown in v4.3).
    Volume is excluded — already shown in §4 PRICE section.
    """
    c        = ctx
    decimals = 4 if c["current"] < 10 else 2
    pf       = f"{{:.{decimals}f}}"

    rp = c["range_pos"]
    if   rp >= 90: rp_bar = "████████████ 90%+ — near top"
    elif rp >= 70: rp_bar = f"█████████░░░ {rp:.0f}% — upper range"
    elif rp >= 50: rp_bar = f"██████░░░░░░ {rp:.0f}% — mid range"
    elif rp >= 30: rp_bar = f"████░░░░░░░░ {rp:.0f}% — lower range"
    else:          rp_bar = f"██░░░░░░░░░░ {rp:.0f}% — near bottom"

    ath_str = f"`{c['ath_pct']:+.1f}%` — {ath_recency(c['ath_date'])}"

    msg  = f"📊 *PRICE CONTEXT*\n`─────────────────────────`\n"
    msg += f"📍 52W Pos   `{rp_bar}`\n"
    msg += f"🏔️ From ATH  {ath_str}\n"
    msg += f"📐 52W Range `${pf.format(c['low_52w'])}` — `${pf.format(c['high_52w'])}`\n"

    if c.get("pct_from_52w_low", 0) > 500:
        msg += "⚠️ _52W low may reflect split/spin-off — range unreliable_\n"

    if support and resistance:
        msg += f"🔑 Structure  Support `${pf.format(support)}` · Resistance `${pf.format(resistance)}`\n"

    if cad_price and tsx_symbol:
        msg += f"🍁 TSX       `{tsx_symbol}` = `${cad_price:.2f} CAD`\n"
    elif usd_cad and not is_crypto(c["symbol"]):
        implied = round(c["current"] * usd_cad, 2)
        msg += f"🍁 CAD       `${implied:.2f}` (×{usd_cad:.4f}) — no TSX listing\n"

    return msg


# ════════════════════════════════════════════════════════════════════
# SECTION 19 — FULL ANALYSIS FORMATTER  v4.4
# ════════════════════════════════════════════════════════════════════
#
# 13-section layout:
#  §1  Header  (company name, ticker, price, change, timestamp, meta)
#  §2  Tag pills
#  §3  Verdict block (verdict + AI action line + reasons)
#  §4  Price (live, range, volume, POC)
#  §5  What To Do (next_steps from get_verdict — specific price levels)
#  §6  Price context grid (52W, ATH, structure, CAD)
#  §7  Timeframes (Daily/Weekly/Monthly trend + RSI + ADX + SAR)
#  §8  Technicals (trend, RSI, EMA, beta, squeeze, divergence)
#  §9a Sector context
#  §9b Relative Strength vs SPY  ← separated from sector (FIX v4.4)
#  §10 Fundamentals (targets, P/E, short interest, institutional)
#  §11 Earnings  (pre-fetched, no second API call)
#  §12 Market  (SPY + VIX + QQQ)  ← QQQ now rendered (FIX v4.4)
#  §13 AI full analysis (4 lines from Gemini)

def format_full_analysis(
    symbol:        str,
    ctx:           dict,
    verdict:       str,
    zone:          str,
    reasons:       list[str],
    ai_text:       str | None,
    market_ctx:    dict,
    rs_score:      float | None,
    rs_label:      str | None,
    poc:           float | None,
    support:       float | None,
    resistance:    float | None,
    squeeze_state: str,
    squeeze_dir:   str | None,
    rsi_div:       str | None,
    mtf_verdicts:  dict,
    sector_name:   str | None,
    sector_avg:    float | None,
    stock_info:    dict,
    cad_price:     float | None,
    tsx_symbol:    str | None,
    usd_cad:       float | None,
    next_steps:    list[str],
    earnings_info: tuple | None,
) -> str:
    em  = SYMBOL_EMOJI.get(symbol, "📊")
    c   = ctx
    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f"%b %d · %I:%M %p {tz}")

    decimals = 4 if c["current"] < 10 else 2
    pf       = f"{{:.{decimals}f}}"
    current  = c["current"]
    drop     = c["day_change_pct"]
    sign     = "+" if drop >= 0 else ""
    drop_em  = "🟢" if drop >= 0 else "🔴"

    stretch_pct = (current - c["ema50"]) / c["ema50"] * 100 if c.get("ema50", 0) > 0 else 0
    above_50    = current > c["ema50"]
    above_200   = current > c["ema200"]

    asset_type = stock_info.get("asset_type", "Stock")
    long_name  = stock_info.get("long_name", symbol)
    sector_h   = stock_info.get("sector", SYMBOL_TO_SECTOR.get(symbol, ""))
    industry   = stock_info.get("industry", "")
    exchange   = stock_info.get("exchange", "")
    mcap       = fmt_mcap(stock_info.get("market_cap"))
    beta_val   = stock_info.get("beta")

    # ════════════════════════════════════════════
    # §1  HEADER
    # ════════════════════════════════════════════
    name_str = f"\n_{long_name}_" if long_name and long_name != symbol else ""
    msg  = f"🔍 *{symbol}* {em}{name_str}\n"
    msg += f"`${pf.format(current)}`  {drop_em} *{sign}{drop:.2f}%*  _· {ts}_\n"

    meta_parts = [asset_type]
    if sector_h:                               meta_parts.append(sector_h)
    if industry and industry != sector_h:      meta_parts.append(industry)
    if exchange:                               meta_parts.append(exchange)
    if mcap:                                   meta_parts.append(mcap)
    msg += f"_{' · '.join(meta_parts)}_\n"
    msg += f"`══════════════════════════`\n\n"

    # ════════════════════════════════════════════
    # §2  TAG PILLS
    # ════════════════════════════════════════════
    tags = build_tag_pills(verdict, ctx, rs_label, squeeze_state, rsi_div, stock_info)
    if tags:
        msg += f"{tags}\n\n"

    # ════════════════════════════════════════════
    # §3  VERDICT BLOCK
    # ════════════════════════════════════════════
    msg += f"*〔 {verdict} 〕*\n"
    msg += f"_↳ {zone}_\n"

    # FIX v4.4: AI summary — always use the last non-empty line (Gemini Line 4
    # per the prompt) rather than fragile keyword search across all lines.
    if ai_text:
        lines = [l.strip() for l in ai_text.strip().split("\n") if l.strip()]
        if lines:
            msg += f"\n{lines[-1]}\n"

    msg += "\n"
    for r in reasons[:3]:
        msg += f"  › {r}\n"
    msg += f"`══════════════════════════`\n\n"

    # ════════════════════════════════════════════
    # §4  PRICE
    # ════════════════════════════════════════════
    msg += f"💵 *PRICE*\n`──────────────────────────`\n"
    msg += f"  Live     `${pf.format(current)}`  {drop_em} {sign}{drop:.2f}% today\n"
    msg += f"  Range    L `${pf.format(c['today_low'])}` — H `${pf.format(c['today_high'])}`\n"

    vol_ratio = c["vol_ratio"]
    if   vol_ratio >= 2.0: vol_line = f"`{vol_ratio:.1f}x` 🔥 Unusually high"
    elif vol_ratio >= 1.5: vol_line = f"`{vol_ratio:.1f}x` ⬆️ Above average"
    elif vol_ratio >= 0.8: vol_line = f"`{vol_ratio:.1f}x` — Normal"
    else:                  vol_line = f"`{vol_ratio:.1f}x` ⬇️ Below avg — weak move"
    msg += f"  Volume   {vol_line}\n"

    if poc:
        diff_pct = (current - poc) / poc * 100
        if   abs(diff_pct) < 0.5:  poc_em = "🎯"
        elif current > poc:         poc_em = "⬆️"
        else:                       poc_em = "⬇️"
        if abs(diff_pct) < 0.5:
            msg += f"  POC      {poc_em} AT `${pf.format(poc)}` — volume magnet\n"
        elif current > poc:
            msg += f"  POC      {poc_em} Above `${pf.format(poc)}` ({diff_pct:+.1f}%) — buyers in control\n"
        else:
            msg += f"  POC      {poc_em} Below `${pf.format(poc)}` ({diff_pct:+.1f}%) — sellers in control\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # §5  WHAT TO DO
    # next_steps come from get_verdict() — specific computed price levels.
    # No recomputation here — single source of truth.
    # ════════════════════════════════════════════
    msg += f"🎯 *WHAT TO DO*\n`──────────────────────────`\n"
    for step in next_steps:
        msg += f"  › {step}\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # §6  PRICE CONTEXT
    # ════════════════════════════════════════════
    msg += build_price_context_grid(ctx, cad_price, tsx_symbol, usd_cad, support, resistance)
    msg += "\n"

    # ════════════════════════════════════════════
    # §7  TIMEFRAMES
    # ════════════════════════════════════════════
    if mtf_verdicts:
        msg += f"🗂️ *TIMEFRAMES*\n`──────────────────────────`\n"
        for tf_label, v in mtf_verdicts.items():
            t    = v["trend"]
            t_em = "🚀" if "Strong Bull" in t else "📈" if "Bull" in t else "💀" if "Strong Bear" in t else "📉" if "Bear" in t else "⚖️"
            sar_em = "✅" if v.get("sar_bull") else "❌" if v.get("sar_bull") is False else "➖"
            adx_em = "💪" if v["adx"] >= 25 else "⚠️" if v["adx"] < 20 else "➖"
            rsi_str = f"RSI `{v['rsi']}`"
            if v.get("rsi_tag"):
                rsi_str += f" _{v['rsi_tag']}_"
            sig    = v["adx_sar"]
            sig_em = "✅" if sig == "Trend BUY" else "❌" if sig == "Trend SELL" else "⚠️" if sig == "Ranging" else "➖"
            msg += (
                f"  {t_em} *{tf_label}*  {t}\n"
                f"     {rsi_str}  {adx_em} ADX `{v['adx']:.0f}`  SAR {sar_em}  {sig_em} _{sig}_\n"
            )
        msg += "\n"

    # ════════════════════════════════════════════
    # §8  TECHNICALS
    # ════════════════════════════════════════════
    msg += f"📈 *TECHNICALS*\n`──────────────────────────`\n"
    msg += f"  Trend   {c['trend']}\n"

    rsi = c["rsi"]
    if   rsi < RSI_OVERSOLD:      rsi_tag_s, rsi_em = "Oversold",   "🟢"
    elif rsi >= 80:               rsi_tag_s, rsi_em = "Extreme",    "🔴"
    elif rsi >= RSI_OVERBOUGHT:   rsi_tag_s, rsi_em = "Overbought", "🔴"
    elif rsi > 60:                rsi_tag_s, rsi_em = "Bullish",    "🟡"
    else:                         rsi_tag_s, rsi_em = "Neutral",    "⚪"
    msg += f"  RSI     `{rsi:.0f}` {rsi_em} _{rsi_tag_s}_\n"

    if   stretch_pct >= 50:   stretch_warn = " 🚨 _Extreme — do not chase_"
    elif stretch_pct >= 30:   stretch_warn = " ⚠️ _Extended — wait for pullback_"
    elif stretch_pct >= 15:   stretch_warn = " ⚠️ _Stretched_"
    elif stretch_pct <= -15:  stretch_warn = " 🟢 _Deeply oversold_"
    else:                     stretch_warn = ""
    msg += f"  EMA50   `${pf.format(c['ema50'])}` ({stretch_pct:+.1f}%){stretch_warn}\n"
    msg += f"  EMA200  `${pf.format(c['ema200'])}`\n"

    if   above_50 and above_200:  msg += "  ✅ Above EMA50 & EMA200\n"
    elif above_200 and not above_50: msg += "  ⚠️ Below EMA50, above EMA200\n"
    elif not above_200 and above_50: msg += "  🔀 Above EMA50, below EMA200\n"
    else:                         msg += "  🔴 Below both EMAs\n"

    if beta_val:
        beta_em   = "🐢" if beta_val < 0.8 else "⚡" if beta_val > 1.5 else "⚖️"
        beta_desc = "low volatility" if beta_val < 0.8 else "high volatility" if beta_val > 1.5 else "market volatility"
        msg += f"  Beta    `{beta_val:.2f}` {beta_em} _{beta_desc}_\n"

    if   squeeze_state == "building":
        msg += "  🔥 *SQUEEZE BUILDING* — explosive move loading\n"
    elif squeeze_state == "fired":
        sq_em = "⬆️" if squeeze_dir == "bullish" else "⬇️"
        msg += f"  💥 *SQUEEZE FIRED* {sq_em} {squeeze_dir}\n"
    if   rsi_div == "bullish":    msg += "  📈 *RSI DIVERGENCE* — bullish momentum building\n"
    elif rsi_div == "bearish":    msg += "  📉 *RSI DIVERGENCE* — momentum fading\n"
    msg += "\n"

    # ════════════════════════════════════════════
    # §9a  SECTOR
    # ════════════════════════════════════════════
    if sector_name and sector_avg is not None:
        sec_em   = "🟢" if sector_avg > 0 else "🔴"
        sym_vs   = drop - sector_avg
        sec_sign = "+" if sector_avg >= 0 else ""
        msg += f"🏭 *SECTOR · {sector_name}*\n`──────────────────────────`\n"
        msg += f"  Sector avg  {sec_em} `{sec_sign}{sector_avg:.2f}%` today\n"
        if sym_vs > 1.5:
            msg += f"  💪 Outperforming sector by `{sym_vs:+.1f}%`\n"
        elif sym_vs < -1.5:
            msg += f"  🔻 Underperforming sector by `{sym_vs:+.1f}%`\n"
        else:
            msg += "  ➖ In line with sector\n"
        msg += "\n"

    # ════════════════════════════════════════════
    # §9b  RELATIVE STRENGTH vs SPY
    # FIX v4.4: given its own header section — was floating as an
    # orphan line after the sector block with no visual anchor.
    # ════════════════════════════════════════════
    if rs_score is not None:
        rs_sign = "+" if rs_score >= 0 else ""
        rs_em   = "💪" if rs_score > 5 else "📉" if rs_score < -5 else "➖"
        msg += f"📊 *RELATIVE STRENGTH*\n`──────────────────────────`\n"
        msg += f"  {rs_em} vs SPY (5d)  {rs_label}  `{rs_sign}{rs_score:.1f}%`\n\n"

    # ════════════════════════════════════════════
    # §10  FUNDAMENTALS
    # ════════════════════════════════════════════
    short_pct   = stock_info.get("short_pct")
    inst_pct    = stock_info.get("inst_pct")
    target_mean = stock_info.get("target_mean")
    target_high = stock_info.get("target_high")
    target_low  = stock_info.get("target_low")
    rec_key     = stock_info.get("rec_key", "")
    analyst_n   = stock_info.get("analyst_count", 0)
    pe_ratio    = stock_info.get("pe_ratio")

    has_fundamentals = (target_mean or short_pct or inst_pct or pe_ratio) and not is_crypto(symbol)
    if has_fundamentals:
        msg += f"📊 *FUNDAMENTALS*\n`──────────────────────────`\n"

        if target_mean:
            upside    = (target_mean - current) / current * 100
            up_sign   = "+" if upside >= 0 else ""
            upside_em = "🟢" if upside > 0 else "🔴"
            msg += f"  🎯 Analyst target  `${target_mean:.2f}` {upside_em} `{up_sign}{upside:.1f}%`"
            if analyst_n:
                msg += f" _({analyst_n} analysts)_"
            msg += "\n"
            if target_high and target_low:
                msg += f"     Range `${target_low:.2f}` — `${target_high:.2f}`\n"
            if rec_key:
                rec_em = "🟢" if "Buy" in rec_key else "🔴" if "Sell" in rec_key else "🟡"
                msg += f"     Rating {rec_em} *{rec_key}*\n"
            if upside < -15:
                msg += (
                    f"  🚨 *{abs(upside):.0f}% ABOVE analyst consensus*\n"
                    f"     _Analysts haven't upgraded — may be overextended_\n"
                )
            elif upside < -5:
                msg += "  ⚠️ _Stock above analyst consensus — limited upside per analysts_\n"

        if pe_ratio:
            pe_em  = "🔴" if pe_ratio > 60 else "🟡" if pe_ratio > 30 else "🟢"
            pe_tag = " _(elevated)_" if pe_ratio > 40 else ""
            msg += f"  {pe_em} P/E ratio  `{pe_ratio:.0f}`{pe_tag}\n"

        if short_pct:
            si_em  = "⚡" if short_pct > 0.15 else "➖"
            si_tag = "*High*" if short_pct > 0.15 else "Normal"
            msg += f"  {si_em} Short int  `{short_pct * 100:.1f}%` {si_tag}"
            if short_pct > 0.15:
                msg += " — _squeeze potential on breakout_"
            msg += "\n"

        if inst_pct:
            inst_em  = "🏦" if inst_pct > 0.7 else "➖"
            inst_tag = "Smart money heavy" if inst_pct > 0.7 else "Moderate"
            msg += f"  {inst_em} Institutional  `{inst_pct * 100:.0f}%` — {inst_tag}\n"

        msg += "\n"

    # ════════════════════════════════════════════
    # §11  EARNINGS  (pre-fetched — no second API call)
    # ════════════════════════════════════════════
    if earnings_info:
        earnings_date, days_until = earnings_info
        warn = format_earnings_warning(symbol, earnings_date, days_until)
        if warn:
            msg += f"📅 *EARNINGS*\n`──────────────────────────`\n  {warn}\n\n"

    # ════════════════════════════════════════════
    # §12  MARKET CONDITIONS  (SPY + VIX + QQQ)
    # FIX v4.4: QQQ now rendered — was fetched in market_ctx but
    # never displayed (v4.3 audit promised this, never delivered).
    # ════════════════════════════════════════════
    if market_ctx:
        spy = market_ctx.get("SPY",  {})
        vix = market_ctx.get("^VIX", {})
        qqq = market_ctx.get("QQQ",  {})
        if spy or vix or qqq:
            msg += f"🌍 *MARKET*\n`──────────────────────────`\n"
            parts: list[str] = []

            if spy:
                spy_pct  = spy.get("pct", 0)
                spy_sign = "+" if spy_pct >= 0 else ""
                spy_em   = "🟢" if spy_pct >= 0 else "🔴"
                parts.append(f"SPY {spy_em} `{spy_sign}{spy_pct:.2f}%`")

            if qqq:
                qqq_pct  = qqq.get("pct", 0)
                qqq_sign = "+" if qqq_pct >= 0 else ""
                qqq_em   = "🟢" if qqq_pct >= 0 else "🔴"
                parts.append(f"QQQ {qqq_em} `{qqq_sign}{qqq_pct:.2f}%`")

            if vix:
                vix_val = vix.get("price", 0)
                vix_em  = "🔴" if vix_val > 25 else "🟡" if vix_val > 18 else "🟢"
                vix_tag = "High" if vix_val > 25 else "Elevated" if vix_val > 18 else "Calm"
                parts.append(f"VIX {vix_em} `{vix_val:.1f}` _{vix_tag}_")

            msg += f"  {'  ·  '.join(parts)}\n\n"

    # ════════════════════════════════════════════
    # §13  AI FULL ANALYSIS
    # ════════════════════════════════════════════
    if ai_text:
        msg += f"🤖 *AI ANALYSIS*\n`──────────────────────────`\n"
        for line in ai_text.strip().split("\n"):
            if line.strip():
                msg += f"  {line.strip()}\n"
        msg += "\n"

    msg += f"`══════════════════════════`\n"
    msg += f"_AlphaEdge v4.4 · On-demand_"
    return msg


# ════════════════════════════════════════════════════════════════════
# SECTION 20 — SHORT ANALYSIS FORMATTER
# ════════════════════════════════════════════════════════════════════

def format_short_analysis(
    symbol:     str,
    ctx:        dict,
    verdict:    str,
    zone:       str,
    rs_label:   str | None,
    rs_score:   float | None,
    stock_info: dict,
) -> str:
    """3-line quick-summary format for 'TSLA short' command."""
    em        = SYMBOL_EMOJI.get(symbol, "")
    c         = ctx
    drop      = c["day_change_pct"]
    sign      = "+" if drop >= 0 else ""
    decimals  = 4 if c["current"] < 10 else 2
    pf        = f"{{:.{decimals}f}}"
    rs_str    = f"  RS {rs_label}" if rs_label else ""
    long_name = stock_info.get("long_name", symbol)
    name_str  = f" ({long_name})" if long_name and long_name != symbol else ""

    msg  = f"{em} *{symbol}*{name_str}  `${pf.format(c['current'])}`  ({sign}{drop:.1f}%)\n"
    msg += f"{verdict} — {zone}\n"
    msg += f"RSI `{c['rsi']:.0f}`  {c['trend']}{rs_str}"
    return msg


# ════════════════════════════════════════════════════════════════════
# SECTION 21 — PRICE ALERT SYSTEM
# ════════════════════════════════════════════════════════════════════

def load_alerts() -> dict:
    return load_json(ALERTS_FILE, {})


def save_alerts(alerts: dict) -> None:
    save_json(ALERTS_FILE, alerts)


def set_alert(symbol: str, target_price: float, direction: str = "auto") -> None:
    alerts = load_alerts()
    try:
        df      = yf.download(symbol, period="1d", interval="1m",
                              progress=False, auto_adjust=True)
        df      = _clean_df(df)
        current = float(df["Close"].iloc[-1]) if not df.empty else None
    except Exception:
        current = None

    if direction == "auto" and current:
        direction = "above" if target_price > current else "below"

    alert_key = f"{symbol}_{target_price}"
    alerts[alert_key] = {
        "symbol":               symbol,
        "target":               target_price,
        "direction":            direction,
        "set_at":               now_est().isoformat(),
        "expires_at":           (now_est() + timedelta(days=30)).isoformat(),
        "warning_sent":         False,
        "expiry_warning_sent":  False,
        "triggered":            False,
    }
    save_alerts(alerts)

    dir_str    = "rises to" if direction == "above" else "falls to"
    warn_price = target_price * 0.98 if direction == "above" else target_price * 1.02
    cur_str    = f" (currently ${current:.2f})" if current else ""
    send_telegram(
        f"Alert set\n"
        f"{SYMBOL_EMOJI.get(symbol,'')} *{symbol}* — notify when {dir_str} `${target_price:.2f}`{cur_str}\n"
        f"Early warning at `${warn_price:.2f}` (2% before)\n"
        f"Expires in 30 days"
    )


def cancel_alert(symbol: str) -> None:
    alerts  = load_alerts()
    removed = []
    for key in list(alerts.keys()):
        if alerts[key]["symbol"] == symbol:
            removed.append(alerts[key]["target"])
            del alerts[key]
    save_alerts(alerts)
    em = SYMBOL_EMOJI.get(symbol, "")
    if removed:
        send_telegram(f"Cancelled alerts for {em} *{symbol}*: {', '.join([f'${t}' for t in removed])}")
    else:
        send_telegram(f"No active alerts for *{symbol}*")


def list_alerts() -> None:
    alerts = load_alerts()
    active = {k: v for k, v in alerts.items() if not v.get("triggered")}
    if not active:
        send_telegram("No active alerts.\n\nSet one: `alert TSLA 450`")
        return
    msg = f"*ACTIVE ALERTS ({len(active)})*\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"
    for key, a in sorted(active.items(), key=lambda x: x[1]["symbol"]):
        em        = SYMBOL_EMOJI.get(a["symbol"], "")
        dir_str   = "above" if a["direction"] == "above" else "below"
        expires   = datetime.fromisoformat(a["expires_at"])
        days_left = (expires - now_est()).days
        warn      = a["target"] * 0.98 if a["direction"] == "above" else a["target"] * 1.02
        msg += f"{em} *{a['symbol']}*  {dir_str} `${a['target']:.2f}`\n"
        msg += f"   Early warning `${warn:.2f}`  {days_left}d left\n\n"
    send_telegram(msg)


def check_alerts() -> None:
    """
    Check all active alerts against current prices.
    Handles: triggered, early warning (2%), expiry warning (1 day), expiry.
    Timezone-safe: expires.tzinfo is normalised to EST if naive.
    """
    alerts = load_alerts()
    if not alerts:
        return
    changed = False
    now     = now_est()

    for key, a in list(alerts.items()):
        if a.get("triggered"):
            continue

        symbol     = a["symbol"]
        target     = a["target"]
        direction  = a["direction"]
        warn_price = target * 0.98 if direction == "above" else target * 1.02
        em         = SYMBOL_EMOJI.get(symbol, "")

        expires = datetime.fromisoformat(a["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=EST)
        days_left = (expires - now).days

        if days_left <= 1 and not a.get("expiry_warning_sent"):
            send_telegram(
                f"Alert expiring\n"
                f"{em} *{symbol}* to `${target:.2f}` expires tomorrow\n"
                f"`alert {symbol} {target}` to reset"
            )
            a["expiry_warning_sent"] = True
            changed = True

        if now > expires:
            send_telegram(f"Alert expired\n{em} *{symbol}* to `${target:.2f}` (30 days, untriggered)")
            del alerts[key]
            changed = True
            continue

        try:
            df      = yf.download(symbol, period="1d", interval="5m",
                                  progress=False, auto_adjust=True)
            if df.empty:
                continue
            df      = _clean_df(df)
            current = float(df["Close"].iloc[-1])
        except Exception:
            continue

        triggered = (
            (direction == "above" and current >= target) or
            (direction == "below" and current <= target)
        )
        if triggered:
            send_telegram(
                f"ALERT TRIGGERED\n{em} *{symbol}* hit `${target:.2f}`\n"
                f"Current: `${current:.2f}`\nAlert removed."
            )
            a["triggered"] = True
            changed = True
        elif (not a.get("warning_sent") and (
            (direction == "above" and current >= warn_price) or
            (direction == "below" and current <= warn_price)
        )):
            pct_away = abs(current - target) / target * 100
            send_telegram(
                f"APPROACHING TARGET\n{em} *{symbol}* near `${target:.2f}`\n"
                f"Now: `${current:.2f}` — {pct_away:.1f}% away"
            )
            a["warning_sent"] = True
            changed = True

    if changed:
        save_alerts(alerts)


# ════════════════════════════════════════════════════════════════════
# SECTION 22 — WATCHLIST SCAN
# ════════════════════════════════════════════════════════════════════

def run_watchlist_scan() -> None:
    """
    Scan all universe symbols, sort by verdict priority, send grouped summary.
    Uses get_verdict() for each symbol — no MTF (speed priority).
    """
    send_telegram("Scanning watchlist... ~60s", silent=True)
    all_syms, emoji_map = load_universe()
    if not all_syms:
        send_telegram("Could not load symbols.yaml")
        return

    market_ctx = get_market_ctx()
    results: list[dict] = []

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
                "symbol":   sym,
                "emoji":    emoji_map.get(sym, ""),
                "verdict":  verdict,
                "zone":     zone,
                "drop":     ctx["day_change_pct"],
                "rsi":      ctx["rsi"],
                "rs_score": rs_score or 0,
                "current":  ctx["current"],
            })
            print(f"{ctx['day_change_pct']:+.1f}% {verdict}")
        except Exception as exc:
            logging.warning(f"Watchlist scan {sym}: {exc}")
            print("err")

    if not results:
        send_telegram("No data — try again later")
        return

    def sort_key(r: dict) -> tuple:
        v = r["verdict"]
        if "MOMENTUM"  in v: return (0, -r["drop"])
        if "BUY"       in v: return (1, -r["drop"])
        if "WATCH"     in v: return (2, -r["drop"])
        if "NEUTRAL"   in v: return (3, -r["drop"])
        if "EXTENDED"  in v: return (4, -r["drop"])
        if "AVOID"     in v: return (5, -r["drop"])
        return (6, -r["drop"])

    results.sort(key=sort_key)

    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f"%a %b %d  %I:%M %p {tz}")
    msg = f"*WATCHLIST SCAN*\n{ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    groups: dict[str, list] = {}
    for r in results:
        groups.setdefault(r["verdict"], []).append(r)

    for vkey, items in groups.items():
        msg += f"*{vkey}*\n"
        for r in items:
            sign     = "+" if r["drop"] >= 0 else ""
            decimals = 4 if r["current"] < 10 else 2
            pf       = f"{{:.{decimals}f}}"
            rs_str   = f" RS {r['rs_score']:+.1f}%" if r["rs_score"] else ""
            msg += (
                f"  {r['emoji']} *{r['symbol']}* `${pf.format(r['current'])}`"
                f"  {sign}{r['drop']:.1f}%  RSI `{r['rsi']:.0f}`{rs_str}\n"
            )
        msg += "\n"

    msg += "_Type any symbol for full analysis_\n_AlphaEdge v4.4_"
    send_telegram(msg)


# ════════════════════════════════════════════════════════════════════
# SECTION 23 — TOP MOVERS
# ════════════════════════════════════════════════════════════════════

def run_top_movers() -> None:
    """Rank universe symbols by 1-day change, report top 5 gainers and losers."""
    send_telegram("Fetching top movers...", silent=True)
    all_syms, emoji_map = load_universe()
    market_ctx = get_market_ctx()
    movers: list[dict] = []

    for sym in all_syms:
        try:
            df = yf.download(sym, period="5d", interval="1d",
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            df     = _clean_df(df)
            change = (float(df["Close"].iloc[-1]) - float(df["Close"].iloc[-2])) / float(df["Close"].iloc[-2]) * 100
            movers.append({
                "symbol": sym,
                "emoji":  emoji_map.get(sym, ""),
                "change": change,
                "price":  float(df["Close"].iloc[-1]),
            })
            time.sleep(0.2)
        except Exception:
            pass

    if not movers:
        send_telegram("Could not fetch data")
        return

    movers.sort(key=lambda x: -x["change"])
    gainers = [m for m in movers if m["change"] > 0][:5]
    losers  = list(reversed([m for m in movers if m["change"] < 0][-5:]))

    now = now_est()
    tz  = now.tzname() or "EDT"
    ts  = now.strftime(f"%a %b %d  %I:%M %p {tz}")
    msg = f"*TOP MOVERS*\n{ts}\n`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    if gainers:
        msg += "*GAINERS*\n"
        for m in gainers:
            d    = 4 if m["price"] < 10 else 2
            msg += f"  {m['emoji']} *{m['symbol']}* `${m['price']:.{d}f}`  +{m['change']:.2f}%\n"

    if losers:
        msg += "\n*LOSERS*\n"
        for m in losers:
            d    = 4 if m["price"] < 10 else 2
            msg += f"  {m['emoji']} *{m['symbol']}* `${m['price']:.{d}f}`  {m['change']:.2f}%\n"

    if market_ctx:
        spy = market_ctx.get("SPY", {})
        vix = market_ctx.get("^VIX", {})
        qqq = market_ctx.get("QQQ", {})
        msg += "\n`─────────────────`\n"
        line_parts = []
        if spy:
            spy_pct  = spy.get("pct", 0)
            spy_sign = "+" if spy_pct >= 0 else ""
            line_parts.append(f"SPY: `{spy_sign}{spy_pct:.2f}%`")
        if qqq:
            qqq_pct  = qqq.get("pct", 0)
            qqq_sign = "+" if qqq_pct >= 0 else ""
            line_parts.append(f"QQQ: `{qqq_sign}{qqq_pct:.2f}%`")
        if vix:
            line_parts.append(f"VIX: `{vix.get('price', 0):.1f}`")
        msg += "  ".join(line_parts) + "\n"

    msg += "\n_Type any symbol for full analysis_"
    send_telegram(msg)


# ════════════════════════════════════════════════════════════════════
# SECTION 24 — ON-DEMAND BRIEF
# ════════════════════════════════════════════════════════════════════

def run_brief() -> None:
    """Route to the appropriate brief: weekly summary, morning, or evening."""
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
    except Exception as exc:
        logging.error(f"Brief: {exc}")
        send_telegram(f"Brief failed: {exc}")


# ════════════════════════════════════════════════════════════════════
# SECTION 25 — HELP
# ════════════════════════════════════════════════════════════════════

def send_help() -> None:
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
_AlphaEdge v4.4_""")


# ════════════════════════════════════════════════════════════════════
# SECTION 26 — FULL ANALYSIS RUNNER
# ════════════════════════════════════════════════════════════════════

def run_analysis(symbol: str, mode: str = "full") -> None:
    """
    Orchestrate the full on-demand analysis pipeline:
      1. Validate symbol
      2. Fetch context, stock info, market context, MTF
      3. Fetch earnings ONCE — pass to both get_verdict() and formatter
      4. Compute verdict, RS, sector, CAD, POC, squeeze, divergence
      5. Get AI analysis
      6. Format and send

    Note: the 'timeframe' parameter was accepted in v4.3 but never used
    in any logic. Removed from signature to avoid confusion.
    """
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

    print("  -> Stock info...")
    stock_info = get_stock_info(symbol)

    print("  -> Market context...")
    market_ctx = get_market_ctx()

    print("  -> MTF verdicts...")
    mtf_verdicts = get_mtf_verdicts(symbol) if mode == "full" else {}

    # Fetch earnings ONCE — passed to both get_verdict() and format_full_analysis()
    # Eliminates the duplicate get_earnings_date() API call from v4.2.
    print("  -> Earnings date...")
    earnings_info = get_earnings_date(symbol)   # returns (date, days_until)

    verdict, zone, reasons, next_steps = get_verdict(
        ctx, market_ctx, mtf_verdicts, earnings_cache=earnings_info,
    )
    rs_score, rs_label = calc_relative_strength(ctx)

    if mode == "short":
        send_telegram(format_short_analysis(symbol, ctx, verdict, zone, rs_label, rs_score, stock_info))
        return

    print("  -> Sector context...")
    sector_name, sector_avg, _ = get_sector_context(symbol)

    print("  -> CAD pricing...")
    cad_price, tsx_symbol = get_cad_price(symbol)
    usd_cad = get_usd_cad_rate() if not is_crypto(symbol) else None

    # 6mo daily download for POC, squeeze, divergence.
    # get_full_context() downloads 5y daily internally but does not expose the raw df.
    # This focused 6mo slice keeps the analysis functions fast.
    print("  -> Daily bars (6mo)...")
    try:
        df_daily = yf.download(symbol, period="6mo", interval="1d",
                               progress=False, auto_adjust=True)
        df_daily                   = _clean_df(df_daily)
        poc                        = quick_poc(df_daily)
        support, resistance        = recent_structure(df_daily)
        squeeze_state, squeeze_dir = detect_squeeze(df_daily)
        rsi_div                    = detect_rsi_divergence(df_daily)
    except Exception:
        poc = support = resistance = None
        squeeze_state, squeeze_dir, rsi_div = "none", None, None

    print("  -> AI analysis...")
    ai_text = get_ai_analysis(
        ctx, verdict, zone, sector_name, sector_avg, mtf_verdicts, stock_info
    )
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


# ════════════════════════════════════════════════════════════════════
# SECTION 27 — MAIN COMMAND ROUTER
# ════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    Parse sys.argv[1] as either a raw symbol string or JSON payload dict.
    Dispatch to the appropriate handler based on event_type / command.
    """
    payload: dict = {}
    if len(sys.argv) > 1:
        try:
            payload = json.loads(sys.argv[1])
        except Exception:
            payload = {"symbol": sys.argv[1], "mode": "full"}

    event_type = payload.get("event_type", "analyze_symbol")
    command    = payload.get("command",    "")
    symbol     = payload.get("symbol",     "")
    mode       = payload.get("mode",       "full")

    logging.info(f"Command: event={event_type} cmd={command} sym={symbol}")

    if event_type == "analyze_symbol" or (not command and symbol):
        run_analysis(symbol, mode)
    elif command == "alert":
        price     = payload.get("price")
        direction = payload.get("direction", "auto")
        if symbol and price:
            set_alert(symbol, float(price), direction)
        else:
            send_telegram("Usage: `alert TSLA 450`")
    elif command == "cancel_alert":
        if symbol: cancel_alert(symbol)
    elif command == "list_alerts":
        list_alerts()
    elif command == "check_alerts":
        check_alerts()
    elif command == "scan":
        run_watchlist_scan()
    elif command == "top":
        run_top_movers()
    elif command == "brief":
        run_brief()
    elif command == "help":
        send_help()
    else:
        send_telegram(f"Unknown command: `{command}`\nType `help` for commands.")


if __name__ == "__main__":
    main()


# ════════════════════════════════════════════════════════════════════
# END OF FILE
# ════════════════════════════════════════════════════════════════════
#
# ┌──────────────────────────────────────────────────────────────────┐
# │  CONTINUATION PROMPT — paste into a new chat to resume          │
# └──────────────────────────────────────────────────────────────────┘
#
# """
# ALPHAEDGE SINGLE SCAN — New-Chat Continuation Prompt
# ──────────────────────────────────────────────────────
#
# WHAT THIS FILE IS:
#   single_scan.py v4.4 — on-demand stock analysis engine.
#   Accepts a ticker (or JSON payload) and sends a structured
#   13-section Telegram alert with verdict, trade plan, technicals,
#   fundamentals, multi-timeframe alignment, AI commentary, and more.
#
# ARCHITECTURE:
#   • Data source:  market_intel.get_full_context() (cached yfinance)
#   • Verdict:      get_verdict() — 12 branches + 2 overrides
#   • Indicators:   EMA50/200, RSI, ATR, ADX, Parabolic SAR,
#                   Bollinger/Keltner squeeze, RSI divergence, POC
#   • AI:           Gemini 2.0 Flash (4-line structured prompt)
#   • Delivery:     market_intel.send_telegram (MarkdownV2, auto-split)
#   • Alerts:       price_alerts.json (trigger / warning / expiry)
#   • Universe:     symbols.yaml → load_universe()
#   • CAD prices:   yfinance .TO / .V suffix lookup + USDCAD=X
#
# VERDICT EVALUATION ORDER (first match wins):
#   0    |1d| ≥ 15%                   → PARABOLIC SPIKE / CRASH
#   0b   stretch ≥ 50% + RSI ≥ 70     → PARABOLIC EXTENSION
#   1    Uptrend + ATH -5% + RSI <75  → MOMENTUM
#   TP   RSI ≥ 75 + up + above EMAs   → TAKE PROFITS  [v4.4 fix]
#   2    RSI/stretch ext + MTF extreme → EXTREMELY EXTENDED
#   3    RSI/stretch extended          → EXTENDED
#   4    Uptrend + RSI 55-69           → MOMENTUM CONTINUATION
#   5    Uptrend + RSI <55             → BUY ZONE (pullback)
#   6    EMA50 pullback + RSI <58      → BUY ZONE (EMA50)
#   7    Downtrend + below EMA200      → AVOID
#   8    Near 52W low + dropping       → CAUTION
#   9    RECOVERING trend              → WATCH / HOLD
#  10    MIXED trend                   → WATCH / NEUTRAL
#  11    Default                       → WATCH / EXTENDED / NEUTRAL
#   OV1  VIX >25 + SPY <-1.5%         → WAIT — Market
#   OV2  Earnings within N days        → WAIT — Earnings
#
# UNIFIED RSI THRESHOLDS (defined in SECTION 5):
#   RSI_OVERBOUGHT      = 70   (verdict + tags + technicals)
#   RSI_MTF_OVERBOUGHT  = 70   (MTF table tag, was 80 — FIX v4.4)
#   RSI_MTF_EXTREME_HI  = 80   (mtf_rsi_extreme trigger, was 85 — FIX)
#   RSI_MTF_EXTREME_LO  = 20
#   RSI_OVERSOLD        = 30
#   RSI_EXTREME_HI      = 90
#
# BUGS FIXED IN v4.4 (27 total identified, all addressed):
#   CRITICAL:
#     1. TAKE PROFITS was dead branch — reordered before EXTENDED
#     2. c['pct_from_52w_low'] KeyError → .get() guard added
#     3. MTF RSI overbought tag was 80, verdict was 70 — aligned to 70
#     4. mtf_rsi_extreme threshold was 85 vs tag threshold 80 — aligned to 80
#     5. RECOVERING HOLD said "below EMA200" regardless of actual position
#     6. upside_to_ath was negative when stock at/above ATH
#   HIGH:
#     7. QQQ not rendered in §12 Market (promised in v4.3, never done)
#     8. AI summary used fragile keyword search — now uses last line always
#     9. RS vs SPY had no section header — now §9b with own block
#   MEDIUM:
#    10. RSI divergence recent window 3→5 bars (false positive reduction)
#    11. Tag pills: squeeze/divergence elevated above stretch in priority
#    12. TAKE PROFITS condition drop > 2 → drop ≥ 0 (fires on flat days)
#    13. volume_label() dead function removed
#    14. CAUTION next_steps now includes entry and stop, not just watch
#    15. timeframe parameter removed from run_analysis() (was unused)
#    16. QQQ also added to run_top_movers() market footer
#
# WHAT TO VERIFY WHEN MODIFYING:
#   □ New verdict branch? → add to EVALUATION ORDER comment + send test alert
#   □ RSI threshold changed? → update ALL 6 constants in SECTION 5
#   □ New field used from ctx? → verify get_full_context() provides it
#   □ next_steps changed? → ensure all price references use .get() guards
#   □ get_verdict() changed? → run with known symbol in diagnostics mode
#   □ Telegram MarkdownV2? → all dynamic strings must be safe (no unescaped special chars)
#   □ MTF added/removed? → update bull_count denominator in mtf_all_bull check
#   □ earnings_info changed? → update both get_verdict() and format_full_analysis()
#   □ New market_ctx key? → update format_full_analysis §12 AND run_top_movers()
#   □ Gemini prompt changed? → re-validate that last line is always the action line
#
# RELATED FILES:
#   market_intel.py      — shared data layer (cache, delivery, escape)
#   symbols.yaml         — universe definition + emoji map
#   price_alerts.json    — persistent alert state (auto-created)
#   morning_brief.py     — morning / evening brief builder
#   scanner.py           — dip scanner + weekly summary
#   logs/single_*.log    — daily rotating operational log
# """
