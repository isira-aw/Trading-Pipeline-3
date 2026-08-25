"""Average True Range, for volatility-scaled stop placement.

ATR sizes the stop to how much the instrument actually moves. A fixed
percentage stop is either too tight in a volatile regime (stopped out by
ordinary noise) or too loose in a calm one (a much larger loss than
intended for the same nominal percentage).

Uses **Wilder's** ATR, the original definition: the first value is a simple
average of the first `period` true ranges, and each subsequent value is
smoothed recursively.

    ATR[n-1] = mean(TR[0..n-1])
    ATR[i]   = (ATR[i-1] * (n - 1) + TR[i]) / n

This is deliberately not `ewm(alpha=1/n, adjust=False)`, which seeds from
the first observation rather than the SMA and therefore produces different
numbers early in the series.

This module is exit-side only. It is not part of `risk_engine`, whose rules
govern entry approval and sizing — the stop is a trading decision about an
already-approved position, and the two are kept separate.
"""

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range per candle.

    TR = max(high - low,
             |high - previous close|,
             |low  - previous close|)

    The previous-close terms capture gaps: a market that opens far from the
    last close has moved further than its own high-low span suggests. The
    first row has no previous close, so its TR is just the high-low span.
    """
    high = df["high"].astype("float64")
    low = df["low"].astype("float64")
    previous_close = df["close"].astype("float64").shift(1)

    spans = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    )
    return spans.max(axis=1, skipna=True)


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR over a candle frame sorted oldest-first.

    Returns a Series aligned to `df`, NaN until `period` true ranges exist.
    """
    if period < 1:
        raise ValueError(f"ATR period must be >= 1, got {period}")

    tr = true_range(df)
    atr = pd.Series(index=df.index, dtype="float64")

    if len(tr) < period:
        return atr

    # Seed: simple average of the first `period` true ranges.
    seed = tr.iloc[:period].mean()
    atr.iloc[period - 1] = seed

    # Wilder smoothing thereafter.
    previous = seed
    for i in range(period, len(tr)):
        previous = (previous * (period - 1) + tr.iloc[i]) / period
        atr.iloc[i] = previous

    return atr


def latest_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """ATR of the most recent candle, or None when there is too little data.

    None rather than a fallback number: a stop derived from a guessed
    volatility would be a fabricated risk limit (§1.7).
    """
    if len(df) < period:
        logger.warning(
            "Not enough candles for ATR(%d): have %d.", period, len(df)
        )
        return None

    value = compute_atr(df, period).iloc[-1]
    if pd.isna(value) or value <= 0:
        return None
    return float(value)


def stop_price_for_long(
    entry_price: float, atr_value: float, multiplier: float
) -> float:
    """Stop for a long position: entry - (multiplier x ATR).

    Clamped at zero — a wide multiplier against a volatile instrument can
    arithmetically produce a negative price, which is not a stop.
    """
    return max(0.0, entry_price - multiplier * atr_value)
