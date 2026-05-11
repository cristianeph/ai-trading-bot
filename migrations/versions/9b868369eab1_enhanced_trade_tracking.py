"""enhanced_trade_tracking

Revision ID: 9b868369eab1
Revises: 8c6e8d454895
Create Date: 2026-05-10 22:30:19.158595

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b868369eab1'
down_revision: Union[str, Sequence[str], None] = '8c6e8d454895'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('trade', sa.Column('invested_usdt_equivalent', sa.Float(), nullable=True))
    op.add_column('trade', sa.Column('usdt_rate', sa.Float(), nullable=True))
    op.add_column('trade', sa.Column('fee_usdt', sa.Float(), nullable=True))
    op.add_column('trade', sa.Column('reference_id', sa.String(length=255), nullable=True))
    op.create_index(op.f('ix_trade_reference_id'), 'trade', ['reference_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_trade_reference_id'), table_name='trade')
    op.drop_column('trade', 'reference_id')
    op.drop_column('trade', 'fee_usdt')
    op.drop_column('trade', 'usdt_rate')
    op.drop_column('trade', 'invested_usdt_equivalent')
