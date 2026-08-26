#!/usr/bin/env bash
#
# Restore the trading pipeline database from a pg_dump custom-format file.
#
# Usage:
#   ./scripts/restore_db.sh <dump_file> [target_database_url]
#
# The target database must already exist and should be EMPTY. Restoring
# over a database with data is refused unless FORCE=1, because doing it by
# accident on the wrong machine destroys a live trading record.
#
# Example:
#   createdb trading_pipeline
#   ./scripts/restore_db.sh backups/trading_pipeline_20260825T120000Z.dump \
#       postgresql://postgres:postgres@localhost:5432/trading_pipeline

set -euo pipefail

DUMP_FILE="${1:-}"
TARGET="${2:-${DATABASE_URL:-}}"

if [[ -z "$DUMP_FILE" || ! -f "$DUMP_FILE" ]]; then
  echo "Usage: $0 <dump_file> [target_database_url]" >&2
  exit 1
fi

if [[ -z "$TARGET" ]]; then
  echo "ERROR: no target given and DATABASE_URL is unset." >&2
  exit 1
fi

TARGET="${TARGET/postgresql+asyncpg:/postgresql:}"
TARGET="${TARGET/postgresql+psycopg2:/postgresql:}"

echo "Restoring $DUMP_FILE into $TARGET"

# Refuse to overwrite a database that already holds trades, unless forced.
EXISTING="$(psql "$TARGET" -tAc \
  "SELECT COALESCE((SELECT count(*) FROM trades), 0)" 2>/dev/null || echo 0)"

if [[ "${EXISTING:-0}" -gt 0 && "${FORCE:-0}" != "1" ]]; then
  echo "ERROR: target already contains $EXISTING trades." >&2
  echo "Restoring would overwrite a real trading record. Re-run with FORCE=1" >&2
  echo "only if you are certain this is the right database." >&2
  exit 1
fi

pg_restore \
  --dbname="$TARGET" \
  --no-owner \
  --no-privileges \
  --clean --if-exists \
  "$DUMP_FILE"

echo
echo "Restore complete. Row counts:"
psql "$TARGET" -c "
SELECT 'candles' AS table, count(*) FROM candles
UNION ALL SELECT 'models', count(*) FROM models
UNION ALL SELECT 'trades', count(*) FROM trades
UNION ALL SELECT 'wallet_snapshots', count(*) FROM wallet_snapshots
UNION ALL SELECT 'risk_log', count(*) FROM risk_log
UNION ALL SELECT 'llm_advisories', count(*) FROM llm_advisories
UNION ALL SELECT 'config', count(*) FROM config
UNION ALL SELECT 'component_status', count(*) FROM component_status
ORDER BY 1;"

echo
echo "NEXT STEP: copy backend/app/models_store/ from the old machine, or every"
echo "row in \`models\` will point at a file that does not exist. Verify with:"
echo "  psql \"\$TARGET\" -tAc \"SELECT file_path FROM models WHERE status='active'\""
