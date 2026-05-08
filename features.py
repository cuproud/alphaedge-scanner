"""
AlphaEdge advanced features.
• Signal quality trending (SQS history per symbol)
• Would-have-triggered backtest
• Dynamic SQS threshold based on win rate
• Volume profile / POC
• VIX regime filter
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from utils import (
    now_est, load_json, save_json, clean_df, pine_rsi, pine_atr,
    ema, sma, rma, EST
)

SQS_HISTORY_FILE = 'sqs_history.json'
BACKTEST_STATE_FILE = 'backtest_state.json'
DYNAMIC_THRESHOLD_FILE = 'dynamic_threshold.json'


# ═══════════════════════════════════════════════════════
# 1. SIGNAL QUALITY TRENDING
# ═══════════════════════════════════════════════════════

def record_sqs(symbol, sqs, timestamp=None, keep_last=10):
    """Record latest SQS for a symbol. Keep last N per symbol."""
    history = load_json(SQS_HISTORY_FILE, {})
    if timestamp is None:
        timestamp = now_est().isoformat()
    entries = history.get(symbol, [])
    entries.append({'sqs': int(sqs), 'ts': timestamp})
    # Keep only last N, sorted
    entries = sorted(entries, key=lambda e: e['ts'])[-keep_last:]
    history[symbol] = entries
    save_json(SQS_HISTORY_FILE, history)


def get_sqs_trend(symbol, min_points=3):
    """
    Returns dict:
      {'trend': 'improving'/'declining'/'stable'/'insufficient',
       'values': [68, 74, 82],
       'delta': +14,
       'arrow_str': '68 → 74 → 82'}
    """
    history = load_json(SQS_HISTORY_FILE, {})
    entries = history.get(symbol, [])
    if len(entries) < min_points:
        return {'trend': 'insufficient', 'values': [], 'delta': 0, 'arrow_str': ''}

    recent = entries[-min_points:]
    values = [e['sqs'] for e in recent]
    delta = values[-1] - values[0]

    # Use slope for stability
    xs = np.arange(len(values))
    slope, _ = np.polyfit(xs, values, 1) if len(values) > 1 else (0, 0)

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
    """Returns Telegram-formatted trend note, or empty string."""
    t = get_sqs_trend(symbol)
    if t['trend'] == 'insufficient':
        return ""
    if t['trend'] == 'improving':
        return f"📈 _Quality improving: {t['arrow_str']} (+{t['delta']})_"
    if t['trend'] == 'declining':
        return f"📉 _Quality declining: {t['arrow_str']} ({t['delta']})_"
    return f"➖ _Quality stable: {t['arrow_str']}_"


# ═══════════════════════════════════════════════════════
# 2. "WOULD HAVE TRIGGERED" BACKTEST (last 24h)
# ═══════════════════════════════════════════════════════

def already_ran_backtest_today(key='morning'):
    state = load_json(BACKTEST_STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    return state.get(f'{key}_backtest') == today


def mark_backtest_ran(key='morning'):
    state = load_json(BACKTEST_STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    state[f'{key}_backtest'] = today
    save_json(BACKTEST_STATE_FILE, state)


def scan_missed_signals(analyze_fn, symbols, timeframe='1h', lookback_hours=24):
    """
    Scans historical bars and asks: "would analyze_fn have fired here?"
    Only reports completed setups (excludes bar currently forming).

    analyze_fn signature:  analyze_fn(symbol, tf_config, htf_bull, mtf_sum, None)
    returns:  (result_dict, reason_or_None)

    Returns: list of dicts with ['symbol', 'signal', 'price', 'sqs', 'hours_ago', 'outcome']
    """
    # Stub — the real implementation needs to replay analyze_fn against
    # each historical bar-close of the last 24h. Since this is expensive,
    # we offer a SIMPLIFIED version: just check if a signal would have
    # fired in the most recent LAST_N closed bars.

    # Because scanner.analyze_symbol is coupled to "latest bar",
    # we keep this function generic — user must pass their replay.
    # For now we return an empty list and expose a *simpler* version below.
    return []


def find_recent_big_moves(symbols, hours=24, min_move_pct=3.0):
    """
    Simpler 'would have seen' scan: finds symbols with big moves in last 24h
    that the user likely missed.
    """
    misses = []
    cutoff = now_est() - timedelta(hours=hours)

    for sym in symbols:
        try:
            df = yf.download(sym, period='5d', interval='1h',
                             progress=False, auto_adjust=True)
            if df.empty:
                continue
            df = clean_df(df)

            # Filter to last 24h
            if df.index.tz is None:
                df.index = df.index.tz_localize('UTC').tz_convert(EST)
            else:
                df.index = df.index.tz_convert(EST)

            recent = df[df.index >= cutoff]
            if len(recent) < 3:
                continue

            start = float(recent['Close'].iloc[0])
            end = float(recent['Close'].iloc[-1])
            high = float(recent['High'].max())
            low = float(recent['Low'].min())

            move = (end - start) / start * 100
            max_up = (high - start) / start * 100
            max_down = (low - start) / start * 100

            if abs(move) >= min_move_pct or max_up >= min_move_pct or abs(max_down) >= min_move_pct:
                misses.append({
                    'symbol': sym,
                    'start': start,
                    'end': end,
                    'move_pct': round(move, 2),
                    'max_up_pct': round(max_up, 2),
                    'max_down_pct': round(max_down, 2),
                    'high': high,
                    'low': low,
                })
        except Exception as e:
            logging.debug(f"Backtest scan {sym}: {e}")

    # Sort by absolute move
    misses.sort(key=lambda m: -abs(m['move_pct']))
    return misses


def format_missed_signals(misses, max_shown=8):
    """Format the 'what you missed' summary for morning brief."""
    if not misses:
        return None

    msg = "*🔍 LAST 24H — NOTABLE MOVES*\n"
    msg += "`─────────────────`\n"
    msg += "_Moves you'd have caught had you been watching:_\n\n"

    shown = misses[:max_shown]
    for m in shown:
        em = "🚀" if m['move_pct'] > 3 else "📉" if m['move_pct'] < -3 else "⚡"
        sign = "+" if m['move_pct'] >= 0 else ""
        msg += f"{em} *{m['symbol']}* `{sign}{m['move_pct']}%` "
        msg += f"(H: +{m['max_up_pct']:.1f}% / L: {m['max_down_pct']:.1f}%)\n"

    if len(misses) > max_shown:
        msg += f"\n_+{len(misses) - max_shown} more moves_"

    msg += "\n💡 _Signals fire in real time — this is backward-looking context._"
    return msg


# ═══════════════════════════════════════════════════════
# 3. DYNAMIC SQS THRESHOLD
# ═══════════════════════════════════════════════════════

def compute_grade_winrates(history_file, lookback_days=30):
    """
    Reads trade_history.json, computes win rate per grade.
    Returns: {'A+': {'total': 8, 'wins': 6, 'winrate': 0.75, 'avg_r': 1.8},
              'A':  {...}, 'B': {...}, 'C': {...}}
    """
    history = load_json(history_file, [])
    cutoff = now_est() - timedelta(days=lookback_days)
    stats = {g: {'total': 0, 'wins': 0, 'r_sum': 0.0} for g in ['A+', 'A', 'B', 'C']}

    for t in history:
        try:
            ca = t.get('closed_at')
            if not ca:
                continue
            dt = datetime.fromisoformat(ca)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            if dt < cutoff:
                continue
            grade = t.get('grade', 'C')
            if grade not in stats:
                continue
            r = t.get('final_r', 0) or 0
            stats[grade]['total'] += 1
            if r > 0:
                stats[grade]['wins'] += 1
            stats[grade]['r_sum'] += r
        except:
            continue

    # Compute derived stats
    out = {}
    for g, s in stats.items():
        wr = s['wins'] / s['total'] if s['total'] > 0 else None
        avg_r = s['r_sum'] / s['total'] if s['total'] > 0 else None
        out[g] = {
            'total': s['total'],
            'wins': s['wins'],
            'winrate': wr,
            'avg_r': avg_r,
        }
    return out


def compute_dynamic_threshold(history_file,
                              base_threshold=75,
                              min_threshold=70,
                              max_threshold=85,
                              min_sample=10):
    """
    Adjust SQS threshold based on recent performance.
    - If B-grade winrate < 35%, raise threshold (demand A or better).
    - If A-grade winrate > 65% & sample healthy, can lower slightly to catch more.
    """
    stats = compute_grade_winrates(history_file)

    a_plus = stats.get('A+', {})
    a = stats.get('A', {})
    b = stats.get('B', {})

    # Default
    threshold = base_threshold
    reason = "baseline (no adjustment)"

    total_sample = (a_plus.get('total', 0) + a.get('total', 0) + b.get('total', 0))
    if total_sample < min_sample:
        # Not enough data — stay at baseline
        result = {'threshold': base_threshold, 'reason': f'insufficient data ({total_sample}/{min_sample})',
                  'stats': stats}
        save_json(DYNAMIC_THRESHOLD_FILE, result)
        return result

    b_wr = b.get('winrate') or 0
    a_wr = a.get('winrate') or 0

    # Tighten if B-grade is performing poorly
    if b.get('total', 0) >= 5 and b_wr < 0.35:
        threshold = min(max_threshold, base_threshold + 5)
        reason = f"B-grade winrate {b_wr:.0%} is poor — tightening"
    elif b.get('total', 0) >= 5 and b_wr < 0.45:
        threshold = min(max_threshold, base_threshold + 3)
        reason = f"B-grade winrate {b_wr:.0%} below target — tightening slightly"
    elif a.get('total', 0) >= 5 and a_wr > 0.65:
        threshold = max(min_threshold, base_threshold - 3)
        reason = f"A-grade winrate {a_wr:.0%} strong — relaxing slightly"

    result = {
        'threshold': int(threshold),
        'reason': reason,
        'stats': stats,
        'computed_at': now_est().isoformat(),
    }
    save_json(DYNAMIC_THRESHOLD_FILE, result)
    return result


def get_current_threshold(base=75):
    """Returns cached threshold or base."""
    result = load_json(DYNAMIC_THRESHOLD_FILE, {})
    if not result:
        return base
    # Recompute if older than 1 day
    try:
        dt = datetime.fromisoformat(result.get('computed_at', ''))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        if now_est() - dt > timedelta(hours=24):
            return base  # Force recompute next cycle
    except:
        return base
    return result.get('threshold', base)


# ═══════════════════════════════════════════════════════
# 4. VOLUME PROFILE / POC
# ═══════════════════════════════════════════════════════

def compute_poc(df, bins=30, lookback_bars=200):
    """
    Computes Volume Profile Point of Control (POC).
    Returns dict: {'poc': price, 'vah': value_area_high, 'val': value_area_low}

    POC = price level with highest traded volume.
    Value Area = 70% of total volume.
    """
    if df is None or df.empty or len(df) < 20:
        return None

    recent = df.iloc[-lookback_bars:] if len(df) > lookback_bars else df

    low = float(recent['Low'].min())
    high = float(recent['High'].max())
    if high <= low:
        return None

    bin_edges = np.linspace(low, high, bins + 1)
    volume_at_price = np.zeros(bins)

    for idx in range(len(recent)):
        bar_low = float(recent['Low'].iloc[idx])
        bar_high = float(recent['High'].iloc[idx])
        bar_vol = float(recent['Volume'].iloc[idx])
        if bar_vol <= 0:
            continue
        # Distribute bar volume across bins it spans
        bar_range = max(bar_high - bar_low, 1e-9)
        for b in range(bins):
            bin_low = bin_edges[b]
            bin_high = bin_edges[b + 1]
            overlap = max(0, min(bar_high, bin_high) - max(bar_low, bin_low))
            if overlap > 0:
                volume_at_price[b] += bar_vol * (overlap / bar_range)

    if volume_at_price.sum() == 0:
        return None

    # POC = bin with most volume
    poc_idx = int(np.argmax(volume_at_price))
    poc = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2

    # Value Area (70% of volume)
    total_vol = volume_at_price.sum()
    target = total_vol * 0.70
    # Expand from POC outward
    included = {poc_idx}
    vol_accum = volume_at_price[poc_idx]
    lo, hi = poc_idx, poc_idx
    while vol_accum < target and (lo > 0 or hi < bins - 1):
        next_lo = volume_at_price[lo - 1] if lo > 0 else -1
        next_hi = volume_at_price[hi + 1] if hi < bins - 1 else -1
        if next_hi >= next_lo:
            hi += 1
            included.add(hi)
            vol_accum += next_hi
        else:
            lo -= 1
            included.add(lo)
            vol_accum += next_lo

    val = bin_edges[lo]
    vah = bin_edges[hi + 1]

    return {
        'poc': round(poc, 4),
        'vah': round(vah, 4),
        'val': round(val, 4),
    }


def format_poc_context(current_price, poc_data):
    """
    Returns human-readable POC context line.
    Examples:
      "🎯 Price ABOVE POC ($98.50) — strong hands in control"
      "🎯 Price BELOW POC ($102.00) — sellers defending"
      "🎯 Price AT POC ($100.00) — decision zone"
    """
    if not poc_data:
        return None
    poc = poc_data['poc']
    vah = poc_data['vah']
    val = poc_data['val']

    pct_diff = (current_price - poc) / poc * 100

    if abs(pct_diff) < 0.3:
        return f"🎯 Price *AT POC* `${poc:.2f}` — decision zone (volume magnet)"
    elif current_price > vah:
        return f"🎯 Price *ABOVE Value Area* (POC `${poc:.2f}`, VAH `${vah:.2f}`) — strong hands, premium"
    elif current_price < val:
        return f"🎯 Price *BELOW Value Area* (POC `${poc:.2f}`, VAL `${val:.2f}`) — weak, discount"
    elif current_price > poc:
        return f"🎯 Price *ABOVE POC* `${poc:.2f}` (inside VA) — buyers in control"
    else:
        return f"🎯 Price *BELOW POC* `${poc:.2f}` (inside VA) — sellers in control"


# ═══════════════════════════════════════════════════════
# 5. VIX REGIME FILTER
# ═══════════════════════════════════════════════════════

_vix_cache = {'ts': None, 'data': None}


def get_vix_regime(cache_minutes=10):
    """
    Returns dict:
      {'vix': 22.3, 'vix_prev': 18.1, 'spike_pct': 23.2,
       'regime': 'calm'/'elevated'/'spike'/'extreme',
       'blocks_longs': bool,
       'warning': str or None}
    """
    # Cache to avoid repeated fetches
    if _vix_cache['ts']:
        if (now_est() - _vix_cache['ts']).total_seconds() < cache_minutes * 60:
            return _vix_cache['data']

    try:
        df = yf.download('^VIX', period='10d', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 5:
            return None
        df = clean_df(df)
        vix_now = float(df['Close'].iloc[-1])
        vix_prev = float(df['Close'].iloc[-2])
        vix_5d_avg = float(df['Close'].iloc[-5:].mean())

        spike_pct = (vix_now - vix_5d_avg) / vix_5d_avg * 100 if vix_5d_avg > 0 else 0

        # Classify
        if vix_now >= 35:
            regime = 'extreme'
        elif vix_now >= 25 or (vix_now >= 22 and spike_pct > 15):
            regime = 'spike'
        elif vix_now >= 20:
            regime = 'elevated'
        else:
            regime = 'calm'

        # Blocks longs?
        blocks_longs = (regime == 'extreme') or (regime == 'spike' and spike_pct > 20)

        warning = None
        if regime == 'extreme':
            warning = f"🚨 VIX {vix_now:.1f} EXTREME — panic regime, avoid longs"
        elif regime == 'spike':
            warning = f"⚠️ VIX {vix_now:.1f} spiking (+{spike_pct:.0f}% vs 5d avg) — reduce size"
        elif regime == 'elevated':
            warning = f"🟡 VIX {vix_now:.1f} elevated — be selective"

        result = {
            'vix': round(vix_now, 2),
            'vix_prev': round(vix_prev, 2),
            'vix_5d_avg': round(vix_5d_avg, 2),
            'spike_pct': round(spike_pct, 1),
            'regime': regime,
            'blocks_longs': blocks_longs,
            'warning': warning,
        }
        _vix_cache['ts'] = now_est()
        _vix_cache['data'] = result
        return result
    except Exception as e:
        logging.error(f"VIX regime: {e}")
        return None


def vix_blocks_signal(signal_direction):
    """Returns (blocked, reason). Call this from signal gate."""
    vix = get_vix_regime()
    if not vix:
        return False, None
    if signal_direction == 'BUY' and vix['blocks_longs']:
        return True, f"VIX {vix['vix']} {vix['regime']} — longs blocked"
    return False, None
