"""seed halt flags

Revision ID: d4a1c8e2f091
Revises: c39f2b7d4e18
Create Date: 2026-08-25
"""
import json
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from app.config_defaults import CONFIG_DEFAULTS

revision: str = 'd4a1c8e2f091'
down_revision: Union[str, Sequence[str], None] = 'c39f2b7d4e18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_KEYS = ["halted", "halted_at", "halted_reason"]


def upgrade() -> None:
    connection = op.get_bind()
    for key in NEW_KEYS:
        connection.execute(
            sa.text(
                "INSERT INTO config (key, value) VALUES (:key, CAST(:value AS jsonb)) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": key, "value": json.dumps(CONFIG_DEFAULTS[key])},
        )
    # Migrate any legacy 'halted' stage onto the new flag, so an upgrade
    # performed while halted does not silently resume trading.
    connection.execute(sa.text(
        "UPDATE config SET value = 'true'::jsonb WHERE key = 'halted' AND EXISTS "
        "(SELECT 1 FROM config c WHERE c.key = 'current_stage' AND c.value = '\"halted\"'::jsonb)"
    ))
    connection.execute(sa.text(
        "UPDATE config SET value = '\"paper\"'::jsonb "
        "WHERE key = 'current_stage' AND value = '\"halted\"'::jsonb"
    ))


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM config WHERE key = ANY(:keys)"), {"keys": NEW_KEYS}
    )
