# 2026-07-17 — Risk locks + weekly report timing

## Weekly report late (17:27 vs 17:00 ET)
Not a timezone bug — `ZoneInfo("America/New_York")` correct everywhere.
Cause: GitHub cron lag (3–10 min) + weekly block sat at end of main()
after 8–12 min scan. Fix: moved send before scan loop (trade check still
runs first, so same-day closes included). `fmt_time` now "5:27 PM ET".

## 1W/10 week — risk stacking, not signal quality
5 of 7 losses were duplicates: XRP-USD SELL stopped 3× (cooldown expired,
re-fired), CRWD BUY on 30m + 1h simultaneously. Added:
- `has_open_trade_for_symbol` — one open trade per symbol, all TFs
- `is_post_stop_blocked` — 24h same symbol+direction block after stop-out
  (`POST_STOP_COOLDOWN_HOURS`); opposite direction / trailed winners exempt
- `get_correlated_open` — open trade in CORRELATION_GROUPS blocks group

Commit: ba5e7da
