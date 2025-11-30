import time
from typing import Dict, Optional, Any

import requests
import pandas as pd

from common.config import settings
from common.data_client import get_historical_ohlcv, place_order, get_binance_client
from bot.features import build_features_for_symbol
from bot.storage import Storage

MODEL_URL = "http://localhost:8000/predict"  # service name in docker-compose


def get_model_action(features: list[float]) -> tuple[str, float]:
    """
    Sends the feature vector to the model microservice and returns
    (action, confidence).

    action: "buy" | "sell" | "hold"
    confidence: probability associated with the chosen action.
    """
    payload = {"features": features}
    try:
        resp = requests.post(MODEL_URL, json=payload, timeout=2)
        resp.raise_for_status()
    except requests.RequestException as exc:
        # In an MVP we prefer not to break the loop; return "hold"
        print(f"[MODEL] Error querying the model: {exc}")
        return "hold", 0.0

    data = resp.json()
    action = data.get("action", "hold")
    confidence = float(data.get("confidence", 0.0))
    return action, confidence


def compute_equity(cash: float, positions: Dict[str, Optional[Dict[str, Any]]]) -> float:
    """
    Equity = cash + value of all open positions.

    Each position is a dict with:
      - amount
      - last_price
    """
    equity = cash
    for pos in positions.values():
        if pos is not None:
            amount = pos.get("amount", 0.0)
            last_price = pos.get("last_price", 0.0)
            equity += amount * last_price
    return equity


class TradingBot:
    """
    Main trading bot.

    Encapsulates:
      - capital (cash),
      - positions per symbol,
      - access to storage,
      - the main execution loop.
    """

    def __init__(
            self,
            sleep_seconds: int = 30,
            min_confidence: float = 0.52,
            balance: float = 0,
    ) -> None:
        self.storage = Storage()
        self.capital: float = balance
        self.positions: Dict[str, Optional[Dict[str, Any]]] = {
            symbol: None for symbol in settings.SYMBOLS
        }
        self.sleep_seconds = sleep_seconds
        self.min_confidence = min_confidence

        self.tp_pct: float = 0.005   # +0.5% take profit
        self.sl_pct: float = -0.01   # -1.0% stop loss


        print(f"[BOT] Inicializado. Capital inicial: {self.capital}")

    def _fetch_latest_market_state(
            self, symbol: str
    ) -> Optional[tuple[pd.Series, float, list[float]]]:
        """
        Downloads OHLCV data, builds the feature DataFrame and returns
        (latest_row, price, features) or None if something fails.
        """
        ohlcv = get_historical_ohlcv(symbol, settings.TIMEFRAME, limit=200)
        if not ohlcv:
            print(f"[{symbol}] No OHLCV data received.")
            return None

        df: pd.DataFrame = build_features_for_symbol(
            ohlcv, symbol, settings.TIMEFRAME
        )
        if df.empty:
            print(f"[{symbol}] Feature DataFrame is empty.")
            return None

        latest_row = df.iloc[-1]
        features = latest_row[["ma_ratio", "rsi_14", "vol_20"]].tolist()
        price: float = float(latest_row["close"])

        return latest_row, price, features

    def _update_position_price(self, symbol: str, price: float) -> None:
        """
        Updates the last known price of the position for a symbol, if it exists.
        """
        current_pos = self.positions.get(symbol)
        if current_pos is not None:
            current_pos["last_price"] = price

    def _maybe_close_position_by_pnl(self, symbol: str, price: float) -> bool:
        """
        Checks the unrealized PnL of the position and, if it crosses the
        take profit (tp_pct) or stop loss (sl_pct) thresholds, forces a close (SELL).

        Returns True if the position was closed, False otherwise.
        """
        current_pos = self.positions.get(symbol)
        if current_pos is None or current_pos.get("side") != "buy":
            return False

        amount = float(current_pos.get("amount", 0.0))
        entry_price = float(current_pos.get("entry_price", 0.0))
        entry_fee_usdt = float(current_pos.get("entry_fee_usdt", 0.0))

        if amount <= 0 or entry_price <= 0:
            return False

        invested_usdt = amount * entry_price + entry_fee_usdt
        current_value_usdt = amount * price
        unrealized_pnl_usdt = current_value_usdt - invested_usdt
        unrealized_pnl_pct = (
            unrealized_pnl_usdt / invested_usdt if invested_usdt > 0 else 0.0
        )

        # Take profit
        if unrealized_pnl_pct >= self.tp_pct:
            print(
                f"[{symbol}] TP reached ({unrealized_pnl_pct:.3%}), "
                f"forcing SELL for risk management."
            )
            # Force a sell with confidence 1.0 (bypass the model)
            self._handle_sell(symbol, price, confidence=1.0)
            return True

        # Stop loss
        if unrealized_pnl_pct <= self.sl_pct:
            print(
                f"[{symbol}] SL reached ({unrealized_pnl_pct:.3%}), "
                f"forcing SELL for risk management."
            )
            self._handle_sell(symbol, price, confidence=1.0)
            return True

        return False

    def _handle_buy(
            self,
            symbol: str,
            price: float,
            confidence: float,
    ) -> None:
        """
        Attempts to open a long position if one does not already exist for the symbol.
        """
        if confidence <= self.min_confidence:
            return

        current_pos = self.positions.get(symbol)
        if current_pos is not None:
            # There is already an open position, do not open another one
            return

        position_value = self.capital * settings.POSITION_SIZE_PCT
        if position_value <= 0:
            print(f"[{symbol}] position_value invalid: {position_value}")
            return

        # Theoretical amount we want to buy
        amount = position_value / price

        # Send order (paper or live according to settings)
        order = place_order(symbol, "buy", amount)

        # Determine actual execution details (if available)
        if isinstance(order, dict):
            executed_amount = float(order.get("amount") or amount)
            avg_price = float(order.get("average") or order.get("price") or price)
            cost = float(order.get("cost") or (executed_amount * avg_price))
            fee_info = order.get("fee") or {}
            fee_currency = fee_info.get("currency")
            fee_cost = float(fee_info.get("cost") or 0.0)
            # For the MVP we only subtract the fee if it is in USDT
            entry_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        else:
            executed_amount = amount
            avg_price = price
            cost = executed_amount * avg_price
            entry_fee_usdt = 0.0

        # Reduce capital by the actual operation cost + fee in USDT
        total_debit = cost + entry_fee_usdt
        self.capital -= total_debit

        self.positions[symbol] = {
            "side": "buy",
            "amount": executed_amount,
            "entry_price": avg_price,
            "entry_fee_usdt": entry_fee_usdt,
            "last_price": avg_price,
        }

        self.storage.log_trade(
            symbol=symbol,
            side="buy",
            price=avg_price,
            amount=executed_amount,
            mode=settings.TRADING_MODE,
            pnl=0.0,
        )
        print(
            f"[{symbol}] Apertura long: amount={executed_amount:.6f}, "
            f"entry={avg_price:.2f}, capital={self.capital:.2f}"
        )

    def _handle_sell(
            self,
            symbol: str,
            price: float,
            confidence: float,
    ) -> None:
        """
        Attempts to close an existing long position.
        """
        if confidence <= self.min_confidence:
            return

        current_pos = self.positions.get(symbol)
        if current_pos is None or current_pos.get("side") != "buy":
            # No long position to close
            return

        amount = float(current_pos["amount"])
        entry_price = float(current_pos["entry_price"])
        entry_fee_usdt = float(current_pos.get("entry_fee_usdt", 0.0))

        order = place_order(symbol, "sell", amount)

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

        # Total entry cost (including entry fee)
        cost = amount * entry_price + entry_fee_usdt

        # Recover capital net of exit fees
        net_proceeds = proceeds - exit_fee_usdt
        pnl = net_proceeds - cost
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
        print(
            f"[{symbol}] Cierre long: amount={amount:.6f}, "
            f"entry={entry_price:.2f}, exit={exit_price:.2f}, "
            f"pnl={pnl:.2f}, capital={self.capital:.2f}"
        )

    def _log_equity(self) -> None:
        """
        Computes and records the current equity.
        """
        equity = compute_equity(self.capital, self.positions)
        self.storage.log_equity(equity)
        print(f"[BOT] Current equity: {equity:.2f}")

    def _log_position_status(self, symbol: str) -> None:
        """
        Prints a human-readable summary of the position state for a symbol:
          - free capital in USDT,
          - BTC invested (if any) and its current value in USDT,
          - unrealized PnL of that position.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            print(
                f"[{symbol}] No open position. "
                f"Free capital: {self.capital:.2f} USDT"
            )
            return

        amount = float(pos.get("amount", 0.0))
        entry_price = float(pos.get("entry_price", 0.0))
        last_price = float(pos.get("last_price", entry_price))
        entry_fee_usdt = float(pos.get("entry_fee_usdt", 0.0))

        invested_usdt = amount * entry_price + entry_fee_usdt
        current_value_usdt = amount * last_price
        unrealized_pnl = current_value_usdt - invested_usdt

        print(
            f"[{symbol}] Position: {amount:.6f} BTC, "
            f"invested≈{invested_usdt:.2f} USDT, "
            f"current value≈{current_value_usdt:.2f} USDT, "
            f"unrealized PnL≈{unrealized_pnl:.2f} USDT, "
            f"free capital={self.capital:.2f} USDT"
        )


    def _maybe_log_decision(
            self,
            symbol: str,
            action: str,
            confidence: float,
            latest_row: pd.Series,
            equity_before: float,
    ) -> None:
        """
        Log model decisions with sampling for 'hold':
          - always log 'buy' and 'sell'
          - for 'hold':
              * log if confidence is far from 0.5 (high-conviction hold)
              * or periodically (every N minutes) to avoid excessive volume
        """
        # Always log buy/sell decisions
        should_log = action in ("buy", "sell")

        # Sampling strategy for 'hold'
        if action == "hold":
            hold_conf_margin = 0.10  # high-conviction hold if |conf - 0.5| > 0.10
            hold_sample_every = 10  # log 1 out of 10 minutes as background context

            if abs(confidence - 0.5) > hold_conf_margin:
                should_log = True
            else:
                current_minute = int(time.time() // 60)
                if current_minute % hold_sample_every == 0:
                    should_log = True

        if not should_log:
            return

        ma_ratio = float(latest_row.get("ma_ratio", 0.0))
        rsi_14 = float(latest_row.get("rsi_14", 0.0))
        vol_20 = float(latest_row.get("vol_20", 0.0))

        self.storage.log_decision(
            symbol=symbol,
            action=action,
            confidence=confidence,
            ma_ratio=ma_ratio,
            rsi_14=rsi_14,
            vol_20=vol_20,
            mode=settings.TRADING_MODE,
            equity_before=equity_before,
        )

    def _process_symbol(self, symbol: str) -> None:
        """
        Executes a full cycle for a given symbol:
          - fetch market data,
          - query the model,
          - update the position,
          - record equity.
        """
        market_state = self._fetch_latest_market_state(symbol)
        if market_state is None:
            return

        latest_row, price, features = market_state
        equity_before = compute_equity(self.capital, self.positions)

        # Model action
        action, conf = get_model_action(features)
        print(
            f"[{symbol}] model action: {action}, conf={conf:.2f}, price={price:.2f}"
        )

        # Log decision (with sampling for 'hold') before mutating capital/positions
        self._maybe_log_decision(symbol, action, conf, latest_row, equity_before)

        # Update last position price (if exists)
        self._update_position_price(symbol, price)

        # Trading logic
        if action == "buy":
            self._handle_buy(symbol, price, conf)
        elif action == "sell":
            self._handle_sell(symbol, price, conf)

        # Record equity after processing the symbol
        self._log_position_status(symbol)
        self._log_equity()

    def run(self) -> None:
        """
        Main bot loop. Iterates over symbols defined in settings.SYMBOLS
        and runs the trading logic at intervals defined by self.sleep_seconds.
        """
        print("[BOT] Starting trading loop...")
        try:
            while True:
                for symbol in settings.SYMBOLS:
                    try:
                        self._process_symbol(symbol)
                    except Exception as symbol_exc:  # noqa: BLE001
                        # We do not want a single symbol to break the whole loop
                        print(f"[{symbol}] Error in symbol loop: {symbol_exc}")

                # Wait for the next cycle (e.g. every few seconds/minutes)
                time.sleep(self.sleep_seconds)

        except KeyboardInterrupt:
            print("[BOT] Keyboard interrupt. Shutting down bot...")

        finally:
            try:
                self.storage.close()
            except Exception:
                pass
            print("[BOT] Bot stopped cleanly.")


def check_if_balance():
    exchange = get_binance_client()
    balance = exchange.fetch_balance()

    if settings.TRADING_MODE == "live":
        return balance['USDT']['total']
    else:
        return settings.BASE_CAPITAL


def run_bot_loop() -> None:

    actual_balance = check_if_balance()
    bot = TradingBot(balance=actual_balance)
    bot.run()


if __name__ == "__main__":
    print(
        f"[DEBUG CONFIG] BINANCE_TESTNET={settings.BINANCE_TESTNET}, "
        f"TRADING_MODE={settings.TRADING_MODE}"
    )
    run_bot_loop()
