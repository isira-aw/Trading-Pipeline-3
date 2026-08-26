"""Order placement (§5.2, build order step 7).

Paper stage only for now: orders go to the Binance **testnet**, which runs a
real matching engine against fake funds, so fills and partial fills behave
realistically. Live-stage differences are deliberately absent — that is
step 12's promotion gate, and there must be no live code path before then.

Three invariants this module exists to hold:

1. **Nothing reaches the exchange without risk approval.** `_submit_order`
   takes a `RiskDecision` and refuses any that is not approved, so there is
   no call path to the exchange that skips `risk_engine.assess`.

2. **Crash safety.** The `trades` row is written and committed *before* the
   order is sent, using the trade's own UUID as Binance's client order id.
   If the process dies between sending and recording the response, the row
   is left as `submitted` and `reconcile_open_orders` can find the real
   outcome by that id on restart. Without the write-ahead there would be no
   record that an order had ever been sent.

3. **Failures are recorded, never swallowed.** An exchange error marks the
   trade `failed` and surfaces on `component_status`; it never propagates
   into the scheduler, which would kill the trade loop silently (§1.7).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ComponentStatus, RiskLog, Trade
from app.services import risk_engine
from app.services.binance_client import (
    BinanceClientError,
    get_market_data_client,
    get_trading_client,
)
from app.services.config_service import get_config
from app.services.position_tracker import TradeRecord, match_fifo
from app.services.wallet_service import take_snapshot

logger = logging.getLogger(__name__)

STATUS_SUBMITTED = "submitted"
STATUS_FILLED = "filled"
STATUS_PARTIAL = "partial"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

# Rows in any of these states have an unknown real-world outcome and must be
# reconciled against the exchange before the trade loop resumes.
UNRESOLVED_STATUSES = (STATUS_SUBMITTED, STATUS_PARTIAL)

ORDER_TYPE_MARKET = "market"

# Binance order status -> our status vocabulary.
BINANCE_STATUS_MAP = {
    "NEW": STATUS_SUBMITTED,
    "PARTIALLY_FILLED": STATUS_PARTIAL,
    "FILLED": STATUS_FILLED,
    "CANCELED": STATUS_CANCELLED,
    "PENDING_CANCEL": STATUS_CANCELLED,
    "REJECTED": STATUS_FAILED,
    "EXPIRED": STATUS_FAILED,
    "EXPIRED_IN_MATCH": STATUS_FAILED,
}

QUOTE_ASSET = "USDT"


STAGE_LIVE = "live"


class TradingEngineError(RuntimeError):
    """An order could not be placed or reconciled."""


class StageNotPermitted(TradingEngineError):
    """Refused to place an order for a stage that is not actually active.

    `get_trading_client` returns a PRODUCTION client for stage 'live', so
    the `stage` argument threaded through this module decides whether real
    money moves. This is the guard that stops that argument being trusted
    on its own.

    It replaces step 8's blanket `assert_paper_stage`, which refused live
    outright while no promotion gate existed. The gate exists now, but
    removing the old block without a replacement would leave the window
    unguarded, so the check became narrower rather than absent: an order
    may only be placed for the stage the config table says is currently
    active, and a live *entry* additionally requires the §5.4 gate to still
    pass.
    """


# Backwards-compatible alias: step 8's name, now backed by the real check.
LiveStageNotEnabled = StageNotPermitted


async def assert_stage_permitted(
    db: AsyncSession, stage: str, side: str | None = None
) -> None:
    """Verify an order may be placed for this stage (§5.3, §5.4).

    Two conditions:

    1. `stage` must match `current_stage` in config. A caller cannot reach
       the production client by passing 'live' while the system is in
       paper — the switch endpoint, with its PIN and gate, is the only way
       `current_stage` becomes 'live'.
    2. For live *entries*, the promotion gate must still pass. This is
       defence in depth: if the record degrades after promotion, new risk
       stops being taken.

    Exits are deliberately exempt from the second condition. Blocking a
    sell because the gate slipped would strand real capital in a position
    the system can no longer close, which is the same reasoning that keeps
    sells exempt from the risk engine's entry rules.
    """
    # Defence in depth: the scheduler pauses the trade loop on halt, but a
    # manual trigger or a still-queued job must not slip an order through.
    # Exits stay permitted — the liquidate action deliberately runs while
    # halted, and blocking sells would strand capital.
    if await get_config(db, "halted") and side != "sell":
        raise StageNotPermitted(
            "Trading is halted (emergency stop active). Resume with the "
            "stage PIN before placing new orders. Exits remain permitted."
        )

    current = await get_config(db, "current_stage")

    if stage != current:
        raise StageNotPermitted(
            f"Refusing an order for stage {stage!r} while the active stage "
            f"is {current!r}. Stage changes go through the PIN-gated switch."
        )

    if stage != STAGE_LIVE:
        return

    if side == "sell":
        return

    from app.services.promotion_gate import evaluate_gate

    result = await evaluate_gate(db)
    if not result.passed:
        raise StageNotPermitted(
            f"Live entries are blocked: the promotion gate no longer passes "
            f"({result.summary()}). Exits remain permitted."
        )


class RiskApprovalMissing(TradingEngineError):
    """Refused to submit an order the risk engine did not approve.

    Raised rather than returned: reaching this means a caller found a way
    around the gate, which is a bug, not an outcome.
    """


@dataclass
class AccountState:
    balances: dict[str, float]
    total_value_usdt: float
    exposure_usdt: float


@dataclass
class OrderOutcome:
    placed: bool
    decision: str
    trade_id: str | None = None
    status: str | None = None
    filled_quantity: float = 0.0
    fill_price: float | None = None
    fee_usdt: float = 0.0
    reason: str = ""


# --------------------------------------------------------------------------
# Component status
# --------------------------------------------------------------------------


async def set_component_status(
    db: AsyncSession, component: str, status: str, detail: str | None = None
) -> None:
    """Update a heartbeat row. Caller commits.

    The risk engine reads these; an exchange failure recorded here stops
    further trades until the component recovers (§6).
    """
    row = await db.get(ComponentStatus, component)
    now = datetime.now(timezone.utc)
    if row is None:
        db.add(
            ComponentStatus(
                component=component, status=status, last_heartbeat=now, detail=detail
            )
        )
    else:
        row.status = status
        row.last_heartbeat = now
        row.detail = detail
    await db.flush()


# --------------------------------------------------------------------------
# Account state
# --------------------------------------------------------------------------


def _price_assets(balances: dict[str, float], prices: dict[str, float]) -> tuple[float, float]:
    """Value holdings in USDT. Returns (total, non-quote exposure)."""
    total = 0.0
    exposure = 0.0
    for asset, amount in balances.items():
        if amount <= 0:
            continue
        if asset == QUOTE_ASSET:
            total += amount
            continue
        price = prices.get(f"{asset}{QUOTE_ASSET}")
        if price is None:
            logger.warning("No %s%s price; excluding from wallet value.", asset, QUOTE_ASSET)
            continue
        value = amount * price
        total += value
        exposure += value
    return total, exposure


async def get_account_state(db: AsyncSession, stage: str) -> AccountState:
    """Fetch balances from the exchange and value them in USDT."""
    client = get_trading_client(stage)
    market = get_market_data_client()

    try:
        account = client.client.get_account()
    except (BinanceClientError, Exception) as exc:  # noqa: BLE001
        raise TradingEngineError(f"Could not read account state: {exc}") from exc

    balances = {
        entry["asset"]: float(entry["free"]) + float(entry["locked"])
        for entry in account.get("balances", [])
        if float(entry["free"]) + float(entry["locked"]) > 0
    }

    prices: dict[str, float] = {}
    for asset in balances:
        if asset == QUOTE_ASSET:
            continue
        symbol = f"{asset}{QUOTE_ASSET}"
        try:
            prices[symbol] = market.get_symbol_price(symbol)
        except BinanceClientError:
            logger.warning("Could not price %s", symbol)

    total, exposure = _price_assets(balances, prices)
    return AccountState(balances=balances, total_value_usdt=total, exposure_usdt=exposure)


# --------------------------------------------------------------------------
# Fills
# --------------------------------------------------------------------------


def parse_fills(response: dict, fallback_price: float) -> tuple[float, float, float]:
    """Extract (filled_quantity, average_price, fee_usdt) from an order response.

    Binance reports commission per fill in whatever asset it was taken in.
    A commission in the quote asset is already USDT; one in the base asset
    is converted at that fill's price. Anything else (typically BNB) cannot
    be converted from this response alone and is logged and skipped rather
    than guessed at, which would put a wrong number into realized P&L.
    """
    fills = response.get("fills") or []

    filled_qty = float(response.get("executedQty") or 0.0)
    quote_qty = float(response.get("cummulativeQuoteQty") or 0.0)

    fee_usdt = 0.0
    for fill in fills:
        commission = float(fill.get("commission") or 0.0)
        asset = fill.get("commissionAsset")
        if commission <= 0:
            continue
        if asset == QUOTE_ASSET:
            fee_usdt += commission
        elif asset and response.get("symbol", "").startswith(asset):
            fee_usdt += commission * float(fill.get("price") or fallback_price)
        else:
            logger.warning(
                "Commission of %s %s on order %s cannot be converted to USDT "
                "from the order response; excluded from fees.",
                commission, asset, response.get("orderId"),
            )

    if filled_qty > 0 and quote_qty > 0:
        average_price = quote_qty / filled_qty
    elif fills:
        total = sum(float(f["qty"]) for f in fills)
        average_price = (
            sum(float(f["price"]) * float(f["qty"]) for f in fills) / total
            if total else fallback_price
        )
    else:
        average_price = fallback_price

    return filled_qty, average_price, fee_usdt


def round_to_step(quantity: float, step_size: float) -> float:
    """Round down to the exchange's LOT_SIZE step.

    Rounding *down* matters: rounding up could exceed the size the risk
    engine approved.

    Done in Decimal rather than with `//`. Binary floating point makes
    `0.5 // 0.1` equal 4.0, so a quantity sitting exactly on a step boundary
    would lose a whole step — submitting 0.4 where 0.5 was approved.
    """
    if step_size <= 0:
        return quantity

    quantity_dec = Decimal(str(quantity))
    step_dec = Decimal(str(step_size))
    steps = (quantity_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * step_dec)


async def get_lot_step(stage: str, symbol: str) -> float:
    """LOT_SIZE step for a symbol; 0 when it cannot be determined."""
    client = get_trading_client(stage)
    try:
        info = client.client.get_symbol_info(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch symbol info for %s: %s", symbol, exc)
        return 0.0

    for filter_ in (info or {}).get("filters", []):
        if filter_.get("filterType") == "LOT_SIZE":
            return float(filter_.get("stepSize", 0.0))
    return 0.0


# --------------------------------------------------------------------------
# Order placement
# --------------------------------------------------------------------------


async def latest_advisory_context(db: AsyncSession) -> dict | None:
    """Snapshot of the newest usable advisory, for trades.llm_context (§5.1).

    Read-only and purely for audit: nothing in this function's result is
    consulted when deciding whether or how to trade. The advisory table is
    queried directly rather than importing `llm_advisor`, so the dependency
    only ever runs trading -> advisory data, never the reverse.
    """
    from app.db.models import LLMAdvisory

    try:
        max_age = float(await get_config(db, "llm_advisory_max_age_hours"))
    except Exception:  # noqa: BLE001
        max_age = 36.0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)
    rows = (
        await db.execute(
            select(LLMAdvisory)
            .where(LLMAdvisory.created_at >= cutoff)
            .order_by(LLMAdvisory.created_at.desc())
            .limit(10)
        )
    ).scalars().all()

    for row in rows:
        response = row.response or {}
        if response.get("status") != "ok":
            continue
        return {
            "advisory_id": row.id,
            "provider": row.provider,
            "created_at": row.created_at.isoformat(),
            "uncertainty": response.get("uncertainty"),
            "macro_summary": response.get("macro_summary"),
            "symbols": response.get("symbols"),
            "key_risks": response.get("key_risks"),
        }
    return None


async def _submit_order(
    db: AsyncSession,
    proposal: risk_engine.TradeProposal,
    decision: risk_engine.RiskDecision,
    stage: str,
    risk_log_entry: RiskLog | None = None,
    stop_price: float | None = None,
    exit_reason: str | None = None,
) -> OrderOutcome:
    """Send an approved order to the exchange.

    The only function in the codebase that places an order. It requires the
    `RiskDecision` that approved it and refuses anything else, so there is
    no path to the exchange that bypasses the risk engine.
    """
    await assert_stage_permitted(db, stage, proposal.side)

    if not decision.approved:
        raise RiskApprovalMissing(
            f"Refusing to submit a {decision.decision} order for "
            f"{proposal.symbol}: the risk engine did not approve it."
        )

    quantity = decision.final_quantity
    if quantity <= 0:
        raise RiskApprovalMissing(
            f"Refusing to submit an order for {proposal.symbol} with "
            f"non-positive approved quantity {quantity}."
        )

    step = await get_lot_step(stage, proposal.symbol)
    quantity = round_to_step(quantity, step) if step else quantity
    if quantity <= 0:
        return OrderOutcome(
            placed=False, decision=decision.decision,
            reason=(
                f"Approved quantity {decision.final_quantity} rounds to zero "
                f"at the exchange's {step} lot step."
            ),
        )

    trade_id = uuid.uuid4()

    # Write-ahead: the row is committed BEFORE the order is sent, so a crash
    # between sending and recording still leaves a row to reconcile. The
    # trade's own UUID doubles as Binance's client order id (36 chars, which
    # is exactly the limit), so no extra column is needed to find it again.
    trade = Trade(
        id=trade_id,
        stage=stage,
        symbol=proposal.symbol,
        side=proposal.side,
        order_type=ORDER_TYPE_MARKET,
        quantity=quantity,
        price=None,
        model_id=proposal.model_id,
        model_confidence=proposal.confidence,
        risk_decision=decision.decision,
        risk_notes={
            "reason": decision.reason,
            "original_quantity": decision.original_quantity,
            "approved_quantity": decision.final_quantity,
            "submitted_quantity": quantity,
            "lot_step": step,
        },
        status=STATUS_SUBMITTED,
        fee_usdt=0,
        # Audit only — see latest_advisory_context.
        llm_context=await latest_advisory_context(db),
        # Entry-side: the ATR stop, fixed for this position's life.
        stop_price=stop_price,
        # Exit-side: which of the three rules closed the position.
        exit_reason=exit_reason,
    )
    db.add(trade)

    if risk_log_entry is not None:
        risk_log_entry.trade_id = trade_id

    await db.commit()

    client = get_trading_client(stage)
    try:
        response = await _place_market_order(
            client, proposal.symbol, proposal.side, quantity, str(trade_id)
        )
    except Exception as exc:  # noqa: BLE001
        # §1.7: record the failure and surface it; never let it reach the
        # scheduler, which would kill the trade loop without a trace.
        logger.exception("Order submission failed for %s", proposal.symbol)
        trade.status = STATUS_FAILED
        trade.risk_notes = {**(trade.risk_notes or {}), "error": str(exc)}
        await set_component_status(
            db, "binance_api", "error", f"Order submission failed: {exc}"
        )
        await db.commit()
        return OrderOutcome(
            placed=False, decision=decision.decision, trade_id=str(trade_id),
            status=STATUS_FAILED, reason=f"Exchange error: {exc}",
        )

    outcome = await _record_response(db, trade, response, proposal.price)
    await set_component_status(db, "binance_api", "online", "Order placed")
    await db.commit()

    if outcome.filled_quantity > 0:
        # The risk engine's daily loss cap reads wallet snapshots; letting
        # them drift stale after a fill would measure the cap against an
        # out-of-date baseline (§6).
        await snapshot_wallet(db, stage)

    return outcome


async def _place_market_order(client, symbol: str, side: str, quantity: float, client_order_id: str):
    """Thin wrapper so tests can substitute the exchange call."""
    import asyncio

    return await asyncio.to_thread(
        client.client.create_order,
        symbol=symbol,
        side=side.upper(),
        type="MARKET",
        quantity=quantity,
        newClientOrderId=client_order_id,
    )


async def _record_response(
    db: AsyncSession, trade: Trade, response: dict, fallback_price: float
) -> OrderOutcome:
    """Apply an exchange response to a trade row."""
    filled_qty, average_price, fee_usdt = parse_fills(response, fallback_price)
    status = BINANCE_STATUS_MAP.get(response.get("status", ""), STATUS_SUBMITTED)

    trade.status = status
    trade.binance_order_id = str(response.get("orderId") or "") or None
    trade.fee_usdt = fee_usdt
    if filled_qty > 0:
        trade.price = average_price
        trade.quantity = filled_qty

    await db.flush()

    return OrderOutcome(
        placed=True,
        decision=trade.risk_decision,
        trade_id=str(trade.id),
        status=status,
        filled_quantity=filled_qty,
        fill_price=average_price if filled_qty > 0 else None,
        fee_usdt=fee_usdt,
        reason=f"Order {status}.",
    )


async def place_order(
    db: AsyncSession,
    proposal: risk_engine.TradeProposal,
    stage: str,
    now: datetime | None = None,
    stop_price: float | None = None,
    exit_reason: str | None = None,
) -> OrderOutcome:
    """Assess a proposal and, if approved, place it.

    This is the entry point the trade loop uses. Rejections are logged to
    risk_log by `assess` and return without touching the exchange.
    """
    await assert_stage_permitted(db, stage, proposal.side)

    try:
        state = await get_account_state(db, stage)
    except TradingEngineError as exc:
        logger.error("Cannot read account state: %s", exc)
        await set_component_status(db, "binance_api", "error", str(exc))
        await db.commit()
        return OrderOutcome(
            placed=False, decision="aborted",
            reason=f"Could not read account state: {exc}",
        )

    decision, entry = await risk_engine.assess(
        db, proposal, stage, state.total_value_usdt, state.exposure_usdt, now
    )

    if not decision.approved:
        await db.commit()
        return OrderOutcome(
            placed=False, decision=decision.decision, reason=decision.reason,
        )

    return await _submit_order(
        db, proposal, decision, stage, entry, stop_price, exit_reason
    )


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


COMPONENT_RECONCILIATION = "order_reconciliation"


def _reconcile_state(trade: Trade) -> dict:
    return (trade.risk_notes or {}).get("reconcile", {})


def _set_reconcile_state(trade: Trade, state: dict) -> None:
    # JSONB columns need a new object to be seen as dirty by the ORM.
    trade.risk_notes = {**(trade.risk_notes or {}), "reconcile": state}


def backoff_seconds(attempts: int, base: float, maximum: float) -> float:
    """Exponential backoff, capped. `attempts` is the count already made."""
    return min(base * (2 ** max(0, attempts - 1)), maximum)


def is_due_for_retry(trade: Trade, now: datetime) -> bool:
    """Whether an unresolved order's backoff window has elapsed.

    An order that has already been escalated stops being retried
    automatically — it needs a human to look at it.
    """
    state = _reconcile_state(trade)
    if state.get("needs_attention"):
        return False

    next_attempt = state.get("next_attempt_at")
    if not next_attempt:
        return True
    return now >= datetime.fromisoformat(next_attempt)


async def get_orders_needing_attention(db: AsyncSession, stage: str) -> list[Trade]:
    """Unresolved orders that exhausted their retries (§1.7, §8.1).

    These are the dangerous ones: we do not know whether a position was
    opened, so the account view may be wrong.
    """
    rows = (
        await db.execute(
            select(Trade).where(
                Trade.stage == stage, Trade.status.in_(UNRESOLVED_STATUSES)
            )
        )
    ).scalars().all()
    return [t for t in rows if _reconcile_state(t).get("needs_attention")]


async def refresh_reconciliation_alert(db: AsyncSession, stage: str) -> int:
    """Surface stuck orders on `component_status` so they appear on the
    dashboard rather than sitting unresolved and invisible.

    Deliberately not added to `required_healthy_components` by default: an
    unresolved order means an unknown position, but hard-blocking all
    trading on it could strand the system if the exchange stays unreachable.
    Add it to that config list if you want the stricter behaviour.
    """
    stuck = await get_orders_needing_attention(db, stage)
    if stuck:
        await set_component_status(
            db, COMPONENT_RECONCILIATION, "error",
            f"{len(stuck)} order(s) could not be reconciled after the retry "
            f"limit — position state may be wrong. Trade ids: "
            f"{', '.join(str(t.id) for t in stuck[:5])}",
        )
    else:
        await set_component_status(
            db, COMPONENT_RECONCILIATION, "online", "No unresolved orders."
        )
    await db.commit()
    return len(stuck)


async def reconcile_open_orders(db: AsyncSession, stage: str) -> list[dict]:
    """Resolve trades whose real outcome is unknown (§1.7).

    Run on startup, before the trade loop resumes. Any row left `submitted`
    or `partial` may correspond to an order that actually filled — resuming
    without checking would mean trading against a wrong view of the account.

    Orders are looked up by the trade's UUID as client order id, so a crash
    that happened before the exchange's order id was recorded is still
    recoverable.
    """
    now = datetime.now(timezone.utc)
    max_attempts = await get_config(db, "reconcile_max_attempts")
    backoff_base = await get_config(db, "reconcile_backoff_base_seconds")
    backoff_max = await get_config(db, "reconcile_backoff_max_seconds")

    unresolved = (
        await db.execute(
            select(Trade).where(
                Trade.stage == stage, Trade.status.in_(UNRESOLVED_STATUSES)
            )
        )
    ).scalars().all()

    # Skip rows still inside their backoff window, and ones already escalated.
    rows = [t for t in unresolved if is_due_for_retry(t, now)]

    if not rows:
        logger.info(
            "Reconciliation: nothing due for stage '%s' (%d unresolved, none due).",
            stage, len(unresolved),
        )
        await refresh_reconciliation_alert(db, stage)
        return []

    logger.warning(
        "Reconciliation: %d order(s) due for stage '%s'.", len(rows), stage
    )

    client = get_trading_client(stage)
    results = []

    for trade in rows:
        before = trade.status
        try:
            response = await _fetch_order(client, trade.symbol, str(trade.id))
        except Exception as exc:  # noqa: BLE001
            # An order the exchange has never heard of never reached it, so
            # the write-ahead row is the only trace and is safe to fail.
            if _is_unknown_order(exc):
                trade.status = STATUS_FAILED
                trade.risk_notes = {
                    **(trade.risk_notes or {}),
                    "reconciliation": "Exchange has no record of this order; "
                                      "it never reached the matching engine.",
                }
                results.append({
                    "trade_id": str(trade.id), "before": before,
                    "after": STATUS_FAILED, "resolved": True,
                    "detail": "unknown to exchange",
                })
                continue

            # Anything else is a transient lookup problem. Leave the row
            # unresolved so a later run retries it, rather than guessing —
            # reading a network blip as "never happened" would lose a real
            # position. Back off, and escalate once retries are exhausted.
            state = _reconcile_state(trade)
            attempts = state.get("attempts", 0) + 1
            exhausted = attempts >= max_attempts

            delay = backoff_seconds(attempts, backoff_base, backoff_max)
            _set_reconcile_state(trade, {
                "attempts": attempts,
                "last_attempt_at": now.isoformat(),
                "next_attempt_at": (now + timedelta(seconds=delay)).isoformat(),
                "last_error": str(exc),
                "needs_attention": exhausted,
            })

            if exhausted:
                logger.error(
                    "Trade %s could not be reconciled after %d attempts — "
                    "escalating for manual attention. Last error: %s",
                    trade.id, attempts, exc,
                )
            else:
                logger.warning(
                    "Reconcile attempt %d/%d failed for trade %s; retrying in %.0fs: %s",
                    attempts, max_attempts, trade.id, delay, exc,
                )

            results.append({
                "trade_id": str(trade.id), "before": before, "after": before,
                "resolved": False,
                "attempts": attempts,
                "needs_attention": exhausted,
                "retry_in_seconds": None if exhausted else delay,
                "detail": (
                    f"escalated after {attempts} failed attempts: {exc}"
                    if exhausted else f"lookup failed: {exc}"
                ),
            })
            continue

        await _record_response(db, trade, response, float(trade.price or 0.0))
        if trade.status not in UNRESOLVED_STATUSES:
            _set_reconcile_state(trade, {"attempts": 0, "resolved_at": now.isoformat()})
        results.append({
            "trade_id": str(trade.id), "before": before, "after": trade.status,
            "resolved": trade.status not in UNRESOLVED_STATUSES,
            "detail": f"exchange reports {response.get('status')}",
        })

    await db.commit()

    # Escalations must reach the dashboard, not just the log.
    await refresh_reconciliation_alert(db, stage)

    # Balances may have moved while the outcome was unknown.
    if any(r["resolved"] for r in results):
        await snapshot_wallet(db, stage)

    return results


async def _fetch_order(client, symbol: str, client_order_id: str) -> dict:
    import asyncio

    return await asyncio.to_thread(
        client.client.get_order, symbol=symbol, origClientOrderId=client_order_id
    )


def _is_unknown_order(exc: Exception) -> bool:
    """Binance returns -2013 "Order does not exist"."""
    code = getattr(exc, "code", None)
    if code == -2013:
        return True
    return "does not exist" in str(exc).lower()


# --------------------------------------------------------------------------
# Wallet snapshots
# --------------------------------------------------------------------------


async def snapshot_wallet(db: AsyncSession, stage: str):
    """Record wallet state. Called after every fill so the risk engine's
    daily loss baseline stays current (§6)."""
    try:
        state = await get_account_state(db, stage)
    except TradingEngineError as exc:
        logger.error("Could not snapshot wallet: %s", exc)
        return None

    snapshot = await take_snapshot(
        db, stage, state.balances, state.total_value_usdt
    )
    await db.commit()
    return snapshot


# --------------------------------------------------------------------------
# Realized performance
# --------------------------------------------------------------------------


async def load_trade_records(
    db: AsyncSession, stage: str, symbol: str | None = None, model_id=None
) -> list[TradeRecord]:
    """Load filled trades for position matching.

    Only executed trades take a position, so submitted/failed/cancelled rows
    are excluded — including them would invent positions that never existed.
    """
    query = select(Trade).where(
        Trade.stage == stage,
        Trade.status.in_((STATUS_FILLED, STATUS_PARTIAL)),
        Trade.price.isnot(None),
    )
    if symbol:
        query = query.where(Trade.symbol == symbol)

    rows = (await db.execute(query)).scalars().all()

    return [
        TradeRecord(
            id=str(row.id),
            symbol=row.symbol,
            side=row.side,
            quantity=float(row.quantity),
            price=float(row.price),
            created_at=row.created_at,
            model_id=str(row.model_id) if row.model_id else None,
            fee_usdt=float(row.fee_usdt or 0),
            stop_price=float(row.stop_price) if row.stop_price is not None else None,
        )
        for row in rows
    ]


async def get_realized_performance(
    db: AsyncSession, stage: str, symbol: str | None = None
) -> dict:
    """Realized stats across all closed positions for a stage."""
    from app.services.position_tracker import compute_stats, first_entry_at

    trades = await load_trade_records(db, stage, symbol)
    result = match_fifo(trades)
    stats = compute_stats(result.closed)
    stats["open_lots"] = len(result.open_lots)
    stats["unmatched_sell_quantity"] = result.unmatched_sell_quantity
    stats["first_entry_at"] = first_entry_at(result.closed, result.open_lots)
    return stats
