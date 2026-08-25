"""Baseline model training (§5.1, build order step 4).

Trains an XGBoost classifier that outputs a *probability* that price will
rise by the configured amount within the configured horizon. That
probability is the "confidence" the risk engine gates on — it is explicitly
not a price prediction (§1.6).

Two rules this module exists to enforce:

* The train/test split is strictly time-ordered. Shuffling a time series
  lets the model learn from its own future, which produces excellent
  holdout numbers and loses money in production.
* Every run writes a new model file under a fresh UUID. Nothing is ever
  overwritten, so any previous model stays loadable for rollback (§5.1).
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession
from xgboost import XGBClassifier

from app.db.models import Candle, Model
from app.db.session import AsyncSessionLocal
from app.services.config_service import get_config
from app.services.features import (
    FEATURE_COLUMNS,
    MIN_CANDLES_FOR_FEATURES,
    TARGET_COLUMN,
    add_target,
    compute_features,
)

logger = logging.getLogger(__name__)

MODEL_TYPE = "xgboost_classifier"
MODELS_STORE = Path(__file__).resolve().parents[1] / "models_store"

# Fraction of the (time-ordered) rows used for training; the rest is holdout.
TRAIN_FRACTION = 0.8

# Below this there is not enough signal to say anything meaningful, and the
# holdout would be too small to evaluate against.
MIN_TRAINING_ROWS = 250


class TrainingError(RuntimeError):
    """Training could not produce a usable model."""


async def load_candles(db: AsyncSession, symbol: str, interval: str) -> pd.DataFrame:
    """Load candles for one symbol/interval, oldest first."""
    result = await db.execute(
        select(
            Candle.open_time, Candle.open, Candle.high, Candle.low,
            Candle.close, Candle.volume,
        )
        .where(Candle.symbol == symbol, Candle.interval == interval)
        .order_by(Candle.open_time)
    )
    rows = result.all()

    df = pd.DataFrame(
        rows, columns=["open_time", "open", "high", "low", "close", "volume"]
    )
    if df.empty:
        return df

    # Stored as NUMERIC (Decimal); the indicator maths needs floats.
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = df[column].astype("float64")

    return df


def build_dataset(
    df: pd.DataFrame, move_pct: float, horizon_candles: int
) -> pd.DataFrame:
    """Candles -> model-ready rows, with unusable rows dropped."""
    featured = compute_features(df)
    labelled = add_target(featured, move_pct, horizon_candles)

    # Drops the warm-up rows (indicators still NaN) and the tail rows whose
    # future is not yet known.
    return labelled.dropna(subset=FEATURE_COLUMNS + [TARGET_COLUMN]).reset_index(
        drop=True
    )


def time_ordered_split(dataset: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split oldest->newest. Never shuffled (§5.1)."""
    split_at = int(len(dataset) * TRAIN_FRACTION)
    return dataset.iloc[:split_at], dataset.iloc[split_at:]


def _evaluate(model: XGBClassifier, X: pd.DataFrame, y: pd.Series) -> dict:
    """Holdout metrics. Precision on the "up" class is weighted heavily by
    the registry because a false positive is a losing trade (§5.1)."""
    probabilities = model.predict_proba(X)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)

    metrics = {
        "accuracy": float(accuracy_score(y, predictions)),
        "precision": float(precision_score(y, predictions, zero_division=0)),
        "recall": float(recall_score(y, predictions, zero_division=0)),
        "f1": float(f1_score(y, predictions, zero_division=0)),
        "positive_rate": float(y.mean()),
        "predicted_positive_rate": float(predictions.mean()),
        "holdout_rows": int(len(y)),
    }

    # AUC is undefined when the holdout has only one class.
    if y.nunique() > 1:
        metrics["roc_auc"] = float(roc_auc_score(y, probabilities))
        metrics["avg_precision"] = float(average_precision_score(y, probabilities))
    else:
        metrics["roc_auc"] = None
        metrics["avg_precision"] = None
        logger.warning("Holdout contains a single class; AUC not computable.")

    return metrics


async def train_symbol(db: AsyncSession, symbol: str) -> dict:
    """Train one baseline model and register it as a `candidate` (§5.1).

    Returns the created model's id and metrics. Caller commits.
    """
    interval = await get_config(db, "interval")
    move_pct = await get_config(db, "target_move_pct")
    horizon = await get_config(db, "target_horizon_candles")

    candles = await load_candles(db, symbol, interval)
    if len(candles) < MIN_CANDLES_FOR_FEATURES + MIN_TRAINING_ROWS:
        raise TrainingError(
            f"Not enough candles for {symbol} {interval}: have {len(candles)}, "
            f"need at least {MIN_CANDLES_FOR_FEATURES + MIN_TRAINING_ROWS}. "
            f"Run a data download first."
        )

    dataset = build_dataset(candles, move_pct, horizon)
    if len(dataset) < MIN_TRAINING_ROWS:
        raise TrainingError(
            f"Only {len(dataset)} usable rows for {symbol} after feature "
            f"warm-up; need {MIN_TRAINING_ROWS}."
        )

    train_df, test_df = time_ordered_split(dataset)
    X_train, y_train = train_df[FEATURE_COLUMNS], train_df[TARGET_COLUMN]
    X_test, y_test = test_df[FEATURE_COLUMNS], test_df[TARGET_COLUMN]

    if y_train.nunique() < 2:
        raise TrainingError(
            f"Training window for {symbol} contains only one class — the "
            f"target threshold ({move_pct}% in {horizon} candles) is likely "
            f"too extreme for this data."
        )

    # Class imbalance: a rare "up" move would otherwise train a model that
    # always predicts 0 and scores well on accuracy while being useless.
    negatives = float((y_train == 0).sum())
    positives = float((y_train == 1).sum())
    scale_pos_weight = negatives / positives if positives else 1.0

    model = XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        # Modest regularisation: 4h crypto features are noisy and an
        # unconstrained tree memorises the training window.
        reg_lambda=1.0,
        scale_pos_weight=scale_pos_weight,
        objective="binary:logistic",
        eval_metric="logloss",
        tree_method="hist",
        random_state=42,
    )
    model.fit(X_train, y_train)

    metrics = _evaluate(model, X_test, y_test)
    metrics["train_rows"] = int(len(train_df))
    metrics["scale_pos_weight"] = float(scale_pos_weight)
    metrics["feature_importance"] = {
        name: float(score)
        for name, score in zip(FEATURE_COLUMNS, model.feature_importances_)
    }

    model_id = uuid.uuid4()
    MODELS_STORE.mkdir(parents=True, exist_ok=True)
    # New UUID per run — previous files stay on disk for rollback (§5.1).
    file_path = MODELS_STORE / f"{symbol}_{MODEL_TYPE}_{model_id}.json"
    model.save_model(str(file_path))

    data_start = candles["open_time"].iloc[0]
    data_end = candles["open_time"].iloc[-1]

    record = Model(
        id=model_id,
        symbol=symbol,
        model_type=MODEL_TYPE,
        file_path=str(file_path),
        trained_at=datetime.now(timezone.utc),
        training_data_range=Range(data_start, data_end, bounds="[]"),
        metrics=metrics,
        status="candidate",
        notes=(
            f"Baseline XGBoost. Target: >{move_pct}% within {horizon} candle(s) "
            f"of {interval}. Time-ordered {TRAIN_FRACTION:.0%} split."
        ),
    )
    db.add(record)
    await db.flush()

    logger.info(
        "Trained %s: accuracy=%.3f precision=%.3f auc=%s",
        symbol, metrics["accuracy"], metrics["precision"], metrics["roc_auc"],
    )

    return {"model_id": str(model_id), "symbol": symbol, "metrics": metrics}


async def train_symbol_job(symbol: str) -> dict:
    """Background/scheduler entry point — owns its session."""
    async with AsyncSessionLocal() as db:
        result = await train_symbol(db, symbol)
        await db.commit()
        return result
