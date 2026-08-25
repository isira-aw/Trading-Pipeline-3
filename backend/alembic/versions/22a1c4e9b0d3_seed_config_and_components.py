"""seed config table and component_status rows

Seeds the DB-backed settings described in §3/§5.4/§6/§11 plus the
component_status heartbeat rows the risk engine's health check reads.

Idempotent: uses ON CONFLICT DO NOTHING so re-running never clobbers a
value the user has since changed from the Settings page.

Revision ID: 22a1c4e9b0d3
Revises: 11f243285f66
Create Date: 2026-08-25

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.config_defaults import CONFIG_DEFAULTS, COMPONENTS
from app.services.security import hash_pin, DEFAULT_PIN

# revision identifiers, used by Alembic.
revision: str = '22a1c4e9b0d3'
down_revision: Union[str, Sequence[str], None] = '11f243285f66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Seed config defaults and component heartbeat rows."""
    connection = op.get_bind()

    seed = dict(CONFIG_DEFAULTS)
    # Generated per-install so every deployment gets a distinct salt.
    # §10: default PIN is 000000 and the UI must warn while it is unchanged.
    seed["stage_pin_hash"] = hash_pin(DEFAULT_PIN)

    for key, value in seed.items():
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
    """Remove the seeded rows."""
    connection = op.get_bind()

    keys = list(CONFIG_DEFAULTS.keys()) + ["stage_pin_hash"]
    connection.execute(
        sa.text("DELETE FROM config WHERE key = ANY(:keys)"), {"keys": keys}
    )
    connection.execute(
        sa.text("DELETE FROM component_status WHERE component = ANY(:components)"),
        {"components": COMPONENTS},
    )
