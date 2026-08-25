"""Tests for the training pipeline (§5.1) that need no database."""

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import roc_auc_score
from xgboost import XGBClassifier

from app.services.features import FEATURE_COLUMNS, TARGET_COLUMN
from app.services.training_pipeline import (
    TRAIN_FRACTION,
    build_dataset,
    time_ordered_split,
)
from tests.synth_candles import generate_klines


@pytest.fixture(scope="module")
def dataset():
    klines = generate_klines(count=3000, seed=11)
    df = pd.DataFrame(
        klines,
        columns=["open_time", "open", "high", "low", "close", "volume",
                 "ct", "qav", "n", "tb", "tq", "ig"],
    )[["open_time", "open", "high", "low", "close", "volume"]]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype("float64")
    return build_dataset(df, move_pct=1.0, horizon_candles=1)


class TestBuildDataset:
    def test_drops_rows_with_unusable_values(self, dataset):
        assert not dataset[FEATURE_COLUMNS + [TARGET_COLUMN]].isna().any().any()

    def test_target_is_binary(self, dataset):
        assert set(dataset[TARGET_COLUMN].unique()) <= {0.0, 1.0}

    def test_chronological_order_preserved(self, dataset):
        assert dataset["open_time"].is_monotonic_increasing

    def test_warmup_rows_are_dropped(self, dataset):
        # 3000 candles in, minus the 100-period indicator warm-up and the
        # unknowable tail.
        assert 2800 < len(dataset) < 3000


class TestTimeOrderedSplit:
    def test_split_respects_train_fraction(self, dataset):
        train, test = time_ordered_split(dataset)
        assert len(train) == int(len(dataset) * TRAIN_FRACTION)
        assert len(train) + len(test) == len(dataset)

    def test_test_set_is_strictly_after_train_set(self, dataset):
        """The whole point: never shuffle a time series (§5.1)."""
        train, test = time_ordered_split(dataset)
        assert train["open_time"].max() < test["open_time"].min()

    def test_split_is_deterministic(self, dataset):
        a = time_ordered_split(dataset)[0]["open_time"].tolist()
        b = time_ordered_split(dataset)[0]["open_time"].tolist()
        assert a == b


class TestNoLeakage:
    def test_random_walk_yields_no_predictive_power(self, dataset):
        """Leakage canary.

        The synthetic series is a random walk: future direction is genuinely
        unpredictable from past indicators. A holdout AUC materially above
        0.5 would therefore mean the model is reading its own future, not
        that it found signal.
        """
        train, test = time_ordered_split(dataset)
        model = XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            eval_metric="logloss", tree_method="hist", random_state=42,
        )
        model.fit(train[FEATURE_COLUMNS], train[TARGET_COLUMN])

        probabilities = model.predict_proba(test[FEATURE_COLUMNS])[:, 1]
        auc = roc_auc_score(test[TARGET_COLUMN], probabilities)

        assert 0.40 < auc < 0.62, (
            f"Holdout AUC {auc:.3f} on a random walk suggests lookahead "
            f"leakage in the features or target."
        )

    def test_shuffled_split_would_leak(self, dataset):
        """Demonstrates why §5.1 forbids shuffling.

        Overlapping indicator windows make neighbouring rows near-duplicates,
        so a shuffled split puts near-copies of test rows into training and
        inflates the score. This asserts the failure mode is real, which is
        what makes the ordered-split test above meaningful.
        """
        shuffled = dataset.sample(frac=1.0, random_state=0).reset_index(drop=True)
        cut = int(len(shuffled) * TRAIN_FRACTION)
        train, test = shuffled.iloc[:cut], shuffled.iloc[cut:]

        model = XGBClassifier(
            n_estimators=200, max_depth=6, learning_rate=0.1,
            eval_metric="logloss", tree_method="hist", random_state=42,
        )
        model.fit(train[FEATURE_COLUMNS], train[TARGET_COLUMN])
        auc = roc_auc_score(
            test[TARGET_COLUMN], model.predict_proba(test[FEATURE_COLUMNS])[:, 1]
        )

        ordered_train, ordered_test = time_ordered_split(dataset)
        ordered_model = XGBClassifier(
            n_estimators=200, max_depth=6, learning_rate=0.1,
            eval_metric="logloss", tree_method="hist", random_state=42,
        )
        ordered_model.fit(ordered_train[FEATURE_COLUMNS], ordered_train[TARGET_COLUMN])
        ordered_auc = roc_auc_score(
            ordered_test[TARGET_COLUMN],
            ordered_model.predict_proba(ordered_test[FEATURE_COLUMNS])[:, 1],
        )

        assert auc > ordered_auc, (
            "Shuffling did not inflate the score, so this dataset cannot "
            "detect the leakage the ordered split is meant to prevent."
        )


class TestModelPersistence:
    def test_saved_model_reloads_with_identical_predictions(self, dataset, tmp_path):
        """A model whose stored form predicts differently would silently
        trade on different confidences than the ones it was promoted on."""
        train, test = time_ordered_split(dataset)
        model = XGBClassifier(
            n_estimators=50, max_depth=3, eval_metric="logloss",
            tree_method="hist", random_state=42,
        )
        model.fit(train[FEATURE_COLUMNS], train[TARGET_COLUMN])
        before = model.predict_proba(test[FEATURE_COLUMNS])[:, 1]

        path = tmp_path / "model.json"
        model.save_model(str(path))

        reloaded = XGBClassifier()
        reloaded.load_model(str(path))
        after = reloaded.predict_proba(test[FEATURE_COLUMNS])[:, 1]

        np.testing.assert_allclose(before, after, rtol=0, atol=0)

    def test_feature_order_matters_and_is_fixed(self, dataset):
        """FEATURE_COLUMNS pins the column order; a reordered frame must not
        be silently accepted as equivalent."""
        train, test = time_ordered_split(dataset)
        model = XGBClassifier(
            n_estimators=30, max_depth=3, eval_metric="logloss",
            tree_method="hist", random_state=42,
        )
        model.fit(train[FEATURE_COLUMNS], train[TARGET_COLUMN])

        reordered = test[list(reversed(FEATURE_COLUMNS))]
        with pytest.raises(ValueError):
            model.predict_proba(reordered)
