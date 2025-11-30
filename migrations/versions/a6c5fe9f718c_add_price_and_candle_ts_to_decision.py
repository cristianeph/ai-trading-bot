"""add price and candle_ts to decision

Revision ID: a6c5fe9f718c
Revises: 3a92be3458fe
Create Date: 2025-11-30 11:49:20.878579

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6c5fe9f718c'
down_revision: Union[str, Sequence[str], None] = '3a92be3458fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add price and candle_ts columns to decision."""
    op.add_column("decision", sa.Column("price", sa.Float(), nullable=True))
    op.add_column("decision", sa.Column("candle_ts", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema: remove price and candle_ts columns from decision."""
    op.drop_column("decision", "candle_ts")
    op.drop_column("decision", "price")
