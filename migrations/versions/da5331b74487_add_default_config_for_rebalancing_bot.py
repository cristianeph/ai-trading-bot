"""add default config for rebalancing bot

Revision ID: da5331b74487
Revises: 0110e6124bb7
Create Date: 2025-12-03 00:52:11.284886

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'da5331b74487'
down_revision: Union[str, Sequence[str], None] = '0110e6124bb7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    botconfig_table = sa.table(
        "botconfig",
        sa.Column("id", sa.Integer),
        sa.Column("bot_type", sa.String(255)),
        sa.Column("key", sa.String(255)),
        sa.Column("value", sa.String(255)),
    )

    op.bulk_insert(
        botconfig_table,
        [
            # -----------------------------------------------------------------
            # Parámetros generales del bot de rebalancing
            # -----------------------------------------------------------------
            {
                "bot_type": "rebalancing",
                "key": "min_confidence",
                "value": "0.51",
            },
            {
                "bot_type": "rebalancing",
                "key": "sleep_seconds",
                "value": "60",
            },
            {
                "bot_type": "rebalancing",
                "key": "rebalance_threshold_pct",
                "value": "0.02",  # 2 %
            },
            {
                "bot_type": "rebalancing",
                "key": "max_trade_pct",
                "value": "0.25",  # 25 % de la equity
            },
            {
                "bot_type": "rebalancing",
                "key": "drastic_move_threshold",
                "value": "0.0005",
            },
            {
                "bot_type": "rebalancing",
                "key": "hold_conf_margin",
                "value": "0.10",
            },
            {
                "bot_type": "rebalancing",
                "key": "hold_sample_every_min",
                "value": "10",
            },

            # -----------------------------------------------------------------
            # Pesos objetivo del portafolio (BTC/USDT, ETH/USDT)
            # -----------------------------------------------------------------
            {
                "bot_type": "rebalancing",
                "key": "target_weight.BTC/USDT",
                "value": "0.5",
            },
            {
                "bot_type": "rebalancing",
                "key": "target_weight.ETH/USDT",
                "value": "0.5",
            },

            # -----------------------------------------------------------------
            # Mínimos de trade por símbolo
            # -----------------------------------------------------------------
            {
                "bot_type": "rebalancing",
                "key": "min_trade_amount.BTC/USDT",
                "value": "0.00001",
            },
            {
                "bot_type": "rebalancing",
                "key": "min_trade_amount.ETH/USDT",
                "value": "0.0001",
            },
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    pass
