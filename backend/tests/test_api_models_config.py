"""API tests for the Models and Settings pages (§8.2, §8.3, §10).

The security-relevant cases here are the PIN gate and the refusal that stops
the UI promoting a worse model than the one already active — both must hold
server-side, since a frontend check is a convenience, not a control.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Config, Model
from app.services.security import DEFAULT_PIN, hash_pin, verify_pin

SYMBOL = "APITESTUSDT"
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


@pytest_asyncio.fixture
async def client():
    if not await _db_reachable():
        pytest.skip(f"No database reachable at {DB_URL}")

    from app.main import app

    # The lifespan starts the scheduler; not wanted in tests.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(DB_URL)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.execute(delete(Model).where(Model.symbol == SYMBOL))
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(Model).where(Model.symbol == SYMBOL))
            await session.commit()
    await engine.dispose()


async def _model(db, tmp_path, status, precision, auc, name):
    path = tmp_path / name
    path.write_text("{}")
    model = Model(
        id=uuid.uuid4(), symbol=SYMBOL, model_type="xgboost_classifier",
        file_path=str(path), trained_at=datetime.now(timezone.utc),
        metrics={
            "precision": precision, "positive_rate": 0.25,
            "predicted_positive_rate": 0.20, "roc_auc": auc, "accuracy": 0.6,
        },
        status=status,
    )
    db.add(model)
    await db.commit()
    return model


class TestModelsEndpoint:
    async def test_returns_full_scoring_breakdown_not_just_accuracy(
        self, client, db, tmp_path
    ):
        """§8.2: the page needs each scoring component. Accuracy alone can
        look fine on a model with no edge at all."""
        await _model(db, tmp_path, "candidate", 0.45, 0.65, "m.json")

        response = await client.get("/api/models")
        assert response.status_code == 200

        body = response.json()
        mine = [m for m in body["models"] if m["symbol"] == SYMBOL]
        assert mine, "model missing from listing"

        breakdown = mine[0]["score_breakdown"]
        assert "precision_lift" in breakdown
        assert "discrimination" in breakdown
        assert "realized_win_rate" in breakdown
        assert body["scoring_weights"]["precision_lift"] > 0

    async def test_reports_realized_stats_and_file_presence(
        self, client, db, tmp_path
    ):
        await _model(db, tmp_path, "candidate", 0.45, 0.65, "m.json")
        body = (await client.get("/api/models")).json()
        row = [m for m in body["models"] if m["symbol"] == SYMBOL][0]

        assert row["realized"]["win_rate"] is None  # nothing closed yet
        assert row["file_missing"] is False
        assert row["file_size_bytes"] > 0

    async def test_missing_model_file_is_flagged(self, client, db, tmp_path):
        model = await _model(db, tmp_path, "candidate", 0.45, 0.65, "gone.json")
        os.remove(model.file_path)

        body = (await client.get("/api/models")).json()
        row = [m for m in body["models"] if m["symbol"] == SYMBOL][0]
        assert row["file_missing"] is True


class TestPromotionThroughApi:
    async def test_promote_uses_real_registry_logic(self, client, db, tmp_path):
        model = await _model(db, tmp_path, "candidate", 0.45, 0.65, "m.json")

        response = await client.post(f"/api/models/{model.id}/promote")
        assert response.status_code == 200

        await db.refresh(model)
        assert model.status == "active"

    async def test_promote_best_refuses_a_worse_candidate(
        self, client, db, tmp_path
    ):
        """The registry rule the UI must not be able to shortcut."""
        strong = await _model(db, tmp_path, "candidate", 0.50, 0.70, "s.json")
        await client.post(f"/api/models/{strong.id}/promote")
        await _model(db, tmp_path, "candidate", 0.30, 0.55, "w.json")

        response = await client.post(f"/api/models/{SYMBOL}/promote-best")
        body = response.json()

        assert body["promoted"] is False
        assert "not better" in body["reason"]

        await db.refresh(strong)
        assert strong.status == "active"

    async def test_promote_refuses_disqualified_model(self, client, db, tmp_path):
        model = await _model(db, tmp_path, "candidate", 0.0, 0.5, "dud.json")
        model.metrics = {**model.metrics, "predicted_positive_rate": 0.0}
        await db.commit()

        response = await client.post(f"/api/models/{model.id}/promote")
        assert response.status_code == 409
        assert "disqualified" in response.json()["detail"]

    async def test_force_promotes_a_disqualified_model(self, client, db, tmp_path):
        model = await _model(db, tmp_path, "candidate", 0.0, 0.5, "dud.json")
        model.metrics = {**model.metrics, "predicted_positive_rate": 0.0}
        await db.commit()

        response = await client.post(f"/api/models/{model.id}/promote?force=true")
        assert response.status_code == 200
        assert response.json()["forced"] is True

    async def test_promote_refuses_missing_file_even_with_force(
        self, client, db, tmp_path
    ):
        model = await _model(db, tmp_path, "candidate", 0.45, 0.65, "gone.json")
        os.remove(model.file_path)

        response = await client.post(f"/api/models/{model.id}/promote?force=true")
        assert response.status_code == 409
        assert "missing" in response.json()["detail"]

    async def test_archive_through_api(self, client, db, tmp_path):
        model = await _model(db, tmp_path, "candidate", 0.45, 0.65, "m.json")
        assert (await client.post(f"/api/models/{model.id}/archive")).status_code == 200
        await db.refresh(model)
        assert model.status == "archived"


class TestConfigEndpoint:
    async def test_lists_config_without_the_pin_hash(self, client):
        """The hash must never leave the server."""
        body = (await client.get("/api/config")).json()
        assert "stage_pin_hash" not in body["config"]
        assert "atr_period" in body["config"]
        assert "atr_stop_multiplier" in body["config"]

    async def test_updates_a_value(self, client, db):
        original = 14
        assert (await client.put(
            "/api/config/atr_period", json={"value": 21}
        )).status_code == 200

        value = (await db.execute(
            select(Config.value).where(Config.key == "atr_period")
        )).scalar_one()
        assert value == 21

        await client.put("/api/config/atr_period", json={"value": original})

    async def test_float_config_accepts_an_int(self, client):
        response = await client.put(
            "/api/config/atr_stop_multiplier", json={"value": 3}
        )
        assert response.status_code == 200
        assert response.json()["value"] == 3.0
        await client.put("/api/config/atr_stop_multiplier", json={"value": 2.0})

    async def test_rejects_wrong_type(self, client):
        """A string where a number belongs would fail later inside a
        scheduled job, far from its cause."""
        response = await client.put(
            "/api/config/atr_period", json={"value": "not a number"}
        )
        assert response.status_code == 422

    async def test_rejects_unknown_key(self, client):
        response = await client.put("/api/config/nonsense", json={"value": 1})
        assert response.status_code == 404

    async def test_pin_hash_cannot_be_written_through_config(self, client):
        response = await client.put(
            "/api/config/stage_pin_hash", json={"value": "anything"}
        )
        assert response.status_code == 403

    async def test_current_stage_cannot_be_written_through_config(self, client):
        """Otherwise the promotion gate could be sidestepped entirely by
        setting the key directly."""
        response = await client.put(
            "/api/config/current_stage", json={"value": "live"}
        )
        assert response.status_code == 403


class TestPinChange:
    @pytest_asyncio.fixture(autouse=True)
    async def restore_pin(self, db):
        yield
        await db.execute(
            delete(Config).where(Config.key == "stage_pin_hash")
        )
        db.add(Config(key="stage_pin_hash", value=hash_pin(DEFAULT_PIN)))
        await db.commit()

    async def test_requires_the_current_pin(self, client):
        """Without this, anyone reaching the dashboard could set their own
        PIN and unlock the control the PIN protects."""
        response = await client.post("/api/config/pin", json={
            "current_pin": "999999", "new_pin": "123456",
        })
        assert response.status_code == 403
        assert "incorrect" in response.json()["detail"].lower()

    async def test_changes_with_the_correct_current_pin(self, client, db):
        response = await client.post("/api/config/pin", json={
            "current_pin": DEFAULT_PIN, "new_pin": "246810",
        })
        assert response.status_code == 200

        stored = (await db.execute(
            select(Config.value).where(Config.key == "stage_pin_hash")
        )).scalar_one()
        assert verify_pin("246810", stored)
        assert not verify_pin(DEFAULT_PIN, stored)

    async def test_wrong_pin_leaves_the_stored_hash_untouched(self, client, db):
        before = (await db.execute(
            select(Config.value).where(Config.key == "stage_pin_hash")
        )).scalar_one()

        await client.post("/api/config/pin", json={
            "current_pin": "000001", "new_pin": "555555",
        })

        after = (await db.execute(
            select(Config.value).where(Config.key == "stage_pin_hash")
        )).scalar_one()
        assert before == after

    async def test_rejects_reusing_the_same_pin(self, client):
        response = await client.post("/api/config/pin", json={
            "current_pin": DEFAULT_PIN, "new_pin": DEFAULT_PIN,
        })
        assert response.status_code == 422

    async def test_rejects_too_short_a_pin(self, client):
        response = await client.post("/api/config/pin", json={
            "current_pin": DEFAULT_PIN, "new_pin": "12",
        })
        assert response.status_code == 422

    async def test_reports_when_the_pin_is_still_default(self, client):
        assert (await client.get("/api/config")).json()["pin_is_default"] is True


class TestLlmConfigProtectionReview:
    """The LLM keys were reviewed against the PROTECTED_KEYS bar (step 10).

    None qualified: they are context-only settings with no path to order
    placement, so ordinary editing is right. What they need is validation,
    which these tests pin down.
    """

    async def test_llm_keys_are_editable_not_protected(self, client):
        for key, value in [
            ("llm_calls_per_day", 3),
            ("llm_provider", "gemini"),
            ("llm_confidence_adjustment_enabled", True),
            ("llm_uncertainty_confidence_bonus", 0.1),
        ]:
            response = await client.put(f"/api/config/{key}", json={"value": value})
            assert response.status_code == 200, f"{key}: {response.json()}"

        # Restore.
        for key, value in [
            ("llm_calls_per_day", 2),
            ("llm_provider", "ollama"),
            ("llm_confidence_adjustment_enabled", False),
            ("llm_uncertainty_confidence_bonus", 0.05),
        ]:
            await client.put(f"/api/config/{key}", json={"value": value})

    async def test_negative_confidence_bonus_is_refused(self, client):
        """The one genuine hazard among the LLM keys: a negative bonus would
        LOWER the risk engine's floor from an LLM's opinion. Bounded at zero
        rather than protecting the key."""
        response = await client.put(
            "/api/config/llm_uncertainty_confidence_bonus", json={"value": -0.2}
        )
        assert response.status_code == 422
        assert "at least 0" in response.json()["detail"]

    async def test_unknown_provider_is_refused(self, client):
        response = await client.put(
            "/api/config/llm_provider", json={"value": "gpt-9"}
        )
        assert response.status_code == 422
        assert "ollama" in response.json()["detail"]

    async def test_calls_per_day_is_bounded(self, client):
        assert (await client.put(
            "/api/config/llm_calls_per_day", json={"value": 999}
        )).status_code == 422

    async def test_risk_thresholds_are_bounded_too(self, client):
        """Editable by design (§8.3), but a slipped decimal must not silently
        disable a limit."""
        assert (await client.put(
            "/api/config/min_confidence", json={"value": 5.0}
        )).status_code == 422
        assert (await client.put(
            "/api/config/max_position_pct", json={"value": -10}
        )).status_code == 422
