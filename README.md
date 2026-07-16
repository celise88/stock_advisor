# stock_advisor

Stock Advisor is a FastAPI + Plotly application for beginner day traders, combining scanner discovery, fundamentals, technical interpretation, charting, quote/trading, and trade journaling.

## Current Features

### Scanner
- Continuous background scan (default 45s, configurable).
- Configurable criteria from UI/API:
  - minimum market cap,
  - minimum average volume,
  - minimum relative volume.
- UI displays only currently passing symbols.
- Scanner rows include:
  - price,
  - checks,
  - day move ($/%),
  - trigger time (`Triggered At`).

### Symbol Workspace - Fundamentals
- Current price and key fundamentals (ownership/short metrics, margins, volume stats, 52-week range, etc.).
- Next earnings date, recent 8-K filings, news, and sentiment.
- Data-source warning/degradation messaging when needed.

### Symbol Workspace - Technicals
- Indicator cards with beginner-focused interpretation.
- Advanced flow/context cards with clear trade guidance:
  - Order Flow Imbalance (OFI, Cont-Kukanov-Stoikov style proxy),
  - VPIN (volume-synchronized informed-flow proxy),
  - tick-rule aggressor imbalance,
  - opening/closing auction pressure proxies,
  - cross-asset premarket leadership (ES/NQ + sector ETF),
  - options-derived proxies (GEX, put/call skew, unusual sweep-like flow).
- Holistic interpretation rows:
  - `Action Bias` (with `Risk Mode`),
  - `Holistic Read`,
  - `Regime Filter (ADX)`.
- Holistic output integrates:
  - trend/momentum alignment,
  - advanced flow/options/cross-asset context,
  - conflict detection,
  - session/day performance context,
  - relative-volume conviction adjustments.
- Dynamic Fibonacci interpretations based on current context.

### Intraday Chart
- 5m / 15m candles with days-back control.
- Pre/post-market bars muted.
- Bollinger + VWAP overlays.
- Candle hover interpretation details.
- Structural signals shown in technical signal list.
- Days-back is clipped to trading sessions (`days=1` => one session).

### Quote, Trading, Journal
- Schwab streaming level-one quote integration (when available), with REST fallback.
- Background Schwab stream service subscribes to level-one quotes + level-two book depth.
- Stream diagnostics panel includes connection state, message age, tracked symbols, and last error.
- Manual `Reconnect Stream` action is available from the diagnostics panel.
- Order submission endpoint (market/limit + optional stop-loss/take-profit).
- Dry-run support.
- Broker sync endpoint for pending order reconciliation.
- Broker history import during sync:
  - fetches recent Schwab filled/executed orders,
  - backfills broker order records,
  - creates closed journal trades from matched entry/exit fills.
- Trade journal + performance summaries.
- `orders.jsonl` logs both:
  - order attempts,
  - linked broker outcomes (`broker_order_id`, `broker_status`, `broker_error`).

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run
```bash
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Schwab Token Setup (if needed)
```bash
python init_schwab_token.py
```

## Notes
- Free-tier APIs can rate-limit and return partial fields.
- Scanner universe and defaults live in `app/config.py`.
- Trade/order logs are written as JSONL under `data/`.
- To ingest external Schwab executions into Journal/Performance, run trade sync (`POST /api/trades/sync` or use the UI Sync button).
- For Yahoo certificate issues on macOS, set `DISABLE_YFINANCE=true` in `.env`.
