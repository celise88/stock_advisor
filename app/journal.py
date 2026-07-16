from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .config import DATA_DIR

TRADES_FILE = DATA_DIR / "trades.jsonl"
ORDERS_FILE = DATA_DIR / "orders.jsonl"


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


class TradeJournal:
    def log_order_attempt(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        record = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        _append_jsonl(ORDERS_FILE, record)
        return record

    def log_order_result(
        self,
        order_log_id: str,
        broker_order_id: str | None = None,
        broker_status: str | None = None,
        broker_error: str | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "broker_result",
            "order_log_id": order_log_id,
            "broker_order_id": broker_order_id,
            "broker_status": broker_status,
            "broker_error": broker_error,
        }
        if payload:
            record.update(payload)
        _append_jsonl(ORDERS_FILE, record)
        return record

    def _append_trade_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _append_jsonl(TRADES_FILE, payload)
        return payload

    def _latest_trade_state(self, trade_id: str) -> Dict[str, Any] | None:
        state = None
        for trade in self.trades():
            if trade.get("trade_id") == trade_id:
                state = trade
        return state

    def log_open_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float | None,
        strategy: str,
        order_id: str | None = None,
        status: str = "open",
        broker_order_id: str | None = None,
        broker_location: str | None = None,
        broker_status: str | None = None,
        opened_at: str | None = None,
    ) -> Dict[str, Any]:
        opened = opened_at or datetime.now(timezone.utc).isoformat()
        rec = {
            "trade_id": str(uuid.uuid4()),
            "opened_at": opened,
            "symbol": symbol.upper(),
            "side": side.upper(),
            "quantity": int(quantity),
            "entry_price": float(entry_price) if entry_price is not None else None,
            "strategy": strategy,
            "status": status,
            "order_id": order_id,
            "broker_order_id": broker_order_id,
            "broker_location": broker_location,
            "broker_status": broker_status,
        }
        return self._append_trade_event(rec)

    def log_pending_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        strategy: str,
        order_id: str | None,
        broker_order_id: str | None,
        broker_location: str | None,
        broker_status: str = "SUBMITTED",
    ) -> Dict[str, Any]:
        return self.log_open_trade(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=None,
            strategy=strategy,
            order_id=order_id,
            status="pending",
            broker_order_id=broker_order_id,
            broker_location=broker_location,
            broker_status=broker_status,
        )

    def mark_trade_filled(
        self,
        trade_id: str,
        entry_price: float,
        broker_status: str = "FILLED",
    ) -> Dict[str, Any]:
        current = self._latest_trade_state(trade_id)
        if not current:
            raise ValueError(f"Trade {trade_id} not found")

        updated = {
            **current,
            "status": "open",
            "entry_price": float(entry_price),
            "broker_status": broker_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._append_trade_event(updated)

    def mark_trade_cancelled(self, trade_id: str, broker_status: str = "CANCELED") -> Dict[str, Any]:
        current = self._latest_trade_state(trade_id)
        if not current:
            raise ValueError(f"Trade {trade_id} not found")

        updated = {
            **current,
            "status": "canceled",
            "broker_status": broker_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._append_trade_event(updated)

    def update_broker_status(
        self,
        trade_id: str,
        broker_status: str,
        fill_price: float | None = None,
    ) -> Dict[str, Any]:
        current = self._latest_trade_state(trade_id)
        if not current:
            raise ValueError(f"Trade {trade_id} not found")

        updated = {
            **current,
            "broker_status": broker_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if fill_price is not None:
            updated["entry_price"] = float(fill_price)
            updated["status"] = "open"
        return self._append_trade_event(updated)

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        notes: str = "",
        closed_at: str | None = None,
    ) -> Dict[str, Any]:
        target = self._latest_trade_state(trade_id)
        if not target:
            raise ValueError(f"Trade {trade_id} not found")
        if target.get("status") == "closed":
            return target
        if target.get("status") not in {"open"}:
            raise ValueError("Only open filled trades can be closed.")
        if target.get("entry_price") is None:
            raise ValueError("Cannot close trade before fill price is available.")

        qty = float(target.get("quantity", 0))
        entry = float(target.get("entry_price"))
        side = target.get("side", "BUY")
        mult = 1 if side == "BUY" else -1
        pnl = (float(exit_price) - entry) * qty * mult

        closed = {
            **target,
            "status": "closed",
            "closed_at": closed_at or datetime.now(timezone.utc).isoformat(),
            "exit_price": float(exit_price),
            "pnl": pnl,
            "notes": notes,
        }
        return self._append_trade_event(closed)

    def backfill_trade_times(
        self,
        trade_id: str,
        opened_at: str | None = None,
        closed_at: str | None = None,
    ) -> Dict[str, Any]:
        current = self._latest_trade_state(trade_id)
        if not current:
            raise ValueError(f"Trade {trade_id} not found")
        updated = {
            **current,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if opened_at:
            updated["opened_at"] = opened_at
        if closed_at and str(updated.get("status") or "").lower() == "closed":
            updated["closed_at"] = closed_at
        return self._append_trade_event(updated)

    def overwrite_trade(self, trade_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        current = self._latest_trade_state(trade_id)
        if not current:
            raise ValueError(f"Trade {trade_id} not found")
        updated = {
            **current,
            **updates,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._append_trade_event(updated)

    def trades(self) -> List[Dict[str, Any]]:
        events = _read_jsonl(TRADES_FILE)
        latest: Dict[str, Dict[str, Any]] = {}
        for event in events:
            trade_id = event.get("trade_id")
            if not trade_id:
                continue
            latest[trade_id] = event

        rows = list(latest.values())
        rows.sort(
            key=lambda r: (
                r.get("updated_at")
                or r.get("closed_at")
                or r.get("opened_at")
                or ""
            ),
            reverse=True,
        )
        return rows

    def pending_trades(self) -> List[Dict[str, Any]]:
        return [t for t in self.trades() if t.get("status") == "pending"]

    def broker_sync_candidates(self) -> List[Dict[str, Any]]:
        candidates = []
        for trade in self.trades():
            status = str(trade.get("status") or "").lower()
            broker_order_id = trade.get("broker_order_id")
            if status == "pending":
                candidates.append(trade)
                continue
            if broker_order_id and status in {"open", "closed"}:
                candidates.append(trade)
        return candidates

    def orders(self) -> List[Dict[str, Any]]:
        return _read_jsonl(ORDERS_FILE)

    def performance(self) -> Dict[str, Any]:
        rows = [r for r in self.trades() if r.get("status") == "closed"]
        if not rows:
            return {
                "totalTrades": 0,
                "wins": 0,
                "losses": 0,
                "winRate": 0.0,
                "netPnl": 0.0,
                "byStrategy": {},
            }

        wins = sum(1 for r in rows if float(r.get("pnl", 0)) > 0)
        losses = sum(1 for r in rows if float(r.get("pnl", 0)) <= 0)
        net = sum(float(r.get("pnl", 0)) for r in rows)

        by_strategy: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = row.get("strategy") or "unspecified"
            bucket = by_strategy.setdefault(
                key, {"trades": 0, "wins": 0, "losses": 0, "netPnl": 0.0}
            )
            bucket["trades"] += 1
            if float(row.get("pnl", 0)) > 0:
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1
            bucket["netPnl"] += float(row.get("pnl", 0))

        for bucket in by_strategy.values():
            bucket["winRate"] = (
                float(bucket["wins"]) / float(bucket["trades"]) if bucket["trades"] else 0.0
            )

        return {
            "totalTrades": len(rows),
            "wins": wins,
            "losses": losses,
            "winRate": wins / len(rows),
            "netPnl": net,
            "byStrategy": by_strategy,
        }