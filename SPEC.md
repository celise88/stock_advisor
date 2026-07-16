# Stock Advisor Product Specification

## 1) Product Goal
Deliver an educational-first day-trading workspace for beginner and intermediate retail traders that combines:
- recurring opportunity scanning,
- fundamentals + technical interpretation in plain language,
- intraday charting with context-rich candle interpretation,
- quote visibility, trade execution, and trade-performance tracking.

## 2) Target User
- Newer traders who need concise "what this means / what to do next" guidance.
- Users who want a single pane for scanner, analysis, chart, quote, and journal.

## 3) Current Functional Scope

### 3.1 Scanner
The scanner runs continuously on a configurable interval and evaluates a configured symbol universe.

Configurable criteria (UI + API):
- `market_cap > market_cap_min`
- `average_volume > avg_volume_min`
- `relative_volume > relative_volume_min`

Scanner output behavior:
- UI table shows only symbols that currently pass all criteria.
- Row-level details include:
  - symbol,
  - current price,
  - market cap,
  - average volume,
  - relative volume,
  - pass/fail checks,
  - day move ($ and %),
  - trigger timestamp (`triggered_at`) for current pass state.
- Trigger time is set when a symbol newly transitions to pass, and resets if it later fails.

Required scanner APIs:
- `GET /api/scanner/results`
- `POST /api/scanner/run`
- `POST /api/scanner/interval/{interval_sec}`
- `POST /api/scanner/criteria?market_cap_min=...&avg_volume_min=...&relative_volume_min=...`

### 3.2 Symbol Workspace - Fundamentals
For the selected symbol, show:
- current price,
- market cap, shares outstanding, beta,
- trailing P/E, EPS, revenue, margins,
- insider/institutional ownership, short float, short ratio,
- 52-week range, average/current/relative volume,
- next earnings date,
- recent 8-K filings,
- recent news and sentiment.

Data notes:
- Multi-provider fallback is used (Schwab, Finnhub, AlphaVantage, optional yfinance).
- Data warnings/errors are surfaced to the UI when upstream feeds degrade.

### 3.3 Symbol Workspace - Technicals
Compute and display:
- SMA, EMA, MACD, RSI, Bollinger, Stochastic, ADL, Chaikin, PSAR proxy, VWMA, ADX, VWAP,
- Fibonacci (0.382 and 0.618) with dynamic context.
- Advanced contextual indicators:
  - OFI (Cont-Kukanov-Stoikov style proxy),
  - VPIN (volume-synchronized informed-flow proxy),
  - trade-flow aggressor imbalance via tick-rule signing,
  - opening/closing auction imbalance proxies,
  - cross-asset leadership using ES/NQ premarket and sector ETF premarket context,
  - options-derived proxies (GEX, put/call skew, unusual sweep-like flow from chain activity).

Interpretation requirements:
- Indicator-by-indicator interpretation + explanation.
- Holistic interpretation layer that includes:
  - `Action Bias` with `Risk Mode` tag (`Aggressive`, `Standard`, `Defensive`),
  - `Holistic Read` (alignment, regime, conflict handling),
  - `Regime Filter (ADX)` guidance.
- Holistic text integrates:
  - indicator alignment,
  - advanced flow context (OFI, VPIN, aggressor/auction),
  - cross-asset leadership and options context when available,
  - regime type,
  - conflict detection,
  - session/day performance context,
  - relative-volume conviction adjustment.

### 3.4 Intraday Charting
Chart requirements:
- 5m / 15m intraday candles,
- selectable days-back window,
- pre/post-market bars visually muted,
- Bollinger + VWAP overlays,
- candle hover text with candle type, interpretation, and action.
- candlestick interpretation panel below the chart that returns:
  - pattern classification,
  - directional bias + confidence,
  - plain-language interpretation,
  - recommendation based on recent candle sequence and context.

Windowing requirement:
- `days=N` means last `N` trading sessions (not a rolling `N*24h` time slice).

Signal display requirement:
- Structural signals are computed and returned, shown in the technical signal list.
- Signal text is not rendered as on-chart overlay blocks.

### 3.5 Quote, Trading, Journal, Performance
Quote:
- Schwab streaming-first real-time quote endpoint when stream is available.
- Graceful fallback to REST quote behavior when stream is unavailable.
- Stream diagnostics support:
  - connection/error visibility in UI,
  - manual stream restart endpoint/action.
- Stream service must tolerate Schwab API method variants (`nyse_book_*` vs `listed_book_*`).
- Stream service must suppress benign subscription response frames that otherwise appear as non-fatal errors.

Trading:
- market/limit order submission with optional stop-loss / take-profit.
- dry-run simulation mode.

Lifecycle + journal:
- order attempt logging,
- order outcome logging linked to attempts (`broker_order_id`, `broker_status`, `broker_error`),
- pending order sync/reconciliation to filled/canceled states,
- broker history import on sync:
  - fetch recent filled/executed Schwab orders,
  - include valid partial fills from replace/cancel chains when filled quantity is present,
  - include nested child-order fills (e.g., bracket exit child orders),
  - record broker order events,
  - derive/update closed trades by matching entry/exit fills (LIFO by symbol),
  - correct previously imported broker trades when newer matching evidence is available,
- manual trade close endpoint,
- journal + performance endpoints/cards.

## 4) Architecture
- FastAPI backend with modular services:
  - providers,
  - analytics,
  - scanner,
  - charting,
  - Schwab client,
  - journal/performance.
- Static frontend (`index.html`, `app.js`, `styles.css`) served by FastAPI.
- In-process background scanner loop.
- In-process background Schwab stream loop for level-one quotes and level-two book data.
- JSONL persistence in `data/`.

## 5) Reliability and Guardrails
- Graceful degradation when a data source fails.
- Explicit warning/error messaging instead of silent failures.
- API responses remain usable when partial data is available.
- Stream worker shutdown/restart should close sockets cleanly to avoid orphan websocket tasks on reload.
- Educational interpretation is informational; user confirms trade actions.

## 6) Out of Scope
- Fully automated execution without user confirmation.
- Multi-user auth and permissions.
- Paid-only data feeds as hard dependencies.
