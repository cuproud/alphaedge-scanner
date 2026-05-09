# AlphaEdge Bot — Setup & Architecture

## Overview
Telegram bot that analyses stocks and crypto on demand.
Send a ticker in Telegram → get a full technical analysis back.

```
Telegram → Cloudflare Worker → GitHub Actions → single_scan.py → Telegram
```

---

## Components

### 1. Telegram Bot
- Created via @BotFather
- Bot username: @VK_AlphaEdge_bot
- Webhook points to Cloudflare Worker
- All messages route through the Worker

### 2. Cloudflare Worker
- File: `worker.js`
- URL: `https://alphaedge-bot.cuproud.workers.dev`
- Free tier: 100,000 requests/day
- Receives Telegram messages, parses commands, dispatches to GitHub
- Sends instant ACK back to Telegram before GitHub even starts

### 3. GitHub Actions
- Repo: `cuproud/alphaedge-scanner`
- Workflow: `.github/workflows/single_scan.yml`
- Triggered by `repository_dispatch` events: `analyze_symbol` and `bot_command`
- Runs `single_scan.py` with the command payload

### 4. single_scan.py
- Main analysis engine
- Fetches data via yfinance
- Calculates RSI, EMA, squeeze, divergence, sector context
- Calls Gemini AI for analysis
- Sends formatted result back to Telegram

---

## Environment Variables

### Cloudflare Worker (Settings → Variables and Secrets)
| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub Personal Access Token (classic) |
| `TELEGRAM_TOKEN` | Telegram bot token from @BotFather |
| `CHAT_ID` | Your Telegram chat/user ID |

### GitHub Repo Secrets (Settings → Secrets → Actions)
| Secret | Description |
|--------|-------------|
| `TELEGRAM_TOKEN` | Telegram bot token |
| `CHAT_ID` | Your Telegram chat/user ID |
| `GEMINI_API_KEY` | Google Gemini API key for AI analysis |

---

## GitHub Token Requirements
- Type: Classic Personal Access Token
- Scopes required:
  - `repo` — full repo access
  - `workflow` — trigger GitHub Actions ← REQUIRED for dispatch

Generate at: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)

---

## Telegram Webhook Setup
Set webhook (run in browser):
```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://alphaedge-bot.cuproud.workers.dev
```

Verify webhook:
```
https://api.telegram.org/bot<TOKEN>/getWebhookInfo
```

Expected response:
```json
{
  "url": "https://alphaedge-bot.cuproud.workers.dev/",
  "has_custom_certificate": false,
  "pending_update_count": 0
}
```

---

## Bot Commands

### Stock Analysis
```
TSLA                  — full analysis
TSLA short            — 3-line quick summary
TSLA week             — weekly timeframe
TSLA monthly          — monthly timeframe
TSLA NVDA AMD         — multiple stocks (up to 5)
```

### Watchlist
```
scan                  — rank all symbols by momentum
watchlist             — same as scan
my stocks             — same as scan
```

### Top Movers
```
top                   — today's top gainers & losers
movers                — same as top
gainers               — same as top
losers                — same as top
```

### Brief
```
brief                 — auto morning/evening/weekend brief
briefing              — same as brief
morning               — same as brief
summary               — same as brief
recap                 — same as brief
```

### Price Alerts
```
alert TSLA 450        — alert when TSLA hits $450
alert TSLA 450 above  — alert when TSLA rises to $450
alert TSLA 400 below  — alert when TSLA falls to $400
cancel TSLA           — cancel all alerts for TSLA
alerts                — list all active alerts
my alerts             — same as alerts
```

### Crypto Shortcuts
```
BTC                   — auto-expands to BTC-USD
ETH                   — auto-expands to ETH-USD
SOL                   — auto-expands to SOL-USD
XRP                   — auto-expands to XRP-USD
DOGE                  — auto-expands to DOGE-USD
GOLD                  — auto-expands to GC=F
OIL                   — auto-expands to CL=F
SILVER                — auto-expands to SI=F
```

### Help
```
help                  — show command list
?                     — same as help
commands              — same as help
```

### Slash commands also work
```
/scan
/top
/brief
/help
/TSLA
```

---

## GitHub Actions Workflow
File: `.github/workflows/single_scan.yml`

Triggers:
- `repository_dispatch` with types: `analyze_symbol`, `bot_command`
- `workflow_dispatch` (manual run from GitHub UI)

Payload structure for `analyze_symbol`:
```json
{
  "event_type": "analyze_symbol",
  "client_payload": {
    "symbol": "TSLA",
    "mode": "full",
    "timeframe": "1d",
    "sequence": 1
  }
}
```

Payload structure for `bot_command`:
```json
{
  "event_type": "bot_command",
  "client_payload": {
    "command": "scan"
  }
}
```

Alert command payload:
```json
{
  "event_type": "bot_command",
  "client_payload": {
    "command": "alert",
    "symbol": "TSLA",
    "price": 450.0,
    "direction": "above"
  }
}
```

---

## File Structure
```
alphaedge-scanner/
├── .github/
│   └── workflows/
│       └── single_scan.yml       # Main workflow — handles all commands
├── single_scan.py                # Main analysis + command router
├── market_intel.py               # Data fetching, indicators, Telegram sender
├── morning_brief.py              # Morning/evening brief builder
├── scanner.py                    # Scheduled scanner + weekly summary
├── symbols.yaml                  # Watchlist symbols
├── requirements.txt              # Python dependencies
├── price_alerts.json             # Active price alerts (auto-generated)
├── scanner_state.json            # Scanner state cache (auto-generated)
├── worker.js                     # Cloudflare Worker source (reference copy)
└── SETUP.md                      # This file
```

---

## How GitHub Actions Receives the Payload
The workflow passes the full client_payload as JSON to single_scan.py:
```yaml
run: |
  python single_scan.py "$(python3 -c "
  import json
  print(json.dumps({
      'event_type':  '${{ github.event.action || 'analyze_symbol' }}',
      'command':     '${{ github.event.client_payload.command || inputs.command }}',
      'symbol':      '${{ github.event.client_payload.symbol || inputs.symbol }}',
      'mode':        '${{ github.event.client_payload.mode || inputs.mode || 'full' }}',
      'timeframe':   '${{ github.event.client_payload.timeframe || inputs.timeframe || '1d' }}',
      'price':       '${{ github.event.client_payload.price || inputs.price }}' or None,
      'direction':   '${{ github.event.client_payload.direction || inputs.direction || 'auto' }}',
  }))
  ")"
```

---

## Debugging

### Check Cloudflare Worker logs
- Cloudflare Dashboard → Workers & Pages → alphaedge-bot → Logs → Begin log stream
- Send a message in Telegram and watch for INCOMING, TEXT, DISPATCH, TG_ACK entries

### Check GitHub Actions logs
- GitHub → Actions tab → find the triggered run → check step output

### Common errors
| Error | Cause | Fix |
|-------|-------|-----|
| 401 from GitHub | Token invalid/expired | Regenerate token, update in Cloudflare |
| 403 from GitHub | Missing `workflow` scope | Add `workflow` scope to token |
| 403 User-Agent | Missing User-Agent header | Add `"User-Agent": "AlphaEdge-Bot/1.0"` |
| 404 from GitHub | Wrong repo name | Check REPO constant in worker.js |
| No logs in Cloudflare | Webhook not set | Run setWebhook URL in browser |
| Bot analysing pinned message words | Pinned message leaking | Worker ignores `pinned_message` events |

---

## Version History
- v1.0 — Basic Pipedream + GitHub Actions pipeline
- v2.0 — Added alert system, MTF verdicts, squeeze detection
- v2.1 — Moved webhook from Pipedream to Cloudflare Workers (100k/day free)
- v2.2 — Added instant ACK, slash commands, crypto aliases, deduplication, unknown command feedback
