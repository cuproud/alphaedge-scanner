# AlphaEdge Telegram Alerts Guide

**v6.10** — Complete alert reference with examples

---

## 📱 Alert Types

AlphaEdge sends 7 types of alerts via Telegram:

1. **🚀 Signal Alerts** — New trade setups (scanner.py)
2. **✅ Trade Progress** — TP1/TP2/TP3 hits (scanner.py)
3. **🛑 Trade Exits** — SL hits, timeouts (scanner.py)
4. **🩸 Market Intel** — Big moves, sector bleeds (market_intel.py)
5. **💎 Dip Opportunities** — Oversold uptrends (dip_scanner.py)
6. **🌅 Morning Brief** — Daily digest (morning_brief.py)
7. **📈 Weekly Summary** — Performance recap (scanner.py)

---

## ⏰ Quiet Hours (v6.10 NEW)

**No alerts 10 PM - 7 AM ET**

- Scanner continues running (data collection)
- Alerts queue for 7:00 AM batch delivery
- **Exceptions (bypass quiet hours):**
  - VIX spike > 35
  - Circuit breaker activated
  - Critical system events

**Morning Batch:**
```
🌅 Morning Alert Batch — 3 queued overnight
━━━━━━━━━━━━━━━━━━━━
[Queued alert 1]
[Queued alert 2]
[Queued alert 3]
```

---

## 1. 🚀 Signal Alerts

### Format (v6.10 Enhanced)

```
🚨🔥 STRONG BUY — NVDA 💎
━━━━━━━━━━━━━━━━━━━━

📊 SIGNAL QUALITY
━━━━━━━━━━━━━━━━
⭐ SQS: 82/100 (STRONG)
📈 Score: 10/12 (A)
🎯 Pillars: 4/5 aligned
🏷️ Regime: TRENDING

🔢 CONFLUENCE (10/12)
━━━━━━━━━━━━━━━━
P1 HTF Trend:    ✅✅✅ (3/3)
P2 Momentum:     ✅✅ (2/2)
P3 Volume:       ✅✅✅ (3/3)
P4 Regime:       ✅✅ (2/2)
P5 Candle Body:  ⬜⬜ (0/2)

💰 ENTRY & TARGETS
━━━━━━━━━━━━━━━━
Entry:  $487.20
SL:     $478.50 (-1.79%)
TP1:    $495.90 (+1.79% · 1R)
TP2:    $505.60 (+3.78% · 2R)
TP3:    $520.72 (+6.88% · 3.5R)

Risk $8.70 → Make $33.52 (3.9× reward)

📍 KEY LEVELS
━━━━━━━━━━━━━━━━
🎯 POC:    $485.30 (above — buyers control)
📊 VWAP:   $484.10 (above)
📉 Support: $478.20 (swing low)
📈 Resist:  $492.50 (prior high)

⚡ TECHNICALS
━━━━━━━━━━━━━━━━
RSI: 58 (neutral)
ADX: 28 (strong trend)
EMA50: $481.20 ✅
EMA200: $465.40 ✅

🌍 MARKET CONTEXT
━━━━━━━━━━━━━━━━
SPY: +0.4% | QQQ: +0.6% | VIX: 14.2 (calm)
Sector: AI/Semis +0.8% (leading)

🤖 AI INSIGHT
━━━━━━━━━━━━━━━━
Resumption after healthy pullback to EMA50.
Volume confirms institutional buying.
Earnings in 3 weeks — no near-term risk.
TRENDING regime favors continuation.

⏰ 30m · 14:30 ET
```

### Urgency Levels

| Emoji | SQS Range | Description |
|-------|-----------|-------------|
| 🚨🔥🔥 | 90-100 | ELITE — all systems aligned |
| 🚨🔥 | 80-89 | STRONG — high conviction |
| ⭐ | 70-79 | SOLID — above threshold |
| ⚠️ | 60-69 | FAIR — consider half size |

### Signal Filters Applied

Every signal passes these gates:
- ✅ 12-point confluence ≥ 7
- ✅ SQS ≥ 75 (raised to 80 on 1H+)
- ✅ Pillar purity ≥ 3 of 5
- ✅ Regime = TRENDING or VOLATILE
- ✅ No HTF/LTF conflict (severity-weighted)
- ✅ POC distance < 3.5 ATR
- ✅ EMA200 distance < 8.0 ATR
- ✅ Choppiness Index < 61.8 (1H+ only)
- ✅ Circuit breaker not active
- ✅ Not in quiet hours (10pm-7am)

---

## 2. ✅ Trade Progress Alerts

### TP1 Hit

```
✅ TP1 HIT — NVDA 💎
━━━━━━━━━━━━━━━━
Entry:  $487.20
TP1:    $495.90 ✅
Gain:   +$8.70 (+1.79% · 1R)

Trail moved to breakeven
Position: 60% remaining

⏰ 2h 15m in trade
```

### TP2 Hit

```
✅ TP2 HIT — NVDA 💎
━━━━━━━━━━━━━━━━
Entry:  $487.20
TP2:    $505.60 ✅
Gain:   +$18.40 (+3.78% · 2R)

Trail moved to TP1 ($495.90)
Position: 25% remaining

⏰ 4h 30m in trade
```

### TP3 Hit

```
🏆 TP3 HIT — NVDA 💎
━━━━━━━━━━━━━━━━
Entry:  $487.20
TP3:    $520.72 ✅
Gain:   +$33.52 (+6.88% · 3.5R)

Trailing final 25%
Position closed if trail hits

⏰ 8h 45m in trade
```

---

## 3. 🛑 Trade Exit Alerts

### SL Hit (Loss)

```
❌ SL HIT — NVDA 💎
━━━━━━━━━━━━━━━━
Entry:  $487.20
SL:     $478.50 ❌
Loss:   -$8.70 (-1.79% · -1R)

Exit: Structure SL hit
Duration: 1h 20m

Regime: TRENDING
Grade: A (10/12)

⏰ 15:50 ET
```

### Trail Exit (Win)

```
✅ TRAIL EXIT — NVDA 💎
━━━━━━━━━━━━━━━━
Entry:  $487.20
Exit:   $512.40 (trail)
Gain:   +$25.20 (+5.17% · 2.9R)

TP2 hit, trail locked profit
Final position closed

Duration: 6h 15m
Grade: A (10/12)

⏰ 20:45 ET
```

### Timeout (72h Auto-Close)

```
⏰ TIMEOUT — NVDA 💎
━━━━━━━━━━━━━━━━
Entry:  $487.20
Exit:   $490.30 (manual close)
Gain:   +$3.10 (+0.64% · +0.36R)

72h limit reached, no TP hit
Small win, trail never engaged

Duration: 3d 0h 0m
Grade: B (7/12)

⏰ 14:30 ET
```

---

## 4. 🩸 Market Intel Alerts

### Big Drop Alert

```
🚨 BIG DROP — NVDA 💎
━━━━━━━━━━━━━━━━
📉 -5.2% today
💰 $487.20
🎯 Verdict: BUY ZONE

⚡ TECHNICALS
━━━━━━━━━━━━━━━━
RSI: 42 (oversold)
EMA50: $481.20 ✅ (above)
EMA200: $465.40 ✅ (above)

🎯 POC: $485.30 (above — buyers control)
📍 52W Range: 68% (mid-range)
🏔️ ATH: -12% (pullback, 2 weeks ago)

📅 EARNINGS
━━━━━━━━━━━━━━━━
Next: Dec 18 (12 days)
No near-term risk

🌍 MARKET
━━━━━━━━━━━━━━━━
SPY: +0.4% | QQQ: +0.6% | VIX: 14.2

💪 RELATIVE STRENGTH
━━━━━━━━━━━━━━━━
vs SPY (5d): -1.2% (in-line)

🤖 AI ANALYSIS
━━━━━━━━━━━━━━━━
Profit-taking after strong run.
Still above key support levels.
Healthy pullback in uptrend.
Consider entry on dip extension.

⏰ 11:45 ET
```

### Sector Bleed Alert (v6.10 Grouped)

```
🏚️ SECTOR BLEED — AI / Semis
━━━━━━━━━━━━━━━━
💎 NVDA:  -6.0%
🔴 AMD:   -5.5%
🏭 TSM:   -4.8%
💾 MU:    -4.2%
⚡ AVGO:  -3.9%
━━━━━━━━━━━━━━━━
Avg: -5.4% | Volatility spike

🌍 Market: SPY -0.2% | VIX 18.5
⚠️ Broad tech weakness
Monitor for recovery signs

⏰ 13:20 ET
```

### Leadership Alert

```
💪 LEADERSHIP — Holding Strong
━━━━━━━━━━━━━━━━
While AI/Semis bleed -5.4%:

🟢 NVDA: -2.1% (leader)
🟢 CRWD: +0.8% (leader)
🟢 TSLA: -0.5% (leader)

📊 Sector avg: -5.4%
💪 These held firm
→ Watch for rotation

⏰ 13:30 ET
```

---

## 5. 💎 Dip Scanner Alerts

```
💎 DIP OPPORTUNITIES — 5 Found
━━━━━━━━━━━━━━━━

1. GOOGL 🔍 [Score: 12/14]
━━━━━━━━━━━━━━━━
Price: $142.50
RSI: 38 (oversold)
Above EMA200: ✅
ATH: -8% (shallow pullback)
Buy Zone: $140-143
━━━━━━━━━━━━━━━━

2. TSLA 🚘 [Score: 11/14]
━━━━━━━━━━━━━━━━
Price: $248.20
RSI: 35 (oversold)
Above EMA200: ✅
ATH: -15% (healthy pullback)
Buy Zone: $245-250
━━━━━━━━━━━━━━━━

3. AMD ⚡ [Score: 10/14]
━━━━━━━━━━━━━━━━
Price: $168.40
RSI: 32 (oversold)
Above EMA200: ✅
ATH: -18% (deeper pullback)
Buy Zone: $165-170
━━━━━━━━━━━━━━━━

⏰ 10:30 ET | Next scan: 13:30 ET
```

---

## 6. 🌅 Morning Brief

```
🌅 MORNING BRIEF — Dec 6, 2026
━━━━━━━━━━━━━━━━━━━━

🌍 MARKET SNAPSHOT
━━━━━━━━━━━━━━━━
SPY: $485.20 (+0.3% pre)
QQQ: $412.80 (+0.5% pre)
VIX: 13.8 (calm)

🤖 AI OUTLOOK
━━━━━━━━━━━━━━━━
Markets poised for continuation after
yesterday's consolidation. Tech leading
with AI/Semis sector strength. Monitor
VIX for any spike above 18.

📅 EARNINGS TODAY
━━━━━━━━━━━━━━━━
⚠️ NVDA reports after close
⚠️ AMD reports after close

🌡️ SECTOR HEATMAP
━━━━━━━━━━━━━━━━
AI/Semis:    +0.8% 🟢
Mega Tech:   +0.5% 🟢
Crypto:      +2.1% 🟢🟢
EV/Auto:     -0.2% 🔴
Quantum:     +1.5% 🟢

🚀 TOP GAINERS
━━━━━━━━━━━━━━━━
ETH-USD: +3.2%
IONQ:    +2.8%
NVDA:    +1.9%

📉 TOP LOSERS
━━━━━━━━━━━━━━━━
TSLA:    -1.5%
MU:      -0.8%
OKLO:    -0.6%

🎯 BUY ZONE CANDIDATES
━━━━━━━━━━━━━━━━
✅ GOOGL: $142 (BUY)
⚠️ AMD: $168 (WAIT — earnings today)
✅ TSLA: $248 (BUY)

🚫 AVOID
━━━━━━━━━━━━━━━━
❌ QBTS: Overbought, ATH+25%
❌ RGTI: Parabolic, no support

⏰ 9:00 AM ET
```

---

## 7. 📈 Weekly Summary

```
📈 WEEKLY SUMMARY — Dec 1-7, 2026
━━━━━━━━━━━━━━━━━━━━

📊 PERFORMANCE
━━━━━━━━━━━━━━━━
Trades: 24
Wins: 15 (62.5%)
Losses: 9
Total R: +18.5R

💰 R BREAKDOWN
━━━━━━━━━━━━━━━━
Wins:   +28.3R
Losses: -9.8R
Net:    +18.5R
Avg/Trade: +0.77R

🏆 GRADE PERFORMANCE
━━━━━━━━━━━━━━━━
A+ (12pts): 3 trades, 100% WR, +8.2R
A  (9-11):  8 trades, 75% WR, +12.4R
B  (7-8):   13 trades, 46% WR, -2.1R

📊 REGIME PERFORMANCE
━━━━━━━━━━━━━━━━
TRENDING:  12 trades, 75% WR, +14.2R
VOLATILE:  8 trades, 50% WR, +6.8R
QUIET:     4 trades, 25% WR, -2.5R

🔥 BEST TRADES
━━━━━━━━━━━━━━━━
1. NVDA: +3.8R (TP3)
2. TSLA: +3.5R (TP3)
3. ETH-USD: +2.9R (TP2 trail)

💥 WORST TRADES
━━━━━━━━━━━━━━━━
1. MU: -1R (SL)
2. OKLO: -1R (SL)
3. QBTS: -1R (SL)

🎯 SQS THRESHOLD
━━━━━━━━━━━━━━━━
Current: 75
B-grade WR: 46% (target 40%+)
Status: ✅ Threshold optimal

🔧 CIRCUIT BREAKER
━━━━━━━━━━━━━━━━
Activated: 0 times
Max loss streak: 2

⏰ Sunday 9:00 PM ET
```

---

## 📐 Alert Formatting Rules (v6.10)

### Emoji Usage

| Emoji | Meaning |
|-------|---------|
| 🚨 | High-priority signal |
| 🔥 | Strong conviction |
| ⭐ | Solid setup |
| ⚠️ | Warning / caution |
| ✅ | Confirmed / success |
| ❌ | Failed / loss |
| 🏆 | Big win / TP3 |
| 💎 | Symbol emoji (per symbols.yaml) |
| 🎯 | Target / level |
| ⚡ | Technical indicator |
| 🌍 | Market context |
| 🤖 | AI analysis |
| 📊 | Chart / data |
| 💰 | Price / money |
| ⏰ | Time / duration |

### Dividers

```
━━━━━━━━━━━━━━━━  (section separator)
──────────────────  (sub-section)
```

### Compact Layout Principles

1. **Symbol header:** `🚨 ACTION — SYMBOL 💎`
2. **Section headers:** Bold + divider
3. **Data rows:** `Label: Value (context)`
4. **No verbose prose** — facts only
5. **AI last** — optional 4-line max

### Color Coding (via emoji)

- 🟢 Green = bullish / up / good
- 🔴 Red = bearish / down / bad
- 🟡 Yellow = neutral / caution
- ⚪ White = neutral (when no color needed)

---

## 🔕 Alert Frequency

### Real-Time (During Market Hours)
- Signal alerts: As they fire (every 10-30 min checks)
- Trade progress: Immediate when TP/SL hit
- Market intel: Every 30 min scan

### Scheduled
- Dip scanner: 10:30 AM, 1:30 PM, 3:30 PM ET
- Morning brief: 9:00 AM ET weekdays
- Weekly summary: Sunday 9:00 PM ET

### Quiet Hours (10 PM - 7 AM ET)
- **No alerts sent** (except VIX spike bypass)
- Queued for 7:00 AM batch delivery

---

## 🛑 Circuit Breaker Alerts

```
⚠️ CIRCUIT BREAKER ACTIVATED
━━━━━━━━━━━━━━━━━━━━
3 consecutive losses detected
Pausing new signals for 30 min

Last 3 trades:
❌ NVDA: -1R
❌ TSLA: -1R
❌ AMD: -1R

Cooldown ends: 15:20 ET
Monitoring continues

⏰ 14:50 ET
```

**Escalation:**
- 3 losses: 30 min pause
- 4 losses: 60 min pause
- 5 losses: 90 min pause

---

## 🎯 Summary

**Total Alert Types:** 7  
**Quiet Hours:** 10 PM - 7 AM ET  
**Bypass Exceptions:** VIX spike, circuit breaker  
**Morning Batch:** 7:00 AM ET  
**Format:** Emoji-rich, compact cards (v6.10)  

All alerts respect quiet hours unless bypass criteria met.

---

**Last Updated:** 2026-06-30  
**Pine Script:** v6.10  
**Python Modules:** scanner.py, market_intel.py, dip_scanner.py, morning_brief.py
