"""Integration tests for the job_runs-backed download flow.

Supersedes the old in-memory `_progress`/`_publish` unit tests (removed
along with that mechanism from `data_downloader.py`): progress is now a
`job_runs` row, so this exercises the same "does progress update, does it
land on the bus, does it reach a terminal status" behavior against the
database. Skipped when no database is reachable, matching the other `_db`
test modules.
"""

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Candle, JobRun
from app.services import data_downloader as dd
from app.services import job_runs
from app.services.event_bus import EVENT_DATA_DOWNLOAD, bus

SYMBOLS = ["TDAUSDT", "TDBUSDT"]
INTERVAL = "4h"

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
    await session.execute(delete(Candle).where(Candle.symbol.in_(SYMBOLS)))
    await session.execute(delete(JobRun).where(JobRun.job_type == job_runs.JOB_DOWNLOAD))
    await session.commit()


@pytest_asyncio.fixture
async def db(test_database_url):
    if not await _db_reachable(test_database_url):
        pytest.skip(f"No database reachable at {test_database_url}")

    engine = create_async_engine(test_database_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # download_historical_data opens its own session via the module-level
    # AsyncSessionLocal (bound to dev DATABASE_URL), independent of the `db`
    # session below — patch it so the function under test writes to the same
    # disposable database this fixture asserts against.
    with patch.object(dd, "AsyncSessionLocal", maker):
        async with maker() as session:
            await _cleanup(session)
            try:
                yield session
            finally:
                await session.rollback()
                await _cleanup(session)


def fake_klines(count=3):
    """Minimal synthetic klines — enough fields for `_to_candle_rows`."""
    return [
        [1700000000000 + i * 3600_000, "100", "101", "99", "100.5", "10"]
        for i in range(count)
    ]


class TestDownloadHistoricalDataJobRuns:
    async def test_creates_a_running_job_before_any_symbol_completes(self, db):
        """Regression guard for the button-disable fix: a status poll
        right after the download starts must see a running job, not None."""
        with patch.object(dd, "_fetch_klines", side_effect=lambda *a, **k: fake_klines()):
            result = await dd.download_historical_data(
                symbols=SYMBOLS, interval=INTERVAL, history_years=1,
            )

        job = await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD)
        assert str(job.id) == result["job_id"]
        assert job.status == job_runs.STATUS_SUCCESS
        assert job.progress == pytest.approx(1.0)

    async def test_progress_advances_per_symbol_and_reaches_terminal_success(self, db):
        with patch.object(dd, "_fetch_klines", side_effect=lambda *a, **k: fake_klines()):
            await dd.download_historical_data(
                symbols=SYMBOLS, interval=INTERVAL, history_years=1,
            )

        job = await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD)
        assert job.status == job_runs.STATUS_SUCCESS
        assert job.detail["total"] == len(SYMBOLS)
        assert [c["symbol"] for c in job.detail["completed"]] == SYMBOLS

    async def test_one_symbol_failing_marks_the_job_failed_but_finishes_the_rest(self, db):
        calls = {"n": 0}

        async def flaky(symbol, interval, start_str):
            calls["n"] += 1
            if symbol == SYMBOLS[0]:
                raise RuntimeError("exchange unreachable")
            return fake_klines()

        with patch.object(dd, "_fetch_klines", side_effect=flaky):
            result = await dd.download_historical_data(
                symbols=SYMBOLS, interval=INTERVAL, history_years=1,
            )

        assert calls["n"] == len(SYMBOLS)  # the second symbol still ran
        job = await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD)
        assert job.status == job_runs.STATUS_FAILED
        assert result["results"][0]["error"] is True
        assert result["results"][1]["symbol"] == SYMBOLS[1]

    async def test_progress_is_published_on_the_bus(self, db):
        queue = bus.subscribe()
        try:
            with patch.object(dd, "_fetch_klines", side_effect=lambda *a, **k: fake_klines()):
                await dd.download_historical_data(
                    symbols=SYMBOLS, interval=INTERVAL, history_years=1,
                )

            messages = []
            while not queue.empty():
                messages.append(queue.get_nowait())
        finally:
            bus.unsubscribe(queue)

        assert all(m["event"] == EVENT_DATA_DOWNLOAD for m in messages)
        assert messages[-1]["status"] == job_runs.STATUS_SUCCESS
        assert messages[-1]["progress"] == pytest.approx(1.0)

    async def test_a_preexisting_job_id_is_used_instead_of_creating_a_new_one(self, db):
        """The API route pre-creates the job row so a poll right after the
        POST never races the background task for it."""
        job = await job_runs.start_job(
            db, job_runs.JOB_DOWNLOAD, detail={"symbols": SYMBOLS, "total": 2, "completed": []},
        )
        await db.commit()

        with patch.object(dd, "_fetch_klines", side_effect=lambda *a, **k: fake_klines()):
            result = await dd.download_historical_data(
                symbols=SYMBOLS, interval=INTERVAL, history_years=1, job_id=job.id,
            )

        assert result["job_id"] == str(job.id)
        only_job = await job_runs.latest_job(db, job_runs.JOB_DOWNLOAD)
        assert only_job.id == job.id
