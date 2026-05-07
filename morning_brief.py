"""
ALPHAEDGE MORNING BRIEF
═══════════════════════════════════════════════════════════════
Fired once every weekday at ~9 AM EST.
Delivers:
• Pre-market snapshot of entire watchlist
• Sector winners/losers
• Verdicts for every symbol
• Earnings calendar for the day
• Market context + AI daily outlook
"""

import os
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from market_intel import (
    get_full_context, get_verdict, get_market_ctx, calc_relative_strength,
    get_earnings_date, format_earnings_warning,
    MONITOR_LIST, SYMBOL_EMOJI, SECTORS, send_telegram,
    now_est, load_json, save_json, STATE_FILE
)

import requests
import yfinance as yf
import pandas as pd

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

logging.basicConfig(
    filename=f'logs/brief_{now_est().strftime("%Y-%m-%d")}.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
Path('logs').mkdir(exist_ok=True)

# ═══════════════════════════════════════════════
# AI DAILY OUTLOOK
# ═══════════════════════════════════════════════

def ai_daily_outlook(market_ctx, sector_summary, top_movers):
    if not GEMINI_API_KEY:
        return None

    mkt = market_ctx or {}
    spy = mkt.get('SPY', {}).get('pct', 0)
    qqq = mkt.get('QQQ', {}).get('pct', 0)
    vix = mkt.get('^VIX', {}).get('price', 15)

    sec_lines = "\n".join([f"  {name}: {avg:+.2f}%" for name, avg in sector_summary[:5]])
    mover_lines = "\n".join([f"  {m['symbol']}: {m['pct']:+.2f}%" for m in top_movers[:5]])

    prompt = f"""You are a senior market strategist writing the morning brief.

TODAY'S PRE-MARKET SNAPSHOT:
SPY: {spy:+.2f}%, QQQ: {qqq:+.2f}%, VIX: {vix:.1f}

TOP MOVERS (pre-market):
{mover_lines}

SECTOR PERFORMANCE:
{sec_lines}

Write EXACTLY 4 short lines (max 110 chars each):
🌅 [Today's setup: risk-on/risk-off/mixed — why, in 1 sentence]
🎯 [Sectors/themes to favor today]
⚠️ [Main risk / what to avoid]
💡 [Actionable bias: trade aggressive/selective/defensive]

No extra headers, bullets, or intros. 4 lines only."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        r = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 400}
        }, timeout=20)
        if r.status_code == 200:
            data = r.json()
            if 'candidates' in data and len(data['candidates']) > 0:
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        logging.error(f"AI outlook: {e}")
    return None

# ═══════════════════════════════════════════════
# BRIEF BUILDER
# ═══════════════════════════════════════════════

def build_morning_brief():
    print(f"\n📊 Building Morning Brief @ {now_est().strftime('%H:%M %Z')}")

    market_ctx = get_market_ctx()
    all_contexts = {}

    # Fetch all symbols
    for symbol in MONITOR_LIST:
        try:
            print(f"  → {symbol:10s}...", end=" ")
            ctx = get_full_context(symbol)
            time.sleep(0.3)
            if ctx:
                all_contexts[symbol] = ctx
                print(f"{ctx['day_change_pct']:+.2f}%")
            else:
                print("—")
        except Exception as e:
            print(f"💥 {e}")

    if not all_contexts:
        logging.error("No contexts fetched for morning brief")
        return

    # Compute sector averages
    sector_moves = []
    for sector, syms in SECTORS.items():
        ctxs = [all_contexts[s] for s in syms if s in all_contexts]
        if ctxs:
            avg = sum(c['day_change_pct'] for c in ctxs) / len(ctxs)
            sector_moves.append((sector, avg))
    sector_moves.sort(key=lambda x: -x[1])

    # Top gainers/losers
    sorted_all = sorted(all_contexts.values(), key=lambda c: -c['day_change_pct'])
    top_gainers = [{'symbol': c['symbol'], 'pct': c['day_change_pct']} for c in sorted_all[:5] if c['day_change_pct'] > 0]
    top_losers = [{'symbol': c['symbol'], 'pct': c['day_change_pct']} for c in sorted_all[-5:] if c['day_change_pct'] < 0]
    top_losers.reverse()

    # Today's earnings
    earnings_today = []
    for sym, ctx in all_contexts.items():
        ed, days = get_earnings_date(sym)
        if ed and days is not None and days <= 1:
            earnings_today.append((sym, ed, days))

    # AI outlook
    ai_outlook = ai_daily_outlook(
        market_ctx,
        sector_moves,
        top_gainers + top_losers
    )

    # ═══════════════════════════════════════════════
    # BUILD MESSAGE
    # ═══════════════════════════════════════════════
    now = now_est()
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%A, %B %d • %I:%M %p {tz}')

    msg = f"🌅 *MORNING BRIEF*\n"
    msg += f"🕒 {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # Market snapshot
    if market_ctx:
        spy = market_ctx.get('SPY', {})
        qqq = market_ctx.get('QQQ', {})
        vix = market_ctx.get('^VIX', {})
        msg += f"*🌍 MARKET SNAPSHOT*\n"
        msg += f"`─────────────────`\n"
        def _row(lbl, d):
            if not d: return f"{lbl}: —\n"
            em = "🟢" if d.get('pct', 0) >= 0 else "🔴"
            return f"{lbl}: {em} `${d.get('price', 0):.2f}` ({d.get('pct', 0):+.2f}%)\n"
        msg += _row("SPY ", spy)
        msg += _row("QQQ ", qqq)
        msg += _row("VIX ", vix)

    # AI outlook
    if ai_outlook:
        msg += f"\n*🤖 TODAY'S OUTLOOK*\n"
        msg += f"`─────────────────`\n"
        msg += f"{ai_outlook}\n"

    # Earnings today/tomorrow
    if earnings_today:
        msg += f"\n*📅 EARNINGS ALERT*\n"
        msg += f"`─────────────────`\n"
        for sym, ed, days in earnings_today:
            em = SYMBOL_EMOJI.get(sym, '📊')
            when = "TODAY" if days == 0 else "TOMORROW"
            msg += f"  {em} *{sym}* — Earnings {when}\n"
        msg += f"_Avoid new entries. Existing positions: consider hedging._\n"

    # Sector performance
    msg += f"\n*🌡️ SECTOR PERFORMANCE*\n"
    msg += f"`─────────────────`\n"
    for sector, avg in sector_moves:
        em = "🚀" if avg > 2 else "🟢" if avg > 0.5 else "⚖️" if avg > -0.5 else "🔴" if avg > -2 else "🩸"
        msg += f"{em} {sector}: `{avg:+.2f}%`\n"

    # Top movers
    if top_gainers:
        msg += f"\n*🚀 TOP GAINERS*\n"
        msg += f"`─────────────────`\n"
        for g in top_gainers[:5]:
            em = SYMBOL_EMOJI.get(g['symbol'], '📊')
            msg += f"  {em} {g['symbol']}: `{g['pct']:+.2f}%`\n"

    if top_losers:
        msg += f"\n*📉 TOP LOSERS*\n"
        msg += f"`─────────────────`\n"
        for l in top_losers[:5]:
            em = SYMBOL_EMOJI.get(l['symbol'], '📊')
            msg += f"  {em} {l['symbol']}: `{l['pct']:+.2f}%`\n"

    # Watchlist verdicts (top 8 BUY ZONE candidates)
    msg += f"\n*🎯 BUY ZONE CANDIDATES*\n"
    msg += f"`─────────────────`\n"
    buy_candidates = []
    for sym, ctx in all_contexts.items():
        verdict, zone, reasons = get_verdict(ctx, market_ctx)
        if "BUY" in verdict:
            buy_candidates.append((sym, ctx, verdict, zone, reasons))

    if buy_candidates:
        for sym, ctx, verdict, zone, reasons in buy_candidates[:8]:
            em = SYMBOL_EMOJI.get(sym, '📊')
            msg += f"  {em} *{sym}* @ `${ctx['current']:.2f}` — _{zone}_\n"
            if reasons:
                msg += f"     💡 {reasons[0]}\n"
    else:
        msg += f"  _No clean buy setups — wait for better conditions_\n"

    # Avoid / caution list
    avoid = []
    for sym, ctx in all_contexts.items():
        verdict, zone, _ = get_verdict(ctx, market_ctx)
        if "AVOID" in verdict or "WAIT" in verdict:
            avoid.append((sym, ctx, verdict, zone))

    if avoid:
        msg += f"\n*🚫 AVOID / WAIT*\n"
        msg += f"`─────────────────`\n"
        for sym, ctx, verdict, zone in avoid[:6]:
            em = SYMBOL_EMOJI.get(sym, '📊')
            msg += f"  {em} {sym}: _{zone}_\n"

    # Footer
    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Full scans running every 10-15 min during market hours._"

    # Send
    if send_telegram(msg, silent=False):
        print("✅ Morning brief sent")
        logging.info("Morning brief sent")
    else:
        print("❌ Failed to send brief")

def should_send_brief():
    state = load_json(STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    if state.get('last_morning_brief') == today:
        return False
    state['last_morning_brief'] = today
    save_json(STATE_FILE, state)
    return True

if __name__ == "__main__":
    # Allow manual invocation any time; cron handles timing
    if should_send_brief() or os.environ.get('FORCE_BRIEF'):
        build_morning_brief()
    else:
        print("ℹ️  Brief already sent today")
