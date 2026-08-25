"""seed LLM advisor config keys

Revision ID: b2d81e5a6c07
Revises: a1f70c9d3e42
Create Date: 2026-08-25
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.config_defaults import CONFIG_DEFAULTS

revision: str = 'b2d81e5a6c07'
down_revision: Union[str, Sequence[str], None] = 'a1f70c9d3e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_KEYS = [
    "llm_models", "llm_timeout_seconds", "llm_advisory_hours_utc",
    "llm_confidence_adjustment_enabled", "llm_uncertainty_confidence_bonus",
    "llm_advisory_max_age_hours",
]


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


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM config WHERE key = ANY(:keys)"), {"keys": NEW_KEYS}
    )
