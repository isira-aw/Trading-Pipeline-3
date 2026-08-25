"""Shared test setup.

The app keeps a module-level async engine (`app.db.session.engine`), which
is correct in production where uvicorn runs a single event loop for the
process lifetime. pytest-asyncio gives each test its own loop, so pooled
connections opened under a previous loop fail with "attached to a different
loop" the moment a second test reuses them.

Disposing the engine after every test drops that pool, so each test starts
with connections belonging to its own loop.
"""

import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _dispose_app_engine():
    yield

    from app.db.session import engine

    await engine.dispose()
