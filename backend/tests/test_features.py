"""Tests for feature engineering and target labelling (§5.1).

The causality test is the important one here. Lookahead leakage does not
raise an error — it produces a model with excellent holdout metrics that
loses money in production, which is the single most expensive failure mode
this pipeline has.
"""

import numpy as np
import pandas as pd
import pytest

from app.services.features import (
    FEATURE_COLUMNS,
    TARGET_COLUMN,
    add_target,
    compute_features,
)
from tests.synth_candles import generate_klines


def _frame(klines):
    df = pd.DataFrame(
        klines,
        columns=["open_time", "open", "high", "low", "close", "volume",
                 "ct", "qav", "n", "tb", "tq", "ig"],
    )[["open_time", "open", "high", "low", "close", "volume"]]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype("float64")
    return df


@pytest.fixture
def candles():
    return _frame(generate_klines(count=600, seed=3))


class TestTarget:
    """Target = did high exceed close*(1+move_pct) within the next N candles."""

    @staticmethod
    def _fixed():
        return pd.DataFrame({
            "close": [100.0] * 6,
            "high": [100.0, 101.5, 100.2, 100.0, 103.0, 100.0],
            "low": [99.0] * 6,
            "volume": [1.0] * 6,
        })

    def test_horizon_1_looks_only_at_next_candle(self):
        target = add_target(self._fixed(), move_pct=1.0, horizon_candles=1)[TARGET_COLUMN]
        assert list(target[:5]) == [1.0, 0.0, 0.0, 1.0, 0.0]

    def test_horizon_2_spans_next_two_candles(self):
        target = add_target(self._fixed(), move_pct=1.0, horizon_candles=2)[TARGET_COLUMN]
        assert list(target[:4]) == [1.0, 0.0, 1.0, 1.0]

    def test_horizon_3_keeps_leading_rows(self):
        """A backward-window implementation loses the first N-1 rows to NaN."""
        target = add_target(self._fixed(), move_pct=1.0, horizon_candles=3)[TARGET_COLUMN]
        assert list(target[:3]) == [1.0, 1.0, 1.0]

    @pytest.mark.parametrize("horizon", [1, 2, 3, 5])
    def test_unknowable_tail_is_nan_not_zero(self, horizon):
        """The last N rows have no future yet; labelling them 0 would teach
        the model that the end of every dataset is bearish."""
        target = add_target(self._fixed(), move_pct=1.0, horizon_candles=horizon)[TARGET_COLUMN]
        assert target.iloc[-horizon:].isna().all()

    def test_current_candle_never_counts(self):
        """A spike in the CURRENT candle must not label the current row."""
        df = pd.DataFrame({
            "close": [100.0, 100.0, 100.0],
            "high": [200.0, 100.0, 100.0],  # row 0 spikes on itself
            "low": [99.0] * 3,
            "volume": [1.0] * 3,
        })
        target = add_target(df, move_pct=1.0, horizon_candles=1)[TARGET_COLUMN]
        assert target.iloc[0] == 0.0


class TestFeatureCausality:
    def test_future_candles_cannot_change_past_features(self, candles):
        """Mutating the last candle must leave every earlier row identical.

        If any indicator peeked forward, rows before the edit would move.
        """
        baseline = compute_features(candles)

        tampered = candles.copy()
        last = tampered.index[-1]
        tampered.loc[last, ["open", "high", "low", "close"]] *= 5.0
        tampered.loc[last, "volume"] *= 100.0

        after = compute_features(tampered)

        pd.testing.assert_frame_equal(
            baseline.iloc[:-1][FEATURE_COLUMNS],
            after.iloc[:-1][FEATURE_COLUMNS],
        )

    def test_truncating_history_does_not_change_earlier_rows(self, candles):
        """Features at row t depend only on rows <= t, so computing on a
        truncated series must reproduce the same values."""
        full = compute_features(candles)
        cut = 400
        partial = compute_features(candles.iloc[:cut])

        pd.testing.assert_frame_equal(
            full.iloc[:cut][FEATURE_COLUMNS].reset_index(drop=True),
            partial[FEATURE_COLUMNS].reset_index(drop=True),
        )


class TestIndicators:
    def test_rsi_bounded(self, candles):
        rsi = compute_features(candles)["rsi_14"].dropna()
        assert rsi.between(0, 100).all()

    def test_rsi_all_gains_is_100(self):
        df = pd.DataFrame({
            "close": np.arange(100.0, 140.0),
            "high": np.arange(100.0, 140.0) + 1,
            "low": np.arange(100.0, 140.0) - 1,
            "volume": [1.0] * 40,
        })
        assert compute_features(df)["rsi_14"].dropna().iloc[-1] == 100.0

    def test_bb_position_within_band(self, candles):
        pos = compute_features(candles)["bb_position"].dropna()
        # 2-sigma bands; occasional excursions are legitimate, so just check
        # the value is finite and in a sane neighbourhood.
        assert np.isfinite(pos).all()
        assert pos.between(-1.0, 2.0).mean() > 0.99

    def test_no_infinities(self, candles):
        featured = compute_features(candles)[FEATURE_COLUMNS]
        assert not np.isinf(featured.to_numpy(dtype="float64")).any()

    def test_flat_market_does_not_divide_by_zero(self):
        """Zero volatility makes several denominators zero."""
        df = pd.DataFrame({
            "close": [100.0] * 150, "high": [100.0] * 150,
            "low": [100.0] * 150, "volume": [0.0] * 150,
        })
        featured = compute_features(df)[FEATURE_COLUMNS]
        assert not np.isinf(featured.to_numpy(dtype="float64")).any()
