"""
AlphaEdge shared utilities.
Used by all 4 scanner modules.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

EST = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════
# TIME
# ═══════════════════════════════════════════════
def now_est():
    return datetime.now(EST)

def fmt_time():
    return now_est().strftime('%H:%M %Z')

def fmt_datetime():
    return now_est().strftime('%Y-%m-%d %H:%M %Z')


# ═══════════════════════════════════════════════
# JSON I/O
# ═══════════════════════════════════════════════
def load_json(path, default):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)


# ═══════════════════════════════════════════════
# DATAFRAME
# ═══════════════════════════════════════════════
def clean_df(df):
    """Flatten yfinance MultiIndex columns."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ═══════════════════════════════════════════════
# MARKDOWN ESCAPE (Telegram-safe)
# ═══════════════════════════════════════════════
# Telegram Markdown (legacy): _ * [ ` are special
# We escape these ONLY outside intended formatting.
_MD_SPECIALS = r'_*[]()~>#+-=|{}.!'

def md_escape(text):
    """Escape Telegram MarkdownV1 special chars for user data (symbols, numbers, etc.)."""
    if text is None:
        return ""
    text = str(text)
    # Escape backticks, asterisks, underscores, brackets
    text = text.replace('\', '\\\')
    for ch in '_*`[]':
        text = text.replace(ch, '\' + ch)
    return text

def safe_symbol(sym):
    """Symbols like BRK.B or GC=F have safe chars. Just escape underscores if any."""
    return str(sym).replace('_', r'\_')


# ═══════════════════════════════════════════════
# READABLE FORMATTING (no R-speak)
# ═══════════════════════════════════════════════

def fmt_price(val, decimals=2):
    """Format price with right decimals."""
    try:
        return f"{float(val):.{decimals}f}"
    except:
        return str(val)


def fmt_r(r, plain=False):
    """
    Format R multiple.
    plain=True → "+2.5× risk" (readable)
    plain=False → "+2.5R" (terse, for headers/tables)
    """
    try:
        r = float(r)
    except:
        return "—"
    if abs(r) < 0.01:
        return "breakeven" if plain else "0.00R"
    sign = "+" if r > 0 else ""
    if plain:
        return f"{sign}{r:.2f}× your risk"
    return f"{sign}{r:.2f}R"


def fmt_risk_reward(risk_dollars, reward_dollars, plain=True):
    """
    Convert R:R to plain English.
    plain=True → "Risk $2.00 → Make $6.00 (3× reward)"
    plain=False → "R:R 1:3"
    """
    try:
        ratio = reward_dollars / risk_dollars if risk_dollars > 0 else 0
    except:
        ratio = 0
    if plain:
        return f"Risk `${risk_dollars:.2f}` → Make `${reward_dollars:.2f}` (*{ratio:.1f}× reward*)"
    return f"R:R 1:{ratio:.1f}"


def tp_description(tp_num, entry, target, risk, is_long, plain=True):
    """
    Returns human-readable TP description.
    plain=True: "Target 1: $102.50 — +$2.50 profit (matches your risk)"
    """
    profit = abs(target - entry)
    r_mult = profit / risk if risk > 0 else 0
    if plain:
        if r_mult < 1.1:
            desc = "matches your risk"
        elif r_mult < 2.1:
            desc = f"{r_mult:.1f}× your risk"
        else:
            desc = f"{r_mult:.1f}× your risk — big win"
        return f"Target {tp_num}: `${fmt_price(target)}` — +${profit:.2f} profit ({desc})"
    return f"TP{tp_num}: `${fmt_price(target)}` (+{r_mult:.0f}R)"


# ═══════════════════════════════════════════════
# URGENCY EMOJI (visual priority)
# ═══════════════════════════════════════════════

def urgency_prefix(sqs, strong_trend=False, vix_warning=False):
    """
    Returns urgency emoji prefix for signal alerts.
    Stacks by quality + conditions.
    """
    if vix_warning:
        return "⚠️🌋 "           # market turbulent
    if sqs >= 92 and strong_trend:
        return "🚨🔥🔥 "          # rare top-tier
    if sqs >= 88:
        return "🚨🔥 "            # elite
    if sqs >= 80:
        return "🚨 "              # strong
    if sqs >= 72:
        return "⭐ "              # solid
    return ""                     # standard


def tier_label(sqs):
    if sqs >= 90: return "🏆 ELITE"
    if sqs >= 80: return "⭐ STRONG"
    if sqs >= 70: return "✅ GOOD"
    if sqs >= 60: return "⚠️ FAIR"
    return "🔹 LOW"


# ═══════════════════════════════════════════════
# TIME HELPERS
# ═══════════════════════════════════════════════

def time_ago(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        delta = now_est() - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60: return f"{mins}m ago"
        h = mins // 60
        if h < 24: return f"{h}h {mins % 60}m ago"
        d = h // 24
        return f"{d}d {h % 24}h ago"
    except:
        return "?"


def time_until(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        delta = dt - now_est()
        mins = int(delta.total_seconds() / 60)
        if mins <= 0: return "expired"
        if mins < 60: return f"in {mins}m"
        h = mins // 60
        return f"in {h}h {mins % 60}m"
    except:
        return "?"


def absolute_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        return dt.strftime('%I:%M %p %Z').lstrip('0')
    except:
        return "?"


# ═══════════════════════════════════════════════
# PINE INDICATORS (shared)
# ═══════════════════════════════════════════════

def rma(series, length):
    """Wilder's moving average — matches Pine's ta.rma()."""
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


# ═══════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════

def send_telegram(message, token, chat_id, silent=False, parse_mode='Markdown'):
    """Send Telegram message with auto-split for >4000 chars."""
    if not token or not chat_id:
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
            header = f"_(part {i+1}/{len(parts)})_\n" if len(parts) > 1 else ""
            if not _send_single(header + part, token, chat_id, silent, parse_mode):
                success = False
            time.sleep(0.3)
        return success
    return _send_single(message, token, chat_id, silent, parse_mode)


def _send_single(message, token, chat_id, silent, parse_mode):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': chat_id,
            'text': message,
            'parse_mode': parse_mode,
            'disable_notification': silent,
            'disable_web_page_preview': True,
        }, timeout=10)
        if r.status_code != 200:
            logging.error(f"Telegram {r.status_code}: {r.text[:200]}")
            # Retry without Markdown if parse error
            if parse_mode and 'parse' in r.text.lower():
                logging.warning("Retrying without parse_mode")
                r = requests.post(url, json={
                    'chat_id': chat_id, 'text': message,
                    'disable_notification': silent,
                }, timeout=10)
                return r.status_code == 200
        return r.status_code == 200
    except Exception as e:
        logging.error(f"Telegram send: {e}")
        return False


# ═══════════════════════════════════════════════
# COOLDOWN (generic, file-backed)
# ═══════════════════════════════════════════════

def can_alert(state_file, key, hours):
    """Returns True if cooldown expired; marks as sent."""
    state = load_json(state_file, {})
    last = state.get(key)
    if last:
        try:
            dt = datetime.fromisoformat(last)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            if now_est() - dt < timedelta(hours=hours):
                return False
        except:
            pass
    state[key] = now_est().isoformat()
    save_json(state_file, state)
    return True
