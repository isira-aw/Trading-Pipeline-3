"""FastAPI entrypoint (§2, §9)."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_config import router as config_router
from app.api.routes_data import router as data_router
from app.api.routes_models import router as models_router
from app.api.routes_system import router as system_router
from app.api.routes_trades import router as trades_router
from app.api.routes_ws import router as ws_router
from app.services.scheduler import (
    run_startup_reconciliation,
    shutdown_scheduler,
    start_scheduler,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Reconcile before scheduling, then start the jobs.

    Order matters: unresolved orders are resolved first so the trade loop
    never runs against an account view with unknown fills in it (§1.7).
    The trade loop itself stays gated on `trading_enabled`, so starting the
    scheduler does not by itself start trading.
    """
    try:
        await run_startup_reconciliation()
        await start_scheduler()
    except Exception:  # noqa: BLE001
        # A scheduler that fails to start must not take the API down with
        # it — the dashboard is how the operator finds out and recovers.
        logger.exception("Scheduler failed to start; API is up without it.")

    yield

    await shutdown_scheduler()


app = FastAPI(
    title="Automated Crypto Trading Pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

# §10: the dashboard is assumed localhost-only, with no auth layer.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system_router, prefix="/api", tags=["System"])
app.include_router(models_router, prefix="/api", tags=["Models"])
app.include_router(config_router, prefix="/api", tags=["Config"])
app.include_router(trades_router, prefix="/api", tags=["Trades"])
app.include_router(data_router, prefix="/api/data", tags=["Data"])
app.include_router(ws_router, tags=["WebSocket"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}
