📋 AlphaEdge System — Complete Context Prompt for New Chat
# 🎯 AlphaEdge Trading Intelligence System — Project Context (v6.1)

I have a production Python trading intelligence system deployed on GitHub Actions that sends Telegram alerts. Full context below.

---

## 🎯 WHAT IT DOES

A **4-module market intelligence suite** — NOT just a signal scanner:

1. **Signal Scanner** (Pine Script parity) — detects trading setups across 24 symbols, 30m & 1h
2. **Market Intel** — big drops, sector bleeds, leadership/laggard detection, ATH/52W context
3. **Dip Scanner** — finds oversold uptrending stocks across ~50 quality names
4. **Morning Brief** — 9 AM daily digest with AI outlook

**Important:** This is a SIGNAL TRACKER / INTELLIGENCE system, NOT an auto-trader. I do NOT execute trades from alerts — I use them alongside my Pine Script indicator on TradingView charts. The system tells me what's firing, provides context, and tracks outcomes.

---

## 🏗️ ARCHITECTURE

**Stack:**
- Python 3.11
- yfinance (market data, free)
- Google Gemini 2.0 Flash API (free tier: 1,500 req/day)
- GitHub Actions (free unlimited minutes — repo is PUBLIC)
- Telegram Bot API (alerts)
- zoneinfo for EST/EDT auto-handling

**Files:**
your-repo/ ├── scanner.py ← v6.1 Pine-parity signal scanner ├── market_intel.py ← v2.0 context/intelligence engine ├── dip_scanner.py ← v2.0 oversold uptrend finder ├── morning_brief.py ← v2.0 daily digest ├── requirements.txt ← yfinance, pandas, numpy, requests └── .github/workflows/ ├── scanner.yml ← main scanner (every 10-15 min) ├── intel.yml ← context scan (every 30 min) ├── dip_scanner.yml ← 3-4x/day └── morning_brief.yml ← 9 AM weekdays

**State files (cached between GitHub Actions runs):**
- `alert_cache.json` — signal cooldowns by tier
- `active_trades.json` — open positions being tracked
- `trade_history.json` — archived closed trades (capped at 500)
- `scanner_state.json` — daily/weekly/intel state + timestamps
- `logs/` — daily scan logs per module

**GitHub Secrets:**
- `TELEGRAM_TOKEN`, `CHAT_ID`, `GEMINI_API_KEY`

---

## 📊 WATCHLIST (24 symbols, session-aware)

**CRYPTO (24/7):** BTC-USD, ETH-USD, XRP-USD, GC=F (gold futures)

**EXTENDED_HOURS_STOCKS** (pre-market + after-hours OK): NVDA, TSLA, AMD, MSFT, META, AMZN, GOOGL, NFLX

**REGULAR_HOURS_ONLY** (9:30 AM - 4:00 PM local): MU, SNDK, NBIS, IONQ, RGTI, QBTS, OKLO, IREN, UAMY, WGRX, SOFI, NVO

**DIP_UNIVERSE** (dip scanner only, ~50 symbols): AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA, AMD, AVGO, TSM, ASML, MU, SMCI, MRVL, ARM, SNDK, NFLX, CRM, ADBE, ORCL, CRWD, PLTR, SNOW, NOW, DDOG, NBIS, APP, DUOL, HOOD, OKLO, CEG, VST, SMR, NNE, LLY, NVO, REGN, JPM, V, MA, SOFI, AXP, IONQ, RGTI, QBTS, QUBT, MSTR, IREN, MARA, RIOT, COIN, SHOP, UBER, SPOT, ANET, COST, CAVA

---

## ⚙️ KEY CONFIG (v6.1)

### scanner.py — Pine Parity Settings
```python
# Pine: AE Core
AE_LENGTH = 200

# Pine: Signal Filters
MIN_CONF_SCORE = 4
GRADE_FILTER = "A+ and A"       # A+ (≥8), A (≥6), B (≥4), C (<4)
MIN_BARS_BETWEEN = 3
USE_COUNTER_TREND_BLOCK = True
USE_MTF_GATE = True
MTF_GATE_BULL = 9                # Block SELL if MTF sum ≥ 9/12
MTF_GATE_BEAR = 3                # Block BUY if MTF sum ≤ 3/12
USE_CHOP_FILTER = True
ADX_BYPASS_MIN = 5

# Pine: SQS (Signal Quality Score 0-100)
USE_SQS = True
SQS_MIN_FOR_ALERT = 75           # Pine default
AI_TIER_THRESHOLD = 75

# Safety caps
MAX_SL_PCT_STOCKS = 0.04         # 4% max stop
MAX_SL_PCT_CRYPTO = 0.08         # 8% max stop
MIN_SL_PCT_STOCKS = 0.005
MIN_SL_PCT_CRYPTO = 0.01
PRICE_SANITY_DEVIATION = 0.20    # reject if live diffs >20% from daily close

# Alert management
DIGEST_THRESHOLD = 4             # 4+ signals → digest mode
MAX_TRADE_AGE_HOURS = 72
COOLDOWN_ELITE = 2               # SQS 85+
COOLDOWN_STRONG = 4              # SQS 70-84
COOLDOWN_GOOD = 6                # SQS 55-69
COOLDOWN_FAIR = 10

# Multi-timeframe
TIMEFRAMES = [
    {'tf': '30m', 'lookback': '60d', 'label': '⚡30m', 'min_bars': 250},
    {'tf': '1h',  'lookback': '3mo', 'label': '📊1h',  'min_bars': 250},
]
MTF_FRAMES = ['15m', '1h', '4h', '1d']  # For MTF gate sum (0-12)
market_intel.py — Intelligence Thresholds
BIG_DROP_WARN = -5.0             # ±5% triggers big-move alert
BIG_DROP_CRITICAL = -10.0        # ±10% = CRITICAL
BIG_GAIN_ALERT = 8.0
COOLDOWN_HOURS = 4               # per-symbol cooldown
EARNINGS_WARNING_DAYS = 3        # warn if earnings ≤3d
dip_scanner.py — Dip Qualification
DIP_RSI_MIN = 28
DIP_RSI_MAX = 45
DIP_MIN_DROP_1D = -2.0
DIP_MIN_DROP_5D = -5.0
DIP_MAX_FROM_ATH = -25.0
DIP_MIN_VOL_RATIO = 0.8
PER_SYMBOL_COOLDOWN_HOURS = 6
🕐 TIMEZONE
Uses zoneinfo.ZoneInfo("America/New_York") for auto EST/EDT handling. All timestamps displayed in EDT/EST. Workflow crons run in UTC but cover both DST regimes.

🎯 SIGNAL LOGIC (scanner.py — Pine Script parity)
Indicators (all use Wilder's RMA — matches Pine's ta.rma, ta.rsi, ta.atr, ta.dmi):

RSI, ATR, ADX (+DI/-DI) with proper Wilder smoothing
Range Filter (Pine's rngfilt_va with volume-direction logic)
Supertrend (HL2 + ratcheting bands)
MACD, EMA20/50/200, VWAP, BB/KC Squeeze
10-point confluence scoring (bull AND bear tracked independently): AE • Supertrend • MACD • RSI • EMA50>200 • VWAP • ADX+DI • HTF • Squeeze • SMC

Triggers:

AE Flip (range filter direction change)
Band Breakout (close crosses hband/lowband)
Hard Gates:

ADX gate (>20) with high-score bypass (≥5)
Counter-trend block (score <6 AND HTF+ST opposite)
MTF Gate (blocks longs when 4-TF sum ≤3, shorts when ≥9)
Grade filter (A+ ≥8, A ≥6, B ≥4)
Chop filter (blocks signals too close to last signal, ATR-relative)
SQS (Signal Quality Score 0-100):

Confluence: 40% (score/10 × 40)
MTF alignment: 25% (directional match with MTF sum)
Regime fit: 15% (trending/ranging/transitional)
Volume: 10%
Volatility fit: 10%
Tiers: 🏆 ELITE (90+) / ⭐ STRONG (75-89) / ✅ GOOD (60-74) / ⚠️ FAIR (<60)

🤖 GEMINI AI INTEGRATION
Model: gemini-2.0-flash (free tier: 1,500 req/day)
Used by:
scanner.py — fires on SQS ≥75 (3-line verdict per signal)
market_intel.py — "why is this moving?" on big drops (4-line analysis)
morning_brief.py — daily outlook (4-line strategy brief)
Graceful failure: If API fails or rate-limits, alerts still send without AI block
Typical usage: ~30-60 calls/day (well under free tier limit)
📱 TELEGRAM ALERT TYPES
From scanner.py:
🚀 New Signal — full trade plan with entry, SL, 3 TPs, R-multiples, key levels, technicals, expiry, AI analysis
✅ TP1/TP2/TP3 Hit — with next-step guidance
🛑 SL Hit — distinguishes trailed profit vs true loss
⏰ Timeout — auto-close after 72h
📊 Open Positions Summary — grouped (winners/building/flat/losing/near-SL) every 2h if 2+ open
🔔 Signal Digest — when 4+ signals fire simultaneously
🔗 Correlation Notice — multiple signals/positions in same sector
🌍 Daily Market Context — 9 AM EDT weekdays (SPY/QQQ/VIX bias)
📈 Weekly Summary — Sunday 9 PM (win rate, R totals, grade performance)
From market_intel.py:
🩸 Big Drop Alert — ≥5% drop with AI "why" analysis + ATH/52W context + RS + earnings
🚀 Big Gain Alert — ≥8% gain
🚨 CRITICAL DROP — ≥10% drop
🏚️ Sector Bleed — when sector avg ≤-2%
💪 Leadership Signal — stocks holding while sector bleeds
🔻 Laggard Signal — stocks weak in strong sectors
From dip_scanner.py:
💎 Dip Opportunities — top-10 oversold uptrend setups (score 0-14)
From morning_brief.py:
🌅 Morning Brief — 9 AM daily digest (market snapshot, AI outlook, earnings, sectors, movers, buy candidates, avoid list)
🎨 ALERT UI FEATURES
Symbol emojis (₿ BTC, 💎 NVDA, 🥇 gold, etc.)
SQS visual meter (🟢🟢🟢🟢🟡🟡⚪⚪⚪⚪)
MTF bar (█████░░░░░░░ 5/12)
Borderline warning if SQS 60-74
Tight stop warning (<1%) / wide stop warning (>5%)
Nearby key levels (resistance, support, EMA50, EMA200) — ONLY if ≥0.3% distance
Absolute expiry time + relative: "Valid until: 19:09 EDT (59m)"
Session-specific trading tips (pre-market, after-hours warnings)
Trade age counter
After-hours warning ⚠️ for thin liquidity
Trailing stop instructions on TP hits
All P&L in R-multiples (no dollars)
Auto-split for messages >4000 chars
🛡️ SAFETY FEATURES
Price sanity check (rejects if live differs >20% from daily close)
Max SL caps (4% stocks, 8% crypto)
Min SL distance (0.5% stocks, 1% crypto)
Min SL distance (0.5× ATR)
Structure-based SL (recent swing low/high ± 0.2× ATR)
Rate limit delays (0.3s between symbol fetches)
Error recovery per-symbol (one failure doesn't kill scan)
Cooldowns prevent duplicate alerts
Pandas 2.x safe (numpy-based loops for Supertrend/range filter)
Earnings exclusion (blocks BUY verdicts within 3d of earnings)
Weekend/overnight guards (skip scans when data stale)
📅 CRON SCHEDULE (all DST-aware)
Workflow	Schedule	Purpose
scanner.yml	Every 10 min (market), 15 min (extended), 30 min (overnight/weekend)	Main signal scans
intel.yml	Every 30 min	Big moves, sector bleed, leadership
dip_scanner.yml	3-4× per trading day	Oversold uptrend finder
morning_brief.yml	9 AM ET weekdays	Daily digest
🔗 CORRELATION GROUPS / SECTORS
SECTORS = {
    'AI/Semis': ['NVDA', 'AMD', 'MU', 'SNDK', 'NBIS'],
    'Crypto': ['BTC-USD', 'ETH-USD', 'XRP-USD'],
    'Crypto-Adj': ['IREN', 'COIN', 'MSTR'],
    'Quantum': ['IONQ', 'RGTI', 'QBTS'],
    'Nuclear/Energy': ['OKLO', 'UAMY'],
    'Mega Tech': ['GOOGL', 'MSFT', 'META', 'AMZN', 'AAPL'],
    'EV/Auto': ['TSLA'],
    'Fintech': ['SOFI'],
    'Biotech': ['NVO', 'WGRX'],
    'Streaming': ['NFLX'],
    'Safe Haven': ['GC=F'],
}
📈 VERSION HISTORY
Version	Key Changes
v3.0	Initial scanner
v4.0-4.2	Smart filtering, context-aware RSI
v5.0-5.3	Multi-TF, position sizing, trade history
v6.0	Pine Script parity (Wilder's RMA, Range Filter)
v6.1	Audited & bug-fixed (pandas 2.x safe, chop filter wired, auto-split)
+ Intel v2.0	Market intelligence module (big moves, sector bleed, leadership)
+ Dip v2.0	Oversold uptrend scanner
+ Brief v2.0	9 AM daily digest with AI outlook
📜 COMPANION PINE SCRIPT
I also have AlphaEdge Pine Script v6.3.2 (indicator) on TradingView that runs on charts with identical signal logic. The Python scanner.py essentially mirrors that indicator's logic across multiple symbols with Telegram delivery.

Pine Script features: 16 themes, Signal Quality Score (SQS), Smart Re-Entry, Trade History log, Drawdown tracker, Session Volume Profile (NY/London/Asia), Liquidity Sweeps, Market Structure (BOS/CHoCH), Order Blocks, FVGs, Anchored VWAP, CVD, Multi-TF POC lines, Confluence Heatmap, Market Regime Badge.

💬 WHAT I MIGHT ASK FOR NEXT
Common requests:

Add new feature to any module (with audit for bugs)
Fix bugs in specific file (paste file, I'll audit)
Add a new module (e.g., news integration, weekly review, price-level alerts)
Tune thresholds / filters
Refactor / clean up code
Debug a specific alert I received
Compare Telegram alert vs my Pine chart (paste chart screenshot)
Update README / workflow files
When I paste a file, please:

Audit first — list all bugs found with severity
Then rewrite — clean version split into parts if long
Pine Script parity is critical — for scanner.py, it must match TradingView's ta.* functions exactly (Wilder's RMA for RSI/ATR/ADX)
Keep imports clean — shared helpers live in market_intel.py, reused by dip_scanner and morning_brief
🎯 DESIGN PRINCIPLES
Context over signals — every alert must explain WHY, not just WHAT
Pine parity is non-negotiable — scanner must match my TradingView chart
R-multiples only — no dollar amounts in alerts
Session awareness — different behavior for RTH vs extended vs overnight
Cooldowns everywhere — prevent spam
Graceful failure — if AI/API fails, alert still fires
Mobile-readable — concise layout, grouped info, emojis for scanning
Auto-split — Telegram's 4096 char limit handled automatically
---

Then after pasting that, follow with something like:

I want to [add a news integration / fix X / build Y]. Here's my current [file_name.py]:

[paste file]

---

## 💡 Shorter Version (If You Just Want a Tiny Reminder)

If you don't want the whole thing above, here's a **1-paragraph compact version** for quick-resume chats:

```markdown
## AlphaEdge Context (v6.1 — short form)

Production Python trading intelligence system on GitHub Actions → Telegram. 4 modules: scanner.py (Pine Script v6.3.2 parity signal detection, 24 symbols, 30m/1h, Wilder's RMA RSI/ATR/ADX, Range Filter, 10-pt confluence, SQS 0-100), market_intel.py (big drops ≥5%, sector bleeds, leadership, ATH/52W, earnings, RS), dip_scanner.py (50 quality names, oversold uptrend, RSI 28-45), morning_brief.py (9 AM daily AI digest). Free tier stack: yfinance + Gemini 2.0 Flash + GitHub Actions + Telegram. R-multiples only, no dollars. Session-aware, DST-aware, auto-split messages, pandas 2.x safe. I track signals alongside my TradingView Pine Script chart — NOT auto-trading. When I paste a file, audit bugs first then rewrite cleanly.
