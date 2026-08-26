"""Model registry endpoints: scoring breakdown, promote, archive (§5.1, step 10).

The Models page exists so a human can see *why* the scorer prefers one
candidate over another — raw accuracy was rejected as misleading (§5.1), so
every model here is returned with its full breakdown, not a single number.
Promote/archive go through `model_registry` directly so the "won't promote a
candidate no better than the incumbent" rule (see `promote_best_candidate`)
still applies to a manual click, not just the scheduled retrain job.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import model_registry as registry
from app.services.config_service import get_config

router = APIRouter()


@router.get("/models")
async def list_models(db: AsyncSession = Depends(get_db)):
    """Every model for every configured symbol, scored, best first."""
    symbols = await get_config(db, "symbols")

    out = []
    for symbol in symbols:
        scored = await registry.score_candidates(db, symbol)
        active = await registry.get_active_model(db, symbol)
        active_id = str(active.id) if active is not None else None

        out.append({
            "symbol": symbol,
            "active_model_id": active_id,
            "models": scored,
        })

    return {"symbols": out}


@router.post("/models/{model_id}/promote")
async def promote(model_id: str, force: bool = False, db: AsyncSession = Depends(get_db)):
    """Manually promote a model. Subject to the same disqualification and
    incumbent-comparison rules as the automatic retrain promotion, unless
    `force` overrides disqualification explicitly (§5.1)."""
    try:
        result = await registry.promote_model(db, model_id, force=force)
        await db.commit()
    except registry.ModelRegistryError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/models/{model_id}/archive")
async def archive(model_id: str, db: AsyncSession = Depends(get_db)):
    try:
        result = await registry.archive_model(db, model_id)
        await db.commit()
    except registry.ModelRegistryError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result
