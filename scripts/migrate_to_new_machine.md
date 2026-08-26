# Migrating to a new machine

Core Principle #3: code lives in Git, data lives in PostgreSQL, and a
restore on a fresh machine should be one command. This document is the
exact procedure, including the parts a `pg_dump` **does not** carry.

## What the database dump does and does not contain

Verified by actually doing it — dumping the dev database, restoring into a
separate Postgres instance on another port, and comparing.

| Thing | In the dump? | How it gets to the new machine |
|---|---|---|
| `candles`, `trades`, `risk_log`, `wallet_snapshots`, `llm_advisories`, `models` (rows), `config`, `component_status` | **Yes** | `restore_db.sh` |
| Alembic version (`alembic_version`) | **Yes** | `restore_db.sh` — the restored DB is already at the right migration |
| Stage PIN hash | **Yes** (inside `config`) | `restore_db.sh`. Your existing PIN keeps working. |
| **`backend/app/models_store/*.json`** — the trained models themselves | **NO** | **Copy manually. See below.** |
| **`.env`** — API keys, DB password | **NO** (deliberately) | Recreate from `.env.example` |
| Application code | No | `git clone` |

### Why models_store matters

The `models` table stores a **`file_path`, not the model**. Restore the
database without the files and every model row points at something that
does not exist. Confirmed by simulating it:

```
ModelRegistryError: Model file not found:
  /…/backend/app/models_store/BTCUSDT_xgboost_classifier_f19c3f4d….json
```

The trade loop would then find no loadable active model and skip every
symbol — visible in the logs, but the system would simply never trade.

**A second, subtler problem:** `file_path` is stored **absolute**. If the
project lives at a different path on the new machine, copying the files is
still not enough on its own. `model_registry.resolve_model_path` handles
this by also looking for the filename in the new installation's own
`models_store/`, so a moved install works — but the files must be there.

---

## Procedure

### 1. On the OLD machine — back up

```bash
cd /path/to/trading-pipeline
export DATABASE_URL='postgresql://postgres:postgres@localhost:5432/trading_pipeline'

./scripts/backup_db.sh ./backups
```

Then take the model files, which the dump does not include:

```bash
tar -czf ./backups/models_store.tar.gz -C backend/app models_store
```

Copy **both** to the new machine (dump + tarball). Also note your `.env`
values — they are not in either.

### 2. On the NEW machine — code and dependencies

```bash
git clone <your repo url> trading-pipeline
cd trading-pipeline

cp .env.example .env
# Fill in BINANCE_API_KEY / BINANCE_API_SECRET (withdrawal permission
# DISABLED, per §10) and the database settings.

cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Create an empty database

```bash
createdb trading_pipeline
```

Do **not** run `alembic upgrade head` first. The dump contains the schema
and the `alembic_version` row; restoring into an already-migrated database
means `restore_db.sh` has to drop and recreate everything it finds.

### 4. Restore

```bash
cd /path/to/trading-pipeline
export DATABASE_URL='postgresql://postgres:postgres@localhost:5432/trading_pipeline'

./scripts/restore_db.sh ./backups/trading_pipeline_<STAMP>.dump
```

The script prints row counts per table. Compare them against the old
machine — that comparison is the actual verification, not the absence of
an error message.

`restore_db.sh` refuses to run against a database that already contains
trades, so a mistyped target cannot quietly destroy a live record. Override
with `FORCE=1` only when you are certain.

### 5. Restore the model files — do not skip this

```bash
tar -xzf ./backups/models_store.tar.gz -C backend/app
ls backend/app/models_store/ | head
```

Verify the active model is actually loadable:

```bash
cd backend
DATABASE_URL="$DATABASE_URL" .venv/bin/python - <<'PY'
import asyncio, sys
sys.path.insert(0, '.')
from app.db.session import AsyncSessionLocal
from app.services.model_registry import load_active_model
from app.services.config_service import get_config

async def main():
    async with AsyncSessionLocal() as db:
        for symbol in await get_config(db, 'symbols'):
            try:
                record, _ = await load_active_model(db, symbol)
                print(f'{symbol}: OK  {record.file_path}')
            except Exception as exc:
                print(f'{symbol}: {exc}')

asyncio.run(main())
PY
```

Every configured symbol should print `OK` or say plainly that it has no
active model yet. Anything else means the files did not come across.

### 6. Confirm the migration

```bash
cd backend
.venv/bin/alembic current       # should already be at head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open the dashboard and check:

- **Stage badge** matches what the old machine was on.
- **Component strip** — Binance API and Data Feed go green.
- **Models page** — no model shows "file missing".
- **Trades feed and wallet** show your existing history.

The system starts with trading **stopped** regardless of the old machine's
state, because `trading_enabled` is only turned on deliberately. Start it
when you are satisfied the migration is sound.

---

## Backup schedule

`backup_db.sh` is safe to run on a cron while the system is trading —
`pg_dump` takes a consistent snapshot and does not lock writers.

```cron
# Daily at 03:00 UTC, keeping 14 days.
0 3 * * * cd /path/to/trading-pipeline && \
  DATABASE_URL='postgresql://…' ./scripts/backup_db.sh ./backups && \
  find ./backups -name '*.dump' -mtime +14 -delete
```

Back up `models_store/` on the same schedule. It only changes when a model
trains, so a weekly tarball is usually enough — but a database restored
alongside model files from a *different* week will have `models` rows with
no matching file. Keep them in step.

§10: if backups leave this machine, encrypt them. The dump contains your
full trading history and the stage PIN hash.
