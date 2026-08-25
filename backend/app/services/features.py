"""Feature engineering for the baseline model (§5.1).

Indicators are all causal: the value at row *t* is computed only from
candles at or before *t*. That property is what makes a plain time-ordered
train/test split safe — see `training_pipeline.build_dataset`.

The target is deliberately the only forward-looking column, and the rows
where it cannot be known yet are dropped rather than filled.
"""

import numpy as np
import pandas as pd
from pandas.api.indexers import FixedForwardWindowIndexer

# Column order is fixed so a stored model's feature vector always lines up
# with what inference builds later.
FEATURE_COLUMNS = [
    "sma_20_ratio",
    "sma_50_ratio",
    "sma_100_ratio",
    "ema_20_ratio",
    "ema_50_ratio",
    "ema_100_ratio",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_width",
    "bb_position",
    "volume_delta",
    "return_1",
    "return_4",
    "high_low_range",
]

TARGET_COLUMN = "target"

# Longest lookback (SMA/EMA 100) — rows before this have no valid features.
MIN_CANDLES_FOR_FEATURES = 100


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder smoothing == EWM with alpha = 1/period.
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # A window with no losses is maximally overbought, not undefined.
    return rsi.where(avg_loss != 0.0, 100.0)


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicator columns to a candle frame sorted by open_time.

    Moving averages are expressed as a *ratio* to close rather than a raw
    price level, so the features stay comparable across the wide price
    ranges a 2-year window covers (and across symbols).
    """
    out = df.copy()
    close = out["close"]

    for period in (20, 50, 100):
        sma = close.rolling(window=period, min_periods=period).mean()
        ema = close.ewm(span=period, min_periods=period, adjust=False).mean()
        out[f"sma_{period}_ratio"] = close / sma - 1.0
        out[f"ema_{period}_ratio"] = close / ema - 1.0

    out["rsi_14"] = _rsi(close, 14)

    ema_12 = close.ewm(span=12, min_periods=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, min_periods=26, adjust=False).mean()
    macd = ema_12 - ema_26
    macd_signal = macd.ewm(span=9, min_periods=9, adjust=False).mean()
    # Normalised by price so the scale is comparable across symbols.
    out["macd"] = macd / close
    out["macd_signal"] = macd_signal / close
    out["macd_hist"] = (macd - macd_signal) / close

    bb_mid = close.rolling(window=20, min_periods=20).mean()
    bb_std = close.rolling(window=20, min_periods=20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    out["bb_width"] = (bb_upper - bb_lower) / bb_mid
    # Where price sits in the band: 0 = lower, 1 = upper.
    band = (bb_upper - bb_lower).replace(0.0, np.nan)
    out["bb_position"] = (close - bb_lower) / band

    vol_mean = out["volume"].rolling(window=20, min_periods=20).mean()
    out["volume_delta"] = out["volume"] / vol_mean.replace(0.0, np.nan) - 1.0

    out["return_1"] = close.pct_change(1)
    out["return_4"] = close.pct_change(4)
    out["high_low_range"] = (out["high"] - out["low"]) / close

    return out


def add_target(
    df: pd.DataFrame, move_pct: float, horizon_candles: int
) -> pd.DataFrame:
    """Label each row: did close rise more than `move_pct`% within the next
    `horizon_candles` candles? (§5.1, both values config-driven.)

    Uses the maximum *high* over the horizon, not just the closing price at
    the horizon: a target that a stop-limit order would actually have hit.
    """
    out = df.copy()

    # A forward window over the once-shifted highs covers exactly
    # highs[t+1 .. t+N] — the next N candles, never the current one.
    # (A backward window would run off the start and lose the first N-1 rows.)
    indexer = FixedForwardWindowIndexer(window_size=horizon_candles)
    future_high = (
        out["high"]
        .shift(-1)
        .rolling(window=indexer, min_periods=horizon_candles)
        .max()
    )

    threshold = out["close"] * (1.0 + move_pct / 100.0)
    target = (future_high >= threshold).astype("float64")

    # A NaN future compares False, which would silently label the last N
    # rows as "no move" instead of "unknown". Restore them to NaN so
    # build_dataset drops them.
    target[future_high.isna()] = np.nan
    out[TARGET_COLUMN] = target

    return out
