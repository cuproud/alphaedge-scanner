"""
ALPHAEDGE MORNING BRIEF v2.0 — AUDITED
═══════════════════════════════════════════════════════════════
Fired once every weekday at 9 AM ET.
Delivers the day's setup:
• Market snapshot (SPY/QQQ/VIX)
• AI-powered daily outlook
• Earnings-today alerts
• Sector performance heatmap
• Top gainers / losers
• Buy Zone candidates with verdicts
• Avoid list

v2.0 FIXES:
• Only ONE cron fires per day (dispatches once based on actual EST hour)
• should_send_brief only sets flag AFTER successful send
• Weekend guard (workflow_dispatch safe)
• Auto-split for long briefs
• Dedup between earnings list and avoid list
• Sector movers shown only once (no redundant listing)
• Truncation indicator on lists
"""

import os
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import yfinance as yf
import pandas as pd
import requests

from market_intel import (
    get_full_context, get_verdict, get_market_ctx, calc_relative_strength,
    get_earnings_date, format_earnings_warning,
    MONITOR_LIST, SYMBOL_EMOJI, SECTORS, send_telegram,
    now_est, load_json, save_json, STATE_FILE
)

EST = ZoneInfo("America/New_York")
LOGS_DIR = Path('logs')
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / f'brief_{now_est().strftime("%Y-%m-%d")}.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

FETCH_DELAY = 0.25

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

    sec_lines = "\n".join([f"  {name}: {avg:+.2f}%" for name, avg in sector_summary[:6]])
    mover_lines = "\n".join([f"  {m['symbol']}: {m['pct']:+.2f}%" for m in top_movers[:6]])

    prompt = f"""You are a senior trading strategist writing the morning brief for an active trader.

TODAY'S PRE-MARKET SNAPSHOT:
SPY: {spy:+.2f}%, QQQ: {qqq:+.2f}%, VIX: {vix:.1f}

TOP MOVERS (pre-market):
{mover_lines}

SECTOR PERFORMANCE:
{sec_lines}

Write EXACTLY 4 lines (max 120 chars each). Be direct and actionable.

🌅 [Setup: risk-on/risk-off/mixed — briefly why]
🎯 [Which sectors/themes to favor today — be specific with tickers if relevant]
⚠️ [Main risk / what to avoid — pinpoint it]
💡 [Bias: trade aggressive / selective / defensive — with size guidance]

NO extra headers, bullets, intros, or outros. 4 lines only."""

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
        elif r.status_code == 429:
            logging.warning("Gemini rate-limited for brief")
        else:
            logging.error(f"Gemini brief: {r.status_code} {r.text[:150]}")
    except Exception as e:
        logging.error(f"AI brief: {e}")
    return None

# ═══════════════════════════════════════════════
# BRIEF BUILDER
# ═══════════════════════════════════════════════

def build_morning_brief():
    print(f"\n📊 Building Morning Brief @ {now_est().strftime('%H:%M %Z')}")
    logging.info("Brief build start")

    # Weekend guard
    now = now_est()
    if now.weekday() >= 5:
        print("⚠️ Weekend — no brief today")
        logging.info("Brief skipped (weekend)")
        return False

    market_ctx = get_market_ctx()
    all_contexts = {}
    failed = 0

    # Fetch all contexts
    for symbol in MONITOR_LIST:
        try:
            print(f"  → {symbol:10s}...", end=" ", flush=True)
            ctx = get_full_context(symbol)
            time.sleep(FETCH_DELAY)
            if ctx:
                all_contexts[symbol] = ctx
                print(f"{ctx['day_change_pct']:+.2f}%")
            else:
                print("—")
                failed += 1
        except Exception as e:
            print(f"💥 {e}")
            failed += 1
            logging.error(f"Brief fetch {symbol}: {e}")

    if not all_contexts:
        print("❌ No contexts — aborting brief")
        logging.error("Brief aborted — no contexts")
        return False

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
    top_gainers = [{'symbol': c['symbol'], 'pct': c['day_change_pct']}
                   for c in sorted_all[:5] if c['day_change_pct'] > 0]
    top_losers = [{'symbol': c['symbol'], 'pct': c['day_change_pct']}
                  for c in sorted_all[-5:] if c['day_change_pct'] < 0]
    top_losers.reverse()

    # Today's/tomorrow's earnings
    earnings_flag = []
    earnings_syms = set()
    for sym in all_contexts:
        ed, days = get_earnings_date(sym)
        if ed and days is not None and days <= 1:
            earnings_flag.append((sym, ed, days))
            earnings_syms.add(sym)

    # AI outlook
    print("  🤖 Getting AI outlook...")
    ai_outlook = ai_daily_outlook(market_ctx, sector_moves, top_gainers + top_losers)

    # Build verdicts
    buy_candidates = []
    avoid_list = []
    for sym, ctx in all_contexts.items():
        verdict, zone, reasons = get_verdict(ctx, market_ctx)
        if "BUY" in verdict:
            buy_candidates.append((sym, ctx, verdict, zone, reasons))
        elif "AVOID" in verdict or "WAIT" in verdict:
            # Skip symbols already in earnings list (avoid duplication)
            if sym not in earnings_syms:
                avoid_list.append((sym, ctx, verdict, zone))

    # Sort buy candidates by RSI (most oversold = best)
    buy_candidates.sort(key=lambda x: x[1]['rsi'])

    # ═══════════════════════════════════════════════
    # BUILD MESSAGE
    # ═══════════════════════════════════════════════
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
            if not d:
                return f"{lbl}: —\n"
            em = "🟢" if d.get('pct', 0) >= 0 else "🔴"
            return f"{lbl}: {em} `${d.get('price', 0):.2f}` ({d.get('pct', 0):+.2f}%)\n"
        msg += _row("SPY ", spy)
        msg += _row("QQQ ", qqq)
        msg += _row("VIX ", vix)

        # Interpretation
        vix_price = vix.get('price', 15)
        spy_pct = spy.get('pct', 0)
        if vix_price > 25:
            msg += f"\n⚠️ _Elevated VIX — expect chop & risk-off_\n"
        elif vix_price < 14 and spy_pct > 0.3:
            msg += f"\n✅ _Low vol + uptick — clean trend environment_\n"

    # AI outlook
    if ai_outlook:
        msg += f"\n*🤖 TODAY'S OUTLOOK*\n"
        msg += f"`─────────────────`\n"
        msg += f"{ai_outlook}\n"

    # Earnings alert
    if earnings_flag:
        msg += f"\n*📅 EARNINGS ALERT*\n"
        msg += f"`─────────────────`\n"
        for sym, ed, days in earnings_flag:
            em = SYMBOL_EMOJI.get(sym, '📊')
            when = "TODAY" if days == 0 else "TOMORROW"
            msg += f"  {em} *{sym}* — Earnings {when}\n"
        msg += f"_Avoid new entries. Existing positions: consider hedging._\n"

    # Sector performance
    msg += f"\n*🌡️ SECTOR PERFORMANCE*\n"
    msg += f"`─────────────────`\n"
    for sector, avg in sector_moves:
        if avg > 2: em = "🚀"
        elif avg > 0.5: em = "🟢"
        elif avg > -0.5: em = "⚖️"
        elif avg > -2: em = "🔴"
        else: em = "🩸"
        msg += f"{em} {sector}: `{avg:+.2f}%`\n"

    # Top movers (consolidated — gainers + losers in one block)
    if top_gainers or top_losers:
        msg += f"\n*📊 TOP MOVERS*\n"
        msg += f"`─────────────────`\n"
        if top_gainers:
            msg += f"🚀 _Gainers:_\n"
            for g in top_gainers[:5]:
                em = SYMBOL_EMOJI.get(g['symbol'], '📊')
                msg += f"  {em} {g['symbol']}: `{g['pct']:+.2f}%`\n"
        if top_losers:
            msg += f"📉 _Losers:_\n"
            for l in top_losers[:5]:
                em = SYMBOL_EMOJI.get(l['symbol'], '📊')
                msg += f"  {em} {l['symbol']}: `{l['pct']:+.2f}%`\n"

    # Buy candidates
    msg += f"\n*🎯 BUY ZONE CANDIDATES*\n"
    msg += f"`─────────────────`\n"
    if buy_candidates:
        shown = min(8, len(buy_candidates))
        for sym, ctx, verdict, zone, reasons in buy_candidates[:shown]:
            em = SYMBOL_EMOJI.get(sym, '📊')
            msg += f"  {em} *{sym}* @ `${ctx['current']:.2f}` — _{zone}_\n"
            msg += f"     RSI `{ctx['rsi']:.0f}` • {ctx['day_change_pct']:+.2f}% today\n"
            if reasons:
                msg += f"     💡 {reasons[0]}\n"
        if len(buy_candidates) > shown:
            msg += f"  _+{len(buy_candidates) - shown} more buy candidates_\n"
    else:
        msg += f"  _No clean buy setups — wait for better conditions_\n"

    # Avoid list
    if avoid_list:
        msg += f"\n*🚫 AVOID / WAIT*\n"
        msg += f"`─────────────────`\n"
        shown = min(6, len(avoid_list))
        for sym, ctx, verdict, zone in avoid_list[:shown]:
            em = SYMBOL_EMOJI.get(sym, '📊')
            msg += f"  {em} {sym}: _{zone}_\n"
        if len(avoid_list) > shown:
            msg += f"  _+{len(avoid_list) - shown} more_\n"

    # Footer
    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Scanners running every 10-15 min during market hours._\n"
    msg += f"_Watch for 🩸 sector bleed, 💪 RS signals, 🎯 dip alerts._"

    # send_telegram auto-splits long messages
    success = send_telegram(msg, silent=False)
    if success:
        print(f"✅ Morning brief sent ({len(msg)} chars)")
        logging.info(f"Brief sent | chars={len(msg)} | candidates={len(buy_candidates)}")
        return True
    else:
        print("❌ Failed to send brief")
        logging.error("Brief send failed")
        return False

# ═══════════════════════════════════════════════
# GATE LOGIC
# ═══════════════════════════════════════════════

def should_send_brief():
    """Returns True if brief can be sent today (hasn't already fired)."""
    state = load_json(STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    if state.get('last_morning_brief') == today:
        return False
    return True

def mark_brief_sent():
    state = load_json(STATE_FILE, {})
    state['last_morning_brief'] = now_est().strftime('%Y-%m-%d')
    save_json(STATE_FILE, state)

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    force = os.environ.get('FORCE_BRIEF', '').lower() in ('true', '1', 'yes')

    if should_send_brief() or force:
        success = build_morning_brief()
        if success:
            mark_brief_sent()
        else:
            print("⚠️ Brief failed — will retry next cron")
    else:
        print(f"ℹ️  Brief already sent today ({now_est().strftime('%Y-%m-%d')})")
