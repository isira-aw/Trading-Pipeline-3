"""seed promotion_gate_changed_at

Revision ID: c39f2b7d4e18
Revises: b2d81e5a6c07
Create Date: 2026-08-25
"""
import json
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from app.config_defaults import CONFIG_DEFAULTS

revision: str = 'c39f2b7d4e18'
down_revision: Union[str, Sequence[str], None] = 'b2d81e5a6c07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "INSERT INTO config (key, value) VALUES (:key, CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {"key": "promotion_gate_changed_at",
         "value": json.dumps(CONFIG_DEFAULTS["promotion_gate_changed_at"])},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM config WHERE key = 'promotion_gate_changed_at'")
    )
