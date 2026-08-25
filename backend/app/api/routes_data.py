from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.services.data_downloader import download_historical_data

router = APIRouter()

@router.post("/download")
async def trigger_data_download(
    background_tasks: BackgroundTasks, 
    symbol: str = "BTCUSDT",
    interval: str = "4h",
    years: int = 2,
    db: AsyncSession = Depends(get_db)
):
    # Run the data downloader as a background task
    background_tasks.add_task(download_historical_data, db, symbol, interval, years)
    return {"status": "ok", "message": f"Data download started in the background for {symbol}."}
