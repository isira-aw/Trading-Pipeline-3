"""Integration tests for the `job_runs` audit trail (download/training).

These back the "state lost on navigation" fix: the dashboard must be able
to answer "what's the latest download/training run" straight from the
database, not only from a WebSocket message. Skipped when no database is
reachable, matching the other `_db` test modules.
"""

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import JobRun
from app.services import job_runs


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
    await session.execute(delete(JobRun).where(JobRun.job_type.in_(["download", "training"])))
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


class TestStartUpdateFinish:
    async def test_start_job_defaults_to_running(self, db):
        job = await job_runs.start_job(db, job_runs.JOB_DOWNLOAD, detail={"symbols": ["BTCUSDT"]})
        await db.commit()

        assert job.status == job_runs.STATUS_RUNNING
        assert job.progress == 0
        assert job.finished_at is None

    async def test_update_job_sets_progress_and_detail(self, db):
        job = await job_runs.start_job(db, job_runs.JOB_DOWNLOAD)
        await db.commit()

        await job_runs.update_job(db, job.id, progress=0.5, detail={"completed": 1})
        await db.commit()

        fetched = await db.get(JobRun, job.id)
        assert fetched.progress == 0.5
        assert fetched.detail == {"completed": 1}
        assert fetched.status == job_runs.STATUS_RUNNING

    async def test_finish_job_marks_terminal_state(self, db):
        job = await job_runs.start_job(db, job_runs.JOB_TRAINING)
        await db.commit()

        await job_runs.finish_job(db, job.id, status=job_runs.STATUS_SUCCESS, detail={"done": True})
        await db.commit()

        fetched = await db.get(JobRun, job.id)
        assert fetched.status == job_runs.STATUS_SUCCESS
        assert fetched.progress == 1.0
        assert fetched.finished_at is not None
        assert fetched.error is None

    async def test_finish_job_records_error(self, db):
        job = await job_runs.start_job(db, job_runs.JOB_DOWNLOAD)
        await db.commit()

        await job_runs.finish_job(db, job.id, status=job_runs.STATUS_FAILED, error="boom")
        await db.commit()

        fetched = await db.get(JobRun, job.id)
        assert fetched.status == job_runs.STATUS_FAILED
        assert fetched.error == "boom"

    async def test_update_and_finish_on_missing_job_is_a_noop(self, db):
        """A stale/unknown job_id must not raise — the caller already
        moved on and there is nothing sensible to update."""
        import uuid

        await job_runs.update_job(db, uuid.uuid4(), progress=0.5)
        await job_runs.finish_job(db, uuid.uuid4(), status=job_runs.STATUS_SUCCESS)
        await db.commit()  # must not raise


class TestLatestJob:
    async def test_latest_job_returns_none_when_no_runs_exist(self, db):
        assert await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD) is None

    async def test_latest_job_returns_the_most_recent_run(self, db):
        first = await job_runs.start_job(db, job_runs.JOB_DOWNLOAD)
        await db.commit()
        await job_runs.finish_job(db, first.id, status=job_runs.STATUS_SUCCESS)
        await db.commit()

        second = await job_runs.start_job(db, job_runs.JOB_DOWNLOAD)
        await db.commit()

        latest = await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD)
        assert latest.id == second.id
        assert latest.status == job_runs.STATUS_RUNNING

    async def test_latest_job_is_scoped_to_job_type(self, db):
        download = await job_runs.start_job(db, job_runs.JOB_DOWNLOAD)
        training = await job_runs.start_job(db, job_runs.JOB_TRAINING)
        await db.commit()

        assert (await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD)).id == download.id
        assert (await job_runs.latest_job(db, job_runs.JOB_TRAINING)).id == training.id


class TestToDict:
    def test_none_job_serializes_to_none(self):
        assert job_runs.to_dict(None) is None

    async def test_serializes_a_running_job(self, db):
        job = await job_runs.start_job(
            db, job_runs.JOB_DOWNLOAD, symbol="BTCUSDT", detail={"symbols": ["BTCUSDT"]},
        )
        await db.commit()

        data = job_runs.to_dict(job)
        assert data["job_id"] == str(job.id)
        assert data["job_type"] == job_runs.JOB_DOWNLOAD
        assert data["symbol"] == "BTCUSDT"
        assert data["status"] == job_runs.STATUS_RUNNING
        assert data["progress"] == 0.0
        assert data["detail"] == {"symbols": ["BTCUSDT"]}
        assert data["finished_at"] is None
