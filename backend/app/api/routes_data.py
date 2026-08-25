from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Candle
from app.db.session import get_db
from app.services.config_service import get_config
from app.services.data_downloader import download_historical_data

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

    The background task deliberately receives no DB session: the
    request-scoped one is closed as soon as this response is sent, well
    before the task commits. It opens its own instead.
    """
    symbols = [symbol] if symbol else await get_config(db, "symbols")

    background_tasks.add_task(download_historical_data, symbols, interval, years)

    return {
        "status": "started",
        "symbols": symbols,
        "message": f"Data download started in the background for {', '.join(symbols)}.",
    }


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
