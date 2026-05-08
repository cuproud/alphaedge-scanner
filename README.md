# 🎯 AlphaEdge Trading System

> **Automated multi-module signal + intelligence suite for stocks, crypto & gold.**
> Rich Telegram alerts • AI-enhanced • Pine Script parity • Runs 24/7 on GitHub Actions • **Free forever.**

![Python](https://img.shields.io/badge/Python-3.11-blue)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-automated-success)
![Telegram](https://img.shields.io/badge/Telegram-alerts-blue)
![Gemini AI](https://img.shields.io/badge/AI-Gemini%202.0-purple)
![Status](https://img.shields.io/badge/status-live-brightgreen)
![Version](https://img.shields.io/badge/version-v7.0-orange)

---

## ✨ What It Does

AlphaEdge is a **4-module market intelligence system** that goes far beyond entry signals:

1. 🎯 **Signal Scanner** (`scanner.py` v7.0) — Pine Script parity signal detection across 25 symbols, 30m & 1h timeframes, with full trade lifecycle tracking
2. 🧠 **Market Intel** (`market_intel.py`) — Big drops, sector bleeds, leadership/laggard detection, ATH/52W context
3. 🌅 **Morning Brief** (`morning_brief.py`) — 9 AM daily digest with AI outlook, buy candidates, earnings alerts
4. 💎 **Dip Scanner** (`dip_scanner.py`) — Proactively finds oversold uptrending stocks across ~50 quality names

Every alert is **context-rich**: not just "BUY here" but _why, where to enter, where it invalidates, what the market is doing, is earnings coming, and what the AI thinks_.

> **⚠️ It's a signal tracker, not an auto-trader.** You get the alerts with full reasoning — you decide what to do.

---

## 📦 File Structure

```
your-repo/
├── scanner.py              ← Signal scanner v7.0 (Pine parity + advanced intelligence)
├── market_intel.py         ← Big drops + sector bleed + leadership + earnings + RS
├── dip_scanner.py          ← Oversold uptrend opportunity finder
├── morning_brief.py        ← 9 AM daily intelligence digest
├── symbols.yaml            ← Unified watchlist (single source of truth for scanner.py)
├── requirements.txt
└── .github/workflows/
    ├── scanner.yml         ← Every 10-15 min during market hours
    ├── intel.yml           ← Every 30 min context scan
    ├── dip_scanner.yml     ← 3-4× per trading day
    └── morning_brief.yml   ← 9 AM weekdays
```

### State files (auto-created on first run)
```
alert_cache.json            ← Deduplication cache (48h TTL, auto-cleaned)
active_trades.json          ← Open trade tracking
trade_history.json          ← Closed trade archive (last 500)
scanner_state.json          ← Cooldown & weekly summary state
sqs_history.json            ← SQS quality trend history per symbol  ← NEW v7.0
dynamic_threshold.json      ← Adaptive SQS threshold (recalculated daily)  ← NEW v7.0
logs/scan_YYYY-MM-DD.log    ← Daily rotating log  ← NEW v7.0
```

---

## 🧩 The 4 Modules

### 📄 `scanner.py` v7.0 — The Signal Engine

Pine Script v6.3.2 parity — mirrors your TradingView indicator **exactly**:

- ✅ **Wilder's RMA** RSI/ATR/ADX (not SMA — critical for TradingView match)
- ✅ **AE Range Filter** (volume-directional, mirrors Pine's `rngfilt_va`)
- ✅ **Supertrend** with HL2 + ratcheting bands (numpy-based, pandas 2.x safe)
- ✅ **9-point confluence** (AE, Supertrend, MACD, RSI, EMA, VWAP, ADX+DI, HTF, Squeeze)
- ✅ **SQS composite score** (40% confluence / 25% MTF / 15% regime / 10% vol / 10% volatility)
- ✅ **Counter-trend blocks**, MTF gate, grade filter, chop filter
- ✅ **Structure-based SL** with ATR distance caps (separate % caps for stocks vs crypto)
- ✅ **External `symbols.yaml`** — add/remove symbols without touching code
- 🎯 Signal tiers: 🏆 ELITE / ⭐ STRONG / ✅ GOOD / ⚠️ FAIR
- 🤖 AI enrichment (Gemini 2.0 Flash) on SQS ≥ 75
- 📊 Full trade lifecycle: open → TP1 → TP2 → TP3 → SL / timeout (72h)
- 📈 Open positions summary every 2h (when 2+ trades active)
- 📅 Weekly performance summary (Sunday 9 PM)

#### What's New in v7.0 vs v6.1

| Fix / Feature | Detail |
|---|---|
| ✅ No-repaint signal bar | Uses `iloc[-2]` (last closed bar), not the forming bar |
| ✅ RSI bull/bear mutually exclusive | Was double-counting confluence points |
| ✅ Flip detection window | Looks at last 2 bars — catches flips missed within 10m |
| ✅ Cache auto-cleanup | Entries older than 48h removed on every run |
| ✅ Markdown-safe escaping | Symbols like `BRK.B`, `GC=F` no longer break alerts |
| ✅ SQS denominator fix | Corrected to /9 (was /10) |
| ✅ Single data fetch per symbol/TF | Was fetching 3× per symbol — now 1× |
| 🆕 `symbols.yaml` | Unified watchlist — one file controls all symbol lists and sector groups |
| 🆕 SQS quality trending | "NVDA: 68 → 74 → 82 improving" shown in every alert |
| 🆕 VIX regime filter | Blocks longs when VIX ≥ 35 or spiking > 20% above 5d avg |
| 🆕 Volume Profile / POC | "Price above POC — strong hands" context line per signal |
| 🆕 Dynamic SQS threshold | Tightens automatically if B-grade win rate drops below 35% |
| 🆕 Plain-English R:R | "Risk $12.22 → Make $36.65 (3.0× reward)" instead of raw numbers |
| 🆕 Urgency emoji prefixes | 🚨🔥🔥 elite, 🚨🔥 strong, ⭐ solid, ⚠️🌋 VIX warning |

### 🧠 `market_intel.py` — The Context Engine

Runs every 30 minutes. Detects:

- 🩸 **Big moves** — Any stock down ≥5% or up ≥8% intraday
- 🏚️ **Sector bleeds** — When AI/Semis, Crypto, Quantum, etc. are bleeding together
- 💪 **Leaders & laggards** — Stocks outperforming/underperforming their sector
- 📏 **ATH / 52W context** — How far from peak, position in range
- 📅 **Earnings calendar** — Warns if earnings ≤3 days away
- 🎯 **Clear verdicts** — BUY ZONE / HOLD / AVOID / WAIT with reasoning
- 🤖 **AI drop analysis** — Gemini explains _why_ a stock is moving

### 💎 `dip_scanner.py` — The Opportunity Finder

Runs 3-4× per trading day. Scans ~50 high-quality names for **healthy pullbacks**:

- ✅ Above daily EMA200 (uptrend confirmed)
- ✅ Daily RSI 28-45 (oversold but not dead)
- ✅ Within 25% of ATH
- ✅ NOT in earnings window
- ✅ Volume confirmation
- 📊 Scores 0-14 based on setup quality
- 💪 Includes relative strength vs SPY
- 🎯 Ranked top-10 delivered with buy zones

### 🌅 `morning_brief.py` — The Daily Digest

Fires once at 9 AM ET on weekdays:

- 🌍 Market snapshot (SPY/QQQ/VIX)
- 🤖 AI-powered daily outlook
- 📅 Earnings today/tomorrow warnings
- 🌡️ Sector performance heatmap
- 🚀 Top gainers / 📉 top losers
- 🎯 Buy Zone candidates with verdicts
- 🚫 Avoid list

---

## ⚙️ Automated Workflow Schedule

All workflows are **DST-aware** — cron expressions cover both EDT and EST windows.

| Workflow | Schedule | Purpose |
|---|---|---|
| 🎯 **scanner.yml** | Every 10 min (market), 15 min (extended), 30 min (overnight/weekend) | Main signal scans |
| 🧠 **intel.yml** | Every 30 min | Big moves, sector bleed, leadership |
| 💎 **dip_scanner.yml** | 3-4× per trading day | Oversold uptrend finder |
| 🌅 **morning_brief.yml** | 9 AM ET weekdays | Daily digest |

### Scanner Session Details

| Session | Time (ET) | Frequency | Symbols Scanned |
|---|---|---|---|
| 🔔 Regular Market | 9:30 AM – 4:00 PM | Every 10 min | All 25 symbols |
| 🌙 After-Hours | 4:00 PM – 8:00 PM | Every 15 min | Crypto + extended-hours stocks |
| 🌅 Pre-Market | 4:00 AM – 9:30 AM | Every 15 min | Crypto + extended-hours stocks |
| 🌑 Overnight | 8:00 PM – 4:00 AM | Every 30 min | Crypto only |
| 🪙 Weekends | 24/7 | Every 30 min | Crypto only |

---

## 📱 What You'll Receive on Telegram

### 🎯 From the Signal Scanner

| Alert | When |
|---|---|
| **🚀 New Signal** | Full trade plan: entry, SL, 3 TPs, POC context, key levels, RSI/ADX, SQS trend, AI verdict, session tips |
| **✅ TP1/TP2/TP3 Hit** | Progress alert with next-step guidance (move SL to BE, take partial profits) |
| **🛑 SL Hit** | Distinguishes trailed profit exit vs true loss, with price ladder |
| **⏰ Timeout** | Auto-close after 72h if trade never resolves |
| **📊 Open Positions Summary** | Every 2h when 2+ positions open — grouped by winners/losers/near-SL |
| **🔗 Correlation Notice** | When multiple signals fire in the same sector simultaneously |
| **🔔 Signal Digest** | When 4+ signals fire at once — compressed summary, then full details for top quality |
| **📈 Weekly Summary** | Sunday 9 PM — win rate, R totals, grade performance, adaptive threshold status |

### 🧠 From Market Intel

| Alert | When |
|---|---|
| **🩸 Big Drop Alert** | Any stock down ≥5% — with AI "why is it dropping" analysis |
| **🚀 Big Gain Alert** | Any stock up ≥8% — potential profit-taking zone |
| **🚨 CRITICAL DROP** | Down ≥10% — with full positional context |
| **🏚️ Sector Bleed** | When a sector's stocks average down ≥2% together |
| **💪 Leadership Signal** | Stocks holding firm while sector bleeds (future winners) |
| **🔻 Laggard Signal** | Stocks weak in strong sectors (avoid) |

### 💎 From Dip Scanner

| Alert | When |
|---|---|
| **🎯 Dip Opportunities** | 3-4× daily — oversold uptrend setups with score, buy zone, RS vs SPY |

### 🌅 From Morning Brief

| Alert | When |
|---|---|
| **🌅 Daily Digest** | 9 AM weekdays — full market setup for the day ahead |

---

## 🎯 Expected Daily Alert Flow

| Time (ET) | Alert |
|---|---|
| **9:00 AM** | 🌅 Morning Brief — setup for the day |
| **9:30 AM** | 🔔 First scanner run (market open) |
| **10:30 AM** | 💎 Dip Scan |
| **Throughout RTH** | 🎯 Signal alerts, TP/SL updates, 🩸 big drops, 💪 leadership |
| **Every 2h (if trades open)** | 📊 Open positions summary |
| **1:30 PM** | 💎 Dip Scan refresh |
| **3:30 PM** | 💎 Final dip scan + pre-close |
| **4:00 PM+** | Scanner continues into after-hours (crypto + ext-hrs) |
| **Sunday 9 PM** | 📈 Weekly performance summary |

---

## 🧠 Signal Quality System

### Confluence Score (9 points)

| Point | Condition |
|---|---|
| AE Range Filter | Bullish/bearish direction |
| Supertrend | Trend direction |
| MACD | Signal line crossover |
| RSI | Above/below 50 (mutually exclusive) |
| EMA | 50 above/below 200 |
| VWAP | Price above/below VWAP |
| ADX + DI | Strong trend with correct DI alignment |
| HTF Bias | 4h EMA50 vs EMA200 |
| Squeeze | BB/KC squeeze breakout direction |

### Signal Quality Tiers

| Tier | SQS Range | Urgency |
|---|---|---|
| 🏆 ELITE | 90–100 | 🚨🔥🔥 All systems aligned |
| ⭐ STRONG | 80–89 | 🚨🔥 High conviction |
| ✅ GOOD | 70–79 | 🚨 Solid, above threshold |
| ⚠️ FAIR | 60–69 | Silent notification, consider half size |
| 🔹 LOW | < 60 | Filtered out — not sent |

### Quality Gates (all must pass)

- **Grade filter** — default "A+ and A" (score ≥ 6/9)
- **SQS ≥ threshold** — default 75, adaptive based on recent B-grade win rate
- **ADX gate** — trend strength ≥ 20, or confluence ≥ 5 bypasses
- **MTF gate** — blocks longs when 4 higher TFs are bearish, shorts when bullish
- **Counter-trend block** — blocks weak signals against HTF + Supertrend alignment
- **Chop filter** — blocks signals too close to last signal (ATR-relative distance)
- **VIX filter** — blocks longs when VIX ≥ 35 or spiking > 20% above 5d avg
- **Price sanity check** — rejects if live price > 20% off daily close (bad data guard)

---

## 📊 `symbols.yaml` — Unified Watchlist

All symbol lists live in one file. No need to edit `scanner.py` to add or remove symbols.

```yaml
crypto:
  - symbol: BTC-USD
    emoji: ₿
    sector: Crypto

extended_hours:
  - symbol: NVDA
    emoji: 🎮
    sector: AI/Semis

regular_hours:
  - symbol: AAPL
    emoji: 🍎
    sector: Tech

dip_extras:                   # used by dip_scanner.py only
  - symbol: PLTR
    emoji: 🔮
    sector: AI/Semis

settings:
  scanner:
    sqs_min_for_alert: 75     # raise to 80 for fewer, higher-quality alerts
    grade_filter: "A+ and A"  # "A+ Only" | "A+ and A" | "B and better" | "All"
```

Symbols in the same `sector` trigger a **correlation notice** when multiple signals fire
together — reminding you to manage overall sector exposure.

---

## 🛠️ Stack

| Component | Tech |
|---|---|
| Language | Python 3.11 |
| Market Data | yfinance (free) |
| AI Analysis | Google Gemini 2.0 Flash (free tier: 1,500 req/day) |
| Execution | GitHub Actions (unlimited minutes on public repos) |
| Alerts | Telegram Bot API |
| Timezone | `zoneinfo` (auto EDT/EST handling) |
| Storage | JSON state files, persisted between runs via GitHub Actions cache |

---

## 📦 Setup

### Prerequisites

1. **Fork this repo** (keep it public for unlimited GitHub Actions minutes)
2. Get your **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)
3. Get your **Chat ID** (DM the bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`)
4. Get a **free Gemini API key** from [Google AI Studio](https://ai.google.dev/)

### Configure Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `TELEGRAM_TOKEN` | From @BotFather |
| `CHAT_ID` | Your Telegram chat ID |
| `GEMINI_API_KEY` | From Google AI Studio (optional — enables AI analysis) |

### Customize Your Watchlist

Edit `symbols.yaml` to add or remove symbols:

```yaml
regular_hours:
  - symbol: TSLA
    emoji: 🚗
    sector: EV
```

### Customize Signal Quality

Override defaults via `symbols.yaml` settings section:

```yaml
settings:
  scanner:
    sqs_min_for_alert: 80    # fewer but higher-quality alerts
    grade_filter: "A+ Only"  # strictest — score must be 8+/9
```

### Test Run

Go to **Actions tab → pick any workflow → Run workflow** to fire it manually.

---

## 🔬 Version History

| Version | Highlights |
|---|---|
| v3.0 | Initial scanner with basic confluence |
| v4.0–4.2 | Smart filtering (RSI context, parabolic blocks) |
| v5.0–5.3 | Multi-timeframe, position sizing, trade history |
| v6.0 | Pine Script parity (Wilder's RMA, Range Filter, Supertrend) |
| v6.1 | Bug fixes: pandas 2.x safe, chop filter wired, auto-split messages |
| **v7.0** | No-repaint fix · SQS trending · VIX filter · Volume Profile/POC · Dynamic threshold · Plain-English R:R · Urgency emojis · `symbols.yaml` unified watchlist |
| Intel v2.0 | Market intelligence (big moves, sector bleed, leadership, earnings) |
| Dip v2.0 | Oversold uptrend scanner (50 symbols) |
| Brief v2.0 | 9 AM daily digest with AI outlook |

---

## ⚠️ Disclaimer

This is a **signal scanner and market intelligence tool** for educational/informational purposes.
It does **not execute trades**. Alerts are not financial advice. Always do your own research,
verify signals against your own analysis, and manage risk appropriately.

Past signal performance does not guarantee future results. Market data from yfinance is
delayed and may contain errors. AI analysis is probabilistic commentary, not certainty.

**Trade at your own risk.**

---

## 📜 Credits

**Author:** VAMSI
**License:** Personal use
**Companion:** [AlphaEdge Pine Script v6.3.2](https://tradingview.com/) for TradingView chart integration

---

*Built with ❤️ for traders who want context, not just arrows.*
