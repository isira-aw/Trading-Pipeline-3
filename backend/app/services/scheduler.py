"""APScheduler jobs: the trade loop, retraining, heartbeat, reconciliation (§4).

This is what makes the system *run* rather than merely be callable. Jobs:

* **heartbeat** — refreshes `component_status` so the risk engine's health
  check and the dashboard's status strip reflect reality.
* **reconcile** — re-checks unresolved orders with backoff (see
  `trading_engine.reconcile_open_orders`).
* **trade loop** — the paper-stage entry/exit cycle.
* **retrain** — periodic model training and promotion.
* **data refresh** — daily candle top-up.

Every job body is wrapped so an exception is logged and surfaced on
`component_status` rather than propagating into APScheduler, which would
silently stop that job from ever running again (§1.7).

Startup order matters: reconciliation runs *before* the trade loop is
allowed to start, so the system never trades against an account view that
still has unknown orders in it.
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.db.session import AsyncSessionLocal
from app.services import trading_engine as te
from app.services.config_service import get_config
from app.services.event_bus import (
    EVENT_COMPONENT_STATUS,
    EVENT_SYSTEM,
    EVENT_TRADE,
    EVENT_TRAINING_PROGRESS,
    EVENT_WALLET,
    bus,
)
from app.services.model_registry import ModelRegistryError, promote_best_candidate
from app.services.position_tracker import match_fifo
from app.services.risk_engine import TradeProposal
from app.services.signal_generator import evaluate_exit, generate_signal
from app.services.training_pipeline import TrainingError, train_symbol
from app.services.wallet_service import ensure_daily_baseline

logger = logging.getLogger(__name__)

STAGE_HALTED = "halted"
STAGE_SETUP = "setup"

scheduler: AsyncIOScheduler | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _publish_component(db, component: str, status: str, detail: str) -> None:
    await te.set_component_status(db, component, status, detail)
    bus.publish(
        EVENT_COMPONENT_STATUS,
        {"component": component, "status": status, "detail": detail},
    )


async def trading_allowed(db) -> tuple[bool, str]:
    """Whether the trade loop may place orders right now.

    Halted overrides whichever stage was active (§7), and `trading_enabled`
    is the dashboard's Start/Stop control.
    """
    stage = await get_config(db, "current_stage")

    if stage == STAGE_HALTED:
        return False, "Trading is halted (emergency stop active)."
    if stage == STAGE_SETUP:
        return False, "Stage is 'setup'; switch to paper to begin trading."
    if stage == te.STAGE_LIVE:
        return False, (
            "Live stage is not enabled: the promotion gate is not implemented."
        )
    if not await get_config(db, "trading_enabled"):
        return False, "Trading is stopped (start it from the dashboard)."

    return True, stage


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


async def heartbeat_job() -> None:
    """Refresh component heartbeats (§6, §8.1)."""
    async with AsyncSessionLocal() as db:
        try:
            await _publish_component(db, "scheduler", "online", "Scheduler running.")
            # The engine is alive by virtue of this code executing; it never
            # gates on its own heartbeat (see risk_engine).
            await _publish_component(db, "risk_engine", "online", "Ready.")

            stage = await get_config(db, "current_stage")
            try:
                state = await te.get_account_state(db, stage)
                await _publish_component(
                    db, "binance_api", "online",
                    f"Account reachable; {state.total_value_usdt:.2f} USDT total.",
                )
                await ensure_daily_baseline(
                    db, stage, state.balances, state.total_value_usdt
                )
            except te.TradingEngineError as exc:
                await _publish_component(db, "binance_api", "offline", str(exc))

            await _refresh_data_feed_status(db)
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Heartbeat job failed")
            await db.rollback()


async def _refresh_data_feed_status(db) -> None:
    """Data feed is healthy when candles are recent enough to trade on."""
    from sqlalchemy import func, select

    from app.db.models import Candle

    interval = await get_config(db, "interval")
    symbols = await get_config(db, "symbols")

    latest = (
        await db.execute(
            select(func.max(Candle.open_time)).where(
                Candle.symbol.in_(symbols), Candle.interval == interval
            )
        )
    ).scalar_one_or_none()

    if latest is None:
        await _publish_component(
            db, "data_feed", "offline", "No candles stored; run a data download."
        )
        return

    age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0
    from app.services.signal_generator import INTERVAL_MINUTES

    # Allow two intervals of lag before calling the feed stale.
    tolerance = INTERVAL_MINUTES.get(interval, 240) * 2 / 60.0

    if age_hours > tolerance:
        await _publish_component(
            db, "data_feed", "error",
            f"Newest candle is {age_hours:.1f}h old (tolerance {tolerance:.1f}h).",
        )
    else:
        await _publish_component(
            db, "data_feed", "online", f"Newest candle {age_hours:.1f}h old.",
        )


async def reconcile_job() -> None:
    """Resolve unresolved orders, with backoff and escalation."""
    async with AsyncSessionLocal() as db:
        try:
            stage = await get_config(db, "current_stage")
            if stage in (STAGE_SETUP, te.STAGE_LIVE):
                return
            results = await te.reconcile_open_orders(db, stage)
            for result in results:
                if result.get("needs_attention"):
                    bus.publish(EVENT_SYSTEM, {
                        "level": "error",
                        "message": (
                            f"Order {result['trade_id'][:8]} could not be "
                            f"reconciled after {result['attempts']} attempts — "
                            f"needs attention."
                        ),
                    })
        except Exception:  # noqa: BLE001
            logger.exception("Reconciliation job failed")
            await db.rollback()


async def trade_loop_job() -> None:
    """One pass of the paper trading cycle: exits first, then entries.

    Exits run first so capital freed by a close is available to the entries
    in the same pass.
    """
    async with AsyncSessionLocal() as db:
        try:
            allowed, reason = await trading_allowed(db)
            if not allowed:
                logger.debug("Trade loop skipped: %s", reason)
                return

            stage = reason  # trading_allowed returns the stage when allowed
            symbols = await get_config(db, "symbols")

            await _process_exits(db, stage, symbols)
            await _process_entries(db, stage, symbols)

        except Exception:  # noqa: BLE001
            # Never let this escape into APScheduler: the job would be
            # dropped and the loop would stop with no visible cause.
            logger.exception("Trade loop failed")
            await db.rollback()
            async with AsyncSessionLocal() as alert_db:
                await _publish_component(
                    alert_db, "scheduler", "error", "Trade loop raised; see logs."
                )
                await alert_db.commit()


async def _process_entries(db, stage: str, symbols: list[str]) -> None:
    for symbol in symbols:
        try:
            signal = await generate_signal(db, symbol)
            if not signal.available:
                logger.info("No signal for %s: %s", symbol, signal.reason)
                continue

            min_confidence = await get_config(db, "min_confidence")
            if signal.confidence < min_confidence:
                logger.debug(
                    "%s confidence %.4f below floor %.2f; no entry.",
                    symbol, signal.confidence, min_confidence,
                )
                continue

            # Size the proposal at the full position limit and let the risk
            # engine resize it — the engine, not this loop, owns sizing.
            max_pct = await get_config(db, "max_position_pct")
            state = await te.get_account_state(db, stage)
            notional = state.total_value_usdt * max_pct / 100.0
            if notional <= 0 or signal.price <= 0:
                continue

            proposal = TradeProposal(
                symbol=symbol, side="buy", quantity=notional / signal.price,
                price=signal.price, confidence=signal.confidence,
                model_id=signal.model_id,
            )
            outcome = await te.place_order(db, proposal, stage)
            _publish_trade(symbol, "buy", outcome)

        except Exception:  # noqa: BLE001
            logger.exception("Entry evaluation failed for %s", symbol)
            await db.rollback()


async def _process_exits(db, stage: str, symbols: list[str]) -> None:
    interval = await get_config(db, "interval")
    target_move = await get_config(db, "target_move_pct")
    horizon = await get_config(db, "target_horizon_candles")

    records = await te.load_trade_records(db, stage)
    open_lots = match_fifo(records).open_lots

    for lot in open_lots:
        if lot.symbol not in symbols:
            continue
        try:
            signal = await generate_signal(db, lot.symbol)
            price = signal.price
            if price <= 0:
                continue

            decision = evaluate_exit(
                entry_price=lot.price, opened_at=lot.opened_at,
                remaining=lot.remaining, current_price=price,
                interval=interval, target_move_pct=target_move,
                horizon_candles=horizon,
            )
            if not decision.should_exit:
                continue

            proposal = TradeProposal(
                symbol=lot.symbol, side="sell", quantity=decision.quantity,
                price=price, confidence=None, model_id=None,
            )
            outcome = await te.place_order(db, proposal, stage)
            _publish_trade(lot.symbol, "sell", outcome, decision.reason)

        except Exception:  # noqa: BLE001
            logger.exception("Exit evaluation failed for %s", lot.symbol)
            await db.rollback()


def _publish_trade(symbol: str, side: str, outcome, note: str = "") -> None:
    bus.publish(EVENT_TRADE, {
        "symbol": symbol,
        "side": side,
        "placed": outcome.placed,
        "decision": outcome.decision,
        "status": outcome.status,
        "trade_id": outcome.trade_id,
        "filled_quantity": outcome.filled_quantity,
        "fill_price": outcome.fill_price,
        "reason": note or outcome.reason,
    })
    if outcome.filled_quantity:
        bus.publish(EVENT_WALLET, {"reason": "fill"})


async def retrain_job() -> None:
    """Retrain every configured symbol and promote the best candidate (§5.1)."""
    async with AsyncSessionLocal() as db:
        try:
            symbols = await get_config(db, "symbols")
        except Exception:  # noqa: BLE001
            logger.exception("Could not read symbols for retraining")
            return

    for symbol in symbols:
        async with AsyncSessionLocal() as db:
            try:
                bus.publish(EVENT_TRAINING_PROGRESS, {
                    "symbol": symbol, "phase": "started", "progress": 0.0,
                })
                result = await train_symbol(db, symbol)
                await db.commit()

                bus.publish(EVENT_TRAINING_PROGRESS, {
                    "symbol": symbol, "phase": "trained", "progress": 0.7,
                    "model_id": result["model_id"],
                    "metrics": {
                        k: result["metrics"].get(k)
                        for k in ("accuracy", "precision", "roc_auc")
                    },
                })

                promotion = await promote_best_candidate(db, symbol)
                await db.commit()

                bus.publish(EVENT_TRAINING_PROGRESS, {
                    "symbol": symbol, "phase": "complete", "progress": 1.0,
                    "promoted": promotion.get("promoted"),
                    "reason": promotion.get("reason"),
                })
            except (TrainingError, ModelRegistryError) as exc:
                logger.warning("Training skipped for %s: %s", symbol, exc)
                await db.rollback()
                bus.publish(EVENT_TRAINING_PROGRESS, {
                    "symbol": symbol, "phase": "failed", "progress": 1.0,
                    "reason": str(exc),
                })
            except Exception:  # noqa: BLE001
                logger.exception("Training failed for %s", symbol)
                await db.rollback()
                bus.publish(EVENT_TRAINING_PROGRESS, {
                    "symbol": symbol, "phase": "failed", "progress": 1.0,
                    "reason": "Unexpected error; see logs.",
                })


async def data_refresh_job() -> None:
    """Keep candles current (§5.1)."""
    from app.services.data_downloader import download_historical_data

    try:
        await download_historical_data()
    except Exception:  # noqa: BLE001
        logger.exception("Scheduled data refresh failed")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def start_scheduler() -> AsyncIOScheduler:
    """Build and start the scheduler using intervals from `config`."""
    global scheduler

    if scheduler is not None and scheduler.running:
        return scheduler

    async with AsyncSessionLocal() as db:
        trade_minutes = await get_config(db, "trade_loop_interval_minutes")
        heartbeat_seconds = await get_config(db, "heartbeat_interval_seconds")
        reconcile_minutes = await get_config(db, "reconcile_interval_minutes")
        retrain_hours = await get_config(db, "retrain_interval_hours")
        refresh_hour = await get_config(db, "data_refresh_hour_utc")

    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        heartbeat_job, IntervalTrigger(seconds=heartbeat_seconds),
        id="heartbeat", replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        reconcile_job, IntervalTrigger(minutes=reconcile_minutes),
        id="reconcile", replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        trade_loop_job, IntervalTrigger(minutes=trade_minutes),
        id="trade_loop", replace_existing=True,
        # coalesce + max_instances=1 stop a slow pass from stacking up and
        # placing duplicate orders.
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        retrain_job, IntervalTrigger(hours=retrain_hours),
        id="retrain", replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        data_refresh_job, CronTrigger(hour=refresh_hour, minute=0),
        id="data_refresh", replace_existing=True, max_instances=1,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: trade loop every %smin, heartbeat every %ss, "
        "reconcile every %smin, retrain every %sh.",
        trade_minutes, heartbeat_seconds, reconcile_minutes, retrain_hours,
    )
    bus.publish(EVENT_SYSTEM, {"level": "info", "message": "Scheduler started."})
    return scheduler


async def run_startup_reconciliation() -> None:
    """Resolve unknown orders before the trade loop is allowed to run (§1.7).

    Trading before this completes would size positions against an account
    view that may be missing a fill.
    """
    async with AsyncSessionLocal() as db:
        try:
            stage = await get_config(db, "current_stage")
            if stage in (STAGE_SETUP, te.STAGE_LIVE):
                return
            results = await te.reconcile_open_orders(db, stage)
            if results:
                logger.warning("Startup reconciliation resolved %d order(s).", len(results))
        except Exception:  # noqa: BLE001
            logger.exception("Startup reconciliation failed")
            await db.rollback()


async def shutdown_scheduler() -> None:
    global scheduler
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
    scheduler = None


def job_status() -> list[dict]:
    """Job names and next run times, for the dashboard."""
    if scheduler is None or not scheduler.running:
        return []
    return [
        {
            "id": job.id,
            "next_run_at": job.next_run_time.isoformat() if job.next_run_time else None,
        }
        for job in scheduler.get_jobs()
    ]
