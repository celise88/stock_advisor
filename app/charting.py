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
        return {"figure": json.loads(fig.to_json()), "signals": [], "candles": []}

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
    }
