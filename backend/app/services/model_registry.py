"""Model registry: scoring, promotion, rollback (§5.1, build order step 5).

Scoring deliberately does *not* rank on raw accuracy. With a class balance
around 25/75, a model that never predicts "up" scores 75% accuracy and is
worthless. The two things that matter are:

* **Precision lift** — precision on the "up" class relative to the base
  rate. Precision of 0.27 when 26% of candles go up anyway is no edge at
  all; the same 0.27 when only 10% go up is a real one. §5.1 weights
  precision heavily because a false positive is a losing trade.
* **Discrimination** — how well the probability ranks outcomes (AUC),
  which is what the risk engine's confidence floor actually relies on.

Once paper trading has produced enough closed trades, realized win rate
joins the score and outweighs both, because it reflects fills, fees and
slippage that holdout metrics cannot see.

Promotion never deletes: the previous active model is archived and its file
stays on disk, so any promotion is reversible (§5.1).
"""

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from xgboost import XGBClassifier

from app.db.models import Model, Trade
from app.services.training_pipeline import MODELS_STORE
from app.services.position_tracker import match_fifo
from app.services.config_service import get_config

logger = logging.getLogger(__name__)

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"


class ModelRegistryError(RuntimeError):
    """A promotion or lookup could not be completed."""


def score_model(
    metrics: dict,
    weights: dict,
    min_predicted_positive_rate: float,
    trading_stats: dict | None = None,
    min_trades_for_realized: int = 20,
) -> dict:
    """Score a candidate model. Returns the score plus its breakdown.

    Pure function of its inputs so it can be unit-tested without a database.
    Score is roughly 0-1; higher is better. A disqualified model scores 0.0
    and records why.
    """
    breakdown: dict = {}

    precision = metrics.get("precision") or 0.0
    base_rate = metrics.get("positive_rate") or 0.0
    predicted_rate = metrics.get("predicted_positive_rate") or 0.0
    roc_auc = metrics.get("roc_auc")

    # Degenerate model: fires so rarely that its precision is noise.
    if predicted_rate < min_predicted_positive_rate:
        return {
            "score": 0.0,
            "disqualified": True,
            "reason": (
                f"Predicts positive on only {predicted_rate:.2%} of rows "
                f"(minimum {min_predicted_positive_rate:.2%})."
            ),
            "breakdown": {},
        }

    # Precision lift: how much better than guessing at the base rate.
    # 0.0 means no edge; 1.0 means double the base rate.
    if base_rate > 0:
        lift = (precision / base_rate) - 1.0
    else:
        lift = 0.0
    # Anything at or below the base rate is worthless, not negative-value.
    breakdown["precision_lift"] = max(0.0, min(lift, 1.0))

    # Discrimination: AUC 0.5 is a coin flip, 1.0 is perfect.
    if roc_auc is None:
        breakdown["discrimination"] = 0.0
    else:
        breakdown["discrimination"] = max(0.0, min((roc_auc - 0.5) * 2.0, 1.0))

    # Realized results, once there are enough of them to be meaningful.
    realized_weight = weights.get("realized_win_rate", 0.0)
    has_realized = (
        trading_stats is not None
        and trading_stats.get("closed_trades", 0) >= min_trades_for_realized
    )
    if has_realized:
        breakdown["realized_win_rate"] = max(
            0.0, min(trading_stats.get("win_rate", 0.0), 1.0)
        )
    else:
        breakdown["realized_win_rate"] = 0.0

    active_weights = dict(weights)
    if not has_realized:
        # Don't punish a model for having no trading history yet — drop the
        # realized term and renormalise over the remaining components.
        active_weights.pop("realized_win_rate", None)

    total_weight = sum(active_weights.values()) or 1.0
    score = sum(
        breakdown.get(name, 0.0) * weight for name, weight in active_weights.items()
    ) / total_weight

    return {
        "score": float(score),
        "disqualified": False,
        "reason": None,
        "breakdown": breakdown,
        "used_realized_stats": has_realized,
    }


async def get_trading_stats(db: AsyncSession, model_id, stage: str | None = None) -> dict:
    """Realized results for a model, from closed positions (§5.1).

    Positions are matched FIFO and attributed to the model that opened them,
    so a model's win rate reflects the entries it actually signalled. Win
    rate is net of fees — see `position_tracker`.

    `win_rate` is None when nothing has closed yet, which the scorer treats
    as "no data" rather than a 0% win rate.
    """
    from app.services.position_tracker import compute_stats
    from app.services.trading_engine import load_trade_records

    if stage is None:
        stage = await get_config(db, "current_stage")

    trades = await load_trade_records(db, stage)
    matched = match_fifo(trades)

    owned = [p for p in matched.closed if p.model_id == str(model_id)]
    stats = compute_stats(owned)
    stats["pnl_available"] = bool(owned)
    stats["stage"] = stage
    return stats


async def score_candidates(db: AsyncSession, symbol: str) -> list[dict]:
    """Score every candidate model for a symbol, best first."""
    weights = await get_config(db, "model_scoring_weights")
    min_rate = await get_config(db, "min_predicted_positive_rate")
    min_trades = await get_config(db, "min_trades_for_realized_score")

    result = await db.execute(
        select(Model)
        .where(Model.symbol == symbol, Model.status == STATUS_CANDIDATE)
        .order_by(Model.trained_at.desc())
    )
    candidates = result.scalars().all()

    scored = []
    for model in candidates:
        stats = await get_trading_stats(db, model.id)
        # win_rate is None until this model has closed positions; treat that
        # as "no realized data" rather than feeding None into the score.
        usable_stats = stats if stats["win_rate"] is not None else None

        scoring = score_model(
            model.metrics or {}, weights, min_rate, usable_stats, min_trades
        )
        scored.append(
            {
                "model_id": str(model.id),
                "symbol": model.symbol,
                "trained_at": model.trained_at,
                "status": model.status,
                "file_path": model.file_path,
                **scoring,
            }
        )

    scored.sort(key=lambda entry: entry["score"], reverse=True)
    return scored


async def get_active_model(db: AsyncSession, symbol: str) -> Model | None:
    """The currently active model for a symbol, if any."""
    result = await db.execute(
        select(Model).where(Model.symbol == symbol, Model.status == STATUS_ACTIVE)
    )
    return result.scalars().first()


async def _archive_current_active(db: AsyncSession, symbol: str) -> str | None:
    """Archive whatever is active for this symbol. Files are never deleted."""
    current = await get_active_model(db, symbol)
    if current is None:
        return None
    current.status = STATUS_ARCHIVED
    await db.flush()
    return str(current.id)


async def promote_model(db: AsyncSession, model_id, force: bool = False) -> dict:
    """Promote one model to active, archiving the previous one.

    `force` is the §5.1 manual override: it allows promoting a model the
    scorer disqualified. It does not bypass the file-exists check — a model
    whose binary is missing can never be made active, because the trading
    loop would then fail at inference time with a live position open (§1.7).
    Caller commits.
    """
    model = await db.get(Model, model_id)
    if model is None:
        raise ModelRegistryError(f"No model with id {model_id}")

    if resolve_model_path(model.file_path) is None:
        raise ModelRegistryError(
            f"Model file is missing at {model.file_path}; refusing to promote."
        )

    if model.status == STATUS_ACTIVE:
        return {"model_id": str(model.id), "status": STATUS_ACTIVE, "changed": False}

    if not force:
        weights = await get_config(db, "model_scoring_weights")
        min_rate = await get_config(db, "min_predicted_positive_rate")
        scoring = score_model(model.metrics or {}, weights, min_rate)
        if scoring["disqualified"]:
            raise ModelRegistryError(
                f"Model {model_id} is disqualified: {scoring['reason']} "
                f"Use force=True to override."
            )

    archived_id = await _archive_current_active(db, model.symbol)
    model.status = STATUS_ACTIVE
    await db.flush()

    logger.info(
        "Promoted model %s for %s (archived %s, forced=%s)",
        model.id, model.symbol, archived_id, force,
    )
    return {
        "model_id": str(model.id),
        "symbol": model.symbol,
        "status": STATUS_ACTIVE,
        "archived_model_id": archived_id,
        "forced": force,
        "changed": True,
    }


async def promote_best_candidate(db: AsyncSession, symbol: str) -> dict:
    """Promote the highest-scoring candidate for a symbol (§5.1).

    Refuses to promote a candidate that scores no better than the model
    already active — churning to a worse model is worse than doing nothing.
    Caller commits.
    """
    scored = await score_candidates(db, symbol)
    eligible = [entry for entry in scored if not entry["disqualified"]]

    if not eligible:
        return {
            "symbol": symbol,
            "promoted": False,
            "reason": "No eligible candidate models.",
            "candidates_considered": len(scored),
        }

    best = eligible[0]
    current = await get_active_model(db, symbol)

    if current is not None:
        weights = await get_config(db, "model_scoring_weights")
        min_rate = await get_config(db, "min_predicted_positive_rate")
        current_score = score_model(current.metrics or {}, weights, min_rate)["score"]
        if best["score"] <= current_score:
            return {
                "symbol": symbol,
                "promoted": False,
                "reason": (
                    f"Best candidate scores {best['score']:.4f}, not better "
                    f"than the active model's {current_score:.4f}."
                ),
                "active_model_id": str(current.id),
                "best_candidate_id": best["model_id"],
            }

    result = await promote_model(db, best["model_id"])
    return {
        "symbol": symbol,
        "promoted": True,
        "score": best["score"],
        **result,
    }


async def archive_model(db: AsyncSession, model_id) -> dict:
    """Archive a model. Caller commits."""
    model = await db.get(Model, model_id)
    if model is None:
        raise ModelRegistryError(f"No model with id {model_id}")

    model.status = STATUS_ARCHIVED
    await db.flush()
    return {"model_id": str(model.id), "status": STATUS_ARCHIVED}


def resolve_model_path(file_path: str) -> Path | None:
    """Locate a model file, tolerating a moved installation.

    `models.file_path` is stored absolute, so after restoring a database on
    a machine where the project lives at a different path, every row points
    somewhere that does not exist. Rather than declaring a perfectly good
    model missing, the filename is also looked for in this installation's
    own models_store.

    Returns None when neither location has it.
    """
    path = Path(file_path)
    if path.exists():
        return path

    relocated = MODELS_STORE / path.name
    if relocated.exists():
        logger.info(
            "Model file not at its recorded path (%s); found it in this "
            "installation's models_store instead.", file_path,
        )
        return relocated

    return None


def load_model_file(file_path: str) -> XGBClassifier:
    """Load a trained model from disk for inference."""
    path = resolve_model_path(file_path)
    if path is None:
        raise ModelRegistryError(
            f"Model file not found: {file_path} (also checked "
            f"{MODELS_STORE / Path(file_path).name}). If this database was "
            f"restored from another machine, copy backend/app/models_store/ "
            f"across — the dump does not contain it."
        )

    model = XGBClassifier()
    model.load_model(str(path))
    return model


async def load_active_model(db: AsyncSession, symbol: str) -> tuple[Model, XGBClassifier]:
    """Fetch and load the active model for a symbol.

    Raises rather than returning None: a caller about to trade must fail
    loudly when no model is available, never proceed without one (§1.7).
    """
    record = await get_active_model(db, symbol)
    if record is None:
        raise ModelRegistryError(f"No active model for {symbol}.")

    return record, load_model_file(record.file_path)
