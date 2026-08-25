"""seed risk engine config defaults

Adds the keys the risk engine reads that were not part of the original
seed. Re-runs the full CONFIG_DEFAULTS insert with ON CONFLICT DO NOTHING,
so only genuinely new keys are written and user-changed values are left
alone.

Revision ID: 44c8e3a2b9f7
Revises: 33b7d2f1a8c5
Create Date: 2026-08-25

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.config_defaults import CONFIG_DEFAULTS

# revision identifiers, used by Alembic.
revision: str = '44c8e3a2b9f7'
down_revision: Union[str, Sequence[str], None] = '33b7d2f1a8c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_KEYS = [
    "required_healthy_components",
    "volatility_lookback_candles",
    "max_pnl_baseline_age_hours",
]


def upgrade() -> None:
    connection = op.get_bind()
    for key, value in CONFIG_DEFAULTS.items():
        connection.execute(
            sa.text(
                "INSERT INTO config (key, value) VALUES (:key, CAST(:value AS jsonb)) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": key, "value": json.dumps(value)},
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM config WHERE key = ANY(:keys)"), {"keys": NEW_KEYS}
    )
