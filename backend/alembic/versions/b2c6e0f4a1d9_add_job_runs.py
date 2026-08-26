"""add job_runs table

Audit-trail rows for background jobs (data downloads, training runs) so the
dashboard can answer "what's the most recent download/training run status"
from the database on page load, instead of only from a WebSocket event that
may have arrived while the tab was closed.

Revision ID: b2c6e0f4a1d9
Revises: d4a1c8e2f091
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b2c6e0f4a1d9'
down_revision: Union[str, Sequence[str], None] = 'd4a1c8e2f091'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('job_type', sa.String(length=20), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=True),
        sa.Column('status', sa.String(length=15), nullable=False, server_default='running'),
        sa.Column('progress', sa.Numeric(), nullable=True),
        sa.Column('detail', postgresql.JSONB(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Every "latest run of this type" query filters on job_type and orders
    # by started_at — this index serves both in one pass.
    op.create_index(
        'ix_job_runs_type_started_at', 'job_runs', ['job_type', 'started_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_job_runs_type_started_at', table_name='job_runs')
    op.drop_table('job_runs')
