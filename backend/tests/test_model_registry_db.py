"""Integration tests for promotion/rollback state transitions (§5.1).

These need a live Postgres with migrations applied. They are skipped rather
than failed when one is not reachable, so the pure-logic suite still runs in
environments without a database.

Set TEST_DATABASE_URL to point at a scratch database. Everything created
here is namespaced to a test-only symbol and cleaned up afterwards.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Model
from app.services.model_registry import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_CANDIDATE,
    ModelRegistryError,
    archive_model,
    get_active_model,
    promote_best_candidate,
    promote_model,
    score_candidates,
)

TEST_SYMBOL = "REGTESTUSDT"

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
async def db(tmp_path_factory):
    if not await _db_reachable():
        pytest.skip(f"No database reachable at {DB_URL}")

    engine = create_async_engine(DB_URL)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as session:
        await session.execute(delete(Model).where(Model.symbol == TEST_SYMBOL))
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(Model).where(Model.symbol == TEST_SYMBOL))
            await session.commit()

    await engine.dispose()


def _model_file(tmp_path, name="m.json") -> str:
    """A real file on disk — promotion refuses to activate a missing one."""
    path = tmp_path / name
    path.write_text("{}")
    return str(path)


async def _add_candidate(db, tmp_path, precision, auc, name) -> Model:
    model = Model(
        id=uuid.uuid4(),
        symbol=TEST_SYMBOL,
        model_type="xgboost_classifier",
        file_path=_model_file(tmp_path, name),
        trained_at=datetime.now(timezone.utc),
        metrics={
            "precision": precision,
            "positive_rate": 0.25,
            "predicted_positive_rate": 0.20,
            "roc_auc": auc,
            "accuracy": 0.6,
        },
        status=STATUS_CANDIDATE,
    )
    db.add(model)
    await db.flush()
    return model


@pytest.mark.asyncio
class TestPromotion:
    async def test_ranks_candidates_best_first(self, db, tmp_path):
        weak = await _add_candidate(db, tmp_path, 0.26, 0.51, "weak.json")
        strong = await _add_candidate(db, tmp_path, 0.45, 0.65, "strong.json")

        scored = await score_candidates(db, TEST_SYMBOL)

        assert [entry["model_id"] for entry in scored] == [
            str(strong.id), str(weak.id),
        ]

    async def test_promotes_best_candidate(self, db, tmp_path):
        await _add_candidate(db, tmp_path, 0.26, 0.51, "weak.json")
        strong = await _add_candidate(db, tmp_path, 0.45, 0.65, "strong.json")

        result = await promote_best_candidate(db, TEST_SYMBOL)

        assert result["promoted"]
        active = await get_active_model(db, TEST_SYMBOL)
        assert str(active.id) == str(strong.id)

    async def test_previous_active_is_archived_not_deleted(self, db, tmp_path):
        first = await _add_candidate(db, tmp_path, 0.40, 0.60, "first.json")
        await promote_model(db, first.id)

        second = await _add_candidate(db, tmp_path, 0.50, 0.70, "second.json")
        await promote_model(db, second.id)

        # The old model must still exist, for rollback (§5.1).
        refetched = await db.get(Model, first.id)
        assert refetched is not None
        assert refetched.status == STATUS_ARCHIVED
        assert (await get_active_model(db, TEST_SYMBOL)).id == second.id

    async def test_only_one_active_model_per_symbol(self, db, tmp_path):
        for i in range(3):
            model = await _add_candidate(db, tmp_path, 0.40 + i / 100, 0.60, f"m{i}.json")
            await promote_model(db, model.id)

        result = await db.execute(
            select(Model).where(
                Model.symbol == TEST_SYMBOL, Model.status == STATUS_ACTIVE
            )
        )
        assert len(result.scalars().all()) == 1

    async def test_refuses_candidate_no_better_than_active(self, db, tmp_path):
        strong = await _add_candidate(db, tmp_path, 0.50, 0.70, "strong.json")
        await promote_model(db, strong.id)
        await _add_candidate(db, tmp_path, 0.30, 0.55, "weak.json")

        result = await promote_best_candidate(db, TEST_SYMBOL)

        assert not result["promoted"]
        assert (await get_active_model(db, TEST_SYMBOL)).id == strong.id

    async def test_rollback_by_promoting_archived_model(self, db, tmp_path):
        """§5.1 requires promotion to be reversible."""
        first = await _add_candidate(db, tmp_path, 0.40, 0.60, "first.json")
        await promote_model(db, first.id)
        second = await _add_candidate(db, tmp_path, 0.50, 0.70, "second.json")
        await promote_model(db, second.id)

        await promote_model(db, first.id)

        assert (await get_active_model(db, TEST_SYMBOL)).id == first.id
        assert (await db.get(Model, second.id)).status == STATUS_ARCHIVED


@pytest.mark.asyncio
class TestPromotionGuards:
    async def test_missing_model_file_is_refused(self, db, tmp_path):
        model = await _add_candidate(db, tmp_path, 0.45, 0.65, "gone.json")
        os.remove(model.file_path)

        with pytest.raises(ModelRegistryError, match="missing"):
            await promote_model(db, model.id)

    async def test_force_does_not_bypass_missing_file(self, db, tmp_path):
        """Manual override may ignore the score, never a missing binary —
        the trading loop would fail at inference with a position open."""
        model = await _add_candidate(db, tmp_path, 0.45, 0.65, "gone.json")
        os.remove(model.file_path)

        with pytest.raises(ModelRegistryError, match="missing"):
            await promote_model(db, model.id, force=True)

    async def test_disqualified_model_refused_without_force(self, db, tmp_path):
        model = await _add_candidate(db, tmp_path, 0.0, 0.5, "dud.json")
        model.metrics = {**model.metrics, "predicted_positive_rate": 0.0}
        await db.flush()

        with pytest.raises(ModelRegistryError, match="disqualified"):
            await promote_model(db, model.id)

    async def test_force_promotes_disqualified_model(self, db, tmp_path):
        """§5.1 manual override."""
        model = await _add_candidate(db, tmp_path, 0.0, 0.5, "dud.json")
        model.metrics = {**model.metrics, "predicted_positive_rate": 0.0}
        await db.flush()

        result = await promote_model(db, model.id, force=True)

        assert result["forced"]
        assert (await get_active_model(db, TEST_SYMBOL)).id == model.id

    async def test_unknown_model_id_raises(self, db):
        with pytest.raises(ModelRegistryError, match="No model"):
            await promote_model(db, uuid.uuid4())

    async def test_promoting_active_model_is_a_noop(self, db, tmp_path):
        model = await _add_candidate(db, tmp_path, 0.45, 0.65, "m.json")
        await promote_model(db, model.id)

        result = await promote_model(db, model.id)

        assert not result["changed"]

    async def test_archive_leaves_symbol_without_active_model(self, db, tmp_path):
        model = await _add_candidate(db, tmp_path, 0.45, 0.65, "m.json")
        await promote_model(db, model.id)

        await archive_model(db, model.id)

        assert await get_active_model(db, TEST_SYMBOL) is None

    async def test_no_candidates_reports_cleanly(self, db):
        result = await promote_best_candidate(db, TEST_SYMBOL)
        assert not result["promoted"]
        assert result["candidates_considered"] == 0

    async def test_manual_promote_also_refuses_candidate_no_better_than_active(
        self, db, tmp_path
    ):
        """The Models page's Promote button must obey the same incumbent
        comparison as the automatic retrain promotion (§5.1) — a manual
        click on a specific candidate is not a bypass."""
        strong = await _add_candidate(db, tmp_path, 0.50, 0.70, "strong.json")
        await promote_model(db, strong.id)
        weak = await _add_candidate(db, tmp_path, 0.30, 0.55, "weak.json")

        with pytest.raises(ModelRegistryError, match="not"):
            await promote_model(db, weak.id)

        assert (await get_active_model(db, TEST_SYMBOL)).id == strong.id

    async def test_force_bypasses_incumbent_comparison(self, db, tmp_path):
        strong = await _add_candidate(db, tmp_path, 0.50, 0.70, "strong.json")
        await promote_model(db, strong.id)
        weak = await _add_candidate(db, tmp_path, 0.30, 0.55, "weak.json")

        result = await promote_model(db, weak.id, force=True)

        assert result["forced"]
        assert (await get_active_model(db, TEST_SYMBOL)).id == weak.id
