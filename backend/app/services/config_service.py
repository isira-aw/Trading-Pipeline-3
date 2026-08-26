"""Read/write access to the DB-backed `config` table (§3).

Core Principle #1: no hardcoded operational values. Every runtime knob is
read through here so that a change saved from the Settings page takes
effect without a restart.

``CONFIG_DEFAULTS`` is only a fallback for a key that is missing from the
table entirely (e.g. a key added by a newer version of the code before its
seed migration has run) — the DB always wins when the row exists.
"""

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config_defaults import CONFIG_DEFAULTS
from app.db.models import Config

# Binance spot symbols are uppercase alphanumeric and end in one of a small
# set of quote assets. 6-20 chars covers everything from "BTCUSDT" up to the
# longest realistic pair while still rejecting garbage.
_QUOTE_ASSETS = ("USDT", "BUSD", "USDC", "BTC", "ETH", "BNB")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{5,19}$")


class ConfigKeyError(KeyError):
    """Raised when a config key exists neither in the DB nor in the defaults."""


class ConfigValueError(ValueError):
    """Raised when a value fails validation for its config key."""


def _validate_symbol(symbol: Any) -> str:
    if not isinstance(symbol, str):
        raise ConfigValueError(f"Symbol must be a string, got {symbol!r}")
    if not _SYMBOL_RE.match(symbol):
        raise ConfigValueError(
            f"Invalid symbol {symbol!r}: must be 6-20 uppercase "
            "alphanumeric characters (e.g. 'BTCUSDT')"
        )
    if not symbol.endswith(_QUOTE_ASSETS):
        raise ConfigValueError(
            f"Invalid symbol {symbol!r}: must end in a supported quote "
            f"asset {_QUOTE_ASSETS}"
        )
    return symbol


def _validate_symbols(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigValueError("symbols must be a non-empty list of strings")
    return [_validate_symbol(s) for s in value]


# Per-key validators, applied in `set_config` before anything touches the DB.
_VALIDATORS = {
    "symbols": _validate_symbols,
}


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
    """Upsert one config value. Caller commits.

    Raises ``ConfigValueError`` for a key with a registered validator
    (e.g. "symbols") when the value fails validation. This is the single
    choke point every config write funnels through, so validating here
    protects the DB regardless of which route or script calls in.
    """
    validator = _VALIDATORS.get(key)
    if validator is not None:
        value = validator(value)

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
