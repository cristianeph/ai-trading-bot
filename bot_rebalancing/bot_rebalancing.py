import math
from typing import Optional, Any, cast

import pandas as pd
from dotenv import load_dotenv

# Load specific env for this bot (if you want a separate config file)
load_dotenv(".env.rebalancing")

from common.logger import BotLogger
from common.config import settings
from common.data_client import place_order, get_binance_client
from common.base_bot import (
    BaseTradingBot,
    Position,
    compute_equity,
)
from common.model_client import ModelClient
from common.storage import Storage
from common.monitoring import init_sentry

init_sentry()

# ------------------------------------------------------------------ #
# Strategy defaults (all overridden by BotConfig when present)       #
# ------------------------------------------------------------------ #

# When the allocation deviation (in % of total equity) is smaller than this,
# no rebalance will be triggered for that symbol.
DEFAULT_REBALANCE_THRESHOLD_PCT: float = 0.02  # 2%

# Maximum fraction of total equity to move in a single rebalance step.
DEFAULT_MAX_TRADE_PCT: float = 0.25  # 25% of equity

# For candle-skip logic (same as foundational, but with different bot_type)
DEFAULT_DRASTIC_MOVE_THRESHOLD: float = 0.0005  # 0.05%

DEFAULT_HOLD_CONF_MARGIN: float = 0.10
DEFAULT_HOLD_SAMPLE_EVERY_MIN: int = 10


class RebalancingTradingBot(BaseTradingBot):
    """
    Portfolio rebalancing bot:

    - Maintains target weights for each symbol (BTC/USDT, ETH/USDT, etc.).
    - Periodically compares current allocation vs. targets.
    - Executes partial BUY/SELL trades to move allocations toward targets.
    - Uses the prediction model (action + confidence) to modulate when to
      rebalance and in which direction, avoiding trades that strongly
      contradict the model.
    """

    def __init__(
        self,
        *,
        sleep_seconds: int = 60,
        min_confidence: float = 0.51,
        balance: float = 0.0,
        storage: Optional[Storage] = None,
        model_client: Optional[ModelClient] = None,
    ) -> None:
        super().__init__(
            sleep_seconds=sleep_seconds,
            min_confidence=min_confidence,
            balance=balance,
            storage=storage,
            model_client=model_client,
            bot_type="rebalancing",
        )

        # Override logger name to make logs easy to filter
        self.log = BotLogger("RebalancingTradingBot")
        self.log.info(
            f"[DEBUG CONFIG] BINANCE_TESTNET={settings.BINANCE_TESTNET}, "
            f"TRADING_MODE={settings.TRADING_MODE}"
        )

        # ------------------------------------------------------------------
        # Load dynamic configuration from BotConfig (bot_type="rebalancing")
        # ------------------------------------------------------------------
        # Same pattern as foundational bot: fallback to defaults if not found.

        # Global trading gate
        self.min_confidence = self.storage.get_bot_config_float(
            "min_confidence",
            default=min_confidence,
        )
        self.sleep_seconds = self.storage.get_bot_config_int(
            "sleep_seconds",
            default=sleep_seconds,
        )

        # Rebalancing parameters
        self.rebalance_threshold_pct = self.storage.get_bot_config_float(
            "rebalance_threshold_pct",
            default=DEFAULT_REBALANCE_THRESHOLD_PCT,
        )
        self.max_trade_pct = self.storage.get_bot_config_float(
            "max_trade_pct",
            default=DEFAULT_MAX_TRADE_PCT,
        )

        # Candle-skip / decision logging sampling
        self.drastic_move_threshold = self.storage.get_bot_config_float(
            "drastic_move_threshold",
            default=DEFAULT_DRASTIC_MOVE_THRESHOLD,
        )
        self.hold_conf_margin = self.storage.get_bot_config_float(
            "hold_conf_margin",
            default=DEFAULT_HOLD_CONF_MARGIN,
        )
        self.hold_sample_every_min = self.storage.get_bot_config_int(
            "hold_sample_every_min",
            default=DEFAULT_HOLD_SAMPLE_EVERY_MIN,
        )

        # Per-symbol target weights and minimum tradable amounts
        self.target_weights: dict[str, float] = {}
        self.min_trade_amount: dict[str, float] = {}

        num_symbols = len(settings.SYMBOLS)
        default_equal_weight = 1.0 / num_symbols if num_symbols > 0 else 0.0

        for symbol in settings.SYMBOLS:
            # Example key in BotConfig: "target_weight.BTC/USDT"
            weight_key = f"target_weight.{symbol}"
            self.target_weights[symbol] = self.storage.get_bot_config_float(
                weight_key,
                default=default_equal_weight,
            )

            # Example key in BotConfig: "min_trade_amount.BTC/USDT"
            min_amount_key = f"min_trade_amount.{symbol}"
            self.min_trade_amount[symbol] = self.storage.get_bot_config_float(
                min_amount_key,
                default=0.00001,
            )

        self._normalize_target_weights()

        self.log.info(
            f"[REBALANCING] Target weights: {self.target_weights}, "
            f"rebalance_threshold_pct={self.rebalance_threshold_pct:.2%}, "
            f"max_trade_pct={self.max_trade_pct:.2%}, "
            f"min_confidence={self.min_confidence:.2f}"
        )

    # ------------------------------------------------------------------ #
    # Config helpers                                                     #
    # ------------------------------------------------------------------ #

    def _normalize_target_weights(self) -> None:
        """
        Normalize target weights so that they sum to 1.0 (if they are not zero).
        """
        total = sum(max(w, 0.0) for w in self.target_weights.values())
        if total <= 0:
            # If misconfigured, keep them as-is and log a warning.
            self.log.error(
                "[REBALANCING] Target weights sum to zero or negative. "
                "Please configure BotConfig.target_weight.* for bot_type='rebalancing'."
            )
            return

        for symbol, w in list(self.target_weights.items()):
            normalized = max(w, 0.0) / total
            self.target_weights[symbol] = normalized

    # ------------------------------------------------------------------ #
    # Core helpers for rebalancing                                       #
    # ------------------------------------------------------------------ #

    def _compute_current_allocation(
        self,
        symbol: str,
        price: float,
        total_equity: float,
    ) -> tuple[float, float, float, float]:
        """
        Compute allocation metrics for a symbol.

        Returns:
            current_value:    current market value of the symbol (USDT)
            current_pct:      current allocation (% of total equity)
            target_pct:       target allocation (% of total equity)
            delta_pct:        target_pct - current_pct
        """
        current_pos = self.positions.get(symbol)
        current_value = 0.0
        if current_pos is not None:
            amount = float(current_pos.get("amount", 0.0))
            current_value = amount * price

        target_pct = self.target_weights.get(symbol, 0.0)
        current_pct = current_value / total_equity if total_equity > 0 else 0.0
        delta_pct = target_pct - current_pct
        return current_value, current_pct, target_pct, delta_pct

    def _should_skip_decision_same_candle(
        self,
        symbol: str,
        candle_ts: Any,
        price: float,
    ) -> bool:
        """
        Same-candle skip logic:
        - If we are still on the same candle.
        - And price has moved less than self.drastic_move_threshold.
        - And there is already an open position.

        Then we skip model + rebalance logic to reduce noise.
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

        if price_change < self.drastic_move_threshold:
            self.log.info(
                f"[{symbol}] Skipping model decision: same candle, "
                f"price_change={price_change:.4%} "
                f"(<{self.drastic_move_threshold:.4%})"
            )
            self._update_position_price(symbol, price)
            self._log_position_status(symbol)
            self._log_equity()
            return True

        return False

    # ------------------------------------------------------------------ #
    # Trade execution helpers (partial BUY/SELL)                         #
    # ------------------------------------------------------------------ #

    def _rebalance_buy(
        self,
        symbol: str,
        price: float,
        trade_value: float,
    ) -> None:
        """
        Execute a partial BUY to increase allocation towards target.
        """
        if trade_value <= 0 or not math.isfinite(trade_value):
            return

        min_amount = self.min_trade_amount.get(symbol, 0.0)
        amount = trade_value / price
        if amount < min_amount:
            self.log.info(
                f"[{symbol}] Skipping BUY: amount {amount:.8f} < "
                f"min_trade_amount {min_amount:.8f}"
            )
            return

        if trade_value > self.capital:
            # Safety: cap by free capital
            trade_value = max(0.0, self.capital)
            amount = trade_value / price
            if amount < min_amount or trade_value <= 0:
                self.log.info(
                    f"[{symbol}] Skipping BUY: not enough capital for minimum trade "
                    f"(capital={self.capital:.2f}, trade_value={trade_value:.2f})"
                )
                return

        try:
            order = place_order(symbol, "buy", amount)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error placing BUY order: {exc}")
            return

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

        total_cost = executed_amount * avg_price + fee_usdt
        self.capital -= total_cost

        # Update in-memory position: we allow multiple partial buys
        current_pos = self.positions.get(symbol)
        if current_pos is None:
            self.positions[symbol] = cast(
                Position,
                {
                    "side": "buy",
                    "amount": executed_amount,
                    "entry_price": avg_price,
                    "entry_fee_usdt": 0.0,  # we fold fees into capital only
                    "last_price": avg_price,
                },
            )
        else:
            prev_amount = float(current_pos.get("amount", 0.0))
            prev_price = float(current_pos.get("entry_price", 0.0))

            new_amount = prev_amount + executed_amount
            if new_amount <= 0:
                self.positions[symbol] = None
            else:
                # Weighted average entry price (fees already discounted from capital)
                new_entry_price = (
                    prev_amount * prev_price + executed_amount * avg_price
                ) / new_amount
                current_pos["amount"] = new_amount
                current_pos["entry_price"] = new_entry_price
                current_pos["last_price"] = avg_price

        # Persist trade
        self.storage.log_trade(
            symbol=symbol,
            side="buy",
            price=avg_price,
            amount=executed_amount,
            mode=settings.TRADING_MODE,
            pnl=0.0,
        )

        self.log.info(
            f"[{symbol}] Rebalance BUY: amount={executed_amount:.6f}, "
            f"avg_price={avg_price:.2f}, cost={total_cost:.2f}, "
            f"capital={self.capital:.2f}"
        )

    def _rebalance_sell(
        self,
        symbol: str,
        price: float,
        trade_value: float,
    ) -> None:
        """
        Execute a partial SELL to decrease allocation towards target.
        """
        current_pos = self.positions.get(symbol)
        if current_pos is None or current_pos.get("side") != "buy":
            self.log.info(f"[{symbol}] Skipping SELL: no open long position.")
            return

        if trade_value <= 0 or not math.isfinite(trade_value):
            return

        amount_available = float(current_pos.get("amount", 0.0))
        if amount_available <= 0:
            self.log.info(f"[{symbol}] Skipping SELL: position amount <= 0.")
            return

        amount = trade_value / price
        # Cap by available amount
        amount = min(amount, amount_available)

        min_amount = self.min_trade_amount.get(symbol, 0.0)
        if amount < min_amount:
            self.log.info(
                f"[{symbol}] Skipping SELL: amount {amount:.8f} < "
                f"min_trade_amount {min_amount:.8f}"
            )
            return

        try:
            order = place_order(symbol, "sell", amount)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error placing SELL order: {exc}")
            return

        if isinstance(order, dict):
            executed_amount = float(order.get("amount") or amount)
            exit_price = float(order.get("average") or order.get("price") or price)
            fee_info = order.get("fee") or {}
            fee_currency = fee_info.get("currency")
            fee_cost = float(fee_info.get("cost") or 0.0)
            exit_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        else:
            executed_amount = amount
            exit_price = price
            exit_fee_usdt = 0.0

        proceeds = executed_amount * exit_price - exit_fee_usdt
        self.capital += proceeds

        prev_amount = float(current_pos.get("amount", 0.0))
        entry_price = float(current_pos.get("entry_price", 0.0))

        # Simple realized PnL for the sold portion (ignoring historical fees in basis)
        cost_sold = executed_amount * entry_price
        pnl_realized = proceeds - cost_sold

        remaining_amount = prev_amount - executed_amount
        if remaining_amount <= 0:
            self.positions[symbol] = None
        else:
            current_pos["amount"] = remaining_amount
            current_pos["last_price"] = exit_price
            # Keep same entry_price for remaining amount (average cost approximation)

        self.storage.log_trade(
            symbol=symbol,
            side="sell",
            price=exit_price,
            amount=executed_amount,
            mode=settings.TRADING_MODE,
            pnl=pnl_realized,
        )

        self.log.info(
            f"[{symbol}] Rebalance SELL: amount={executed_amount:.6f}, "
            f"exit={exit_price:.2f}, proceeds={proceeds:.2f}, "
            f"pnl_realized={pnl_realized:.2f}, capital={self.capital:.2f}"
        )

    # ------------------------------------------------------------------ #
    # Strategy hook implementation                                       #
    # ------------------------------------------------------------------ #

    def _apply_rebalancing_logic(
        self,
        symbol: str,
        latest_row: pd.Series,
        price: float,
        model_action: str,
        model_conf: float,
    ) -> None:
        """
        Core rebalancing logic for a single symbol:

        - Compute current vs target allocation.
        - Decide direction (buy/sell) and trade size.
        - Use model_action + model_conf to block or attenuate trades
          that contradict the model.
        """
        total_equity = compute_equity(self.capital, self.positions)
        if total_equity <= 0:
            self.log.info(
                f"[{symbol}] Skipping rebalance: non-positive equity ({total_equity})."
            )
            return

        (
            current_value,
            current_pct,
            target_pct,
            delta_pct,
        ) = self._compute_current_allocation(symbol, price, total_equity)

        if abs(delta_pct) < self.rebalance_threshold_pct:
            self.log.info(
                f"[{symbol}] Allocation within threshold: "
                f"current={current_pct:.2%}, target={target_pct:.2%}, "
                f"delta={delta_pct:.2%}, threshold={self.rebalance_threshold_pct:.2%}"
            )
            return

        desired_value_change = delta_pct * total_equity
        # Cap trade value by configured max fraction of equity
        max_trade_value = self.max_trade_pct * total_equity
        trade_value = min(abs(desired_value_change), max_trade_value)

        if trade_value <= 0:
            return

        direction = "buy" if desired_value_change > 0 else "sell"

        # ------------------------------------------------------------------
        # Model gating / modulation
        # ------------------------------------------------------------------
        # Very simple rules to start with:
        # - If model strongly contradicts the rebalance direction (conf >= min_confidence),
        #   skip the rebalance for now.
        # - If model is aligned with direction and conf >= min_confidence,
        #   execute full trade_value.
        # - If model is HOLD or low confidence, execute half of trade_value to
        #   still move allocations slowly towards targets.
        # ------------------------------------------------------------------

        if model_action == "buy" and direction == "sell" and model_conf >= self.min_confidence:
            self.log.info(
                f"[{symbol}] Skipping SELL rebalance: model suggests BUY "
                f"with conf={model_conf:.2f} >= min_conf={self.min_confidence:.2f}"
            )
            return

        if model_action == "sell" and direction == "buy" and model_conf >= self.min_confidence:
            self.log.info(
                f"[{symbol}] Skipping BUY rebalance: model suggests SELL "
                f"with conf={model_conf:.2f} >= min_conf={self.min_confidence:.2f}"
            )
            return

        if model_conf < self.min_confidence or model_action == "hold":
            scaled_trade_value = trade_value * 0.5
            self.log.info(
                f"[{symbol}] Model neutral/low confidence (action={model_action}, "
                f"conf={model_conf:.2f}). Executing half trade_value: "
                f"{scaled_trade_value:.2f} USDT (full={trade_value:.2f})."
            )
            trade_value = scaled_trade_value
            if trade_value <= 0:
                return
        else:
            self.log.info(
                f"[{symbol}] Model aligned or not strongly opposing. "
                f"Executing full trade_value: {trade_value:.2f} USDT "
                f"(direction={direction}, action={model_action}, conf={model_conf:.2f})."
            )

        # ------------------------------------------------------------------
        # Execute rebalance trade
        # ------------------------------------------------------------------
        if direction == "buy":
            self._rebalance_buy(symbol, price, trade_value)
        else:
            self._rebalance_sell(symbol, price, trade_value)

    def _process_symbol_decision(
        self,
        *,
        symbol: str,
        latest_row: pd.Series,
        price: float,
        features: list[float],
        candle_ts: Any,
    ) -> None:
        # Skip noisy repeated decisions on same candle with tiny price movement
        if self._should_skip_decision_same_candle(symbol, candle_ts, price):
            return

        # Update last decision state
        self.last_decision_state[symbol]["candle_ts"] = candle_ts
        self.last_decision_state[symbol]["price"] = price

        equity_before = compute_equity(self.capital, self.positions)

        # Model decision
        action, conf = self.model_client.predict(features)
        self.log.info(
            f"[{symbol}] model action: {action}, conf={conf:.2f}, price={price:.2f}"
        )

        # Log decision (sampling for 'hold')
        self._maybe_log_decision(
            symbol=symbol,
            action=action,
            confidence=conf,
            latest_row=latest_row,
            equity_before=equity_before,
            price=price,
            candle_ts=candle_ts,
            hold_conf_margin=self.hold_conf_margin,
            hold_sample_every_min=self.hold_sample_every_min,
        )

        # Update last seen price in the current position (if any)
        self._update_position_price(symbol, price)

        # Apply portfolio rebalancing logic
        self._apply_rebalancing_logic(
            symbol=symbol,
            latest_row=latest_row,
            price=price,
            model_action=action,
            model_conf=conf,
        )

        # Final status + equity snapshot
        self._log_position_status(symbol)
        self._log_equity()


# ---------------------------------------------------------------------- #
# Bootstrap                                                              #
# ---------------------------------------------------------------------- #


def run_bot_loop() -> None:
    exchange = get_binance_client()
    balance = exchange.fetch_balance()

    if settings.TRADING_MODE == "live":
        usdt_balance = float(balance["USDT"]["total"])
    else:
        usdt_balance = float(settings.BASE_CAPITAL)

    bot = RebalancingTradingBot(
        balance=usdt_balance,
        sleep_seconds=60,  # initial hint, overridden by BotConfig if present
        min_confidence=0.51,
    )
    bot.run()


if __name__ == "__main__":
    run_bot_loop()