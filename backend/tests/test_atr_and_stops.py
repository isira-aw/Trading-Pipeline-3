"""ATR computation and stop-loss exit logic.

ATR values here are computed by hand in the comments. A wrong ATR silently
produces a stop that is too tight (stopped out by ordinary noise) or too
loose (a much bigger loss than intended), and neither raises an error.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.services.atr import (
    compute_atr,
    latest_atr,
    stop_price_for_long,
    true_range,
)
from app.services.signal_generator import (
    EXIT_HORIZON,
    EXIT_STOP,
    EXIT_TARGET,
    evaluate_exit,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def frame(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """rows of (high, low, close)."""
    return pd.DataFrame(rows, columns=["high", "low", "close"])


class TestTrueRange:
    def test_first_row_is_the_high_low_span(self):
        """No previous close exists, so TR is just the bar's own range."""
        tr = true_range(frame([(110, 100, 105)]))
        assert tr.iloc[0] == pytest.approx(10.0)

    def test_uses_high_low_when_no_gap(self):
        # prev close 105; high 112, low 108.
        #   high-low            = 4
        #   |high - prev close| = 7   <- largest
        #   |low  - prev close| = 3
        tr = true_range(frame([(110, 100, 105), (112, 108, 110)]))
        assert tr.iloc[1] == pytest.approx(7.0)

    def test_gap_up_uses_distance_from_previous_close(self):
        # prev close 105; bar opens far above: high 130, low 125.
        #   high-low            = 5
        #   |high - prev close| = 25  <- largest
        tr = true_range(frame([(110, 100, 105), (130, 125, 128)]))
        assert tr.iloc[1] == pytest.approx(25.0)

    def test_gap_down_uses_distance_from_previous_close(self):
        # prev close 105; high 90, low 85 -> |low - prev| = 20.
        tr = true_range(frame([(110, 100, 105), (90, 85, 88)]))
        assert tr.iloc[1] == pytest.approx(20.0)

    def test_inside_bar_uses_own_range(self):
        tr = true_range(frame([(110, 100, 105), (106, 104, 105)]))
        assert tr.iloc[1] == pytest.approx(2.0)


class TestAtrHandComputed:
    def test_constant_true_range_gives_that_atr(self):
        """Every TR is exactly 10, so ATR must be 10 at every point."""
        # close == high each bar so |high - prev close| never exceeds 10.
        rows = [(110.0, 100.0, 110.0)]
        for _ in range(20):
            rows.append((110.0, 100.0, 110.0))

        atr = compute_atr(frame(rows), period=14)
        assert atr.iloc[-1] == pytest.approx(10.0)

    def test_seed_is_the_simple_average_of_the_first_n_true_ranges(self):
        """Wilder seeds with an SMA, not the first observation.

        TRs are 1,2,3,...,14 (each bar's own span, no gaps because close
        sits inside the next bar's range). Mean = (1+14)*14/2/14 = 7.5.
        """
        rows = []
        for i in range(1, 15):
            # Bar spans [100, 100+i]; close at 100 so the next bar's
            # prev-close terms never exceed its own span.
            rows.append((100.0 + i, 100.0, 100.0))

        atr = compute_atr(frame(rows), period=14)
        assert atr.iloc[13] == pytest.approx(7.5)

    def test_wilder_recursion_after_the_seed(self):
        """ATR[i] = (ATR[i-1] * 13 + TR[i]) / 14.

        Seed 7.5 (as above), then one more bar with TR = 21.5:
            (7.5 * 13 + 21.5) / 14 = (97.5 + 21.5) / 14 = 119 / 14 = 8.5
        """
        rows = [(100.0 + i, 100.0, 100.0) for i in range(1, 15)]
        # 15th bar: prev close 100, high 121.5, low 100 -> TR = 21.5.
        rows.append((121.5, 100.0, 100.0))

        atr = compute_atr(frame(rows), period=14)
        assert atr.iloc[13] == pytest.approx(7.5)
        assert atr.iloc[14] == pytest.approx(8.5)

    def test_short_period_is_easy_to_verify(self):
        """period=3, TRs 3, 6, 9, 12.
        seed = (3+6+9)/3 = 6
        next = (6*2 + 12)/3 = 24/3 = 8
        """
        rows = [(103.0, 100.0, 100.0), (106.0, 100.0, 100.0),
                (109.0, 100.0, 100.0), (112.0, 100.0, 100.0)]
        atr = compute_atr(frame(rows), period=3)
        assert atr.iloc[2] == pytest.approx(6.0)
        assert atr.iloc[3] == pytest.approx(8.0)

    def test_values_before_the_period_are_nan(self):
        rows = [(100.0 + i, 100.0, 100.0) for i in range(1, 15)]
        atr = compute_atr(frame(rows), period=14)
        assert atr.iloc[:13].isna().all()
        assert not pd.isna(atr.iloc[13])

    def test_too_little_data_returns_all_nan(self):
        assert compute_atr(frame([(110, 100, 105)] * 5), period=14).isna().all()

    def test_invalid_period_raises(self):
        with pytest.raises(ValueError):
            compute_atr(frame([(110, 100, 105)]), period=0)


class TestLatestAtr:
    def test_returns_the_final_value(self):
        rows = [(100.0 + i, 100.0, 100.0) for i in range(1, 15)]
        assert latest_atr(frame(rows), 14) == pytest.approx(7.5)

    def test_none_when_not_enough_candles(self):
        """None, not a guess: a stop from fabricated volatility is a
        fabricated risk limit (§1.7)."""
        assert latest_atr(frame([(110, 100, 105)] * 3), 14) is None

    def test_none_when_atr_is_zero(self):
        """A perfectly flat market gives ATR 0, which would place the stop
        exactly at entry."""
        assert latest_atr(frame([(100.0, 100.0, 100.0)] * 20), 14) is None


class TestStopPrice:
    def test_stop_is_multiplier_times_atr_below_entry(self):
        # 50000 - 2.0 * 250 = 49500
        assert stop_price_for_long(50000.0, 250.0, 2.0) == pytest.approx(49500.0)

    def test_wider_multiplier_moves_the_stop_further(self):
        assert stop_price_for_long(100.0, 5.0, 3.0) < stop_price_for_long(100.0, 5.0, 1.0)

    def test_higher_volatility_widens_the_stop(self):
        """The whole point: a volatile instrument gets more room."""
        calm = stop_price_for_long(100.0, 1.0, 2.0)
        volatile = stop_price_for_long(100.0, 10.0, 2.0)
        assert volatile < calm

    def test_never_negative(self):
        assert stop_price_for_long(10.0, 50.0, 2.0) == 0.0


class TestExitPrecedence:
    """Three exits, checked stop -> target -> horizon."""

    @staticmethod
    def _exit(**overrides):
        params = dict(
            entry_price=100.0, opened_at=NOW, remaining=1.0,
            current_price=100.0, interval="4h", target_move_pct=1.0,
            horizon_candles=1, stop_price=95.0,
            now=NOW + timedelta(minutes=10),
        )
        params.update(overrides)
        return evaluate_exit(**params)

    def test_stop_hit_exits_with_stop_reason(self):
        decision = self._exit(current_price=94.0)
        assert decision.should_exit
        assert decision.exit_reason == EXIT_STOP

    def test_stop_exactly_touched_counts_as_hit(self):
        assert self._exit(current_price=95.0).exit_reason == EXIT_STOP

    def test_target_reached_exits_with_target_reason(self):
        assert self._exit(current_price=101.5).exit_reason == EXIT_TARGET

    def test_horizon_elapsed_exits_with_horizon_reason(self):
        decision = self._exit(
            current_price=100.2, now=NOW + timedelta(hours=5)
        )
        assert decision.exit_reason == EXIT_HORIZON

    def test_holds_when_nothing_triggers(self):
        decision = self._exit(current_price=100.2)
        assert not decision.should_exit
        assert decision.exit_reason is None

    def test_stop_wins_when_both_hit_in_the_same_candle(self):
        """The documented tie-break.

        Candle low 94 breached the 95 stop AND high 102 reached the 101
        target. Within one candle the order is unknowable from OHLC, so the
        conservative resolution wins: resolving in favour of the target
        would overstate performance, and that inflated win rate feeds the
        §5.4 gate deciding whether real money gets deployed.
        """
        decision = self._exit(
            current_price=100.0, candle_high=102.0, candle_low=94.0
        )
        assert decision.should_exit
        assert decision.exit_reason == EXIT_STOP
        assert "Stop hit" in decision.reason

    def test_stop_wins_over_target_and_horizon_together(self):
        decision = self._exit(
            current_price=100.0, candle_high=102.0, candle_low=94.0,
            now=NOW + timedelta(hours=5),
        )
        assert decision.exit_reason == EXIT_STOP

    def test_target_wins_over_horizon(self):
        decision = self._exit(
            current_price=101.5, now=NOW + timedelta(hours=5)
        )
        assert decision.exit_reason == EXIT_TARGET

    def test_intra_candle_stop_honoured_even_if_close_recovered(self):
        """The candle closed back above the stop, but it was breached."""
        decision = self._exit(current_price=99.0, candle_low=94.0, candle_high=99.5)
        assert decision.exit_reason == EXIT_STOP

    def test_no_stop_set_falls_back_to_target_and_horizon(self):
        """Positions opened before stops existed must still exit."""
        assert self._exit(stop_price=None, current_price=101.5).exit_reason == EXIT_TARGET
        assert self._exit(
            stop_price=None, current_price=100.2, now=NOW + timedelta(hours=5)
        ).exit_reason == EXIT_HORIZON

    def test_scalar_price_makes_stop_and_target_mutually_exclusive(self):
        """With one price and stop < entry < target, only one can trigger."""
        assert self._exit(current_price=94.0).exit_reason == EXIT_STOP
        assert self._exit(current_price=101.5).exit_reason == EXIT_TARGET

    def test_exit_closes_the_whole_remaining_lot(self):
        assert self._exit(current_price=94.0, remaining=0.37).quantity == pytest.approx(0.37)
