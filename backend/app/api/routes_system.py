"""System status and control endpoints (§8.1, §9)."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ComponentStatus
from app.db.session import get_db
from app.services import scheduler as scheduler_service
from app.services import trading_engine as te
from app.services.config_service import get_config, set_config
from app.services.event_bus import EVENT_SYSTEM, bus

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status")
async def system_status(db: AsyncSession = Depends(get_db)):
    """Everything the dashboard header and status strip need (§8.1)."""
    rows = (await db.execute(select(ComponentStatus))).scalars().all()
    now = datetime.now(timezone.utc)
    max_age = await get_config(db, "component_heartbeat_max_age_seconds")

    components = []
    for row in rows:
        age = (now - row.last_heartbeat).total_seconds() if row.last_heartbeat else None
        # A component that stopped reporting is stale regardless of the
        # status it last wrote — a crashed process leaves 'online' behind.
        stale = age is None or age > max_age
        components.append({
            "component": row.component,
            "status": row.status,
            "effective_status": (
                row.status if (row.status == "online" and not stale)
                else ("stale" if stale and row.status == "online" else row.status)
            ),
            "last_heartbeat": row.last_heartbeat,
            "age_seconds": age,
            "detail": row.detail,
        })

    stage = await get_config(db, "current_stage")
    trading_enabled = await get_config(db, "trading_enabled")
    allowed, reason = await scheduler_service.trading_allowed(db)

    stuck = await te.get_orders_needing_attention(db, stage)

    return {
        "stage": stage,
        "trading_enabled": trading_enabled,
        "trading_allowed": allowed,
        "trading_blocked_reason": None if allowed else reason,
        "components": components,
        "jobs": scheduler_service.job_status(),
        "scheduler_running": bool(scheduler_service.job_status()),
        "orders_needing_attention": [
            {
                "trade_id": str(t.id), "symbol": t.symbol, "side": t.side,
                "quantity": float(t.quantity), "created_at": t.created_at,
                "detail": (t.risk_notes or {}).get("reconcile", {}).get("last_error"),
            }
            for t in stuck
        ],
        "websocket_clients": bus.subscriber_count,
    }


@router.post("/system/start")
async def start_trading(db: AsyncSession = Depends(get_db)):
    """Enable the trade loop (§8.1 Start System).

    Reconciliation runs first: starting with unresolved orders would size
    positions against a possibly-wrong account view.
    """
    stage = await get_config(db, "current_stage")
    if stage == scheduler_service.STAGE_HALTED:
        return {
            "started": False,
            "reason": "Emergency stop is active. Resume from the halt banner first.",
        }

    await scheduler_service.run_startup_reconciliation()

    await set_config(db, "trading_enabled", True)
    await db.commit()

    bus.publish(EVENT_SYSTEM, {"level": "info", "message": "Trading started."})
    allowed, reason = await scheduler_service.trading_allowed(db)
    return {"started": True, "trading_allowed": allowed, "detail": reason}


@router.post("/system/stop")
async def stop_trading(db: AsyncSession = Depends(get_db)):
    """Stop the trade loop without the ceremony of an emergency stop."""
    await set_config(db, "trading_enabled", False)
    await db.commit()
    bus.publish(EVENT_SYSTEM, {"level": "warn", "message": "Trading stopped."})
    return {"stopped": True}


@router.post("/system/emergency-stop")
async def emergency_stop(db: AsyncSession = Depends(get_db)):
    """Halt trading immediately (§7).

    Freezes only — holdings are deliberately NOT liquidated. Selling
    everything at market during whatever caused the panic is its own
    risk, so that is a separate, explicitly confirmed action.
    """
    await set_config(db, "current_stage", scheduler_service.STAGE_HALTED)
    await set_config(db, "trading_enabled", False)
    await db.commit()

    logger.warning("EMERGENCY STOP triggered.")
    bus.publish(EVENT_SYSTEM, {
        "level": "error",
        "message": "TRADING HALTED — emergency stop active.",
        "halted": True,
    })
    return {
        "halted": True,
        "halted_at": datetime.now(timezone.utc),
        "note": "Existing holdings were not liquidated (freeze only, §7).",
    }


@router.post("/system/reconcile")
async def trigger_reconcile(db: AsyncSession = Depends(get_db)):
    """Force a reconciliation pass, including orders inside their backoff."""
    stage = await get_config(db, "current_stage")
    results = await te.reconcile_open_orders(db, stage)
    return {"reconciled": results}
