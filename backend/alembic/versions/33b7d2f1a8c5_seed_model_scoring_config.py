"""seed any config defaults not already present

Re-runs the CONFIG_DEFAULTS seed with ON CONFLICT DO NOTHING, which adds
keys introduced since the previous seed (here: the model-scoring settings)
without touching values the user has changed.

Revision ID: 33b7d2f1a8c5
Revises: 22a1c4e9b0d3
Create Date: 2026-08-25

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.config_defaults import CONFIG_DEFAULTS

# revision identifiers, used by Alembic.
revision: str = '33b7d2f1a8c5'
down_revision: Union[str, Sequence[str], None] = '22a1c4e9b0d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Keys this revision introduces — tracked explicitly so downgrade removes
# only these, rather than every default.
NEW_KEYS = [
    "model_scoring_weights",
    "min_predicted_positive_rate",
    "min_trades_for_realized_score",
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
