"""Model registry endpoints (§8.2, §9).

Promote/archive delegate entirely to `model_registry`, including its rule
that a candidate scoring no better than the incumbent is refused. The UI
must not be able to shortcut that: a button that promotes a worse model
because the frontend skipped the check would silently degrade trading.
"""

import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Model
from app.db.session import get_db
from app.services import model_registry as registry
from app.services.config_service import get_config
from app.services.event_bus import EVENT_TRAINING_PROGRESS, bus

logger = logging.getLogger(__name__)
router = APIRouter()


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


@router.get("/models")
async def list_models(
    symbol: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Every model with its full scoring breakdown (§8.2).

    The breakdown matters more than any single number: a model can show 61%
    accuracy and still have zero edge once precision is compared against the
    base rate, so the page shows each component rather than one headline
    figure.
    """
    weights = await get_config(db, "model_scoring_weights")
    min_rate = await get_config(db, "min_predicted_positive_rate")
    min_trades = await get_config(db, "min_trades_for_realized_score")

    query = select(Model)
    if symbol:
        query = query.where(Model.symbol == symbol)
    if status:
        query = query.where(Model.status == status)

    rows = (await db.execute(query.order_by(Model.trained_at.desc()))).scalars().all()

    models = []
    for row in rows:
        stats = await registry.get_trading_stats(db, row.id)
        usable = stats if stats.get("win_rate") is not None else None
        scoring = registry.score_model(
            row.metrics or {}, weights, min_rate, usable, min_trades
        )

        metrics = row.metrics or {}
        models.append({
            "id": str(row.id),
            "symbol": row.symbol,
            "model_type": row.model_type,
            "status": row.status,
            "trained_at": row.trained_at,
            "notes": row.notes,
            "file_path": row.file_path,
            "file_size_bytes": _file_size(row.file_path),
            "file_missing": _file_size(row.file_path) is None,
            # Holdout metrics as trained.
            "metrics": {
                key: metrics.get(key)
                for key in (
                    "accuracy", "precision", "recall", "f1", "roc_auc",
                    "avg_precision", "positive_rate", "predicted_positive_rate",
                    "train_rows", "holdout_rows",
                )
            },
            "feature_importance": metrics.get("feature_importance", {}),
            # The full §5.1 scoring breakdown, not just one number.
            "score": scoring["score"],
            "score_breakdown": scoring.get("breakdown", {}),
            "disqualified": scoring["disqualified"],
            "disqualified_reason": scoring.get("reason"),
            "used_realized_stats": scoring.get("used_realized_stats", False),
            "realized": {
                "closed_trades": stats.get("closed_trades", 0),
                "win_rate": stats.get("win_rate"),
                "total_realized_pnl": stats.get("total_realized_pnl", 0.0),
                "max_drawdown_pct": stats.get("max_drawdown_pct", 0.0),
            },
        })

    return {
        "models": models,
        "scoring_weights": weights,
        "min_predicted_positive_rate": min_rate,
        "min_trades_for_realized_score": min_trades,
    }


@router.post("/models/{model_id}/promote")
async def promote(model_id: str, force: bool = False, db: AsyncSession = Depends(get_db)):
    """Promote to active (§5.1, §8.2).

    Goes through `model_registry.promote_model`, so the disqualification
    check and the missing-file refusal both apply. `force` is the documented
    manual override of the *score* only — it never bypasses a missing file.
    """
    try:
        result = await registry.promote_model(db, model_id, force=force)
        await db.commit()
        return result
    except registry.ModelRegistryError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/models/{symbol}/promote-best")
async def promote_best(symbol: str, db: AsyncSession = Depends(get_db)):
    """Promote the best candidate for a symbol.

    Returns `promoted: false` with a reason when the best candidate is no
    better than the incumbent — that refusal is the registry's, not the
    UI's, so it holds regardless of which client calls it.
    """
    try:
        result = await registry.promote_best_candidate(db, symbol)
        await db.commit()
        return result
    except registry.ModelRegistryError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/models/{model_id}/archive")
async def archive(model_id: str, db: AsyncSession = Depends(get_db)):
    try:
        result = await registry.archive_model(db, model_id)
        await db.commit()
        return result
    except registry.ModelRegistryError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/models/train/{symbol}")
async def train(symbol: str, background_tasks: BackgroundTasks):
    """Kick off training for one symbol (§8.2 "Retrain Now")."""
    from app.services.training_pipeline import train_symbol_job

    async def run():
        bus.publish(EVENT_TRAINING_PROGRESS, {
            "symbol": symbol, "phase": "started", "progress": 0.0,
        })
        try:
            result = await train_symbol_job(symbol)
            bus.publish(EVENT_TRAINING_PROGRESS, {
                "symbol": symbol, "phase": "complete", "progress": 1.0,
                "model_id": result["model_id"],
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("Manual training failed for %s", symbol)
            bus.publish(EVENT_TRAINING_PROGRESS, {
                "symbol": symbol, "phase": "failed", "progress": 1.0,
                "reason": str(exc),
            })

    background_tasks.add_task(run)
    return {"status": "started", "symbol": symbol}


@router.post("/models/train-all")
async def train_all(background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Train every configured symbol (§8.1 "Train Now", §8.2 "Train All")."""
    from app.services.scheduler import retrain_job

    symbols = await get_config(db, "symbols")
    background_tasks.add_task(retrain_job)
    return {"status": "started", "symbols": symbols}


@router.get("/models/candidates/{symbol}")
async def candidates(symbol: str, db: AsyncSession = Depends(get_db)):
    """Ranked candidates for a symbol, best first."""
    return {"symbol": symbol, "candidates": await registry.score_candidates(db, symbol)}
