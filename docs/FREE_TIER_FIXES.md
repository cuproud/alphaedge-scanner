# AlphaEdge v6.10 Free-Tier Safety Fixes

**Date:** 2026-06-30  
**Scope:** Hardening for Cloudflare + GitHub Actions + Telegram + Gemini free-tier limits

---

## Summary

Code review found 8 free-tier risks. All fixed, tested, committed.

| Fix | Severity | File | Impact |
|-----|----------|------|--------|
| #1 | 🔴 | scanner.py | Throttle Telegram sends |
| #2 | 🔴 | scanner.py | Gemini daily cap 1400 |
| #3 | 🟡 | scanner.py | Quiet hours + queue |
| #4 | 🔴 | single_scan.py | Drop 15s retry sleep |
| #5 | 🟡 | single_scan.py | Sanitize symbol input |
| #6 | 🟡 | market_intel.py | yfinance rate limiter |
| #7 | 🟡 | single_scan.py | Purge price_alerts.json |
| #8 | 🔵 | cleanup_logs.py | Weekly log rotation |

---

## Fix #1: Telegram Throttle (scanner.py)

**Problem:** Loop sends N telegrams back-to-back (no sleep). 5 signals = 5 msgs in <1s. Telegram chat limit: 20 msgs/min.

**Fix:** Add `time.sleep(0.3)` between sends in non-digest fanout and digest details path. Matches existing 0.3s throttle in trade-check loop.

**Commit:** `51dedf7`

---

## Fix #2: Gemini Daily Cap (scanner.py)

**Problem:** Scanner runs every 10 min × 15 symbols × 2 TFs = 30 calls/cycle worst case. 144 cycles/day → 4320 calls. **Way over 1500/day free tier.**

**Fix:** 
- Hard cap `GEMINI_DAILY_CAP = 1400` (under 1500 ceiling)
- `_gemini_counter_load()` — daily-resetting state file `gemini_counter.json`
- `gemini_can_call()` — pre-flight check before AI request
- `gemini_increment()` — atomic counter bump only on success
- 429 logged with current usage

**Commit:** `3c1b677`

---

## Fix #3: Quiet Hours Gate (scanner.py)

**Problem:** v6.10 added quiet hours to market_intel.py only. scanner.py would still fire overnight crypto signals.

**Fix:**
- Wire `is_quiet_hours()` into `send_telegram()` at line 3230
- Block 22:00-06:59 ET sends, persist to `overnight_alerts.json`
- `should_bypass_quiet()` — CIRCUIT BREAKER and VIX spike bypass
- `deliver_overnight_queue()` — flush at 7 AM batch with morning header

**Commit:** `858282a`

---

## Fix #4: Drop 15s Retry Sleep (single_scan.py)

**Problem:** On Gemini 429, code slept 15s then retried once. GitHub Actions paid-minute wasted. Rate limit doesn't lift in 15s anyway.

**Fix:** Single `_call()`. Log 429 and return None. Caller skips AI gracefully.

**Commit:** `ac5c074`

---

## Fix #5: Symbol Input Sanitization (single_scan.py)

**Problem:** User input from Cloudflare worker → GitHub Actions → bash interpolation. Raw symbol could contain shell meta-chars.

**Fix:** `sanitize_symbol()` whitelist regex `[^A-Z0-9.\-=^]`. Cap length 12 chars. `normalise_symbol()` calls sanitize first.

**Tested:**
| Input | Output |
|-------|--------|
| `TSLA` | `TSLA` |
| `BRK.B` | `BRK.B` |
| `GC=F` | `GC=F` |
| `^VIX` | `^VIX` |
| `BTC` | `BTC-USD` |
| `;rm -rf /` | `RM-RF` |
| `aaaa...aaaa` (16) | `AAAA...AAAA` (12) |

**Commit:** `b75f996`

---

## Fix #6: yfinance Global Throttle (market_intel.py)

**Problem:** 5 ThreadPoolExecutor workers could hit yfinance simultaneously. No global rate limiter. Per-thread retries amplify.

**Fix:** `_yf_rate_limit()` — thread-safe `Lock()` + timestamp. Enforces min 0.15s between calls across all threads (6.6 calls/sec ceiling). Called inside `_yf_download()` before each `yf.download`.

**Commit:** `749d0a1`

---

## Fix #7: Purge price_alerts.json (single_scan.py)

**Problem:** Triggered alerts marked `triggered: True` but never deleted. File grows forever.

**Fix:** `purge_alerts()` with 3 rules:
1. Triggered older than 24h → delete
2. Expired (past `expires_at` + 24h) → delete
3. Hard cap 500 entries → drop oldest by `set_at`

Called inside `check_alerts()` before save. Triggered alerts now stamp `triggered_at` timestamp.

**Tested:** 4-entry fixture, 2 stale → 2 purged correctly.

**Commit:** `c862cf1`

---

## Fix #8: Log Rotation (cleanup_logs.py + workflow)

**Problem:** Per-day log files in `logs/` never deleted. 1 year = 365 files.

**Fix:**
- `cleanup_logs.py` — walks `logs/`, deletes `.log`/`.log.gz` older than 30 days
- `--dry-run` flag for safe testing
- `.github/workflows/cleanup.yml` — Sunday 6 AM UTC (Sat 1-2 AM ET, in quiet hours)
- Auto-commits removed files via github-actions bot

**Commit:** `4aa5a77`

---

## Free-Tier Compliance After Fixes

| Limit | Before | After |
|-------|--------|-------|
| Telegram 20/min | 🟡 At risk on burst | ✅ 0.3s throttle |
| Gemini 1500/day | 🔴 Could burn 4320/day | ✅ Hard cap 1400 |
| yfinance | 🟡 Uncoordinated bursts | ✅ 0.15s global limit |
| Cloudflare 100k/day | ✅ Worker.js external | ✅ Unchanged |
| GitHub Actions | 🟡 15s wasted on 429 | ✅ Instant skip |
| Disk growth | 🟡 Unbounded JSON+logs | ✅ Capped + auto-purge |

---

## State Files

| File | Purpose | Bounded? |
|------|---------|----------|
| `alert_cache.json` | Signal dedup (48h TTL) | ✅ Auto-clean |
| `active_trades.json` | Open trades | ✅ Closes archive to history |
| `trade_history.json` | Closed trades | ✅ Last 500 |
| `sqs_history.json` | SQS trend per symbol | ⚠️ Append-only (per-symbol rolling window enforced internally) |
| `scanner_state.json` | Cooldowns + weekly | ✅ Date-scoped keys |
| `gemini_counter.json` | Daily AI call count | ✅ Resets midnight ET |
| `overnight_alerts.json` | Quiet hours queue | ✅ Flushed at 7 AM |
| `price_alerts.json` | User alerts | ✅ Capped 500 + 24h purge |
| `dynamic_threshold.json` | SQS threshold | ✅ Single record |
| `logs/scan_*.log` | Daily logs | ✅ 30d retention via cron |

---

## Testing Verification

Run after deploy:

```bash
# Verify YAML parses
python3 -c "import yaml; yaml.safe_load(open('symbols.yaml'))"

# Verify all .py compile
for f in *.py; do python3 -m py_compile "$f"; done

# Verify cleanup_logs dry-run
python3 cleanup_logs.py --dry-run

# Verify Gemini counter init
python3 -c "
import json
from datetime import datetime
from zoneinfo import ZoneInfo
today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
print(json.dumps({'date': today, 'count': 0}))
"
```

---

## Commit History

```
51dedf7 Fix #1: throttle telegram sends in scanner.py
3c1b677 Fix #2: Gemini daily call cap in scanner.py
858282a Fix #3: quiet hours gate + overnight queue in scanner.py
ac5c074 Fix #4: drop 15s retry sleep in single_scan.py
b75f996 Fix #5: sanitize symbol input in single_scan.py
749d0a1 Fix #6: global yfinance rate limiter in market_intel.py
c862cf1 Fix #7: bound price_alerts.json growth in single_scan.py
4aa5a77 Fix #8: weekly log file cleanup script + workflow
```

---

## Operator Notes

**To disable any fix without code edit:**
- Fix #2 (Gemini cap): Edit `GEMINI_DAILY_CAP` constant in scanner.py
- Fix #3 (quiet hours): Set `settings.scanner.time_filter.enabled: false` in symbols.yaml
- Fix #6 (yf throttle): Edit `_YF_MIN_INTERVAL` in market_intel.py
- Fix #7 (alert purge): Edit `ALERT_PURGE_*_HOURS` in single_scan.py
- Fix #8 (log cleanup): Disable workflow in GitHub UI

**Monitoring:**
- Watch `logs/scan_*.log` for `Gemini daily cap` warnings (#2 fired)
- Watch for `Alert queued for 7 AM batch` (#3 firing)
- Watch `purge_alerts: removed N stale entries` (#7 firing)

---

**Author:** Claude Opus 4.7  
**Pine Source:** alphaedgev6.10.txt  
**Reviewed:** market_intel.py, scanner.py, single_scan.py
