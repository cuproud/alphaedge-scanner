Here's the complete, modernized README reflecting your full system (scanner + intel + dip + brief):

# 🎯 AlphaEdge Trading System

> **Automated multi-module signal + intelligence suite for stocks, crypto & gold.**
> Rich Telegram alerts • AI-enhanced • Pine Script parity • Runs 24/7 on GitHub Actions • **Free forever.**

![Python](https://img.shields.io/badge/Python-3.11-blue)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-automated-success)
![Telegram](https://img.shields.io/badge/Telegram-alerts-blue)
![Gemini AI](https://img.shields.io/badge/AI-Gemini%202.0-purple)
![Status](https://img.shields.io/badge/status-live-brightgreen)
![Version](https://img.shields.io/badge/version-v6.1-orange)

---

## ✨ What It Does

AlphaEdge is a **4-module market intelligence system** that goes far beyond entry signals:

1. 🎯 **Signal Scanner** — Pine Script parity signal detection across 24 symbols, 30m & 1h timeframes
2. 🧠 **Market Intel** — Big drops, sector bleeds, leadership/laggard detection, ATH/52W context
3. 🌅 **Morning Brief** — 9 AM daily digest with AI outlook, buy candidates, earnings alerts
4. 💎 **Dip Scanner** — Proactively finds oversold uptrending stocks across ~50 quality names

Every alert is **context-rich**: not just "BUY here" but _why, where to enter, where it invalidates, what the market is doing, is earnings coming, and what the AI thinks_.

> **⚠️ It's a signal tracker, not an auto-trader.** You get the alerts with full reasoning — you decide what to do.

---

## 📦 File Structure

your-repo/ ├── scanner.py ← Main signal scanner (Pine Script parity) ├── market_intel.py ← Big drops + sector bleed + leadership + earnings + RS ├── dip_scanner.py ← Oversold uptrend opportunity finder ├── morning_brief.py ← 9 AM daily intelligence digest ├── requirements.txt └── .github/workflows/ ├── scanner.yml ← every 10-15 min during market ├── intel.yml ← every 30 min context scan ├── dip_scanner.yml ← 3-4x/day dip hunting └── morning_brief.yml ← 9 AM weekdays

---

## 🧩 The 4 Modules

### 📄 `scanner.py` — The Signal Engine

Pine Script v6.3.2 parity — mirrors your TradingView indicator **exactly**:

- ✅ **Wilder's RMA** RSI/ATR/ADX (not SMA — critical for TradingView match)
- ✅ **Range Filter** (AE Core) with volume-directional logic
- ✅ **Supertrend** with HL2 + ratcheting bands
- ✅ **10-point confluence** (AE, Supertrend, MACD, RSI, EMA, VWAP, ADX+DI, HTF, Squeeze, SMC)
- ✅ **SQS composite** (40% confluence / 25% MTF / 15% regime / 10% vol / 10% volat)
- ✅ **Counter-trend blocks**, MTF gate, grade filter, chop filter
- ✅ **Structure-based SL** with min ATR distance
- 🎯 Classifies into tiers: 🏆 ELITE / ⭐ STRONG / ✅ GOOD / ⚠️ FAIR
- 🤖 AI enrichment on SQS ≥75 via Gemini
- 📊 Tracks open trades, fires TP1/TP2/TP3/SL/timeout alerts
- 🛡️ Session-aware watchlist, price sanity checks, auto-split long messages

### 🧠 `market_intel.py` — The Context Engine

Runs every 30 minutes. Detects:

- 🩸 **Big moves** — Any stock down ≥5% or up ≥8% intraday
- 🏚️ **Sector bleeds** — When AI/Semis, Crypto, Quantum, etc. are bleeding together
- 💪 **Leaders & laggards** — Stocks outperforming/underperforming their sector
- 📏 **ATH / 52W context** — How far from peak, position in range
- 📅 **Earnings calendar** — Warns if earnings ≤3 days away
- 🎯 **Clear verdicts** — BUY ZONE / HOLD / AVOID / WAIT with reasoning
- 🤖 **AI drop analysis** — Gemini explains _why_ a stock is moving (sector? news? market?)

### 💎 `dip_scanner.py` — The Opportunity Finder

Runs 3-4× per trading day. Scans ~50 high-quality names for **healthy pullbacks**:

- ✅ Above daily EMA200 (uptrend confirmed)
- ✅ Daily RSI 28-45 (oversold but not dead)
- ✅ Today or 5d drop triggering dip
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

### Scanner Details

| Session | Time (ET) | Frequency | What's Scanned |
|---|---|---|---|
| 🔔 Regular Market | 9:30 AM – 4:00 PM | Every 10 min | All 24 symbols |
| 🌙 After-Hours | 4:00 PM – 8:00 PM | Every 15 min | Crypto + 8 mega-caps |
| 🌅 Pre-Market | 4:00 AM – 9:30 AM | Every 15 min | Crypto + 8 mega-caps |
| 🌑 Overnight | 8:00 PM – 4:00 AM | Every 30 min | Crypto only |
| 🪙 Weekends | 24/7 | Every 30 min | Crypto only |

---

## 📱 What You'll Receive on Telegram

### 🚨 From the Signal Scanner

| Alert | When |
|---|---|
| **🚀 New Signal** | Full trade plan: entry, SL, 3 TPs, nearby levels, RSI/ADX, AI verdict, session tips |
| **✅ TP1/TP2/TP3 Hit** | Progress alert with next-step guidance (move SL to BE, take partial) |
| **🛑 SL Hit** | Stop-loss alert (distinguishes trailed profit vs true loss) |
| **⏰ Timeout** | Auto-close after 72h if no resolution |
| **📊 Open Positions Summary** | Every 2h when 2+ positions open — grouped by winners/losers/near-SL |
| **🔗 Correlation Notice** | When multiple signals fire in same sector |
| **🔔 Signal Digest** | When 4+ signals fire at once — compressed view |
| **📈 Weekly Summary** | Sunday 9 PM — win rate, R totals, grade performance |

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
| **🎯 Dip Opportunities** | 3-4× daily when oversold uptrend setups qualify |

Each dip alert includes score (0-14), buy zone, support level, RS vs SPY, and reasoning.

### 🌅 From Morning Brief

Daily 9 AM digest covering the entire day ahead in one message.

---

## 🧠 Signal Logic (Scanner)

### Triggers (any one can fire)

- 🔀 **AE Flip** — Range Filter direction flips
- 🎯 **Band Breakout** — Close breaks above hband / below lowband
- 🔁 **Oversold Bounce / Overbought Drop** — RSI extremes with reversal
- 📈 **Trend Continuation** — Strong trend breaking recent highs/lows
- 💪 **Strong Momentum** — 7+ confluence with healthy ADX

### Smart Blocks (context-aware)

- 🚫 **RSI ≥ 80** without trend confirmation
- 🚫 **Parabolic stretch** (>5× ATR) in weak trends
- 🚫 **ADX ≥ 65** (trend exhausted)
- 🚫 **Counter-HTF** momentum chases
- 🚫 **MTF Gate** — blocks longs when higher TFs bearish
- 🚫 **Chop Filter** — blocks signals too close to last signal (ATR-relative)
- 🚫 **Earnings Blocker** — no BUY verdicts within 3 days of earnings

---

## 🛠️ Stack

| Component | Tech |
|---|---|
| Language | Python 3.11 |
| Market Data | yfinance (free) |
| AI Analysis | Google Gemini 2.0 Flash (free tier: 1,500 req/day) |
| Execution | GitHub Actions (unlimited minutes on public repos) |
| Alerts | Telegram Bot API |
| Timezone | zoneinfo (auto EDT/EST handling) |
| Storage | JSON state files cached between runs |

---

## 📦 Setup

### Prerequisites

1. **Fork this repo** (keep it public for unlimited GitHub Actions minutes)
2. Get your **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)
3. Get your **Chat ID** (DM the bot, then check `https://api.telegram.org/bot/getUpdates`)
4. Get a **free Gemini API key** from [Google AI Studio](https://ai.google.dev/)

### Configure Secrets

Go to your forked repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `TELEGRAM_TOKEN` | From @BotFather |
| `CHAT_ID` | Your Telegram chat ID |
| `GEMINI_API_KEY` | From Google AI Studio |

### Customize (Optional)

Edit in `scanner.py`:
- `ALL_SYMBOLS` — your watchlist
- `SQS_MIN_FOR_ALERT` — signal quality threshold (default 75)
- `GRADE_FILTER` — confluence grade filter (default "A+ and A")

Edit in `market_intel.py`:
- `MONITOR_LIST` — symbols to watch for big moves
- `BIG_DROP_WARN` / `BIG_DROP_CRITICAL` — alert thresholds
- `SECTORS` — sector groupings for correlation/leadership

Edit in `dip_scanner.py`:
- `DIP_UNIVERSE` — stocks to scan for dip opportunities
- `DIP_RSI_MIN` / `DIP_RSI_MAX` — oversold zone

### Test Run

Go to **Actions tab → pick any workflow → Run workflow** manually to test immediately.

---

## 🎯 Expected Daily Alert Flow

| Time (ET) | Alert Type |
|---|---|
| **9:00 AM** | 🌅 Morning Brief — setup for the day |
| **9:30 AM** | 🔔 First scanner run (market open) |
| **10:30 AM** | 💎 Dip Scan |
| **Throughout RTH** | 🎯 Signal alerts, 🩸 big drops, 💪 leadership |
| **1:30 PM** | 💎 Dip Scan refresh |
| **3:30 PM** | 💎 Final dip scan + pre-close |
| **4:00 PM** | Regular scanner continues into after-hours |
| **Sunday 9 PM** | 📈 Weekly performance summary |
| **Anytime on events** | 🩸 Sector bleed, ⚠️ near-SL clusters |

---

## 🔬 Version History

| Version | Highlights |
|---|---|
| v3.0 | Initial scanner with basic confluence |
| v4.0-4.2 | Smart filtering (RSI context, parabolic blocks) |
| v5.0-5.3 | Multi-timeframe, position sizing, trade history |
| **v6.0** | Pine Script parity edition (Wilder's RMA, Range Filter) |
| **v6.1** | Audited & bug-fixed (pandas 2.x safe, chop filter wired, auto-split messages) |
| **+ Intel v2.0** | Market intelligence module (big moves, sector bleed, leadership, earnings, RS) |
| **+ Dip v2.0** | Oversold uptrend scanner (50 symbols) |
| **+ Brief v2.0** | 9 AM daily digest with AI outlook |

---

## ⚠️ Disclaimer

This is a **signal scanner and market intelligence tool** for educational/informational purposes. It does **not execute trades**. Alerts are not financial advice. Always do your own research, verify signals against your own analysis, and manage risk appropriately.

Past signal performance does not guarantee future results. Market data provided by yfinance is delayed and may contain errors. AI analysis is probabilistic commentary, not certainty.

**Trade at your own risk.**

---

## 📜 Credits

**Author:** VAMSI
**License:** Personal use
**Companion:** [AlphaEdge Pine Script v6.3.2](https://tradingview.com/) for TradingView chart integration

---

*Built with ❤️ for traders who want context, not just arrows.*
