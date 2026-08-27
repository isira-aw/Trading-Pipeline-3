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
from app.services import job_runs
from app.services.binance_client import get_market_data_client
from app.services.config_service import get_config
from app.services.event_bus import EVENT_DATA_DOWNLOAD, bus

logger = logging.getLogger(__name__)

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
    job_id=None,
) -> dict:
    """Background-task entry point: download every configured symbol.

    Opens its own session — see the module docstring on session ownership.
    A failure on one symbol is logged and does not abort the others.

    `job_id` is the `job_runs` row to report progress against. The API
    route creates this row *before* scheduling the background task (so a
    status poll right after the POST already sees a "running" row rather
    than racing this task for it); the scheduled daily refresh has no
    caller to do that, so it creates its own row here instead. This
    supersedes an earlier in-memory progress dict, which didn't survive a
    backend restart and couldn't answer "last run" the way an audit-trail
    row can (and didn't cover training at all).
    """
    async with AsyncSessionLocal() as db:
        if symbols is None:
            symbols = await get_config(db, "symbols")
        if interval is None:
            interval = await get_config(db, "interval")
        if history_years is None:
            history_years = await get_config(db, "history_years")

        if job_id is None:
            job = await job_runs.start_job(
                db, job_runs.JOB_DOWNLOAD,
                detail={"symbols": symbols, "total": len(symbols), "completed": []},
            )
            await db.commit()
            job_id = job.id

        results = []
        had_error = False
        for i, symbol in enumerate(symbols):
            if i > 0:
                # A gap between symbols, not just within one symbol's own
                # pagination (python-binance already sleeps every 3rd page).
                # Weight is shared per-IP across all symbols, so back-to-back
                # symbols with no gap is what actually exceeds it.
                await asyncio.sleep(2)
            try:
                result = await download_symbol(db, symbol, interval, history_years)
            except Exception:
                # §1.7 fail loudly, but one bad symbol must not sink the rest.
                logger.exception("Download failed for %s", symbol)
                await db.rollback()
                result = {"symbol": symbol, "error": True}
                had_error = True

            results.append(result)
            progress = len(results) / len(symbols)
            bus.publish(EVENT_DATA_DOWNLOAD, {
                "job_id": str(job_id), "status": "running", "progress": progress,
                "completed": len(results), "total": len(symbols),
                "symbol": symbol, "result": result,
            })
            await job_runs.update_job(
                db, job_id, progress=progress,
                # A copy, not `results` itself: `results` keeps growing on
                # later iterations, and SQLAlchemy's dirty-check compares
                # the new JSONB value against the previous one by content —
                # a shared, still-mutating list would compare equal to
                # itself and the UPDATE would be silently skipped.
                detail={"symbols": symbols, "total": len(symbols), "completed": list(results)},
            )
            await db.commit()

        final_status = job_runs.STATUS_FAILED if had_error else job_runs.STATUS_SUCCESS
        await job_runs.finish_job(
            db, job_id, status=final_status,
            detail={"symbols": symbols, "total": len(symbols), "completed": list(results)},
        )
        await db.commit()
        bus.publish(EVENT_DATA_DOWNLOAD, {
            "job_id": str(job_id), "status": final_status, "progress": 1.0,
            "completed": len(results), "total": len(symbols),
        })

    return {"results": results, "job_id": str(job_id)}
