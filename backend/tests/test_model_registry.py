"""Tests for model scoring (§5.1).

Scoring is the thing that decides which model gets to spend real money, so
the cases that matter most are the ones where a bad model *looks* good:
high accuracy from never predicting positive, and high precision that merely
matches the base rate.
"""

import pytest

from app.services.model_registry import score_model

WEIGHTS = {
    "precision_lift": 0.45,
    "discrimination": 0.25,
    "realized_win_rate": 0.30,
}
MIN_RATE = 0.02


def metrics(**overrides) -> dict:
    base = {
        "accuracy": 0.60,
        "precision": 0.35,
        "recall": 0.30,
        "positive_rate": 0.25,
        "predicted_positive_rate": 0.22,
        "roc_auc": 0.58,
    }
    base.update(overrides)
    return base


def score(m, stats=None, min_trades=20) -> dict:
    return score_model(m, WEIGHTS, MIN_RATE, stats, min_trades)


class TestDegenerateModels:
    def test_model_that_never_fires_is_disqualified(self):
        """The classic trap: 75% accuracy by always predicting "no move"."""
        result = score(metrics(
            accuracy=0.75, precision=0.0, predicted_positive_rate=0.0, roc_auc=0.5,
        ))
        assert result["disqualified"]
        assert result["score"] == 0.0

    def test_model_firing_below_threshold_is_disqualified(self):
        result = score(metrics(predicted_positive_rate=0.01))
        assert result["disqualified"]
        assert "0.02" in result["reason"] or "2.00%" in result["reason"]

    def test_model_at_threshold_is_allowed(self):
        assert not score(metrics(predicted_positive_rate=0.02))["disqualified"]


class TestPrecisionLift:
    def test_precision_equal_to_base_rate_scores_no_lift(self):
        """Precision 0.26 when 26% of candles rise anyway is not an edge."""
        result = score(metrics(precision=0.26, positive_rate=0.26))
        assert result["breakdown"]["precision_lift"] == 0.0

    def test_precision_below_base_rate_floors_at_zero(self):
        result = score(metrics(precision=0.10, positive_rate=0.30))
        assert result["breakdown"]["precision_lift"] == 0.0

    def test_double_base_rate_gives_full_lift(self):
        result = score(metrics(precision=0.50, positive_rate=0.25))
        assert result["breakdown"]["precision_lift"] == pytest.approx(1.0)

    def test_same_precision_scores_higher_when_base_rate_is_lower(self):
        """Identical precision is a stronger result against a rarer event."""
        common = score(metrics(precision=0.30, positive_rate=0.28))["score"]
        rare = score(metrics(precision=0.30, positive_rate=0.10))["score"]
        assert rare > common

    def test_zero_base_rate_does_not_divide_by_zero(self):
        result = score(metrics(precision=0.0, positive_rate=0.0))
        assert result["breakdown"]["precision_lift"] == 0.0


class TestDiscrimination:
    def test_coin_flip_auc_scores_zero(self):
        assert score(metrics(roc_auc=0.5))["breakdown"]["discrimination"] == 0.0

    def test_perfect_auc_scores_one(self):
        assert score(metrics(roc_auc=1.0))["breakdown"]["discrimination"] == 1.0

    def test_worse_than_random_floors_at_zero(self):
        assert score(metrics(roc_auc=0.30))["breakdown"]["discrimination"] == 0.0

    def test_missing_auc_is_treated_as_no_discrimination(self):
        """AUC is None when the holdout had a single class."""
        result = score(metrics(roc_auc=None))
        assert result["breakdown"]["discrimination"] == 0.0
        assert not result["disqualified"]


class TestRealizedStats:
    def test_ignored_until_enough_closed_trades(self):
        stats = {"closed_trades": 5, "win_rate": 0.9}
        result = score(metrics(), stats, min_trades=20)
        assert not result["used_realized_stats"]

    def test_used_once_threshold_met(self):
        stats = {"closed_trades": 25, "win_rate": 0.9}
        result = score(metrics(), stats, min_trades=20)
        assert result["used_realized_stats"]
        assert result["breakdown"]["realized_win_rate"] == 0.9

    def test_no_history_does_not_penalise_relative_to_itself(self):
        """A fresh model must not be dragged down by an absent realized term;
        weights are renormalised instead of scoring the missing term as 0."""
        fresh = score(metrics())
        assert fresh["score"] > 0.0
        # With only the two holdout components, a perfect model reaches 1.0.
        perfect = score(metrics(precision=0.50, positive_rate=0.25, roc_auc=1.0))
        assert perfect["score"] == pytest.approx(1.0)

    def test_good_realized_results_beat_good_holdout_alone(self):
        proven = score(metrics(), {"closed_trades": 50, "win_rate": 0.95}, 20)
        unproven = score(metrics())
        assert proven["score"] > unproven["score"]

    def test_poor_realized_results_drag_score_down(self):
        losing = score(metrics(), {"closed_trades": 50, "win_rate": 0.05}, 20)
        unproven = score(metrics())
        assert losing["score"] < unproven["score"]


class TestScoreBounds:
    @pytest.mark.parametrize("m", [
        metrics(),
        metrics(precision=1.0, positive_rate=0.01, roc_auc=1.0),
        metrics(precision=0.0, positive_rate=1.0, roc_auc=0.0),
        metrics(roc_auc=None, precision=0.0),
    ])
    def test_score_stays_within_unit_interval(self, m):
        result = score(m)
        assert 0.0 <= result["score"] <= 1.0

    def test_missing_metric_keys_do_not_raise(self):
        """Metrics come from JSONB written by an older version of the code."""
        result = score({"predicted_positive_rate": 0.2})
        assert 0.0 <= result["score"] <= 1.0

    def test_empty_metrics_disqualified_rather_than_crashing(self):
        assert score({})["disqualified"]
