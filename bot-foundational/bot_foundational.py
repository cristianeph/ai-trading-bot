from dataclasses import dataclass
from typing import Dict, Optional, Any, cast

import math
import pandas as pd

from common.config import settings
from common.logger import BotLogger
from common.data_client import place_order, get_binance_client
from common.base_bot import BaseTradingBot, Position, compute_equity
from common.model_client import ModelClient
from common.storage import Storage

# Assuming these constants are defined somewhere; using values from comments or defaults
DRASTIC_MOVE_THRESHOLD = 0.0005  # Example value
HOLD_CONF_MARGIN = 0.10
HOLD_SAMPLE_EVERY_MIN = 10
MIN_TRADE_AMOUNT = {"BTC/USDT": 0.00001}

@dataclass
class PnL:
    invested_usdt: float
    current_value_usdt: float
    unrealized_pnl_usdt: float
    unrealized_pnl_pct: float


@dataclass
class FoundationalConfig:
    min_confidence: float
    sleep_seconds: int
    tp_pct: float
    sl_pct: float
    drastic_move_threshold: float
    hold_conf_margin: float
    hold_sample_every_min: int
    min_trade_amount: Dict[str, float]
    symbols: list[str]
    bot_type: str

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

        tp_pct = storage.get_bot_config_float("tp_pct", default=0.003)
        sl_pct = storage.get_bot_config_float("sl_pct", default=-0.004)

        drastic_move_threshold = storage.get_bot_config_float(
            "drastic_move_threshold",
            default=DRASTIC_MOVE_THRESHOLD,
        )
        hold_conf_margin = storage.get_bot_config_float(
            "hold_conf_margin",
            default=HOLD_CONF_MARGIN,
        )
        hold_sample_every_min = storage.get_bot_config_int(
            "hold_sample_every_min",
            default=HOLD_SAMPLE_EVERY_MIN,
        )

        min_trade_amount = {
            "BTC/USDT": storage.get_bot_config_float(
                "min_trade_amount.BTC/USDT",
                default=MIN_TRADE_AMOUNT.get("BTC/USDT", 0.00001),
            ),
        }

        return cls(
            min_confidence=min_confidence,
            sleep_seconds=sleep_seconds,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            drastic_move_threshold=drastic_move_threshold,
            hold_conf_margin=hold_conf_margin,
            hold_sample_every_min=hold_sample_every_min,
            min_trade_amount=min_trade_amount,
            symbols=settings.SYMBOLS,
            bot_type="foundational",
        )


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
        return FoundationalConfig.from_storage(
            self.storage,
            default_min_confidence=self._default_min_confidence,
            default_sleep_seconds=self._default_sleep_seconds,
        )

    # ------------------------------------------------------------------ #
    # Strategy-specific helpers                                          #
    # ------------------------------------------------------------------ #

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

    def _compute_unrealized_pnl(
        self,
        amount: float,
        entry_price: float,
        entry_fee_usdt: float,
        current_price: float,
    ) -> PnL:
        """
        Compute unrealized PnL for a long position.

        Returns:
            PnL: invested capital, current value, unrealized PnL in USDT, and percentage.
        """
        invested_usdt = amount * entry_price + entry_fee_usdt
        current_value_usdt = amount * current_price
        unrealized_pnl_usdt = current_value_usdt - invested_usdt
        unrealized_pnl_pct = (
            unrealized_pnl_usdt / invested_usdt if invested_usdt > 0 else 0.0
        )
        return PnL(invested_usdt, current_value_usdt, unrealized_pnl_usdt, unrealized_pnl_pct)

    def _maybe_close_position_by_pnl(self, symbol: str, price: float) -> bool:
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
            position_value = self.capital * settings.POSITION_SIZE_PCT

        if position_value <= 0 or not math.isfinite(position_value):
            self.log.info(f"[{symbol}] position_value invalid: {position_value}")
            return None

        return position_value

    def _execute_buy_order(
        self,
        symbol: str,
        amount: float,
        price: float,
    ) -> tuple[float, float, float]:
        """
        Execute a buy order and return (executed_amount, avg_price, entry_fee_usdt).
        """
        try:
            order = place_order(symbol, "buy", amount)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error placing BUY order: {exc}")
            raise

        if isinstance(order, dict):
            executed_amount = float(order.get("amount") or amount)
            avg_price = float(order.get("average") or order.get("price") or price)
            fee_info = order.get("fee") or {}
            fee_currency = fee_info.get("currency")
            fee_cost = float(fee_info.get("cost") or 0.0)
            entry_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        else:
            executed_amount = amount
            avg_price = price
            entry_fee_usdt = 0.0

        return executed_amount, avg_price, entry_fee_usdt

    def _handle_buy(
        self,
        symbol: str,
        price: float,
        confidence: float,
        position_value: Optional[float] = None,
    ) -> None:
        position_value = self._prepare_position_value_for_long(
            symbol, confidence, position_value
        )
        if position_value is None:
            return

        amount = position_value / price

        try:
            executed_amount, avg_price, entry_fee_usdt = self._execute_buy_order(
                symbol, amount, price
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

    def _execute_sell_order(
        self,
        symbol: str,
        amount: float,
        price: float,
    ) -> tuple[float, float]:
        """
        Execute a sell order and return (exit_price, exit_fee_usdt).
        """
        try:
            order = place_order(symbol, "sell", amount)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error placing SELL order: {exc}")
            raise

        if isinstance(order, dict):
            exit_price = float(order.get("average") or order.get("price") or price)
            fee_info = order.get("fee") or {}
            fee_currency = fee_info.get("currency")
            fee_cost = float(fee_info.get("cost") or 0.0)
            exit_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        else:
            exit_price = price
            exit_fee_usdt = 0.0

        return exit_price, exit_fee_usdt

    def _compute_realized_pnl(
        self,
        amount: float,
        entry_price: float,
        entry_fee_usdt: float,
        exit_price: float,
        exit_fee_usdt: float,
    ) -> tuple[float, float]:
        """
        Compute realized PnL (USDT) and net proceeds for a closed position.
        """
        cost = amount * entry_price + entry_fee_usdt
        proceeds = amount * exit_price
        net_proceeds = proceeds - exit_fee_usdt
        pnl = net_proceeds - cost
        return pnl, net_proceeds

    def _handle_sell(
        self,
        symbol: str,
        price: float,
        confidence: float,
    ) -> None:
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

        try:
            exit_price, exit_fee_usdt = self._execute_sell_order(
                symbol, amount, price
            )
        except Exception:
            return

        pnl, net_proceeds = self._compute_realized_pnl(
            amount=amount,
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
            amount=amount,
            mode=settings.TRADING_MODE,
            pnl=pnl,
        )
        self.log.info(
            f"[{symbol}] Close long: amount={amount:.6f}, "
            f"entry={entry_price:.2f}, exit={exit_price:.2f}, "
            f"pnl={pnl:.2f}, capital={self.capital:.2f}"
        )

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

            dynamic_position_value = self.capital * base_pct * risk_factor
            dynamic_position_value = max(0.0, min(dynamic_position_value, self.capital))

            self._handle_buy(
                symbol,
                price,
                confidence,
                position_value=dynamic_position_value,
            )
        elif action == "sell":
            self._handle_sell(symbol, price, confidence)

    def _process_symbol_decision(
        self,
        *,
        symbol: str,
        latest_row: pd.Series,
        price: float,
        features: list[float],
        candle_ts: Any,
    ) -> None:
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
    exchange = get_binance_client()
    balance = exchange.fetch_balance()

    if settings.TRADING_MODE == "live":
        usdt_balance = float(balance["USDT"]["total"])
        btc_balance = float(balance.get("BTC", {}).get("total", 0.0))
        return usdt_balance, btc_balance
    else:
        return float(settings.BASE_CAPITAL), 0.0

def run_bot_loop() -> None:
    usdt_balance, btc_balance = check_if_balance()
    bot = FoundationalTradingBot(
        balance=usdt_balance,
        initial_btc_amount=btc_balance,
        sleep_seconds=30,
        min_confidence=0.51,
    )
    bot.run()

if __name__ == "__main__":
    run_bot_loop()