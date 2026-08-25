"""Stage switching and the promotion gate (§5.4, §7, §10).

The switch to live is the boundary between a simulation and real money, so
everything that guards it is enforced here, server-side. A disabled button
is UX; this is the control.

**Gate thresholds are PIN-gated, not freely editable.** `promotion_gate`
sits in PROTECTED_KEYS so the generic config route refuses it, and it can
only be changed through `PUT /api/stage/gate` with the stage PIN. Without
that, the gate would guard nothing: setting `min_win_rate` to 0 and
`min_trade_count` to 1 makes an untested system pass instantly, so an
editable threshold is exactly as powerful as the switch it protects and
needs the same credential.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import promotion_gate as gate_service
from app.services.config_service import get_config, set_config
from app.services.event_bus import EVENT_SYSTEM, bus
from app.services.security import DEFAULT_PIN, is_default_pin, verify_pin

logger = logging.getLogger(__name__)
router = APIRouter()


class StageSwitch(BaseModel):
    stage: str = Field(min_length=1, max_length=10)
    pin: str = Field(min_length=1, max_length=64)


class GateUpdate(BaseModel):
    pin: str = Field(min_length=1, max_length=64)
    thresholds: dict


def _default_pin_warning(pin_hash: str, target_stage: str) -> dict | None:
    """§7/§10: reaching live on the factory PIN must be unmissable.

    Deliberately a loud warning rather than a block — the spec asks for a
    warning, and blocking would strand anyone who genuinely wants the
    default. It is returned on the response and rendered as a banner.
    """
    if not is_default_pin(pin_hash):
        return None
    return {
        "level": "critical" if target_stage == gate_service.STAGE_LIVE else "warning",
        "message": (
            f"The stage PIN is still the factory default ({DEFAULT_PIN}). "
            f"It is the only confirmation step in front of real money — "
            f"change it in Settings."
        ),
    }


@router.get("/stage/gate")
async def read_gate(db: AsyncSession = Depends(get_db)):
    """Current paper-trading stats against each gate criterion (§5.4)."""
    result = await gate_service.evaluate_gate(db)
    pin_hash = await get_config(db, "stage_pin_hash", default="")
    current_stage = await get_config(db, "current_stage")

    return {
        **result.to_dict(),
        "current_stage": current_stage,
        "can_switch_to_live": result.passed,
        "pin_is_default": is_default_pin(pin_hash),
        "pin_warning": _default_pin_warning(pin_hash, gate_service.STAGE_LIVE),
    }


@router.post("/stage/switch")
async def switch_stage(payload: StageSwitch, db: AsyncSession = Depends(get_db)):
    """Switch stage. Requires the PIN; live additionally requires the gate.

    Both checks run here rather than in the UI. A frontend that disables a
    button is a convenience for the operator, not a control — anything that
    can reach this endpoint bypasses it entirely.
    """
    target = payload.stage.strip().lower()

    if target not in gate_service.VALID_STAGES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown stage {target!r}. Valid: {', '.join(gate_service.VALID_STAGES)}.",
        )

    stored_hash = await get_config(db, "stage_pin_hash", default="")
    if not verify_pin(payload.pin, stored_hash):
        logger.warning("Stage switch to %s refused: incorrect PIN.", target)
        raise HTTPException(status_code=403, detail="Incorrect stage PIN.")

    current = await get_config(db, "current_stage")
    warning = _default_pin_warning(stored_hash, target)

    if target == gate_service.STAGE_LIVE:
        result = await gate_service.evaluate_gate(db)
        if not result.passed:
            logger.warning(
                "Stage switch to live refused despite a correct PIN: %s",
                result.summary(),
            )
            # 409, not 403: the credential was right, the record is not
            # ready. Conflating the two would send someone hunting for a
            # PIN problem they do not have.
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Promotion gate not met; cannot switch to live.",
                    "gate": result.to_dict(),
                },
            )

    await set_config(db, "current_stage", target)
    # Switching stage never resumes trading by itself — the operator starts
    # it deliberately from the dashboard.
    await set_config(db, "trading_enabled", False)
    await db.commit()

    logger.warning("Stage switched: %s -> %s", current, target)
    bus.publish(EVENT_SYSTEM, {
        "level": "warn" if target == gate_service.STAGE_LIVE else "info",
        "message": f"Stage switched from {current} to {target}.",
    })

    return {
        "switched": True,
        "from": current,
        "to": target,
        "trading_enabled": False,
        "switched_at": datetime.now(timezone.utc),
        "pin_warning": warning,
        "note": "Trading is stopped after a stage switch; start it deliberately.",
    }


@router.put("/stage/gate")
async def update_gate(payload: GateUpdate, db: AsyncSession = Depends(get_db)):
    """Change the gate thresholds. Requires the stage PIN.

    Loosening the gate is equivalent to switching stages — it is the same
    decision reached by a different route — so it takes the same credential.
    Thresholds are also bounded: a gate demanding nothing is not a gate.
    """
    stored_hash = await get_config(db, "stage_pin_hash", default="")
    if not verify_pin(payload.pin, stored_hash):
        raise HTTPException(status_code=403, detail="Incorrect stage PIN.")

    problems = gate_service.validate_thresholds(payload.thresholds)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))

    previous = await get_config(db, "promotion_gate")
    await set_config(db, "promotion_gate", payload.thresholds)
    await set_config(
        db, "promotion_gate_changed_at", datetime.now(timezone.utc).isoformat()
    )
    await db.commit()

    loosened = [
        key for key in ("min_paper_trading_days", "min_trade_count", "min_win_rate")
        if payload.thresholds.get(key, 0) < (previous or {}).get(key, 0)
    ]
    if payload.thresholds.get("max_drawdown_pct", 0) > (previous or {}).get(
        "max_drawdown_pct", 0
    ):
        loosened.append("max_drawdown_pct")

    if loosened:
        logger.warning(
            "Promotion gate LOOSENED on %s: %s -> %s",
            ", ".join(loosened), previous, payload.thresholds,
        )
        bus.publish(EVENT_SYSTEM, {
            "level": "warn",
            "message": f"Promotion gate loosened: {', '.join(loosened)}.",
        })

    return {
        "updated": True,
        "previous": previous,
        "thresholds": payload.thresholds,
        "loosened": loosened,
    }
