"""Emergency stop, resume and liquidation (§7).

These are the two buttons that exist for a bad day, so the tests focus on
the ways a halt could be *partially* defeated — a side door that re-enables
trading, a stage switch that walks around Resume, an order that slips
through while the loop is paused.
"""

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Config, Trade, WalletSnapshot
from app.services import halt_service
from app.services import trading_engine as te
from app.services.security import DEFAULT_PIN, hash_pin

PAPER = "paper"
SYMBOL = "HALTUSDT"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

async def _db_reachable(db_url) -> bool:
    engine = create_async_engine(db_url)
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
async def db(test_database_url):
    if not await _db_reachable(test_database_url):
        pytest.skip(f"No database reachable at {test_database_url}")

    engine = create_async_engine(test_database_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as session:
        await session.execute(delete(Trade).where(Trade.stage == PAPER))
        await _set(session, "current_stage", PAPER)
        await _set(session, "halted", False)
        await _set(session, "halted_at", "")
        await _set(session, "halted_reason", "")
        await _set(session, "trading_enabled", False)
        await _set(session, "stage_pin_hash", hash_pin(DEFAULT_PIN))
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(Trade).where(Trade.stage == PAPER))
            await _set(session, "current_stage", "setup")
            await _set(session, "halted", False)
            await _set(session, "halted_at", "")
            await _set(session, "trading_enabled", False)

    await engine.dispose()


@pytest_asyncio.fixture
async def client(db, test_database_url):
    from app.main import app
    from app.db.session import get_db

    # `db` writes to the disposable test DB; override the app's dependency
    # so HTTP requests through this client read/write the same database
    # instead of falling through to the dev-bound production engine.
    engine = create_async_engine(test_database_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


def _no_exchange():
    """The exchange is unreachable — the realistic case when halting."""
    class Down:
        class client:
            @staticmethod
            def get_open_orders():
                raise RuntimeError("exchange unreachable")
    return patch.object(halt_service, "get_trading_client", lambda stage: Down())


class TestEmergencyStop:
    async def test_halt_is_a_flag_not_a_stage_value(self, db, client):
        """§7: halted OVERRIDES the stage. Writing it into current_stage
        would erase which stage was running, so Resume would have to guess
        between paper and live."""
        with _no_exchange():
            await client.post("/api/system/emergency-stop")

        stage = (await db.execute(
            select(Config.value).where(Config.key == "current_stage")
        )).scalar_one()
        halted = (await db.execute(
            select(Config.value).where(Config.key == "halted")
        )).scalar_one()

        assert stage == PAPER, "the stage was overwritten and is now unrecoverable"
        assert halted is True

    async def test_halt_records_a_timestamp(self, db, client):
        with _no_exchange():
            body = (await client.post("/api/system/emergency-stop")).json()

        assert body["halted"] is True
        assert body["halted_at"]
        assert body["stage_when_halted"] == PAPER

    async def test_halt_does_not_liquidate(self, db, client):
        """§7 step 3: freeze only."""
        with _no_exchange():
            body = (await client.post("/api/system/emergency-stop")).json()

        assert body["holdings_liquidated"] is False
        trades = (await db.execute(select(Trade).where(Trade.stage == PAPER))).scalars().all()
        assert trades == []

    async def test_halt_disables_trading(self, db, client):
        await _set(db, "trading_enabled", True)
        with _no_exchange():
            await client.post("/api/system/emergency-stop")

        enabled = (await db.execute(
            select(Config.value).where(Config.key == "trading_enabled")
        )).scalar_one()
        assert enabled is False

    async def test_halt_succeeds_even_when_the_exchange_is_unreachable(self, db, client):
        """The exchange being down is a likely reason for hitting the button.
        Stopping this system must not depend on reaching Binance."""
        with _no_exchange():
            body = (await client.post("/api/system/emergency-stop")).json()

        assert body["halted"] is True
        assert body["exchange_reachable"] is False
        assert "unreachable" in body["cancel_error"]

    async def test_halt_cancels_open_orders(self, db, client):
        cancelled = []

        class Exchange:
            class client:
                @staticmethod
                def get_open_orders():
                    return [
                        {"symbol": "BTCUSDT", "orderId": 11},
                        {"symbol": "ETHUSDT", "orderId": 22},
                    ]

                @staticmethod
                def cancel_order(symbol, orderId):
                    cancelled.append((symbol, orderId))

        with patch.object(halt_service, "get_trading_client", lambda s: Exchange()):
            body = (await client.post("/api/system/emergency-stop")).json()

        assert cancelled == [("BTCUSDT", 11), ("ETHUSDT", 22)]
        assert len(body["orders_cancelled"]) == 2

    async def test_one_failed_cancel_does_not_stop_the_others(self, db, client):
        class Exchange:
            class client:
                @staticmethod
                def get_open_orders():
                    return [
                        {"symbol": "BTCUSDT", "orderId": 11},
                        {"symbol": "ETHUSDT", "orderId": 22},
                    ]

                @staticmethod
                def cancel_order(symbol, orderId):
                    if orderId == 11:
                        raise RuntimeError("already filled")

        with patch.object(halt_service, "get_trading_client", lambda s: Exchange()):
            body = (await client.post("/api/system/emergency-stop")).json()

        assert len(body["cancel_failures"]) == 1
        assert len(body["orders_cancelled"]) == 1

    async def test_emergency_stop_needs_no_pin(self, db, client):
        """Deliberate: a credential in front of *stopping* is the wrong
        trade-off. The PIN guards resuming, where an accidental press is
        the actual risk."""
        with _no_exchange():
            assert (await client.post("/api/system/emergency-stop")).status_code == 200


class TestResume:
    async def _halt(self, client):
        with _no_exchange():
            await client.post("/api/system/emergency-stop")

    async def test_resume_requires_the_pin(self, db, client):
        await self._halt(client)

        response = await client.post("/api/system/resume", json={"pin": "999999"})
        assert response.status_code == 403

        halted = (await db.execute(
            select(Config.value).where(Config.key == "halted")
        )).scalar_one()
        assert halted is True, "the halt cleared despite a wrong PIN"

    async def test_resume_with_the_correct_pin_clears_the_halt(self, db, client):
        await self._halt(client)

        response = await client.post("/api/system/resume", json={"pin": DEFAULT_PIN})
        assert response.status_code == 200
        assert response.json()["halted"] is False

    async def test_resume_returns_to_the_original_stage(self, db, client):
        """The reason halted is a flag: the stage survives the halt."""
        await self._halt(client)
        body = (await client.post("/api/system/resume", json={"pin": DEFAULT_PIN})).json()
        assert body["stage"] == PAPER

    async def test_resume_does_not_restart_trading(self, db, client):
        """Coming back should be two deliberate acts, not one button that
        starts placing orders again."""
        await self._halt(client)
        body = (await client.post("/api/system/resume", json={"pin": DEFAULT_PIN})).json()
        assert body["trading_enabled"] is False


class TestHaltSideDoors:
    """Ways a halt could be defeated without going through Resume."""

    async def _halt(self, client):
        with _no_exchange():
            await client.post("/api/system/emergency-stop")

    async def test_config_put_cannot_clear_the_halt(self, db, client):
        """The obvious side door: writing halted=false directly."""
        await self._halt(client)

        response = await client.put("/api/config/halted", json={"value": False})
        assert response.status_code == 403

        halted = (await db.execute(
            select(Config.value).where(Config.key == "halted")
        )).scalar_one()
        assert halted is True

    async def test_config_put_cannot_rewrite_the_halt_timestamp(self, client):
        await self._halt(client)
        assert (await client.put(
            "/api/config/halted_at", json={"value": ""}
        )).status_code == 403

    async def test_start_endpoint_refuses_while_halted(self, db, client):
        """Without this, Start would re-enable trading and only the loop's
        own guard would stand in the way."""
        await self._halt(client)

        response = await client.post("/api/system/start")
        assert response.status_code == 409

        enabled = (await db.execute(
            select(Config.value).where(Config.key == "trading_enabled")
        )).scalar_one()
        assert enabled is False

    async def test_stage_switch_refuses_while_halted(self, client):
        """A stage switch would otherwise be a second way out of a halt that
        skips Resume and its audit trail."""
        await self._halt(client)

        response = await client.post(
            "/api/stage/switch", json={"stage": "setup", "pin": DEFAULT_PIN}
        )
        assert response.status_code == 409
        assert "Emergency stop is active" in response.json()["detail"]

    async def test_trading_allowed_is_false_while_halted(self, db, client):
        from app.services.scheduler import trading_allowed

        await self._halt(client)
        await _set(db, "trading_enabled", True)  # even if this were forced on

        allowed, reason = await trading_allowed(db)
        assert not allowed
        assert "halted" in reason.lower()

    async def test_order_path_itself_refuses_entries_while_halted(self, db, client):
        """Defence in depth: the paused job is not the only thing stopping
        an order — a manual trigger must be refused too."""
        await self._halt(client)

        with pytest.raises(te.StageNotPermitted, match="halted"):
            await te.assert_stage_permitted(db, PAPER, "buy")

    async def test_exits_remain_permitted_while_halted(self, db, client):
        """Liquidation runs while halted, and blocking sells would strand
        capital in positions the system can no longer close."""
        await self._halt(client)
        await te.assert_stage_permitted(db, PAPER, "sell")


class TestLiquidate:
    def _account(self, balances):
        async def state(db, stage):
            total = balances.get("USDT", 0.0) + sum(
                v * 100 for k, v in balances.items() if k != "USDT"
            )
            return te.AccountState(
                balances=balances, total_value_usdt=total, exposure_usdt=total,
            )
        return state

    async def test_requires_the_pin(self, client):
        response = await client.post(
            "/api/system/liquidate", json={"pin": "999999", "confirm": True}
        )
        assert response.status_code == 403

    async def test_requires_explicit_confirmation(self, client):
        """§7 step 6: a second confirmation, not just one click."""
        response = await client.post(
            "/api/system/liquidate", json={"pin": DEFAULT_PIN, "confirm": False}
        )
        assert response.status_code == 428
        assert "confirmation" in response.json()["detail"]

    async def test_correct_pin_alone_is_not_enough(self, client):
        """The PIN and the confirmation are separate gates."""
        response = await client.post(
            "/api/system/liquidate", json={"pin": DEFAULT_PIN}
        )
        assert response.status_code == 428

    async def test_sells_go_through_the_normal_order_path(self, db, client):
        """Not a bypass: each sale is risk-assessed and recorded in trades
        exactly like any other order."""
        calls = []

        async def fake_place(db_, proposal, stage, now=None, **kwargs):
            calls.append((proposal.symbol, proposal.side, proposal.quantity))
            return te.OrderOutcome(
                placed=True, decision="approved", trade_id=str(uuid.uuid4()),
                status="filled", filled_quantity=proposal.quantity,
                fill_price=proposal.price,
            )

        with patch.object(te, "get_account_state", self._account({"USDT": 500.0, "BTC": 0.05})), \
             patch.object(te, "place_order", fake_place), \
             patch("app.services.binance_client.BinanceAPIClient.get_symbol_price",
                   lambda self, symbol: 50000.0):
            body = (await client.post(
                "/api/system/liquidate", json={"pin": DEFAULT_PIN, "confirm": True}
            )).json()

        assert calls == [("BTCUSDT", "sell", 0.05)]
        assert body["placed_count"] == 1

    async def test_quote_asset_is_not_sold(self, db, client):
        """Selling USDT for USDT is not a thing."""
        calls = []

        async def fake_place(db_, proposal, stage, now=None, **kwargs):
            calls.append(proposal.symbol)
            return te.OrderOutcome(placed=True, decision="approved", status="filled")

        with patch.object(te, "get_account_state", self._account({"USDT": 1000.0})), \
             patch.object(te, "place_order", fake_place):
            body = (await client.post(
                "/api/system/liquidate", json={"pin": DEFAULT_PIN, "confirm": True}
            )).json()

        assert calls == []
        assert body["attempted_count"] == 0

    async def test_unpriceable_asset_is_reported_not_silently_skipped(self, db, client):
        def boom(self, symbol):
            raise RuntimeError("no such pair")

        with patch.object(te, "get_account_state", self._account({"WEIRD": 5.0})), \
             patch("app.services.binance_client.BinanceAPIClient.get_symbol_price", boom):
            body = (await client.post(
                "/api/system/liquidate", json={"pin": DEFAULT_PIN, "confirm": True}
            )).json()

        assert body["placed_count"] == 0
        assert body["sales"][0]["placed"] is False
        assert "No price available" in body["sales"][0]["reason"]

    async def test_one_failure_does_not_abandon_the_rest(self, db, client):
        async def fake_place(db_, proposal, stage, now=None, **kwargs):
            if proposal.symbol == "BTCUSDT":
                raise RuntimeError("exchange rejected")
            return te.OrderOutcome(placed=True, decision="approved", status="filled")

        with patch.object(te, "get_account_state",
                          self._account({"BTC": 0.05, "ETH": 1.0})), \
             patch.object(te, "place_order", fake_place), \
             patch("app.services.binance_client.BinanceAPIClient.get_symbol_price",
                   lambda self, symbol: 100.0):
            body = (await client.post(
                "/api/system/liquidate", json={"pin": DEFAULT_PIN, "confirm": True}
            )).json()

        assert body["attempted_count"] == 2
        assert body["placed_count"] == 1

    async def test_liquidation_is_permitted_while_halted(self, db, client):
        """The whole point of the separate button: the halt freezes new
        risk, and this is the decision to exit instead."""
        with _no_exchange():
            await client.post("/api/system/emergency-stop")

        async def fake_place(db_, proposal, stage, now=None, **kwargs):
            return te.OrderOutcome(
                placed=True, decision="approved", status="filled",
                filled_quantity=proposal.quantity,
            )

        with patch.object(te, "get_account_state", self._account({"BTC": 0.05})), \
             patch.object(te, "place_order", fake_place), \
             patch("app.services.binance_client.BinanceAPIClient.get_symbol_price",
                   lambda self, symbol: 100.0):
            body = (await client.post(
                "/api/system/liquidate", json={"pin": DEFAULT_PIN, "confirm": True}
            )).json()

        assert body["placed_count"] == 1
