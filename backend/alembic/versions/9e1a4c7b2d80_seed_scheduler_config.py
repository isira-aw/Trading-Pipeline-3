"""seed scheduler and reconciliation config, add reconciliation component

Revision ID: 9e1a4c7b2d80
Revises: 8d5966c1eaed
Create Date: 2026-08-25

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.config_defaults import CONFIG_DEFAULTS, COMPONENTS

revision: str = '9e1a4c7b2d80'
down_revision: Union[str, Sequence[str], None] = '8d5966c1eaed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_KEYS = [
    "trade_loop_interval_minutes",
    "heartbeat_interval_seconds",
    "reconcile_interval_minutes",
    "data_refresh_hour_utc",
    "trading_enabled",
    "reconcile_max_attempts",
    "reconcile_backoff_base_seconds",
    "reconcile_backoff_max_seconds",
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

    for component in COMPONENTS:
        connection.execute(
            sa.text(
                "INSERT INTO component_status (component, status, last_heartbeat, detail) "
                "VALUES (:component, 'offline', now(), 'Never started') "
                "ON CONFLICT (component) DO NOTHING"
            ),
            {"component": component},
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM config WHERE key = ANY(:keys)"), {"keys": NEW_KEYS}
    )
    connection.execute(
        sa.text("DELETE FROM component_status WHERE component = 'order_reconciliation'")
    )
