# 🎯 AlphaEdge Trading Scanner

> **Automated multi-timeframe signal scanner for stocks & crypto.  
> Rich Telegram alerts. AI-enhanced. Runs 24/7 on GitHub Actions. Free forever.**
---
![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Automated-green?logo=githubactions&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Alerts-blue?logo=telegram&logoColor=white)
![Gemini AI](https://img.shields.io/badge/Gemini_AI-Enhanced-orange?logo=google&logoColor=white)
![Status](https://img.shields.io/badge/Status-Production-brightgreen)
---

## ✨ What It Does

AlphaEdge watches **24 symbols** (stocks + crypto + gold) across **multiple timeframes**, detects high-quality trading setups using 10+ technical indicators, and sends **beautiful Telegram alerts** with full trade plans — including entry, stop loss, 3 take-profit targets, and AI-powered analysis.

It tracks every signal from the moment it fires until it closes (TP hit, SL hit, or 72h timeout), and automatically sends follow-up alerts when targets are reached.

**It's a signal scanner, not an auto-trader.** You get the alerts — you decide what to do.

---

## 🧩 How It Works

The system has **2 files** working together:

### 📄 `scanner.py` — The Brain
The main Python scanner that does all the work:

- 🔍 **Scans 24 symbols** across 30m and 1h timeframes
- 🧠 **Smart filtering**: Blocks extreme RSI signals, parabolic chases, and counter-trend setups
- 📊 **Scores every signal** on two axes:
  - Confluence Score (0–10) — how many indicators align
  - Signal Quality Score / SQS (0–100) — overall setup grade
- 🎯 **Classifies signals into tiers**: 🏆 ELITE / ⭐ STRONG / ✅ GOOD / ⚠️ FAIR
- 🤖 **AI enrichment**: Sends top-tier signals (SQS ≥70) to Google Gemini for a 3-line verdict
- 📱 **Sends Telegram alerts** with live entry price, risk levels, nearby support/resistance, and session-specific tips
- 🔔 **Tracks active trades** and fires notifications on TP1/TP2/TP3/SL hits
- 📉 **Session-aware**: Only scans stocks during market hours; crypto 24/7
- 📆 **Daily market context** (SPY/QQQ/VIX bias) at 9 AM weekdays
- 📊 **Weekly performance summary** every Sunday night
- 🕵️ **Near-miss digest**: Shows setups that almost triggered
- 🛡️ **Safety features**: Price sanity checks, minimum/maximum stop distances, cooldowns per tier

### ⚙️ `.github/workflows/scanner.yml` — The Clock
A GitHub Actions workflow that runs the scanner automatically on a smart schedule:

| Session | Time (Local) | Frequency | What's Scanned |
|---------|--------------|-----------|----------------|
| 🔔 **Regular Market** | 9:30 AM – 4:00 PM | Every 10 min | All 24 symbols |
| 🌙 **After-Hours** | 4:00 PM – 8:00 PM | Every 15 min | Crypto + 8 mega-caps |
| 🌅 **Pre-Market** | 4:00 AM – 9:30 AM | Every 15 min | Crypto + 8 mega-caps |
| 🌑 **Overnight** | 8:00 PM – 4:00 AM | Every 30 min | Crypto only |
| 🪙 **Weekends** | 24/7 | Every 30 min | Crypto only |

The schedule is **daylight-saving-time aware** — automatically covers both EDT and EST windows.

---

## 📱 What You'll Receive on Telegram

### 🚨 New Signal Alert
Full trade plan with entry price, stop loss, 3 TPs, nearby key levels, technicals (RSI/ADX/regime), signal expiry time, session tips, and AI analysis.

### ✅ Trade Progress Alerts
When a signal hits TP1, TP2, TP3, or SL — you get notified immediately with next-step guidance (move SL to breakeven, take partials, etc.).

### 📊 Open Positions Summary
Periodic snapshot of all tracked signals with current R-multiple progress, sorted from best to worst.

### 🌍 Daily Market Context
Morning brief at 9 AM EDT with SPY/QQQ/VIX readings and bias (risk-on / risk-off / mixed).

### 👀 Near-Miss Digest
Symbols with strong confluence but no trigger yet — "watch these."

### 📈 Weekly Summary
Every Sunday night: win rate, total R, grade performance breakdown, best/worst trades.

### 🔗 Correlation Notice
When multiple signals fire in the same sector (e.g., AI/Semis or Crypto) — helps you avoid over-exposure.

---

## 🧠 The Signal Logic

**Triggers** (any one fires a potential signal):
- 🔀 **Fresh Cross** — EMA50, Supertrend, or MACD just flipped
- 🎯 **Pullback** — Retest of EMA20 inside an established trend
- 🔁 **Oversold Bounce / Overbought Drop** — RSI extremes reversing
- 📈 **Trend Continuation** — Strong trend breaking recent highs/lows
- 💪 **Strong Momentum** — 7+ confluence with healthy ADX

**Smart Blocks** (context-aware — won't block healthy uptrends):
- 🚫 RSI ≥ 80 without trend confirmation
- 🚫 Parabolic stretch (>5× ATR) in weak trends
- 🚫 ADX ≥ 65 (trend exhausted)
- 🚫 Counter-higher-timeframe momentum chases
- 🚫 Crypto fresh-cross signals without trend confirmation (kills chop)

---

## 🛠️ Stack

| Component | Tech |
|-----------|------|
| Language | Python 3.11 |
| Market Data | `yfinance` (free) |
| AI Analysis | Google Gemini 2.0 Flash (free tier: 1,500 req/day) |
| Execution | GitHub Actions (unlimited minutes on public repos) |
| Alerts | Telegram Bot API |
| Timezone | `zoneinfo` (auto EDT/EST handling) |
| Storage | JSON files cached between runs |

---

## 📦 Setup (If You Want to Run Your Own)

1. **Fork this repo** (keep it public for unlimited GitHub Actions minutes)
2. **Add these secrets** under repo Settings → Secrets → Actions:
   - `TELEGRAM_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `CHAT_ID` — your Telegram chat ID
   - `GEMINI_API_KEY` — from [Google AI Studio](https://aistudio.google.com) (free)
3. **Customize the watchlist** in `scanner.py` if desired
4. **That's it** — the workflow runs automatically on schedule

---

## ⚠️ Disclaimer

This is a **signal scanner for educational/informational purposes**. It does not execute trades. Signals are not financial advice. Always do your own research and manage risk appropriately. Past signal performance does not guarantee future results.

---

## 📜 Version

**Current:** v5.3  
**Author:** VAMSI  
**License:** Personal use
