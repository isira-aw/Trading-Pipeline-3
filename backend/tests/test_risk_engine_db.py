"""Integration tests for the risk engine's I/O layer (§6).

Covers the context builders that read real tables — especially the daily
P&L baseline, whose cold-start behaviour decides whether the system trades
blind after a restart.

Skipped when no database is reachable.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Candle, RiskLog, Trade, WalletSnapshot
from app.services.risk_engine import (
    ACTION_REJECT,
    CHECK_DAILY_LOSS,
    DECISION_REJECTED,
    TradeProposal,
    assess,
    count_trades_today,
    get_daily_pnl,
    get_volatility_stats,
)
from app.services.wallet_service import ensure_daily_baseline, take_snapshot

TEST_STAGE = "risktest"
TEST_SYMBOL = "RISKTESTUSDT"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
START_OF_DAY = NOW.replace(hour=0, minute=0, second=0, microsecond=0)

async def _db_reachable(db_url) -> bool:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect():
            return True
    except Exception:
        return False
    finally:
        await engine.dispose()


async def _cleanup(session):
    await session.execute(delete(WalletSnapshot).where(WalletSnapshot.stage == TEST_STAGE))
    await session.execute(delete(Trade).where(Trade.stage == TEST_STAGE))
    await session.execute(delete(Candle).where(Candle.symbol == TEST_SYMBOL))
    await session.commit()


@pytest_asyncio.fixture
async def db(test_database_url):
    if not await _db_reachable(test_database_url):
        pytest.skip(f"No database reachable at {test_database_url}")

    engine = create_async_engine(test_database_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as session:
        await _cleanup(session)
        try:
            yield session
        finally:
            await session.rollback()
            await _cleanup(session)

    await engine.dispose()


class TestDailyPnlColdStart:
    """The two cold-start failure modes, which are distinct problems."""

    async def test_no_snapshot_exists_at_all(self, db):
        """Case 1: fresh install / empty table.

        Must report unavailable so the rule rejects. Passing through with an
        implicit 0% would let the system trade with no loss cap at all.
        """
        pnl = await get_daily_pnl(db, TEST_STAGE, NOW, max_baseline_age_hours=24)

        assert pnl.available is False
        assert pnl.pnl_pct is None
        assert "no wallet snapshot" in pnl.reason

    async def test_snapshot_exists_but_is_stale_from_multi_day_gap(self, db):
        """Case 2: the bot was down for days and restarted.

        A 3-day-old snapshot is a real baseline, but not *today's* — using it
        would report a 3-day change as today's P&L and could either mask a
        breach or invent one.
        """
        await take_snapshot(
            db, TEST_STAGE, {"USDT": 10_000}, 10_000.0, NOW - timedelta(days=3)
        )
        await db.commit()

        pnl = await get_daily_pnl(db, TEST_STAGE, NOW, max_baseline_age_hours=24)

        assert pnl.available is False
        assert "stale" in pnl.reason
        assert "multi-day gap" in pnl.reason
        # Distinguishable from case 1, so risk_log tells them apart.
        assert "no wallet snapshot" not in pnl.reason

    async def test_no_snapshot_rejects_through_the_full_path(self, db):
        """Case 1, end to end: an otherwise-perfect trade is still rejected."""
        proposal = TradeProposal(TEST_SYMBOL, "buy", 0.01, 100.0, 0.99, None)
        decision, entry = await assess(db, proposal, TEST_STAGE, 10_000.0, 0.0, NOW)
        await db.commit()

        assert decision.decision == DECISION_REJECTED
        daily = decision.checks_dict()[CHECK_DAILY_LOSS]
        assert daily["action"] == ACTION_REJECT
        assert "no wallet snapshot" in daily["detail"]

    async def test_stale_snapshot_rejects_through_the_full_path(self, db):
        """Case 2, end to end, with a reason distinct from case 1."""
        await take_snapshot(
            db, TEST_STAGE, {"USDT": 10_000}, 10_000.0, NOW - timedelta(days=3)
        )
        await db.commit()

        proposal = TradeProposal(TEST_SYMBOL, "buy", 0.01, 100.0, 0.99, None)
        decision, _ = await assess(db, proposal, TEST_STAGE, 10_000.0, 0.0, NOW)
        await db.commit()

        assert decision.decision == DECISION_REJECTED
        daily = decision.checks_dict()[CHECK_DAILY_LOSS]
        assert daily["action"] == ACTION_REJECT
        assert "stale" in daily["detail"]

    async def test_ensure_daily_baseline_closes_the_gap(self, db):
        """The mitigation: startup takes a snapshot so the gap is not routine."""
        created = await ensure_daily_baseline(
            db, TEST_STAGE, {"USDT": 5_000}, 5_000.0, NOW
        )
        await db.commit()
        assert created is not None

        pnl = await get_daily_pnl(db, TEST_STAGE, NOW, max_baseline_age_hours=24)
        assert pnl.available is True
        assert pnl.pnl_pct == pytest.approx(0.0)

    async def test_ensure_daily_baseline_is_idempotent(self, db):
        await ensure_daily_baseline(db, TEST_STAGE, {"USDT": 5_000}, 5_000.0, NOW)
        await db.commit()

        second = await ensure_daily_baseline(
            db, TEST_STAGE, {"USDT": 9_999}, 9_999.0, NOW
        )
        await db.commit()

        assert second is None, "must not overwrite today's existing baseline"


class TestDailyPnlCalculation:
    async def test_uses_day_open_not_first_snapshot_today(self, db):
        """Baseline is the last snapshot before the day boundary, so a move
        that happened before the bot started today is still counted."""
        await take_snapshot(
            db, TEST_STAGE, {"USDT": 10_000}, 10_000.0, START_OF_DAY - timedelta(hours=1)
        )
        # Bot starts mid-morning, already down 4%.
        await take_snapshot(
            db, TEST_STAGE, {"USDT": 9_600}, 9_600.0, START_OF_DAY + timedelta(hours=6)
        )
        await take_snapshot(db, TEST_STAGE, {"USDT": 9_500}, 9_500.0, NOW)
        await db.commit()

        pnl = await get_daily_pnl(db, TEST_STAGE, NOW, max_baseline_age_hours=24)

        # Against day-open 10000 -> -5%. Against today's first (9600) it
        # would have read as only -1.04% and missed the cap breach.
        assert pnl.pnl_pct == pytest.approx(-5.0)

    async def test_first_day_falls_back_to_earliest_snapshot_today(self, db):
        await take_snapshot(
            db, TEST_STAGE, {"USDT": 1_000}, 1_000.0, START_OF_DAY + timedelta(hours=1)
        )
        await take_snapshot(db, TEST_STAGE, {"USDT": 1_100}, 1_100.0, NOW)
        await db.commit()

        pnl = await get_daily_pnl(db, TEST_STAGE, NOW, max_baseline_age_hours=24)

        assert pnl.available is True
        assert pnl.pnl_pct == pytest.approx(10.0)

    async def test_profit_is_positive(self, db):
        await take_snapshot(
            db, TEST_STAGE, {"USDT": 1_000}, 1_000.0, START_OF_DAY - timedelta(minutes=5)
        )
        await take_snapshot(db, TEST_STAGE, {"USDT": 1_250}, 1_250.0, NOW)
        await db.commit()

        pnl = await get_daily_pnl(db, TEST_STAGE, NOW, max_baseline_age_hours=24)
        assert pnl.pnl_pct == pytest.approx(25.0)

    async def test_zero_baseline_value_does_not_divide_by_zero(self, db):
        await take_snapshot(
            db, TEST_STAGE, {}, 0.0, START_OF_DAY - timedelta(minutes=5)
        )
        await take_snapshot(db, TEST_STAGE, {"USDT": 100}, 100.0, NOW)
        await db.commit()

        pnl = await get_daily_pnl(db, TEST_STAGE, NOW, max_baseline_age_hours=24)
        assert pnl.available is False

    async def test_other_stages_do_not_leak_into_the_baseline(self, db):
        """Paper and live P&L must never be mixed."""
        await take_snapshot(
            db, "otherstg", {"USDT": 1}, 1.0, START_OF_DAY - timedelta(hours=1)
        )
        await db.commit()

        pnl = await get_daily_pnl(db, TEST_STAGE, NOW, max_baseline_age_hours=24)
        assert pnl.available is False

        await db.execute(
            delete(WalletSnapshot).where(WalletSnapshot.stage == "otherstg")
        )
        await db.commit()


class TestTradeFrequencyCounting:
    async def _trade(self, db, status, created_at, side="buy"):
        db.add(Trade(
            id=uuid.uuid4(), stage=TEST_STAGE, symbol=TEST_SYMBOL, side=side,
            order_type="market", quantity=1, price=100, risk_decision="approved",
            status=status, created_at=created_at,
        ))
        await db.flush()

    async def test_counts_filled_trades_today(self, db):
        await self._trade(db, "filled", NOW - timedelta(hours=1))
        await self._trade(db, "filled", NOW - timedelta(hours=2))
        await db.commit()

        assert await count_trades_today(db, TEST_STAGE, NOW) == 2

    async def test_counts_partial_fills(self, db):
        await self._trade(db, "partial", NOW - timedelta(hours=1))
        await db.commit()
        assert await count_trades_today(db, TEST_STAGE, NOW) == 1

    async def test_excludes_failed_and_cancelled_orders(self, db):
        """An order that never took a position must not consume the cap."""
        await self._trade(db, "failed", NOW - timedelta(hours=1))
        await self._trade(db, "cancelled", NOW - timedelta(hours=2))
        await db.commit()

        assert await count_trades_today(db, TEST_STAGE, NOW) == 0

    async def test_excludes_yesterdays_trades(self, db):
        await self._trade(db, "filled", START_OF_DAY - timedelta(minutes=1))
        await db.commit()
        assert await count_trades_today(db, TEST_STAGE, NOW) == 0

    async def test_rejected_proposals_never_reach_trades_so_never_count(self, db):
        """The refinement that matters: six rejections for unrelated reasons
        must not exhaust the frequency cap.

        Rejections are written to risk_log only, so the trade count stays 0
        and a later reader cannot mistake them for "hit the daily limit".
        """
        proposal = TradeProposal(TEST_SYMBOL, "buy", 1.0, 100.0, 0.05, None)
        for _ in range(6):
            await assess(db, proposal, TEST_STAGE, 10_000.0, 0.0, NOW)
        await db.commit()

        assert await count_trades_today(db, TEST_STAGE, NOW) == 0

        logged = (await db.execute(
            RiskLog.__table__.select().where(RiskLog.decision == DECISION_REJECTED)
        )).all()
        assert len(logged) >= 6


class TestVolatilityStatsFromDb:
    async def _candles(self, db, count, volume=100.0, last_range=None):
        base = NOW - timedelta(hours=4 * count)
        for i in range(count):
            close = 100.0
            spread = 1.0
            if last_range is not None and i == count - 1:
                spread = last_range
            db.add(Candle(
                symbol=TEST_SYMBOL, interval="4h",
                open_time=base + timedelta(hours=4 * i),
                open=close, high=close + spread / 2, low=close - spread / 2,
                close=close, volume=volume,
            ))
        await db.flush()

    async def test_insufficient_history_is_unavailable(self, db):
        await self._candles(db, 5)
        await db.commit()

        stats = await get_volatility_stats(db, TEST_SYMBOL, "4h", 100)
        assert stats.available is False
        assert "need at least 20" in stats.reason

    async def test_normal_history_produces_finite_z_scores(self, db):
        await self._candles(db, 60)
        await db.commit()

        stats = await get_volatility_stats(db, TEST_SYMBOL, "4h", 100)
        assert stats.available is True
        assert stats.range_z == pytest.approx(0.0, abs=1e-6)

    async def test_range_spike_produces_large_z(self, db):
        """A flash event or bad tick should show up as a big z-score."""
        await self._candles(db, 60, last_range=50.0)
        await db.commit()

        stats = await get_volatility_stats(db, TEST_SYMBOL, "4h", 100)
        assert stats.range_z == float("inf") or stats.range_z > 3.0


class TestRiskLogPersistence:
    async def test_every_attempt_is_logged_including_approvals(self, db):
        """§6: risk_log records every attempt, not only rejections."""
        await take_snapshot(
            db, TEST_STAGE, {"USDT": 10_000}, 10_000.0, START_OF_DAY - timedelta(hours=1)
        )
        await take_snapshot(db, TEST_STAGE, {"USDT": 10_000}, 10_000.0, NOW)
        await db.commit()

        before = len((await db.execute(RiskLog.__table__.select())).all())

        proposal = TradeProposal(TEST_SYMBOL, "buy", 0.01, 100.0, 0.9, None)
        decision, entry = await assess(db, proposal, TEST_STAGE, 10_000.0, 0.0, NOW)
        await db.commit()

        after = len((await db.execute(RiskLog.__table__.select())).all())
        assert after == before + 1
        assert entry.decision == decision.decision

    async def test_rejected_entry_embeds_the_proposal_snapshot(self, db):
        """A rejection has trade_id NULL, so the proposal must be recoverable
        from the checks JSONB alone (§3)."""
        proposal = TradeProposal(TEST_SYMBOL, "buy", 0.5, 200.0, 0.05, None)
        _, entry = await assess(db, proposal, TEST_STAGE, 10_000.0, 0.0, NOW)
        await db.commit()

        assert entry.trade_id is None

        snapshot = entry.checks["proposal"]
        assert snapshot["symbol"] == TEST_SYMBOL
        assert snapshot["side"] == "buy"
        assert snapshot["quantity"] == 0.5
        assert snapshot["price"] == 200.0
        assert snapshot["notional_usdt"] == pytest.approx(100.0)
        assert snapshot["confidence"] == 0.05

    async def test_all_seven_checks_recorded_on_every_entry(self, db):
        proposal = TradeProposal(TEST_SYMBOL, "buy", 0.5, 200.0, 0.05, None)
        _, entry = await assess(db, proposal, TEST_STAGE, 10_000.0, 0.0, NOW)
        await db.commit()

        assert len(entry.checks["results"]) == 7
