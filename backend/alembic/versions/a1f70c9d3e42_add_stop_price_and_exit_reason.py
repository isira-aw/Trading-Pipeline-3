"""add trades.stop_price and trades.exit_reason, seed ATR config

stop_price is set on the ENTRY trade (the ATR-derived stop, fixed at open);
exit_reason is set on the EXIT trade (stop_hit | target_reached |
horizon_elapsed). Two nullable columns rather than JSONB because both are
queryable facts the dashboard and any later analysis will filter on, and
because risk_notes is specifically the risk engine's entry-side record —
the stop is a trading-engine exit decision and is kept separate.

Revision ID: a1f70c9d3e42
Revises: 9e1a4c7b2d80
Create Date: 2026-08-25

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.config_defaults import CONFIG_DEFAULTS

revision: str = 'a1f70c9d3e42'
down_revision: Union[str, Sequence[str], None] = '9e1a4c7b2d80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_KEYS = ["atr_period", "atr_stop_multiplier"]


def upgrade() -> None:
    op.add_column('trades', sa.Column('stop_price', sa.Numeric(), nullable=True))
    op.add_column('trades', sa.Column('exit_reason', sa.String(length=20), nullable=True))

    connection = op.get_bind()
    for key in NEW_KEYS:
        connection.execute(
            sa.text(
                "INSERT INTO config (key, value) VALUES (:key, CAST(:value AS jsonb)) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": key, "value": json.dumps(CONFIG_DEFAULTS[key])},
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM config WHERE key = ANY(:keys)"), {"keys": NEW_KEYS}
    )
    op.drop_column('trades', 'exit_reason')
    op.drop_column('trades', 'stop_price')
