Do a deep audit of the entire codebase and validate the full logical flow, data correlation flow, and execution lifecycle end-to-end. This system processes Telegram messages and returns on-demand stock analysis alerts, so accuracy, consistency, latency, formatting quality, indicator correctness, and resilience are critical.

Perform a comprehensive block-by-block analysis of the entire codebase.

For every module, function, condition, parser, formatter, API call, indicator calculation, async flow, retry mechanism, caching layer, and Telegram response builder:

* Analyze the purpose of the block
* Validate whether the logic is correct
* Check whether the implementation is stable across future iterations
* Verify edge-case handling
* Detect hidden bugs, race conditions, stale state issues, async timing problems, and formatting inconsistencies
* Identify duplicated logic, dead code, unnecessary abstraction, and weak naming
* Review maintainability, readability, extensibility, and performance
* Score each block internally before suggesting improvements
* Think twice before changing any line or structure:

  * Why is this change needed?
  * Does it improve long-term reliability?
  * Will it break downstream flows?
  * Will it remain stable on future iterations?
  * Is there a cleaner or more scalable design?

Perform a true architectural audit — not just linting or superficial review.

Focus heavily on:

1. Telegram Alert Quality

* Audit all Telegram message formatting
* Identify inconsistencies in spacing, emojis, markdown usage, alignment, separators, bullet structure, capitalization, and readability
* Ensure alerts are visually consistent across bullish, bearish, neutral, intraday, and swing scenarios
* Verify Telegram markdown compatibility and escaping
* Ensure formatting never breaks on special characters or ticker names
* Improve readability for mobile Telegram users
* Ensure important information appears in the correct order of priority

2. Stock Analysis Accuracy
   Validate the correctness of:

* RSI calculations
* MACD
* Volume analysis
* Trend detection
* Momentum logic
* Multi-timeframe correlation
* Support/resistance logic
* Price action interpretation
* Signal scoring
* Confidence scoring
* Entry/exit logic
* Risk/reward calculations
* Sentiment aggregation
* AI-generated summaries
* Indicator synchronization timing

Specifically verify:

* Is RSI correctly populated everywhere?
* Is RSI timeframe-aware?
* Is RSI stale anywhere due to cache reuse?
* Are indicators computed using closed candles only?
* Are values mismatched across timeframes?
* Are indicator labels always aligned with actual values?

3. Context & Company Information
   In the opening section of each alert:

* Include full company name alongside ticker
* Include exchange if available
* Include sector/industry if relevant
* Improve contextual awareness before technical analysis begins

Example:
AAPL → Apple Inc. (NASDAQ)
TSLA → Tesla, Inc. (NASDAQ)

4. Data Pipeline Validation
   Audit:

* Market data fetching
* API reliability
* Retry/fallback handling
* Rate limiting
* Websocket vs polling logic
* Cache invalidation
* Timestamp consistency
* Timezone handling
* Candle aggregation
* Delayed vs real-time data labeling

5. Signal Correlation Flow
   Validate whether:

* Indicators support each other logically
* Contradictory signals are detected
* Weak-confidence setups are filtered
* Summary conclusions actually match underlying metrics
* Final recommendation aligns with technical evidence

Check for cases where:

* RSI says oversold but summary says overbought
* Trend says bullish while momentum is bearish
* Confidence score does not match actual signal quality
* Recommendation contradicts raw indicator data

6. AI Summary & Narrative Logic
   Audit all generated commentary:

* Remove repetitive phrasing
* Prevent hallucinated reasoning
* Ensure summaries are data-backed
* Improve analyst-style readability
* Ensure bearish/bullish wording aligns with actual metrics
* Reduce generic filler language
* Make insights concise but information-dense

7. Error Handling & Stability
   Check:

* Missing ticker handling
* Invalid symbols
* Empty API responses
* Timeout handling
* Partial indicator failures
* Telegram send failures
* Markdown parsing failures
* NaN propagation
* Undefined/null handling
* Async crashes
* Memory leaks
* Infinite retry loops

8. Performance & Scalability
   Analyze:

* Unnecessary API calls
* Duplicate indicator calculations
* Serialization overhead
* Slow async chains
* Blocking operations
* Scalability under multiple Telegram requests
* Queueing strategy
* Concurrency safety

9. Code Quality Expectations
   Provide:

* Structural improvements
* Refactor recommendations
* Suggested abstractions
* Better naming conventions
* Modularization opportunities
* Reliability improvements
* Observability/logging improvements
* Testing recommendations
* Monitoring recommendations

10. Output Expectations
    For every major issue found:

* Explain the root cause
* Explain why it matters
* Explain downstream impact
* Propose corrected logic
* Suggest production-grade implementation improvements

Prioritize:

* Correctness
* Reliability
* Signal integrity
* Telegram UX quality
* Long-term maintainability
* Future scalability

Do not provide shallow feedback. Perform a production-level engineering and trading-system audit.
