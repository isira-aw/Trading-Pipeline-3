"""Settings page endpoints: config editing and the stage PIN (§3, §7, §10, step 10).

Every value here is read live from the `config` table by its consumer (see
`config_service`), so a save from this page takes effect on the next read —
no restart — *except* the handful of scheduler intervals that APScheduler
bakes into a trigger at job-add time. For those, saving here also calls
`scheduler.reschedule_job_for_config` so Core Principle #1 holds for them
too.

The stage PIN is never returned to the client. Changing it requires the
current PIN, verified against the stored hash — a bare new-PIN field would
let anyone with dashboard access relock the stage switch.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config_defaults import CONFIG_DEFAULTS
from app.db.session import get_db
from app.services import scheduler as scheduler_service
from app.services import security
from app.services.config_service import get_config, get_all_config, set_config

router = APIRouter()

# The PIN hash is config-table-backed but must never round-trip to the client.
HIDDEN_KEYS = {"stage_pin_hash"}


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    """All editable config, plus whether the stage PIN is still the factory default."""
    all_config = await get_all_config(db)
    pin_hash = all_config.get("stage_pin_hash")

    values = {k: v for k, v in all_config.items() if k not in HIDDEN_KEYS}
    return {
        "values": values,
        "editable_keys": sorted(CONFIG_DEFAULTS.keys()),
        "pin_is_default": security.is_default_pin(pin_hash) if pin_hash else True,
    }


class SettingsUpdate(BaseModel):
    values: dict


@router.put("/settings")
async def update_settings(body: SettingsUpdate, db: AsyncSession = Depends(get_db)):
    """Upsert one or more config values. Unknown or hidden keys are rejected."""
    unknown = [k for k in body.values if k not in CONFIG_DEFAULTS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown setting(s): {unknown}")
    hidden = [k for k in body.values if k in HIDDEN_KEYS]
    if hidden:
        raise HTTPException(
            status_code=400,
            detail=f"{hidden} cannot be set here; use /api/settings/pin.",
        )

    for key, value in body.values.items():
        await set_config(db, key, value)
    await db.commit()

    # Scheduler intervals are baked into a running trigger at add_job time;
    # without this they would silently keep the old cadence until a restart.
    rescheduled = [
        key for key, value in body.values.items()
        if scheduler_service.reschedule_job_for_config(key, value)
    ]

    return {"updated": list(body.values.keys()), "rescheduled_jobs": rescheduled}


class PinChange(BaseModel):
    current_pin: str
    new_pin: str


@router.post("/settings/pin")
async def change_pin(body: PinChange, db: AsyncSession = Depends(get_db)):
    stored = await get_config(db, "stage_pin_hash", None)
    if not stored or not security.verify_pin(body.current_pin, stored):
        raise HTTPException(status_code=403, detail="Current PIN is incorrect.")

    if not (body.new_pin.isdigit() and len(body.new_pin) == 6):
        raise HTTPException(status_code=400, detail="New PIN must be 6 digits.")

    await set_config(db, "stage_pin_hash", security.hash_pin(body.new_pin))
    await db.commit()
    return {"changed": True}
