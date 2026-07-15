# Task Tracker

Legend:
- `[ ]` pending
- `[-]` in progress
- `[x]` completed

## Documentation
- [x] Keep `SPEC.md` aligned with implemented behavior.
- [x] Keep `build.md` aligned with delivered phases.
- [x] Keep `README.md` aligned with current setup and features.
- [x] Keep `tasks.md` synchronized with recent changes.

## Core Platform
- [x] FastAPI app bootstrap + static frontend serving.
- [x] Centralized config/env handling.
- [x] Health endpoint and startup scanner initialization.

## Scanner
- [x] Recurring scanner loop with configurable interval.
- [x] Runtime-configurable scanner criteria:
  - [x] market cap minimum,
  - [x] average volume minimum,
  - [x] relative volume minimum.
- [x] Scanner APIs for results, manual run, interval update, and criteria update.
- [x] Scanner UI controls for interval and criteria.
- [x] Scanner pass-only table rendering.
- [x] Scanner row enrichment:
  - [x] day move ($/%),
  - [x] triggered timestamp for pass transitions.

## Fundamentals + Data Providers
- [x] Multi-source fundamentals aggregation.
- [x] Source fallback behavior (Schwab/Finnhub/AlphaVantage/yfinance optional).
- [x] Ownership and short-interest fallback improvements.
- [x] Data warning/error surfacing in UI.
- [x] News, sentiment, earnings, and SEC 8-K integration.

## Technicals + Interpretation
- [x] Technical indicator engine implementation.
- [x] Per-indicator plain-language interpretation.
- [x] Dynamic Fibonacci interpretation based on trend and level position.
- [x] Holistic interpretation layer:
  - [x] Action Bias summary line,
  - [x] Risk Mode tag,
  - [x] Holistic Read alignment summary,
  - [x] Regime Filter guidance.
- [x] Context integration:
  - [x] session/day performance impact,
  - [x] relative-volume-based conviction adjustment,
  - [x] conflict detection text.

## Charting
- [x] 5m/15m intraday chart with days-back control.
- [x] Muted extended-hours bars + RTH distinction.
- [x] Bollinger + VWAP overlays.
- [x] Candle hover interpretation context.
- [x] Structural signal computation for technical signal list.
- [x] Remove chart text overlays for structure signals.
- [x] Fix chart top text overlap.
- [x] Enforce trading-session clipping for days-back (`days=1` => one market day).

## Quotes + Trading + Journal
- [x] Schwab-first quote endpoint with fallback.
- [x] Order placement endpoint and dry-run path.
- [x] Pending-order sync/reconciliation endpoint.
- [x] Broker outcome logging in order ledger (`broker_order_id`, `broker_status`, `broker_error`).
- [x] Broker history import on sync:
  - [x] ingest recent Schwab filled/executed orders,
  - [x] backfill order records,
  - [x] derive closed trades from matched entry/exit fills.
- [x] Trade close endpoint.
- [x] Journal and performance APIs + UI.

## Cleanup
- [x] Removed legacy experimental `.ipynb` notebooks from repository.
- [ ] Optional: remove local ad-hoc `tests.ipynb` when no longer needed.

## Open Follow-ups
- [ ] Optional: persist runtime scanner criteria updates back to `.env`.
- [ ] Optional: surface latest `broker_error` entries directly in Journal UI.
