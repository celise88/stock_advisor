from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dateutil import parser as date_parser

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
from .streaming_service import SchwabStreamService


app = FastAPI(title=SETTINGS.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

schwab = SchwabClient()
stream_service = SchwabStreamService(schwab_client=schwab, seed_symbols=SETTINGS.scanner_symbols)
market_provider = MarketDataProvider(schwab_client=schwab, stream_service=stream_service)
finnhub_provider = FinnhubProvider()
edgar_provider = EdgarProvider()
scanner_service = ScannerService(market_provider)
journal = TradeJournal()


def _clean_symbol(raw_symbol: str) -> str:
    cleaned = (raw_symbol or "").upper().strip()
    cleaned = cleaned.replace(",", "").replace(";", "")
    return cleaned


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_timestamp(order_payload: Dict[str, Any]) -> datetime:
    def _parse(raw: Any) -> Optional[datetime]:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value <= 0:
                return None
            if value > 10_000_000_000:
                value = value / 1000.0
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text:
            return None
        if re.search(r"[+-]\d{4}$", text):
            text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
        text = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = date_parser.parse(text)
            except Exception:
                return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    for key in ("closeTime", "enteredTime", "releaseTime", "transactionTime"):
        parsed = _parse(order_payload.get(key))
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _order_id(order_payload: Dict[str, Any]) -> Optional[str]:
    raw = order_payload.get("orderId")
    if raw is None:
        raw = order_payload.get("id")
    if raw is None:
        return None
    return str(raw)


def _parse_filled_events(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        summary = SchwabClient.summarize_order(order)
        status = str(summary.get("status") or "").upper()
        symbol = summary.get("symbol")
        side = summary.get("side")
        fill_price = _safe_float(summary.get("averageFillPrice"))
        filled_qty = _safe_float(summary.get("filledQuantity"))
        qty = filled_qty if filled_qty is not None and filled_qty > 0 else _safe_float(summary.get("quantity"))
        order_id = _order_id(order)
        has_confirmed_fill = status in {"FILLED", "EXECUTED"}
        has_partial_fill = status not in {"FILLED", "EXECUTED"} and filled_qty is not None and filled_qty > 0
        if not (has_confirmed_fill or has_partial_fill):
            continue
        if not symbol or side not in {"BUY", "SELL"} or fill_price is None or qty is None or qty <= 0 or not order_id:
            continue
        events.append(
            {
                "order_id": order_id,
                "symbol": str(symbol).upper(),
                "side": side,
                "qty": float(qty),
                "price": float(fill_price),
                "status": status,
                "timestamp": _order_timestamp(order),
                "raw": order,
            }
        )
    events.sort(key=lambda row: (row["timestamp"], row["order_id"]))
    return events


def _import_broker_history(orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    events = _parse_filled_events(orders)
    if not events:
        return {
            "fetchedFilledOrders": 0,
            "loggedOrders": 0,
            "importedClosedTrades": 0,
            "skippedExistingTrades": 0,
            "backfilledTradeTimestamps": 0,
            "openUnmatchedLots": 0,
        }

    existing_trades = journal.trades()
    existing_trade_keys = set()
    existing_trade_by_key: Dict[str, Dict[str, Any]] = {}
    existing_trade_key_by_id: Dict[str, str] = {}
    existing_trade_by_exit_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    existing_trade_broker_ids = set()
    for trade in existing_trades:
        notes = str(trade.get("notes") or "")
        marker = "broker_import_key="
        if marker in notes:
            import_key = notes.split(marker, 1)[1].split(";", 1)[0].strip()
            existing_trade_keys.add(import_key)
            existing_trade_by_key[import_key] = trade
            trade_id = str(trade.get("trade_id") or "")
            if trade_id:
                existing_trade_key_by_id[trade_id] = import_key
        exit_marker = "exit_order_id="
        if exit_marker in notes:
            exit_order_id = notes.split(exit_marker, 1)[1].split(";", 1)[0].strip()
            existing_trade_broker_ids.add(exit_order_id)
            if exit_order_id:
                existing_trade_by_exit_id[exit_order_id].append(trade)
        broker_id = trade.get("broker_order_id")
        if broker_id:
            existing_trade_broker_ids.add(str(broker_id))

    existing_order_ids = set()
    for record in journal.orders():
        if not isinstance(record, dict):
            continue
        broker_id = record.get("broker_order_id")
        if broker_id:
            existing_order_ids.add(str(broker_id))

    logged_orders = 0
    for event in events:
        if event["order_id"] in existing_order_ids:
            continue
        journal.log_order_attempt(
            {
                "source": "broker_import",
                "broker_order_id": event["order_id"],
                "symbol": event["symbol"],
                "side": event["side"],
                "quantity": int(round(event["qty"])),
                "fill_price": event["price"],
                "broker_status": event["status"],
                "filled_at": event["timestamp"].isoformat(),
            }
        )
        existing_order_ids.add(event["order_id"])
        logged_orders += 1

    # Ignore fills already tied to existing tracked trades to avoid duplicates.
    importable_events = list(events)

    long_lots: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    short_lots: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    matches: List[Dict[str, Any]] = []

    def _consume_match(
        entry_side: str,
        entry_lots: Dict[str, List[Dict[str, Any]]],
        exit_event: Dict[str, Any],
        symbol: str,
        qty_to_match: float,
    ) -> float:
        remaining = qty_to_match
        lots = entry_lots[symbol]
        while remaining > 0 and lots:
            # Match newest lots first (LIFO) to better align with intraday
            # round-trip behavior when users carry older swing inventory.
            lot = lots[-1]
            matched_qty = min(remaining, lot["qty_remaining"])
            lot["qty_remaining"] -= matched_qty
            remaining -= matched_qty
            lot["match_seq"] += 1

            matches.append(
                {
                    "entry_order_id": lot["order_id"],
                    "exit_order_id": exit_event["order_id"],
                    "symbol": symbol,
                    "entry_side": entry_side,
                    "entry_price": lot["price"],
                    "exit_price": exit_event["price"],
                    "qty": matched_qty,
                    "entry_ts": lot["timestamp"],
                    "exit_ts": exit_event["timestamp"],
                    "segment": lot["match_seq"],
                }
            )

            if lot["qty_remaining"] <= 1e-9:
                lots.pop()
        return remaining

    for event in importable_events:
        symbol = event["symbol"]
        if event["side"] == "BUY":
            remaining = _consume_match("SELL", short_lots, event, symbol, event["qty"])
            if remaining > 1e-9:
                long_lots[symbol].append(
                    {
                        "order_id": event["order_id"],
                        "timestamp": event["timestamp"],
                        "price": event["price"],
                        "qty_remaining": remaining,
                        "match_seq": 0,
                    }
                )
        else:  # SELL
            remaining = _consume_match("BUY", long_lots, event, symbol, event["qty"])
            if remaining > 1e-9:
                short_lots[symbol].append(
                    {
                        "order_id": event["order_id"],
                        "timestamp": event["timestamp"],
                        "price": event["price"],
                        "qty_remaining": remaining,
                        "match_seq": 0,
                    }
                )

    imported_closed = 0
    skipped_existing = 0
    backfilled_timestamps = 0
    correctedClosedTrades = 0

    def _to_iso(value: Any) -> Optional[str]:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc).isoformat()
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, str) and value:
            return value
        return None

    for match in matches:
        qty_int = max(1, int(round(match["qty"])))
        import_key = (
            f"{match['entry_order_id']}:{match['exit_order_id']}:"
            f"{qty_int}:{match['entry_side']}:{match['segment']}"
        )
        entry_iso = _to_iso(match.get("entry_ts"))
        exit_iso = _to_iso(match.get("exit_ts"))
        if import_key in existing_trade_keys:
            existing = existing_trade_by_key.get(import_key)
            if existing and existing.get("trade_id"):
                if (
                    (entry_iso and str(existing.get("opened_at") or "") != entry_iso)
                    or (exit_iso and str(existing.get("closed_at") or "") != exit_iso)
                ):
                    journal.backfill_trade_times(
                        trade_id=str(existing.get("trade_id")),
                        opened_at=entry_iso,
                        closed_at=exit_iso,
                    )
                    backfilled_timestamps += 1
            skipped_existing += 1
            continue

        # If an existing imported closed trade used the same exit order/qty but
        # was previously paired to a different entry lot, overwrite it.
        corrected = False
        for existing in existing_trade_by_exit_id.get(str(match["exit_order_id"]), []):
            if str(existing.get("strategy") or "") != "broker_import":
                continue
            if str(existing.get("status") or "").lower() != "closed":
                continue
            if int(round(float(existing.get("quantity") or 0))) != qty_int:
                continue
            trade_id = str(existing.get("trade_id") or "")
            if not trade_id:
                continue
            side = str(match["entry_side"]).upper()
            pnl_mult = 1.0 if side == "BUY" else -1.0
            pnl = (float(match["exit_price"]) - float(match["entry_price"])) * float(qty_int) * pnl_mult
            journal.overwrite_trade(
                trade_id=trade_id,
                updates={
                    "opened_at": entry_iso or existing.get("opened_at"),
                    "closed_at": exit_iso or existing.get("closed_at"),
                    "symbol": match["symbol"],
                    "side": side,
                    "quantity": qty_int,
                    "entry_price": float(match["entry_price"]),
                    "exit_price": float(match["exit_price"]),
                    "pnl": pnl,
                    "strategy": "broker_import",
                    "status": "closed",
                    "order_id": f"broker_import:{import_key}",
                    "broker_order_id": match["entry_order_id"],
                    "broker_status": "FILLED",
                    "notes": (
                        f"Imported from Schwab fills; "
                        f"broker_import_key={import_key}; "
                        f"exit_order_id={match['exit_order_id']}"
                    ),
                },
            )
            old_key = existing_trade_key_by_id.get(trade_id)
            if old_key:
                existing_trade_keys.discard(old_key)
                existing_trade_by_key.pop(old_key, None)
            existing_trade_keys.add(import_key)
            existing_trade_key_by_id[trade_id] = import_key
            existing_trade_by_key[import_key] = {
                **existing,
                "order_id": f"broker_import:{import_key}",
                "quantity": qty_int,
                "entry_price": float(match["entry_price"]),
                "exit_price": float(match["exit_price"]),
                "opened_at": entry_iso or existing.get("opened_at"),
                "closed_at": exit_iso or existing.get("closed_at"),
            }
            correctedClosedTrades += 1
            corrected = True
            break
        if corrected:
            continue

        opened = journal.log_open_trade(
            symbol=match["symbol"],
            side=match["entry_side"],
            quantity=qty_int,
            entry_price=float(match["entry_price"]),
            strategy="broker_import",
            order_id=f"broker_import:{import_key}",
            status="open",
            broker_order_id=match["entry_order_id"],
            broker_status="FILLED",
            opened_at=entry_iso,
        )
        journal.close_trade(
            trade_id=opened["trade_id"],
            exit_price=float(match["exit_price"]),
            notes=(
                f"Imported from Schwab fills; "
                f"broker_import_key={import_key}; "
                f"exit_order_id={match['exit_order_id']}"
            ),
            closed_at=exit_iso,
        )
        existing_trade_keys.add(import_key)
        existing_trade_by_key[import_key] = {
            **opened,
            "status": "closed",
            "closed_at": exit_iso,
            "opened_at": entry_iso or opened.get("opened_at"),
        }
        imported_closed += 1

    unmatched = 0
    for lots in long_lots.values():
        unmatched += len([l for l in lots if l.get("qty_remaining", 0) > 1e-9])
    for lots in short_lots.values():
        unmatched += len([l for l in lots if l.get("qty_remaining", 0) > 1e-9])

    return {
        "fetchedFilledOrders": len(events),
        "loggedOrders": logged_orders,
        "importedClosedTrades": imported_closed,
        "correctedClosedTrades": correctedClosedTrades,
        "skippedExistingTrades": skipped_existing,
        "backfilledTradeTimestamps": backfilled_timestamps,
        "openUnmatchedLots": unmatched,
    }


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup() -> None:
    stream_service.start()
    scanner_service.start()
    scanner_service.scan_once()


@app.on_event("shutdown")
def shutdown() -> None:
    scanner_service.stop()
    stream_service.stop()


@app.get("/")
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found.")
    return FileResponse(
        index_path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/health")
def health() -> Dict[str, Any]:
    stream_status = stream_service.status()
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scannerIntervalSec": scanner_service.interval_sec,
        "scannerMarketCapMin": scanner_service.market_cap_min,
        "scannerAvgVolumeMin": scanner_service.avg_volume_min,
        "scannerRelativeVolumeMin": scanner_service.relative_volume_min,
        "schwabEnabled": schwab.enabled,
        "schwabStreamEnabled": stream_status.get("enabled"),
        "schwabStreamConnected": stream_status.get("connected"),
        "schwabStreamLastError": stream_status.get("lastError"),
        "schwabStreamLastMessageAgeSec": stream_status.get("lastMessageAgeSec"),
        "schwabStreamTrackedSymbols": stream_status.get("trackedSymbols"),
    }


@app.post("/api/stream/restart")
def stream_restart() -> Dict[str, Any]:
    if not schwab.enabled:
        return {
            "status": "skipped",
            "reason": "Schwab not configured",
            "stream": stream_service.status(),
        }
    status = stream_service.restart()
    return {"status": "ok", "stream": status}


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
    market_context = market_provider.advanced_market_context(
        symbol=symbol,
        interval=interval,
        days=days,
        history=history,
    )
    indicators = build_indicator_summary(data, market_context=market_context)
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

    stream_quote = market_provider.streaming_quote(symbol)
    if stream_quote:
        return {"source": "schwab_stream", **stream_quote}

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
            "broker_order_id": None,
            "broker_status": None,
            "broker_error": None,
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
        journal.log_order_result(
            order_log_id=logged["id"],
            broker_order_id=None,
            broker_status="SIMULATED",
            broker_error=None,
            payload={"reason": "dry_run=true or Schwab unavailable"},
        )
        return {
            "status": "simulated",
            "orderLogId": logged["id"],
            "reason": "dry_run=true or Schwab unavailable",
            "trade": opened,
        }

    try:
        result = schwab.place_order(payload)
        journal.log_order_result(
            order_log_id=logged["id"],
            broker_order_id=str(result.get("orderId")) if result.get("orderId") is not None else None,
            broker_status=str(result.get("status", "SUBMITTED")).upper(),
            broker_error=None,
            payload={"http_status": result.get("http_status"), "location": result.get("location")},
        )
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
        journal.log_order_result(
            order_log_id=logged["id"],
            broker_order_id=None,
            broker_status="ERROR",
            broker_error=str(exc),
        )
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

    history_import = {
        "fetchedFilledOrders": 0,
        "loggedOrders": 0,
        "importedClosedTrades": 0,
        "skippedExistingTrades": 0,
        "backfilledTradeTimestamps": 0,
        "openUnmatchedLots": 0,
    }
    try:
        recent_orders = schwab.get_recent_orders()
        history_import = _import_broker_history(recent_orders)
    except Exception as exc:
        errors.append({"trade_id": "broker_history_import", "error": str(exc)})

    return {
        "status": "ok",
        "checked": len(journal.pending_trades()),
        "updates": updates,
        "errors": errors,
        "historyImport": history_import,
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
