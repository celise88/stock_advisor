from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from .config import SETTINGS
from .models import ScannerCandidate, ScannerSnapshot
from .providers import MarketDataProvider


class ScannerService:
    def __init__(self, market_provider: MarketDataProvider) -> None:
        self.market_provider = market_provider
        self.interval_sec = SETTINGS.scanner_interval_sec
        self.market_cap_min = max(0.0, float(SETTINGS.scanner_market_cap_min))
        self.avg_volume_min = max(0.0, float(SETTINGS.scanner_avg_volume_min))
        self.relative_volume_min = max(0.0, float(SETTINGS.scanner_relative_volume_min))
        self.symbols = SETTINGS.scanner_symbols
        self._lock = threading.Lock()
        self._snapshot = ScannerSnapshot(
            generated_at=datetime.now(timezone.utc),
            interval_sec=self.interval_sec,
            market_cap_min=self.market_cap_min,
            avg_volume_min=self.avg_volume_min,
            relative_volume_min=self.relative_volume_min,
            candidates=[],
        )
        self._trigger_times: dict[str, datetime] = {}
        self._last_passed: dict[str, bool] = {}
        self._thread: threading.Thread | None = None
        self._running = False

    def update_interval(self, interval_sec: int) -> None:
        self.interval_sec = max(10, interval_sec)
        with self._lock:
            self._snapshot.interval_sec = self.interval_sec

    def update_market_cap_min(self, market_cap_min: float) -> None:
        self.market_cap_min = max(0.0, float(market_cap_min))
        with self._lock:
            self._snapshot.market_cap_min = self.market_cap_min

    def update_avg_volume_min(self, avg_volume_min: float) -> None:
        self.avg_volume_min = max(0.0, float(avg_volume_min))
        with self._lock:
            self._snapshot.avg_volume_min = self.avg_volume_min

    def update_relative_volume_min(self, relative_volume_min: float) -> None:
        self.relative_volume_min = max(0.0, float(relative_volume_min))
        with self._lock:
            self._snapshot.relative_volume_min = self.relative_volume_min

    def update_criteria(
        self,
        market_cap_min: Optional[float] = None,
        avg_volume_min: Optional[float] = None,
        relative_volume_min: Optional[float] = None,
    ) -> None:
        if market_cap_min is not None:
            self.market_cap_min = max(0.0, float(market_cap_min))
        if avg_volume_min is not None:
            self.avg_volume_min = max(0.0, float(avg_volume_min))
        if relative_volume_min is not None:
            self.relative_volume_min = max(0.0, float(relative_volume_min))
        with self._lock:
            self._snapshot.market_cap_min = self.market_cap_min
            self._snapshot.avg_volume_min = self.avg_volume_min
            self._snapshot.relative_volume_min = self.relative_volume_min

    @staticmethod
    def _pretty_threshold(value: float) -> str:
        abs_val = abs(float(value))
        if abs_val >= 1_000_000_000:
            return f"{value / 1_000_000_000:g}B"
        if abs_val >= 1_000_000:
            return f"{value / 1_000_000:g}M"
        if abs_val >= 1_000:
            return f"{value / 1_000:g}k"
        return f"{value:g}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def latest(self) -> ScannerSnapshot:
        with self._lock:
            return self._snapshot

    def scan_once(self) -> ScannerSnapshot:
        market_cap_min = self.market_cap_min
        avg_volume_min = self.avg_volume_min
        relative_volume_min = self.relative_volume_min
        now = datetime.now(timezone.utc)
        candidates: List[ScannerCandidate] = []
        for symbol in self.symbols:
            try:
                snap = self.market_provider.fundamentals_snapshot(
                    symbol,
                    include_ownership_fallbacks=False,
                )
                market_cap = float(snap.get("marketCap") or 0.0)
                avg_volume = float(snap.get("averageVolume") or 0.0)
                current_volume = float(snap.get("currentVolume") or 0.0)
                rel_volume = (
                    float(snap.get("relativeVolume"))
                    if snap.get("relativeVolume") is not None
                    else 0.0
                )
                price = float(snap.get("price")) if snap.get("price") is not None else None
                previous_close = (
                    float(snap.get("previousClose"))
                    if snap.get("previousClose") is not None
                    else None
                )
                day_change = (
                    float(snap.get("change"))
                    if snap.get("change") is not None
                    else (price - previous_close if price is not None and previous_close is not None else None)
                )
                day_change_percent = (
                    float(snap.get("changePercent"))
                    if snap.get("changePercent") is not None
                    else (
                        ((price - previous_close) / previous_close) * 100.0
                        if price is not None and previous_close is not None and previous_close != 0
                        else None
                    )
                )

                checks = [
                    (
                        f"market cap > {self._pretty_threshold(market_cap_min)}",
                        market_cap > market_cap_min,
                    ),
                    (
                        f"avg volume > {self._pretty_threshold(avg_volume_min)}",
                        avg_volume > avg_volume_min,
                    ),
                    (f"relative volume > {relative_volume_min:g}", rel_volume > relative_volume_min),
                ]
                passed = all(c[1] for c in checks)
                reasons = [f"{label}: {'PASS' if ok else 'FAIL'}" for label, ok in checks]
                if snap.get("dataError"):
                    reasons.insert(0, f"data source error: {snap.get('dataError')}")
                    passed = False

                was_passed = self._last_passed.get(symbol, False)
                if passed:
                    if not was_passed or symbol not in self._trigger_times:
                        self._trigger_times[symbol] = now
                else:
                    self._trigger_times.pop(symbol, None)
                self._last_passed[symbol] = passed
                triggered_at = self._trigger_times.get(symbol) if passed else None

                candidates.append(
                    ScannerCandidate(
                        symbol=symbol,
                        market_cap=market_cap,
                        avg_volume=avg_volume,
                        current_volume=current_volume,
                        relative_volume=rel_volume,
                        price=price,
                        day_change=day_change,
                        day_change_percent=day_change_percent,
                        triggered_at=triggered_at,
                        passed=passed,
                        reasons=reasons,
                    )
                )
            except Exception as exc:
                self._last_passed[symbol] = False
                self._trigger_times.pop(symbol, None)
                candidates.append(
                    ScannerCandidate(
                        symbol=symbol,
                        market_cap=0.0,
                        avg_volume=0.0,
                        current_volume=0.0,
                        relative_volume=0.0,
                        triggered_at=None,
                        passed=False,
                        reasons=[f"scan error: {exc}"],
                    )
                )

        candidates.sort(
            key=lambda c: (c.passed, c.relative_volume, c.current_volume),
            reverse=True,
        )
        snapshot = ScannerSnapshot(
            generated_at=datetime.now(timezone.utc),
            interval_sec=self.interval_sec,
            market_cap_min=market_cap_min,
            avg_volume_min=avg_volume_min,
            relative_volume_min=relative_volume_min,
            candidates=candidates,
        )
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _run_loop(self) -> None:
        while self._running:
            self.scan_once()
            time.sleep(self.interval_sec)
