from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import settings

# Async Engine
# We replace postgresql:// with postgresql+asyncpg:// if not already set
async_db_url = settings.DATABASE_URL
if async_db_url.startswith("postgresql://"):
    async_db_url = async_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

# Sized for the dashboard's poll fan-out (7 concurrent DB-touching endpoints
# per tick) plus headroom for the scheduler/trade loop running alongside it.
# The SQLAlchemy default (pool_size=5, max_overflow=10 => 15 total) is easy
# to exhaust with a single tab and causes pool_timeout errors under load.
engine = create_async_engine(
    async_db_url,
    echo=False,
    pool_size=20,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Sync engine for Alembic and blocking scripts
sync_db_url = settings.DATABASE_URL
sync_engine = create_engine(sync_db_url, echo=False, pool_size=5, max_overflow=5, pool_recycle=1800)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
