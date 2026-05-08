"""
ALPHAEDGE MORNING BRIEF v3.0 — MORNING + EVENING
═══════════════════════════════════════════════════════════════
Fired twice every weekday:
  🌅 9:00 AM ET  — Morning Brief (day setup)
  🌆 4:30 PM ET  — Evening Brief (day recap + after-hours watch)

MORNING BRIEF delivers:
• Market snapshot (SPY/QQQ/VIX)
• AI-powered daily outlook
• Earnings-today alerts
• Sector performance heatmap
• Top gainers / losers
• Buy Zone candidates with verdicts
• Avoid list

EVENING BRIEF delivers:
• Day close recap (SPY/QQQ/VIX final)
• How open scanner trades finished
• Sector close heatmap
• After-hours movers worth watching
• Earnings tonight / tomorrow warning
• AI end-of-day summary + overnight bias

v3.0 CHANGES vs v2.0:
• Evening brief added (fires at 4:30 PM ET)
• Shared gate logic — morning and evening each fire once per day
• FORCE_BRIEF env var works for both (FORCE_MORNING / FORCE_EVENING added)
• Weekend guard applies to both
• Auto-split for long briefs (inherited)
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

TRADES_FILE = 'active_trades.json'
HISTORY_FILE = 'trade_history.json'


# ═══════════════════════════════════════════════
# AI — MORNING OUTLOOK
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
            if data.get('candidates'):
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
        elif r.status_code == 429:
            logging.warning("Gemini rate-limited (morning)")
        else:
            logging.error(f"Gemini morning: {r.status_code} {r.text[:150]}")
    except Exception as e:
        logging.error(f"AI morning: {e}")
    return None


# ═══════════════════════════════════════════════
# AI — EVENING SUMMARY
# ═══════════════════════════════════════════════

def ai_evening_summary(market_ctx, sector_summary, day_winners, day_losers, open_trades):
    if not GEMINI_API_KEY:
        return None

    mkt = market_ctx or {}
    spy = mkt.get('SPY', {}).get('pct', 0)
    qqq = mkt.get('QQQ', {}).get('pct', 0)
    vix = mkt.get('^VIX', {}).get('price', 15)

    sec_lines = "\n".join([f"  {name}: {avg:+.2f}%" for name, avg in sector_summary[:6]])
    winner_lines = "\n".join([f"  {w['symbol']}: {w['pct']:+.2f}%" for w in day_winners[:4]])
    loser_lines = "\n".join([f"  {l['symbol']}: {l['pct']:+.2f}%" for l in day_losers[:4]])
    trade_lines = f"{len(open_trades)} trade(s) still open going into after-hours" if open_trades else "No open trades"

    prompt = f"""You are a senior trading strategist writing the end-of-day brief for an active trader.

TODAY'S CLOSE:
SPY: {spy:+.2f}%, QQQ: {qqq:+.2f}%, VIX: {vix:.1f}

DAY LEADERS: {winner_lines if winner_lines else 'none'}
DAY LAGGARDS: {loser_lines if loser_lines else 'none'}

SECTOR CLOSE:
{sec_lines}

OPEN POSITIONS: {trade_lines}

Write EXACTLY 4 lines (max 120 chars each). Be direct.

🌆 [Day recap: what drove price — sector rotation, macro, momentum?]
🎯 [What set up well today — note any themes for tomorrow]
⚠️ [Overnight risk — what to watch AH / pre-market tomorrow]
💡 [Overnight bias: cautious / hold / reduce — brief reason]

NO extra headers, bullets, intros, or outros. 4 lines only."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        r = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 400}
        }, timeout=20)
        if r.status_code == 200:
            data = r.json()
            if data.get('candidates'):
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
        elif r.status_code == 429:
            logging.warning("Gemini rate-limited (evening)")
        else:
            logging.error(f"Gemini evening: {r.status_code} {r.text[:150]}")
    except Exception as e:
        logging.error(f"AI evening: {e}")
    return None


# ═══════════════════════════════════════════════
# MORNING BRIEF BUILDER (unchanged from v2.0)
# ═══════════════════════════════════════════════

def build_morning_brief():
    print(f"\n🌅 Building Morning Brief @ {now_est().strftime('%H:%M %Z')}")
    logging.info("Morning brief build start")

    now = now_est()
    if now.weekday() >= 5:
        print("⚠️ Weekend — no brief today")
        return False

    market_ctx = get_market_ctx()
    all_contexts = {}
    failed = 0

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
            logging.error(f"Morning fetch {symbol}: {e}")

    if not all_contexts:
        print("❌ No contexts — aborting morning brief")
        return False

    sector_moves = []
    for sector, syms in SECTORS.items():
        ctxs = [all_contexts[s] for s in syms if s in all_contexts]
        if ctxs:
            avg = sum(c['day_change_pct'] for c in ctxs) / len(ctxs)
            sector_moves.append((sector, avg))
    sector_moves.sort(key=lambda x: -x[1])

    sorted_all = sorted(all_contexts.values(), key=lambda c: -c['day_change_pct'])
    top_gainers = [{'symbol': c['symbol'], 'pct': c['day_change_pct']}
                   for c in sorted_all[:5] if c['day_change_pct'] > 0]
    top_losers = [{'symbol': c['symbol'], 'pct': c['day_change_pct']}
                  for c in sorted_all[-5:] if c['day_change_pct'] < 0]
    top_losers.reverse()

    earnings_flag = []
    earnings_syms = set()
    for sym in all_contexts:
        ed, days = get_earnings_date(sym)
        if ed and days is not None and days <= 1:
            earnings_flag.append((sym, ed, days))
            earnings_syms.add(sym)

    print("  🤖 Getting AI morning outlook...")
    ai_outlook = ai_daily_outlook(market_ctx, sector_moves, top_gainers + top_losers)

    buy_candidates = []
    avoid_list = []
    for sym, ctx in all_contexts.items():
        verdict, zone, reasons = get_verdict(ctx, market_ctx)
        if "BUY" in verdict:
            buy_candidates.append((sym, ctx, verdict, zone, reasons))
        elif "AVOID" in verdict or "WAIT" in verdict:
            if sym not in earnings_syms:
                avoid_list.append((sym, ctx, verdict, zone))

    buy_candidates.sort(key=lambda x: x[1]['rsi'])

    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%A, %B %d • %I:%M %p {tz}')

    msg = f"🌅 *MORNING BRIEF*\n"
    msg += f"🕒 {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    if market_ctx:
        spy = market_ctx.get('SPY', {})
        qqq = market_ctx.get('QQQ', {})
        vix = market_ctx.get('^VIX', {})
        msg += f"*🌍 MARKET SNAPSHOT*\n`─────────────────`\n"
        def _row(lbl, d):
            if not d: return f"{lbl}: —\n"
            em = "🟢" if d.get('pct', 0) >= 0 else "🔴"
            return f"{lbl}: {em} `${d.get('price', 0):.2f}` ({d.get('pct', 0):+.2f}%)\n"
        msg += _row("SPY ", spy)
        msg += _row("QQQ ", qqq)
        msg += _row("VIX ", vix)
        vix_price = vix.get('price', 15)
        spy_pct = spy.get('pct', 0)
        if vix_price > 25:
            msg += f"\n⚠️ _Elevated VIX — expect chop & risk-off_\n"
        elif vix_price < 14 and spy_pct > 0.3:
            msg += f"\n✅ _Low vol + uptick — clean trend environment_\n"

    if ai_outlook:
        msg += f"\n*🤖 TODAY'S OUTLOOK*\n`─────────────────`\n"
        msg += f"{ai_outlook}\n"

    if earnings_flag:
        msg += f"\n*📅 EARNINGS ALERT*\n`─────────────────`\n"
        for sym, ed, days in earnings_flag:
            em = SYMBOL_EMOJI.get(sym, '📊')
            when = "TODAY" if days == 0 else "TOMORROW"
            msg += f"  {em} *{sym}* — Earnings {when}\n"
        msg += f"_Avoid new entries. Existing positions: consider hedging._\n"

    msg += f"\n*🌡️ SECTOR PERFORMANCE*\n`─────────────────`\n"
    for sector, avg in sector_moves:
        if avg > 2:       em = "🚀"
        elif avg > 0.5:   em = "🟢"
        elif avg > -0.5:  em = "⚖️"
        elif avg > -2:    em = "🔴"
        else:             em = "🩸"
        msg += f"{em} {sector}: `{avg:+.2f}%`\n"

    if top_gainers or top_losers:
        msg += f"\n*📊 TOP MOVERS*\n`─────────────────`\n"
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

    msg += f"\n*🎯 BUY ZONE CANDIDATES*\n`─────────────────`\n"
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

    if avoid_list:
        msg += f"\n*🚫 AVOID / WAIT*\n`─────────────────`\n"
        shown = min(6, len(avoid_list))
        for sym, ctx, verdict, zone in avoid_list[:shown]:
            em = SYMBOL_EMOJI.get(sym, '📊')
            msg += f"  {em} {sym}: _{zone}_\n"
        if len(avoid_list) > shown:
            msg += f"  _+{len(avoid_list) - shown} more_\n"

    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Scanners running every 10-15 min during market hours._\n"
    msg += f"_Watch for 🩸 sector bleed, 💪 RS signals, 🎯 dip alerts._"

    success = send_telegram(msg, silent=False)
    if success:
        print(f"✅ Morning brief sent ({len(msg)} chars)")
        logging.info(f"Morning brief sent | chars={len(msg)} | candidates={len(buy_candidates)}")
    else:
        print("❌ Failed to send morning brief")
        logging.error("Morning brief send failed")
    return success


# ═══════════════════════════════════════════════
# EVENING BRIEF BUILDER
# ═══════════════════════════════════════════════

def build_evening_brief():
    print(f"\n🌆 Building Evening Brief @ {now_est().strftime('%H:%M %Z')}")
    logging.info("Evening brief build start")

    now = now_est()
    if now.weekday() >= 5:
        print("⚠️ Weekend — no brief today")
        return False

    market_ctx = get_market_ctx()
    all_contexts = {}

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
        except Exception as e:
            print(f"💥 {e}")
            logging.error(f"Evening fetch {symbol}: {e}")

    if not all_contexts:
        print("❌ No contexts — aborting evening brief")
        return False

    # Sector close
    sector_moves = []
    for sector, syms in SECTORS.items():
        ctxs = [all_contexts[s] for s in syms if s in all_contexts]
        if ctxs:
            avg = sum(c['day_change_pct'] for c in ctxs) / len(ctxs)
            sector_moves.append((sector, avg))
    sector_moves.sort(key=lambda x: -x[1])

    # Day's best/worst
    sorted_all = sorted(all_contexts.values(), key=lambda c: -c['day_change_pct'])
    day_winners = [{'symbol': c['symbol'], 'pct': c['day_change_pct']}
                   for c in sorted_all[:5] if c['day_change_pct'] > 0]
    day_losers = [{'symbol': c['symbol'], 'pct': c['day_change_pct']}
                  for c in sorted_all[-5:] if c['day_change_pct'] < 0]
    day_losers.reverse()

    # Open trades from scanner
    open_trades = {}
    try:
        all_trades = load_json(TRADES_FILE, {})
        open_trades = {k: v for k, v in all_trades.items() if not v.get('closed')}
    except Exception as e:
        logging.error(f"Evening trades load: {e}")

    # Today's closed trades from history
    closed_today = []
    try:
        history = load_json(HISTORY_FILE, [])
        today_str = now.strftime('%Y-%m-%d')
        for t in history:
            ca = t.get('closed_at', '')
            if ca and ca.startswith(today_str):
                closed_today.append(t)
    except Exception as e:
        logging.error(f"Evening history load: {e}")

    # Earnings tonight / tomorrow
    earnings_soon = []
    for sym in all_contexts:
        ed, days = get_earnings_date(sym)
        if ed and days is not None and days <= 1:
            earnings_soon.append((sym, ed, days))

    # After-hours movers (symbols with significant AH move)
    ah_movers = []
    for sym, ctx in all_contexts.items():
        try:
            df = yf.download(sym, period='1d', interval='5m',
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            close_4pm = float(df['Close'].iloc[-78]) if len(df) >= 78 else float(df['Close'].iloc[0])
            ah_price = float(df['Close'].iloc[-1])
            ah_pct = (ah_price - close_4pm) / close_4pm * 100 if close_4pm > 0 else 0
            if abs(ah_pct) >= 0.5:
                ah_movers.append({'symbol': sym, 'pct': ah_pct, 'price': ah_price})
            time.sleep(FETCH_DELAY)
        except Exception:
            pass
    ah_movers.sort(key=lambda x: -abs(x['pct']))

    print("  🤖 Getting AI evening summary...")
    ai_summary = ai_evening_summary(
        market_ctx, sector_moves, day_winners, day_losers, open_trades
    )

    # ─── Build message ───
    tz = now.tzname() or "EDT"
    ts = now.strftime(f'%A, %B %d • %I:%M %p {tz}')

    msg = f"🌆 *EVENING BRIEF*\n"
    msg += f"🕒 {ts}\n"
    msg += f"`━━━━━━━━━━━━━━━━━━━━━`\n\n"

    # Market close
    if market_ctx:
        spy = market_ctx.get('SPY', {})
        qqq = market_ctx.get('QQQ', {})
        vix = market_ctx.get('^VIX', {})
        msg += f"*🔔 DAY CLOSE*\n`─────────────────`\n"
        def _row(lbl, d):
            if not d: return f"{lbl}: —\n"
            em = "🟢" if d.get('pct', 0) >= 0 else "🔴"
            return f"{lbl}: {em} `${d.get('price', 0):.2f}` ({d.get('pct', 0):+.2f}%)\n"
        msg += _row("SPY ", spy)
        msg += _row("QQQ ", qqq)
        msg += _row("VIX ", vix)

    # AI summary
    if ai_summary:
        msg += f"\n*🤖 END-OF-DAY ANALYSIS*\n`─────────────────`\n"
        msg += f"{ai_summary}\n"

    # Open scanner trades
    if open_trades:
        msg += f"\n*📊 OPEN TRADES ({len(open_trades)})*\n`─────────────────`\n"
        msg += f"_Still active going into after-hours:_\n"
        for k, t in open_trades.items():
            em = t.get('emoji', '📈')
            dir_em = "🟢" if t['signal'] == 'BUY' else "🔴"
            sym = t['symbol'].replace('_', r'\_')
            msg += f"  {em} {dir_em} *{sym}* `{t.get('tf_label', t['tf'])}` "
            msg += f"@ `${t['entry']:.2f}` — SL `${t['sl']:.2f}`\n"
        msg += f"_Use LIMIT orders in after-hours. Watch for gap risk overnight._\n"
    else:
        msg += f"\n📊 _No open trades going into after-hours._\n"

    # Trades closed today
    if closed_today:
        wins = [t for t in closed_today if (t.get('final_r') or 0) > 0]
        losses = [t for t in closed_today if (t.get('final_r') or 0) < 0]
        msg += f"\n*✅ TODAY'S CLOSED TRADES*\n`─────────────────`\n"
        msg += f"Closed: *{len(closed_today)}* • Wins: *{len(wins)}* • Losses: *{len(losses)}*\n"
        total_r = sum((t.get('final_r') or 0) for t in closed_today)
        r_em = "🟢" if total_r >= 0 else "🔴"
        msg += f"{r_em} Day P&L: *{total_r:+.1f}R*\n"

    # Sector close heatmap
    msg += f"\n*🌡️ SECTOR CLOSE*\n`─────────────────`\n"
    for sector, avg in sector_moves:
        if avg > 2:       em = "🚀"
        elif avg > 0.5:   em = "🟢"
        elif avg > -0.5:  em = "⚖️"
        elif avg > -2:    em = "🔴"
        else:             em = "🩸"
        msg += f"{em} {sector}: `{avg:+.2f}%`\n"

    # Day's best/worst
    if day_winners or day_losers:
        msg += f"\n*🏆 DAY MOVERS*\n`─────────────────`\n"
        if day_winners:
            msg += f"🚀 _Winners:_\n"
            for w in day_winners[:5]:
                em = SYMBOL_EMOJI.get(w['symbol'], '📊')
                msg += f"  {em} {w['symbol']}: `{w['pct']:+.2f}%`\n"
        if day_losers:
            msg += f"📉 _Losers:_\n"
            for l in day_losers[:5]:
                em = SYMBOL_EMOJI.get(l['symbol'], '📊')
                msg += f"  {em} {l['symbol']}: `{l['pct']:+.2f}%`\n"

    # After-hours movers
    if ah_movers:
        msg += f"\n*🌙 AFTER-HOURS MOVERS*\n`─────────────────`\n"
        for m in ah_movers[:6]:
            em = SYMBOL_EMOJI.get(m['symbol'], '📊')
            dir_em = "🟢" if m['pct'] > 0 else "🔴"
            msg += f"  {dir_em} {em} {m['symbol']}: `{m['pct']:+.2f}%` AH @ `${m['price']:.2f}`\n"
    else:
        msg += f"\n🌙 _No significant after-hours moves._\n"

    # Earnings tonight / tomorrow
    if earnings_soon:
        msg += f"\n*📅 EARNINGS WATCH*\n`─────────────────`\n"
        for sym, ed, days in earnings_soon:
            em = SYMBOL_EMOJI.get(sym, '📊')
            when = "TONIGHT" if days == 0 else "TOMORROW"
            msg += f"  {em} *{sym}* — Reports {when}\n"
        msg += f"_Consider reducing exposure before report._\n"

    # Footer
    msg += f"\n`━━━━━━━━━━━━━━━━━━━━━`\n"
    msg += f"_Scanner continues in after-hours (crypto + ext-hrs stocks)._\n"
    msg += f"_Next morning brief: tomorrow at 9:00 AM ET._"

    success = send_telegram(msg, silent=False)
    if success:
        print(f"✅ Evening brief sent ({len(msg)} chars)")
        logging.info(f"Evening brief sent | chars={len(msg)} | open_trades={len(open_trades)}")
    else:
        print("❌ Failed to send evening brief")
        logging.error("Evening brief send failed")
    return success


# ═══════════════════════════════════════════════
# GATE LOGIC
# ═══════════════════════════════════════════════

def should_send_morning():
    state = load_json(STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    return state.get('last_morning_brief') != today

def mark_morning_sent():
    state = load_json(STATE_FILE, {})
    state['last_morning_brief'] = now_est().strftime('%Y-%m-%d')
    save_json(STATE_FILE, state)

def should_send_evening():
    state = load_json(STATE_FILE, {})
    today = now_est().strftime('%Y-%m-%d')
    return state.get('last_evening_brief') != today

def mark_evening_sent():
    state = load_json(STATE_FILE, {})
    state['last_evening_brief'] = now_est().strftime('%Y-%m-%d')
    save_json(STATE_FILE, state)


# ═══════════════════════════════════════════════
# MAIN — dispatches based on current hour
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    force_morning = os.environ.get('FORCE_MORNING', '').lower() in ('true', '1', 'yes')
    force_evening = os.environ.get('FORCE_EVENING', '').lower() in ('true', '1', 'yes')
    force_brief   = os.environ.get('FORCE_BRIEF', '').lower() in ('true', '1', 'yes')

    now = now_est()
    hour = now.hour + now.minute / 60   # e.g. 16.5 = 4:30 PM

    # Determine which brief to run
    # Morning: 8:45 AM – 12:00 PM  (cron fires at 9:00 AM)
    # Evening: 4:15 PM – 8:00 PM   (cron fires at 4:30 PM)
    is_morning_window = 8.75 <= hour < 12.0
    is_evening_window = 16.25 <= hour < 20.0

    if force_morning or (force_brief and is_morning_window):
        success = build_morning_brief()
        if success:
            mark_morning_sent()

    elif force_evening or (force_brief and is_evening_window):
        success = build_evening_brief()
        if success:
            mark_evening_sent()

    elif is_morning_window:
        if should_send_morning():
            success = build_morning_brief()
            if success:
                mark_morning_sent()
        else:
            print(f"ℹ️  Morning brief already sent today ({now.strftime('%Y-%m-%d')})")

    elif is_evening_window:
        if should_send_evening():
            success = build_evening_brief()
            if success:
                mark_evening_sent()
        else:
            print(f"ℹ️  Evening brief already sent today ({now.strftime('%Y-%m-%d')})")

    else:
        print(f"ℹ️  Outside brief windows (hour={hour:.1f}). "
              f"Use FORCE_MORNING=true or FORCE_EVENING=true to override.")
