"""target_weight_scheduling

Revision ID: af33407db16c
Revises: 39f988634c73
Create Date: 2026-05-10 23:43:41.093774

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'af33407db16c'
down_revision: Union[str, Sequence[str], None] = '39f988634c73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'targetweightschedule',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bot_type', sa.String(length=255), nullable=False),
        sa.Column('symbol', sa.String(length=255), nullable=False),
        sa.Column('target_weight', sa.Float(), nullable=False),
        sa.Column('start_timestamp', sa.String(length=255), nullable=False),
        sa.Column('end_timestamp', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_targetweightschedule_bot_type'), 'targetweightschedule', ['bot_type'], unique=False)
    op.create_index(op.f('ix_targetweightschedule_start_timestamp'), 'targetweightschedule', ['start_timestamp'], unique=False)
    op.create_index(op.f('ix_targetweightschedule_end_timestamp'), 'targetweightschedule', ['end_timestamp'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_targetweightschedule_end_timestamp'), table_name='targetweightschedule')
    op.drop_index(op.f('ix_targetweightschedule_start_timestamp'), table_name='targetweightschedule')
    op.drop_index(op.f('ix_targetweightschedule_bot_type'), table_name='targetweightschedule')
    op.drop_table('targetweightschedule')
