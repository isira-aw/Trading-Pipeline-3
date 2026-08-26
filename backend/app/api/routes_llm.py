"""LLM advisory endpoints (§5.1, §8.1).

Manual triggering goes through the same `generate_advisory` path as the
scheduled job, so the daily cap applies identically — a manual trigger
cannot be used to get extra calls.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import llm_advisor
from app.services.config_service import get_config

router = APIRouter()


@router.get("/advisories")
async def list_advisories(limit: int = 2, db: AsyncSession = Depends(get_db)):
    """Most recent advisories for the dashboard panel (§8.1)."""
    rows = await llm_advisor.get_recent_advisories(db, min(limit, 20))
    now_today, rolling = await llm_advisor.count_calls_today(
        db, __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    )
    cap = await get_config(db, "llm_calls_per_day")

    return {
        "advisories": [
            {
                "id": row.id,
                "provider": row.provider,
                "created_at": row.created_at,
                "status": (row.response or {}).get("status"),
                "uncertainty": (row.response or {}).get("uncertainty"),
                "uncertainty_reason": (row.response or {}).get("uncertainty_reason"),
                "macro_summary": (row.response or {}).get("macro_summary"),
                "symbols": (row.response or {}).get("symbols"),
                "key_risks": (row.response or {}).get("key_risks"),
                "error": (row.response or {}).get("error"),
            }
            for row in rows
        ],
        "calls_today": now_today,
        "calls_rolling_24h": rolling,
        "cap": cap,
    }


@router.post("/advisories/generate")
async def generate(db: AsyncSession = Depends(get_db)):
    """Trigger an advisory now. Subject to the same daily cap."""
    result = await llm_advisor.generate_advisory(db)
    return {
        "created": result.created,
        "advisory_id": result.advisory_id,
        "provider": result.provider,
        "reason": result.reason,
    }
