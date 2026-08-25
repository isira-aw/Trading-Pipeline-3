"""Wallet snapshots (§3, §8.1).

Snapshots are the source of the dashboard's value sparkline and, more
importantly, the baseline the risk engine's daily loss cap measures against.

The risk engine rejects trades when it cannot establish a day-open baseline
(§1.7: absent data is not healthy data). `ensure_daily_baseline` is what
keeps that from being a routine occurrence — call it on startup and from the
heartbeat job so a snapshot always exists before any trade is attempted.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WalletSnapshot

logger = logging.getLogger(__name__)


async def take_snapshot(
    db: AsyncSession,
    stage: str,
    balances: dict,
    total_value_usdt: float,
    now: datetime | None = None,
) -> WalletSnapshot:
    """Record current wallet state. Caller commits."""
    snapshot = WalletSnapshot(
        stage=stage,
        balances=balances,
        total_value_usdt=total_value_usdt,
        snapshot_at=now or datetime.now(timezone.utc),
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


async def get_latest_snapshot(
    db: AsyncSession, stage: str
) -> WalletSnapshot | None:
    return (
        await db.execute(
            select(WalletSnapshot)
            .where(WalletSnapshot.stage == stage)
            .order_by(WalletSnapshot.snapshot_at.desc())
            .limit(1)
        )
    ).scalars().first()


async def ensure_daily_baseline(
    db: AsyncSession,
    stage: str,
    balances: dict,
    total_value_usdt: float,
    now: datetime | None = None,
) -> WalletSnapshot | None:
    """Guarantee a usable day-open baseline exists for today.

    Writes a snapshot when none has been taken since the UTC day boundary.
    Returns the new snapshot, or None when one already existed.

    Call this on startup and from the heartbeat job. Without it, the first
    trade after a restart is rejected for having no baseline — correct, but
    avoidable.
    """
    now = now or datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    existing = (
        await db.execute(
            select(WalletSnapshot.id)
            .where(
                WalletSnapshot.stage == stage,
                WalletSnapshot.snapshot_at >= start_of_day,
            )
            .limit(1)
        )
    ).scalars().first()

    if existing is not None:
        return None

    logger.info("No wallet snapshot yet today for stage '%s'; taking one.", stage)
    return await take_snapshot(db, stage, balances, total_value_usdt, now)
