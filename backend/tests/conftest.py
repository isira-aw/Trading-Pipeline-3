"""Shared test setup and fixtures for the `_db` integration test modules.

Two unrelated concerns live here:

1. Event-loop hygiene: the app keeps a module-level async engine
   (`app.db.session.engine`), which is correct in production where uvicorn
   runs a single event loop for the process lifetime. pytest-asyncio gives
   each test its own loop, so pooled connections opened under a previous
   loop fail with "attached to a different loop" the moment a second test
   reuses them. Disposing the engine after every test drops that pool, so
   each test starts with connections belonging to its own loop.

2. Test/dev database isolation: these tests need a live Postgres with
   migrations applied. To make sure a test run can never touch the
   dev/paper-trading database (see the incident this fixture was added
   for: TEST_DATABASE_URL silently defaulting to the same connection
   string as DATABASE_URL), this module:

   a. Refuses to start the test session at all if TEST_DATABASE_URL is
      explicitly set to the same database as DATABASE_URL.
   b. When TEST_DATABASE_URL is *not* set, provisions a uniquely-named
      disposable database on the same Postgres server, runs `alembic
      upgrade head` against it, and drops it when the session ends — so a
      human never has to remember to configure two different .env values
      correctly.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture(autouse=True)
async def _dispose_app_engine():
    yield

    from app.db.session import engine

    await engine.dispose()


def _to_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _to_plain_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _normalize(url: str) -> str:
    """host:port/dbname, ignoring driver prefix and query string."""
    u = url.replace("postgresql+asyncpg://", "").replace("postgresql://", "")
    return u.split("?")[0].rstrip("/")


DEV_DATABASE_URL = _to_asyncpg_url(settings.DATABASE_URL)
_explicit_test_url = os.environ.get("TEST_DATABASE_URL")

if _explicit_test_url and _normalize(_explicit_test_url) == _normalize(DEV_DATABASE_URL):
    raise pytest.UsageError(
        "TEST_DATABASE_URL is identical to DATABASE_URL "
        f"({_normalize(DEV_DATABASE_URL)!r}). Refusing to run the test suite "
        "against the dev/paper-trading database — this project's entire "
        "premise is 'paper before real', so tests must never share a "
        "database with dev data. Unset TEST_DATABASE_URL to let the test "
        "suite provision its own disposable database automatically, or "
        "point it at a genuinely separate one."
    )


def _admin_dsn(url: str) -> str:
    """DSN for the `postgres` maintenance database on the same server."""
    plain = _to_plain_url(url)
    base = plain.rsplit("/", 1)[0]
    return base + "/postgres"


@pytest.fixture(scope="session")
def test_database_url():
    """Yield a DB connection string that is guaranteed distinct from DATABASE_URL.

    Without TEST_DATABASE_URL set, creates `trading_pipeline_test_<random>`,
    migrates it, and drops it afterwards. With TEST_DATABASE_URL set (and
    already validated above to differ from DATABASE_URL), just uses it as-is
    and leaves it alone — the caller owns its lifecycle.
    """
    if _explicit_test_url:
        yield _to_asyncpg_url(_explicit_test_url)
        return

    db_name = f"trading_pipeline_test_{uuid.uuid4().hex[:10]}"
    admin_dsn = _to_plain_url(_admin_dsn(DEV_DATABASE_URL))

    async def _create():
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await conn.close()

    async def _drop():
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await conn.close()

    try:
        asyncio.run(_create())
    except Exception as exc:
        pytest.skip(f"Could not provision disposable test database: {exc}")
        return

    base = DEV_DATABASE_URL.rsplit("/", 1)[0]
    new_url = f"{base}/{db_name}"
    sync_url = _to_plain_url(new_url)

    env = os.environ.copy()
    env["DATABASE_URL"] = sync_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        asyncio.run(_drop())
        pytest.fail(
            f"alembic upgrade head failed against disposable test database "
            f"{db_name!r}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    try:
        yield new_url
    finally:
        asyncio.run(_drop())
