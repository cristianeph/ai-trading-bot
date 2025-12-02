"""make decision outcome fields nullable

Revision ID: cfbe9a66b878
Revises: 57f3a1205080
Create Date: 2025-12-01 14:51:22.013070

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cfbe9a66b878'
down_revision: Union[str, Sequence[str], None] = '57f3a1205080'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "decision",
        "outcome_pnl_usdt",
        existing_type=sa.Float(),
        nullable=True,
    )
    op.alter_column(
        "decision",
        "outcome_pnl_pct",
        existing_type=sa.Float(),
        nullable=True,
    )
    op.alter_column(
        "decision",
        "outcome_equity_delta",
        existing_type=sa.Float(),
        nullable=True,
    )
    op.alter_column(
        "decision",
        "outcome_label",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "decision",
        "outcome_label",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "decision",
        "outcome_equity_delta",
        existing_type=sa.Float(),
        nullable=False,
    )
    op.alter_column(
        "decision",
        "outcome_pnl_pct",
        existing_type=sa.Float(),
        nullable=False,
    )
    op.alter_column(
        "decision",
        "outcome_pnl_usdt",
        existing_type=sa.Float(),
        nullable=False,
    )
