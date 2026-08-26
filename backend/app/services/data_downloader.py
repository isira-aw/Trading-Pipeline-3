"""Historical candle downloader (§5.1).

Pulls klines from Binance and upserts them into `candles`. Symbol list,
interval and history length all come from the `config` table — never from
hardcoded literals (Core Principle #1).

Session ownership: every entry point here opens its own AsyncSession. These
run as background tasks, which outlive the request that scheduled them, so a
session injected via `Depends(get_db)` would already be closed by the time
the work runs.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Candle
from app.db.session import AsyncSessionLocal
from app.services.binance_client import get_market_data_client
from app.services.config_service import get_config
from app.services.event_bus import EVENT_DATA_DOWNLOAD, bus

logger = logging.getLogger(__name__)

# Latest download progress, for the poll fallback when a client has no
# WebSocket (§9). In-memory: it describes a run in this process, and a
# restart means there is no run to report on.
_progress: dict = {"running": False, "symbols": [], "completed": 0, "current": None}


def get_progress() -> dict:
    """Snapshot of the current or most recent download."""
    total = len(_progress.get("symbols") or [])
    done = _progress.get("completed", 0)
    return {
        **_progress,
        "total": total,
        "progress": (done / total) if total else 0.0,
    }


def _publish(symbol: str | None, completed: int, total: int, phase: str) -> None:
    _progress.update(
        {"current": symbol, "completed": completed, "running": phase != "complete"}
    )
    bus.publish(EVENT_DATA_DOWNLOAD, {
        "symbol": symbol,
        "phase": phase,
        "completed": completed,
        "total": total,
        "progress": (completed / total) if total else 0.0,
    })

# Binance returns klines as positional arrays.
KLINE_OPEN_TIME = 0
KLINE_OPEN = 1
KLINE_HIGH = 2
KLINE_LOW = 3
KLINE_CLOSE = 4
KLINE_VOLUME = 5

# Rows per INSERT. Postgres caps a statement at 65535 bind parameters and
# each candle binds 8, so batches keep large backfills under that ceiling.
UPSERT_BATCH_SIZE = 1000


async def _fetch_klines(symbol: str, interval: str, start_str: str) -> list:
    """Fetch klines off the event loop — python-binance is synchronous."""
    client = get_market_data_client()
    return await asyncio.to_thread(
        client.get_historical_klines, symbol, interval, start_str
    )


def _to_candle_rows(klines: list, symbol: str, interval: str) -> list[dict]:
    rows = []
    for k in klines:
        rows.append(
            {
                "symbol": symbol,
                "interval": interval,
                "open_time": datetime.fromtimestamp(
                    k[KLINE_OPEN_TIME] / 1000.0, tz=timezone.utc
                ),
                # Numeric columns: keep the exchange's decimal strings rather
                # than round-tripping through float, which loses precision.
                "open": k[KLINE_OPEN],
                "high": k[KLINE_HIGH],
                "low": k[KLINE_LOW],
                "close": k[KLINE_CLOSE],
                "volume": k[KLINE_VOLUME],
            }
        )
    return rows


async def _upsert_candles(db: AsyncSession, rows: list[dict]) -> int:
    """Insert candles, skipping ones already stored. Historical candles are
    immutable once closed, so a conflict means we already have it."""
    written = 0
    for start in range(0, len(rows), UPSERT_BATCH_SIZE):
        batch = rows[start : start + UPSERT_BATCH_SIZE]
        stmt = insert(Candle).values(batch)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["symbol", "interval", "open_time"]
        )
        result = await db.execute(stmt)
        written += result.rowcount or 0
    return written


async def download_symbol(
    db: AsyncSession,
    symbol: str,
    interval: str | None = None,
    history_years: int | None = None,
) -> dict:
    """Download and store history for one symbol. Caller owns the session."""
    if interval is None:
        interval = await get_config(db, "interval")
    if history_years is None:
        history_years = await get_config(db, "history_years")

    start_time = datetime.now(timezone.utc) - timedelta(days=365 * history_years)
    start_str = start_time.strftime("%d %b, %Y")

    logger.info("Downloading %s %s candles from %s", symbol, interval, start_str)
    klines = await _fetch_klines(symbol, interval, start_str)

    if not klines:
        logger.warning("No klines returned for %s %s", symbol, interval)
        return {"symbol": symbol, "fetched": 0, "inserted": 0}

    rows = _to_candle_rows(klines, symbol, interval)
    inserted = await _upsert_candles(db, rows)
    await db.commit()

    logger.info(
        "%s: fetched %d klines, inserted %d new", symbol, len(rows), inserted
    )
    return {"symbol": symbol, "fetched": len(rows), "inserted": inserted}


async def download_historical_data(
    symbols: list[str] | None = None,
    interval: str | None = None,
    history_years: int | None = None,
) -> dict:
    """Background-task entry point: download every configured symbol.

    Opens its own session — see the module docstring on session ownership.
    A failure on one symbol is logged and does not abort the others.
    """
    async with AsyncSessionLocal() as db:
        if symbols is None:
            symbols = await get_config(db, "symbols")
        if interval is None:
            interval = await get_config(db, "interval")
        if history_years is None:
            history_years = await get_config(db, "history_years")

        total = len(symbols)
        _progress.update({"running": True, "symbols": list(symbols), "completed": 0})

        results = []
        for index, symbol in enumerate(symbols):
            _publish(symbol, index, total, "downloading")
            try:
                results.append(
                    await download_symbol(db, symbol, interval, history_years)
                )
                _publish(symbol, index + 1, total, "symbol_complete")
            except Exception:
                # §1.7 fail loudly, but one bad symbol must not sink the rest.
                logger.exception("Download failed for %s", symbol)
                await db.rollback()
                results.append({"symbol": symbol, "error": True})
                _publish(symbol, index + 1, total, "symbol_failed")

        _publish(None, total, total, "complete")

    return {"results": results}
