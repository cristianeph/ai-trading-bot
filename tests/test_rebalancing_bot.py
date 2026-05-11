import unittest
from unittest.mock import MagicMock, patch
import decimal
import math
import sys
import os

import sys
import os
import shutil

# Copy bot_rebalancing.py to a temporary file with a valid name
shutil.copy('bot-rebalancing/bot_rebalancing.py', 'bot_rebalancing_test_helper.py')

from bot_rebalancing_test_helper import RebalancingTradingBot, RebalancingConfig
from common.base_bot import compute_equity

class TestRebalancingBot(unittest.TestCase):
    def setUp(self):
        self.storage_mock = MagicMock()
        self.model_client_mock = MagicMock()
        
        # Mock RebalancingConfig.from_storage to return a default config
        self.config = MagicMock(spec=RebalancingConfig)
        self.config.symbols = ['BTC/USDT', 'ETH/USDT']
        self.config.target_weights = {'BTC/USDT': 0.5, 'ETH/USDT': 0.5}
        self.config.rebalance_threshold_pct = 0.02
        self.config.max_trade_pct = 0.25
        self.config.cash_buffer_pct = 0.05
        self.config.cash_buffer_min_usdt = 10.0
        self.config.min_trade_amount = {'BTC/USDT': 0.0, 'ETH/USDT': 0.0}
        self.config.sleep_seconds = 60
        self.config.min_confidence = 0.5
        self.config.smart_scale_max_delta_pct = 0.1
        self.config.smart_vol_enabled = True
        self.config.bot_enabled = True
        self.config.hold_conf_margin = 0.05
        self.config.hold_sample_every_min = 60

        with patch('bot_rebalancing_test_helper.RebalancingConfig.from_storage', return_value=self.config):
            self.bot = RebalancingTradingBot(
                balance=1000.0,
                storage=self.storage_mock,
                model_client=self.model_client_mock
            )
            self.bot.config = self.config

    def test_decimal_conversion_syntax_simulation(self):
        """
        Test if the bot handles extremely small amounts without triggering decimal.ConversionSyntax
        when calling place_order (simulated).
        """
        # Set a very low min_notional to allow this test to proceed
        self.bot._get_min_notional = MagicMock(return_value=0.000000001)

        price = 80000.0
        trade_value = 0.0008 # 0.0008 / 80000 = 0.00000001
        
        with patch('bot_rebalancing_test_helper.place_order') as mock_place_order:
            mock_place_order.return_value = {
                'id': '123',
                'filled': 0.00000001,
                'price': 80000.0,
                'average': 80000.0,
                'cost': 0.0008,
                'fee': {'currency': 'USDT', 'cost': 0.000008}
            }
            
            self.bot.positions['BTC/USDT'] = {
                'symbol': 'BTC/USDT',
                'side': 'buy',
                'amount': 1.0,
                'entry_price': 70000.0,
                'entry_fee_usdt': 1.0
            }
            
            self.bot._get_free_balance_base = MagicMock(return_value=1.0)
            
            try:
                self.bot._rebalance_sell('BTC/USDT', price, trade_value)
            except Exception as e:
                self.fail(f'_rebalance_sell raised {type(e)}: {e}')

            mock_place_order.assert_called()
            args, kwargs = mock_place_order.call_args
            # The third arg is amount
            self.assertIsInstance(args[2], float)
            self.assertEqual(args[2], 0.00000001)

    def test_compute_equity_missing_price(self):
        """
        Test if compute_equity handles positions with missing price keys.
        """
        cash = 100.0
        positions = {
            'BTC/USDT': {
                'amount': 1.0,
                'entry_price': 50000.0
            }
        }
        
        equity = compute_equity(cash, positions)
        self.assertEqual(equity, 50100.0)
        
        positions['BTC/USDT'] = {'amount': 1.0}
        equity = compute_equity(cash, positions)
        self.assertEqual(equity, 100.0)

    def test_log_equity_safety(self):
        """
        Test if _log_equity doesn't crash if prices are missing.
        """
        self.bot.capital = 100.0
        self.bot.positions = {
            'BTC/USDT': {'amount': 0.001}
        }
        
        self.bot.storage.log_equity = MagicMock()
        self.bot.storage.log_drift = MagicMock()
        
        try:
            self.bot._log_equity()
        except Exception as e:
            self.fail(f'_log_equity raised {type(e)}: {e}')

    def test_min_notional_skipping(self):
        """
        Test if the bot skips trades below minimum notional.
        """
        self.bot._get_min_notional = MagicMock(return_value=10.0)
        
        # Mock _execute_exchange_order to return a dummy result to avoid unpacking errors
        dummy_result = (1.0, 100.0, 100.0, 'USDT', 0.1)

        # BUY case
        with patch.object(self.bot, '_execute_exchange_order', return_value=dummy_result) as mock_exec:
            self.bot._rebalance_buy('BTC/USDT', 80000.0, 5.0) # 5.0 USDT < 10.0
            mock_exec.assert_not_called()
            
            self.bot._rebalance_buy('BTC/USDT', 80000.0, 15.0) # 15.0 USDT > 10.0
            mock_exec.assert_called()

        # SELL case
        with patch.object(self.bot, '_execute_exchange_order', return_value=dummy_result) as mock_exec:
            self.bot.positions['BTC/USDT'] = {
                'side': 'buy',
                'amount': 1.0,
                'entry_price': 70000.0
            }
            self.bot._get_free_balance_base = MagicMock(return_value=1.0)
            
            self.bot._rebalance_sell('BTC/USDT', 80000.0, 5.0) # 5.0 USDT < 10.0
            mock_exec.assert_not_called()
            
            self.bot._rebalance_sell('BTC/USDT', 80000.0, 15.0) # 15.0 USDT > 10.0
            mock_exec.assert_called()

if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        if os.path.exists('bot_rebalancing_test_helper.py'):
            os.remove('bot_rebalancing_test_helper.py')
