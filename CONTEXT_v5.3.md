Here's my context:
📋 AlphaEdge Scanner — Context for Next Chat
Copy-paste this entire block at the start of your next chat. Then paste your current scanner.py immediately after.

# AlphaEdge Trading Scanner — Project Context (v5.3)

I have a production Python trading scanner deployed on GitHub Actions that sends Telegram alerts. Full context below.

## 🎯 WHAT IT DOES

Scans 24 symbols across multi-timeframes for high-quality trading signals, sends rich Telegram alerts with AI analysis, tracks active signals from entry through TP/SL hits, and generates weekly performance summaries.

**Important:** This is a SIGNAL TRACKER / SCANNER, not an auto-trader. I do NOT execute trades from these alerts — I use them alongside my Pine Script indicator on charts. The scanner just tells me what's firing and tracks outcomes.

## 🏗️ ARCHITECTURE

**Stack:**
- Python 3.11
- yfinance (market data, free)
- Google Gemini 2.0 Flash API (free tier AI enrichment)
- GitHub Actions (free unlimited minutes — repo is PUBLIC)
- Telegram Bot API (alerts)
- zoneinfo for EST/EDT auto-handling

**Files:**
- `scanner.py` — v5.3 main scanner (will be pasted next)
- `.github/workflows/scanner.yml` — DST-aware cron schedule
- `requirements.txt` — yfinance, pandas, numpy, requests
- State files (cached between GitHub Actions runs):
  - `alert_cache.json` — signal cooldowns by tier
  - `active_trades.json` — open positions being tracked
  - `trade_history.json` — archived closed trades (capped at 500)
  - `scanner_state.json` — daily/weekly digest timestamps, position summary throttle
  - `logs/` — daily scan logs

**GitHub Secrets:**
- `TELEGRAM_TOKEN`, `CHAT_ID`, `GEMINI_API_KEY`

## 📊 WATCHLIST (24 symbols, session-aware)

**CRYPTO_WATCHLIST (24/7):**
BTC-USD, ETH-USD, XRP-USD, GC=F (gold futures)

**EXTENDED_HOURS_STOCKS (pre-market + after-hours OK):**
NVDA, TSLA, AMD, MSFT, META, AMZN, GOOGL, NFLX

**REGULAR_HOURS_ONLY (9:30 AM - 4:00 PM local only):**
MU, SNDK, NBIS, IONQ, RGTI, QBTS, OKLO, IREN, UAMY, WGRX, SOFI, NVO

## ⚙️ KEY CONFIG (v5.3)

```python
ACCOUNT_SIZE = 10000            # reference only
RISK_PCT = 1.0                  # reference only
SHOW_DOLLAR_AMOUNTS = False     # v5.3: R-multiples only in alerts, NO $ amounts

MIN_SQS = 60                    # signal quality threshold (0-100)
MIN_SCORE = 5                   # confluence threshold (0-10)
AI_TIER_THRESHOLD = 70          # Gemini fires only on SQS ≥ 70
FULL_DETAIL_SQS = 70            # digest mode: only SQS≥70 get full message
AFTER_HOURS_SQS_PENALTY = 5     # -5 SQS for stocks during extended hours

MAX_TRADE_AGE_HOURS = 72        # auto-close stale trades
MAX_SL_PCT_STOCKS = 0.04        # 4% max stop
MAX_SL_PCT_CRYPTO = 0.08        # 8% max stop
MIN_SL_PCT_STOCKS = 0.005       # 0.5% minimum (prevents noise stops)
MIN_SL_PCT_CRYPTO = 0.01        # 1% minimum (prevents noise stops like BTC 0.63%)
PRICE_SANITY_DEVIATION = 0.20   # reject if live price differs >20% from daily close

# Cooldown by SQS tier (hours)
COOLDOWN_ELITE = 2    # SQS 85+
COOLDOWN_STRONG = 4   # SQS 70-84
COOLDOWN_GOOD = 6     # SQS 55-69
COOLDOWN_FAIR = 10    # below 55

DIGEST_THRESHOLD = 4    # 4+ signals → digest mode instead of individual

# Multi-timeframe
TIMEFRAMES = [
    {'tf': '30m', 'lookback': '60d', 'label': '⚡30m', 'min_bars': 100},
    {'tf': '1h',  'lookback': '3mo', 'label': '📊1h',  'min_bars': 200},
]
🕐 TIMEZONE
Uses zoneinfo.ZoneInfo("America/New_York") for auto EST/EDT handling. All timestamps displayed in EDT/EST. Workflow crons run in UTC but cover both DST regimes.

🎯 SIGNAL LOGIC
Confluence Scoring (0-10): 10 indicators checked — EMA20, EMA50, EMA200, Supertrend, MACD, RSI>50, VWAP, ADX±DI, Volume, RSI direction.

Triggers:

Fresh Cross (EMA50/Supertrend/MACD just flipped)
Pullback (retest of EMA20 in established trend)
Oversold Bounce / Overbought Drop (RSI <32 / >68)
Trend Continuation (strong trend breaking recent highs/lows)
Strong Momentum (7+ confluence with ADX 22-55)
Smart Hard Blocks (context-aware — don't kill healthy trends):

RSI ≥ 80 blocked UNLESS strong uptrend confirmed
RSI ≤ 20 similar for bear
Parabolic (stretch >5× ATR) blocked unless strong trend
ADX ≥ 65 = trend exhaustion, always blocked
Counter-HTF momentum blocked
v5.3 NEW: Crypto Fresh-Cross requires trend confirmation (kills chop signals like weak BTC flips)
v5.3 NEW: Stocks in pre-market/after-hours get -5 SQS penalty (quality filter, not block)
SQS (Signal Quality Score 0-100):

Confluence: 40%
Regime fit (ADX): 15%
Volume: 10%
RSI fit (40-60 ideal): 10%
Trend alignment: 15%
Parabolic penalty: -10% if stretch >4
Tiers: 🏆 ELITE (85+) / ⭐ STRONG (70-84) / ✅ GOOD (55-69) / ⚠️ FAIR (<55)

📅 CRON SCHEDULE (DST-aware)
Regular market (9:30-4 local) — every 10 min, full watchlist
After-hours (4-8 PM local) — every 15 min, crypto + mega-caps
Pre-market (4-9:30 AM local) — every 15 min, crypto + mega-caps
Overnight (8 PM-4 AM local) — every 30 min, crypto only
Weekends — every 30 min, crypto only
Covers both EDT and EST windows so no DST-related gaps.

🤖 GEMINI AI INTEGRATION
Model: gemini-2.0-flash (free tier: 1,500 req/day)
Fires ONLY on signals with SQS ≥ 70
Returns 3 structured lines:
📝 Setup quality assessment
⚠️ Main risk factor
💡 Verdict (STRONG BUY/BUY/NEUTRAL/CAUTION/AVOID) — brief reason
Graceful failure: if API fails, signal still sent without AI
Usage: ~90 calls/day (6% of free tier limit)
📱 TELEGRAM ALERT TYPES
New Signal — Full trade plan with entry, SL (with tight/wide warnings), 3 TPs (shown as +1R/+2R/+3R), key levels (≥0.3% away only), technicals, absolute expiry time, session tips, AI analysis
Trade Events — TP1/TP2/TP3 hit (✅), SL hit (🛑), Timeout (⏰) — all in R-multiples
Price Ladder — Visual SL→Entry→TPs with hit markers
Market Context (daily 9 AM EDT weekdays) — SPY/QQQ/VIX with bias
Near-Miss Digest (daily 9 AM) — symbols with 7+ confluence but no trigger
Weekly Summary (Sunday 9 PM+) — win rate, total R, grade performance (NO dollar amounts)
Correlation Notice — when multiple signals/positions in same sector (informational, not blocking)
Open Positions Summary — max every 2 hours if 2+ positions open; sorted by R (best → worst); shows longs/shorts/TF breakdown
🎨 ALERT UI FEATURES (v5.3)
Symbol emojis (₿ BTC, 💎 NVDA, 🥇 gold, etc.)
SQS visual meter (🟢🟢🟢🟢🟡🟡⚪⚪⚪⚪)
Borderline warning if SQS 60-69
Tight stop warning (<1% away) / wide stop warning (>5%)
Nearby key levels (resistance, support, EMA50, EMA200) — ONLY if ≥0.3% distance
Absolute expiry time + relative: "Valid until: 19:09 EDT (59m)"
Session-specific trading tips (pre-market, after-hours warnings)
Multi-TF confirmation badge (🎯🎯 when symbol fires on both TFs)
Trade age counter
After-hours warning ⚠️ for thin liquidity
Trailing stop instructions on TP hits
All P&L in R-multiples (no dollars)
🛡️ SAFETY FEATURES
Price sanity check (rejects if live differs >20% from daily close)
Max SL caps (4% stocks, 8% crypto)
Min SL distance (0.5% stocks, 1% crypto) — prevents noise stops
Min SL distance (0.5× ATR minimum)
Structure-based SL (recent swing low/high - 0.2× ATR)
Rate limit delay (0.3s between symbol fetches)
Error recovery per-symbol (one failure doesn't kill scan)
Active trade check runs 24/7 regardless of session
Cooldown prevents duplicate alerts
🔗 CORRELATION GROUPS (informational only, not blocking)
CORRELATION_GROUPS = {
    'AI/Semis': ['NVDA', 'AMD', 'MU', 'SNDK', 'NBIS'],
    'Crypto': ['BTC-USD', 'ETH-USD', 'XRP-USD'],
    'Quantum': ['IONQ', 'RGTI', 'QBTS'],
    'Mega Tech': ['GOOGL', 'MSFT', 'META', 'AMZN'],
}
Alerts when 2+ signals/positions in same group (includes existing open trades for true exposure view). Kept intentionally loose — just a notice, doesn't block signals.

📈 v5.3 CHANGES (AUDIT RESULTS)
Bugs Fixed:

Removed ALL dollar/P&L calculations (R-multiples only throughout)
Fixed -0.00R ($-0) formatting → clean R display via fmt_r()
Fixed tight-stop noise (e.g., BTC 0.63% SL) → MIN_SL_PCT enforced
Removed dead price_progress() code
Fixed stale expiry display → absolute time "HH:MM TZ (relative)"
Fixed correlation to include open trades for true exposure
Fixed noise levels shown at 0.2% → filter ≥0.3% only
Logic Improvements (quality, NOT tightening):

Crypto Fresh-Cross needs trend confirmation (kills weak chop signals)
After-hours stock SQS penalty (-5) as quality tweak
Removed Per User Request:

All $ profit/P&L displays
Position size units/notional
Dollar risk displays
Max concurrent position cap (this is a tracker, not executor)
Dollar math in correlation
Intentionally Unchanged:

Correlation groups (left loose)
All signal trigger types
All filters (no additional tightening)
Cooldowns
Watchlist composition
🎯 VERSION HISTORY
v3.0 — Initial scanner with basic confluence
v4.0 — Added strict filters (blocked parabolic chases)
v4.1 — Balanced filters, near-miss diagnostics
v4.2 — Smart RSI (context-aware, allows healthy uptrends)
v5.0 — Multi-timeframe, position sizing, trade history, weekly summary
v5.1 — Session-aware watchlist
v5.2 — All bug fixes + UI polish (multi-TF badge, price ladder, key levels, expiry)
v5.3 (current) — Audited & polished: R-only (no $), min SL distance, smart crypto filters, cleaner UI
📜 COMPANION PINE SCRIPT
I also have an AlphaEdge Pine Script v6.3.2 indicator that runs on TradingView charts with identical signal logic. The Python scanner essentially automates that indicator's signal detection across multiple symbols with Telegram delivery.

Pine Script features: 16 themes, Signal Quality Score (SQS), Smart Re-Entry, Trade History log, Drawdown tracker, Session Volume Profile (NY/London/Asia), Liquidity Sweeps, Market Structure (BOS/CHoCH), Order Blocks, FVGs, Anchored VWAP, CVD, Multi-TF POC lines, Confluence Heatmap, Market Regime Badge.

📝 REQUEST FORMAT
When I paste this + my question, please:

Answer in context of this v5.3 scanner
Don't suggest changes that break existing features
Provide complete updated code blocks when making changes (no ... placeholders)
Note any dependencies/config that need updating
Call out potential side effects on existing features
Keep R-multiples only — NO dollar amounts in alerts
Don't add a position cap — this is a signal tracker, not an executor
Don't tighten correlation logic — keep it as informational only
💡 WHAT I MIGHT ASK FOR NEXT
Add new trigger types (breakout-only, volume spike, mean reversion)
Add new timeframe (15m or 4h)
Tune specific filters based on recent signal performance
Add backtest mode on historical data
Expand watchlist with new symbols
Add new correlation groups
Modify AI prompt
Add trade journal auto-export (Google Sheets/CSV)
Debug specific signal behavior
Change alert formatting
Add Discord alerts in parallel
Add EOD email digest
NEXT: I will paste the current scanner.py v5.3 code for continuation.
My question is:
Here's my current scanner.py:
