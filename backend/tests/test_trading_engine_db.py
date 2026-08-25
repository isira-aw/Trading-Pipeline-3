"""Integration tests for order placement, reconciliation and realized P&L.

The exchange is stubbed — these verify our side of the contract (what gets
written, what is refused, what happens after a crash), not Binance's.

Skipped when no database is reachable.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import ComponentStatus, Model, RiskLog, Trade, WalletSnapshot
from app.services import trading_engine as te
from app.services.risk_engine import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    DECISION_RESIZED,
    RiskDecision,
    TradeProposal,
)
from app.services.wallet_service import take_snapshot

TEST_STAGE = "tetest"
SYMBOL = "TEUSDT"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
START_OF_DAY = NOW.replace(hour=0, minute=0, second=0, microsecond=0)

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/trading_pipeline",
)


async def _db_reachable() -> bool:
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect():
            return True
    except Exception:
        return False
    finally:
        await engine.dispose()


async def _cleanup(session):
    await session.execute(
        delete(RiskLog).where(
            RiskLog.trade_id.in_(select(Trade.id).where(Trade.stage == TEST_STAGE))
        )
    )
    await session.execute(delete(Trade).where(Trade.stage == TEST_STAGE))
    await session.execute(delete(WalletSnapshot).where(WalletSnapshot.stage == TEST_STAGE))
    # After trades: trades.model_id references models.id.
    await session.execute(delete(Model).where(Model.symbol == SYMBOL))
    await session.commit()


@pytest_asyncio.fixture
async def db():
    if not await _db_reachable():
        pytest.skip(f"No database reachable at {DB_URL}")

    engine = create_async_engine(DB_URL)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as session:
        await _cleanup(session)
        try:
            yield session
        finally:
            await session.rollback()
            await _cleanup(session)

    await engine.dispose()


# --------------------------------------------------------------------------
# Exchange stubs
# --------------------------------------------------------------------------


def fill_response(order_id=1, qty="1", quote="100", status="FILLED", fee="0.1"):
    fills = []
    if float(qty) > 0:
        fills = [{"price": str(float(quote) / float(qty)), "qty": qty,
                  "commission": fee, "commissionAsset": "USDT"}]
    return {
        "symbol": SYMBOL, "orderId": order_id, "status": status,
        "executedQty": qty, "cummulativeQuoteQty": quote, "fills": fills,
    }


class StubExchange:
    """Records calls so tests can assert whether the exchange was reached."""

    def __init__(self, response=None, error=None):
        self.response = response or fill_response()
        self.error = error
        self.orders = []

    def create_order(self, **kwargs):
        self.orders.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def patch_exchange(stub, account_total=10_000.0):
    """Patch every outbound call the engine makes."""
    async def fake_place(client, symbol, side, quantity, client_order_id):
        return stub.create_order(
            symbol=symbol, side=side, quantity=quantity,
            newClientOrderId=client_order_id,
        )

    async def fake_state(db, stage):
        return te.AccountState(
            balances={"USDT": account_total}, total_value_usdt=account_total,
            exposure_usdt=0.0,
        )

    async def fake_step(stage, symbol):
        return 0.0

    return (
        patch.object(te, "_place_market_order", fake_place),
        patch.object(te, "get_account_state", fake_state),
        patch.object(te, "get_lot_step", fake_step),
    )


async def _seed_baseline(db):
    """A wallet baseline, so the daily-loss rule has something to measure."""
    await take_snapshot(db, TEST_STAGE, {"USDT": 10_000}, 10_000.0, START_OF_DAY - timedelta(hours=1))
    await take_snapshot(db, TEST_STAGE, {"USDT": 10_000}, 10_000.0, NOW)
    await db.commit()


async def _seed_healthy_components(db):
    for name in ("binance_api", "data_feed"):
        await te.set_component_status(db, name, "online", "test")
    await db.commit()


# --------------------------------------------------------------------------
# 1. Risk engine cannot be bypassed
# --------------------------------------------------------------------------


class TestRiskGateCannotBeBypassed:
    async def test_submit_refuses_a_rejected_decision(self, db):
        """Calling the order-placement function directly with a rejected
        decision must refuse, not place the order."""
        stub = StubExchange()
        decision = RiskDecision(
            decision=DECISION_REJECTED, reason="confidence too low",
            checks=[], final_quantity=0.0, original_quantity=1.0,
        )
        proposal = TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.1, None)

        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            with pytest.raises(te.RiskApprovalMissing):
                await te._submit_order(db, proposal, decision, TEST_STAGE)

        assert stub.orders == [], "an order reached the exchange despite rejection"
        rows = (await db.execute(select(Trade).where(Trade.stage == TEST_STAGE))).scalars().all()
        assert rows == [], "a trade row was written for a rejected decision"

    async def test_submit_refuses_zero_approved_quantity(self, db):
        stub = StubExchange()
        decision = RiskDecision(
            decision=DECISION_APPROVED, reason="ok", checks=[],
            final_quantity=0.0, original_quantity=1.0,
        )
        proposal = TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.9, None)

        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            with pytest.raises(te.RiskApprovalMissing):
                await te._submit_order(db, proposal, decision, TEST_STAGE)

        assert stub.orders == []

    async def test_place_order_stops_at_a_rejection(self, db):
        """The full path: a proposal the risk engine rejects never reaches
        the exchange, and is still logged to risk_log."""
        await _seed_baseline(db)
        await _seed_healthy_components(db)

        stub = StubExchange()
        # Confidence far below the floor -> rejected.
        proposal = TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.01, None)

        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            outcome = await te.place_order(db, proposal, TEST_STAGE, NOW)

        assert not outcome.placed
        assert outcome.decision == DECISION_REJECTED
        assert stub.orders == []

        logs = (await db.execute(select(RiskLog).order_by(RiskLog.id.desc()).limit(1))).scalars().all()
        assert logs and logs[0].decision == DECISION_REJECTED

    async def test_resized_decision_submits_the_resized_quantity(self, db):
        """An approved-but-resized order must send the risk engine's
        quantity, not the one originally proposed."""
        stub = StubExchange()
        decision = RiskDecision(
            decision=DECISION_RESIZED, reason="position size",
            checks=[], final_quantity=0.25, original_quantity=5.0,
        )
        proposal = TradeProposal(SYMBOL, "buy", 5.0, 100.0, 0.9, None)

        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            await te._submit_order(db, proposal, decision, TEST_STAGE)

        assert stub.orders[0]["quantity"] == pytest.approx(0.25)


# --------------------------------------------------------------------------
# 2. Crash safety / reconciliation
# --------------------------------------------------------------------------


class TestReconciliation:
    async def _orphan(self, db, status=te.STATUS_SUBMITTED):
        """A row left behind by a crash: written before the order was sent,
        never updated with the response."""
        trade = Trade(
            id=uuid.uuid4(), stage=TEST_STAGE, symbol=SYMBOL, side="buy",
            order_type="market", quantity=1.0, price=None, risk_decision="approved",
            status=status, binance_order_id=None, created_at=NOW,
        )
        db.add(trade)
        await db.commit()
        return trade

    async def test_write_ahead_row_exists_before_the_exchange_call(self, db):
        """The crash-safety precondition: if the process dies during the
        exchange call, a row must already be on disk to reconcile."""
        seen = {}

        async def crash(client, symbol, side, quantity, client_order_id):
            # Read from a *separate* session to prove the row was committed.
            engine = create_async_engine(DB_URL)
            maker = async_sessionmaker(engine, class_=AsyncSession)
            async with maker() as other:
                rows = (await other.execute(
                    select(Trade).where(Trade.stage == TEST_STAGE)
                )).scalars().all()
                seen["count"] = len(rows)
                seen["status"] = rows[0].status if rows else None
                seen["id_matches_client_order_id"] = (
                    str(rows[0].id) == client_order_id if rows else False
                )
            await engine.dispose()
            raise RuntimeError("process died mid-order")

        decision = RiskDecision(
            decision=DECISION_APPROVED, reason="ok", checks=[],
            final_quantity=1.0, original_quantity=1.0,
        )
        proposal = TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.9, None)

        _, p2, p3 = patch_exchange(StubExchange())
        with patch.object(te, "_place_market_order", crash), p2, p3:
            await te._submit_order(db, proposal, decision, TEST_STAGE)

        assert seen["count"] == 1
        assert seen["status"] == te.STATUS_SUBMITTED
        assert seen["id_matches_client_order_id"], (
            "the trade UUID must be the client order id, or a crashed order "
            "cannot be found again on the exchange"
        )

    async def test_reconciles_a_submitted_order_that_actually_filled(self, db):
        """The dangerous case: the order filled but we never recorded it.
        Resuming without reconciling would trade against a wrong account view.
        """
        trade = await self._orphan(db)

        async def fake_fetch(client, symbol, client_order_id):
            assert client_order_id == str(trade.id)
            return fill_response(order_id=999, qty="1", quote="150")

        with patch.object(te, "_fetch_order", fake_fetch), \
             patch.object(te, "snapshot_wallet", lambda db, stage: _noop()):
            results = await te.reconcile_open_orders(db, TEST_STAGE)

        assert len(results) == 1
        assert results[0]["before"] == te.STATUS_SUBMITTED
        assert results[0]["after"] == te.STATUS_FILLED
        assert results[0]["resolved"]

        await db.refresh(trade)
        assert trade.status == te.STATUS_FILLED
        assert float(trade.price) == pytest.approx(150.0)
        assert trade.binance_order_id == "999"

    async def test_order_unknown_to_exchange_is_marked_failed(self, db):
        """The crash happened before the order left; nothing was executed."""
        trade = await self._orphan(db)

        class UnknownOrder(Exception):
            code = -2013

        async def fake_fetch(client, symbol, client_order_id):
            raise UnknownOrder("Order does not exist.")

        with patch.object(te, "_fetch_order", fake_fetch):
            results = await te.reconcile_open_orders(db, TEST_STAGE)

        assert results[0]["after"] == te.STATUS_FAILED
        await db.refresh(trade)
        assert trade.status == te.STATUS_FAILED
        assert "never reached" in trade.risk_notes["reconciliation"]

    async def test_transient_lookup_failure_leaves_row_for_retry(self, db):
        """A network blip must not be read as "the order never happened" —
        that would lose a real position."""
        trade = await self._orphan(db)

        async def fake_fetch(client, symbol, client_order_id):
            raise ConnectionError("temporary network failure")

        with patch.object(te, "_fetch_order", fake_fetch):
            results = await te.reconcile_open_orders(db, TEST_STAGE)

        assert not results[0]["resolved"]
        await db.refresh(trade)
        assert trade.status == te.STATUS_SUBMITTED, "must stay unresolved for retry"

    async def test_partial_fills_are_reconciled_too(self, db):
        trade = await self._orphan(db, status=te.STATUS_PARTIAL)

        async def fake_fetch(client, symbol, client_order_id):
            return fill_response(qty="1", quote="100", status="FILLED")

        with patch.object(te, "_fetch_order", fake_fetch), \
             patch.object(te, "snapshot_wallet", lambda db, stage: _noop()):
            results = await te.reconcile_open_orders(db, TEST_STAGE)

        assert results[0]["before"] == te.STATUS_PARTIAL
        assert results[0]["after"] == te.STATUS_FILLED

    async def test_settled_rows_are_not_touched(self, db):
        """Filled/failed/cancelled rows have a known outcome already."""
        for status in (te.STATUS_FILLED, te.STATUS_FAILED, te.STATUS_CANCELLED):
            db.add(Trade(
                id=uuid.uuid4(), stage=TEST_STAGE, symbol=SYMBOL, side="buy",
                order_type="market", quantity=1.0, price=100.0,
                risk_decision="approved", status=status, created_at=NOW,
            ))
        await db.commit()

        results = await te.reconcile_open_orders(db, TEST_STAGE)
        assert results == []

    async def test_nothing_to_reconcile_is_clean(self, db):
        assert await te.reconcile_open_orders(db, TEST_STAGE) == []


async def _noop():
    return None


# --------------------------------------------------------------------------
# 3. Error handling
# --------------------------------------------------------------------------


class TestErrorHandling:
    async def _submit_with_error(self, db, error):
        stub = StubExchange(error=error)
        decision = RiskDecision(
            decision=DECISION_APPROVED, reason="ok", checks=[],
            final_quantity=1.0, original_quantity=1.0,
        )
        proposal = TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.9, None)

        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            return await te._submit_order(db, proposal, decision, TEST_STAGE)

    async def test_exchange_error_does_not_propagate(self, db):
        """An exception escaping here would kill the scheduler's trade loop
        with no record of why (§1.7)."""
        outcome = await self._submit_with_error(db, RuntimeError("rate limit -1003"))
        assert not outcome.placed
        assert outcome.status == te.STATUS_FAILED

    async def test_failed_order_is_still_recorded_as_a_trade(self, db):
        """A failed attempt must leave a trace, not disappear."""
        await self._submit_with_error(db, RuntimeError("connection reset"))

        rows = (await db.execute(select(Trade).where(Trade.stage == TEST_STAGE))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == te.STATUS_FAILED
        assert "connection reset" in rows[0].risk_notes["error"]

    async def test_exchange_error_marks_component_unhealthy(self, db):
        """So the risk engine's health check stops further trades (§6)."""
        await self._submit_with_error(db, RuntimeError("rate limit"))

        row = await db.get(ComponentStatus, "binance_api")
        assert row.status == "error"
        assert "rate limit" in row.detail

    async def test_failed_orders_do_not_consume_the_frequency_cap(self, db):
        """A failed order took no position, so it must not count (step 7)."""
        from app.services.risk_engine import count_trades_today

        await self._submit_with_error(db, RuntimeError("boom"))
        assert await count_trades_today(db, TEST_STAGE, NOW) == 0

    async def test_account_state_failure_aborts_before_risk_assessment(self, db):
        """Without balances, position sizing would be computed against a
        wrong wallet total."""
        async def broken_state(db, stage):
            raise te.TradingEngineError("Could not read account state: 503")

        with patch.object(te, "get_account_state", broken_state):
            outcome = await te.place_order(
                db, TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.9, None),
                TEST_STAGE, NOW,
            )

        assert not outcome.placed
        assert outcome.decision == "aborted"
        row = await db.get(ComponentStatus, "binance_api")
        assert row.status == "error"


# --------------------------------------------------------------------------
# 4. Wallet snapshot on fill
# --------------------------------------------------------------------------


class TestSnapshotOnFill:
    async def test_fill_triggers_a_wallet_snapshot(self, db):
        """The daily loss cap reads snapshots; letting them go stale after a
        fill would measure the cap against an out-of-date baseline (§6)."""
        before = len((await db.execute(
            select(WalletSnapshot).where(WalletSnapshot.stage == TEST_STAGE)
        )).scalars().all())

        stub = StubExchange(response=fill_response(qty="1", quote="100"))
        decision = RiskDecision(
            decision=DECISION_APPROVED, reason="ok", checks=[],
            final_quantity=1.0, original_quantity=1.0,
        )
        proposal = TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.9, None)

        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            outcome = await te._submit_order(db, proposal, decision, TEST_STAGE)

        assert outcome.filled_quantity == pytest.approx(1.0)
        after = len((await db.execute(
            select(WalletSnapshot).where(WalletSnapshot.stage == TEST_STAGE)
        )).scalars().all())
        assert after == before + 1

    async def test_unfilled_order_does_not_snapshot(self, db):
        stub = StubExchange(
            response=fill_response(qty="0", quote="0", status="NEW", fee="0")
        )
        decision = RiskDecision(
            decision=DECISION_APPROVED, reason="ok", checks=[],
            final_quantity=1.0, original_quantity=1.0,
        )
        proposal = TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.9, None)

        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            await te._submit_order(db, proposal, decision, TEST_STAGE)

        rows = (await db.execute(
            select(WalletSnapshot).where(WalletSnapshot.stage == TEST_STAGE)
        )).scalars().all()
        assert rows == []


# --------------------------------------------------------------------------
# 5. Realized performance from real trade rows
# --------------------------------------------------------------------------


class TestRealizedPerformance:
    async def _trade(self, db, side, qty, price, hours, status=te.STATUS_FILLED, fee=0.0, model=None):
        db.add(Trade(
            id=uuid.uuid4(), stage=TEST_STAGE, symbol=SYMBOL, side=side,
            order_type="market", quantity=qty, price=price, model_id=model,
            risk_decision="approved", status=status, fee_usdt=fee,
            created_at=NOW + timedelta(hours=hours),
        ))
        await db.flush()

    async def test_win_rate_from_a_known_sequence(self, db):
        """Three round trips: +10, -5, +20 -> 2 wins of 3 = 0.667."""
        await self._trade(db, "buy", 1, 100, 1)
        await self._trade(db, "sell", 1, 110, 2)
        await self._trade(db, "buy", 1, 100, 3)
        await self._trade(db, "sell", 1, 95, 4)
        await self._trade(db, "buy", 1, 100, 5)
        await self._trade(db, "sell", 1, 120, 6)
        await db.commit()

        stats = await te.get_realized_performance(db, TEST_STAGE)

        assert stats["closed_trades"] == 3
        assert stats["wins"] == 2
        assert stats["win_rate"] == pytest.approx(2 / 3)
        assert stats["total_realized_pnl"] == pytest.approx(25.0)

    async def test_unfilled_trades_excluded_from_matching(self, db):
        """A submitted-but-unfilled order took no position; counting it would
        invent a trade that never happened."""
        await self._trade(db, "buy", 1, 100, 1)
        await self._trade(db, "sell", 1, 110, 2)
        await self._trade(db, "buy", 99, 100, 3, status=te.STATUS_SUBMITTED)
        await self._trade(db, "buy", 99, 100, 4, status=te.STATUS_FAILED)
        await db.commit()

        stats = await te.get_realized_performance(db, TEST_STAGE)
        assert stats["closed_trades"] == 1
        assert stats["open_lots"] == 0

    async def test_open_position_reported_but_not_counted_as_closed(self, db):
        await self._trade(db, "buy", 1, 100, 1)
        await db.commit()

        stats = await te.get_realized_performance(db, TEST_STAGE)
        assert stats["closed_trades"] == 0
        assert stats["win_rate"] is None
        assert stats["open_lots"] == 1

    async def test_model_win_rate_replaces_the_none_placeholder(self, db):
        """Step 6's get_trading_stats returned None for win_rate; it now
        computes a real one from closed positions.

        Model A opens three positions (+10, -5, +20) -> 2/3 wins.
        Model B opens one (-50) -> 0/1. Attribution must not mix them.
        """
        from app.services.model_registry import get_trading_stats

        model_a, model_b = uuid.uuid4(), uuid.uuid4()
        for model_id in (model_a, model_b):
            db.add(Model(
                id=model_id, symbol=SYMBOL, model_type="xgboost_classifier",
                file_path="/tmp/none.json", trained_at=NOW, metrics={},
                status="archived",
            ))
        await db.flush()

        for entry, exit_, hour in [(100, 110, 1), (100, 95, 3), (100, 120, 5)]:
            await self._trade(db, "buy", 1, entry, hour, model=model_a)
            await self._trade(db, "sell", 1, exit_, hour + 1, model=None)
        await self._trade(db, "buy", 1, 100, 9, model=model_b)
        await self._trade(db, "sell", 1, 50, 10, model=None)
        await db.commit()

        stats_a = await get_trading_stats(db, model_a, stage=TEST_STAGE)
        assert stats_a["closed_trades"] == 3
        assert stats_a["win_rate"] == pytest.approx(2 / 3)
        assert stats_a["total_realized_pnl"] == pytest.approx(25.0)
        assert stats_a["pnl_available"] is True

        stats_b = await get_trading_stats(db, model_b, stage=TEST_STAGE)
        assert stats_b["closed_trades"] == 1
        assert stats_b["win_rate"] == pytest.approx(0.0)
        assert stats_b["total_realized_pnl"] == pytest.approx(-50.0)

    async def test_model_with_no_closed_positions_reports_none(self, db):
        """None, not 0.0 — the scorer must not read "no data" as "all losses"."""
        from app.services.model_registry import get_trading_stats

        stats = await get_trading_stats(db, uuid.uuid4(), stage=TEST_STAGE)
        assert stats["win_rate"] is None
        assert stats["pnl_available"] is False


# --------------------------------------------------------------------------
# 6. Live stage is blocked until the promotion gate exists
# --------------------------------------------------------------------------


class TestLiveStageBlocked:
    """`get_trading_client` returns a PRODUCTION client for stage 'live', so
    without an explicit block, setting current_stage='live' in the config
    table would place real-money orders before the §5.4 gate exists."""

    async def test_place_order_refuses_live_stage(self, db):
        stub = StubExchange()
        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            with pytest.raises(te.LiveStageNotEnabled):
                await te.place_order(
                    db, TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.9, None),
                    "live", NOW,
                )
        assert stub.orders == []

    async def test_submit_order_refuses_live_stage_even_when_approved(self, db):
        stub = StubExchange()
        decision = RiskDecision(
            decision=DECISION_APPROVED, reason="ok", checks=[],
            final_quantity=1.0, original_quantity=1.0,
        )
        p1, p2, p3 = patch_exchange(stub)
        with p1, p2, p3:
            with pytest.raises(te.LiveStageNotEnabled):
                await te._submit_order(
                    db, TradeProposal(SYMBOL, "buy", 1.0, 100.0, 0.9, None),
                    decision, "live",
                )
        assert stub.orders == []

    async def test_paper_stage_is_unaffected(self, db):
        te.assert_paper_stage("paper")
        te.assert_paper_stage(TEST_STAGE)


# --------------------------------------------------------------------------
# 7. Reconciliation retry, backoff and escalation
# --------------------------------------------------------------------------


class TestReconcileRetryAndEscalation:
    async def _orphan(self, db):
        trade = Trade(
            id=uuid.uuid4(), stage=TEST_STAGE, symbol=SYMBOL, side="buy",
            order_type="market", quantity=1.0, price=None,
            risk_decision="approved", status=te.STATUS_SUBMITTED, created_at=NOW,
        )
        db.add(trade)
        await db.commit()
        return trade

    @staticmethod
    def _failing_lookup():
        async def fake(client, symbol, client_order_id):
            raise ConnectionError("temporary network failure")
        return fake

    async def test_first_failure_records_backoff_not_escalation(self, db):
        trade = await self._orphan(db)

        with patch.object(te, "_fetch_order", self._failing_lookup()):
            results = await te.reconcile_open_orders(db, TEST_STAGE)

        assert results[0]["attempts"] == 1
        assert not results[0]["needs_attention"]
        assert results[0]["retry_in_seconds"] == 30

        await db.refresh(trade)
        state = trade.risk_notes["reconcile"]
        assert state["attempts"] == 1
        assert state["needs_attention"] is False
        assert "next_attempt_at" in state

    async def test_order_inside_backoff_is_skipped(self, db):
        """A retry storm against an unreachable exchange helps nobody."""
        await self._orphan(db)

        with patch.object(te, "_fetch_order", self._failing_lookup()):
            first = await te.reconcile_open_orders(db, TEST_STAGE)
            assert len(first) == 1
            # Immediately again — still inside the 30s window.
            second = await te.reconcile_open_orders(db, TEST_STAGE)

        assert second == []

    async def test_escalates_after_the_attempt_cap(self, db):
        trade = await self._orphan(db)
        max_attempts = 6

        with patch.object(te, "_fetch_order", self._failing_lookup()):
            for _ in range(max_attempts):
                # Force each attempt to be due by clearing the backoff.
                state = dict(te._reconcile_state(trade))
                state.pop("next_attempt_at", None)
                te._set_reconcile_state(trade, state)
                await db.commit()
                results = await te.reconcile_open_orders(db, TEST_STAGE)

        assert results[0]["needs_attention"]
        assert results[0]["attempts"] == max_attempts

        await db.refresh(trade)
        assert trade.risk_notes["reconcile"]["needs_attention"] is True
        # Still unresolved — escalation is not a resolution.
        assert trade.status == te.STATUS_SUBMITTED

    async def test_escalated_order_surfaces_on_component_status(self, db):
        """The alert path: it must be visible on the dashboard, not just in
        the log (§1.7, §8.1)."""
        trade = await self._orphan(db)
        te._set_reconcile_state(trade, {"attempts": 6, "needs_attention": True})
        await db.commit()

        count = await te.refresh_reconciliation_alert(db, TEST_STAGE)

        assert count == 1
        row = await db.get(ComponentStatus, te.COMPONENT_RECONCILIATION)
        assert row.status == "error"
        assert str(trade.id) in row.detail

    async def test_alert_clears_once_nothing_is_stuck(self, db):
        await te.refresh_reconciliation_alert(db, TEST_STAGE)
        row = await db.get(ComponentStatus, te.COMPONENT_RECONCILIATION)
        assert row.status == "online"

    async def test_escalated_orders_are_listed_for_the_dashboard(self, db):
        trade = await self._orphan(db)
        te._set_reconcile_state(trade, {"attempts": 6, "needs_attention": True})
        await db.commit()

        stuck = await te.get_orders_needing_attention(db, TEST_STAGE)
        assert [str(t.id) for t in stuck] == [str(trade.id)]

    async def test_successful_retry_clears_the_retry_state(self, db):
        trade = await self._orphan(db)

        with patch.object(te, "_fetch_order", self._failing_lookup()):
            await te.reconcile_open_orders(db, TEST_STAGE)

        async def succeeds(client, symbol, client_order_id):
            return fill_response(order_id=42, qty="1", quote="100")

        # Clear the backoff so the retry is due.
        state = dict(te._reconcile_state(trade))
        state.pop("next_attempt_at", None)
        te._set_reconcile_state(trade, state)
        await db.commit()

        with patch.object(te, "_fetch_order", succeeds), \
             patch.object(te, "snapshot_wallet", lambda db, stage: _noop()):
            results = await te.reconcile_open_orders(db, TEST_STAGE)

        assert results[0]["resolved"]
        await db.refresh(trade)
        assert trade.status == te.STATUS_FILLED
        assert trade.risk_notes["reconcile"]["attempts"] == 0
