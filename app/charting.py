from __future__ import annotations

import json
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .analytics import add_indicators, classify_candle, detect_structure_signals, normalize_ohlcv


def _is_rth(ts: pd.Timestamp) -> bool:
    t = ts.tz_convert("America/New_York") if ts.tzinfo is not None else ts
    minutes = t.hour * 60 + t.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)


def _analyze_candlestick_read(data: pd.DataFrame, candle_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if data.empty:
        return {
            "pattern": "No Data",
            "bias": "neutral",
            "confidence": "low",
            "interpretation": "No candles available to interpret.",
            "recommendation": "Load a symbol/timeframe with intraday bars.",
            "recentCandles": [],
        }

    recent = data.tail(8).copy()
    recent_rows: List[Dict[str, Any]] = []
    candle_type_by_ts = {str(c.get("timestamp")): str(c.get("candleType") or "Unclassified") for c in candle_rows}
    for idx, row in recent.iterrows():
        ts = idx.isoformat()
        recent_rows.append(
            {
                "timestamp": ts,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
                "candleType": candle_type_by_ts.get(ts, "Unclassified"),
            }
        )

    def _parts(c: Dict[str, Any]) -> Dict[str, float]:
        body = abs(c["close"] - c["open"])
        rng = max(c["high"] - c["low"], 1e-9)
        upper = c["high"] - max(c["open"], c["close"])
        lower = min(c["open"], c["close"]) - c["low"]
        return {"body": body, "range": rng, "upper": upper, "lower": lower}

    def _is_bull(c: Dict[str, Any]) -> bool:
        return c["close"] > c["open"]

    def _is_bear(c: Dict[str, Any]) -> bool:
        return c["close"] < c["open"]

    last = recent_rows[-1]
    prev = recent_rows[-2] if len(recent_rows) >= 2 else None
    prev2 = recent_rows[-3] if len(recent_rows) >= 3 else None
    last_parts = _parts(last)
    prev_parts = _parts(prev) if prev else None
    prev2_parts = _parts(prev2) if prev2 else None

    closes = [c["close"] for c in recent_rows]
    close_now = closes[-1]
    close_base = closes[0]
    trend_delta = (close_now - close_base) / close_base if close_base else 0.0
    short_trend = "bullish" if trend_delta > 0.004 else ("bearish" if trend_delta < -0.004 else "sideways")

    bb_upper = float(data["BB_UPPER"].iloc[-1]) if "BB_UPPER" in data.columns else None
    bb_lower = float(data["BB_LOWER"].iloc[-1]) if "BB_LOWER" in data.columns else None
    vwap = float(data["VWAP"].iloc[-1]) if "VWAP" in data.columns else None
    near_upper = bool(bb_upper and close_now >= bb_upper * 0.995)
    near_lower = bool(bb_lower and close_now <= bb_lower * 1.005)
    above_vwap = bool(vwap and close_now >= vwap)

    pattern = "Continuation / No clear reversal"
    bias = "neutral"
    confidence = "medium"
    interpretation = (
        "Recent candles show directional movement but no high-conviction multi-candle reversal pattern."
    )
    recommendation = "Wait for either a confirmed breakout above recent highs or a pullback into support before entering."

    if prev and _is_bull(prev) and _is_bear(last) and last["open"] > prev["close"] and last["close"] < prev["open"]:
        pattern = "Bearish Engulfing"
        bias = "bearish"
        confidence = "high"
        interpretation = "A strong red candle fully engulfed the prior green body, signaling near-term downside pressure."
        recommendation = (
            "Avoid chasing longs immediately; wait for support hold/reclaim or a lower-risk pullback entry."
        )
    elif prev and _is_bear(prev) and _is_bull(last) and last["open"] < prev["close"] and last["close"] > prev["open"]:
        pattern = "Bullish Engulfing"
        bias = "bullish"
        confidence = "high"
        interpretation = "A strong green candle engulfed the prior red body, suggesting buyers are taking control."
        recommendation = "Prefer long setups on continuation above the engulfing candle high with risk defined below its low."
    elif prev and prev2 and prev2_parts and prev_parts:
        if _is_bull(prev2) and prev2_parts["body"] > prev2_parts["range"] * 0.45 and prev_parts["body"] < prev_parts["range"] * 0.3 and _is_bear(last):
            pattern = "Evening Star-style Reversal"
            bias = "bearish"
            confidence = "medium"
            interpretation = "A strong up candle followed by indecision and then a bearish candle points to momentum exhaustion."
            recommendation = "Treat as pullback risk; wait for either trend-support bounce confirmation or deeper reset."
        elif _is_bear(prev2) and prev2_parts["body"] > prev2_parts["range"] * 0.45 and prev_parts["body"] < prev_parts["range"] * 0.3 and _is_bull(last):
            pattern = "Morning Star-style Reversal"
            bias = "bullish"
            confidence = "medium"
            interpretation = "Downside momentum faded and buyers stepped in on the latest candle."
            recommendation = "Look for follow-through above the latest high before committing to a long."
    if pattern == "Continuation / No clear reversal":
        hammer_like = last_parts["lower"] > last_parts["body"] * 1.8 and last_parts["upper"] < last_parts["body"] * 0.9
        shooting_star_like = last_parts["upper"] > last_parts["body"] * 1.8 and last_parts["lower"] < last_parts["body"] * 0.9
        doji_like = last_parts["body"] <= last_parts["range"] * 0.12
        if hammer_like and short_trend in {"bearish", "sideways"}:
            pattern = "Hammer-like Reversal Attempt"
            bias = "bullish"
            confidence = "medium"
            interpretation = "Long lower wick shows buyers defended lower prices late in the candle."
            recommendation = "Wait for confirmation above the hammer high before a long entry."
        elif shooting_star_like and short_trend in {"bullish", "sideways"}:
            pattern = "Shooting Star-like Exhaustion"
            bias = "bearish"
            confidence = "medium"
            interpretation = "Long upper wick indicates rejection near highs and possible short-term exhaustion."
            recommendation = "Avoid chasing highs; consider waiting for a support retest or trend confirmation."
        elif doji_like:
            pattern = "Doji / Indecision"
            bias = "neutral"
            confidence = "low"
            interpretation = "The latest candle closed near its open, showing a balance between buyers and sellers."
            recommendation = "Stand by for directional confirmation from the next 1-2 candles."
        elif short_trend == "bullish":
            pattern = "Bullish Continuation"
            bias = "bullish"
            confidence = "medium"
            interpretation = "The recent candle sequence continues to print higher closes."
            recommendation = "Favor long setups on pullbacks that hold VWAP/fast support."
        elif short_trend == "bearish":
            pattern = "Bearish Continuation"
            bias = "bearish"
            confidence = "medium"
            interpretation = "The recent sequence trends lower with weak close location."
            recommendation = "Favor defensive posture or short-bias only after confirmation."

    if bias == "bullish" and near_upper:
        interpretation += " Price is near the upper Bollinger area, so upside may be less efficient."
    if bias == "bearish" and near_lower:
        interpretation += " Price is near the lower Bollinger area, so downside follow-through may be choppy."
    if vwap is not None:
        interpretation += " Last close is {} VWAP.".format("above" if above_vwap else "below")

    recent_for_ui = []
    for c in recent_rows[-4:]:
        recent_for_ui.append(
            {
                "timestamp": c["timestamp"],
                "candleType": c["candleType"],
                "close": c["close"],
                "changeFromOpenPct": ((c["close"] - c["open"]) / c["open"] * 100.0) if c["open"] else None,
            }
        )

    return {
        "pattern": pattern,
        "bias": bias,
        "confidence": confidence,
        "interpretation": interpretation,
        "recommendation": recommendation,
        "recentCandles": recent_for_ui,
    }


def build_chart_payload(df: pd.DataFrame, symbol: str) -> Dict[str, Any]:
    data = add_indicators(df)
    if data.empty:
        fig = go.Figure()
        fig.update_layout(
            template="plotly_dark",
            title=f"{symbol.upper()} Intraday Price + Volume",
            xaxis={"visible": False},
            yaxis={"visible": False},
            annotations=[
                {
                    "text": "No intraday bars available from active data sources.",
                    "xref": "paper",
                    "yref": "paper",
                    "x": 0.5,
                    "y": 0.5,
                    "showarrow": False,
                    "font": {"size": 14, "color": "#cbd5e1"},
                }
            ],
            margin={"l": 30, "r": 20, "t": 40, "b": 24},
        )
        return {
            "figure": json.loads(fig.to_json()),
            "signals": [],
            "candles": [],
            "candlestickRead": _analyze_candlestick_read(data, []),
        }

    data = data.copy()
    data.index = pd.to_datetime(data.index, utc=True, errors="coerce")
    data = data.dropna(subset=["Open", "High", "Low", "Close"])
    data["is_rth"] = [_is_rth(ts) for ts in data.index]

    hover_lines: List[str] = []
    prev = None
    candle_rows: List[Dict[str, Any]] = []
    for _, row in data.iterrows():
        candle_type, interpretation, action = classify_candle(prev, row)
        prev_context = "No prior candle context."
        if prev is not None:
            prev_type, _, _ = classify_candle(None, prev)
            prev_context = f"Prior candle: {prev_type}."
        hover_lines.append(
            "<br>".join(
                [
                    f"Candle: {candle_type}",
                    f"Interpretation: {interpretation}",
                    f"Action: {action}",
                    prev_context,
                ]
            )
        )
        candle_rows.append(
            {
                "timestamp": row.name.isoformat(),
                "candleType": candle_type,
                "interpretation": interpretation,
                "action": action,
                "isRth": bool(row["is_rth"]),
            }
        )
        prev = row

    rth = data[data["is_rth"]]
    ext = data[~data["is_rth"]]

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
    )

    fig.add_trace(
        go.Candlestick(
            x=rth.index,
            open=rth["Open"],
            high=rth["High"],
            low=rth["Low"],
            close=rth["Close"],
            name="RTH",
            increasing_line_color="#14b8a6",
            decreasing_line_color="#ef4444",
            hovertext=[hover_lines[data.index.get_loc(i)] for i in rth.index],
            hoverinfo="text",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Candlestick(
            x=ext.index,
            open=ext["Open"],
            high=ext["High"],
            low=ext["Low"],
            close=ext["Close"],
            name="Extended",
            increasing_line_color="#64748b",
            decreasing_line_color="#94a3b8",
            opacity=0.55,
            hovertext=[hover_lines[data.index.get_loc(i)] for i in ext.index],
            hoverinfo="text",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=data.index, y=data["BB_UPPER"], mode="lines", name="BB Upper", line={"color": "#f59e0b", "width": 1}),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=data.index, y=data["BB_MID"], mode="lines", name="BB Mid", line={"color": "#eab308", "width": 1}),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=data.index, y=data["BB_LOWER"], mode="lines", name="BB Lower", line={"color": "#f59e0b", "width": 1}),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=data.index, y=data["VWAP"], mode="lines", name="VWAP", line={"color": "#22c55e", "width": 2}),
        row=1,
        col=1,
    )

    volume_colors = np.where(
        data["is_rth"],
        np.where(data["Close"] >= data["Open"], "rgba(20,184,166,0.7)", "rgba(239,68,68,0.7)"),
        "rgba(148,163,184,0.45)",
    )
    fig.add_trace(
        go.Bar(
            x=data.index,
            y=data["Volume"],
            marker_color=volume_colors,
            name="Volume",
        ),
        row=2,
        col=1,
    )

    signals = detect_structure_signals(data)

    fig.update_layout(
        template="plotly_dark",
        title=None,
        margin={"l": 30, "r": 20, "t": 24, "b": 24},
        xaxis_rangeslider_visible=False,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"},
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    return {
        "figure": json.loads(fig.to_json()),
        "signals": [
            {
                "name": s.name,
                "interpretation": s.interpretation,
                "prediction": s.prediction,
            }
            for s in signals
        ],
        "candles": candle_rows,
        "candlestickRead": _analyze_candlestick_read(data, candle_rows),
    }
