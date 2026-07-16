from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .config import SETTINGS
from .schwab_client import SchwabClient


class SchwabStreamService:
    """Background Schwab streaming client for quotes + book depth.

    This service is intentionally best-effort:
    - if stream login fails, the app still works on REST fallbacks,
    - if a stream message is malformed/unexpected, we skip it,
    - consumers can check availability per-symbol.
    """

    def __init__(
        self,
        schwab_client: SchwabClient,
        seed_symbols: Optional[List[str]] = None,
        lookback_sec: int = 15 * 60,
    ) -> None:
        self.schwab_client = schwab_client
        self.seed_symbols = [self._clean_symbol(s) for s in (seed_symbols or []) if self._clean_symbol(s)]
        self.lookback_sec = max(300, int(lookback_sec))

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stream: Optional[Any] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._connected = False
        self._enabled = bool(self.schwab_client.enabled)
        self._last_error: Optional[str] = None
        self._last_message_ts: Optional[float] = None

        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._books: Dict[str, Dict[str, Any]] = {}
        self._flow_state: Dict[str, Dict[str, Any]] = {}
        self._subscribed_symbols: set[str] = set()
        self._pending_symbols: set[str] = set(self.seed_symbols)

    @staticmethod
    def _clean_symbol(symbol: str) -> str:
        return (symbol or "").upper().strip().replace(",", "").replace(";", "")

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_ts_seconds(row: Dict[str, Any], default: Optional[float] = None) -> float:
        for key in ("QUOTE_TIME_MILLIS", "TRADE_TIME_MILLIS", "BOOK_TIME", "timestamp"):
            raw = row.get(key)
            if isinstance(raw, (int, float)) and raw > 0:
                return float(raw) / 1000.0 if raw > 10_000_000_000 else float(raw)
        return default if default is not None else time.time()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def status(self) -> Dict[str, Any]:
        with self._lock:
            age = None
            if self._last_message_ts is not None:
                age = max(0.0, time.time() - self._last_message_ts)
            return {
                "enabled": self._enabled,
                "connected": self._connected,
                "lastError": self._last_error,
                "lastMessageAgeSec": age,
                "trackedSymbols": len(set(self._subscribed_symbols) | set(self._pending_symbols)),
            }

    def start(self) -> None:
        if not self._enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="schwab-stream-service")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        loop = self._loop
        with self._lock:
            stream = self._stream
        if loop and loop.is_running():
            if stream is not None:
                try:
                    close_future = asyncio.run_coroutine_threadsafe(self._close_stream_socket(stream), loop)
                    close_future.result(timeout=2.5)
                except Exception:
                    pass
            loop.call_soon_threadsafe(lambda: None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        with self._lock:
            self._connected = False
            self._loop = None
            self._stream = None
        if self._thread and not self._thread.is_alive():
            self._thread = None

    def restart(self) -> Dict[str, Any]:
        """Restart the stream worker and resubscribe existing symbols."""
        self.stop()
        with self._lock:
            self._connected = False
            self._last_error = None
            self._last_message_ts = None
            self._pending_symbols.update(self._subscribed_symbols)
            self._subscribed_symbols.clear()
        self.start()
        return self.status()

    def ensure_symbol(self, symbol: str) -> None:
        sym = self._clean_symbol(symbol)
        if not sym:
            return
        with self._lock:
            if sym in self._subscribed_symbols:
                return
            self._pending_symbols.add(sym)

    def quote_snapshot(self, symbol: str, max_age_sec: float = 25.0) -> Optional[Dict[str, Any]]:
        sym = self._clean_symbol(symbol)
        if not sym:
            return None
        self.ensure_symbol(sym)
        with self._lock:
            quote = self._quotes.get(sym)
            if not quote:
                return None
            ts = quote.get("timestamp")
            age_sec = max(0.0, time.time() - float(ts)) if isinstance(ts, (int, float)) else None
            if age_sec is not None and age_sec > max_age_sec:
                return None
            out = dict(quote)
            out["ageSec"] = age_sec
            return out

    def flow_snapshot(self, symbol: str) -> Dict[str, Any]:
        sym = self._clean_symbol(symbol)
        if not sym:
            return {
                "available": False,
                "ofi": None,
                "aggressorImbalance": None,
                "vpin": None,
                "note": "Invalid symbol.",
            }
        self.ensure_symbol(sym)
        with self._lock:
            state = self._flow_state.get(sym)
            quote = self._quotes.get(sym, {})
            book = self._books.get(sym, {})
            now = time.time()
            if not state:
                return {
                    "available": False,
                    "ofi": None,
                    "aggressorImbalance": None,
                    "vpin": None,
                    "quoteAgeSec": max(0.0, now - float(quote.get("timestamp"))) if quote.get("timestamp") else None,
                    "bookAgeSec": max(0.0, now - float(book.get("timestamp"))) if book.get("timestamp") else None,
                    "note": "No stream flow state yet.",
                }

            self._prune_old_locked(state, now)
            ofi_events: Deque[Dict[str, float]] = state["ofi_events"]
            signed_vol_events: Deque[Dict[str, float]] = state["signed_vol_events"]

            ofi = None
            if ofi_events:
                denom = sum(max(1.0, e.get("denom", 1.0)) for e in ofi_events)
                if denom > 0:
                    ofi = sum(e.get("value", 0.0) for e in ofi_events) / denom

            buy_vol = sum(max(0.0, e.get("signed", 0.0)) for e in signed_vol_events)
            sell_vol = sum(max(0.0, -e.get("signed", 0.0)) for e in signed_vol_events)
            total_traded = sum(max(0.0, e.get("total", 0.0)) for e in signed_vol_events)
            aggressor = (buy_vol - sell_vol) / total_traded if total_traded > 0 else None
            vpin = (
                sum(abs(e.get("signed", 0.0)) for e in signed_vol_events) / total_traded
                if total_traded > 0
                else None
            )

            quote_age = max(0.0, now - float(quote.get("timestamp"))) if quote.get("timestamp") else None
            book_age = max(0.0, now - float(book.get("timestamp"))) if book.get("timestamp") else None
            available = bool(ofi_events or signed_vol_events)
            return {
                "available": available,
                "ofi": ofi,
                "aggressorImbalance": aggressor,
                "vpin": vpin,
                "buyVolume": buy_vol if total_traded > 0 else None,
                "sellVolume": sell_vol if total_traded > 0 else None,
                "eventCount": len(ofi_events) + len(signed_vol_events),
                "quoteAgeSec": quote_age,
                "bookAgeSec": book_age,
                "note": "Real-time stream metrics from Schwab level-one + book updates." if available else "Awaiting stream updates.",
            }

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._run_loop())
        except Exception as exc:
            with self._lock:
                self._connected = False
                self._last_error = f"Stream service crashed: {exc}"

    async def _run_loop(self) -> None:
        backoff_sec = 2.0
        while not self._stop_event.is_set():
            try:
                await self._connect_and_stream()
                backoff_sec = 2.0
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                msg = str(exc)
                with self._lock:
                    self._connected = False
                    self._last_error = f"Stream reconnecting after error: {msg}"
                if "403" in msg or "401" in msg:
                    backoff_sec = max(backoff_sec, 30.0)
                await asyncio.sleep(backoff_sec)
                backoff_sec = min(backoff_sec * 1.5, 60.0)

    @staticmethod
    def _is_benign_stream_response_error(exc: Exception) -> bool:
        text = str(exc).upper()
        if "UNEXPECTED RESPONSE CODE DURING MESSAGE HANDLING: 0" not in text:
            return False
        return ("SUBS COMMAND SUCCEEDED" in text) or ("UNSUBS COMMAND SUCCEEDED" in text)

    async def _connect_and_stream(self) -> None:
        try:
            from schwab.streaming import StreamClient
        except Exception as exc:
            with self._lock:
                self._enabled = False
                self._connected = False
                self._last_error = f"schwab-py streaming unavailable: {exc}"
            return

        sdk_client = self.schwab_client.get_sdk_client()
        if sdk_client is None:
            with self._lock:
                self._connected = False
                self._last_error = "Unable to create schwab-py SDK client for streaming."
            return

        account_id = SETTINGS.schwab_account_id or None
        stream = StreamClient(sdk_client, account_id=account_id)
        try:
            self._loop = asyncio.get_running_loop()
            with self._lock:
                self._stream = stream

            await stream.login()
            stream.add_level_one_equity_handler(self._handle_level_one_message)
            stream.add_nasdaq_book_handler(self._handle_book_message)
            nyse_handler = getattr(stream, "add_nyse_book_handler", None)
            listed_handler = getattr(stream, "add_listed_book_handler", None)
            if callable(nyse_handler):
                nyse_handler(self._handle_book_message)
            elif callable(listed_handler):
                listed_handler(self._handle_book_message)

            with self._lock:
                self._connected = True
                self._last_error = None

            await self._subscribe_pending_symbols(stream, full_subscribe=True)

            while not self._stop_event.is_set():
                await self._subscribe_pending_symbols(stream, full_subscribe=False)
                try:
                    await asyncio.wait_for(stream.handle_message(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception as exc:
                    if self._is_benign_stream_response_error(exc):
                        continue
                    raise
        finally:
            with self._lock:
                self._connected = False
            await self._close_stream_socket(stream)
            with self._lock:
                if self._stream is stream:
                    self._stream = None

    async def _close_stream_socket(self, stream: Any) -> None:
        socket = getattr(stream, "_socket", None)
        if socket is None:
            return

        close_method = getattr(socket, "close", None)
        if callable(close_method):
            try:
                await close_method()
            except Exception:
                pass

        wait_closed = getattr(socket, "wait_closed", None)
        if callable(wait_closed):
            try:
                await asyncio.wait_for(wait_closed(), timeout=2.0)
            except Exception:
                pass

        try:
            stream._socket = None
        except Exception:
            pass

    async def _subscribe_pending_symbols(self, stream: Any, full_subscribe: bool) -> None:
        with self._lock:
            symbols = sorted(self._pending_symbols)
            if not symbols and full_subscribe:
                symbols = sorted(set(self.seed_symbols) - self._subscribed_symbols)
            self._pending_symbols.clear()
        if not symbols:
            return

        symbols = [s for s in symbols if s]
        if not symbols:
            return

        level_sub = getattr(stream, "level_one_equity_subs", None)
        level_add = getattr(stream, "level_one_equity_add", None)
        nasdaq_sub = getattr(stream, "nasdaq_book_subs", None)
        nasdaq_add = getattr(stream, "nasdaq_book_add", None)
        nyse_sub = getattr(stream, "nyse_book_subs", None)
        nyse_add = getattr(stream, "nyse_book_add", None)
        listed_sub = getattr(stream, "listed_book_subs", None)
        listed_add = getattr(stream, "listed_book_add", None)

        secondary_book_sub = nyse_sub if callable(nyse_sub) else (listed_sub if callable(listed_sub) else None)
        secondary_book_add = nyse_add if callable(nyse_add) else (listed_add if callable(listed_add) else None)

        try:
            if full_subscribe:
                if callable(level_sub):
                    await level_sub(symbols)
                if callable(nasdaq_sub):
                    await nasdaq_sub(symbols)
                if callable(secondary_book_sub):
                    await secondary_book_sub(symbols)
            else:
                if callable(level_add):
                    await level_add(symbols)
                elif callable(level_sub):
                    await level_sub(symbols)
                if callable(nasdaq_add):
                    await nasdaq_add(symbols)
                elif callable(nasdaq_sub):
                    await nasdaq_sub(symbols)
                if callable(secondary_book_add):
                    await secondary_book_add(symbols)
                elif callable(secondary_book_sub):
                    await secondary_book_sub(symbols)
            with self._lock:
                self._subscribed_symbols.update(symbols)
        except Exception as exc:
            with self._lock:
                self._last_error = f"Symbol subscription error: {exc}"

    def _extract_content(self, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        data = message.get("data")
        if not isinstance(data, list):
            return out
        for envelope in data:
            if not isinstance(envelope, dict):
                continue
            content = envelope.get("content")
            if isinstance(content, list):
                for row in content:
                    if isinstance(row, dict):
                        out.append(row)
        return out

    def _handle_level_one_message(self, message: Dict[str, Any]) -> None:
        rows = self._extract_content(message)
        now = time.time()
        if rows:
            with self._lock:
                self._last_message_ts = now
        for row in rows:
            symbol = self._clean_symbol(str(row.get("key") or row.get("SYMBOL") or row.get("0") or ""))
            if not symbol:
                continue
            bid = self._as_float(row.get("BID_PRICE") if "BID_PRICE" in row else row.get("1"))
            ask = self._as_float(row.get("ASK_PRICE") if "ASK_PRICE" in row else row.get("2"))
            last = self._as_float(row.get("LAST_PRICE") if "LAST_PRICE" in row else row.get("3"))
            bid_size = self._as_float(row.get("BID_SIZE") if "BID_SIZE" in row else row.get("4"))
            ask_size = self._as_float(row.get("ASK_SIZE") if "ASK_SIZE" in row else row.get("5"))
            total_volume = self._as_float(row.get("TOTAL_VOLUME") if "TOTAL_VOLUME" in row else row.get("8"))
            ts = self._extract_ts_seconds(row, default=now)

            with self._lock:
                self._quotes[symbol] = {
                    "symbol": symbol,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "bidSize": bid_size,
                    "askSize": ask_size,
                    "totalVolume": total_volume,
                    "timestamp": ts,
                }
                self._update_trade_flow_locked(symbol, bid, ask, last, total_volume, ts)

    def _parse_book_levels(self, raw_levels: Any, is_bid: bool) -> List[Dict[str, float]]:
        if not isinstance(raw_levels, list):
            return []
        out: List[Dict[str, float]] = []
        for lvl in raw_levels:
            if not isinstance(lvl, dict):
                continue
            if is_bid:
                price = self._as_float(lvl.get("BID_PRICE") if "BID_PRICE" in lvl else lvl.get("0"))
                size = self._as_float(lvl.get("TOTAL_VOLUME") if "TOTAL_VOLUME" in lvl else lvl.get("1"))
            else:
                price = self._as_float(lvl.get("ASK_PRICE") if "ASK_PRICE" in lvl else lvl.get("0"))
                size = self._as_float(lvl.get("TOTAL_VOLUME") if "TOTAL_VOLUME" in lvl else lvl.get("1"))
            if price is None:
                continue
            out.append({"price": float(price), "size": float(size or 0.0)})
        out.sort(key=lambda x: x["price"], reverse=is_bid)
        return out

    def _handle_book_message(self, message: Dict[str, Any]) -> None:
        rows = self._extract_content(message)
        now = time.time()
        if rows:
            with self._lock:
                self._last_message_ts = now
        for row in rows:
            symbol = self._clean_symbol(str(row.get("key") or row.get("SYMBOL") or row.get("0") or ""))
            if not symbol:
                continue
            bids_raw = row.get("BIDS") if "BIDS" in row else row.get("2")
            asks_raw = row.get("ASKS") if "ASKS" in row else row.get("3")
            bids = self._parse_book_levels(bids_raw, is_bid=True)
            asks = self._parse_book_levels(asks_raw, is_bid=False)
            if not bids and not asks:
                continue
            ts = self._extract_ts_seconds(row, default=now)
            with self._lock:
                self._books[symbol] = {
                    "symbol": symbol,
                    "bids": bids[:10],
                    "asks": asks[:10],
                    "timestamp": ts,
                }
                self._update_ofi_from_book_locked(symbol, bids, asks, ts)

    def _ensure_flow_state_locked(self, symbol: str) -> Dict[str, Any]:
        if symbol not in self._flow_state:
            self._flow_state[symbol] = {
                "last_bid": None,
                "last_ask": None,
                "last_bid_size": None,
                "last_ask_size": None,
                "last_mid": None,
                "last_last": None,
                "last_total_volume": None,
                "ofi_events": deque(maxlen=5000),
                "signed_vol_events": deque(maxlen=5000),
            }
        return self._flow_state[symbol]

    def _prune_old_locked(self, state: Dict[str, Any], now_ts: float) -> None:
        cutoff = now_ts - self.lookback_sec
        for key in ("ofi_events", "signed_vol_events"):
            dq: Deque[Dict[str, float]] = state[key]
            while dq and dq[0].get("ts", 0.0) < cutoff:
                dq.popleft()

    def _update_ofi_from_book_locked(
        self,
        symbol: str,
        bids: List[Dict[str, float]],
        asks: List[Dict[str, float]],
        ts: float,
    ) -> None:
        if not bids or not asks:
            return
        state = self._ensure_flow_state_locked(symbol)
        bid_top = bids[0]
        ask_top = asks[0]
        pb = float(bid_top.get("price", 0.0))
        pa = float(ask_top.get("price", 0.0))
        qb = max(0.0, float(bid_top.get("size", 0.0)))
        qa = max(0.0, float(ask_top.get("size", 0.0)))

        pb_prev = state.get("last_bid")
        pa_prev = state.get("last_ask")
        qb_prev = state.get("last_bid_size")
        qa_prev = state.get("last_ask_size")

        if all(v is not None for v in (pb_prev, pa_prev, qb_prev, qa_prev)):
            e_n = (
                (1.0 if pb >= float(pb_prev) else 0.0) * qb
                - (1.0 if pb <= float(pb_prev) else 0.0) * float(qb_prev)
                - (1.0 if pa <= float(pa_prev) else 0.0) * qa
                + (1.0 if pa >= float(pa_prev) else 0.0) * float(qa_prev)
            )
            denom = max(qb + qa + float(qb_prev) + float(qa_prev), 1.0)
            state["ofi_events"].append({"ts": ts, "value": e_n, "denom": denom})

        state["last_bid"] = pb
        state["last_ask"] = pa
        state["last_bid_size"] = qb
        state["last_ask_size"] = qa
        if pb > 0 and pa > 0:
            state["last_mid"] = (pb + pa) / 2.0
        self._prune_old_locked(state, ts)

    def _update_trade_flow_locked(
        self,
        symbol: str,
        bid: Optional[float],
        ask: Optional[float],
        last: Optional[float],
        total_volume: Optional[float],
        ts: float,
    ) -> None:
        state = self._ensure_flow_state_locked(symbol)

        prev_total = state.get("last_total_volume")
        delta = None
        if total_volume is not None and prev_total is not None:
            delta = float(total_volume) - float(prev_total)

        mid = None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (float(bid) + float(ask)) / 2.0
        prev_mid = state.get("last_mid")
        prev_last = state.get("last_last")

        if delta is not None and delta > 0:
            sign = 0.0
            if last is not None:
                reference = prev_last if prev_last is not None else prev_mid
                if reference is not None:
                    if float(last) > float(reference):
                        sign = 1.0
                    elif float(last) < float(reference):
                        sign = -1.0
            if sign == 0.0 and mid is not None and prev_mid is not None:
                if mid > prev_mid:
                    sign = 1.0
                elif mid < prev_mid:
                    sign = -1.0
            signed = float(delta) * sign
            state["signed_vol_events"].append({"ts": ts, "signed": signed, "total": float(delta)})

        if bid is not None:
            state["last_bid"] = float(bid)
        if ask is not None:
            state["last_ask"] = float(ask)
        if mid is not None:
            state["last_mid"] = mid
        if last is not None:
            state["last_last"] = float(last)
        if total_volume is not None:
            state["last_total_volume"] = float(total_volume)

        self._prune_old_locked(state, ts)
