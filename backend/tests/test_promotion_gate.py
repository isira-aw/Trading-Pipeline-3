"""Promotion gate and stage switch (§5.4, §7, §10).

This is the last control between a simulation and real money, so the tests
that matter most are the ones proving a *correct PIN alone* is not enough,
and that the gate's own thresholds cannot be quietly rewritten to make an
untested system pass.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Config, Trade, WalletSnapshot
from app.services import promotion_gate as gate_service
from app.services.security import DEFAULT_PIN, hash_pin

PAPER = "paper"
SYMBOL = "GATETESTUSDT"
NOW = datetime.now(timezone.utc)

STRICT_GATE = {
    "min_paper_trading_days": 30,
    "min_trade_count": 40,
    "min_win_rate": 0.52,
    "max_drawdown_pct": 15,
}

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


async def _set(session, key, value):
    await session.execute(delete(Config).where(Config.key == key))
    session.add(Config(key=key, value=value))
    await session.commit()


@pytest_asyncio.fixture
async def db():
    if not await _db_reachable():
        pytest.skip(f"No database reachable at {DB_URL}")

    engine = create_async_engine(DB_URL)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as session:
        await session.execute(delete(Trade).where(Trade.stage == PAPER))
        await session.execute(delete(WalletSnapshot).where(WalletSnapshot.stage == PAPER))
        await _set(session, "promotion_gate", STRICT_GATE)
        await _set(session, "stage_pin_hash", hash_pin(DEFAULT_PIN))
        await _set(session, "current_stage", "setup")
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(Trade).where(Trade.stage == PAPER))
            await session.execute(delete(WalletSnapshot).where(WalletSnapshot.stage == PAPER))
            await _set(session, "promotion_gate", STRICT_GATE)
            await _set(session, "current_stage", "setup")
            await session.commit()

    await engine.dispose()


@pytest_asyncio.fixture
async def client(db):
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _round_trips(db, count, win_ratio=1.0, days_ago=60, entry=100.0):
    """Create `count` closed paper positions, a share of them winners.

    Wins and losses are interleaved evenly rather than grouped. Putting all
    the winners first produces an equity curve that rises then falls, and
    therefore a large drawdown that says more about the fixture than the
    strategy.
    """
    start = NOW - timedelta(days=days_ago)
    wins = int(count * win_ratio)

    for i in range(count):
        # Bresenham-style spread: True on exactly `wins` of `count` steps.
        is_win = (i * wins) // count != ((i + 1) * wins) // count
        exit_price = entry * (1.10 if is_win else 0.95)
        opened = start + timedelta(hours=i * 2)
        db.add(Trade(
            id=uuid.uuid4(), stage=PAPER, symbol=SYMBOL, side="buy",
            order_type="market", quantity=1, price=entry, risk_decision="approved",
            status="filled", fee_usdt=0, created_at=opened,
        ))
        db.add(Trade(
            id=uuid.uuid4(), stage=PAPER, symbol=SYMBOL, side="sell",
            order_type="market", quantity=1, price=exit_price,
            risk_decision="approved", status="filled", fee_usdt=0,
            created_at=opened + timedelta(hours=1),
        ))
    await db.commit()
    await _equity_curve(db, count, win_ratio, days_ago, entry)


async def _equity_curve(db, count, win_ratio, days_ago, entry, start_equity=10_000.0):
    """Wallet snapshots tracking the same sequence.

    The drawdown criterion is measured against account equity, so a record
    without snapshots has no drawdown history to judge and fails.
    """
    start = NOW - timedelta(days=days_ago)
    wins = int(count * win_ratio)
    equity = start_equity

    db.add(WalletSnapshot(
        stage=PAPER, balances={"USDT": equity}, total_value_usdt=equity,
        snapshot_at=start - timedelta(minutes=1),
    ))
    for i in range(count):
        is_win = (i * wins) // count != ((i + 1) * wins) // count
        equity += (entry * 0.10) if is_win else -(entry * 0.05)
        db.add(WalletSnapshot(
            stage=PAPER, balances={"USDT": equity}, total_value_usdt=equity,
            snapshot_at=start + timedelta(hours=i * 2 + 1),
        ))
    await db.commit()


class TestGateEvaluation:
    async def test_empty_history_fails_every_criterion(self, db):
        """§1.7: absent evidence is not evidence of success."""
        result = await gate_service.evaluate_gate(db)

        assert not result.passed
        assert len(result.criteria) == 4
        assert all(not c.passed for c in result.criteria)

    async def test_absent_equity_history_is_not_a_pass(self, db):
        """The subtle one: with nothing recorded, drawdown would be
        trivially 0%, sailing past a 15% limit and certifying an untested
        system."""
        result = await gate_service.evaluate_gate(db)
        drawdown = [c for c in result.criteria if c.name == "max_drawdown_pct"][0]

        assert drawdown.current is None
        assert not drawdown.passed
        assert "Not enough wallet history" in drawdown.detail

    async def test_drawdown_measured_against_equity_not_the_pnl_curve(self, db):
        """A P&L curve starting at zero makes the first small loss after the
        first small win read as a ~100% drawdown. Equity is the right base."""
        await _round_trips(db, 50, win_ratio=0.7, days_ago=60)
        result = await gate_service.evaluate_gate(db)

        equity_dd = result.stats["max_drawdown_pct"]
        pnl_curve_dd = result.stats["realized_pnl_curve_drawdown_pct"]

        assert equity_dd < 5.0, "equity drawdown should be small on this record"
        assert pnl_curve_dd > equity_dd, (
            "the P&L-curve metric should be the noisier one — if not, this "
            "test no longer demonstrates why equity is used"
        )

    async def test_no_closed_positions_means_no_win_rate(self, db):
        result = await gate_service.evaluate_gate(db)
        win_rate = [c for c in result.criteria if c.name == "min_win_rate"][0]

        assert win_rate.current is None
        assert not win_rate.passed

    async def test_all_criteria_reported_not_just_the_first_failure(self, db):
        """The dashboard needs a checklist, not a single objection."""
        await _round_trips(db, 3, win_ratio=0.0, days_ago=1)
        result = await gate_service.evaluate_gate(db)

        names = {c.name for c in result.criteria}
        assert names == set(STRICT_GATE)

    async def test_full_pass_on_a_good_record(self, db):
        await _round_trips(db, 50, win_ratio=0.7, days_ago=60)
        result = await gate_service.evaluate_gate(db)

        assert result.passed, result.summary()
        assert result.stats["closed_trades"] == 50
        assert result.stats["win_rate"] == pytest.approx(0.7)

    async def test_too_few_trades_fails_even_with_a_perfect_win_rate(self, db):
        await _round_trips(db, 5, win_ratio=1.0, days_ago=60)
        result = await gate_service.evaluate_gate(db)

        assert not result.passed
        failed = {c.name for c in result.failures()}
        assert "min_trade_count" in failed
        assert "min_win_rate" not in failed

    async def test_too_short_a_record_fails_even_with_enough_trades(self, db):
        """40 good trades crammed into two days is not 30 days of evidence."""
        await _round_trips(db, 50, win_ratio=0.7, days_ago=2)
        result = await gate_service.evaluate_gate(db)

        assert not result.passed
        assert "min_paper_trading_days" in {c.name for c in result.failures()}

    async def test_poor_win_rate_fails(self, db):
        await _round_trips(db, 50, win_ratio=0.3, days_ago=60)
        result = await gate_service.evaluate_gate(db)

        assert not result.passed
        assert "min_win_rate" in {c.name for c in result.failures()}

    async def test_only_paper_stage_counts(self, db):
        """A live or test-stage record must not qualify the paper gate."""
        for i in range(50):
            db.add(Trade(
                id=uuid.uuid4(), stage="someother", symbol=SYMBOL, side="buy",
                order_type="market", quantity=1, price=100,
                risk_decision="approved", status="filled",
                created_at=NOW - timedelta(days=60),
            ))
        await db.commit()

        result = await gate_service.evaluate_gate(db)
        assert not result.passed
        assert result.stats["closed_trades"] == 0

        await db.execute(delete(Trade).where(Trade.stage == "someother"))
        await db.commit()

    async def test_unfilled_trades_do_not_count(self, db):
        await _round_trips(db, 50, win_ratio=0.7, days_ago=60)
        for _ in range(100):
            db.add(Trade(
                id=uuid.uuid4(), stage=PAPER, symbol=SYMBOL, side="buy",
                order_type="market", quantity=1, price=100,
                risk_decision="approved", status="failed",
                created_at=NOW - timedelta(days=1),
            ))
        await db.commit()

        result = await gate_service.evaluate_gate(db)
        assert result.stats["closed_trades"] == 50


class TestStageSwitchApi:
    async def test_switch_to_live_refused_when_the_gate_fails(self, client, db):
        """The headline case: the PIN is correct and it still does not pass."""
        await _round_trips(db, 3, win_ratio=1.0, days_ago=1)

        response = await client.post(
            "/api/stage/switch", json={"stage": "live", "pin": DEFAULT_PIN}
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "Promotion gate not met" in detail["message"]
        assert not detail["gate"]["passed"]

        stage = (await db.execute(
            select(Config.value).where(Config.key == "current_stage")
        )).scalar_one()
        assert stage == "setup", "stage changed despite the gate failing"

    async def test_switch_to_live_succeeds_when_the_gate_passes(self, client, db):
        await _round_trips(db, 50, win_ratio=0.7, days_ago=60)

        response = await client.post(
            "/api/stage/switch", json={"stage": "live", "pin": DEFAULT_PIN}
        )

        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["switched"] and body["to"] == "live"
        # A switch must never start trading by itself.
        assert body["trading_enabled"] is False

    async def test_wrong_pin_refused_even_when_the_gate_passes(self, client, db):
        await _round_trips(db, 50, win_ratio=0.7, days_ago=60)

        response = await client.post(
            "/api/stage/switch", json={"stage": "live", "pin": "999999"}
        )
        assert response.status_code == 403

    async def test_pin_and_gate_failures_are_distinguishable(self, client, db):
        """403 vs 409: conflating them sends someone hunting for a PIN
        problem they do not have."""
        await _round_trips(db, 3, win_ratio=1.0, days_ago=1)

        bad_pin = await client.post(
            "/api/stage/switch", json={"stage": "live", "pin": "999999"}
        )
        good_pin = await client.post(
            "/api/stage/switch", json={"stage": "live", "pin": DEFAULT_PIN}
        )

        assert bad_pin.status_code == 403
        assert good_pin.status_code == 409

    async def test_switch_to_paper_needs_no_gate(self, client, db):
        """The gate guards live only; paper must stay reachable."""
        response = await client.post(
            "/api/stage/switch", json={"stage": "paper", "pin": DEFAULT_PIN}
        )
        assert response.status_code == 200

    async def test_unknown_stage_refused(self, client):
        response = await client.post(
            "/api/stage/switch", json={"stage": "moon", "pin": DEFAULT_PIN}
        )
        assert response.status_code == 422

    async def test_default_pin_warning_is_returned(self, client, db):
        """§7/§10: unmissable, though not blocking."""
        await _round_trips(db, 50, win_ratio=0.7, days_ago=60)

        response = await client.post(
            "/api/stage/switch", json={"stage": "live", "pin": DEFAULT_PIN}
        )
        warning = response.json()["pin_warning"]

        assert warning is not None
        assert warning["level"] == "critical"
        assert DEFAULT_PIN in warning["message"]

    async def test_no_warning_once_the_pin_is_changed(self, client, db):
        await _set(db, "stage_pin_hash", hash_pin("246810"))
        await _round_trips(db, 50, win_ratio=0.7, days_ago=60)

        response = await client.post(
            "/api/stage/switch", json={"stage": "live", "pin": "246810"}
        )
        assert response.json()["pin_warning"] is None

    async def test_gate_endpoint_reports_the_checklist(self, client, db):
        await _round_trips(db, 10, win_ratio=0.6, days_ago=60)

        body = (await client.get("/api/stage/gate")).json()

        assert body["passed"] is False
        assert len(body["criteria"]) == 4
        for criterion in body["criteria"]:
            assert {"name", "label", "passed", "current", "required"} <= set(criterion)
        assert body["can_switch_to_live"] is False


class TestGateTamperingRoutes:
    """The gate is only as strong as its thresholds.

    Setting min_win_rate to 0 and min_trade_count to 1 makes an untested
    system pass instantly, so editing a threshold is exactly as powerful as
    switching stages and must take the same credential.
    """

    async def test_generic_config_route_refuses_the_gate(self, client):
        response = await client.put(
            "/api/config/promotion_gate",
            json={"value": {"min_paper_trading_days": 0, "min_trade_count": 0,
                            "min_win_rate": 0, "max_drawdown_pct": 100}},
        )
        assert response.status_code == 403
        assert "PIN-gated" in response.json()["detail"]

    async def test_gate_change_requires_the_pin(self, client):
        response = await client.put(
            "/api/stage/gate",
            json={"pin": "999999", "thresholds": STRICT_GATE},
        )
        assert response.status_code == 403

    async def test_gate_cannot_be_zeroed_out(self, client):
        """A gate demanding nothing is not a gate."""
        response = await client.put(
            "/api/stage/gate",
            json={"pin": DEFAULT_PIN, "thresholds": {
                "min_paper_trading_days": 0, "min_trade_count": 0,
                "min_win_rate": 0.0, "max_drawdown_pct": 100,
            }},
        )
        assert response.status_code == 422
        assert "min_paper_trading_days" in response.json()["detail"]

    async def test_partial_thresholds_refused(self, client):
        """Omitting a key must not silently default it to zero."""
        response = await client.put(
            "/api/stage/gate",
            json={"pin": DEFAULT_PIN, "thresholds": {"min_win_rate": 0.9}},
        )
        assert response.status_code == 422
        assert "Missing" in response.json()["detail"]

    async def test_valid_change_is_accepted_and_flags_loosening(self, client, db):
        response = await client.put(
            "/api/stage/gate",
            json={"pin": DEFAULT_PIN, "thresholds": {
                "min_paper_trading_days": 14, "min_trade_count": 20,
                "min_win_rate": 0.50, "max_drawdown_pct": 20,
            }},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["updated"]
        # Every one of these moved in the permissive direction.
        assert set(body["loosened"]) == {
            "min_paper_trading_days", "min_trade_count",
            "min_win_rate", "max_drawdown_pct",
        }

    async def test_tightening_is_not_flagged_as_loosening(self, client):
        response = await client.put(
            "/api/stage/gate",
            json={"pin": DEFAULT_PIN, "thresholds": {
                "min_paper_trading_days": 60, "min_trade_count": 80,
                "min_win_rate": 0.60, "max_drawdown_pct": 10,
            }},
        )
        assert response.json()["loosened"] == []

    async def test_the_bounded_minimum_still_requires_real_evidence(self, client, db):
        """Even at the loosest permitted gate, an empty record cannot pass."""
        await client.put(
            "/api/stage/gate",
            json={"pin": DEFAULT_PIN, "thresholds": {
                "min_paper_trading_days": 1, "min_trade_count": 1,
                "min_win_rate": 0.0, "max_drawdown_pct": 100,
            }},
        )

        result = await gate_service.evaluate_gate(db)
        assert not result.passed, "an empty record passed the loosest gate"


class TestThresholdValidation:
    def test_rejects_missing_keys(self):
        problems = gate_service.validate_thresholds({"min_win_rate": 0.5})
        assert any("Missing" in p for p in problems)

    def test_rejects_unknown_keys(self):
        problems = gate_service.validate_thresholds(
            {**STRICT_GATE, "min_luck": 1}
        )
        assert any("Unknown" in p for p in problems)

    def test_rejects_out_of_range(self):
        problems = gate_service.validate_thresholds(
            {**STRICT_GATE, "min_win_rate": 1.5}
        )
        assert any("min_win_rate" in p for p in problems)

    def test_rejects_zero_trade_count(self):
        problems = gate_service.validate_thresholds(
            {**STRICT_GATE, "min_trade_count": 0}
        )
        assert any("min_trade_count" in p for p in problems)

    def test_rejects_booleans_masquerading_as_numbers(self):
        problems = gate_service.validate_thresholds(
            {**STRICT_GATE, "min_trade_count": True}
        )
        assert any("must be a number" in p for p in problems)

    def test_accepts_the_shipped_default(self):
        assert gate_service.validate_thresholds(STRICT_GATE) == []
