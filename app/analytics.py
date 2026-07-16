from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] for c in df.columns]

    required = ["Open", "High", "Low", "Close", "Volume"]
    for col in required:
        if col not in df.columns:
            df[col] = np.nan
    out = df[required].copy()
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out["Volume"] = out["Volume"].fillna(0)
    return out


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    plus_dm = (high.diff()).where((high.diff() > low.diff().abs()) & (high.diff() > 0), 0.0)
    minus_dm = (-low.diff()).where(((-low.diff()) > high.diff().abs()) & ((-low.diff()) > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).sum() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(period).sum() / atr.replace(0, np.nan))
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    return dx.rolling(period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    data = normalize_ohlcv(df)
    if data.empty:
        return data

    data["SMA_20"] = data["Close"].rolling(20).mean()
    data["EMA_20"] = _ema(data["Close"], 20)
    data["EMA_12"] = _ema(data["Close"], 12)
    data["EMA_26"] = _ema(data["Close"], 26)
    data["MACD"] = data["EMA_12"] - data["EMA_26"]
    data["MACD_SIGNAL"] = _ema(data["MACD"], 9)
    data["RSI_14"] = _rsi(data["Close"], 14)

    rolling_std = data["Close"].rolling(20).std()
    data["BB_MID"] = data["SMA_20"]
    data["BB_UPPER"] = data["SMA_20"] + 2 * rolling_std
    data["BB_LOWER"] = data["SMA_20"] - 2 * rolling_std
    data["BB_PCT"] = (
        (data["Close"] - data["BB_LOWER"])
        / (data["BB_UPPER"] - data["BB_LOWER"]).replace(0, np.nan)
    )

    low14 = data["Low"].rolling(14).min()
    high14 = data["High"].rolling(14).max()
    data["STOCH_K"] = 100 * (data["Close"] - low14) / (high14 - low14).replace(0, np.nan)
    data["STOCH_D"] = data["STOCH_K"].rolling(3).mean()

    clv = ((data["Close"] - data["Low"]) - (data["High"] - data["Close"])) / (
        (data["High"] - data["Low"]).replace(0, np.nan)
    )
    data["ADL"] = (clv.fillna(0) * data["Volume"]).cumsum()
    data["CHAIKIN"] = _ema(data["ADL"], 3) - _ema(data["ADL"], 10)

    # Approximate intraday VWAP and rolling VWMA(20).
    typical_price = (data["High"] + data["Low"] + data["Close"]) / 3
    data["VWAP"] = (typical_price * data["Volume"]).cumsum() / data["Volume"].cumsum().replace(0, np.nan)
    data["VWMA_20"] = (
        (data["Close"] * data["Volume"]).rolling(20).sum()
        / data["Volume"].rolling(20).sum().replace(0, np.nan)
    )

    data["ADX_14"] = _adx(data, 14)

    # Lightweight PSAR proxy: EMA-based trend marker suitable for beginner guidance.
    data["PSAR"] = _ema(data["Low"], 5).where(data["Close"] > data["EMA_20"], _ema(data["High"], 5))

    return data


def fibonacci_levels(df: pd.DataFrame, window: int = 120) -> Dict[str, float]:
    data = normalize_ohlcv(df)
    if data.empty:
        return {}
    clipped = data.tail(window)
    hi = float(clipped["High"].max())
    lo = float(clipped["Low"].min())
    diff = hi - lo
    return {
        "0.0": hi,
        "0.236": hi - 0.236 * diff,
        "0.382": hi - 0.382 * diff,
        "0.5": hi - 0.5 * diff,
        "0.618": hi - 0.618 * diff,
        "0.786": hi - 0.786 * diff,
        "1.0": lo,
    }


def classify_candle(prev_row: pd.Series | None, row: pd.Series) -> Tuple[str, str, str]:
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    body = abs(c - o)
    rng = max(h - l, 1e-9)
    upper = h - max(c, o)
    lower = min(c, o) - l

    if body <= rng * 0.1:
        name = "Doji"
        interpretation = "Indecision between buyers and sellers."
        action = "Wait for confirmation on the next candle before entering."
    elif lower >= body * 2 and upper <= body:
        name = "Hammer" if c >= o else "Hanging Man"
        interpretation = "Buyers rejected lower prices intrabar."
        action = "Bias long if the next candle confirms with a higher high."
    elif upper >= body * 2 and lower <= body:
        name = "Shooting Star" if c <= o else "Inverted Hammer"
        interpretation = "Sellers defended higher prices intrabar."
        action = "Avoid fresh longs unless the next candle invalidates this move."
    elif c > o and body >= rng * 0.75:
        name = "Bullish Marubozu"
        interpretation = "Strong directional buying pressure."
        action = "Look for continuation entries near VWAP pullbacks."
    elif c < o and body >= rng * 0.75:
        name = "Bearish Marubozu"
        interpretation = "Strong directional selling pressure."
        action = "Favor short setups or avoid catching a falling knife."
    else:
        name = "Neutral Body"
        interpretation = "No dominant single-candle edge."
        action = "Use structure and volume context before deciding."

    if prev_row is not None:
        po, pc = float(prev_row["Open"]), float(prev_row["Close"])
        if c > o and pc < po and c >= po and o <= pc:
            name = "Bullish Engulfing"
            interpretation = "Current candle fully engulfs prior bearish body."
            action = "Long bias if volume and trend support continuation."
        elif c < o and pc > po and o >= pc and c <= po:
            name = "Bearish Engulfing"
            interpretation = "Current candle fully engulfs prior bullish body."
            action = "Reduce long exposure and look for downside continuation."

    return name, interpretation, action


@dataclass
class Signal:
    name: str
    interpretation: str
    prediction: str


def _fit_line(points: List[Tuple[int, float]]) -> Tuple[float, float]:
    if len(points) < 2:
        return 0.0, 0.0
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope), float(intercept)


def _swing_points(data: pd.DataFrame, window: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    if len(data) < (window * 2 + 1):
        return highs, lows

    high_values = data["High"].values
    low_values = data["Low"].values
    for idx in range(window, len(data) - window):
        high_slice = high_values[idx - window : idx + window + 1]
        low_slice = low_values[idx - window : idx + window + 1]
        if high_values[idx] >= np.max(high_slice):
            highs.append((idx, float(high_values[idx])))
        if low_values[idx] <= np.min(low_slice):
            lows.append((idx, float(low_values[idx])))
    return highs, lows


def _between_count(values: List[float], target: float, tolerance: float) -> int:
    return sum(1 for v in values if abs(v - target) <= tolerance)


def _dedupe_signals(signals: List[Signal]) -> List[Signal]:
    seen = set()
    out: List[Signal] = []
    for signal in signals:
        if signal.name in seen:
            continue
        seen.add(signal.name)
        out.append(signal)
    return out


def detect_structure_signals(df: pd.DataFrame) -> List[Signal]:
    data = normalize_ohlcv(df).tail(180)
    if len(data) < 30:
        return []

    close = float(data["Close"].iloc[-1])
    atr = float((data["High"] - data["Low"]).rolling(14).mean().iloc[-1] or 0.0)
    if not np.isfinite(atr) or atr <= 0:
        atr = max(close * 0.004, 0.1)

    # Use swing points for cleaner trendline math.
    swing_highs, swing_lows = _swing_points(data, window=3)
    high_fit_points = swing_highs[-6:] if len(swing_highs) >= 3 else list(
        zip(np.arange(len(data) - 30, len(data)), data["High"].tail(30).astype(float).tolist())
    )
    low_fit_points = swing_lows[-6:] if len(swing_lows) >= 3 else list(
        zip(np.arange(len(data) - 30, len(data)), data["Low"].tail(30).astype(float).tolist())
    )

    hi_slope, hi_intercept = _fit_line(high_fit_points)
    lo_slope, lo_intercept = _fit_line(low_fit_points)

    slope_eps = max(atr / 80.0, close * 0.0002)
    flat_eps = max(atr * 0.35, close * 0.0012)
    trend_touch_eps = max(atr * 0.55, close * 0.0025)
    last_idx = len(data) - 1
    projected_hi = hi_intercept + hi_slope * last_idx
    projected_lo = lo_intercept + lo_slope * last_idx

    recent_hi = float(data["High"].rolling(20).max().iloc[-1])
    recent_lo = float(data["Low"].rolling(20).min().iloc[-1])

    signals: List[Signal] = []
    # Trendline support/resistance tests.
    if np.isfinite(projected_lo) and abs(close - projected_lo) <= trend_touch_eps:
        signals.append(
            Signal(
                name="Trendline Supporting",
                interpretation="Price is interacting with the active lower trendline support.",
                prediction="Look for bullish confirmation candles before entering long.",
            )
        )
    if np.isfinite(projected_hi) and abs(projected_hi - close) <= trend_touch_eps:
        signals.append(
            Signal(
                name="Trendline Resisting",
                interpretation="Price is testing the active upper trendline resistance.",
                prediction="Avoid chasing upside until resistance clearly breaks.",
            )
        )

    # Horizontal zones.
    if recent_lo > 0 and abs(close - recent_lo) / recent_lo <= 0.01:
        signals.append(
            Signal(
                name="Horizontal S/R (Support)",
                interpretation="Price is near a recent horizontal support shelf.",
                prediction="Bounce setups improve if volume confirms support defense.",
            )
        )
    if recent_hi > 0 and abs(recent_hi - close) / recent_hi <= 0.01:
        signals.append(
            Signal(
                name="Horizontal S/R (Resistance)",
                interpretation="Price is near a recent horizontal resistance shelf.",
                prediction="Expect rejection risk until resistance is decisively cleared.",
            )
        )

    # Regime shape from trendline slopes.
    converging = (hi_slope - lo_slope) < -slope_eps
    parallel = abs(hi_slope - lo_slope) <= slope_eps

    if parallel and hi_slope > slope_eps and lo_slope > slope_eps:
        signals.append(
            Signal(
                name="Channel Up",
                interpretation="Upper and lower trendlines rise in parallel.",
                prediction="Favor pullback longs while lower channel support holds.",
            )
        )
    elif parallel and hi_slope < -slope_eps and lo_slope < -slope_eps:
        signals.append(
            Signal(
                name="Channel Down",
                interpretation="Upper and lower trendlines decline in parallel.",
                prediction="Favor short rebounds while upper channel resistance holds.",
            )
        )
    elif parallel:
        signals.append(
            Signal(
                name="Channel",
                interpretation="Price is oscillating inside a mostly parallel structure.",
                prediction="Prefer range tactics until a clean breakout occurs.",
            )
        )

    if converging and hi_slope > slope_eps and lo_slope > slope_eps:
        signals.append(
            Signal(
                name="Wedge Up",
                interpretation="Price rises while the channel narrows, often losing momentum.",
                prediction="Watch for bearish breakdown if lower wedge support fails.",
            )
        )
    elif converging and hi_slope < -slope_eps and lo_slope < -slope_eps:
        signals.append(
            Signal(
                name="Wedge Down",
                interpretation="Price falls while the range compresses into a downward wedge.",
                prediction="Watch for bullish reversal if upper wedge resistance breaks.",
            )
        )

    if abs(hi_slope) <= slope_eps and lo_slope > slope_eps:
        signals.append(
            Signal(
                name="Triangle Ascending",
                interpretation="Flat resistance with rising lows indicates buyer pressure.",
                prediction="Bullish bias if price breaks above the flat resistance line.",
            )
        )
    elif abs(lo_slope) <= slope_eps and hi_slope < -slope_eps:
        signals.append(
            Signal(
                name="Triangle Descending",
                interpretation="Flat support with falling highs indicates seller pressure.",
                prediction="Bearish bias if price breaks below the flat support line.",
            )
        )
    elif hi_slope < -slope_eps and lo_slope > slope_eps:
        signals.append(
            Signal(
                name="Triangle",
                interpretation="Converging highs and lows indicate compression before expansion.",
                prediction="Wait for breakout direction confirmation before committing size.",
            )
        )

    # Double/multiple top/bottom from swing clusters.
    recent_high_values = [value for _, value in swing_highs[-8:]]
    recent_low_values = [value for _, value in swing_lows[-8:]]
    if recent_high_values:
        top_level = max(recent_high_values)
        top_count = _between_count(recent_high_values, top_level, flat_eps)
        if top_count >= 3:
            signals.append(
                Signal(
                    name="Multiple Top",
                    interpretation="Repeated failures near the same high indicate supply overhead.",
                    prediction="Bearish bias increases if support/neckline breaks.",
                )
            )
        elif top_count == 2:
            signals.append(
                Signal(
                    name="Double Top",
                    interpretation="Two comparable peaks suggest upside exhaustion risk.",
                    prediction="Cautious/short bias if neckline support breaks.",
                )
            )

    if recent_low_values:
        bottom_level = min(recent_low_values)
        bottom_count = _between_count(recent_low_values, bottom_level, flat_eps)
        if bottom_count >= 3:
            signals.append(
                Signal(
                    name="Multiple Bottom",
                    interpretation="Repeated defenses near the same low indicate accumulation.",
                    prediction="Bullish bias increases if neckline resistance breaks.",
                )
            )
        elif bottom_count == 2:
            signals.append(
                Signal(
                    name="Double Bottom",
                    interpretation="Two comparable lows suggest downside exhaustion risk.",
                    prediction="Bullish bias if neckline breaks with momentum.",
                )
            )

    # Head-and-shoulders approximation using last three swing highs.
    if len(swing_highs) >= 3:
        (_, left_shoulder), (_, head), (_, right_shoulder) = swing_highs[-3:]
        shoulders_close = abs(left_shoulder - right_shoulder) <= flat_eps * 1.2
        head_dominant = head > left_shoulder + flat_eps and head > right_shoulder + flat_eps
        if shoulders_close and head_dominant:
            signals.append(
                Signal(
                    name="Head and Shoulders",
                    interpretation="Middle swing high exceeds both shoulders, suggesting distribution.",
                    prediction="Bearish confirmation strengthens if neckline support fails.",
                )
            )

    return _dedupe_signals(signals)[:8]


def _as_valid_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    except Exception:
        return None


def _indicator_row(key: str, value: Any, interpretation: str, explanation: str) -> Dict[str, Any]:
    numeric_value = None
    try:
        if value is not None and not (isinstance(value, float) and math.isnan(value)):
            numeric_value = float(value)
    except Exception:
        numeric_value = None
    return {
        "key": key,
        "value": numeric_value,
        "interpretation": interpretation,
        "explanation": explanation,
    }


def _tick_rule_signs(close: pd.Series) -> pd.Series:
    diff = close.diff()
    signs = np.sign(diff).replace(0, np.nan).ffill().fillna(0.0)
    return signs.astype(float)


def _vpin_proxy(volume: pd.Series, signs: pd.Series, bucket_count: int = 24) -> Optional[float]:
    if volume.empty or signs.empty or len(volume) < 25:
        return None

    vol = pd.to_numeric(volume, errors="coerce").fillna(0.0).astype(float)
    sgn = pd.to_numeric(signs, errors="coerce").fillna(0.0).astype(float)
    bucket_size = float(max(vol.tail(min(len(vol), 50)).mean() * 4.0, 1.0))
    if bucket_size <= 0:
        return None

    imbalances: List[float] = []
    bucket_buy = 0.0
    bucket_sell = 0.0
    bucket_fill = 0.0

    for bar_volume, bar_sign in zip(vol.tolist(), sgn.tolist()):
        if bar_volume <= 0:
            continue
        buy_remaining = bar_volume if bar_sign > 0 else (0.0 if bar_sign < 0 else bar_volume * 0.5)
        sell_remaining = bar_volume - buy_remaining
        volume_remaining = bar_volume

        while volume_remaining > 1e-9:
            capacity = max(bucket_size - bucket_fill, 0.0)
            if capacity <= 1e-9:
                imbalance = abs(bucket_buy - bucket_sell) / bucket_size
                imbalances.append(float(min(max(imbalance, 0.0), 1.0)))
                bucket_buy = 0.0
                bucket_sell = 0.0
                bucket_fill = 0.0
                continue

            take = min(volume_remaining, capacity)
            ratio = take / volume_remaining if volume_remaining > 0 else 0.0
            buy_take = buy_remaining * ratio
            sell_take = sell_remaining * ratio

            bucket_buy += buy_take
            bucket_sell += sell_take
            bucket_fill += take

            buy_remaining -= buy_take
            sell_remaining -= sell_take
            volume_remaining -= take

    if bucket_fill > 0:
        imbalance = abs(bucket_buy - bucket_sell) / max(bucket_fill, 1.0)
        imbalances.append(float(min(max(imbalance, 0.0), 1.0)))

    if not imbalances:
        return None
    lookback = min(bucket_count, len(imbalances))
    return float(np.mean(imbalances[-lookback:]))


def _auction_pressure_proxies(data: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
    if data.empty:
        return None, None
    try:
        work = data.copy()
        work["__ts"] = pd.to_datetime(work.index, utc=True, errors="coerce")
        work = work.dropna(subset=["__ts"])
        if work.empty:
            return None, None

        et = work["__ts"].dt.tz_convert("America/New_York")
        work["__date"] = et.dt.date
        work["__minutes"] = et.dt.hour * 60 + et.dt.minute
        latest_date = work["__date"].iloc[-1]

        day = work[work["__date"] == latest_date].copy()
        if day.empty:
            return None, None
        rth = day[(day["__minutes"] >= 9 * 60 + 30) & (day["__minutes"] <= 16 * 60)]
        if len(rth) < 3:
            return None, None
        premarket = day[(day["__minutes"] >= 4 * 60) & (day["__minutes"] < 9 * 60 + 30)]

        open_bar = rth.iloc[0]
        previous_dates = sorted([d for d in set(work["__date"].tolist()) if d < latest_date])
        previous_close = None
        if previous_dates:
            prev_day = work[work["__date"] == previous_dates[-1]]
            prev_rth = prev_day[(prev_day["__minutes"] >= 9 * 60 + 30) & (prev_day["__minutes"] <= 16 * 60)]
            if not prev_rth.empty:
                previous_close = _as_valid_float(prev_rth["Close"].iloc[-1])
        if previous_close in (None, 0):
            previous_close = _as_valid_float(premarket["Close"].iloc[-1]) if not premarket.empty else _as_valid_float(open_bar["Open"])

        open_close = _as_valid_float(open_bar["Close"])
        open_ref = previous_close
        open_move_pct = ((open_close - open_ref) / open_ref * 100.0) if open_close is not None and open_ref not in (None, 0) else 0.0
        pre_volume_ref = (
            float(premarket["Volume"].mean())
            if not premarket.empty and pd.notna(premarket["Volume"].mean())
            else float(rth["Volume"].head(min(5, len(rth))).mean())
        )
        pre_volume_ref = max(pre_volume_ref, 1.0)
        open_vol_ratio = float(open_bar.get("Volume", 0.0) or 0.0) / pre_volume_ref
        open_range = max(float(open_bar.get("High", 0.0) or 0.0) - float(open_bar.get("Low", 0.0) or 0.0), 1e-9)
        open_clv = ((float(open_bar["Close"]) - float(open_bar["Low"])) - (float(open_bar["High"]) - float(open_bar["Close"]))) / open_range
        open_score = float(np.clip(math.tanh(open_move_pct / 1.5) * min(open_vol_ratio / 2.5, 2.0) + 0.25 * open_clv, -2.0, 2.0))

        close_bar = rth.iloc[-1]
        middle = rth.iloc[max(1, len(rth) // 3) : max(2, (2 * len(rth)) // 3)]
        mid_vol_ref = float(middle["Volume"].mean()) if not middle.empty else float(rth["Volume"].mean())
        mid_vol_ref = max(mid_vol_ref, 1.0)
        close_vol_ratio = float(close_bar.get("Volume", 0.0) or 0.0) / mid_vol_ref
        close_range = max(float(close_bar.get("High", 0.0) or 0.0) - float(close_bar.get("Low", 0.0) or 0.0), 1e-9)
        close_clv = ((float(close_bar["Close"]) - float(close_bar["Low"])) - (float(close_bar["High"]) - float(close_bar["Close"]))) / close_range
        close_score = float(np.clip(close_clv * min(close_vol_ratio / 2.0, 2.0), -2.0, 2.0))

        return open_score, close_score
    except Exception:
        return None, None


def _compute_advanced_flow_metrics(
    data: pd.DataFrame,
    market_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if data.empty:
        return {
            "ofi_ratio": None,
            "vpin": None,
            "aggressor_imbalance": None,
            "opening_auction_score": None,
            "closing_auction_score": None,
            "stream_available": False,
            "stream_note": "",
            "stream_ofi_used": False,
            "stream_vpin_used": False,
            "stream_aggr_used": False,
        }

    volume = pd.to_numeric(data.get("Volume"), errors="coerce").fillna(0.0).astype(float)
    close = pd.to_numeric(data.get("Close"), errors="coerce").astype(float)
    high = pd.to_numeric(data.get("High"), errors="coerce").astype(float)
    low = pd.to_numeric(data.get("Low"), errors="coerce").astype(float)

    signs = _tick_rule_signs(close)
    signed_volume = signs * volume
    window = min(30, len(data))
    total_window_vol = float(volume.tail(window).sum())
    aggressor_imbalance = None
    if total_window_vol > 0:
        aggressor_imbalance = float(signed_volume.tail(window).sum() / total_window_vol)

    spread_proxy = (high - low).abs()
    min_spread = close.abs() * 0.0005
    spread_proxy = spread_proxy.where(spread_proxy > min_spread, min_spread).fillna(min_spread).fillna(0.01)
    bid = close - spread_proxy / 2.0
    ask = close + spread_proxy / 2.0
    buy_ratio = ((signs + 1.0) / 2.0).clip(lower=0.0, upper=1.0)
    q_bid = volume * buy_ratio
    q_ask = volume * (1.0 - buy_ratio)
    bid_prev = bid.shift(1)
    ask_prev = ask.shift(1)
    q_bid_prev = q_bid.shift(1)
    q_ask_prev = q_ask.shift(1)
    event_flow = (
        (bid >= bid_prev).astype(float) * q_bid
        - (bid <= bid_prev).astype(float) * q_bid_prev
        - (ask <= ask_prev).astype(float) * q_ask
        + (ask >= ask_prev).astype(float) * q_ask_prev
    ).fillna(0.0)
    ofi_window = min(40, len(event_flow))
    ofi_total = float(event_flow.tail(ofi_window).sum())
    ofi_den = float(volume.tail(ofi_window).sum())
    ofi_ratio = (ofi_total / ofi_den) if ofi_den > 0 else None

    vpin = _vpin_proxy(volume, signs, bucket_count=24)
    opening_auction_score, closing_auction_score = _auction_pressure_proxies(data)

    streaming_ctx = market_context.get("streaming", {}) if isinstance(market_context, dict) else {}
    stream_available = isinstance(streaming_ctx, dict) and bool(streaming_ctx.get("available"))
    stream_ofi = _as_valid_float(streaming_ctx.get("ofi")) if stream_available else None
    stream_aggr = _as_valid_float(streaming_ctx.get("aggressorImbalance")) if stream_available else None
    stream_vpin = _as_valid_float(streaming_ctx.get("vpin")) if stream_available else None
    stream_note = str(streaming_ctx.get("note") or "") if isinstance(streaming_ctx, dict) else ""

    if stream_ofi is not None:
        ofi_ratio = float(np.clip(stream_ofi, -1.0, 1.0))
    if stream_aggr is not None:
        aggressor_imbalance = float(np.clip(stream_aggr, -1.0, 1.0))
    if stream_vpin is not None:
        vpin = float(np.clip(stream_vpin, 0.0, 1.0))

    return {
        "ofi_ratio": ofi_ratio,
        "vpin": vpin,
        "aggressor_imbalance": aggressor_imbalance,
        "opening_auction_score": opening_auction_score,
        "closing_auction_score": closing_auction_score,
        "stream_available": stream_available,
        "stream_note": stream_note,
        "stream_ofi_used": stream_ofi is not None,
        "stream_vpin_used": stream_vpin is not None,
        "stream_aggr_used": stream_aggr is not None,
    }


def _advanced_indicator_rows(data: pd.DataFrame, market_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if data.empty:
        return []
    metrics = _compute_advanced_flow_metrics(data, market_context=market_context)
    ofi_ratio = metrics["ofi_ratio"]
    vpin = metrics["vpin"]
    aggressor_imbalance = metrics["aggressor_imbalance"]
    opening_auction_score = metrics["opening_auction_score"]
    closing_auction_score = metrics["closing_auction_score"]
    stream_available = bool(metrics["stream_available"])
    stream_note = str(metrics["stream_note"] or "")
    stream_ofi_used = bool(metrics["stream_ofi_used"])
    stream_vpin_used = bool(metrics["stream_vpin_used"])
    stream_aggr_used = bool(metrics["stream_aggr_used"])

    rows: List[Dict[str, Any]] = []

    if ofi_ratio is None:
        ofi_interp = "OFI unavailable."
        ofi_expl = "Not enough reliable bars to estimate order-book pressure."
    elif ofi_ratio >= 0.12:
        ofi_interp = "Buy-side order-flow imbalance is strong."
        ofi_expl = (
            "Cont-Kukanov-Stoikov style OFI is positive; prefer long setups "
            "on pullbacks instead of chasing tops, and avoid fighting the tape."
        )
    elif ofi_ratio <= -0.12:
        ofi_interp = "Sell-side order-flow imbalance is strong."
        ofi_expl = (
            "OFI is negative; prioritize short setups or stay flat until "
            "price reclaims VWAP with volume."
        )
    else:
        ofi_interp = "Order flow is balanced to mixed."
        ofi_expl = "No clear OFI edge; reduce size and wait for a cleaner directional signal."
    if stream_ofi_used:
        ofi_expl = f"{ofi_expl} Source: live Schwab stream order book."
    rows.append(_indicator_row("Order Flow Imbalance (OFI)", None if ofi_ratio is None else ofi_ratio * 100.0, ofi_interp, ofi_expl))

    if vpin is None:
        vpin_interp = "VPIN unavailable."
        vpin_expl = "Not enough volume buckets to estimate informed-flow risk."
    elif vpin >= 0.65:
        vpin_interp = "High toxicity regime (VPIN elevated)."
        vpin_expl = (
            "High VPIN suggests informed flow may dominate; trade smaller, wait for confirmation, "
            "and avoid impulsive entries in noisy spikes."
        )
    elif vpin >= 0.45:
        vpin_interp = "Moderate toxicity regime."
        vpin_expl = "Conditions are tradeable but fragile; tighten stops and require cleaner setups."
    else:
        vpin_interp = "Lower toxicity regime."
        vpin_expl = "Flow looks more two-sided; standard risk is more reasonable if trend context agrees."
    if stream_vpin_used:
        vpin_expl = f"{vpin_expl} Source: real-time signed volume from Schwab stream."
    rows.append(_indicator_row("VPIN (Volume-Synchronized PIN)", vpin, vpin_interp, vpin_expl))

    if aggressor_imbalance is None:
        aggr_interp = "Aggressor imbalance unavailable."
        aggr_expl = "Insufficient signed-volume data to estimate who is lifting/hitting."
    elif aggressor_imbalance >= 0.15:
        aggr_interp = "Aggressive buyers are in control."
        aggr_expl = "Tick-rule signing favors buyer-initiated flow; prefer buying pullbacks over shorting strength."
    elif aggressor_imbalance <= -0.15:
        aggr_interp = "Aggressive sellers are in control."
        aggr_expl = "Tick-rule signing favors seller-initiated flow; avoid bottom-fishing until pressure fades."
    else:
        aggr_interp = "Aggressor flow is roughly balanced."
        aggr_expl = "No dominant side; wait for breakout plus volume confirmation."
    if stream_aggr_used:
        aggr_expl = f"{aggr_expl} Source: live quote tape via Schwab stream."
    rows.append(
        _indicator_row(
            "Aggressor Imbalance (Tick Rule, %)",
            None if aggressor_imbalance is None else aggressor_imbalance * 100.0,
            aggr_interp,
            aggr_expl,
        )
    )

    if opening_auction_score is None:
        open_interp = "Opening auction pressure unavailable."
        open_expl = "Opening cross data was insufficient; using standard intraday signals only."
    elif opening_auction_score >= 0.5:
        open_interp = "Opening auction proxy favors buy imbalance."
        open_expl = "Strong open with supportive volume; look for first pullback long if VWAP holds."
    elif opening_auction_score <= -0.5:
        open_interp = "Opening auction proxy favors sell imbalance."
        open_expl = "Weak open with heavy pressure; avoid early longs until reclaim/absorption appears."
    else:
        open_interp = "Opening auction proxy is neutral."
        open_expl = "Opening cross looked balanced; let the first trend leg form before committing."
    rows.append(_indicator_row("Auction Imbalance (Open Proxy)", opening_auction_score, open_interp, open_expl))

    if closing_auction_score is None:
        close_interp = "Closing auction pressure unavailable."
        close_expl = "Closing cross data was insufficient for a reliable read."
    elif closing_auction_score >= 0.5:
        close_interp = "Closing auction proxy shows buy-side imbalance."
        close_expl = "Late-session buyers dominated; bullish overnight continuation odds improve if no bad catalyst."
    elif closing_auction_score <= -0.5:
        close_interp = "Closing auction proxy shows sell-side imbalance."
        close_expl = "Late-session sellers dominated; expect cautious next-open tone unless futures reverse."
    else:
        close_interp = "Closing auction proxy is balanced."
        close_expl = "No strong late imbalance; rely more on broader trend and premarket context."
    rows.append(_indicator_row("Auction Imbalance (Close Proxy)", closing_auction_score, close_interp, close_expl))

    if stream_available and stream_note:
        rows.append(
            _indicator_row(
                "Stream Data Status",
                None,
                "Streaming microstructure feed active.",
                stream_note,
            )
        )

    cross_asset = market_context.get("crossAsset", {}) if isinstance(market_context, dict) else {}
    if isinstance(cross_asset, dict) and cross_asset.get("available"):
        delta = _as_valid_float(cross_asset.get("leadDeltaPct"))
        symbol_move = _as_valid_float(cross_asset.get("symbolPremarketMovePct"))
        composite = _as_valid_float(cross_asset.get("compositeLeadMovePct"))
        lead_state = str(cross_asset.get("leadState") or "aligned")
        if lead_state == "lagging_upside":
            x_interp = "Cross-asset leadership is bullish; symbol is lagging."
            x_expl = "ES/NQ + sector are stronger than this name; watch for catch-up long only after intraday confirmation."
        elif lead_state == "lagging_downside":
            x_interp = "Cross-asset leadership is bearish; symbol is resisting."
            x_expl = "Futures/sector are weaker than this name; avoid forcing shorts until relative weakness appears."
        elif lead_state == "leading":
            x_interp = "Symbol is leading cross-asset tone."
            x_expl = "Name is moving ahead of futures/sector; momentum setups can work, but use tighter risk in case leadership fades."
        else:
            x_interp = "Symbol is aligned with cross-asset tone."
            x_expl = "Premarket move is consistent with ES/NQ/sector direction; prioritize setups in the same direction."
        if symbol_move is not None and composite is not None:
            x_expl = (
                f"{x_expl} Premarket: symbol {symbol_move:+.2f}% vs cross-asset composite {composite:+.2f}%."
            )
        rows.append(_indicator_row("Cross-Asset Leadership Delta (%)", delta, x_interp, x_expl))
    else:
        rows.append(
            _indicator_row(
                "Cross-Asset Leadership Delta (%)",
                None,
                "Cross-asset leadership unavailable.",
                "Could not fetch enough ES/NQ/sector premarket data for a reliable leadership read.",
            )
        )

    options_ctx = market_context.get("options", {}) if isinstance(market_context, dict) else {}
    if isinstance(options_ctx, dict) and options_ctx.get("available"):
        gex = _as_valid_float(options_ctx.get("gexMillions"))
        skew = _as_valid_float(options_ctx.get("putCallSkew"))
        flow_ratio = _as_valid_float(options_ctx.get("unusualFlowRatio"))
        flow_side = str(options_ctx.get("unusualFlowSide") or "").lower()
        put_call_oi_ratio = _as_valid_float(options_ctx.get("putCallOiRatio"))

        if gex is None:
            gex_interp = "GEX unavailable."
            gex_expl = "Could not compute aggregate gamma exposure from current chain snapshot."
        elif gex >= 0:
            gex_interp = "Net gamma exposure is positive."
            gex_expl = (
                "Positive GEX often dampens intraday volatility; mean-reversion setups and tighter targets can work better."
            )
        else:
            gex_interp = "Net gamma exposure is negative."
            gex_expl = (
                "Negative GEX can amplify directional moves; favor trend-following and avoid averaging into losers."
            )
        rows.append(_indicator_row("Options Gamma Exposure (GEX, $MM)", gex, gex_interp, gex_expl))

        if skew is None:
            skew_interp = "Put/Call skew unavailable."
            skew_expl = "Could not compare OTM put IV vs OTM call IV from current options chain."
        elif skew >= 0.03:
            skew_interp = "Put skew is elevated."
            skew_expl = (
                "Downside hedging demand is high; treat long setups cautiously and insist on stronger confirmation."
            )
        elif skew <= -0.03:
            skew_interp = "Call skew is elevated."
            skew_expl = (
                "Upside optionality demand is stronger; bullish continuation setups gain tailwind if price confirms."
            )
        else:
            skew_interp = "Volatility skew is near neutral."
            skew_expl = "Options market is not strongly tilted; rely more on price/volume than options skew."
        if put_call_oi_ratio is not None:
            skew_expl = f"{skew_expl} Put/Call OI ratio: {put_call_oi_ratio:.2f}."
        rows.append(_indicator_row("Options Put/Call Skew", skew, skew_interp, skew_expl))

        if flow_ratio is None or not flow_side:
            flow_interp = "No unusual options flow flagged."
            flow_expl = "No high volume/open-interest outlier contract in the current chain snapshot."
        elif flow_ratio >= 3.0:
            flow_interp = f"Unusual {flow_side} flow detected."
            flow_expl = (
                "Volume is multiple times open interest (sweep proxy); monitor that direction, "
                "but wait for price confirmation before entering."
            )
        else:
            flow_interp = "Options flow is active but not extreme."
            flow_expl = "Flow is noteworthy but not a clear sweep-style outlier; treat it as secondary confirmation only."
        contract_text = options_ctx.get("unusualFlowContract")
        if contract_text:
            flow_expl = f"{flow_expl} Most active outlier contract: {contract_text}."
        rows.append(_indicator_row("Unusual Sweep Activity (Proxy)", flow_ratio, flow_interp, flow_expl))
    else:
        note = options_ctx.get("note") if isinstance(options_ctx, dict) else None
        rows.extend(
            [
                _indicator_row(
                    "Options Gamma Exposure (GEX, $MM)",
                    None,
                    "Options-derived signal unavailable.",
                    str(note or "No options chain feed available for this symbol."),
                ),
                _indicator_row(
                    "Options Put/Call Skew",
                    None,
                    "Options-derived signal unavailable.",
                    str(note or "No options chain feed available for this symbol."),
                ),
                _indicator_row(
                    "Unusual Sweep Activity (Proxy)",
                    None,
                    "Options-derived signal unavailable.",
                    str(note or "No options chain feed available for this symbol."),
                ),
            ]
        )

    return rows


def _session_change_context(data: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
    if data.empty:
        return None, None

    try:
        work = data.copy()
        work["__ts"] = pd.to_datetime(work.index, utc=True, errors="coerce")
        work = work.dropna(subset=["__ts"])
        if work.empty:
            return None, None

        et = work["__ts"].dt.tz_convert("America/New_York")
        latest_date = et.iloc[-1].date()
        same_day = work[et.dt.date == latest_date].copy()
        if same_day.empty:
            return None, None

        et_day = same_day["__ts"].dt.tz_convert("America/New_York")
        minutes = et_day.dt.hour * 60 + et_day.dt.minute
        rth = same_day[(minutes >= (9 * 60 + 30)) & (minutes <= (16 * 60))]
        session = rth if len(rth) >= 2 else same_day
        if session.empty:
            return None, None

        start_ref = _as_valid_float(session["Open"].iloc[0]) or _as_valid_float(session["Close"].iloc[0])
        end_ref = _as_valid_float(session["Close"].iloc[-1])
        if start_ref in (None, 0) or end_ref is None:
            return None, None

        dollar_change = end_ref - start_ref
        percent_change = (dollar_change / start_ref) * 100.0
        return dollar_change, percent_change
    except Exception:
        return None, None


def _build_holistic_rows(data: pd.DataFrame, market_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if data.empty:
        return []

    last = data.iloc[-1]
    close = _as_valid_float(last.get("Close")) or 0.0
    sma20 = _as_valid_float(last.get("SMA_20"))
    ema20 = _as_valid_float(last.get("EMA_20"))
    vwap = _as_valid_float(last.get("VWAP"))
    psar = _as_valid_float(last.get("PSAR"))
    macd = _as_valid_float(last.get("MACD"))
    macd_signal = _as_valid_float(last.get("MACD_SIGNAL"))
    chaikin = _as_valid_float(last.get("CHAIKIN"))
    rsi = _as_valid_float(last.get("RSI_14"))
    stoch_k = _as_valid_float(last.get("STOCH_K"))
    bb_pct = _as_valid_float(last.get("BB_PCT"))
    adx = _as_valid_float(last.get("ADX_14"))
    session_change_dollar, session_change_pct = _session_change_context(data)

    bull = 0
    bear = 0
    total_votes = 0

    def _vote(condition: Optional[bool]) -> None:
        nonlocal bull, bear, total_votes
        if condition is None:
            return
        total_votes += 1
        if condition:
            bull += 1
        else:
            bear += 1

    _vote(close > sma20 if sma20 is not None else None)
    _vote(close > ema20 if ema20 is not None else None)
    _vote(close > vwap if vwap is not None else None)
    _vote(close > psar if psar is not None else None)
    _vote(macd > macd_signal if macd is not None and macd_signal is not None else None)
    _vote(chaikin > 0 if chaikin is not None else None)

    advanced_context_notes: List[str] = []
    advanced_conflicts: List[str] = []
    advanced_metrics = _compute_advanced_flow_metrics(data, market_context=market_context)

    ofi_ratio = _as_valid_float(advanced_metrics.get("ofi_ratio"))
    aggressor_imbalance = _as_valid_float(advanced_metrics.get("aggressor_imbalance"))
    vpin = _as_valid_float(advanced_metrics.get("vpin"))
    open_auction = _as_valid_float(advanced_metrics.get("opening_auction_score"))
    close_auction = _as_valid_float(advanced_metrics.get("closing_auction_score"))

    if ofi_ratio is not None:
        if ofi_ratio >= 0.10:
            _vote(True)
            advanced_context_notes.append("OFI supports buyers")
        elif ofi_ratio <= -0.10:
            _vote(False)
            advanced_context_notes.append("OFI supports sellers")

    if aggressor_imbalance is not None:
        if aggressor_imbalance >= 0.12:
            _vote(True)
            advanced_context_notes.append("aggressor flow favors buyers")
        elif aggressor_imbalance <= -0.12:
            _vote(False)
            advanced_context_notes.append("aggressor flow favors sellers")

    if open_auction is not None:
        if open_auction >= 0.6:
            _vote(True)
            advanced_context_notes.append("opening auction pressure is buy-side")
        elif open_auction <= -0.6:
            _vote(False)
            advanced_context_notes.append("opening auction pressure is sell-side")

    if close_auction is not None:
        if close_auction >= 0.6:
            advanced_context_notes.append("closing auction indicates buy-side continuation risk")
        elif close_auction <= -0.6:
            advanced_context_notes.append("closing auction indicates sell-side continuation risk")

    if vpin is not None and vpin >= 0.65:
        advanced_conflicts.append("VPIN is elevated (flow toxicity risk)")

    cross_asset = market_context.get("crossAsset", {}) if isinstance(market_context, dict) else {}
    cross_asset_available = False
    cross_asset_lead_state: Optional[str] = None
    cross_asset_composite: Optional[float] = None
    if isinstance(cross_asset, dict) and cross_asset.get("available"):
        cross_asset_available = True
        cross_asset_lead_state = str(cross_asset.get("leadState") or "aligned")
        cross_asset_composite = _as_valid_float(cross_asset.get("compositeLeadMovePct"))
        lead_state = cross_asset_lead_state
        composite = cross_asset_composite
        if lead_state in {"aligned", "leading"} and composite is not None:
            if composite >= 0.25:
                _vote(True)
                advanced_context_notes.append("cross-asset leadership is bullish")
            elif composite <= -0.25:
                _vote(False)
                advanced_context_notes.append("cross-asset leadership is bearish")
        elif lead_state == "lagging_upside":
            advanced_conflicts.append("cross-asset tone is bullish but symbol is lagging")
        elif lead_state == "lagging_downside":
            advanced_conflicts.append("cross-asset tone is bearish but symbol is resisting")

    options_ctx = market_context.get("options", {}) if isinstance(market_context, dict) else {}
    options_available = False
    gex = None
    skew = None
    flow_side = ""
    flow_ratio = None
    put_call_oi_ratio = None
    if isinstance(options_ctx, dict) and options_ctx.get("available"):
        options_available = True
        gex = _as_valid_float(options_ctx.get("gexMillions"))
        skew = _as_valid_float(options_ctx.get("putCallSkew"))
        flow_side = str(options_ctx.get("unusualFlowSide") or "").lower()
        flow_ratio = _as_valid_float(options_ctx.get("unusualFlowRatio"))
        put_call_oi_ratio = _as_valid_float(options_ctx.get("putCallOiRatio"))

        if skew is not None:
            if skew >= 0.03:
                _vote(False)
                advanced_context_notes.append("put skew is elevated")
            elif skew <= -0.03:
                _vote(True)
                advanced_context_notes.append("call skew is elevated")

        if flow_ratio is not None and flow_ratio >= 3.0 and flow_side in {"call", "put"}:
            _vote(flow_side == "call")
            advanced_context_notes.append(f"unusual {flow_side} flow is present")

        if put_call_oi_ratio is not None:
            if put_call_oi_ratio >= 1.2:
                _vote(False)
            elif put_call_oi_ratio <= 0.8:
                _vote(True)

        if gex is not None and gex < 0:
            advanced_conflicts.append("negative GEX can amplify directional volatility")

    bias_delta = bull - bear
    if bias_delta >= 2:
        bias = "bullish"
    elif bias_delta <= -2:
        bias = "bearish"
    else:
        bias = "mixed"

    if adx is None:
        regime = "unclear regime"
    elif adx >= 30:
        regime = "strong trend"
    elif adx >= 20:
        regime = "developing trend"
    else:
        regime = "range/chop"

    conflicts: List[str] = []
    if bias == "bullish":
        if rsi is not None and rsi >= 70:
            conflicts.append("RSI is overbought")
        if stoch_k is not None and stoch_k >= 80:
            conflicts.append("Stochastic is overbought")
        if bb_pct is not None and bb_pct >= 0.9:
            conflicts.append("price is near upper Bollinger band")
    elif bias == "bearish":
        if rsi is not None and rsi <= 30:
            conflicts.append("RSI is oversold")
        if stoch_k is not None and stoch_k <= 20:
            conflicts.append("Stochastic is oversold")
        if bb_pct is not None and bb_pct <= 0.1:
            conflicts.append("price is near lower Bollinger band")

    if regime == "strong trend":
        if bias == "bullish":
            regime_guidance = (
                "Strong trend filter: prioritize trend-following signals "
                "(price vs EMA/VWAP, MACD, PSAR). Oscillator overbought readings "
                "can persist and are lower-priority reversal signals."
            )
        elif bias == "bearish":
            regime_guidance = (
                "Strong trend filter: prioritize trend-following bearish signals. "
                "Oscillator oversold readings can persist and are lower-priority "
                "for reversal entries."
            )
        else:
            regime_guidance = (
                "Trend strength is elevated but direction is mixed; reduce size and "
                "wait for cleaner alignment before acting."
            )
    elif regime == "developing trend":
        regime_guidance = (
            "Developing-trend filter: weigh trend-following signals slightly more "
            "than oscillators, but still require confirmation before full-size entries."
        )
    elif regime == "range/chop":
        regime_guidance = (
            "Range/chop filter: oscillators (RSI, Stochastic, Bollinger %B) are "
            "more useful for entries/exits, while breakout signals need stronger confirmation."
        )
    else:
        regime_guidance = "Regime is unclear; reduce size and wait for cleaner structure."

    alignment_pct = (max(bull, bear) / total_votes * 100.0) if total_votes else 0.0
    if alignment_pct >= 80:
        conviction_tier = 2
    elif alignment_pct >= 65:
        conviction_tier = 1
    else:
        conviction_tier = 0

    volume_ratio: Optional[float] = None
    volume_context = "Relative volume context unavailable."
    try:
        vol = data["Volume"].dropna().astype(float)
        # Compare the most recent 5 bars vs the prior 20 bars for a smoother intraday relative-volume read.
        if len(vol) >= 25:
            recent_vol = float(vol.tail(5).mean())
            prior_vol = float(vol.tail(25).head(20).mean())
            if prior_vol > 0:
                volume_ratio = recent_vol / prior_vol
    except Exception:
        volume_ratio = None

    if volume_ratio is not None:
        if volume_ratio >= 1.5:
            conviction_tier = min(2, conviction_tier + 1)
            volume_context = (
                f"Relative volume is elevated ({volume_ratio:.2f}x recent baseline), "
                "so conviction is upgraded."
            )
        elif volume_ratio <= 0.75:
            conviction_tier = max(0, conviction_tier - 1)
            volume_context = (
                f"Relative volume is muted ({volume_ratio:.2f}x recent baseline), "
                "so conviction is reduced."
            )
        else:
            volume_context = (
                f"Relative volume is neutral ({volume_ratio:.2f}x recent baseline), "
                "so conviction is unchanged."
            )

    if vpin is not None and vpin >= 0.65:
        conviction_tier = max(0, conviction_tier - 1)
    if gex is not None and gex < 0:
        conviction_tier = max(0, conviction_tier - 1)
    flow_alignment_bonus = 0
    if ofi_ratio is not None and aggressor_imbalance is not None:
        if ofi_ratio >= 0.10 and aggressor_imbalance >= 0.12:
            flow_alignment_bonus = 1
        elif ofi_ratio <= -0.10 and aggressor_imbalance <= -0.12:
            flow_alignment_bonus = 1
    if flow_alignment_bonus:
        conviction_tier = min(2, conviction_tier + flow_alignment_bonus)

    conviction = ("low conviction", "moderate conviction", "high conviction")[conviction_tier]

    all_conflicts = conflicts + advanced_conflicts
    conflict_text = (
        "No major indicator conflicts detected."
        if not all_conflicts
        else "Conflict watch: " + "; ".join(all_conflicts[:4]) + "."
    )
    advanced_context_text = (
        " Advanced context: " + "; ".join(advanced_context_notes[:4]) + "."
        if advanced_context_notes
        else ""
    )

    overall_interpretation = (
        f"{regime.title()} with {bias} bias ({conviction})."
        if bias != "mixed"
        else f"{regime.title()} with mixed signals ({conviction})."
    )

    session_text = ""
    session_conflict_text = ""
    if session_change_pct is not None and session_change_dollar is not None:
        session_text = f" Day performance: {session_change_pct:+.2f}% ({session_change_dollar:+.2f}) from session open."
        if bias == "bullish" and session_change_pct < -0.75:
            session_conflict_text = (
                " Broader day context is still negative, so treat bullish alignment as a possible rebound until structure confirms."
            )
        elif bias == "bearish" and session_change_pct > 0.75:
            session_conflict_text = (
                " Broader day context is still positive, so treat bearish alignment as a pullback unless downside follow-through appears."
            )

    overall_interpretation = f"{overall_interpretation}{session_text}".strip()
    overall_explanation = (
        f"Alignment: {bull} bullish vs {bear} bearish trend/momentum votes. "
        f"{conflict_text}{advanced_context_text} {regime_guidance} {volume_context}{session_conflict_text}"
    )

    if regime == "strong trend":
        if bias == "bullish":
            action_interpretation = "Bullish Bias: favor pullback longs with trend."
            action_explanation = (
                "Prioritize entries that hold above EMA/VWAP. "
                "Avoid aggressive counter-trend shorts unless trend structure breaks."
            )
        elif bias == "bearish":
            action_interpretation = "Bearish Bias: favor short setups with trend."
            action_explanation = (
                "Prioritize short entries on weak bounces below EMA/VWAP. "
                "Avoid bottom-fishing until bearish structure weakens."
            )
        else:
            action_interpretation = "Neutral Bias: trend is strong but signals conflict."
            action_explanation = "Wait for clearer directional alignment before committing full size."
    elif regime == "developing trend":
        if bias == "bullish":
            action_interpretation = "Bullish Bias: trend is building upward."
            action_explanation = "Favor long setups, but demand confirmation because trend strength is still developing."
        elif bias == "bearish":
            action_interpretation = "Bearish Bias: downside trend is building."
            action_explanation = "Favor short setups, but use tighter risk until trend strength expands."
        else:
            action_interpretation = "Neutral Bias: early trend phase with mixed confirmation."
            action_explanation = "Keep size smaller and wait for follow-through before directional bets."
    elif regime == "range/chop":
        if bias == "bullish":
            action_interpretation = "Slight Bullish Bias: range conditions favor tactical longs."
            action_explanation = "Use oscillator extremes for timing and take profits faster inside the range."
        elif bias == "bearish":
            action_interpretation = "Slight Bearish Bias: range conditions favor tactical shorts."
            action_explanation = "Use oscillator extremes for timing and take profits faster inside the range."
        else:
            action_interpretation = "Neutral Bias: choppy range regime."
            action_explanation = "Prefer patience or small mean-reversion trades near clear support/resistance."
    else:
        action_interpretation = "Neutral Bias: regime unclear."
        action_explanation = "Wait for clearer structure before acting aggressively."

    if bias == "mixed" or regime == "unclear regime":
        risk_mode = "Defensive"
    elif all_conflicts:
        risk_mode = "Defensive"
    elif conviction == "high conviction" and regime in {"strong trend", "developing trend"} and bias in {
        "bullish",
        "bearish",
    }:
        risk_mode = "Aggressive"
    elif conviction == "low conviction":
        risk_mode = "Defensive"
    elif regime == "range/chop":
        risk_mode = "Standard" if conviction == "high conviction" else "Defensive"
    else:
        risk_mode = "Standard"

    priority_drivers: List[str] = []
    key_risks: List[str] = []

    def _add_unique(items: List[str], text: str) -> None:
        if text and text not in items:
            items.append(text)

    if bias == "bullish":
        if ofi_ratio is not None and aggressor_imbalance is not None and ofi_ratio >= 0.10 and aggressor_imbalance >= 0.12:
            _add_unique(priority_drivers, "OFI and aggressor flow both favor buyers")
        elif ofi_ratio is not None and ofi_ratio >= 0.10:
            _add_unique(priority_drivers, "OFI favors buyers")
        elif aggressor_imbalance is not None and aggressor_imbalance >= 0.12:
            _add_unique(priority_drivers, "aggressor flow favors buyers")

        if cross_asset_available and cross_asset_composite is not None and cross_asset_composite >= 0.25 and cross_asset_lead_state in {"aligned", "leading"}:
            _add_unique(priority_drivers, "cross-asset leadership is supportive")
        if options_available and skew is not None and skew <= -0.03:
            _add_unique(priority_drivers, "call skew supports upside")
        if options_available and flow_ratio is not None and flow_ratio >= 3.0 and flow_side == "call":
            _add_unique(priority_drivers, "unusual call flow supports continuation")

        if vpin is not None and vpin >= 0.65:
            _add_unique(key_risks, "VPIN is elevated")
        if gex is not None and gex < 0:
            _add_unique(key_risks, "negative GEX can amplify volatility")
        if cross_asset_lead_state == "lagging_upside":
            _add_unique(key_risks, "symbol is lagging a bullish cross-asset tape")
        if options_available and skew is not None and skew >= 0.03:
            _add_unique(key_risks, "put skew reflects downside hedge demand")
    elif bias == "bearish":
        if ofi_ratio is not None and aggressor_imbalance is not None and ofi_ratio <= -0.10 and aggressor_imbalance <= -0.12:
            _add_unique(priority_drivers, "OFI and aggressor flow both favor sellers")
        elif ofi_ratio is not None and ofi_ratio <= -0.10:
            _add_unique(priority_drivers, "OFI favors sellers")
        elif aggressor_imbalance is not None and aggressor_imbalance <= -0.12:
            _add_unique(priority_drivers, "aggressor flow favors sellers")

        if cross_asset_available and cross_asset_composite is not None and cross_asset_composite <= -0.25 and cross_asset_lead_state in {"aligned", "leading"}:
            _add_unique(priority_drivers, "cross-asset leadership is risk-off")
        if options_available and skew is not None and skew >= 0.03:
            _add_unique(priority_drivers, "put skew supports downside caution")
        if options_available and flow_ratio is not None and flow_ratio >= 3.0 and flow_side == "put":
            _add_unique(priority_drivers, "unusual put flow supports downside")

        if vpin is not None and vpin >= 0.65:
            _add_unique(key_risks, "VPIN is elevated")
        if gex is not None and gex < 0:
            _add_unique(key_risks, "negative GEX can increase downside swings")
        if cross_asset_lead_state == "lagging_downside":
            _add_unique(key_risks, "symbol is resisting a bearish cross-asset tape")
        if options_available and skew is not None and skew <= -0.03:
            _add_unique(key_risks, "call skew may reduce downside follow-through")
    else:
        if advanced_context_notes:
            _add_unique(priority_drivers, "; ".join(advanced_context_notes[:2]))
        if advanced_conflicts:
            _add_unique(key_risks, "; ".join(advanced_conflicts[:2]))

    if priority_drivers:
        action_explanation = f"{action_explanation} Primary drivers: {'; '.join(priority_drivers[:3])}."
    if key_risks:
        action_explanation = f"{action_explanation} Key risk: {'; '.join(key_risks[:2])}."

    action_interpretation = f"{action_interpretation} [Risk Mode: {risk_mode}]"

    return [
        {
            "key": "Action Bias",
            "value": float(bias_delta),
            "interpretation": action_interpretation,
            "explanation": action_explanation,
        },
        {
            "key": "Holistic Read",
            "value": alignment_pct,
            "interpretation": overall_interpretation,
            "explanation": overall_explanation,
        },
        {
            "key": "Regime Filter (ADX)",
            "value": adx,
            "interpretation": regime.title() if regime else "Regime unavailable.",
            "explanation": regime_guidance,
        },
    ]


def build_indicator_summary(data: pd.DataFrame, market_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if data.empty:
        return []
    last = data.iloc[-1]
    close = float(last["Close"])
    rsi = float(last.get("RSI_14") or np.nan)
    macd = float(last.get("MACD") or np.nan)
    macd_signal = float(last.get("MACD_SIGNAL") or np.nan)
    adx = float(last.get("ADX_14") or np.nan)

    rows = [
        _indicator_row(
            "SMA (20)",
            last.get("SMA_20"),
            "Bullish bias above SMA." if close > (last.get("SMA_20") or close) else "Bearish bias below SMA.",
            "Simple Moving Average smooths recent price to show trend direction.",
        ),
        _indicator_row(
            "EMA (20)",
            last.get("EMA_20"),
            "Price above EMA can signal near-term strength." if close > (last.get("EMA_20") or close) else "Price below EMA can signal near-term weakness.",
            "Exponential Moving Average reacts faster than SMA to recent changes.",
        ),
        _indicator_row(
            "MACD",
            macd,
            "Momentum improving (MACD > signal)." if macd > macd_signal else "Momentum fading (MACD < signal).",
            "MACD tracks momentum by comparing short and long EMAs.",
        ),
        _indicator_row(
            "RSI (14)",
            rsi,
            "Overbought risk." if rsi >= 70 else ("Oversold bounce zone." if rsi <= 30 else "Neutral momentum."),
            "RSI measures recent up/down velocity on a 0-100 scale.",
        ),
        _indicator_row(
            "Bollinger %B",
            last.get("BB_PCT"),
            "Upper-band pressure." if (last.get("BB_PCT") or 0) >= 0.8 else ("Lower-band pressure." if (last.get("BB_PCT") or 0) <= 0.2 else "Mid-band equilibrium."),
            "Bollinger Bands estimate volatility and where price sits in that range.",
        ),
        _indicator_row(
            "Stochastic %K",
            last.get("STOCH_K"),
            "Momentum stretched high." if (last.get("STOCH_K") or 0) > 80 else ("Momentum stretched low." if (last.get("STOCH_K") or 0) < 20 else "Momentum balanced."),
            "Stochastic compares close to recent high/low range.",
        ),
        _indicator_row(
            "Accum/Dist (ADL)",
            last.get("ADL"),
            "Buying pressure rising." if (last.get("CHAIKIN") or 0) > 0 else "Selling pressure rising.",
            "Accumulation/Distribution approximates whether volume confirms buying or selling.",
        ),
        _indicator_row(
            "Chaikin Oscillator",
            last.get("CHAIKIN"),
            "Positive money-flow momentum." if (last.get("CHAIKIN") or 0) > 0 else "Negative money-flow momentum.",
            "Chaikin is a momentum view of accumulation/distribution flow.",
        ),
        _indicator_row(
            "Parabolic SAR (proxy)",
            last.get("PSAR"),
            "Bullish trend support." if close > (last.get("PSAR") or close) else "Bearish trend pressure.",
            "Parabolic SAR estimates likely trailing stop direction.",
        ),
        _indicator_row(
            "VWMA (20)",
            last.get("VWMA_20"),
            "Price leading weighted trend." if close > (last.get("VWMA_20") or close) else "Price below weighted trend.",
            "VWMA weights price by volume to highlight conviction.",
        ),
        _indicator_row(
            "ADX (14)",
            adx,
            "Strong trend regime." if adx >= 25 else "Weak/sideways trend regime.",
            "ADX measures trend strength, not direction.",
        ),
        _indicator_row(
            "VWAP",
            last.get("VWAP"),
            "Above VWAP favors long continuation." if close > (last.get("VWAP") or close) else "Below VWAP favors cautious or short bias.",
            "VWAP is the session's volume-weighted fair-price benchmark.",
        ),
    ]
    return _build_holistic_rows(data, market_context=market_context) + _advanced_indicator_rows(data, market_context=market_context) + rows


def fibonacci_indicator_rows(data: pd.DataFrame, fib: Dict[str, float]) -> List[Dict[str, Any]]:
    if data.empty or not fib:
        return [
            {
                "key": "Fibonacci (0.382)",
                "value": None,
                "interpretation": "Insufficient data.",
                "explanation": "Not enough valid bars to compute Fibonacci levels.",
            },
            {
                "key": "Fibonacci (0.618)",
                "value": None,
                "interpretation": "Insufficient data.",
                "explanation": "Not enough valid bars to compute Fibonacci levels.",
            },
        ]

    close = float(data["Close"].iloc[-1])
    ema20_now = float(data["EMA_20"].iloc[-1]) if "EMA_20" in data.columns and pd.notna(data["EMA_20"].iloc[-1]) else close
    ema_series = data["EMA_20"].dropna() if "EMA_20" in data.columns else pd.Series(dtype=float)
    ema20_prev = float(ema_series.iloc[-6]) if len(ema_series) >= 6 else ema20_now

    if close > ema20_now and ema20_now >= ema20_prev:
        trend = "uptrend"
    elif close < ema20_now and ema20_now <= ema20_prev:
        trend = "downtrend"
    else:
        trend = "range"

    recent_high = float(data["High"].tail(30).max())
    recent_low = float(data["Low"].tail(30).min())
    level_distance_threshold_pct = 0.8

    def _fib_row(level_key: str, label: str) -> Dict[str, Any]:
        level_value = fib.get(level_key)
        if level_value is None:
            return {
                "key": label,
                "value": None,
                "interpretation": "Level unavailable.",
                "explanation": "Fibonacci level could not be computed from current bars.",
            }

        level = float(level_value)
        if level <= 0:
            return {
                "key": label,
                "value": level,
                "interpretation": "Level unavailable.",
                "explanation": "Computed level was invalid for interpretation.",
            }

        dist_pct = ((close - level) / level) * 100.0
        abs_dist_pct = abs(dist_pct)
        near_level = abs_dist_pct <= level_distance_threshold_pct
        position = "above" if close >= level else "below"

        if trend == "uptrend":
            if close >= level and near_level:
                interpretation = f"Uptrend holding near {label} support."
                explanation = (
                    f"Price is {abs_dist_pct:.2f}% above {label}; a clean hold favors continuation toward {recent_high:.2f}."
                )
            elif close >= level:
                interpretation = f"Uptrend remains intact above {label}."
                explanation = (
                    f"Price is {abs_dist_pct:.2f}% above {label}; pullbacks that stay above this level keep bulls in control."
                )
            else:
                interpretation = f"Uptrend is weakening below {label}."
                explanation = (
                    f"Price is {abs_dist_pct:.2f}% below {label}; reclaiming this level is needed to restore bullish momentum."
                )
        elif trend == "downtrend":
            if close <= level and near_level:
                interpretation = f"Downtrend pressing near {label} resistance."
                explanation = (
                    f"Price is {abs_dist_pct:.2f}% below {label}; repeated rejection here supports continuation toward {recent_low:.2f}."
                )
            elif close <= level:
                interpretation = f"Downtrend remains intact below {label}."
                explanation = (
                    f"Price is {abs_dist_pct:.2f}% below {label}; rallies into this level are still lower-probability long entries."
                )
            else:
                interpretation = f"Downtrend is softening above {label}."
                explanation = (
                    f"Price is {abs_dist_pct:.2f}% above {label}; sustained hold above this level weakens bearish control."
                )
        else:
            if near_level:
                interpretation = f"Price is balancing at {label}."
                explanation = (
                    f"Range conditions: price sits within {abs_dist_pct:.2f}% of {label}, so breakout confirmation is more important than prediction."
                )
            else:
                interpretation = f"Price is trading {position} {label} in a range."
                explanation = (
                    f"Without clear trend direction, use {label} as a reaction level and wait for volume-backed break/hold."
                )

        return {
            "key": label,
            "value": level,
            "interpretation": interpretation,
            "explanation": explanation,
        }

    return [
        _fib_row("0.382", "Fibonacci (0.382)"),
        _fib_row("0.618", "Fibonacci (0.618)"),
    ]
