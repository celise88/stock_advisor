# Build Plan and Delivery Status

## Build Objective
Maintain a production-minded MVP for beginner day traders with:
- configurable scanner,
- fundamentals + technical interpretation,
- intraday charting,
- quote + trading flow,
- journal and performance tracking.

## Stack
- Backend: FastAPI
- Background work: in-process scanner thread
- Data/analytics: pandas, numpy, requests (+ provider SDKs/APIs)
- Charting: Plotly (server-side figure JSON -> client render)
- Frontend: static HTML/CSS/JavaScript served by FastAPI
- Persistence: JSONL files in `data/`

## Implemented Phases

### Phase 1 - Foundation (Complete)
- App scaffold, config loader, static serving, health endpoint.

### Phase 2 - Scanner (Complete)
- Continuous scanner loop.
- Runtime-configurable scanner criteria:
  - market cap minimum,
  - average volume minimum,
  - relative volume minimum.
- Runtime-configurable interval.
- Scanner table shows pass-only matches with:
  - checks,
  - day move ($/%),
  - triggered timestamp.

### Phase 3 - Fundamentals + Technicals (Complete)
- Multi-source fundamentals aggregation and fallback handling.
- News, sentiment, earnings, and SEC 8-K integration.
- Technical indicator engine and plain-language explanations.
- Added holistic interpretation layer:
  - Action Bias + Risk Mode,
  - Holistic Read,
  - Regime Filter,
  - day-performance and relative-volume context integration.

### Phase 4 - Charting (Complete)
- 5m/15m intraday chart with days-back selection.
- RTH vs extended-hours styling.
- Bollinger + VWAP overlays.
- Enriched candle hover context.
- Added candlestick pattern interpretation panel below the chart:
  - pattern name,
  - bias/confidence,
  - interpretation,
  - recommendation from recent candles.
- Structural signal detection exposed to technicals panel.
- Trading-session clipping fix so `days=1` represents one market session.
- Top-of-chart text overlap fix (title removed, legend spacing adjusted).

### Phase 5 - Quote + Trading (Complete)
- Schwab streaming-first quote path (level-one stream) with REST fallback behavior.
- Background Schwab stream service for level-one quotes and level-two book depth.
- Stream subscription compatibility hardening for Schwab API variants (`nyse_book_*` / `listed_book_*`).
- Benign stream subscription response noise handling (`SUBS command succeeded`) to avoid unnecessary reconnects.
- Stream shutdown/reload cleanup hardening to reduce pending websocket-task warnings on restart.
- Order submission endpoints (market/limit + optional stop-loss/take-profit).
- Dry-run simulation flow.

### Phase 6 - Journal + Performance (Complete)
- Order attempts and trade state logging.
- Broker order-result/error logging linked to order attempts.
- Pending-order broker sync endpoint.
- Broker history import during sync:
  - fetches recent filled/executed Schwab orders,
  - includes valid partial fills from replace/cancel chains,
  - flattens nested child orders (e.g., bracket exits) from Schwab payloads,
  - logs broker order records to `orders.jsonl`,
  - pairs entry/exit fills (LIFO by symbol) into closed journal trades,
  - corrects previously imported broker trades when better matching evidence arrives.
- Trade close endpoint.
- Performance summary metrics and UI.
- Journal table ordering updated to explicit most-recent-first rendering.

### Phase 7 - Stability and UX Iteration (Complete, ongoing polish)
- SSL/degraded-source handling hardening.
- Cross-provider intraday fallback and consistency improvements.
- UI clarity improvements for scanner and technical interpretation.
- Added advanced flow/context technical cards with actionable guidance:
  - OFI, VPIN, aggressor imbalance, open/close auction pressure proxies,
  - cross-asset leadership (ES/NQ + sector ETF premarket),
  - options-derived proxies (GEX, skew, unusual flow).
- Holistic interpretation now incorporates advanced flow/cross-asset/options context.
- Added stream diagnostics + manual reconnect control in the UI.

## Current Build Notes
- Scanner and technical interpretation are tuned for educational readability first.
- External API quality/rate limits can still affect freshness/completeness.
- Schwab integration requires valid local token setup and account permissions.
- Schwab stream connectivity/entitlements can vary by account and exchange agreements.
- Journal/performance reflects app-submitted trades and broker-history imports after `/api/trades/sync`.
- Some Schwab fills may appear only as nested child orders; sync now flattens these so closed trade legs are imported.
