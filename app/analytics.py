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


def _build_holistic_rows(data: pd.DataFrame) -> List[Dict[str, Any]]:
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

    conviction = ("low conviction", "moderate conviction", "high conviction")[conviction_tier]

    conflict_text = (
        "No major indicator conflicts detected."
        if not conflicts
        else "Conflict watch: " + "; ".join(conflicts[:3]) + "."
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
        f"{conflict_text} {regime_guidance} {volume_context}{session_conflict_text}"
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
    elif conflicts:
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


def build_indicator_summary(data: pd.DataFrame) -> List[Dict[str, Any]]:
    if data.empty:
        return []
    last = data.iloc[-1]
    close = float(last["Close"])
    rsi = float(last.get("RSI_14") or np.nan)
    macd = float(last.get("MACD") or np.nan)
    macd_signal = float(last.get("MACD_SIGNAL") or np.nan)
    adx = float(last.get("ADX_14") or np.nan)

    def _row(key: str, value: Any, interpretation: str, explanation: str) -> Dict[str, Any]:
        v = None
        try:
            if value is not None and not (isinstance(value, float) and math.isnan(value)):
                v = float(value)
        except Exception:
            v = None
        return {
            "key": key,
            "value": v,
            "interpretation": interpretation,
            "explanation": explanation,
        }

    rows = [
        _row(
            "SMA (20)",
            last.get("SMA_20"),
            "Bullish bias above SMA." if close > (last.get("SMA_20") or close) else "Bearish bias below SMA.",
            "Simple Moving Average smooths recent price to show trend direction.",
        ),
        _row(
            "EMA (20)",
            last.get("EMA_20"),
            "Price above EMA can signal near-term strength." if close > (last.get("EMA_20") or close) else "Price below EMA can signal near-term weakness.",
            "Exponential Moving Average reacts faster than SMA to recent changes.",
        ),
        _row(
            "MACD",
            macd,
            "Momentum improving (MACD > signal)." if macd > macd_signal else "Momentum fading (MACD < signal).",
            "MACD tracks momentum by comparing short and long EMAs.",
        ),
        _row(
            "RSI (14)",
            rsi,
            "Overbought risk." if rsi >= 70 else ("Oversold bounce zone." if rsi <= 30 else "Neutral momentum."),
            "RSI measures recent up/down velocity on a 0-100 scale.",
        ),
        _row(
            "Bollinger %B",
            last.get("BB_PCT"),
            "Upper-band pressure." if (last.get("BB_PCT") or 0) >= 0.8 else ("Lower-band pressure." if (last.get("BB_PCT") or 0) <= 0.2 else "Mid-band equilibrium."),
            "Bollinger Bands estimate volatility and where price sits in that range.",
        ),
        _row(
            "Stochastic %K",
            last.get("STOCH_K"),
            "Momentum stretched high." if (last.get("STOCH_K") or 0) > 80 else ("Momentum stretched low." if (last.get("STOCH_K") or 0) < 20 else "Momentum balanced."),
            "Stochastic compares close to recent high/low range.",
        ),
        _row(
            "Accum/Dist (ADL)",
            last.get("ADL"),
            "Buying pressure rising." if (last.get("CHAIKIN") or 0) > 0 else "Selling pressure rising.",
            "Accumulation/Distribution approximates whether volume confirms buying or selling.",
        ),
        _row(
            "Chaikin Oscillator",
            last.get("CHAIKIN"),
            "Positive money-flow momentum." if (last.get("CHAIKIN") or 0) > 0 else "Negative money-flow momentum.",
            "Chaikin is a momentum view of accumulation/distribution flow.",
        ),
        _row(
            "Parabolic SAR (proxy)",
            last.get("PSAR"),
            "Bullish trend support." if close > (last.get("PSAR") or close) else "Bearish trend pressure.",
            "Parabolic SAR estimates likely trailing stop direction.",
        ),
        _row(
            "VWMA (20)",
            last.get("VWMA_20"),
            "Price leading weighted trend." if close > (last.get("VWMA_20") or close) else "Price below weighted trend.",
            "VWMA weights price by volume to highlight conviction.",
        ),
        _row(
            "ADX (14)",
            adx,
            "Strong trend regime." if adx >= 25 else "Weak/sideways trend regime.",
            "ADX measures trend strength, not direction.",
        ),
        _row(
            "VWAP",
            last.get("VWAP"),
            "Above VWAP favors long continuation." if close > (last.get("VWAP") or close) else "Below VWAP favors cautious or short bias.",
            "VWAP is the session's volume-weighted fair-price benchmark.",
        ),
    ]
    return _build_holistic_rows(data) + rows


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
