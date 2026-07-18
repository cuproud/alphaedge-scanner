# 🎯 AlphaEdge v7.0 Trading System

> **Pine Script parity signal scanner + market intelligence suite**  
> Telegram alerts • 12-point scoring + RS bonus • Quiet hours • Circuit breaker • **Free forever**

![Python](https://img.shields.io/badge/Python-3.11-blue)
![Status](https://img.shields.io/badge/status-v7.0%20beta-orange)
![Version](https://img.shields.io/badge/version-7.0--beta-yellow)

---

## ⚡ What's New in v6.10

### 🌙 Quiet Hours — No Overnight Alerts
- **Silent 10 PM - 7 AM ET** — scanner runs, alerts queue
- **7 AM batch delivery** — overnight signals delivered together
- **Bypass for critical events** — VIX spike >35, circuit breaker

### 🎯 12-Point Scoring System
**Old:** 9 equal-weight pillars → **New:** 5 weighted pillars
- **P1:** HTF Trend (3 pts) — most important
- **P2:** Momentum (2 pts) — MACD + RSI state
- **P3:** Volume (3 pts) — institutional confirmation
- **P4:** Regime (2 pts) — TRENDING/VOLATILE only
- **P5:** Candle Body (2 pts) — directional conviction ← **NEW**

### 🛑 Circuit Breaker
- **Pauses after 3 consecutive losses** (30 min on 30m chart)
- **Escalating cooldown** — 4th loss = 60 min, 5th = 90 min
- **Resets on first win** — prevents loss streaks

### 📊 Dynamic Regime-Based TP/SL
**VOLATILE Regime:**
- TP1: 1.5R (was 1.0R) — ride momentum
- TP3: 4.0R (was 3.5R) — wider targets
- SL: 2.5× ATR — avoid noise stops

**QUIET Regime:**
- TP1: 0.8R — grab profit fast
- TP3: 2.0R — tighter targets
- SL: 1.5× ATR — tighter stop

### 🎨 Enhanced Telegram UI
**Before:**
```
🩸 Big Drop Alert
NVDA down 5.2%
RSI: 42
```

**After:**
```
🚨 BIG DROP — NVDA 💎
━━━━━━━━━━━━━━━━
📉 -5.2% today
💰 $487.20
⚡ RSI: 42 (oversold)
🎯 Above POC — buyers control
📅 Earnings: 12 days
━━━━━━━━━━━━━━━━
🤖 AI: Profit-taking after run...
```

---

## 📦 What It Does

AlphaEdge is a **4-module market intelligence system**:

1. **🎯 Signal Scanner** (`scanner.py`) — Pine Script v6.10 parity, 12-point scoring
2. **🧠 Market Intel** (`market_intel.py`) — Big moves, sector bleeds, leadership
3. **🌅 Morning Brief** (`morning_brief.py`) — 9 AM daily digest with AI outlook
4. **💎 Dip Scanner** (`dip_scanner.py`) — Oversold uptrend finder

Every alert is **context-rich**: not just "BUY here" but _why, where to enter, where it invalidates, what the market is doing, and what the AI thinks_.

---

## 🎯 Signal Quality System

### 12-Point Confluence (v6.10)

| Pillar | Points | Criteria |
|--------|--------|----------|
| **P1: HTF Trend** | 3 | 4H EMA50 vs EMA200 |
| **P2: Momentum** | 2 | MACD bull + RSI rising |
| **P3: Volume** | 3 | High vol + CVD confirmation |
| **P4: Regime** | 2 | TRENDING or VOLATILE only |
| **P5: Candle Body** | 2 | Body > 50% of bar range |
| **Total** | **12** | Was 9 in v7.0 |

### Signal Quality Tiers

| Tier | SQS Range | Urgency | Grade |
|------|-----------|---------|-------|
| 🏆 ELITE | 90-100 | 🚨🔥🔥 | A+ (12 pts) |
| ⭐ STRONG | 80-89 | 🚨🔥 | A (9-11 pts) |
| ✅ GOOD | 70-79 | 🚨 | A/B (7-10 pts) |
| ⚠️ FAIR | 60-69 | Silent | B (7-8 pts) |

### Quality Gates (All Must Pass)

- ✅ 12-point confluence ≥ 7
- ✅ SQS ≥ 75 (raised to 80 on 1H+)
- ✅ Pillar purity ≥ 3 of 5 aligned
- ✅ Regime = TRENDING or VOLATILE
- ✅ No HTF/LTF conflict (severity-weighted)
- ✅ POC distance < 3.5 ATR
- ✅ EMA200 extension < 8.0 ATR
- ✅ Choppiness Index < 61.8 (1H+ only)
- ✅ Circuit breaker not active
- ✅ Not in quiet hours (10pm-7am)

### Risk Locks (v7.2)

- 🔒 **Same-symbol lock** — one open trade per symbol across all timeframes (no 30m + 1h double risk)
- 🛑 **Post-stop cooldown** — after a stop-out, same symbol+direction blocked 24h (opposite direction allowed)
- 🔗 **Correlation lock** — open trade in a correlation group blocks new entries in that group (e.g. ETH open blocks XRP)

---

## 📱 What You Receive on Telegram

### Alert Types

1. **🚀 New Signal** — Entry, SL, 3 TPs, POC context, AI verdict
2. **✅ TP1/TP2/TP3 Hit** — Progress with next-step guidance
3. **🛑 SL Hit / Trail Exit** — Distinguishes profit exit vs loss
4. **🩸 Big Moves** — Drops ≥5%, gains ≥8% with AI analysis
5. **🏚️ Sector Bleed** — Grouped sector moves (reduce spam)
6. **💪 Leadership** — Stocks holding firm in weak sectors
7. **💎 Dip Opportunities** — Oversold uptrends (3-4× daily)
8. **🌅 Morning Brief** — 9 AM daily setup
9. **📈 Weekly Report** — Friday 5 PM ET: wins/losses, win rate, net R, per-trade outcomes (optional teaser GIF via `WEEKLY_GIF_URL`)

### Quiet Hours Behavior

**10 PM - 7 AM ET:**
- Scanner continues (data collection)
- Alerts queue for morning batch
- **7 AM batch delivery:**
  ```
  🌅 Morning Alert Batch — 3 queued overnight
  ━━━━━━━━━━━━━━━━━━━━
  [Alert 1]
  [Alert 2]
  [Alert 3]
  ```

**Exceptions (bypass quiet hours):**
- VIX spike > 35
- Circuit breaker activated

---

## 🛠️ Tech Stack

| Component | Tech |
|-----------|------|
| Language | Python 3.11 |
| Market Data | yfinance (free) |
| AI Analysis | Google Gemini 2.0 Flash |
| Execution | GitHub Actions (unlimited on public repos) |
| Alerts | Telegram Bot API |
| Timezone | `zoneinfo` (auto EDT/EST) |
| Storage | JSON state files, persisted between runs |

---

## 📦 Setup

### 1. Fork & Clone
```bash
git clone https://github.com/YOUR-USERNAME/alphaedge-scanner
cd alphaedge-scanner
pip install -r requirements.txt
```

### 2. Configure Secrets

GitHub → Settings → Secrets and variables → Actions

| Secret | Value |
|--------|-------|
| `TELEGRAM_TOKEN` | From @BotFather |
| `CHAT_ID` | Your Telegram chat ID |
| `GEMINI_API_KEY` | From Google AI Studio (optional) |

### 3. Customize Watchlist

Edit `symbols.yaml`:
```yaml
regular_hours:
  - symbol: TSLA
    emoji: 🚗
    sector: EV
    roles: [intel, brief, scanner]
```

### 4. Configure v6.10 Features

Edit `symbols.yaml` settings:
```yaml
settings:
  scanner:
    time_filter:
      enabled: true          # Quiet hours 10pm-7am
      quiet_start: "22:00"
      quiet_end: "07:00"
      
    circuit_breaker:
      enabled: true          # Pause after 3 losses
      loss_threshold: 3
      
    dynamic_rr:
      enabled: true          # Regime-based TP/SL
```

### 5. Test Run

GitHub → Actions → Pick workflow → Run workflow

---

## 📁 File Structure

```
alphaedge-scanner/
├── scanner.py              # Main signal engine (v6.10 in progress)
├── market_intel.py         # Market intelligence (v6.10 ✅ quiet hours)
├── dip_scanner.py          # Oversold uptrend finder
├── morning_brief.py        # Daily digest
├── symbols.yaml            # Watchlist (v6.10 ✅ config added)
├── alphaedgev6.10.txt      # Pine Script source (3851 lines)
├── docs/
│   ├── V6.10_MIGRATION_PLAN.md
│   ├── V6.10_PROGRESS.md
│   ├── TELEGRAM_ALERTS.md   # Complete alert reference
│   └── SESSION_SUMMARY_2026-06-30.md
└── .github/workflows/
    ├── scanner.yml         # Every 10-30 min
    ├── intel.yml           # Every 30 min
    ├── dip_scanner.yml     # 3-4× per day
    └── morning_brief.yml   # 9 AM weekdays
```

---

## 🔬 Migration Status

**Current:** v6.10 migration in progress

| Feature | Status |
|---------|--------|
| Configuration (symbols.yaml) | ✅ Done |
| Quiet Hours Gate (market_intel + scanner) | ✅ Done |
| Free-tier safety fixes (8 total) | ✅ Done |
| Enhanced Alert UI | ⏳ Next |
| 12-Point Scoring | ⏳ Pending |
| Circuit Breaker | ⏳ Pending |
| Dynamic TP/SL | ⏳ Pending |

**See:** `docs/V6.10_PROGRESS.md` and `docs/FREE_TIER_FIXES.md` for detail

---

## 📖 Documentation

- **[V7.0_RELEASE.md](docs/V7.0_RELEASE.md)** — v7.0 release notes + backtest plan
- **[V7.0_REVIEW_AND_DESIGN.md](docs/V7.0_REVIEW_AND_DESIGN.md)** — Deep review of v6.10 + design rationale
- **[TELEGRAM_ALERTS.md](docs/TELEGRAM_ALERTS.md)** — All 7 alert types with examples
- **[V6.10_MIGRATION_PLAN.md](docs/V6.10_MIGRATION_PLAN.md)** — Technical blueprint (legacy)
- **[V6.10_PROGRESS.md](docs/V6.10_PROGRESS.md)** — v6.10 migration tracker
- **[FREE_TIER_FIXES.md](docs/FREE_TIER_FIXES.md)** — 8 free-tier safety fixes
- **[SETUP.md](SETUP.md)** — Detailed setup instructions

---

## ⚠️ Disclaimer

Signal scanner for educational/informational purposes. **Not financial advice.**  
Does **not execute trades**. Always verify signals, manage risk, do your own research.

**Trade at your own risk.**

---

## 📜 Credits

**Author:** VAMSI  
**License:** Personal use  
**Pine Script:** AlphaEdge v6.10 (TradingView companion)  

---

*Built for traders who want context, not just arrows.*
