---
name: pine-script
description: TradingView Pine Script v6 reference — syntax, common patterns, indicator/strategy differences, repaint avoidance, backtest stat extraction. Use when reading/writing/debugging .pine files or when user asks about TradingView, Pine, AlphaEdge Pine logic.
---

# Pine Script v6 Skill

## When to use this skill

- Reading/editing `*.pine`, `*.pinescript`, `*.txt` Pine sources (e.g., `alphaedge_v7.0.pine`)
- User mentions "Pine", "TradingView", "TV", "indicator", "strategy", "Pine Script"
- Debugging Pine errors (`Mismatched input`, `Undeclared identifier`, etc.)
- Designing repaint-safe signal logic
- Extracting backtest stats from a strategy

## Core syntax (Pine v6)

### Declaration
```pine
//@version=6
indicator("Name", overlay=true, max_labels_count=500, max_lines_count=500, max_bars_back=2000)
// OR
strategy("Name", overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=10)
```

### Inputs (UI controls)
```pine
length    = input.int(14, "Length", minval=1, maxval=200)
mult      = input.float(2.0, "Multiplier", step=0.1)
showLine  = input.bool(true, "Show Line")
tf        = input.timeframe("60", "Timeframe")
src       = input.source(close, "Source")
choice    = input.string("A", "Mode", options=["A", "B", "C"])
col       = input.color(color.red, "Color")
group     = "📊 Group Name"  // visible in UI
```

### Variables
```pine
x = 10          // immutable inferred
var int y = 0   // mutable, persists across bars
y := y + 1      // reassign with :=
```

### Series math
```pine
hl2_val = (high + low) / 2
rng     = ta.tr  // true range
atr14   = ta.atr(14)
rsi14   = ta.rsi(close, 14)
ema50   = ta.ema(close, 50)
[macdLine, sigLine, hist] = ta.macd(close, 12, 26, 9)
```

### Conditionals
```pine
// Ternary
col = close > open ? color.green : color.red

// Switch
result = switch x
    1 => "one"
    2 => "two"
    => "other"

// If (statement, not expression for assignment)
if close > ema50
    label.new(bar_index, high, "above")
```

### Functions
```pine
f_mult(float a, float b) =>
    a * b

f_state() =>
    var int counter = 0
    counter := counter + 1
    counter
```

### Arrays
```pine
var prices = array.new_float()
array.push(prices, close)
if array.size(prices) > 100
    array.shift(prices)
maxP = array.max(prices)
```

### Multi-timeframe data
```pine
htfClose = request.security(syminfo.tickerid, "240", close[1], barmerge.gaps_off, barmerge.lookahead_off)
//                                              ↑ "4H"   ↑ NEVER use without [1] without lookahead_off
```

**REPAINT WARNING:** Use `close[1]` (prior bar) + `lookahead_off` to avoid future-data leak. v6 default is `barmerge.lookahead_off` for `request.security` but be explicit.

### Plotting
```pine
plot(ema50, "EMA 50", color=color.aqua, linewidth=2)
plotshape(buy, "Buy", location=location.belowbar, style=shape.triangleup, color=color.green, size=size.small)
bgcolor(bull ? color.new(color.green, 90) : na)
```

### Labels (for state/debug)
```pine
if buy
    label.new(bar_index, low, "BUY @ " + str.tostring(close, format.mintick),
              style=label.style_label_up, color=color.green, textcolor=color.white)
```

### Tables (for dashboards)
```pine
var table t = table.new(position.top_right, 2, 3, bgcolor=color.black)
if barstate.islast
    table.cell(t, 0, 0, "Score",     text_color=color.white)
    table.cell(t, 1, 0, str.tostring(score))
```

## Indicator vs Strategy

| Aspect | indicator | strategy |
|--------|-----------|----------|
| Alerts | `alert()` / `alertcondition()` | `strategy.entry()` triggers built-in |
| Backtest | Manual via plot + eyeball | Built-in Strategy Tester |
| Position sizing | Calculated, not enforced | Enforced |
| Use case | Live charts, manual exec | Auto-trade simulation, backtest stats |

### Strategy backtest extraction
After `strategy(...)` block, TV's Strategy Tester gives:
- Net Profit
- Total Trades
- Win Rate %
- **Profit Factor** ← key metric
- Max Drawdown
- Sharpe Ratio
- Avg Trade
- Largest Winning/Losing Trade

To get programmatic access in Pine:
```pine
plot(strategy.netprofit, "Net Profit", display=display.data_window)
plot(strategy.wintrades / math.max(1, strategy.closedtrades) * 100, "Win Rate", display=display.data_window)
profitFactor = strategy.grossprofit / math.max(1, strategy.grossloss)
plot(profitFactor, "PF", display=display.data_window)
```

## Repaint avoidance patterns

### Pattern 1: confirmed bar entries
```pine
buyCondition = ...
confirmedBuy = buyCondition and barstate.isconfirmed  // only on closed bar
if confirmedBuy
    // do entry
```

### Pattern 2: HTF data with [1] offset
```pine
// WRONG (repaints):
htfClose = request.security(syminfo.tickerid, "D", close)

// RIGHT (no repaint):
htfClose = request.security(syminfo.tickerid, "D", close[1], barmerge.gaps_off, barmerge.lookahead_off)
```

### Pattern 3: pivot-based signals
```pine
// ta.pivothigh(high, lookback, lookback) returns value at bar [lookback] back
// So pivot detected NOW = real pivot from N bars ago — no repaint
ph = ta.pivothigh(high, 5, 5)
```

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `Mismatched input` | Indentation off | Pine uses 4-space indent for nested blocks |
| `Undeclared identifier` | Variable used before declared | Declare before use, watch `var` scope |
| `Cannot modify const` | Forgot `var` for mutable | `var int x = 0` |
| `Function calls cannot be used in declared local scope` | f() inside `if` | Move declaration outside conditional |
| `Series exceed max bars back` | Looking too far back | Add `max_bars_back=2000` to `indicator(...)` |
| `Script could not be compiled — too many labels` | Label count limit | Add `max_labels_count=500`, prune with `label.delete()` |

## AlphaEdge Pine specifics

### File layout (v7.0)
```
Lines 1-50:    Header comments + version constants
Lines 51-405:  Inputs (grouped via G_* constants)
Lines 406-535: Theme engine (5 themes)
Lines 536-707: Indicators (AE Range, ADX, ATR, RSI, MACD, EMA, VWAP, BB/KC, CHOP)
Lines 708-1188: HTF/MTF data, SMC (OB/FVG/MS/Liq), AVWAP/CVD, Time filter
Lines 1189-1417: SQS engine, MTF scoring
Lines 1418-1700: Signal engine (gates → confirmedBuy/Sell) + Gate Funnel audit
Lines 1701-2120: Trade management (SL/TP/Trail/Re-entry/Circuit breaker)
Lines 2121-2360: Backtest (PF, WR, DD, position sizing, grade-level WR)
Lines 2361-2550: VP + Multi-TF POC
Lines 2551+:    Visual layer (plots, labels, tables, bgcolor)
```

### Key v6.10 variables
- `bullScore` / `bearScore` — 12pt confluence
- `sqsBull` / `sqsBear` — 0-100 quality score
- `marketRegime` — TRENDING / VOLATILE / RANGING / QUIET / TRANSITIONAL
- `confirmedBuy` / `confirmedSell` — final entry signal (barstate.isconfirmed)
- `tradeDir` — 1 long, -1 short, 0 flat
- `entryPrice`, `slPrice`, `tp1Price`, `tp2Price`, `tp3Price`, `trailPrice`
- `consecutiveLosses` — circuit breaker counter
- `pocPrice` — current Volume Profile POC

## Testing in TradingView

1. Open chart of test symbol (e.g., NVDA, BTCUSD)
2. Pine Editor → paste full script → Save → Add to Chart
3. For backtest stats: convert `indicator()` → `strategy()` + add `strategy.entry(...)` / `strategy.exit(...)` calls at signal points
4. Strategy Tester tab → review Performance Summary
5. Test on multiple symbols (4+ ideal) and timeframes
6. Walk-forward: split chart 70/30 in-sample/out-sample using `properties.commission` + dates

## When editing Pine for AlphaEdge

1. **Preserve gate structure** — don't reorder unless intended; each gate stamps the funnel audit
2. **Match TF branches** — 15m/30m/1H+ have different calibration; keep parity
3. **Update version constants** — `VERSION = "v7.0"`, `VERSION_FULL = "AlphaEdge v7.0"`
4. **Update indicator title** — `indicator("AlphaEdge v7.0 by VAMSI", shorttitle="AE v7.0", ...)`
5. **Document change** in header comment block (`v7.0 CHANGES (2026-XX-XX):`)
6. **Test compile in TV** before claiming done — `tradingview.com` Pine Editor
7. **Watch label/box/line counts** — TV soft-limits at 500; v6.10 already at edge

## Useful pine patterns from AlphaEdge

### TF-aware tuning
```pine
float threshold = timeframe.in_seconds() <= 900 ? 0.003 :     // 15m
                   timeframe.in_seconds() <= 1800 ? 0.005 :    // 30m
                   0.006                                        // 1H+
```

### Atomic state update
```pine
var float lastPrice = na
if condition
    lastPrice := close  // only updates on event
```

### Severity-weighted gate
```pine
int disagree = 0
disagree += htfOpposes ? 1 : 0
disagree += structConflict ? 1 : 0
disagree += mtfBlock ? 1 : 0
int minScore = disagree == 0 ? 0 : disagree == 1 ? 8 : disagree == 2 ? 10 : 11
bool blocked = disagree > 0 and bullScore < minScore
```

## References

- [Pine Script v6 docs](https://www.tradingview.com/pine-script-docs/welcome/)
- [Pine Script Reference Manual](https://www.tradingview.com/pine-script-reference/v6/)
- [Pine Script Style Guide](https://www.tradingview.com/pine-script-docs/writing/style-guide/)
