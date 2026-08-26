"""System status and control endpoints (§8.1, §9)."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ComponentStatus
from app.db.session import get_db
from app.services import job_runs
from app.services import scheduler as scheduler_service
from app.services import trading_engine as te
from app.services import halt_service
from app.services.config_service import get_config, set_config
from app.services.event_bus import EVENT_COMPONENT_STATUS, EVENT_SYSTEM, bus
from app.services.security import verify_pin

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
    halt = await halt_service.get_halt_state(db)

    stuck = await te.get_orders_needing_attention(db, stage)

    latest_download = await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD)
    latest_training = await job_runs.latest_job(db, job_runs.JOB_TRAINING)

    return {
        "stage": stage,
        # The halt overrides the stage rather than replacing it, so both are
        # reported: "paper, halted" is a real and distinct state from "paper".
        "halted": halt.halted,
        "halted_at": halt.halted_at,
        "halted_reason": halt.reason,
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
        # Included here (not just via the dedicated endpoints) so a single
        # /api/status fetch on mount is enough to render the readiness
        # table's download/training rows correctly before any WS event.
        "latest_download": job_runs.to_dict(latest_download),
        "latest_training": job_runs.to_dict(latest_training),
    }


@router.get("/training/status")
async def training_status(db: AsyncSession = Depends(get_db)):
    """Latest training job — lets Main/Settings show the real last-run
    outcome on load rather than only after the next `training_progress`
    WebSocket message (§8.1)."""
    job = await job_runs.latest_job(db, job_runs.JOB_TRAINING)
    return job_runs.to_dict(job)


@router.post("/system/test-binance")
async def test_binance_connection(db: AsyncSession = Depends(get_db)):
    """On-demand Binance credential check (§8.1).

    Separate from the passive `binance_api` row on the status table, which
    is the scheduler's 60s heartbeat — useful right after entering new keys,
    without waiting for the next heartbeat tick. A successful/failed check
    also updates that same component row so the passive view reflects it
    immediately rather than up to a heartbeat interval later.
    """
    stage = await get_config(db, "current_stage")
    try:
        state = await te.get_account_state(db, stage)
        detail = f"Account reachable; {state.total_value_usdt:.2f} USDT total."
        await te.set_component_status(db, "binance_api", "online", detail)
        await db.commit()
        bus.publish(EVENT_COMPONENT_STATUS, {
            "component": "binance_api", "status": "online", "detail": detail,
        })
        return {"ok": True, "detail": detail}
    except te.TradingEngineError as exc:
        await te.set_component_status(db, "binance_api", "offline", str(exc))
        await db.commit()
        bus.publish(EVENT_COMPONENT_STATUS, {
            "component": "binance_api", "status": "offline", "detail": str(exc),
        })
        return {"ok": False, "detail": str(exc)}


@router.post("/system/start")
async def start_trading(db: AsyncSession = Depends(get_db)):
    """Enable the trade loop (§8.1 Start System).

    Reconciliation runs first: starting with unresolved orders would size
    positions against a possibly-wrong account view.
    """
    halt = await halt_service.get_halt_state(db)
    if halt.halted:
        # Closing a side door: without this, Start would re-enable trading
        # while halted, and only the loop's own guard would stop it.
        raise HTTPException(
            status_code=409,
            detail=(
                "Emergency stop is active. Resume with the stage PIN before "
                "starting trading."
            ),
        )

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


class PinRequest(BaseModel):
    pin: str = Field(min_length=1, max_length=64)


class LiquidateRequest(BaseModel):
    pin: str = Field(min_length=1, max_length=64)
    # §7 step 6: a second, explicit confirmation — not just one click.
    confirm: bool = False
    confirm_text: str = ""


@router.post("/system/emergency-stop")
async def emergency_stop(db: AsyncSession = Depends(get_db)):
    """Halt trading immediately (§7).

    Deliberately requires no PIN. This is the button for the moment
    something is going wrong, and putting a credential in front of stopping
    is the wrong trade-off — the PIN guards *resuming*, which is where the
    risk of an accidental press actually lies.

    Freezes only. Holdings are not sold: liquidating at market during
    whatever caused the panic is its own risk and its own decision.
    """
    result = await halt_service.engage_halt(db)

    bus.publish(EVENT_SYSTEM, {
        "level": "error",
        "message": "TRADING HALTED — emergency stop active.",
        "halted": True,
    })
    return result


@router.post("/system/resume")
async def resume(payload: PinRequest, db: AsyncSession = Depends(get_db)):
    """Clear the halt (§7 step 5). Requires the stage PIN."""
    stored = await get_config(db, "stage_pin_hash", default="")
    if not verify_pin(payload.pin, stored):
        raise HTTPException(status_code=403, detail="Incorrect stage PIN.")

    state = await halt_service.get_halt_state(db)
    if not state.halted:
        return {"halted": False, "changed": False, "note": "No halt was active."}

    result = await halt_service.release_halt(db)
    bus.publish(EVENT_SYSTEM, {
        "level": "info", "message": "Halt released. Trading remains stopped.",
    })
    return {**result, "changed": True}


@router.post("/system/liquidate")
async def liquidate(payload: LiquidateRequest, db: AsyncSession = Depends(get_db)):
    """Sell all holdings to USDT at market (§7 step 6).

    Explicitly distinct from the emergency stop, and gated twice: the stage
    PIN plus a confirmation flag. Selling everything is irreversible at the
    prices it gets, so it should not be reachable by one stray click.

    Permitted while halted — the halt stops new risk, and this is the
    separate decision to exit instead.
    """
    stored = await get_config(db, "stage_pin_hash", default="")
    if not verify_pin(payload.pin, stored):
        raise HTTPException(status_code=403, detail="Incorrect stage PIN.")

    if not payload.confirm:
        raise HTTPException(
            status_code=428,
            detail=(
                "Liquidation requires explicit confirmation. Re-send with "
                "confirm=true to sell all holdings at market price."
            ),
        )

    stage = await get_config(db, "current_stage")
    result = await halt_service.liquidate_all(db, stage)
    await db.commit()

    bus.publish(EVENT_SYSTEM, {
        "level": "warn",
        "message": f"Liquidation: {result.get('placed_count', 0)} holding(s) sold to USDT.",
    })
    return result


@router.post("/system/reconcile")
async def trigger_reconcile(db: AsyncSession = Depends(get_db)):
    """Force a reconciliation pass, including orders inside their backoff."""
    stage = await get_config(db, "current_stage")
    results = await te.reconcile_open_orders(db, stage)
    return {"reconciled": results}
