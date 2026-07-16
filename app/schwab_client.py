from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from .config import SETTINGS


class SchwabClient:
    BASE_URL = "https://api.schwabapi.com"
    OAUTH_URL = f"{BASE_URL}/v1/oauth/token"

    def __init__(self) -> None:
        self.session = requests.Session()
        self._account_hash: Optional[str] = None
        self._sdk_client = None

    @property
    def enabled(self) -> bool:
        return all(
            [
                SETTINGS.schwab_api_key,
                SETTINGS.schwab_app_secret,
                SETTINGS.schwab_token_path,
            ]
        )

    def _load_token_file(self) -> Dict[str, Any]:
        path = Path(SETTINGS.schwab_token_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Schwab token file not found at {path}. Create token first."
            )
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _save_token_file(self, payload: Dict[str, Any], original: Dict[str, Any]) -> None:
        path = Path(SETTINGS.schwab_token_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Preserve schwab-py token wrapper format if present.
        if "token" in original and isinstance(original["token"], dict):
            wrapped = dict(original)
            wrapped["token"] = payload
            wrapped["creation_timestamp"] = int(time.time())
            to_write = wrapped
        else:
            to_write = dict(payload)
            to_write["creation_timestamp"] = int(time.time())

        with path.open("w", encoding="utf-8") as f:
            json.dump(to_write, f, indent=2)

    @staticmethod
    def _unwrap_token(payload: Dict[str, Any]) -> Dict[str, Any]:
        if "token" in payload and isinstance(payload["token"], dict):
            return payload["token"]
        return payload

    @staticmethod
    def _is_expired(token: Dict[str, Any]) -> bool:
        now = time.time()
        expires_at = token.get("expires_at")
        if isinstance(expires_at, (int, float)):
            return now >= float(expires_at) - 30

        created = token.get("creation_timestamp") or token.get("created_at")
        expires_in = token.get("expires_in")
        if isinstance(created, (int, float)) and isinstance(expires_in, (int, float)):
            return now >= float(created) + float(expires_in) - 30

        # If we cannot determine expiry, do not force refresh.
        return False

    def _refresh_token(self, current_token: Dict[str, Any], original: Dict[str, Any]) -> Dict[str, Any]:
        refresh_token = current_token.get("refresh_token")
        if not refresh_token:
            return current_token

        basic = base64.b64encode(
            f"{SETTINGS.schwab_api_key}:{SETTINGS.schwab_app_secret}".encode("utf-8")
        ).decode("utf-8")
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        resp = self.session.post(self.OAUTH_URL, data=form, headers=headers, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Schwab token refresh failed: HTTP {resp.status_code} {resp.text}")

        fresh = resp.json()
        # Some refresh responses omit a new refresh token; preserve old one.
        if "refresh_token" not in fresh:
            fresh["refresh_token"] = refresh_token
        fresh["expires_at"] = int(time.time()) + int(fresh.get("expires_in", 1800))
        self._save_token_file(fresh, original)
        return fresh

    def _access_token(self) -> str:
        if not self.enabled:
            raise RuntimeError("Schwab integration not configured.")
        original = self._load_token_file()
        token = self._unwrap_token(original)

        if self._is_expired(token):
            token = self._refresh_token(token, original)
        access = token.get("access_token")
        if not access:
            raise RuntimeError("No access token present in Schwab token file.")
        return access

    def _headers(self, method: str = "GET") -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
        }
        if method.upper() == "POST":
            headers["Content-Type"] = "application/json"
        return headers

    def _get_sdk_client(self):
        if self._sdk_client is not None:
            return self._sdk_client
        try:
            import schwab
        except Exception:
            return None

        token_path = str(SETTINGS.schwab_token_path)
        self._sdk_client = schwab.auth.client_from_token_file(
            token_path=token_path,
            api_key=SETTINGS.schwab_api_key,
            app_secret=SETTINGS.schwab_app_secret,
            asyncio=False,
        )
        return self._sdk_client

    def get_sdk_client(self):
        """Public accessor for callers that need schwab-py SDK primitives."""
        return self._get_sdk_client()

    def _resolve_account_hash(self) -> str:
        if self._account_hash:
            return self._account_hash

        sdk = self._get_sdk_client()
        if sdk is not None:
            try:
                resp = sdk.get_account_numbers()
                if getattr(resp, "status_code", None) == 200:
                    accounts = resp.json()
                    if isinstance(accounts, list) and accounts:
                        configured = SETTINGS.schwab_account_id.replace("-", "")
                        chosen = accounts[0]
                        if configured:
                            for acct in accounts:
                                if str(acct.get("accountNumber", "")).replace("-", "") == configured:
                                    chosen = acct
                                    break
                        hash_value = chosen.get("hashValue")
                        if hash_value:
                            self._account_hash = hash_value
                            return hash_value
            except Exception:
                pass

        url = f"{self.BASE_URL}/trader/v1/accounts/accountNumbers"
        resp = self.session.get(url, headers=self._headers(), timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Could not resolve account hash: HTTP {resp.status_code} {resp.text}")
        accounts = resp.json()
        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("No Schwab accounts available for token.")

        configured = SETTINGS.schwab_account_id.replace("-", "")
        chosen = accounts[0]
        if configured:
            for acct in accounts:
                if str(acct.get("accountNumber", "")).replace("-", "") == configured:
                    chosen = acct
                    break
        hash_value = chosen.get("hashValue")
        if not hash_value:
            raise RuntimeError("Account hash missing from Schwab response.")
        self._account_hash = hash_value
        return hash_value

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        symbol = (symbol or "").upper().strip().replace(",", "").replace(";", "")
        sdk = self._get_sdk_client()
        last_error = ""

        # Primary path: schwab-py SDK methods (known-good request formatting).
        if sdk is not None:
            try:
                resp = sdk.get_quote(symbol)
                if getattr(resp, "status_code", None) == 200:
                    payload = resp.json()
                    return self._parse_quote_payload(symbol, payload)
                last_error = (
                    f"SDK get_quote HTTP {getattr(resp, 'status_code', '?')} "
                    f"{getattr(resp, 'text', '')}"
                )
            except Exception as exc:
                last_error = f"SDK get_quote failed: {exc}"

            try:
                resp = sdk.get_quotes([symbol], indicative=False)
                if getattr(resp, "status_code", None) == 200:
                    payload = resp.json()
                    return self._parse_quote_payload(symbol, payload)
                last_error = (
                    f"SDK get_quotes HTTP {getattr(resp, 'status_code', '?')} "
                    f"{getattr(resp, 'text', '')}"
                )
            except Exception as exc:
                last_error = f"SDK get_quotes failed: {exc}"

        # Secondary path: direct REST fallbacks.
        url = f"{self.BASE_URL}/marketdata/v1/quotes"
        params = {"symbols": symbol, "fields": "quote"}
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=10)
        if resp.status_code != 200:
            params = {"symbols": symbol}
            resp = self.session.get(url, headers=self._headers(method="GET"), params=params, timeout=10)
        if resp.status_code != 200:
            url2 = f"{self.BASE_URL}/marketdata/v1/{symbol}/quotes"
            resp = self.session.get(url2, headers=self._headers(method="GET"), timeout=10)
        if resp.status_code != 200:
            detail = resp.text.strip() if hasattr(resp, "text") and resp.text else "(empty body)"
            raise RuntimeError(
                f"Quote fetch failed: HTTP {resp.status_code} {detail}. "
                f"Last SDK error: {last_error}"
            )

        payload = resp.json()
        return self._parse_quote_payload(symbol, payload)

    def get_equity_fundamentals(self, symbol: str) -> Dict[str, Any]:
        symbol = (symbol or "").upper().strip().replace(",", "").replace(";", "")
        sdk = self._get_sdk_client()
        last_error = ""
        if sdk is not None:
            try:
                projection = sdk.Instrument.Projection.FUNDAMENTAL
                resp = sdk.get_instruments([symbol], projection)
                if getattr(resp, "status_code", None) == 200:
                    payload = resp.json()
                    instruments = payload.get("instruments", []) if isinstance(payload, dict) else []
                    if isinstance(instruments, list) and instruments:
                        first = instruments[0] if isinstance(instruments[0], dict) else {}
                        fundamentals = first.get("fundamental", {}) if isinstance(first, dict) else {}
                        if isinstance(fundamentals, dict):
                            return fundamentals
                last_error = (
                    f"SDK get_instruments HTTP {getattr(resp, 'status_code', '?')} "
                    f"{getattr(resp, 'text', '')}"
                )
            except Exception as exc:
                last_error = f"SDK get_instruments failed: {exc}"

        url = f"{self.BASE_URL}/marketdata/v1/instruments"
        params = {"symbol": symbol, "projection": "fundamental"}
        resp = self.session.get(url, headers=self._headers(method="GET"), params=params, timeout=12)
        if resp.status_code != 200:
            detail = resp.text.strip() if hasattr(resp, "text") and resp.text else "(empty body)"
            raise RuntimeError(
                f"Instrument fundamentals fetch failed: HTTP {resp.status_code} {detail}. "
                f"Last SDK error: {last_error}"
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected instrument fundamentals payload type.")
        instruments = payload.get("instruments", [])
        if not isinstance(instruments, list) or not instruments:
            return {}
        first = instruments[0] if isinstance(instruments[0], dict) else {}
        fundamentals = first.get("fundamental", {}) if isinstance(first, dict) else {}
        return fundamentals if isinstance(fundamentals, dict) else {}

    @staticmethod
    def _parse_quote_payload(symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sym_data = payload.get(symbol) or next(iter(payload.values()), {})
        quote = sym_data.get("quote", {}) if isinstance(sym_data, dict) else {}
        return {
            "symbol": symbol,
            "bid": quote.get("bidPrice"),
            "ask": quote.get("askPrice"),
            "last": quote.get("lastPrice"),
            "bidSize": quote.get("bidSize"),
            "askSize": quote.get("askSize"),
            "totalVolume": quote.get("totalVolume") or quote.get("regularMarketVolume"),
            "raw": payload,
        }

    def get_price_history(
        self,
        symbol: str,
        interval_minutes: int = 5,
        days: int = 3,
        need_extended_hours_data: bool = True,
    ) -> Dict[str, Any]:
        symbol = (symbol or "").upper().strip().replace(",", "").replace(";", "")
        if interval_minutes not in {1, 5, 10, 15, 30}:
            raise ValueError("interval_minutes must be one of: 1, 5, 10, 15, 30")
        days = max(1, min(int(days), 30))

        sdk = self._get_sdk_client()
        last_error = ""
        if sdk is not None:
            end_dt = datetime.now(timezone.utc)
            start_dt = end_dt - timedelta(days=days)
            try:
                if interval_minutes == 1:
                    resp = sdk.get_price_history_every_minute(
                        symbol,
                        start_datetime=start_dt,
                        end_datetime=end_dt,
                        need_extended_hours_data=need_extended_hours_data,
                        need_previous_close=False,
                    )
                elif interval_minutes == 5:
                    resp = sdk.get_price_history_every_five_minutes(
                        symbol,
                        start_datetime=start_dt,
                        end_datetime=end_dt,
                        need_extended_hours_data=need_extended_hours_data,
                        need_previous_close=False,
                    )
                elif interval_minutes == 10:
                    resp = sdk.get_price_history_every_ten_minutes(
                        symbol,
                        start_datetime=start_dt,
                        end_datetime=end_dt,
                        need_extended_hours_data=need_extended_hours_data,
                        need_previous_close=False,
                    )
                elif interval_minutes == 15:
                    resp = sdk.get_price_history_every_fifteen_minutes(
                        symbol,
                        start_datetime=start_dt,
                        end_datetime=end_dt,
                        need_extended_hours_data=need_extended_hours_data,
                        need_previous_close=False,
                    )
                else:
                    resp = sdk.get_price_history_every_thirty_minutes(
                        symbol,
                        start_datetime=start_dt,
                        end_datetime=end_dt,
                        need_extended_hours_data=need_extended_hours_data,
                        need_previous_close=False,
                    )

                if getattr(resp, "status_code", None) == 200:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        return payload
                last_error = (
                    f"SDK history HTTP {getattr(resp, 'status_code', '?')} "
                    f"{getattr(resp, 'text', '')}"
                )
            except Exception as exc:
                last_error = f"SDK history failed: {exc}"

        url = f"{self.BASE_URL}/marketdata/v1/pricehistory"
        params = {
            "symbol": symbol,
            "periodType": "day",
            "period": days,
            "frequencyType": "minute",
            "frequency": interval_minutes,
            "needExtendedHoursData": str(bool(need_extended_hours_data)).lower(),
            "needPreviousClose": "false",
        }
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=15)
        if resp.status_code != 200:
            # Fallback to start/end-only format.
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - (days * 24 * 60 * 60 * 1000)
            params = {
                "symbol": symbol,
                "frequencyType": "minute",
                "frequency": interval_minutes,
                "startDate": start_ms,
                "endDate": now_ms,
                "needExtendedHoursData": str(bool(need_extended_hours_data)).lower(),
                "needPreviousClose": "false",
            }
            resp = self.session.get(url, headers=self._headers(method="GET"), params=params, timeout=15)
        if resp.status_code != 200:
            detail = resp.text.strip() if hasattr(resp, "text") and resp.text else "(empty body)"
            raise RuntimeError(
                f"Price history fetch failed: HTTP {resp.status_code} {detail}. "
                f"Last SDK error: {last_error}"
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Schwab price-history payload type.")
        return payload

    def place_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        account_hash = self._resolve_account_hash()
        url = f"{self.BASE_URL}/trader/v1/accounts/{account_hash}/orders"
        resp = self.session.post(url, headers=self._headers(method="POST"), json=order, timeout=15)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Order rejected: HTTP {resp.status_code} {resp.text}")
        location = resp.headers.get("Location") or resp.headers.get("location")
        order_id = self.extract_order_id(location) if location else None
        return {
            "status": "submitted",
            "http_status": resp.status_code,
            "location": location,
            "orderId": order_id,
        }

    @staticmethod
    def extract_order_id(location: Optional[str]) -> Optional[str]:
        if not location:
            return None
        match = re.search(r"/orders/([^/?]+)", location)
        if not match:
            return None
        return match.group(1)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        account_hash = self._resolve_account_hash()
        url = f"{self.BASE_URL}/trader/v1/accounts/{account_hash}/orders/{order_id}"
        resp = self.session.get(url, headers=self._headers(method="GET"), timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Order lookup failed: HTTP {resp.status_code} {resp.text}")
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Schwab order payload type.")
        return payload

    def get_recent_orders(self) -> List[Dict[str, Any]]:
        account_hash = self._resolve_account_hash()
        url = f"{self.BASE_URL}/trader/v1/accounts/{account_hash}/orders"
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=30)
        params = {
            "fromEnteredTime": start.strftime("%Y-%m-%dT%H:%M:%S%z")[:-5] + "Z",
            "toEnteredTime": now.strftime("%Y-%m-%dT%H:%M:%S%z")[:-5] + "Z",
        }
        resp = self.session.get(url, headers=self._headers(method="GET"), params=params, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Order history fetch failed: HTTP {resp.status_code} {resp.text}")
        payload = resp.json()
        raw_orders: List[Dict[str, Any]]
        if isinstance(payload, list):
            raw_orders = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            orders = payload.get("orders")
            if isinstance(orders, list):
                raw_orders = [item for item in orders if isinstance(item, dict)]
            else:
                raw_orders = []
        else:
            raw_orders = []

        flattened: List[Dict[str, Any]] = []

        def _walk(order: Dict[str, Any]) -> None:
            flattened.append(order)
            children = order.get("childOrderStrategies")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        _walk(child)

        for order in raw_orders:
            _walk(order)

        deduped: Dict[str, Dict[str, Any]] = {}
        extras: List[Dict[str, Any]] = []
        for order in flattened:
            order_id = order.get("orderId") or order.get("id")
            if order_id is None:
                extras.append(order)
                continue
            key = str(order_id)
            existing = deduped.get(key)
            # Prefer the richer payload when duplicates exist.
            if existing is None or len(order.keys()) >= len(existing.keys()):
                deduped[key] = order

        return list(deduped.values()) + extras

    @staticmethod
    def summarize_order(order_payload: Dict[str, Any]) -> Dict[str, Any]:
        status = str(order_payload.get("status") or "").upper()
        filled_qty = order_payload.get("filledQuantity")
        remaining_qty = order_payload.get("remainingQuantity")

        average_fill_price = None
        activities = order_payload.get("orderActivityCollection")
        if isinstance(activities, list):
            for activity in activities:
                legs = activity.get("executionLegs")
                if not isinstance(legs, list):
                    continue
                prices = []
                quantities = []
                for leg in legs:
                    price = leg.get("price")
                    quantity = leg.get("quantity")
                    if not isinstance(price, (int, float)) or float(price) <= 0:
                        continue
                    qty_value = float(quantity) if isinstance(quantity, (int, float)) and float(quantity) > 0 else None
                    prices.append(float(price))
                    if qty_value is not None:
                        quantities.append(qty_value)
                if prices:
                    if quantities and len(quantities) == len(prices) and sum(quantities) > 0:
                        average_fill_price = sum(p * q for p, q in zip(prices, quantities)) / sum(quantities)
                    else:
                        average_fill_price = sum(prices) / len(prices)
                    break

        if average_fill_price is None:
            order_price = order_payload.get("price")
            if isinstance(order_price, (int, float)) and float(order_price) > 0:
                average_fill_price = float(order_price)

        symbol = None
        side = None
        quantity = None
        legs = order_payload.get("orderLegCollection")
        if isinstance(legs, list):
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                instrument = leg.get("instrument")
                if isinstance(instrument, dict):
                    symbol_value = instrument.get("symbol") or leg.get("symbol")
                    if symbol_value:
                        symbol = str(symbol_value).upper()
                instruction = str(leg.get("instruction") or "").upper()
                if not side and instruction:
                    if instruction in {"BUY", "BUY_TO_COVER"}:
                        side = "BUY"
                    elif instruction in {"SELL", "SELL_SHORT", "SELL_TO_COVER"}:
                        side = "SELL"
                leg_quantity = leg.get("quantity")
                if quantity is None:
                    if isinstance(leg_quantity, (int, float)) and not isinstance(leg_quantity, bool):
                        quantity = int(leg_quantity)
                    elif isinstance(leg_quantity, str):
                        try:
                            quantity = int(float(leg_quantity))
                        except ValueError:
                            quantity = None
                if symbol or side or quantity is not None:
                    break

        if quantity is None and isinstance(order_payload.get("quantity"), (int, float)):
            quantity = int(order_payload.get("quantity"))

        if side is None:
            side_value = order_payload.get("side") or order_payload.get("instruction")
            side_text = str(side_value or "").upper()
            if side_text in {"BUY", "BUY_TO_COVER"}:
                side = "BUY"
            elif side_text in {"SELL", "SELL_SHORT", "SELL_TO_COVER"}:
                side = "SELL"

        return {
            "status": status,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "filledQuantity": float(filled_qty) if isinstance(filled_qty, (int, float)) else None,
            "remainingQuantity": float(remaining_qty) if isinstance(remaining_qty, (int, float)) else None,
            "averageFillPrice": average_fill_price,
        }

    @staticmethod
    def build_equity_order_payload(
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        side_up = side.upper()
        if side_up not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        order_type = order_type.upper()
        if order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("order_type must be MARKET or LIMIT")
        if order_type == "LIMIT" and limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")

        base_order: Dict[str, Any] = {
            "orderType": order_type,
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": side_up,
                    "quantity": int(quantity),
                    "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
                }
            ],
        }
        if order_type == "LIMIT":
            base_order["price"] = float(limit_price)

        if stop_loss is None and take_profit is None:
            return base_order

        # Simple bracket: primary order triggers OCO exits.
        exit_instruction = "SELL" if side_up == "BUY" else "BUY"
        children = []
        if stop_loss is not None:
            children.append(
                {
                    "orderType": "STOP",
                    "session": "NORMAL",
                    "duration": "GOOD_TILL_CANCEL",
                    "stopPrice": float(stop_loss),
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [
                        {
                            "instruction": exit_instruction,
                            "quantity": int(quantity),
                            "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
                        }
                    ],
                }
            )
        if take_profit is not None:
            children.append(
                {
                    "orderType": "LIMIT",
                    "session": "NORMAL",
                    "duration": "GOOD_TILL_CANCEL",
                    "price": float(take_profit),
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [
                        {
                            "instruction": exit_instruction,
                            "quantity": int(quantity),
                            "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
                        }
                    ],
                }
            )

        if children:
            base_order = {
                **base_order,
                "orderStrategyType": "TRIGGER",
                "childOrderStrategies": [
                    {
                        "orderStrategyType": "OCO",
                        "childOrderStrategies": children,
                    }
                ],
            }
        return base_order
