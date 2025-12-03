"""add additional config for rebalancing bot

Revision ID: 8c6e8d454895
Revises: da5331b74487
Create Date: 2025-12-03 02:24:55.764306

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c6e8d454895'
down_revision: Union[str, Sequence[str], None] = 'da5331b74487'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    op.execute(
        """INSERT INTO botconfig (bot_type, `key`, `value`) VALUES
        -- Cash buffer: deja parte del equity como USDT líquido
        ('rebalancing', 'cash_buffer_pct', '0.05'),
        
        -- Smart scaling según desviación % (delta_pct)
        ('rebalancing', 'smart_scale_max_delta_pct', '0.10'),
        
        -- Volatility-aware sizing
        ('rebalancing', 'smart_vol_enabled', 'true'),
        ('rebalancing', 'smart_vol_medium', '0.02'),
        ('rebalancing', 'smart_vol_high', '0.04');
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    pass
