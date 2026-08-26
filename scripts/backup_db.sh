#!/usr/bin/env bash
#
# Back up the trading pipeline database (§12 step 12).
#
# What this captures: everything in Postgres — candles, models metadata,
# trades, risk_log, wallet_snapshots, llm_advisories, config (including the
# stage PIN hash) and component_status.
#
# What this does NOT capture, and must be moved separately:
#   * backend/app/models_store/  — the trained model .json files. The
#     `models` table stores a file_path, not the model itself, so a restore
#     without these leaves every model row pointing at a missing file.
#   * .env — secrets are deliberately not in the database.
# See scripts/migrate_to_new_machine.md.
#
# Usage:
#   ./scripts/backup_db.sh [output_directory]
#
# Environment:
#   DATABASE_URL   postgresql://user:pass@host:port/dbname   (preferred)
#   or PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE

set -euo pipefail

OUT_DIR="${1:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT_DIR"

DUMP_FILE="$OUT_DIR/trading_pipeline_${STAMP}.dump"

if [[ -n "${DATABASE_URL:-}" ]]; then
  # Strip a SQLAlchemy driver suffix (postgresql+asyncpg://) that libpq
  # does not understand.
  CONN="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"
  CONN="${CONN/postgresql+psycopg2:/postgresql:}"
  echo "Backing up from DATABASE_URL to $DUMP_FILE"
  pg_dump --format=custom --no-owner --no-privileges --file="$DUMP_FILE" "$CONN"
else
  : "${PGDATABASE:=trading_pipeline}"
  echo "Backing up ${PGDATABASE} to $DUMP_FILE"
  pg_dump --format=custom --no-owner --no-privileges --file="$DUMP_FILE" "$PGDATABASE"
fi

SIZE="$(du -h "$DUMP_FILE" | cut -f1)"
echo "Wrote $DUMP_FILE ($SIZE)"

# A dump nobody can restore is not a backup. Verify it is readable and
# reports the tables we expect before reporting success.
TABLE_COUNT="$(pg_restore --list "$DUMP_FILE" | grep -c 'TABLE DATA' || true)"
echo "Dump contains $TABLE_COUNT table data section(s)."

if [[ "$TABLE_COUNT" -lt 1 ]]; then
  echo "ERROR: dump contains no table data. Not treating this as a backup." >&2
  exit 1
fi

echo
echo "REMINDER: this dump does NOT include backend/app/models_store/ or .env."
echo "Copy models_store/ separately or trained models will not load after a restore."
