from __future__ import annotations

import datetime as dt
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf

from .config import SETTINGS
from .schwab_client import SchwabClient


class TTLCache:
    def __init__(self, ttl_sec: int = 300):
        self.ttl_sec = ttl_sec
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > self.ttl_sec:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)


class MarketDataProvider:
    FINNHUB_BASE = "https://finnhub.io/api/v1"
    ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"
    NASDAQ_BASE = "https://api.nasdaq.com/api"

    def __init__(self, schwab_client: Optional[SchwabClient] = None) -> None:
        self.session = requests.Session()
        self.cache = TTLCache(ttl_sec=180)
        self.finnhub_api_key = SETTINGS.finnhub_api_key
        self.alphavantage_api_key = SETTINGS.alphavantage_api_key
        self.schwab_client = schwab_client
        self._intraday_cache: Dict[str, Tuple[float, pd.DataFrame]] = {}
        self._intraday_cache_ttl_sec = 120
        self._yfinance_disabled = SETTINGS.disable_yfinance
        self._yfinance_disable_reason = (
            "Disabled by DISABLE_YFINANCE setting." if self._yfinance_disabled else ""
        )

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and float(value) > 0

    @staticmethod
    def _metric_value(metric: Dict[str, Any], keys: List[str]) -> Optional[float]:
        for key in keys:
            value = metric.get(key)
            try:
                if value is None or value == "":
                    continue
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text_to_number(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text or text in {"--", "-", "N/A", "n/a", "None"}:
            return None

        negative = text.startswith("(") and text.endswith(")")
        cleaned = (
            text.replace("%", "")
            .replace("$", "")
            .replace(",", "")
            .replace("(", "")
            .replace(")", "")
            .strip()
        )
        try:
            number = float(cleaned)
            return -number if negative else number
        except ValueError:
            return None

    @classmethod
    def _text_to_percent(cls, value: Any) -> Optional[float]:
        number = cls._text_to_number(value)
        if number is None:
            return None
        return number / 100.0 if abs(number) > 1 else number

    @staticmethod
    def _is_ssl_cert_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "ssl certificate problem" in message
            or "certificate verify failed" in message
            or "unable to get local issuer certificate" in message
        )

    def _disable_yfinance(self, reason: str) -> None:
        self._yfinance_disabled = True
        if not self._yfinance_disable_reason:
            self._yfinance_disable_reason = reason

    def _finnhub_get(self, endpoint: str, params: Dict[str, Any]) -> Any:
        if not self.finnhub_api_key:
            return None
        query = {**params, "token": self.finnhub_api_key}
        try:
            resp = self.session.get(f"{self.FINNHUB_BASE}/{endpoint}", params=query, timeout=10)
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    def _nasdaq_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        cache_key = f"nasdaq:{endpoint}:{sorted((params or {}).items())}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        symbol_hint = endpoint.split("/")[2] if "/" in endpoint else ""
        referer_symbol = symbol_hint.lower().strip() if symbol_hint else ""
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": f"https://www.nasdaq.com/market-activity/stocks/{referer_symbol}",
        }
        try:
            resp = self.session.get(
                f"{self.NASDAQ_BASE}/{endpoint}",
                params=params or {},
                headers=headers,
                timeout=12,
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if not isinstance(payload, dict):
                return None

            status = payload.get("status")
            if isinstance(status, dict):
                rcode = status.get("rCode")
                if isinstance(rcode, int) and rcode >= 400:
                    return None
            data = payload.get("data")
            if data is None:
                return None
            self.cache.set(cache_key, data)
            return data
        except Exception:
            return None

    def _alphavantage_get(self, params: Dict[str, Any]) -> Any:
        if not self.alphavantage_api_key:
            return None
        query = {**params, "apikey": self.alphavantage_api_key}
        try:
            resp = self.session.get(self.ALPHAVANTAGE_BASE, params=query, timeout=12)
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if not isinstance(payload, dict):
                return None
            if payload.get("Error Message") or payload.get("Information"):
                return None
            return payload
        except Exception:
            return None

    def _alphavantage_intraday(self, symbol: str, interval: str) -> pd.DataFrame:
        payload = self._alphavantage_get(
            {
                "function": "TIME_SERIES_INTRADAY",
                "symbol": symbol,
                "interval": interval,
                "outputsize": "full",
                "datatype": "json",
            }
        )
        if not isinstance(payload, dict):
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        key = f"Time Series ({interval})"
        series = payload.get(key)
        if not isinstance(series, dict) or not series:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        rows = []
        for ts, values in series.items():
            if not isinstance(values, dict):
                continue
            rows.append(
                (
                    ts,
                    self._as_float(values.get("1. open")),
                    self._as_float(values.get("2. high")),
                    self._as_float(values.get("3. low")),
                    self._as_float(values.get("4. close")),
                    self._as_float(values.get("5. volume")),
                )
            )
        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        frame = pd.DataFrame(
            rows,
            columns=["timestamp", "Open", "High", "Low", "Close", "Volume"],
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.dropna(subset=["timestamp", "Open", "High", "Low", "Close"])
        if frame.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        try:
            frame["timestamp"] = (
                frame["timestamp"]
                .dt.tz_localize("America/New_York", ambiguous="infer", nonexistent="shift_forward")
                .dt.tz_convert("UTC")
            )
        except Exception:
            # If timezone localization fails, keep naive timestamps.
            pass

        frame = frame.set_index("timestamp").sort_index()
        frame["Volume"] = frame["Volume"].fillna(0)
        return frame[["Open", "High", "Low", "Close", "Volume"]]

    def _finnhub_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> pd.DataFrame:
        payload = self._finnhub_get(
            "stock/candle",
            {"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts},
        )
        if not isinstance(payload, dict) or payload.get("s") != "ok":
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        try:
            frame = pd.DataFrame(
                {
                    "Open": payload.get("o", []),
                    "High": payload.get("h", []),
                    "Low": payload.get("l", []),
                    "Close": payload.get("c", []),
                    "Volume": payload.get("v", []),
                },
                index=pd.to_datetime(payload.get("t", []), unit="s", utc=True),
            )
            frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
            return frame
        except Exception:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def _schwab_intraday(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        if not self.schwab_client or not self.schwab_client.enabled:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        interval_map = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}
        interval_minutes = interval_map.get(interval)
        if interval_minutes is None:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        try:
            payload = self.schwab_client.get_price_history(
                symbol=symbol,
                interval_minutes=interval_minutes,
                days=days,
                need_extended_hours_data=True,
            )
            candles = payload.get("candles", [])
            if not isinstance(candles, list) or not candles:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

            frame = pd.DataFrame(candles)
            if frame.empty:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

            if "datetime" in frame.columns:
                frame.index = pd.to_datetime(frame["datetime"], unit="ms", utc=True, errors="coerce")
            else:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

            renamed = frame.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )
            out = renamed[["Open", "High", "Low", "Close", "Volume"]].dropna(
                subset=["Open", "High", "Low", "Close"]
            )
            out["Volume"] = out["Volume"].fillna(0)
            return out.sort_index()
        except Exception:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def _cache_intraday(self, key: str, frame: pd.DataFrame) -> None:
        if frame is not None and not frame.empty:
            self._intraday_cache[key] = (time.time(), frame.copy())

    def _get_cached_intraday(self, key: str, allow_stale: bool = False) -> pd.DataFrame:
        item = self._intraday_cache.get(key)
        if not item:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        ts, frame = item
        if not allow_stale and (time.time() - ts > self._intraday_cache_ttl_sec):
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        return frame.copy()

    def _clip_to_recent_trading_days(self, frame: pd.DataFrame, days: int) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        keep_days = max(1, int(days))
        try:
            out = frame.copy().sort_index()
            out.index = pd.to_datetime(out.index, utc=True, errors="coerce")
            out = out[~out.index.isna()]
            if out.empty:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

            ny_dates = out.index.tz_convert("America/New_York").date
            unique_dates = sorted(set(ny_dates))
            keep_dates = set(unique_dates[-keep_days:])
            mask = [d in keep_dates for d in ny_dates]
            clipped = out[mask]
            return clipped if not clipped.empty else out
        except Exception:
            # If timezone/date clipping fails unexpectedly, return original frame.
            return frame

    def ticker_info(self, symbol: str, include_ownership_fallbacks: bool = True) -> Dict[str, Any]:
        cache_key = f"info:{symbol}:ownership:{int(include_ownership_fallbacks)}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        info: Dict[str, Any] = {}
        fast: Dict[str, Any] = {}
        error_msg: Optional[str] = None
        if not self._yfinance_disabled:
            try:
                ticker = yf.Ticker(symbol)
                info = ticker.info or {}
                fast = ticker.fast_info if hasattr(ticker, "fast_info") and ticker.fast_info else {}
            except Exception as exc:
                error_msg = str(exc)
                if self._is_ssl_cert_error(exc):
                    self._disable_yfinance(error_msg)
        elif self._yfinance_disable_reason:
            error_msg = self._yfinance_disable_reason

        merged = {**info}
        for key in ("market_cap", "last_price", "last_volume", "shares", "currency"):
            if fast and key in fast and fast.get(key) is not None:
                merged[f"fast_{key}"] = fast.get(key)

        # Fill gaps from Finnhub when yfinance is unavailable/partial.
        profile = self._finnhub_get("stock/profile2", {"symbol": symbol})
        if isinstance(profile, dict):
            mcap_mn = profile.get("marketCapitalization")
            if merged.get("marketCap") is None and isinstance(mcap_mn, (int, float)):
                merged["marketCap"] = float(mcap_mn) * 1_000_000
            if not merged.get("longName") and profile.get("name"):
                merged["longName"] = profile.get("name")
            if not merged.get("exchange") and profile.get("exchange"):
                merged["exchange"] = profile.get("exchange")
            if not merged.get("industry") and profile.get("finnhubIndustry"):
                merged["industry"] = profile.get("finnhubIndustry")

        quote = self._finnhub_get("quote", {"symbol": symbol})
        if isinstance(quote, dict):
            if merged.get("currentPrice") is None and isinstance(quote.get("c"), (int, float)):
                merged["currentPrice"] = float(quote.get("c"))
            if merged.get("regularMarketPrice") is None and isinstance(quote.get("c"), (int, float)):
                merged["regularMarketPrice"] = float(quote.get("c"))
            if merged.get("volume") is None and isinstance(quote.get("v"), (int, float)):
                merged["volume"] = float(quote.get("v"))
            if merged.get("previousClose") is None and isinstance(quote.get("pc"), (int, float)):
                merged["previousClose"] = float(quote.get("pc"))
            if merged.get("change") is None and isinstance(quote.get("d"), (int, float)):
                merged["change"] = float(quote.get("d"))
            if merged.get("changePercent") is None and isinstance(quote.get("dp"), (int, float)):
                merged["changePercent"] = float(quote.get("dp"))

        float_shares: Optional[float] = None
        # Schwab quote (primary for realtime fields) if available.
        if self.schwab_client and self.schwab_client.enabled:
            try:
                schwab_quote = self.schwab_client.get_quote(symbol)
                if merged.get("currentPrice") is None and self._is_positive_number(schwab_quote.get("last")):
                    merged["currentPrice"] = float(schwab_quote.get("last"))
                if merged.get("regularMarketPrice") is None and self._is_positive_number(schwab_quote.get("last")):
                    merged["regularMarketPrice"] = float(schwab_quote.get("last"))
                if merged.get("volume") is None and self._is_positive_number(schwab_quote.get("totalVolume")):
                    merged["volume"] = float(schwab_quote.get("totalVolume"))
                schwab_raw = schwab_quote.get("raw", {})
                if isinstance(schwab_raw, dict):
                    sym_data = schwab_raw.get(symbol) or next(iter(schwab_raw.values()), {})
                    if isinstance(sym_data, dict):
                        quote_block = sym_data.get("quote", {}) if isinstance(sym_data.get("quote"), dict) else {}
                        if merged.get("previousClose") is None and self._is_positive_number(quote_block.get("closePrice")):
                            merged["previousClose"] = float(quote_block.get("closePrice"))
                        if merged.get("change") is None and isinstance(quote_block.get("netChange"), (int, float)):
                            merged["change"] = float(quote_block.get("netChange"))
                        if merged.get("changePercent") is None and isinstance(
                            quote_block.get("netPercentChange"),
                            (int, float),
                        ):
                            merged["changePercent"] = float(quote_block.get("netPercentChange"))
            except Exception:
                pass
            try:
                schwab_fund = self.schwab_client.get_equity_fundamentals(symbol)
                if isinstance(schwab_fund, dict):
                    if merged.get("sharesOutstanding") is None and self._is_positive_number(
                        schwab_fund.get("sharesOutstanding")
                    ):
                        merged["sharesOutstanding"] = float(schwab_fund.get("sharesOutstanding"))
                    if merged.get("marketCap") is None and self._is_positive_number(schwab_fund.get("marketCap")):
                        merged["marketCap"] = float(schwab_fund.get("marketCap"))
                    if merged.get("beta") is None and self._is_positive_number(schwab_fund.get("beta")):
                        merged["beta"] = float(schwab_fund.get("beta"))
                    if merged.get("trailingPE") is None and self._is_positive_number(schwab_fund.get("peRatio")):
                        merged["trailingPE"] = float(schwab_fund.get("peRatio"))
                    if merged.get("trailingEps") is None and self._is_positive_number(schwab_fund.get("epsTTM")):
                        merged["trailingEps"] = float(schwab_fund.get("epsTTM"))
                    if merged.get("averageVolume") is None and self._is_positive_number(schwab_fund.get("avg10DaysVolume")):
                        merged["averageVolume"] = float(schwab_fund.get("avg10DaysVolume"))
                    if merged.get("fiftyTwoWeekHigh") is None and self._is_positive_number(schwab_fund.get("high52")):
                        merged["fiftyTwoWeekHigh"] = float(schwab_fund.get("high52"))
                    if merged.get("fiftyTwoWeekLow") is None and self._is_positive_number(schwab_fund.get("low52")):
                        merged["fiftyTwoWeekLow"] = float(schwab_fund.get("low52"))
                    if merged.get("shortRatio") is None and self._is_positive_number(schwab_fund.get("shortIntDayToCover")):
                        merged["shortRatio"] = float(schwab_fund.get("shortIntDayToCover"))
                    if merged.get("shortPercentOfFloat") is None and self._is_positive_number(
                        schwab_fund.get("shortIntToFloat")
                    ):
                        merged["shortPercentOfFloat"] = float(schwab_fund.get("shortIntToFloat"))
                    if self._is_positive_number(schwab_fund.get("marketCapFloat")):
                        float_shares = float(schwab_fund.get("marketCapFloat"))
            except Exception:
                pass

        overview = self._alphavantage_get({"function": "OVERVIEW", "symbol": symbol})
        if isinstance(overview, dict):
            market_cap = self._as_float(overview.get("MarketCapitalization"))
            if merged.get("marketCap") is None and market_cap is not None:
                merged["marketCap"] = market_cap
            if merged.get("sharesOutstanding") is None:
                merged["sharesOutstanding"] = self._as_float(overview.get("SharesOutstanding"))
            if merged.get("beta") is None:
                merged["beta"] = self._as_float(overview.get("Beta"))
            if merged.get("trailingPE") is None:
                merged["trailingPE"] = self._as_float(overview.get("TrailingPE")) or self._as_float(overview.get("PERatio"))
            if merged.get("trailingEps") is None:
                merged["trailingEps"] = self._as_float(overview.get("EPS"))
            if merged.get("totalRevenue") is None:
                merged["totalRevenue"] = self._as_float(overview.get("RevenueTTM"))
            if merged.get("profitMargins") is None:
                merged["profitMargins"] = self._as_float(overview.get("ProfitMargin"))
            if merged.get("operatingMargins") is None:
                merged["operatingMargins"] = self._as_float(overview.get("OperatingMarginTTM"))
            if merged.get("grossMargins") is None:
                gross_profit = self._as_float(overview.get("GrossProfitTTM"))
                revenue = self._as_float(overview.get("RevenueTTM"))
                if gross_profit is not None and revenue and revenue != 0:
                    merged["grossMargins"] = gross_profit / revenue
            if merged.get("shortRatio") is None:
                merged["shortRatio"] = self._as_float(overview.get("ShortRatio"))
            if merged.get("shortPercentOfFloat") is None:
                merged["shortPercentOfFloat"] = self._as_float(overview.get("ShortPercentFloat")) or self._as_float(
                    overview.get("ShortPercentOutstanding")
                )
            if merged.get("heldPercentInsiders") is None:
                merged["heldPercentInsiders"] = self._as_float(overview.get("PercentInsiders"))
            if merged.get("heldPercentInstitutions") is None:
                merged["heldPercentInstitutions"] = self._as_float(overview.get("PercentInstitutions"))
            if merged.get("fiftyTwoWeekHigh") is None:
                merged["fiftyTwoWeekHigh"] = self._as_float(overview.get("52WeekHigh"))
            if merged.get("fiftyTwoWeekLow") is None:
                merged["fiftyTwoWeekLow"] = self._as_float(overview.get("52WeekLow"))

        alpha_quote = self._alphavantage_get({"function": "GLOBAL_QUOTE", "symbol": symbol})
        if isinstance(alpha_quote, dict):
            gq = alpha_quote.get("Global Quote")
            if isinstance(gq, dict):
                if merged.get("currentPrice") is None:
                    merged["currentPrice"] = self._as_float(gq.get("05. price"))
                if merged.get("regularMarketPrice") is None:
                    merged["regularMarketPrice"] = self._as_float(gq.get("05. price"))
                if merged.get("volume") is None:
                    merged["volume"] = self._as_float(gq.get("06. volume"))
                if merged.get("previousClose") is None:
                    merged["previousClose"] = self._as_float(gq.get("08. previous close"))
                if merged.get("change") is None:
                    merged["change"] = self._as_float(gq.get("09. change"))
                if merged.get("changePercent") is None:
                    merged["changePercent"] = self._text_to_number(gq.get("10. change percent"))

        finnhub_metrics_payload = self._finnhub_get(
            "stock/metric",
            {"symbol": symbol, "metric": "all"},
        )
        if isinstance(finnhub_metrics_payload, dict):
            metrics = finnhub_metrics_payload.get("metric", {})
            if isinstance(metrics, dict):
                if merged.get("fiftyTwoWeekHigh") is None:
                    merged["fiftyTwoWeekHigh"] = self._metric_value(metrics, ["52WeekHigh", "52WeekHighDaily"])
                if merged.get("fiftyTwoWeekLow") is None:
                    merged["fiftyTwoWeekLow"] = self._metric_value(metrics, ["52WeekLow", "52WeekLowDaily"])
                if merged.get("averageVolume") is None:
                    merged["averageVolume"] = self._metric_value(
                        metrics,
                        ["3MonthAverageTradingVolume", "10DayAverageTradingVolume"],
                    )
                if merged.get("beta") is None:
                    merged["beta"] = self._metric_value(metrics, ["beta", "beta1Y"])
                if merged.get("trailingPE") is None:
                    merged["trailingPE"] = self._metric_value(metrics, ["peBasicExclExtraTTM", "peTTM"])
                if merged.get("trailingEps") is None:
                    merged["trailingEps"] = self._metric_value(metrics, ["epsTTM", "epsNormalizedAnnual"])
                if merged.get("shortRatio") is None:
                    merged["shortRatio"] = self._metric_value(metrics, ["shortInterestRatio"])
                if merged.get("shortPercentOfFloat") is None:
                    merged["shortPercentOfFloat"] = self._metric_value(
                        metrics,
                        ["shortFloat", "shortPercentOfFloat"],
                    )
                if merged.get("heldPercentInsiders") is None:
                    merged["heldPercentInsiders"] = self._metric_value(
                        metrics,
                        ["insiderOwnership", "insiderOwnershipPercent"],
                    )
                if merged.get("heldPercentInstitutions") is None:
                    merged["heldPercentInstitutions"] = self._metric_value(
                        metrics,
                        ["institutionalOwnership", "institutionOwnership"],
                    )
                if merged.get("sharesOutstanding") is None:
                    merged["sharesOutstanding"] = self._metric_value(
                        metrics,
                        ["sharesOutstanding", "totalSharesOutstanding"],
                    )

        now_ts = int(time.time())
        daily = self._finnhub_candles(
            symbol=symbol,
            resolution="D",
            from_ts=now_ts - (370 * 86400),
            to_ts=now_ts,
        )
        if not daily.empty:
            if merged.get("averageVolume") is None:
                merged["averageVolume"] = float(daily["Volume"].tail(20).mean())
            if merged.get("volume") is None:
                merged["volume"] = float(daily["Volume"].iloc[-1])
            if merged.get("fiftyTwoWeekHigh") is None:
                merged["fiftyTwoWeekHigh"] = float(daily["High"].max())
            if merged.get("fiftyTwoWeekLow") is None:
                merged["fiftyTwoWeekLow"] = float(daily["Low"].min())

        if include_ownership_fallbacks:
            ownership_summary = self._nasdaq_get(f"company/{symbol}/ownership-summary")
            if isinstance(ownership_summary, dict):
                institutional = ownership_summary.get("institutionalOwnership", {})
                if isinstance(institutional, dict):
                    if merged.get("heldPercentInstitutions") is None:
                        merged["heldPercentInstitutions"] = self._text_to_percent(institutional.get("holdings"))
                    if merged.get("sharesOutstanding") is None:
                        so_millions = self._text_to_number(institutional.get("ShareoutstandingTotal"))
                        if so_millions is not None:
                            merged["sharesOutstanding"] = so_millions * 1_000_000

            short_interest_data = self._nasdaq_get(
                f"quote/{symbol.lower()}/short-interest",
                {"assetclass": "stocks"},
            )
            if isinstance(short_interest_data, dict):
                table = short_interest_data.get("shortInterestTable", {})
                rows = table.get("rows") if isinstance(table, dict) else None
                if isinstance(rows, list) and rows:
                    latest = rows[0] if isinstance(rows[0], dict) else {}
                    if merged.get("shortRatio") is None:
                        ratio_val = self._text_to_number(latest.get("daysToCover"))
                        if self._is_positive_number(ratio_val):
                            merged["shortRatio"] = float(ratio_val)
                    if merged.get("shortPercentOfFloat") is None:
                        short_interest_shares = self._text_to_number(latest.get("interest"))
                        denominator = float_shares or self._as_float(merged.get("sharesOutstanding"))
                        if (
                            short_interest_shares is not None
                            and denominator is not None
                            and float(denominator) > 0
                        ):
                            merged["shortPercentOfFloat"] = float(short_interest_shares) / float(denominator)

            if merged.get("heldPercentInsiders") is None and self._is_positive_number(float_shares):
                shares_outstanding = self._as_float(merged.get("sharesOutstanding"))
                if shares_outstanding and shares_outstanding > 0:
                    est = 1.0 - (float(float_shares) / float(shares_outstanding))
                    if 0 <= est <= 1:
                        merged["heldPercentInsiders"] = est
                        merged["insiderOwnershipEstimated"] = True

        if merged.get("change") is None:
            current_price = self._as_float(merged.get("currentPrice")) or self._as_float(merged.get("regularMarketPrice"))
            prev_close = self._as_float(merged.get("previousClose")) or self._as_float(merged.get("regularMarketPreviousClose"))
            if current_price is not None and prev_close is not None:
                merged["change"] = current_price - prev_close
        if merged.get("changePercent") is None:
            current_price = self._as_float(merged.get("currentPrice")) or self._as_float(merged.get("regularMarketPrice"))
            prev_close = self._as_float(merged.get("previousClose")) or self._as_float(merged.get("regularMarketPreviousClose"))
            if current_price is not None and prev_close not in (None, 0):
                merged["changePercent"] = ((current_price - prev_close) / prev_close) * 100.0

        has_core = any(
            self._is_positive_number(merged.get(k))
            for k in ("marketCap", "averageVolume", "currentPrice", "regularMarketPrice")
        )
        if not has_core:
            merged["dataError"] = "No market data available from Schwab/Finnhub/AlphaVantage."
            if error_msg:
                merged["dataWarning"] = f"yfinance unavailable: {error_msg}"
        elif error_msg:
            merged["dataWarning"] = f"yfinance degraded: {error_msg}"
        if merged.get("insiderOwnershipEstimated"):
            estimate_note = "Insider ownership estimated from Schwab float shares."
            if merged.get("dataWarning"):
                merged["dataWarning"] = f"{merged['dataWarning']} | {estimate_note}"
            else:
                merged["dataWarning"] = estimate_note

        self.cache.set(cache_key, merged)
        return merged

    def intraday_history(self, symbol: str, interval: str, days: int):
        cache_key = f"{symbol.upper()}:{interval}:{int(days)}"
        cached = self._get_cached_intraday(cache_key, allow_stale=False)
        cached = self._clip_to_recent_trading_days(cached, days)
        if not cached.empty:
            return cached

        schwab_frame = self._schwab_intraday(symbol=symbol, interval=interval, days=days)
        schwab_frame = self._clip_to_recent_trading_days(schwab_frame, days)
        if not schwab_frame.empty:
            self._cache_intraday(cache_key, schwab_frame)
            return schwab_frame

        period = f"{max(days, 1)}d"
        if not self._yfinance_disabled:
            try:
                data = yf.download(
                    tickers=symbol,
                    interval=interval,
                    period=period,
                    auto_adjust=False,
                    prepost=True,
                    progress=False,
                    threads=False,
                )
                if data is not None and not data.empty:
                    data = self._clip_to_recent_trading_days(data, days)
                    self._cache_intraday(cache_key, data)
                    return data
            except Exception as exc:
                if self._is_ssl_cert_error(exc):
                    self._disable_yfinance(str(exc))

        # yfinance fallback: pull candles from Finnhub.
        resolution_map = {"5m": "5", "15m": "15", "1m": "1", "30m": "30", "60m": "60"}
        resolution = resolution_map.get(interval, interval.replace("m", ""))
        now_ts = int(time.time())
        from_ts = now_ts - (max(days, 1) * 86400)
        frame = self._finnhub_candles(
            symbol=symbol,
            resolution=resolution,
            from_ts=from_ts,
            to_ts=now_ts,
        )
        frame = self._clip_to_recent_trading_days(frame, days)
        if not frame.empty:
            self._cache_intraday(cache_key, frame)
            return frame

        # AlphaVantage fallback.
        av_interval_map = {"5m": "5min", "15m": "15min", "1m": "1min", "30m": "30min", "60m": "60min"}
        av_interval = av_interval_map.get(interval)
        if av_interval:
            av_frame = self._alphavantage_intraday(symbol=symbol, interval=av_interval)
            av_frame = self._clip_to_recent_trading_days(av_frame, days)
            if not av_frame.empty:
                self._cache_intraday(cache_key, av_frame)
                return av_frame

        # Last resort: return stale cached bars for this symbol/interval if available.
        stale = self._get_cached_intraday(cache_key, allow_stale=True)
        stale = self._clip_to_recent_trading_days(stale, days)
        if not stale.empty:
            return stale

        # Return an empty OHLCV frame so callers can degrade gracefully.
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def fundamentals_snapshot(self, symbol: str, include_ownership_fallbacks: bool = True) -> Dict[str, Any]:
        info = self.ticker_info(symbol, include_ownership_fallbacks=include_ownership_fallbacks)
        market_cap = info.get("marketCap") or info.get("fast_market_cap")
        avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day")
        current_volume = info.get("volume") or info.get("regularMarketVolume") or info.get("fast_last_volume")

        rel_volume = None
        if avg_volume and current_volume is not None and float(avg_volume) > 0:
            rel_volume = float(current_volume) / float(avg_volume)

        return {
            "symbol": symbol,
            "longName": info.get("longName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "marketCap": market_cap,
            "sharesOutstanding": info.get("sharesOutstanding"),
            "beta": info.get("beta"),
            "trailingPE": info.get("trailingPE"),
            "trailingEps": info.get("trailingEps"),
            "revenueTTM": info.get("totalRevenue"),
            "grossMargin": info.get("grossMargins"),
            "operatingMargin": info.get("operatingMargins"),
            "profitMargin": info.get("profitMargins"),
            "insiderOwnership": info.get("heldPercentInsiders"),
            "insiderOwnershipEstimated": info.get("insiderOwnershipEstimated"),
            "institutionalOwnership": info.get("heldPercentInstitutions"),
            "shortFloat": info.get("shortPercentOfFloat"),
            "shortRatio": info.get("shortRatio"),
            "fiftyTwoWeekHigh": info.get("fiftyTwoWeekHigh"),
            "fiftyTwoWeekLow": info.get("fiftyTwoWeekLow"),
            "averageVolume": avg_volume,
            "currentVolume": current_volume,
            "relativeVolume": rel_volume,
            "price": info.get("currentPrice") or info.get("regularMarketPrice") or info.get("fast_last_price"),
            "previousClose": info.get("previousClose") or info.get("regularMarketPreviousClose"),
            "change": info.get("change"),
            "changePercent": info.get("changePercent"),
            "cik": info.get("cik"),
            "exchange": info.get("exchange"),
            "dataError": info.get("dataError"),
            "dataWarning": info.get("dataWarning"),
        }


class FinnhubProvider:
    BASE = "https://finnhub.io/api/v1"

    def __init__(self) -> None:
        self.api_key = SETTINGS.finnhub_api_key
        self.session = requests.Session()
        self.cache = TTLCache(ttl_sec=240)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get(self, endpoint: str, params: Dict[str, Any]) -> Any:
        if not self.enabled:
            return None
        params = {**params, "token": self.api_key}
        key = f"{endpoint}:{sorted(params.items())}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            resp = self.session.get(f"{self.BASE}/{endpoint}", params=params, timeout=10)
            if resp.status_code != 200:
                return None
            payload = resp.json()
            self.cache.set(key, payload)
            return payload
        except Exception:
            return None

    def company_news(self, symbol: str) -> List[Dict[str, Any]]:
        today = dt.date.today()
        frm = today - dt.timedelta(days=7)
        payload = self._get(
            "company-news",
            {"symbol": symbol, "from": frm.isoformat(), "to": today.isoformat()},
        )
        if not isinstance(payload, list):
            return []
        return payload[:10]

    def sentiment(self, symbol: str) -> Dict[str, Any]:
        payload = self._get("news-sentiment", {"symbol": symbol})
        if not isinstance(payload, dict):
            return {}
        return payload

    def next_earnings(self, symbol: str) -> Optional[str]:
        frm = dt.date.today().isoformat()
        to = (dt.date.today() + dt.timedelta(days=120)).isoformat()
        payload = self._get(
            "calendar/earnings",
            {"symbol": symbol, "from": frm, "to": to},
        )
        if not isinstance(payload, dict):
            return None
        earnings = payload.get("earningsCalendar") or []
        if not earnings:
            return None
        next_item = sorted(earnings, key=lambda x: x.get("date", "9999-99-99"))[0]
        return next_item.get("date")


class EdgarProvider:
    BASE = "https://data.sec.gov/submissions"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": SETTINGS.sec_user_agent})
        self.cache = TTLCache(ttl_sec=3600)

    def recent_8k_filings(self, cik: Any, limit: int = 8) -> List[Dict[str, Any]]:
        if not cik:
            return []
        cik_str = str(cik).strip().replace("CIK", "")
        if not cik_str.isdigit():
            return []
        cik10 = cik_str.zfill(10)
        cache_key = f"edgar:{cik10}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached[:limit]

        url = f"{self.BASE}/CIK{cik10}.json"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return []
            payload = resp.json()
        except Exception:
            return []

        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        accession_numbers = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])

        out: List[Dict[str, Any]] = []
        for idx, form in enumerate(forms):
            if str(form).upper() != "8-K":
                continue
            accession = accession_numbers[idx].replace("-", "")
            doc = primary_docs[idx]
            out.append(
                {
                    "form": form,
                    "filingDate": filing_dates[idx],
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik_str)}/{accession}/{doc}",
                }
            )
        self.cache.set(cache_key, out)
        return out[:limit]
