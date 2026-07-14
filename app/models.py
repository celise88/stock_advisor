from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ScannerCandidate(BaseModel):
    symbol: str
    market_cap: float
    avg_volume: float
    current_volume: float
    relative_volume: float
    price: Optional[float] = None
    day_change: Optional[float] = None
    day_change_percent: Optional[float] = None
    triggered_at: Optional[datetime] = None
    passed: bool = False
    reasons: List[str] = Field(default_factory=list)


class ScannerSnapshot(BaseModel):
    generated_at: datetime
    interval_sec: int
    market_cap_min: float
    avg_volume_min: float
    relative_volume_min: float
    candidates: List[ScannerCandidate]


class IndicatorValue(BaseModel):
    key: str
    value: Optional[float] = None
    interpretation: str
    explanation: str


class FundamentalsResponse(BaseModel):
    symbol: str
    updated_at: datetime
    fundamentals: Dict[str, Any]
    news: List[Dict[str, Any]]
    filings_8k: List[Dict[str, Any]]
    sentiment: Dict[str, Any]


class TechnicalsResponse(BaseModel):
    symbol: str
    updated_at: datetime
    interval: str
    days: int
    indicators: List[IndicatorValue]
    signals: List[Dict[str, Any]]


class OrderRequest(BaseModel):
    symbol: str
    side: str
    quantity: int
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy: str = "manual"
    dry_run: bool = False


class OutcomeRequest(BaseModel):
    trade_id: str
    exit_price: float
    notes: Optional[str] = ""

