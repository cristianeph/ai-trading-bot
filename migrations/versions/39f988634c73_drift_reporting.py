"""drift_reporting

Revision ID: 39f988634c73
Revises: 9b868369eab1
Create Date: 2026-05-10 22:43:11.914994

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '39f988634c73'
down_revision: Union[str, Sequence[str], None] = '9b868369eab1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'driftlog',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('timestamp', sa.String(length=255), nullable=False),
        sa.Column('total_drift', sa.Float(), nullable=False),
        sa.Column('mode', sa.String(length=50), nullable=False),
        sa.Column('bot_type', sa.String(length=50), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
    )
    op.create_index(op.f('ix_driftlog_timestamp'), 'driftlog', ['timestamp'], unique=False)
    op.create_index(op.f('ix_driftlog_mode'), 'driftlog', ['mode'], unique=False)
    op.create_index(op.f('ix_driftlog_bot_type'), 'driftlog', ['bot_type'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('driftlog')
