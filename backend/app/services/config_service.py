"""Read/write access to the DB-backed `config` table (§3).

Core Principle #1: no hardcoded operational values. Every runtime knob is
read through here so that a change saved from the Settings page takes
effect without a restart.

``CONFIG_DEFAULTS`` is only a fallback for a key that is missing from the
table entirely (e.g. a key added by a newer version of the code before its
seed migration has run) — the DB always wins when the row exists.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config_defaults import CONFIG_DEFAULTS
from app.db.models import Config


class ConfigKeyError(KeyError):
    """Raised when a config key exists neither in the DB nor in the defaults."""


async def get_config(db: AsyncSession, key: str, default: Any = ...) -> Any:
    """Fetch one config value, falling back to the seeded default."""
    result = await db.execute(select(Config.value).where(Config.key == key))
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    if key in CONFIG_DEFAULTS:
        return CONFIG_DEFAULTS[key]
    if default is not ...:
        return default
    raise ConfigKeyError(f"Unknown config key: {key!r}")


async def get_all_config(db: AsyncSession) -> dict[str, Any]:
    """Fetch every config value, with defaults filled in for missing keys."""
    result = await db.execute(select(Config.key, Config.value))
    stored = {key: value for key, value in result.all()}
    return {**CONFIG_DEFAULTS, **stored}


async def set_config(db: AsyncSession, key: str, value: Any) -> None:
    """Upsert one config value. Caller commits."""
    # `onupdate` on the model only fires for ORM updates, so updated_at is
    # set explicitly here — this is a Core-level upsert.
    stmt = (
        insert(Config)
        .values(key=key, value=value)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": value, "updated_at": func.now()},
        )
    )
    await db.execute(stmt)
