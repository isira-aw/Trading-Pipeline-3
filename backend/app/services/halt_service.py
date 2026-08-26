"""Emergency stop and liquidation (§7).

The halt is a **flag that overrides the stage**, not a stage value. Writing
'halted' into `current_stage` would erase which stage was running, leaving
Resume to guess between paper and live — and guessing 'live' would put real
money back to work without the gate.

Emergency stop **freezes, it does not sell**. Liquidating at market during
whatever caused the panic is its own risk, and doing it automatically takes
that decision away from the operator at the worst moment. Selling is a
separate, explicitly confirmed action.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance_client import BinanceClientError, get_trading_client
from app.services.config_service import get_config, set_config

logger = logging.getLogger(__name__)

TRADE_LOOP_JOB = "trade_loop"


@dataclass
class HaltState:
    halted: bool
    halted_at: str | None = None
    reason: str | None = None
    stage: str = "setup"


@dataclass
class CancelResult:
    cancelled: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    reachable: bool = True
    error: str | None = None


async def get_halt_state(db: AsyncSession) -> HaltState:
    return HaltState(
        halted=bool(await get_config(db, "halted")),
        halted_at=await get_config(db, "halted_at", default="") or None,
        reason=await get_config(db, "halted_reason", default="") or None,
        stage=await get_config(db, "current_stage"),
    )


async def cancel_all_open_orders(db: AsyncSession, stage: str) -> CancelResult:
    """Cancel every open order on the exchange (§7 step 2).

    A failure here does NOT abort the halt. The halt's job is to stop this
    system placing anything further, and that must succeed even when the
    exchange is unreachable — which is a likely reason for hitting the
    button in the first place. Failures are reported so the operator knows
    orders may still be resting.
    """
    result = CancelResult()
    client = get_trading_client(stage)
    symbols = await get_config(db, "symbols")

    try:
        open_orders = client.client.get_open_orders()
    except (BinanceClientError, Exception) as exc:  # noqa: BLE001
        logger.error("Could not list open orders during halt: %s", exc)
        return CancelResult(reachable=False, error=str(exc))

    for order in open_orders:
        symbol = order.get("symbol")
        order_id = order.get("orderId")
        try:
            client.client.cancel_order(symbol=symbol, orderId=order_id)
            result.cancelled.append({"symbol": symbol, "order_id": order_id})
            logger.warning("Cancelled open order %s on %s", order_id, symbol)
        except Exception as exc:  # noqa: BLE001
            result.failures.append(
                {"symbol": symbol, "order_id": order_id, "error": str(exc)}
            )
            logger.error("Could not cancel order %s on %s: %s", order_id, symbol, exc)

    if not open_orders:
        logger.info("No open orders to cancel (symbols configured: %s).", symbols)

    return result


def pause_trade_loop() -> bool:
    """Pause only the trade-loop job (§7 step 4).

    Training and data jobs keep running deliberately: neither can move
    money, and stopping them would mean coming back from a halt with stale
    candles and no fresh model.
    """
    from app.services import scheduler as scheduler_service

    scheduler = scheduler_service.scheduler
    if scheduler is None or not scheduler.running:
        return False

    job = scheduler.get_job(TRADE_LOOP_JOB)
    if job is None:
        return False

    job.pause()
    logger.warning("Trade loop job paused by emergency stop.")
    return True


def resume_trade_loop() -> bool:
    from app.services import scheduler as scheduler_service

    scheduler = scheduler_service.scheduler
    if scheduler is None or not scheduler.running:
        return False

    job = scheduler.get_job(TRADE_LOOP_JOB)
    if job is None:
        return False

    job.resume()
    logger.warning("Trade loop job resumed.")
    return True


async def engage_halt(
    db: AsyncSession, reason: str = "Emergency stop triggered from the dashboard."
) -> dict:
    """Halt trading (§7). Caller commits.

    Order matters: the flag is set and committed FIRST, so that even if
    cancelling orders fails or hangs, the system is already refusing to
    place anything new.
    """
    now = datetime.now(timezone.utc)
    stage = await get_config(db, "current_stage")

    await set_config(db, "halted", True)
    await set_config(db, "halted_at", now.isoformat())
    await set_config(db, "halted_reason", reason)
    await set_config(db, "trading_enabled", False)
    await db.commit()

    paused = pause_trade_loop()
    cancels = await cancel_all_open_orders(db, stage)

    logger.warning(
        "EMERGENCY STOP engaged at %s (stage was %s). Cancelled %d order(s).",
        now.isoformat(), stage, len(cancels.cancelled),
    )

    return {
        "halted": True,
        "halted_at": now,
        "stage_when_halted": stage,
        "trade_loop_paused": paused,
        "orders_cancelled": cancels.cancelled,
        "cancel_failures": cancels.failures,
        "exchange_reachable": cancels.reachable,
        "cancel_error": cancels.error,
        # §7 step 3: freeze only.
        "holdings_liquidated": False,
        "note": (
            "Holdings were NOT sold. Use the separate Liquidate action if you "
            "want to exit positions."
        ),
    }


async def release_halt(db: AsyncSession) -> dict:
    """Clear the halt (§7 step 5). Caller has already verified the PIN.

    Trading is left stopped. Coming back from an emergency stop should be
    two deliberate acts — clear the halt, then start trading — not one
    button that resumes placing orders.
    """
    state = await get_halt_state(db)

    await set_config(db, "halted", False)
    await set_config(db, "halted_at", "")
    await set_config(db, "halted_reason", "")
    await set_config(db, "trading_enabled", False)
    await db.commit()

    resumed = resume_trade_loop()
    logger.warning("Halt released; stage is %s. Trading remains stopped.", state.stage)

    return {
        "halted": False,
        "stage": state.stage,
        "was_halted_at": state.halted_at,
        "trade_loop_resumed": resumed,
        "trading_enabled": False,
        "note": "Trading is still stopped. Start it deliberately when ready.",
    }


# --------------------------------------------------------------------------
# Liquidation (§7 step 6)
# --------------------------------------------------------------------------


async def liquidate_all(
    db: AsyncSession, stage: str, now: datetime | None = None
) -> dict:
    """Sell every non-quote holding to USDT at market.

    Goes through `trading_engine.place_order`, the same path as any other
    trade, so each sale is risk-assessed and lands in `trades`,
    `risk_log` and `wallet_snapshots` identically. A dedicated bypass would
    produce exactly the trades an operator most needs a record of, with no
    record of them.

    Deliberately permitted while halted: the halt stops the system opening
    new risk, and this is the button for deciding to exit instead.
    """
    from app.services import trading_engine as te
    from app.services.binance_client import get_market_data_client
    from app.services.risk_engine import TradeProposal

    try:
        state = await te.get_account_state(db, stage)
    except te.TradingEngineError as exc:
        logger.error("Liquidation could not read account state: %s", exc)
        return {"liquidated": False, "error": str(exc), "sales": []}

    market = get_market_data_client()
    sales = []

    for asset, amount in sorted(state.balances.items()):
        if asset == te.QUOTE_ASSET or amount <= 0:
            continue

        symbol = f"{asset}{te.QUOTE_ASSET}"
        try:
            price = market.get_symbol_price(symbol)
        except (BinanceClientError, Exception) as exc:  # noqa: BLE001
            # An asset with no USDT pair cannot be sold this way; say so
            # rather than silently leaving it out of the report.
            logger.error("Cannot price %s for liquidation: %s", symbol, exc)
            sales.append({
                "symbol": symbol, "quantity": amount, "placed": False,
                "reason": f"No price available: {exc}",
            })
            continue

        proposal = TradeProposal(
            symbol=symbol, side="sell", quantity=amount, price=price,
            confidence=None, model_id=None,
        )
        try:
            outcome = await te.place_order(
                db, proposal, stage, now, exit_reason="liquidation"
            )
            sales.append({
                "symbol": symbol,
                "quantity": amount,
                "placed": outcome.placed,
                "status": outcome.status,
                "filled_quantity": outcome.filled_quantity,
                "fill_price": outcome.fill_price,
                "trade_id": outcome.trade_id,
                "reason": outcome.reason,
            })
        except Exception as exc:  # noqa: BLE001
            # One asset failing must not abandon the rest mid-liquidation.
            logger.exception("Liquidation failed for %s", symbol)
            await db.rollback()
            sales.append({
                "symbol": symbol, "quantity": amount, "placed": False,
                "reason": str(exc),
            })

    placed = [s for s in sales if s.get("placed")]
    logger.warning(
        "Liquidation: %d of %d holdings sold.", len(placed), len(sales)
    )

    return {
        "liquidated": bool(placed),
        "sales": sales,
        "placed_count": len(placed),
        "attempted_count": len(sales),
    }
