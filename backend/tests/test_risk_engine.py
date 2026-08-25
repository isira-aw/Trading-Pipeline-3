"""Unit tests for the risk engine's pure rules (§6).

This is the component that can veto any trade regardless of what the model
says, so it gets the heaviest coverage. Every rule is tested in isolation,
then the composition in `evaluate` is tested for the two properties that
make risk_log useful: all failures are reported, and the tightest resize
wins.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.risk_engine import (
    ACTION_PASS,
    ACTION_REJECT,
    ACTION_RESIZE,
    CHECK_CONFIDENCE,
    CHECK_DAILY_LOSS,
    CHECK_EXPOSURE,
    CHECK_FREQUENCY,
    CHECK_HEALTH,
    CHECK_POSITION_SIZE,
    CHECK_VOLATILITY,
    DECISION_APPROVED,
    DECISION_REJECTED,
    DECISION_RESIZED,
    ComponentHeartbeat,
    DailyPnlBaseline,
    RiskContext,
    TradeProposal,
    VolatilityStats,
    check_component_health,
    check_confidence,
    check_daily_loss,
    check_exposure,
    check_position_size,
    check_trade_frequency,
    check_volatility,
    zscore_latest,
    evaluate,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

LIMITS = {
    "max_position_pct": 10.0,
    "max_daily_loss_pct": 5.0,
    "min_confidence": 0.6,
    "max_total_exposure_pct": 80.0,
    "volatility_sigma_limit": 3.0,
    "component_heartbeat_max_age_seconds": 300,
    "min_order_notional_usdt": 10.0,
    "max_trades_per_day": 10,
    "required_healthy_components": ["binance_api", "data_feed"],
    "volatility_lookback_candles": 100,
    "max_pnl_baseline_age_hours": 24,
}


def healthy_components(age_seconds=10, extra=None):
    beat = NOW - timedelta(seconds=age_seconds)
    components = {
        "binance_api": ComponentHeartbeat("online", beat),
        "data_feed": ComponentHeartbeat("online", beat),
    }
    if extra:
        components.update(extra)
    return components


def context(**overrides) -> RiskContext:
    defaults = dict(
        now=NOW,
        wallet_total_usdt=10_000.0,
        current_exposure_usdt=0.0,
        daily_pnl=DailyPnlBaseline(
            available=True, pnl_pct=0.0, baseline_value=10_000.0,
            current_value=10_000.0, baseline_at=NOW - timedelta(hours=12),
        ),
        volatility=VolatilityStats(
            available=True, range_z=0.5, volume_z=-0.3, sample_size=100
        ),
        components=healthy_components(),
        trades_today=0,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def buy(quantity=0.01, price=50_000.0, confidence=0.75) -> TradeProposal:
    return TradeProposal("BTCUSDT", "buy", quantity, price, confidence, "model-1")


def sell(quantity=0.01, price=50_000.0, confidence=None) -> TradeProposal:
    return TradeProposal("BTCUSDT", "sell", quantity, price, confidence, "model-1")


class TestPositionSize:
    def test_within_limit_passes(self):
        # 0.01 BTC @ 50k = 500 USDT, limit is 10% of 10k = 1000.
        result = check_position_size(buy(), context(), LIMITS)
        assert result.passed and result.action == ACTION_PASS

    def test_exactly_at_limit_passes(self):
        result = check_position_size(buy(quantity=0.02), context(), LIMITS)
        assert result.passed

    def test_oversized_resizes_to_exact_limit(self):
        # 0.1 BTC @ 50k = 5000 USDT against a 1000 USDT cap.
        result = check_position_size(buy(quantity=0.1), context(), LIMITS)
        assert result.action == ACTION_RESIZE
        assert result.suggested_quantity == pytest.approx(1000.0 / 50_000.0)

    def test_rejects_when_resized_below_minimum_notional(self):
        """10% of a 50 USDT wallet is 5 USDT, under the 10 USDT minimum."""
        result = check_position_size(
            buy(quantity=0.1), context(wallet_total_usdt=50.0), LIMITS
        )
        assert result.action == ACTION_REJECT
        assert "minimum viable order" in result.detail

    def test_empty_wallet_rejects(self):
        result = check_position_size(buy(), context(wallet_total_usdt=0.0), LIMITS)
        assert result.action == ACTION_REJECT

    def test_sell_is_never_resized(self):
        """Resizing an exit would strand the system in a large position."""
        result = check_position_size(sell(quantity=100.0), context(), LIMITS)
        assert result.passed and result.action == ACTION_PASS


class TestExposure:
    def test_within_headroom_passes(self):
        result = check_exposure(buy(), context(current_exposure_usdt=1_000.0), LIMITS)
        assert result.passed

    def test_resizes_to_remaining_headroom(self):
        # Cap 8000; already 7500 exposed leaves 500 headroom for a 5000 order.
        result = check_exposure(
            buy(quantity=0.1), context(current_exposure_usdt=7_500.0), LIMITS
        )
        assert result.action == ACTION_RESIZE
        assert result.suggested_quantity == pytest.approx(500.0 / 50_000.0)

    def test_rejects_when_headroom_below_minimum_notional(self):
        result = check_exposure(
            buy(quantity=0.1), context(current_exposure_usdt=7_995.0), LIMITS
        )
        assert result.action == ACTION_REJECT
        assert "headroom" in result.detail

    def test_rejects_when_already_over_cap(self):
        result = check_exposure(
            buy(), context(current_exposure_usdt=9_000.0), LIMITS
        )
        assert result.action == ACTION_REJECT

    def test_sell_always_passes(self):
        result = check_exposure(
            sell(quantity=100.0), context(current_exposure_usdt=9_999.0), LIMITS
        )
        assert result.passed


class TestDailyLoss:
    def test_profit_passes(self):
        pnl = DailyPnlBaseline(available=True, pnl_pct=3.2)
        assert check_daily_loss(buy(), context(daily_pnl=pnl), LIMITS).passed

    def test_small_loss_passes(self):
        pnl = DailyPnlBaseline(available=True, pnl_pct=-4.9)
        assert check_daily_loss(buy(), context(daily_pnl=pnl), LIMITS).passed

    def test_loss_at_cap_rejects(self):
        pnl = DailyPnlBaseline(available=True, pnl_pct=-5.0)
        result = check_daily_loss(buy(), context(daily_pnl=pnl), LIMITS)
        assert result.action == ACTION_REJECT

    def test_loss_beyond_cap_rejects(self):
        pnl = DailyPnlBaseline(available=True, pnl_pct=-8.0)
        assert not check_daily_loss(buy(), context(daily_pnl=pnl), LIMITS).passed

    def test_unavailable_baseline_rejects_rather_than_passes(self):
        """§1.7 — absent data must stop trading, not be read as healthy."""
        pnl = DailyPnlBaseline(available=False, reason="no wallet snapshot")
        result = check_daily_loss(buy(), context(daily_pnl=pnl), LIMITS)
        assert result.action == ACTION_REJECT
        assert "no wallet snapshot" in result.detail

    def test_sell_allowed_even_past_the_cap(self):
        """Blocking exits during a drawdown would trap the losing position."""
        pnl = DailyPnlBaseline(available=True, pnl_pct=-20.0)
        assert check_daily_loss(sell(), context(daily_pnl=pnl), LIMITS).passed

    def test_sell_allowed_with_no_baseline(self):
        pnl = DailyPnlBaseline(available=False, reason="none")
        assert check_daily_loss(sell(), context(daily_pnl=pnl), LIMITS).passed


class TestConfidence:
    def test_above_floor_passes(self):
        assert check_confidence(buy(confidence=0.75), context(), LIMITS).passed

    def test_at_floor_passes(self):
        assert check_confidence(buy(confidence=0.6), context(), LIMITS).passed

    def test_below_floor_rejects(self):
        result = check_confidence(buy(confidence=0.59), context(), LIMITS)
        assert result.action == ACTION_REJECT

    def test_buy_without_confidence_rejects(self):
        """Omitting the field must not become a way around the floor."""
        result = check_confidence(buy(confidence=None), context(), LIMITS)
        assert result.action == ACTION_REJECT
        assert "no model confidence" in result.detail

    def test_sell_without_confidence_passes(self):
        assert check_confidence(sell(), context(), LIMITS).passed


class TestVolatility:
    def test_normal_market_passes(self):
        assert check_volatility(buy(), context(), LIMITS).passed

    def test_range_spike_rejects(self):
        stats = VolatilityStats(available=True, range_z=4.5, volume_z=0.1)
        result = check_volatility(buy(), context(volatility=stats), LIMITS)
        assert result.action == ACTION_REJECT
        assert "candle range" in result.detail

    def test_volume_spike_rejects(self):
        stats = VolatilityStats(available=True, range_z=0.2, volume_z=-6.0)
        result = check_volatility(buy(), context(volatility=stats), LIMITS)
        assert result.action == ACTION_REJECT
        assert "volume" in result.detail

    def test_both_spikes_reported_together(self):
        stats = VolatilityStats(available=True, range_z=4.5, volume_z=5.0)
        detail = check_volatility(buy(), context(volatility=stats), LIMITS).detail
        assert "candle range" in detail and "volume" in detail

    def test_unavailable_stats_reject(self):
        stats = VolatilityStats(available=False, reason="only 3 candles")
        result = check_volatility(buy(), context(volatility=stats), LIMITS)
        assert result.action == ACTION_REJECT

    def test_applies_to_sells_too(self):
        """A data glitch makes any order unsafe, including an exit."""
        stats = VolatilityStats(available=True, range_z=9.0, volume_z=0.0)
        assert not check_volatility(sell(), context(volatility=stats), LIMITS).passed


class TestComponentHealth:
    def test_all_online_passes(self):
        assert check_component_health(buy(), context(), LIMITS).passed

    def test_offline_component_rejects(self):
        components = healthy_components()
        components["binance_api"] = ComponentHeartbeat("offline", NOW)
        result = check_component_health(buy(), context(components=components), LIMITS)
        assert result.action == ACTION_REJECT
        assert "binance_api" in result.detail

    def test_stale_heartbeat_rejects(self):
        components = healthy_components(age_seconds=400)
        result = check_component_health(buy(), context(components=components), LIMITS)
        assert result.action == ACTION_REJECT
        assert "old" in result.detail

    def test_heartbeat_just_within_limit_passes(self):
        components = healthy_components(age_seconds=299)
        assert check_component_health(buy(), context(components=components), LIMITS).passed

    def test_missing_component_row_rejects(self):
        """A missing row is not a healthy one (§1.7)."""
        result = check_component_health(buy(), context(components={}), LIMITS)
        assert result.action == ACTION_REJECT
        assert "no status recorded" in result.detail

    def test_null_heartbeat_rejects(self):
        components = healthy_components()
        components["data_feed"] = ComponentHeartbeat("online", None)
        result = check_component_health(buy(), context(components=components), LIMITS)
        assert result.action == ACTION_REJECT

    def test_all_failing_components_reported(self):
        components = {
            "binance_api": ComponentHeartbeat("error", NOW),
            "data_feed": ComponentHeartbeat("offline", NOW),
        }
        detail = check_component_health(buy(), context(components=components), LIMITS).detail
        assert "binance_api" in detail and "data_feed" in detail

    def test_stale_risk_engine_heartbeat_does_not_block_trading(self):
        """The engine must never gate on its own heartbeat.

        Doing so is meaningless — this code running proves it is alive — and
        deadlock-prone, since the heartbeat is written by the scheduler.
        """
        components = healthy_components(
            extra={
                "risk_engine": ComponentHeartbeat(
                    "offline", NOW - timedelta(days=3)
                )
            }
        )
        assert check_component_health(buy(), context(components=components), LIMITS).passed

    def test_missing_risk_engine_row_does_not_block_trading(self):
        components = healthy_components()
        components.pop("risk_engine", None)
        assert check_component_health(buy(), context(components=components), LIMITS).passed

    def test_unrelated_component_failure_does_not_block(self):
        """Only the configured dependencies gate trading; the LLM advisor is
        context-only (§5.1) and must not stop trades when it is down."""
        components = healthy_components(
            extra={"llm_advisor": ComponentHeartbeat("error", NOW)}
        )
        assert check_component_health(buy(), context(components=components), LIMITS).passed


class TestTradeFrequency:
    def test_below_cap_passes(self):
        result = check_trade_frequency(buy(), context(trades_today=3), LIMITS)
        assert result.passed
        assert "frequency cap:" in result.detail

    def test_at_cap_rejects(self):
        result = check_trade_frequency(buy(), context(trades_today=10), LIMITS)
        assert result.action == ACTION_REJECT

    def test_detail_is_distinguishable_from_other_rejections(self):
        """risk_log must make "hit the frequency cap" unmistakable, so a
        streak of unrelated rejections is never misread as one."""
        result = check_trade_frequency(buy(), context(trades_today=10), LIMITS)
        assert result.detail.startswith("frequency cap:")
        assert "10/10 executed trades" in result.detail

    def test_sell_not_capped(self):
        assert check_trade_frequency(sell(), context(trades_today=99), LIMITS).passed


class TestZScore:
    """`zscore_latest` feeds the volatility rule; a wrong reading here either
    blocks trading in a calm market or misses a real spike."""

    def test_flat_history_reports_zero_not_one_sigma(self):
        """Regression: summing N identical floats and dividing by N does not
        reproduce the value exactly, so std came out tiny-but-nonzero and
        the z-score collapsed to exactly -1.0 on a perfectly flat market."""
        assert zscore_latest([0.01] * 60) == 0.0

    @pytest.mark.parametrize("value", [0.01, 1.0, 1e-8, 12345.678])
    def test_flat_history_at_various_magnitudes(self, value):
        assert zscore_latest([value] * 60) == 0.0

    def test_flat_history_with_a_deviating_latest_is_infinite(self):
        """Any movement at all against a motionless history is anomalous."""
        assert zscore_latest([5.0] + [1.0] * 50) == float("inf")

    def test_all_zero_history(self):
        assert zscore_latest([0.0] * 30) == 0.0

    def test_known_z_score(self):
        # history 1..5: mean 3, population std sqrt(2).
        assert zscore_latest([3 + 2 * 2 ** 0.5, 1, 2, 3, 4, 5]) == pytest.approx(2.0)

    def test_negative_deviation(self):
        assert zscore_latest([1, 2, 3, 4, 5, 6]) < 0

    def test_spike_exceeds_sigma_limit(self):
        values = [10.0] + [1.0, 1.1, 0.9, 1.05, 0.95] * 10
        assert zscore_latest(values) > 3.0


class TestEvaluateComposition:
    def test_happy_path_approves_unchanged(self):
        decision = evaluate(buy(), context(), LIMITS)
        assert decision.decision == DECISION_APPROVED
        assert decision.final_quantity == 0.01
        assert decision.reason == "All risk checks passed."
        assert all(check.passed for check in decision.checks)

    def test_every_rule_runs_on_the_happy_path(self):
        decision = evaluate(buy(), context(), LIMITS)
        assert set(decision.checks_dict()) == {
            CHECK_HEALTH, CHECK_VOLATILITY, CHECK_CONFIDENCE, CHECK_DAILY_LOSS,
            CHECK_POSITION_SIZE, CHECK_EXPOSURE, CHECK_FREQUENCY,
        }

    def test_resize_uses_tightest_constraint(self):
        """Position sizing allows 1000 USDT; exposure headroom allows 500."""
        decision = evaluate(
            buy(quantity=0.1), context(current_exposure_usdt=7_500.0), LIMITS
        )
        assert decision.decision == DECISION_RESIZED
        assert decision.final_quantity == pytest.approx(500.0 / 50_000.0)

    def test_single_rejection_zeroes_quantity(self):
        decision = evaluate(buy(confidence=0.1), context(), LIMITS)
        assert decision.decision == DECISION_REJECTED
        assert decision.final_quantity == 0.0

    def test_rejection_beats_resize(self):
        """An oversized order that also fails a hard rule is rejected, not
        quietly resized and sent."""
        decision = evaluate(buy(quantity=0.1, confidence=0.1), context(), LIMITS)
        assert decision.decision == DECISION_REJECTED

    def test_multiple_simultaneous_failures_all_reported(self):
        """The whole point of not short-circuiting: risk_log has to show
        every objection, or debugging a rejection later is guesswork."""
        decision = evaluate(
            buy(quantity=0.1, confidence=0.2),
            context(
                daily_pnl=DailyPnlBaseline(available=True, pnl_pct=-9.0),
                volatility=VolatilityStats(available=True, range_z=7.0, volume_z=0.0),
                components={
                    "binance_api": ComponentHeartbeat("offline", NOW),
                    "data_feed": ComponentHeartbeat("online", NOW),
                },
                trades_today=50,
            ),
            LIMITS,
        )

        assert decision.decision == DECISION_REJECTED

        results = decision.checks_dict()
        rejected = {n for n, r in results.items() if r["action"] == ACTION_REJECT}
        assert rejected == {
            CHECK_HEALTH, CHECK_VOLATILITY, CHECK_CONFIDENCE,
            CHECK_DAILY_LOSS, CHECK_FREQUENCY,
        }

        # Every rejection must appear in the human-readable reason too.
        for name in rejected:
            assert name in decision.reason

        # The oversized quantity is still recorded as a resize, even though
        # the hard rejections mean it will never be acted on — risk_log
        # shows the full picture, not just the first objection.
        assert results[CHECK_POSITION_SIZE]["action"] == ACTION_RESIZE
        assert results[CHECK_POSITION_SIZE]["suggested_quantity"] is not None

    def test_passing_checks_recorded_alongside_failures(self):
        """risk_log records all checks, not only the failing ones (§6)."""
        decision = evaluate(buy(confidence=0.1), context(), LIMITS)
        results = decision.checks_dict()
        assert results[CHECK_CONFIDENCE]["passed"] is False
        assert results[CHECK_HEALTH]["passed"] is True
        assert results[CHECK_POSITION_SIZE]["passed"] is True

    def test_sell_approved_in_conditions_that_block_a_buy(self):
        ctx = context(
            daily_pnl=DailyPnlBaseline(available=True, pnl_pct=-20.0),
            current_exposure_usdt=9_999.0,
            trades_today=99,
        )
        assert evaluate(buy(), ctx, LIMITS).decision == DECISION_REJECTED
        assert evaluate(sell(), ctx, LIMITS).decision == DECISION_APPROVED

    def test_proposal_snapshot_survives_rejection(self):
        """A rejected trade never becomes a `trades` row, so the proposal has
        to be recoverable from risk_log alone (§3)."""
        proposal = buy(quantity=0.1, confidence=0.2)
        snapshot = proposal.snapshot()
        assert snapshot["symbol"] == "BTCUSDT"
        assert snapshot["side"] == "buy"
        assert snapshot["notional_usdt"] == pytest.approx(5_000.0)
        assert snapshot["confidence"] == 0.2
        assert snapshot["model_id"] == "model-1"
