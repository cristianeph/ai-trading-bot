"""add bot_type columns

Revision ID: d06f4b116dd2
Revises: a6c5fe9f718c
Create Date: 2025-11-30 12:19:40.363578

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd06f4b116dd2'
down_revision: Union[str, Sequence[str], None] = 'a6c5fe9f718c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add bot_type to trade & decision."""

    # Add bot_type to trade
    op.add_column(
        "trade",
        sa.Column("bot_type", sa.String(length=50), nullable=True),
    )

    # Add bot_type to decision
    op.add_column(
        "decision",
        sa.Column("bot_type", sa.String(length=50), nullable=True),
    )

    # Backfill existing rows
    op.execute("UPDATE trade SET bot_type = 'foundational' WHERE bot_type IS NULL")
    op.execute("UPDATE decision SET bot_type = 'foundational' WHERE bot_type IS NULL")


def downgrade() -> None:
    """Downgrade schema: remove bot_type."""
    op.drop_column("decision", "bot_type")
    op.drop_column("trade", "bot_type")
