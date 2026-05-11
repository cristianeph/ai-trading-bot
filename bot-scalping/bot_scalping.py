from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, cast

import pandas as pd

from common.config import settings
from common.data_client import get_binance_client, place_order
from common.logger import BotLogger
from common.model_client import ModelClient
from common.base_bot import (
    BaseTradingBot,
    Position,
    DecisionState,
)
from common.storage import Storage

# ------------------------------------------------------------------ #
# Scalping-specific parameters                                      #
# ------------------------------------------------------------------ #

# Same-candle price move threshold to skip re-decisions (scalping wants to be reactive)
SCALP_DRASTIC_MOVE_THRESHOLD: float = 0.0001  # 0.01%

# Decision logging behavior for "hold"
SCALP_HOLD_CONF_MARGIN: float = 0.05         # |conf - 0.5| > 0.05 => high-conviction hold
SCALP_HOLD_SAMPLE_EVERY_MIN: int = 2         # sample neutral holds every 2 minutes

# TP/SL for micro scalping (tighter than foundational)
DEFAULT_SCALP_TP_PCT: float = 0.0012         # +0.12% take profit
DEFAULT_SCALP_SL_PCT: float = -0.0008        # -0.08% stop loss

# Position sizing for scalping
DEFAULT_SCALP_POSITION_SIZE_PCT: float = 0.05  # 5% of free capital per trade

# Max number of concurrent positions per symbol (risk control)

MAX_OPEN_POSITIONS_PER_SYMBOL: int = 5

# Minimum tradable amount per symbol (to avoid exchange min-amount errors)
MIN_TRADE_AMOUNT: Dict[str, float] = {
    "BTC/USDT": 0.00001,
}


class ScalpingTradingBot(BaseTradingBot):
    """
    V2 strategy (micro scalping, multiple positions per symbol):

    - Allows several independent long positions per symbol.
    - Each position has its own entry, size and PnL.
    - Tight TP/SL to realize very small moves.
    - Smaller position size per trade, but many trades over time.
    """

    # For this bot, positions[symbol] holds a list of Position dicts
    positions: Dict[str, List[Position]]

    def __init__(
        self,
        *,
        sleep_seconds: int = 30,
        min_confidence: float = 0.52,
        balance: float = 0.0,
        storage: Optional[Storage] = None,
        model_client: Optional[ModelClient] = None,
        tp_pct: float = DEFAULT_SCALP_TP_PCT,
        sl_pct: float = DEFAULT_SCALP_SL_PCT,
    ) -> None:
        # Use BaseTradingBot machinery (fetch, logging, run loop)
        super().__init__(
            sleep_seconds=sleep_seconds,
            min_confidence=min_confidence,
            balance=balance,
            storage=storage,
            model_client=model_client,
            bot_type="scalping",
        )

        # Use a scalping-specific logger name
        self.log = BotLogger("scalping_bot")

        # Override positions: now each symbol has a list[Position]
        self.positions = {symbol: [] for symbol in settings.SYMBOLS}

        # Restore open positions from storage/database
        open_positions = self.storage.get_open_positions(mode=settings.TRADING_MODE)
        for row in open_positions:
            pos = cast(
                Position,
                {
                    "side": row.side,
                    "amount": row.amount,
                    "entry_price": row.entry_price,
                    "entry_fee_usdt": row.entry_fee_usdt,
                    "last_price": row.entry_price,
                    "storage_id": row.id,
                },
            )
            if row.symbol in self.positions:
                self.positions[row.symbol].append(pos)

        # TP/SL for this strategy
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

        # Base position size for scalping (can be overridden in settings)
        self.scalp_position_size_pct: float = getattr(
            settings,
            "SCALPING_POSITION_SIZE_PCT",
            DEFAULT_SCALP_POSITION_SIZE_PCT,
        )

        # Initial equity includes any restored open positions from the DB
        self.initial_equity: float = self._compute_equity()

        self.log.info(
            f"[BOT:scalping] Initialized. Initial capital: {self.capital:.2f}, "
            f"Initial equity≈{self.initial_equity:.2f} USDT, "
            f"TP={self.tp_pct:.3%}, SL={self.sl_pct:.3%}, "
            f"position_size_pct={self.scalp_position_size_pct:.1%}"
        )

    # ------------------------------------------------------------------ #
    # Internal helpers: equity & PnL                                     #
    # ------------------------------------------------------------------ #

    def _compute_equity(self) -> float:
        """
        Equity = free cash + value of all open positions across all symbols.
        """
        equity = self.capital
        for symbol_positions in self.positions.values():
            for pos in symbol_positions:
                amount = float(pos.get("amount", 0.0))
                last_price = float(pos.get("last_price", pos.get("entry_price", 0.0)))
                equity += amount * last_price
        return equity

    @staticmethod
    def _compute_unrealized_pnl(
        *,
        amount: float,
        entry_price: float,
        entry_fee_usdt: float,
        current_price: float,
    ) -> tuple[float, float, float, float]:
        """
        Compute unrealized PnL for a single long position.

        Returns:
            invested_usdt, current_value_usdt, unrealized_pnl_usdt, unrealized_pnl_pct
        """
        invested_usdt = amount * entry_price + entry_fee_usdt
        current_value_usdt = amount * current_price
        unrealized_pnl_usdt = current_value_usdt - invested_usdt
        unrealized_pnl_pct = (
            unrealized_pnl_usdt / invested_usdt if invested_usdt > 0 else 0.0
        )
        return invested_usdt, current_value_usdt, unrealized_pnl_usdt, unrealized_pnl_pct

    # ------------------------------------------------------------------ #
    # Overrides: logging & position updates                              #
    # ------------------------------------------------------------------ #

    def _update_position_price(self, symbol: str, price: float) -> None:
        """
        Update last_price for all open positions in a symbol.
        """
        for pos in self.positions.get(symbol, []):
            pos["last_price"] = price

    def _log_equity(self) -> None:
        """
        Log equity using multi-position view.
        """
        equity = self._compute_equity()
        self.storage.log_equity(equity)

        pnl = equity - self.initial_equity
        base = self.initial_equity or equity
        pnl_pct = (pnl / base * 100.0) if base else 0.0

        self.log.info(
            f"[BOT:scalping] Current equity (if fully liquidated): {equity:.2f} USDT, "
            f"PnL={pnl:+.2f} USDT ({pnl_pct:+.2f}%)"
        )

    def _log_position_status(self, symbol: str) -> None:
        """
        Log an aggregated view of all open positions for a symbol.
        """
        symbol_positions = self.positions.get(symbol, [])
        if not symbol_positions:
            self.log.info(
                f"[{symbol}] No open positions. "
                f"Free capital: {self.capital:.2f} USDT"
            )
            return

        total_amount = 0.0
        total_invested = 0.0
        total_current_value = 0.0

        for pos in symbol_positions:
            amount = float(pos.get("amount", 0.0))
            entry_price = float(pos.get("entry_price", 0.0))
            last_price = float(pos.get("last_price", entry_price))
            entry_fee_usdt = float(pos.get("entry_fee_usdt", 0.0))

            invested, current_value, _, _ = self._compute_unrealized_pnl(
                amount=amount,
                entry_price=entry_price,
                entry_fee_usdt=entry_fee_usdt,
                current_price=last_price,
            )

            total_amount += amount
            total_invested += invested
            total_current_value += current_value

        unrealized_pnl = total_current_value - total_invested

        self.log.info(
            f"[{symbol}] Positions: count={len(symbol_positions)}, "
            f"total_amount={total_amount:.6f}, "
            f"invested≈{total_invested:.2f} USDT, "
            f"current value≈{total_current_value:.2f} USDT, "
            f"unrealized PnL≈{unrealized_pnl:.2f} USDT, "
            f"free capital={self.capital:.2f} USDT"
        )

    # ------------------------------------------------------------------ #
    # Order execution helpers                                            #
    # ------------------------------------------------------------------ #

    def _execute_buy_order(
        self,
        symbol: str,
        desired_value_usdt: float,
        price: float,
    ) -> Optional[Position]:
        """
        Execute a buy order for a given notional value and return a Position object
        representing the filled trade, or None if it fails.
        """
        if desired_value_usdt <= 0 or not math.isfinite(desired_value_usdt):
            self.log.info(f"[{symbol}] Invalid desired position value: {desired_value_usdt}")
            return None

        if desired_value_usdt > self.capital:
            self.log.info(
                f"[{symbol}] Not enough capital: desired={desired_value_usdt:.2f}, "
                f"free={self.capital:.2f}"
            )
            return None

        amount = desired_value_usdt / price
        min_amount = MIN_TRADE_AMOUNT.get(symbol)
        if min_amount is not None and amount < min_amount:
            self.log.info(
                f"[{symbol}] Computed amount {amount:.8f} is below minimum tradable amount "
                f"{min_amount:.8f}. Skipping BUY."
            )
            return None

        try:
            order = place_order(symbol, "buy", amount)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error placing BUY order: {exc}")
            return None

        if isinstance(order, dict):
            executed_amount = float(order.get("amount") or amount)
            avg_price = float(order.get("average") or order.get("price") or price)
            cost = float(order.get("cost") or (executed_amount * avg_price))
            fee_info = order.get("fee") or {}
            fee_currency = fee_info.get("currency")
            fee_cost = float(fee_info.get("cost") or 0.0)
            entry_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        else:
            executed_amount = amount
            avg_price = price
            cost = executed_amount * avg_price
            entry_fee_usdt = 0.0

        total_debit = cost + entry_fee_usdt
        self.capital -= total_debit

        pos = cast(
            Position,
            {
                "side": "buy",
                "amount": executed_amount,
                "entry_price": avg_price,
                "entry_fee_usdt": entry_fee_usdt,
                "last_price": avg_price,
            },
        )

        storage_id = self.storage.add_open_position(
            symbol=symbol,
            side="buy",
            amount=executed_amount,
            entry_price=avg_price,
            entry_fee_usdt=entry_fee_usdt,
            mode=settings.TRADING_MODE,
        )
        pos["storage_id"] = storage_id

        self._log_trade(
            symbol=symbol,
            side="buy",
            price=avg_price,
            amount=executed_amount,
            pnl=0.0,
            fee_usdt=entry_fee_usdt,
        )

        self.log.info(
            f"[{symbol}] Open long (scalp): amount={executed_amount:.6f}, "
            f"entry={avg_price:.2f}, debit={total_debit:.2f}, "
            f"remaining capital={self.capital:.2f}"
        )

        return pos

    def _execute_sell_order(
        self,
        symbol: str,
        pos: Position,
        price: float,
    ) -> None:
        """
        Execute a sell order for a given position, compute realized PnL and
        update capital and storage.
        """
        amount = float(pos["amount"])
        entry_price = float(pos["entry_price"])
        entry_fee_usdt = float(pos.get("entry_fee_usdt", 0.0))

        try:
            order = place_order(symbol, "sell", amount)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error placing SELL order: {exc}")
            return

        if isinstance(order, dict):
            exit_price = float(order.get("average") or order.get("price") or price)
            proceeds = float(order.get("cost") or (amount * exit_price))
            fee_info = order.get("fee") or {}
            fee_currency = fee_info.get("currency")
            fee_cost = float(fee_info.get("cost") or 0.0)
            exit_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        else:
            exit_price = price
            proceeds = amount * exit_price
            exit_fee_usdt = 0.0

        cost = amount * entry_price + entry_fee_usdt
        net_proceeds = proceeds - exit_fee_usdt
        pnl = net_proceeds - cost

        self.capital += net_proceeds

        self._log_trade(
            symbol=symbol,
            side="sell",
            price=exit_price,
            amount=amount,
            pnl=pnl,
            fee_usdt=exit_fee_usdt,
        )

        storage_id = cast(Optional[int], pos.get("storage_id"))
        if storage_id is not None:
            self.storage.remove_open_position(storage_id)

        self.log.info(
            f"[{symbol}] Close long (scalp): amount={amount:.6f}, "
            f"entry={entry_price:.2f}, exit={exit_price:.2f}, "
            f"pnl={pnl:.2f}, capital={self.capital:.2f}"
        )

    # ------------------------------------------------------------------ #
    # Risk management: TP/SL for multiple positions                      #
    # ------------------------------------------------------------------ #

    def _maybe_close_positions_by_pnl(self, symbol: str, price: float) -> None:
        """
        Iterate over all open positions for a symbol and close those that hit TP or SL.
        """
        symbol_positions = self.positions.get(symbol, [])
        if not symbol_positions:
            return

        remaining_positions: List[Position] = []

        for pos in symbol_positions:
            amount = float(pos.get("amount", 0.0))
            entry_price = float(pos.get("entry_price", 0.0))
            entry_fee_usdt = float(pos.get("entry_fee_usdt", 0.0))

            if amount <= 0 or entry_price <= 0:
                continue

            _, _, _, unrealized_pnl_pct = self._compute_unrealized_pnl(
                amount=amount,
                entry_price=entry_price,
                entry_fee_usdt=entry_fee_usdt,
                current_price=price,
            )

            if unrealized_pnl_pct >= self.tp_pct:
                self.log.info(
                    f"[{symbol}] TP reached ({unrealized_pnl_pct:.3%}) "
                    f"for one position. Closing it."
                )
                self._execute_sell_order(symbol, pos, price)
            elif unrealized_pnl_pct <= self.sl_pct:
                self.log.info(
                    f"[{symbol}] SL reached ({unrealized_pnl_pct:.3%}) "
                    f"for one position. Closing it."
                )
                self._execute_sell_order(symbol, pos, price)
            else:
                remaining_positions.append(pos)

        self.positions[symbol] = remaining_positions

    # ------------------------------------------------------------------ #
    # Core trading logic                                                 #
    # ------------------------------------------------------------------ #

    def _should_skip_decision_same_candle(
        self,
        symbol: str,
        candle_ts: Any,
        price: float,
    ) -> bool:
        """
        Decide whether to skip a model decision on the same candle.

        For scalping:
          - we still skip when price barely moved AND there are open positions,
          - but with a smaller threshold than the foundational bot.
        """
        last_state: DecisionState = self.last_decision_state.get(
            symbol, {"candle_ts": None, "price": None}
        )
        last_candle_ts = last_state.get("candle_ts")
        last_decision_price = last_state.get("price")

        if last_candle_ts != candle_ts:
            return False

        if last_decision_price is None or last_decision_price <= 0:
            return False

        price_change = abs(price - last_decision_price) / last_decision_price
        symbol_positions = self.positions.get(symbol, [])

        if not symbol_positions:
            # No open positions -> be more reactive, do not skip
            return False

        if price_change < SCALP_DRASTIC_MOVE_THRESHOLD:
            self.log.info(
                f"[{symbol}] Skipping model decision (scalp): same candle, "
                f"price_change={price_change:.4%} "
                f"(<{SCALP_DRASTIC_MOVE_THRESHOLD:.4%})"
            )
            self._update_position_price(symbol, price)
            self._log_position_status(symbol)
            self._log_equity()
            return True

        return False

    def _open_new_position(
        self,
        symbol: str,
        confidence: float,
        latest_row: pd.Series,
        price: float,
    ) -> None:
        """
        Handle a BUY decision: open a new small position if allowed.
        """
        if confidence < self.min_confidence:
            return

        symbol_positions = self.positions.get(symbol, [])

        if len(symbol_positions) >= MAX_OPEN_POSITIONS_PER_SYMBOL:
            self.log.info(
                f"[{symbol}] Max open positions reached "
                f"({MAX_OPEN_POSITIONS_PER_SYMBOL}). Skipping new BUY."
            )
            return

        vol_20 = float(latest_row.get("vol_20", 0.0))
        base_pct = self.scalp_position_size_pct
        risk_factor = 1.0

        if vol_20 > 0:
            # Simple tiered adjustment: higher volatility -> smaller position
            if vol_20 > 0.02:
                risk_factor = 0.25
            elif vol_20 > 0.01:
                risk_factor = 0.5
            elif vol_20 < 0.002:
                risk_factor = 1.3  # slightly more aggressive in very low volatility

        desired_value = self.capital * base_pct * risk_factor
        desired_value = max(0.0, min(desired_value, self.capital))

        pos = self._execute_buy_order(symbol, desired_value, price)
        if pos is not None:
            symbol_positions.append(pos)
            self.positions[symbol] = symbol_positions

    def _close_all_positions_for_symbol(self, symbol: str, price: float) -> None:
        """
        Close all open positions for a symbol (used when model says 'sell').
        """
        symbol_positions = self.positions.get(symbol, [])
        if not symbol_positions:
            return

        total_unrealized_pnl_usdt = 0.0
        per_position_pnl: List[tuple[Position, float]] = []

        for pos in symbol_positions:
            amount = float(pos.get("amount", 0.0))
            entry_price = float(pos.get("entry_price", 0.0))
            entry_fee_usdt = float(pos.get("entry_fee_usdt", 0.0))

            if amount <= 0 or entry_price <= 0:
                continue

            _, _, unrealized_pnl_usdt, _ = self._compute_unrealized_pnl(
                amount=amount,
                entry_price=entry_price,
                entry_fee_usdt=entry_fee_usdt,
                current_price=price,
            )
            total_unrealized_pnl_usdt += unrealized_pnl_usdt
            per_position_pnl.append((pos, unrealized_pnl_usdt))

        # If after skipping invalid positions there is nothing to act on, just return
        if not per_position_pnl:
            return

        if total_unrealized_pnl_usdt > 0:
            # Net profit across all positions: close everything and realize it.
            self.log.info(
                f"[{symbol}] Model SELL signal: closing ALL positions "
                f"(aggregated unrealized PnL≈{total_unrealized_pnl_usdt:.4f} USDT > 0)."
            )
            for pos, _ in per_position_pnl:
                self._execute_sell_order(symbol, pos, price)
            self.positions[symbol] = []
        else:
            # Net loss across all positions: close only individually profitable ones,
            # keep losing trades open for TP/SL management.
            self.log.info(
                f"[{symbol}] Model SELL signal: net unrealized PnL≈{total_unrealized_pnl_usdt:.4f} USDT ≤ 0. "
                f"Closing only profitable positions and keeping losing ones open."
            )
            remaining_positions: List[Position] = []
            for pos, pnl_usdt in per_position_pnl:
                if pnl_usdt > 0:
                    self._execute_sell_order(symbol, pos, price)
                else:
                    remaining_positions.append(pos)

            self.positions[symbol] = remaining_positions

    # ------------------------------------------------------------------ #
    # Strategy hook implementation                                      #
    # ------------------------------------------------------------------ #

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
        Main decision pipeline for the scalping strategy.
        """
        # Same-candle micro-move logic
        if self._should_skip_decision_same_candle(symbol, candle_ts, price):
            return

        # Update decision state
        self.last_decision_state[symbol]["candle_ts"] = candle_ts
        self.last_decision_state[symbol]["price"] = price

        equity_before = self._compute_equity()

        # Model decision
        action, conf = self.model_client.predict(features)
        self.log.info(
            f"[{symbol}] [SCALP] model action: {action}, conf={conf:.2f}, price={price:.2f}"
        )

        # Log decision with scalping-specific sampling params
        self._maybe_log_decision(
            symbol=symbol,
            action=action,
            confidence=conf,
            latest_row=latest_row,
            equity_before=equity_before,
            price=price,
            candle_ts=candle_ts,
            hold_conf_margin=SCALP_HOLD_CONF_MARGIN,
            hold_sample_every_min=SCALP_HOLD_SAMPLE_EVERY_MIN,
        )

        # Update last price for all positions
        self._update_position_price(symbol, price)

        # TP/SL risk management on a per-position basis
        self._maybe_close_positions_by_pnl(symbol, price)

        # Apply core trading logic
        if action == "buy":
            self._open_new_position(symbol, conf, latest_row, price)
        elif action == "sell":
            self._close_all_positions_for_symbol(symbol, price)

        # Log current status
        self._log_position_status(symbol)
        self._log_equity()


# ---------------------------------------------------------------------- #
# Helpers to run this bot                                                #
# ---------------------------------------------------------------------- #


def _check_live_balances() -> float:
    """
    Fetch USDT balance from the exchange when trading live.
    For the scalper we only care about free USDT; we do not preload BTC positions.
    """
    exchange = get_binance_client()
    balance = exchange.fetch_balance()
    usdt_balance = float(balance["USDT"]["total"])
    return usdt_balance


def run_scalping_bot_loop() -> None:
    """
    Helper to run the scalping bot:
      - In live mode: use real USDT balance from the exchange.
      - In paper mode: use BASE_CAPITAL.
    """
    if settings.TRADING_MODE == "live":
        usdt_balance = _check_live_balances()
    else:
        usdt_balance = float(settings.BASE_CAPITAL)

    bot = ScalpingTradingBot(
        balance=usdt_balance,
        sleep_seconds=30,
        min_confidence=0.52,
    )
    bot.run()


if __name__ == "__main__":
    run_scalping_bot_loop()