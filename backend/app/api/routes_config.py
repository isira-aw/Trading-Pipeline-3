"""Settings endpoints (§8.3, §9, §10).

Two things here are security-relevant rather than merely functional:

* **Changing the PIN requires the current PIN.** Without that, anyone who
  can reach the dashboard could set their own PIN and unlock the stage
  switch the PIN exists to protect.
* **`stage_pin_hash` is never returned or writable through the generic
  config endpoints.** It only moves through the dedicated PIN route, so a
  hash cannot be read out or overwritten by a `PUT /api/config/{key}`.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config_defaults import CONFIG_DEFAULTS
from app.db.session import get_db
from app.services import scheduler as scheduler_service
from app.services.config_service import get_all_config, get_config, set_config
from app.services.security import DEFAULT_PIN, hash_pin, is_default_pin, verify_pin

logger = logging.getLogger(__name__)
router = APIRouter()

# Never exposed or settable through the generic config routes.
SECRET_KEYS = {"stage_pin_hash"}

# Changing one of these alters a job's schedule, which is baked into the
# trigger at scheduler start — the jobs are rebuilt so the change takes
# effect without a process restart (§8.3: "no restart needed").
SCHEDULE_KEYS = {
    "trade_loop_interval_minutes",
    "heartbeat_interval_seconds",
    "reconcile_interval_minutes",
    "retrain_interval_hours",
    "data_refresh_hour_utc",
    "llm_calls_per_day",
}

# Stage is deliberately not editable here — it moves only through the
# PIN-gated stage-switch endpoint, so that the promotion gate cannot be
# sidestepped by writing the config key directly.
PROTECTED_KEYS = {"current_stage"}


class ConfigValue(BaseModel):
    value: object


class PinChange(BaseModel):
    current_pin: str = Field(min_length=1, max_length=64)
    new_pin: str = Field(min_length=4, max_length=64)


@router.get("/config")
async def read_config(db: AsyncSession = Depends(get_db)):
    """All settings, minus secrets (§8.3)."""
    values = await get_all_config(db)
    pin_hash = await get_config(db, "stage_pin_hash", default="")

    return {
        "config": {k: v for k, v in values.items() if k not in SECRET_KEYS},
        "defaults": {k: v for k, v in CONFIG_DEFAULTS.items() if k not in SECRET_KEYS},
        "readonly_keys": sorted(PROTECTED_KEYS),
        "schedule_keys": sorted(SCHEDULE_KEYS),
        # §10: the UI must warn while the factory PIN is still in place.
        "pin_is_default": is_default_pin(pin_hash),
    }


@router.put("/config/{key}")
async def update_config(
    key: str, payload: ConfigValue, db: AsyncSession = Depends(get_db)
):
    """Update one setting (§9).

    Values are read from the DB on each use, so most changes take effect on
    the next job run with no restart. Schedule changes additionally rebuild
    the scheduler's triggers.
    """
    if key in SECRET_KEYS:
        raise HTTPException(
            status_code=403,
            detail=f"{key} cannot be set here; use POST /api/config/pin.",
        )
    if key in PROTECTED_KEYS:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{key} is not editable here; it changes only through the "
                f"PIN-gated stage switch."
            ),
        )
    if key not in CONFIG_DEFAULTS:
        raise HTTPException(status_code=404, detail=f"Unknown config key: {key}")

    expected = type(CONFIG_DEFAULTS[key])
    value = payload.value

    # Reject a type change that would blow up later inside a scheduled job,
    # where the failure would be far from its cause.
    if expected in (int, float) and isinstance(value, bool):
        raise HTTPException(status_code=422, detail=f"{key} expects a number.")
    if expected is float and isinstance(value, int):
        value = float(value)
    if not isinstance(value, expected):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} expects {expected.__name__}, got "
                f"{type(value).__name__}."
            ),
        )

    await set_config(db, key, value)
    await db.commit()

    rescheduled = False
    if key in SCHEDULE_KEYS:
        rescheduled = await scheduler_service.reschedule_jobs()

    logger.info("Config %s updated to %r (rescheduled=%s)", key, value, rescheduled)
    return {"key": key, "value": value, "rescheduled": rescheduled}


@router.post("/config/pin")
async def change_pin(payload: PinChange, db: AsyncSession = Depends(get_db)):
    """Change the stage PIN, requiring the current one (§8.3, §10)."""
    stored = await get_config(db, "stage_pin_hash", default="")

    if not verify_pin(payload.current_pin, stored):
        # Deliberately not distinguishing "wrong PIN" from "no PIN set".
        raise HTTPException(status_code=403, detail="Current PIN is incorrect.")

    if payload.new_pin == payload.current_pin:
        raise HTTPException(
            status_code=422, detail="New PIN must differ from the current one."
        )

    await set_config(db, "stage_pin_hash", hash_pin(payload.new_pin))
    await db.commit()

    logger.warning("Stage PIN changed.")
    return {
        "changed": True,
        "pin_is_default": payload.new_pin == DEFAULT_PIN,
    }
