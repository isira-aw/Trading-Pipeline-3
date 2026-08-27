"""Trades, wallet and open positions (§8.1, §9)."""

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Trade, WalletSnapshot
from app.db.session import get_db
from app.services import trading_engine as te
from app.services.binance_client import BinanceClientError, get_market_data_client
from app.services.config_service import get_config
from app.services.position_tracker import match_fifo

router = APIRouter()


@router.get("/trades")
async def list_trades(
    limit: int = 50, stage: str | None = None, symbol: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Recent trades, newest first (§8.1 recent trades feed)."""
    stage = stage or await get_config(db, "current_stage")

    query = select(Trade).where(Trade.stage == stage)
    if symbol:
        query = query.where(Trade.symbol == symbol)
    query = query.order_by(Trade.created_at.desc()).limit(min(limit, 500))

    rows = (await db.execute(query)).scalars().all()

    return {
        "trades": [
            {
                "id": str(row.id),
                "symbol": row.symbol,
                "side": row.side,
                "quantity": float(row.quantity),
                "price": float(row.price) if row.price is not None else None,
                "status": row.status,
                "risk_decision": row.risk_decision,
                "model_id": str(row.model_id) if row.model_id else None,
                "model_confidence": (
                    float(row.model_confidence) if row.model_confidence is not None else None
                ),
                "fee_usdt": float(row.fee_usdt or 0),
                "stop_price": float(row.stop_price) if row.stop_price is not None else None,
                "exit_reason": row.exit_reason,
                "created_at": row.created_at,
                "needs_attention": bool(
                    (row.risk_notes or {}).get("reconcile", {}).get("needs_attention")
                ),
            }
            for row in rows
        ]
    }


@router.get("/positions")
async def open_positions(db: AsyncSession = Depends(get_db)):
    """Open lots with live prices and unrealized P&L (§8.1).

    Current price comes from the exchange; when it is unreachable the
    position is still listed with a null price rather than omitted, so a
    dead feed never makes a real position disappear from the dashboard.
    """
    stage = await get_config(db, "current_stage")
    records = await te.load_trade_records(db, stage)
    lots = match_fifo(records).open_lots

    market = get_market_data_client()
    symbols = {lot.symbol for lot in lots}

    async def _price(symbol: str) -> tuple[str, float | None]:
        try:
            return symbol, await asyncio.to_thread(market.get_symbol_price, symbol)
        except (BinanceClientError, Exception):  # noqa: BLE001
            return symbol, None

    priced = await asyncio.gather(*(_price(symbol) for symbol in symbols))
    prices: dict[str, float | None] = dict(priced)

    positions = []
    for lot in lots:
        price = prices.get(lot.symbol)
        unrealized = (
            (price - lot.price) * lot.remaining if price is not None else None
        )
        positions.append({
            "symbol": lot.symbol,
            "quantity": lot.remaining,
            "entry_price": lot.price,
            "current_price": price,
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": (
                (unrealized / (lot.price * lot.remaining) * 100.0)
                if unrealized is not None and lot.price else None
            ),
            "opened_at": lot.opened_at,
            "model_id": lot.model_id,
            "stop_price": lot.stop_price,
            # How far price can fall before the stop closes this position.
            "stop_distance_pct": (
                (lot.price - lot.stop_price) / lot.price * 100.0
                if lot.stop_price else None
            ),
        })

    return {"positions": positions}


@router.get("/wallet")
async def wallet(db: AsyncSession = Depends(get_db)):
    """Balances plus the snapshot history behind the sparkline (§8.1)."""
    stage = await get_config(db, "current_stage")

    snapshots = (
        await db.execute(
            select(WalletSnapshot)
            .where(WalletSnapshot.stage == stage)
            .order_by(WalletSnapshot.snapshot_at.desc())
            .limit(200)
        )
    ).scalars().all()

    latest = snapshots[0] if snapshots else None

    live_error = None
    try:
        state = await te.get_account_state(db, stage)
        balances = state.balances
        total = state.total_value_usdt
    except te.TradingEngineError as exc:
        # Fall back to the last snapshot rather than showing nothing, but
        # say so — a stale number presented as live would be misleading.
        live_error = str(exc)
        balances = latest.balances if latest else {}
        total = float(latest.total_value_usdt) if latest else 0.0

    return {
        "stage": stage,
        "balances": balances,
        "total_value_usdt": total,
        "live": live_error is None,
        "live_error": live_error,
        "history": [
            {"at": s.snapshot_at, "total_value_usdt": float(s.total_value_usdt)}
            for s in reversed(snapshots)
        ],
    }


@router.get("/performance")
async def performance(db: AsyncSession = Depends(get_db)):
    """Realized stats for the current stage (§5.2)."""
    stage = await get_config(db, "current_stage")
    stats = await te.get_realized_performance(db, stage)
    stats["stage"] = stage
    return stats
