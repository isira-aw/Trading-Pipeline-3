from datetime import datetime, timezone, timedelta
import asyncio
from app.services.binance_client import BinanceAPIClient
from app.db.models import Candle
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

async def download_historical_data(db: AsyncSession, symbol: str = "BTCUSDT", interval: str = "4h", years: int = 2):
    # This acts as the data downloader service
    client = BinanceAPIClient(testnet=True)
    
    # Calculate start date based on years requested
    start_time = datetime.now(timezone.utc) - timedelta(days=365 * years)
    start_str = start_time.strftime("%d %b, %Y")
    
    print(f"Downloading {symbol} data from {start_str}...")
    
    # Fetch klines (blocking call, in a real async environment we'd run this in an executor)
    # python-binance is sync by default; we can use AsyncClient from binance.async_client if preferred,
    # but since this is a background job, running it in executor is fine. For now, we just call it.
    loop = asyncio.get_event_loop()
    klines = await loop.run_in_executor(
        None, 
        lambda: client.get_historical_klines(symbol, interval, start_str)
    )
    
    if not klines:
        print("No data fetched.")
        return 0

    print(f"Fetched {len(klines)} klines. Upserting to database...")
    
    # Klines format: [Open time, Open, High, Low, Close, Volume, Close time, Quote asset volume, Number of trades, Taker buy base asset volume, Taker buy quote asset volume, Ignore]
    candles = []
    for k in klines:
        open_time = datetime.fromtimestamp(k[0] / 1000.0, tz=timezone.utc)
        candles.append({
            "symbol": symbol,
            "interval": interval,
            "open_time": open_time,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    
    # Upsert data using PostgreSQL's ON CONFLICT DO NOTHING (or DO UPDATE)
    # We use DO NOTHING since historical candles shouldn't change
    stmt = insert(Candle).values(candles)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=['symbol', 'interval', 'open_time']
    )
    
    await db.execute(stmt)
    await db.commit()
    
    print("Download and upsert complete.")
    return len(candles)
