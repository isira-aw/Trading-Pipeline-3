from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Candle
from app.db.session import get_db
from app.services import job_runs
from app.services.config_service import get_config
from app.services.data_downloader import download_historical_data
from app.services.event_bus import EVENT_DATA_DOWNLOAD, bus

router = APIRouter()


@router.post("/download")
async def trigger_data_download(
    background_tasks: BackgroundTasks,
    symbol: str | None = None,
    interval: str | None = None,
    years: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Kick off a historical download (§5.1).

    With no arguments this downloads every symbol in the `config` table.
    The optional query params are a manual override for one-off backfills.

    The `job_runs` row is created here, before the background task is
    scheduled, so a client that polls GET /download/status right after this
    returns always sees a "running" row rather than racing the background
    task for it (the frontend's Download button stays disabled until this
    job reaches a terminal status, not until the POST resolves).

    The background task deliberately receives no DB session: the
    request-scoped one is closed as soon as this response is sent, well
    before the task commits. It opens its own instead.
    """
    symbols = [symbol] if symbol else await get_config(db, "symbols")

    job = await job_runs.start_job(
        db, job_runs.JOB_DOWNLOAD,
        detail={"symbols": symbols, "total": len(symbols), "completed": []},
    )
    await db.commit()

    background_tasks.add_task(
        download_historical_data, symbols, interval, years, job.id
    )
    bus.publish(EVENT_DATA_DOWNLOAD, {
        "job_id": str(job.id), "status": "running", "progress": 0.0,
        "completed": 0, "total": len(symbols),
    })

    return {
        "status": "started",
        "job_id": str(job.id),
        "symbols": symbols,
        "message": f"Data download started in the background for {', '.join(symbols)}.",
    }


@router.get("/download/status")
async def data_download_status(db: AsyncSession = Depends(get_db)):
    """Latest download job — lets the dashboard show correct progress
    immediately on load rather than only after the next WS event (§8.1).
    Supersedes the old in-memory /download/progress poll fallback: this is
    DB-backed, so it also survives a backend restart."""
    job = await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD)
    return job_runs.to_dict(job)


@router.get("/status")
async def data_status(db: AsyncSession = Depends(get_db)):
    """Per-symbol candle counts and coverage — lets the dashboard (and a
    human) confirm what actually landed in `candles`."""
    result = await db.execute(
        select(
            Candle.symbol,
            Candle.interval,
            func.count(Candle.id),
            func.min(Candle.open_time),
            func.max(Candle.open_time),
        ).group_by(Candle.symbol, Candle.interval)
    )

    return {
        "symbols": [
            {
                "symbol": symbol,
                "interval": interval,
                "candle_count": count,
                "first_open_time": first,
                "last_open_time": last,
            }
            for symbol, interval, count, first, last in result.all()
        ]
    }
