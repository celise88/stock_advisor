from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .analytics import (
    add_indicators,
    build_indicator_summary,
    detect_structure_signals,
    fibonacci_indicator_rows,
    fibonacci_levels,
)
from .charting import build_chart_payload
from .config import SETTINGS, STATIC_DIR
from .journal import TradeJournal
from .models import FundamentalsResponse, OrderRequest, OutcomeRequest, TechnicalsResponse
from .providers import EdgarProvider, FinnhubProvider, MarketDataProvider
from .scanner import ScannerService
from .schwab_client import SchwabClient


app = FastAPI(title=SETTINGS.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

schwab = SchwabClient()
market_provider = MarketDataProvider(schwab_client=schwab)
finnhub_provider = FinnhubProvider()
edgar_provider = EdgarProvider()
scanner_service = ScannerService(market_provider)
journal = TradeJournal()


def _clean_symbol(raw_symbol: str) -> str:
    cleaned = (raw_symbol or "").upper().strip()
    cleaned = cleaned.replace(",", "").replace(";", "")
    return cleaned


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup() -> None:
    scanner_service.start()
    scanner_service.scan_once()


@app.on_event("shutdown")
def shutdown() -> None:
    scanner_service.stop()


@app.get("/")
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found.")
    return FileResponse(index_path)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scannerIntervalSec": scanner_service.interval_sec,
        "scannerMarketCapMin": scanner_service.market_cap_min,
        "scannerAvgVolumeMin": scanner_service.avg_volume_min,
        "scannerRelativeVolumeMin": scanner_service.relative_volume_min,
        "schwabEnabled": schwab.enabled,
    }


@app.get("/api/scanner/results")
def scanner_results():
    return scanner_service.latest()


@app.post("/api/scanner/run")
def scanner_run_now():
    return scanner_service.scan_once()


@app.post("/api/scanner/interval/{interval_sec}")
def scanner_update_interval(interval_sec: int):
    scanner_service.update_interval(interval_sec)
    return {
        "status": "ok",
        "intervalSec": scanner_service.interval_sec,
        "marketCapMin": scanner_service.market_cap_min,
        "avgVolumeMin": scanner_service.avg_volume_min,
        "relativeVolumeMin": scanner_service.relative_volume_min,
    }


@app.post("/api/scanner/relative-volume/{relative_volume_min}")
def scanner_update_relative_volume(relative_volume_min: float):
    scanner_service.update_relative_volume_min(relative_volume_min)
    return {
        "status": "ok",
        "intervalSec": scanner_service.interval_sec,
        "marketCapMin": scanner_service.market_cap_min,
        "avgVolumeMin": scanner_service.avg_volume_min,
        "relativeVolumeMin": scanner_service.relative_volume_min,
    }


@app.post("/api/scanner/criteria")
def scanner_update_criteria(
    market_cap_min: Optional[float] = Query(None, ge=0),
    avg_volume_min: Optional[float] = Query(None, ge=0),
    relative_volume_min: Optional[float] = Query(None, ge=0),
):
    scanner_service.update_criteria(
        market_cap_min=market_cap_min,
        avg_volume_min=avg_volume_min,
        relative_volume_min=relative_volume_min,
    )
    return {
        "status": "ok",
        "intervalSec": scanner_service.interval_sec,
        "marketCapMin": scanner_service.market_cap_min,
        "avgVolumeMin": scanner_service.avg_volume_min,
        "relativeVolumeMin": scanner_service.relative_volume_min,
    }


@app.get("/api/fundamentals/{symbol}", response_model=FundamentalsResponse)
def fundamentals(symbol: str):
    symbol = _clean_symbol(symbol)
    base = market_provider.fundamentals_snapshot(symbol)
    next_earnings = finnhub_provider.next_earnings(symbol)
    if next_earnings:
        base["nextEarningsDate"] = next_earnings

    recent_news = finnhub_provider.company_news(symbol)
    sentiment = finnhub_provider.sentiment(symbol)
    filings_8k = edgar_provider.recent_8k_filings(base.get("cik"), limit=8)

    return FundamentalsResponse(
        symbol=symbol,
        updated_at=datetime.now(timezone.utc),
        fundamentals=base,
        news=recent_news,
        filings_8k=filings_8k,
        sentiment=sentiment,
    )


@app.get("/api/technicals/{symbol}", response_model=TechnicalsResponse)
def technicals(
    symbol: str,
    interval: str = Query("5m", pattern="^(5m|15m)$"),
    days: int = Query(3, ge=1, le=30),
):
    symbol = _clean_symbol(symbol)
    history = market_provider.intraday_history(symbol, interval=interval, days=days)
    if history.empty:
        return TechnicalsResponse(
            symbol=symbol,
            updated_at=datetime.now(timezone.utc),
            interval=interval,
            days=days,
            indicators=[
                {
                    "key": "Data Source Status",
                    "value": None,
                    "interpretation": "No intraday bars were returned for this symbol/timeframe.",
                    "explanation": "Try a different timeframe/symbol or verify upstream data-feed availability.",
                }
            ],
            signals=[],
        )

    data = add_indicators(history)
    fib = fibonacci_levels(data if not data.empty else history)
    signals = [s.__dict__ for s in detect_structure_signals(data)]
    indicators = build_indicator_summary(data)
    indicators.extend(fibonacci_indicator_rows(data, fib))
    return TechnicalsResponse(
        symbol=symbol,
        updated_at=datetime.now(timezone.utc),
        interval=interval,
        days=days,
        indicators=indicators,
        signals=signals,
    )


@app.get("/api/chart/{symbol}")
def chart(
    symbol: str,
    interval: str = Query("5m", pattern="^(5m|15m)$"),
    days: int = Query(3, ge=1, le=30),
):
    symbol = _clean_symbol(symbol)
    history = market_provider.intraday_history(symbol, interval=interval, days=days)
    payload = build_chart_payload(history, symbol)
    payload["symbol"] = symbol
    payload["interval"] = interval
    payload["days"] = days
    return payload


@app.get("/api/quote/{symbol}")
def quote(symbol: str):
    symbol = _clean_symbol(symbol)

    def _fallback_quote(error_message: str):
        try:
            fallback = market_provider.fundamentals_snapshot(symbol)
            last_price = fallback.get("price")
        except Exception as fallback_exc:
            last_price = None
            error_message = f"{error_message} | fallback failed: {fallback_exc}"
        return {
            "source": "market_fallback",
            "symbol": symbol,
            "bid": None,
            "ask": None,
            "last": last_price,
            "error": error_message,
        }

    try:
        if schwab.enabled:
            return {"source": "schwab", **schwab.get_quote(symbol)}
    except Exception as exc:
        return _fallback_quote(str(exc))

    return _fallback_quote("Schwab not configured.")


@app.post("/api/trades/order")
def place_order(req: OrderRequest):
    symbol = _clean_symbol(req.symbol)
    side = req.side.upper()
    if side not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be positive")

    payload = SchwabClient.build_equity_order_payload(
        symbol=symbol,
        side=side,
        quantity=req.quantity,
        order_type=req.order_type,
        limit_price=req.limit_price,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
    )
    logged = journal.log_order_attempt(
        {
            "symbol": symbol,
            "side": side,
            "quantity": req.quantity,
            "strategy": req.strategy,
            "orderType": req.order_type,
            "dryRun": req.dry_run,
            "payload": payload,
        }
    )

    if req.dry_run or not schwab.enabled:
        quote = market_provider.fundamentals_snapshot(symbol)
        fill_price = req.limit_price or quote.get("price") or 0.0
        opened = journal.log_open_trade(
            symbol=symbol,
            side=side,
            quantity=req.quantity,
            entry_price=float(fill_price),
            strategy=req.strategy,
            order_id=logged["id"],
        )
        return {
            "status": "simulated",
            "orderLogId": logged["id"],
            "reason": "dry_run=true or Schwab unavailable",
            "trade": opened,
        }

    try:
        result = schwab.place_order(payload)
        pending = journal.log_pending_trade(
            symbol=symbol,
            side=side,
            quantity=req.quantity,
            strategy=req.strategy,
            order_id=logged["id"],
            broker_order_id=result.get("orderId"),
            broker_location=result.get("location"),
            broker_status=result.get("status", "SUBMITTED").upper(),
        )
        return {"status": "submitted", "broker": result, "trade": pending}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Order submission failed: {exc}")


@app.post("/api/trades/sync")
def sync_trades():
    if not schwab.enabled:
        return {"status": "skipped", "reason": "Schwab not configured", "updates": [], "errors": []}

    updates = []
    errors = []
    for trade in journal.pending_trades():
        trade_id = trade.get("trade_id")
        broker_order_id = trade.get("broker_order_id")
        broker_location = trade.get("broker_location")
        if not broker_order_id and broker_location:
            broker_order_id = SchwabClient.extract_order_id(broker_location)
        if not trade_id or not broker_order_id:
            continue

        try:
            order_payload = schwab.get_order(str(broker_order_id))
            summary = SchwabClient.summarize_order(order_payload)
            broker_status = summary.get("status", "UNKNOWN")
            avg_fill_price = summary.get("averageFillPrice")

            if broker_status in {"FILLED", "EXECUTED"} and avg_fill_price is not None:
                updated = journal.mark_trade_filled(
                    trade_id=trade_id,
                    entry_price=float(avg_fill_price),
                    broker_status=broker_status,
                )
            elif broker_status in {"CANCELED", "REJECTED", "EXPIRED"}:
                updated = journal.mark_trade_cancelled(
                    trade_id=trade_id,
                    broker_status=broker_status,
                )
            else:
                updated = journal.update_broker_status(
                    trade_id=trade_id,
                    broker_status=broker_status,
                    fill_price=float(avg_fill_price) if avg_fill_price is not None else None,
                )

            updates.append(
                {
                    "trade_id": updated.get("trade_id"),
                    "status": updated.get("status"),
                    "broker_status": updated.get("broker_status"),
                    "entry_price": updated.get("entry_price"),
                }
            )
        except Exception as exc:
            errors.append({"trade_id": trade_id, "error": str(exc)})

    return {
        "status": "ok",
        "checked": len(journal.pending_trades()),
        "updates": updates,
        "errors": errors,
    }


@app.post("/api/trades/close")
def close_trade(req: OutcomeRequest):
    try:
        closed = journal.close_trade(req.trade_id, req.exit_price, req.notes or "")
        return {"status": "ok", "trade": closed}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/trades/journal")
def trades_journal():
    trades = journal.trades()
    return {
        "trades": trades,
        "orders": journal.orders(),
        "pendingCount": len([t for t in trades if t.get("status") == "pending"]),
    }


@app.get("/api/trades/performance")
def trades_performance():
    return journal.performance()
