from dataclasses import dataclass
from typing import Dict, Optional, Any, cast

import math
import time
import pandas as pd

from common.config import settings
from common.logger import BotLogger
from common.data_client import place_order, get_binance_client
from common.base_bot import BaseTradingBot, Position, compute_equity, PnL
from common.model_client import ModelClient
from common.storage import Storage

@dataclass
class FoundationalConfig:
    min_confidence: float
    sleep_seconds: int
    cash_buffer_pct: float
    fee_reserve_usdt: float
    tp_pct: float
    sl_pct: float
    drastic_move_threshold: float
    hold_conf_margin: float
    hold_sample_every_min: int
    min_trade_amount: Dict[str, float]
    symbols: list[str]
    bot_type: str
    max_drawdown_pct: float

    def validate(self) -> None:
        """
        Validate configuration parameters.
        Raises ValueError if any parameter is out of expected range.
        """
        if not (0 <= self.min_confidence <= 1):
            raise ValueError(f"min_confidence must be between 0 and 1, got {self.min_confidence}")
        if self.sleep_seconds <= 0:
            raise ValueError(f"sleep_seconds must be positive, got {self.sleep_seconds}")
        if not (0 <= self.cash_buffer_pct <= 1):
            raise ValueError(f"cash_buffer_pct must be between 0 and 1, got {self.cash_buffer_pct}")
        if self.fee_reserve_usdt < 0:
            raise ValueError(f"fee_reserve_usdt must be non-negative, got {self.fee_reserve_usdt}")
        if self.tp_pct <= 0:
            raise ValueError(f"tp_pct (Take Profit) must be positive, got {self.tp_pct}")
        if self.sl_pct >= 0:
            raise ValueError(f"sl_pct (Stop Loss) must be negative, got {self.sl_pct}")
        if self.drastic_move_threshold <= 0:
            raise ValueError(f"drastic_move_threshold must be positive, got {self.drastic_move_threshold}")
        if not (0 <= self.hold_conf_margin <= 1):
            raise ValueError(f"hold_conf_margin must be between 0 and 1, got {self.hold_conf_margin}")
        if self.hold_sample_every_min <= 0:
            raise ValueError(f"hold_sample_every_min must be positive, got {self.hold_sample_every_min}")
        if self.max_drawdown_pct <= 0 or self.max_drawdown_pct >= 1:
            raise ValueError(f"max_drawdown_pct must be between 0 and 1, got {self.max_drawdown_pct}")

    @classmethod
    def from_storage(
        cls,
        storage: Storage,
        default_min_confidence: float,
        default_sleep_seconds: int,
    ) -> "FoundationalConfig":
        min_confidence = storage.get_bot_config_float(
            "min_confidence",
            default=default_min_confidence,
        )
        sleep_seconds = storage.get_bot_config_int(
            "sleep_seconds",
            default=default_sleep_seconds,
        )

        cash_buffer_pct = storage.get_bot_config_float(
            "cash_buffer_pct",
            default=settings.DEFAULT_CASH_BUFFER_PCT,
        )
        fee_reserve_usdt = storage.get_bot_config_float(
            "fee_reserve_usdt",
            default=settings.DEFAULT_FEE_RESERVE_USDT,
        )

        tp_pct = storage.get_bot_config_float(
            "tp_pct", default=settings.DEFAULT_TP_PCT
        )
        sl_pct = storage.get_bot_config_float(
            "sl_pct", default=settings.DEFAULT_SL_PCT
        )

        drastic_move_threshold = storage.get_bot_config_float(
            "drastic_move_threshold",
            default=settings.DRASTIC_MOVE_THRESHOLD,
        )
        hold_conf_margin = storage.get_bot_config_float(
            "hold_conf_margin",
            default=settings.HOLD_CONF_MARGIN,
        )
        hold_sample_every_min = storage.get_bot_config_int(
            "hold_sample_every_min",
            default=settings.HOLD_SAMPLE_EVERY_MIN,
        )

        min_trade_amount = {
            symbol: storage.get_bot_config_float(
                f"min_trade_amount.{symbol}",
                default=settings.MIN_TRADE_AMOUNT.get(symbol, 0.00001),
            )
            for symbol in settings.SYMBOLS
        }

        max_drawdown_pct = storage.get_bot_config_float(
            "max_drawdown_pct",
            default=settings.DEFAULT_MAX_DRAWDOWN_PCT,
        )

        config = cls(
            min_confidence=min_confidence,
            sleep_seconds=sleep_seconds,
            cash_buffer_pct=cash_buffer_pct,
            fee_reserve_usdt=fee_reserve_usdt,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            drastic_move_threshold=drastic_move_threshold,
            hold_conf_margin=hold_conf_margin,
            hold_sample_every_min=hold_sample_every_min,
            min_trade_amount=min_trade_amount,
            symbols=settings.SYMBOLS,
            bot_type="foundational",
            max_drawdown_pct=max_drawdown_pct,
        )
        config.validate()
        return config


class FoundationalTradingBot(BaseTradingBot):
    """
    V1 strategy (conservative):
    - At most one long position per symbol.
    - Uses micro TP/SL.
    - Dynamic position sizing based on volatility.
    """

    def __init__(
        self,
        *,
        sleep_seconds: int = 30,
        min_confidence: float = 0.52,
        balance: float = 0.0,
        initial_btc_amount: float = 0.0,
        storage: Optional[Storage] = None,
        model_client: Optional[ModelClient] = None,
    ) -> None:
        self._default_sleep_seconds = sleep_seconds
        self._default_min_confidence = min_confidence

        super().__init__(
            balance=balance,
            storage=storage,
            model_client=model_client,
            bot_type="foundational",
        )

        self.log = BotLogger("FoundationalTradingBot")
        self.log.info(f"[DEBUG CONFIG] BINANCE_TESTNET={settings.BINANCE_TESTNET}, TRADING_MODE={settings.TRADING_MODE}")

        # If there is already BTC in the account and BTC/USDT is in SYMBOLS, create an initial position
        if initial_btc_amount > 0 and "BTC/USDT" in self.config.symbols:
            self._init_existing_btc(initial_btc_amount)

        # Recalculate initial equity including potential BTC position
        self.initial_equity = compute_equity(self.capital, self.positions)

    def load_config(self) -> FoundationalConfig:
        """
        Load strategy-specific configuration from Storage.
        """
        return FoundationalConfig.from_storage(
            self.storage,
            default_min_confidence=self._default_min_confidence,
            default_sleep_seconds=self._default_sleep_seconds,
        )

    # ------------------------------------------------------------------ #
    # Strategy-specific helpers                                          #
    # ------------------------------------------------------------------ #

    def _available_capital_for_new_trades(self) -> float:
        """
        Calculate capital available for new trades, respecting buffers and fee reserves.
        """
        buffer_usdt = max(0.0, float(self.capital) * float(self.config.cash_buffer_pct))
        reserve_usdt = max(0.0, float(self.config.fee_reserve_usdt))
        available = float(self.capital) - buffer_usdt - reserve_usdt
        return max(0.0, available)

    def _init_existing_btc(self, initial_btc_amount: float) -> None:
        """
        Initialize an existing BTC position if and only if:
          - the amount is above the minimum tradable threshold, and
          - the trade history indicates there is an open long (last BUY is more
            recent than last SELL) for this bot_type and mode.

        Otherwise, the existing BTC balance is treated as external/dust and is
        not managed as an open position by this bot-foundational.
        """
        try:
            min_amount = self.config.min_trade_amount.get("BTC/USDT")
            if min_amount is not None and initial_btc_amount < min_amount:
                # Too small to trade reliably: treat as dust, do not create a position.
                self.log.info(
                    f"[BTC/USDT] Existing BTC balance {initial_btc_amount:.8f} "
                    f"is below minimum tradable amount {min_amount:.8f}. "
                    f"Ignoring it as dust (no managed position will be created)."
                )
                return

            last_buy = self.storage.get_last_buy("BTC/USDT", mode=settings.TRADING_MODE)
            last_sell = self.storage.get_last_sell("BTC/USDT", mode=settings.TRADING_MODE)

            # Determine if there is an open long in the DB:
            # - No BUY -> no open long.
            # - BUY exists and no SELL -> open long.
            # - BUY and SELL exist -> open long only if last BUY is more recent.
            if last_buy is None:
                self.log.info(
                    "[BTC/USDT] Existing BTC balance detected but no BUY trades "
                    "for this bot-foundational/mode. Treating it as external balance; "
                    "no managed position will be created."
                )
                return

            if last_sell is not None and last_sell.timestamp >= last_buy.timestamp:
                self.log.info(
                    "[BTC/USDT] Existing BTC balance detected but last SELL is "
                    "more recent than or equal to last BUY. Assuming no open "
                    "bot-foundational-managed long position; will not create a managed position."
                )
                return

            # At this point we consider there is an open long from the bot-foundational's perspective.
            entry_price = float(last_buy.price)
            self.log.info(
                f"[BTC/USDT] Loaded existing BTC using last BUY from DB: "
                f"amount={initial_btc_amount:.6f}, entry≈{entry_price:.2f}"
            )

            self.positions["BTC/USDT"] = cast(
                Position,
                {
                    "side": "buy",
                    "amount": float(initial_btc_amount),
                    "entry_price": entry_price,
                    "entry_fee_usdt": 0.0,
                    "last_price": entry_price,
                },
            )
        except Exception as init_exc:  # noqa: BLE001
            self.log.error(f"[BTC/USDT] Error initializing existing BTC position: {init_exc}")

    def _maybe_close_position_by_pnl(self, symbol: str, price: float) -> bool:
        """
        Check if the position should be closed based on TP/SL thresholds.
        """
        current_pos = self.positions.get(symbol)
        if current_pos is None or current_pos.get("side") != "buy":
            return False

        amount = float(current_pos.get("amount", 0.0))
        entry_price = float(current_pos.get("entry_price", 0.0))
        entry_fee_usdt = float(current_pos.get("entry_fee_usdt", 0.0))

        if amount <= 0 or entry_price <= 0:
            return False

        pnl = self._compute_unrealized_pnl(
            amount=amount,
            entry_price=entry_price,
            entry_fee_usdt=entry_fee_usdt,
            current_price=price,
        )

        if pnl.unrealized_pnl_pct >= self.config.tp_pct:
            self.log.info(
                f"[{symbol}] TP reached ({pnl.unrealized_pnl_pct:.3%}), "
                f"forcing SELL for risk management."
            )
            self._handle_sell(symbol, price, confidence=1.0)
            return True

        if pnl.unrealized_pnl_pct <= self.config.sl_pct:
            self.log.info(
                f"[{symbol}] SL reached ({pnl.unrealized_pnl_pct:.3%}), "
                f"forcing SELL for risk management."
            )
            self._handle_sell(symbol, price, confidence=1.0)
            return True

        return False

    def _prepare_position_value_for_long(
        self,
        symbol: str,
        confidence: float,
        position_value: Optional[float],
    ) -> Optional[float]:
        """
        Validate and normalize the desired position value for a new long.
        Returns the effective position value or None if the trade should be skipped.
        """
        if confidence < self.config.min_confidence:
            self.log.info(
                f"[{symbol}] Skipping BUY: conf={confidence:.2f} < "
                f"min_conf={self.config.min_confidence:.2f}"
            )
            return None

        if self.positions.get(symbol) is not None:
            self.log.info(
                f"[{symbol}] Skipping BUY: position already open, "
                f"conf={confidence:.2f}"
            )
            return None

        if position_value is None:
            # Nunca usar 100% del capital: respeta colchón + reserva de fees
            available = self._available_capital_for_new_trades()
            position_value = available * settings.POSITION_SIZE_PCT
        else:
            # Si viene un valor dinámico, igual lo capamos por el capital disponible
            available = self._available_capital_for_new_trades()
            position_value = min(float(position_value), available)

        # Si no queda capital disponible para operar, skip
        if position_value <= 0:
            self.log.info(
                f"[{symbol}] Skipping BUY: insufficient available capital after buffers "
                f"(capital={self.capital:.2f}, cash_buffer_pct={self.config.cash_buffer_pct:.2%}, "
                f"fee_reserve_usdt={self.config.fee_reserve_usdt:.2f})"
            )
            return None

        if not math.isfinite(position_value):
            self.log.info(f"[{symbol}] position_value invalid: {position_value}")
            return None

        return position_value

    def _execute_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        retries: int = 2,
    ) -> tuple[float, float, float]:
        """
        Execute an order and return (executed_amount, avg_price, fee_usdt).
        Implements a simple retry logic for connectivity or transient errors.
        """
        last_exception = None
        for attempt in range(retries):
            try:
                order = place_order(symbol, side, amount)
                if isinstance(order, dict):
                    executed_amount = float(order.get("amount") or amount)
                    avg_price = float(order.get("average") or order.get("price") or price)
                    fee_info = order.get("fee") or {}
                    fee_currency = fee_info.get("currency")
                    fee_cost = float(fee_info.get("cost") or 0.0)
                    fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
                else:
                    executed_amount = amount
                    avg_price = price
                    fee_usdt = 0.0

                return executed_amount, avg_price, fee_usdt

            except Exception as exc:  # noqa: BLE001
                last_exception = exc
                self.log.error(
                    f"[{symbol}] Attempt {attempt + 1}/{retries} failed to place {side.upper()} order: {exc}"
                )
                if attempt < retries - 1:
                    time.sleep(1)  # brief pause before retry

        self.log.error(f"[{symbol}] All {retries} attempts failed to place {side.upper()} order. Final error: {last_exception}")
        raise last_exception if last_exception else Exception(f"Unknown error placing {side.upper()} order")

    def _handle_buy(
        self,
        symbol: str,
        price: float,
        confidence: float,
        position_value: Optional[float] = None,
    ) -> None:
        """
        Handle the logic for buying an asset and opening a long position.
        """
        position_value = self._prepare_position_value_for_long(
            symbol, confidence, position_value
        )
        if position_value is None:
            return

        amount = position_value / price

        try:
            executed_amount, avg_price, entry_fee_usdt = self._execute_order(
                symbol, "buy", amount, price
            )
        except Exception:
            return

        cost = executed_amount * avg_price
        total_debit = cost + entry_fee_usdt
        self.capital -= total_debit

        self.positions[symbol] = cast(
            Position,
            {
                "side": "buy",
                "amount": executed_amount,
                "entry_price": avg_price,
                "entry_fee_usdt": entry_fee_usdt,
                "last_price": avg_price,
            },
        )

        self.storage.log_trade(
            symbol=symbol,
            side="buy",
            price=avg_price,
            amount=executed_amount,
            mode=settings.TRADING_MODE,
            pnl=0.0,
        )
        self.log.info(
            f"[{symbol}] Open long: amount={executed_amount:.6f}, "
            f"entry={avg_price:.2f}, capital={self.capital:.2f}"
        )

    def _handle_sell(
        self,
        symbol: str,
        price: float,
        confidence: float,
    ) -> None:
        """
        Handle the logic for selling an asset and closing a long position.
        """
        if confidence < self.config.min_confidence:
            self.log.info(
                f"[{symbol}] Skipping SELL: conf={confidence:.2f} < "
                f"min_conf={self.config.min_confidence:.2f}"
            )
            return

        current_pos = self.positions.get(symbol)
        if current_pos is None or current_pos.get("side") != "buy":
            self.log.info(
                f"[{symbol}] Skipping SELL: no open long position "
                f"(conf={confidence:.2f})"
            )
            return

        amount = float(current_pos["amount"])
        entry_price = float(current_pos["entry_price"])
        entry_fee_usdt = float(current_pos.get("entry_fee_usdt", 0.0))

        # Check actual balance on exchange to avoid "insufficient balance" errors
        # especially during Stop Loss or forced liquidations.
        try:
            exchange = get_binance_client()
            balance = exchange.fetch_balance()
            base_asset = symbol.split("/")[0]  # e.g., 'BTC' from 'BTC/USDT'
            actual_balance = float(balance.get(base_asset, {}).get("free", 0.0))
            
            if actual_balance < amount:
                self.log.info(
                    f"[{symbol}] Adjusting SELL amount: internal={amount:.8f}, "
                    f"exchange_free={actual_balance:.8f}"
                )
                amount = actual_balance

            if amount < self.config.min_trade_amount.get(symbol, 0.0):
                self.log.error(
                    f"[{symbol}] Cannot SELL: amount {amount:.8f} is below "
                    f"minimum trade amount."
                )
                # If it's too small to sell, we might as well consider it gone from managed positions
                # to avoid infinite loops, but here we'll just return and let the bot try again 
                # or wait for more price movement.
                # Actually, if it's dust, we should probably clear the position.
                if amount < (self.config.min_trade_amount.get(symbol, 0.0) / 2):
                     self.positions[symbol] = None
                return
        except Exception as bal_err:
            self.log.error(f"[{symbol}] Error fetching balance before SELL: {bal_err}")
            # Continue with internal amount if balance fetch fails

        try:
            executed_amount, exit_price, exit_fee_usdt = self._execute_order(
                symbol, "sell", amount, price
            )
        except Exception:
            return

        pnl, net_proceeds = self._compute_realized_pnl(
            amount=executed_amount,
            entry_price=entry_price,
            entry_fee_usdt=entry_fee_usdt,
            exit_price=exit_price,
            exit_fee_usdt=exit_fee_usdt,
        )
        self.capital += net_proceeds

        self.positions[symbol] = None

        self.storage.log_trade(
            symbol=symbol,
            side="sell",
            price=exit_price,
            amount=executed_amount,
            mode=settings.TRADING_MODE,
            pnl=pnl,
        )
        self.log.info(
            f"[{symbol}] Close long: amount={executed_amount:.6f}, "
            f"entry={entry_price:.2f}, exit={exit_price:.2f}, "
            f"pnl={pnl:.2f}, capital={self.capital:.2f}"
        )

    # ------------------------------------------------------------------ #
    # Get Profit / Liquidation                                           #
    # ------------------------------------------------------------------ #

    def _liquidate_position_to_base(self, symbol: str, price: float) -> None:
        """
        Liquidate an open position of the symbol to USDT.
        """
        current_pos = self.positions.get(symbol)
        if current_pos is None or current_pos.get("side") != "buy":
            return

        self.log.info(
            f"[{symbol}] [GET_PROFIT] Forcing liquidation SELL at price≈{price:.2f}"
        )
        # Usamos confidence=1.0 para bypass del min_confidence típico.
        self._handle_sell(symbol, price, confidence=1.0)

    def get_profit(self) -> None:
        """
        Public alias: sell all open positions and leave the bot in USDT.
        """
        self.liquidate_all_positions_to_base()

    # ------------------------------------------------------------------ #
    # Strategy hook implementation                                       #
    # ------------------------------------------------------------------ #

    def _should_skip_decision_same_candle(
        self,
        symbol: str,
        candle_ts: Any,
        price: float,
    ) -> bool:
        """
        Decide whether to skip a model decision when we are on the same candle
        and the price move is very small while a position is already open.
        """
        last_state = self.last_decision_state.get(
            symbol, {"candle_ts": None, "price": None}
        )
        last_candle_ts = last_state.get("candle_ts")
        last_decision_price = last_state.get("price")

        if last_candle_ts != candle_ts:
            return False

        if last_decision_price is None or last_decision_price <= 0:
            return False

        price_change = abs(price - last_decision_price) / last_decision_price
        current_pos = self.positions.get(symbol)
        if current_pos is None:
            return False

        if price_change < self.config.drastic_move_threshold:
            self.log.info(
                f"[{symbol}] Skipping model decision: same candle, "
                f"price_change={price_change:.4%} "
                f"(<{self.config.drastic_move_threshold:.4%})"
            )
            self._update_position_price(symbol, price)
            self._log_position_status(symbol)
            self._log_equity()
            return True

        return False

    def _apply_trading_logic(
        self,
        symbol: str,
        action: str,
        confidence: float,
        latest_row: pd.Series,
        price: float,
    ) -> None:
        """
        Apply the core trading logic (buy/sell) for this strategy.
        """
        if action == "buy":
            vol_20 = float(latest_row.get("vol_20", 0.0))
            base_pct = settings.POSITION_SIZE_PCT
            risk_factor = 1.0

            if vol_20 > 0:
                if vol_20 > 0.02:
                    risk_factor = 0.25
                elif vol_20 > 0.01:
                    risk_factor = 0.5
                elif vol_20 < 0.002:
                    risk_factor = 1.2

            available = self._available_capital_for_new_trades()
            dynamic_position_value = available * base_pct * risk_factor
            dynamic_position_value = max(0.0, min(dynamic_position_value, available))

            self._handle_buy(
                symbol,
                price,
                confidence,
                position_value=dynamic_position_value,
            )
        elif action == "sell":
            self._handle_sell(symbol, price, confidence)

    def _check_max_drawdown(self) -> None:
        """
        Implement a maximum drawdown circuit breaker to stop the bot if losses
        exceed a certain threshold.
        """
        equity = compute_equity(self.capital, self.positions)
        drawdown_pct = (self.initial_equity - equity) / self.initial_equity if self.initial_equity > 0 else 0.0

        if drawdown_pct >= self.config.max_drawdown_pct:
            self.log.error(
                f"[CIRCUIT BREAKER] Max Drawdown reached: {drawdown_pct:.2%}. "
                f"Initial Equity: {self.initial_equity:.2f}, Current Equity: {equity:.2f}. "
                f"Stopping bot."
            )
            self.get_profit()
            raise SystemExit("Max Drawdown circuit breaker triggered.")

    def _process_symbol_decision(
        self,
        *,
        symbol: str,
        latest_row: pd.Series,
        price: float,
        features: list[float],
        candle_ts: Any,
    ) -> None:
        """
        Process the decision for a specific symbol based on model prediction and risk management.
        """
        self._check_max_drawdown()

        if self._should_skip_decision_same_candle(symbol, candle_ts, price):
            return

        # Update last decision state for this symbol
        self.last_decision_state[symbol]["candle_ts"] = candle_ts
        self.last_decision_state[symbol]["price"] = price

        equity_before = compute_equity(self.capital, self.positions)

        # Model decision
        action, conf = self.model_client.predict(features)
        self.log.info(
            f"[{symbol}] model action: {action}, conf={conf:.2f}, price={price:.2f}"
        )

        # Log decision (with sampling for 'hold')
        self._maybe_log_decision(
            symbol=symbol,
            action=action,
            confidence=conf,
            latest_row=latest_row,
            equity_before=equity_before,
            price=price,
            candle_ts=candle_ts,
            hold_conf_margin=self.config.hold_conf_margin,
            hold_sample_every_min=self.config.hold_sample_every_min,
        )

        # Update position price if there is an open position
        self._update_position_price(symbol, price)

        # Risk management via TP/SL
        if self._maybe_close_position_by_pnl(symbol, price):
            self._log_position_status(symbol)
            self._log_equity()
            return

        # Apply core trading logic
        self._apply_trading_logic(symbol, action, conf, latest_row, price)

        self._log_position_status(symbol)
        self._log_equity()


# ---------------------------------------------------------------------- #
# Bootstrap                                                              #
# ---------------------------------------------------------------------- #

def check_if_balance():
    """
    Check the current balance of the account on the exchange.
    """
    exchange = get_binance_client()
    balance = exchange.fetch_balance()

    if settings.TRADING_MODE == "live":
        usdt_balance = float(balance["USDT"]["total"])
        btc_balance = float(balance.get("BTC", {}).get("total", 0.0))
        return usdt_balance, btc_balance
    else:
        return float(settings.BASE_CAPITAL), 0.0

def run_bot_loop() -> None:
    """
    Main entry point to run the trading bot loop.
    """
    usdt_balance, btc_balance = check_if_balance()
    bot = FoundationalTradingBot(
        balance=usdt_balance,
        initial_btc_amount=btc_balance,
        sleep_seconds=300,
        min_confidence=0.51,
    )
    # Get profit (liquidate all positions into USDT) and exit
    # bot.get_profit()
    bot.run()

if __name__ == "__main__":
    run_bot_loop()