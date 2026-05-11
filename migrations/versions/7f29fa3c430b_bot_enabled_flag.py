"""bot_enabled_flag

Revision ID: 7f29fa3c430b
Revises: af33407db16c
Create Date: 2026-05-10 23:59:44.270970

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f29fa3c430b'
down_revision: Union[str, Sequence[str], None] = 'af33407db16c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add bot_enabled to botconfig table for rebalancing bot
    op.execute("INSERT INTO botconfig (bot_type, `key`, `value`) VALUES ('rebalancing', 'bot_enabled', 'true')")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM botconfig WHERE bot_type = 'rebalancing' AND `key` = 'bot_enabled'")
