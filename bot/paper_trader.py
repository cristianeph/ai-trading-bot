import time
import math
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
            initial_btc_amount: float = 0.0,
    ) -> None:
        self.storage = Storage()
        self.capital: float = balance
        self.positions: Dict[str, Optional[Dict[str, Any]]] = {
            symbol: None for symbol in settings.SYMBOLS
        }
        # If we already have BTC in the account and BTC/USDT is one of the symbols,
        # treat it as an existing long position with entry at the current market price.
        if initial_btc_amount > 0 and "BTC/USDT" in settings.SYMBOLS:
            try:
                ohlcv_init = get_historical_ohlcv("BTC/USDT", settings.TIMEFRAME, limit=1)
                if ohlcv_init:
                    last_candle = ohlcv_init[-1]
                    # OHLCV format: [timestamp, open, high, low, close, volume]
                    entry_price = float(last_candle[4])
                    self.positions["BTC/USDT"] = {
                        "side": "buy",
                        "amount": float(initial_btc_amount),
                        "entry_price": entry_price,
                        "entry_fee_usdt": 0.0,
                        "last_price": entry_price,
                    }
                    print(
                        f"[BTC/USDT] Loaded existing BTC balance as position: "
                        f"amount={initial_btc_amount:.6f}, entry≈{entry_price:.2f}"
                    )
            except Exception as init_exc:  # noqa: BLE001
                print(f"[BTC/USDT] Error initializing existing BTC position: {init_exc}")
        # Capture initial equity (cash + any pre-existing BTC position)
        self.initial_equity: float = compute_equity(self.capital, self.positions)
        # Track last processed candle timestamp and decision price per symbol
        self.last_candle_ts: Dict[str, Optional[Any]] = {
            symbol: None for symbol in settings.SYMBOLS
        }
        self.last_decision_price: Dict[str, Optional[float]] = {
            symbol: None for symbol in settings.SYMBOLS
        }
        self.sleep_seconds = sleep_seconds
        self.min_confidence = min_confidence

        self.tp_pct: float = 0.003   # +0.3% take profit (micro scalping)
        self.sl_pct: float = -0.004  # -0.4% stop loss (micro scalping)

        print(f"[BOT] Inicializado. Capital inicial: {self.capital}, Equity inicial≈{self.initial_equity:.2f} USDT")

    def _fetch_latest_market_state(
            self, symbol: str
    ) -> Optional[tuple[pd.Series, float, list[float]]]:
        """
        Downloads OHLCV data, builds the feature DataFrame and returns
        (latest_row, price, features) or None if something fails.
        """
        try:
            ohlcv = get_historical_ohlcv(symbol, settings.TIMEFRAME, limit=200)
        except Exception as exc:  # noqa: BLE001
            print(f"[{symbol}] Error fetching OHLCV data: {exc}")
            return None

        if not ohlcv:
            print(f"[{symbol}] No OHLCV data received.")
            return None

        try:
            df: pd.DataFrame = build_features_for_symbol(
                ohlcv, symbol, settings.TIMEFRAME
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{symbol}] Error building feature DataFrame: {exc}")
            return None
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
            position_value: Optional[float] = None,
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

        # Allow caller to override position_value (for dynamic sizing)
        if position_value is None:
            position_value = self.capital * settings.POSITION_SIZE_PCT

        if position_value <= 0 or not math.isfinite(position_value):
            print(f"[{symbol}] position_value invalid: {position_value}")
            return

        # Theoretical amount we want to buy
        amount = position_value / price

        # Send order (paper or live according to settings)
        try:
            order = place_order(symbol, "buy", amount)
        except Exception as exc:  # noqa: BLE001
            print(f"[{symbol}] Error placing BUY order: {exc}")
            return

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

        try:
            order = place_order(symbol, "sell", amount)
        except Exception as exc:  # noqa: BLE001
            print(f"[{symbol}] Error placing SELL order: {exc}")
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

        pnl = equity - getattr(self, "initial_equity", equity)
        pnl_pct = (pnl / self.initial_equity * 100.0) if getattr(self, "initial_equity", 0) else 0.0

        print(
            f"[BOT] Current equity (if fully liquidated): {equity:.2f} USDT, "
            f"PnL={pnl:+.2f} USDT ({pnl_pct:+.2f}%)"
        )

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
        # Get candle timestamp (index) to know if we are on the same bar
        candle_ts = getattr(latest_row, "name", None)

        # If this is the same candle as last time, only skip model decision if the move is tiny and we already have a position (micro scalping style).
        if self.last_candle_ts.get(symbol) == candle_ts:
            last_decision_price = self.last_decision_price.get(symbol)
            if last_decision_price is not None and last_decision_price > 0:
                price_change = abs(price - last_decision_price) / last_decision_price
                # Lower threshold for micro scalping (e.g. 0.05%)
                drastic_move_threshold = 0.0005
                current_pos = self.positions.get(symbol)
                if current_pos is not None and price_change < drastic_move_threshold:
                    # Same candle, small move and we already have a position:
                    # skip a new model decision, just update status.
                    print(
                        f"[{symbol}] Skipping model decision: same candle, "
                        f"price_change={price_change:.4%} "
                        f"(<{drastic_move_threshold:.4%})"
                    )
                    self._update_position_price(symbol, price)
                    self._log_position_status(symbol)
                    self._log_equity()
                    return

        # New candle or drastic move: update tracking info
        self.last_candle_ts[symbol] = candle_ts
        self.last_decision_price[symbol] = price

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

        # First, risk management: TP/SL based exit, independent of model action.
        # If a position was closed here, we skip the rest of the logic for this symbol.
        if self._maybe_close_position_by_pnl(symbol, price):
            self._log_position_status(symbol)
            self._log_equity()
            return

        # Trading logic
        if action == "buy":
            # Dynamic position sizing based on recent volatility
            vol_20 = float(latest_row.get("vol_20", 0.0))
            base_pct = settings.POSITION_SIZE_PCT
            risk_factor = 1.0

            if vol_20 > 0:
                # Simple tiered adjustment: higher volatility -> smaller position
                if vol_20 > 0.02:
                    risk_factor = 0.25
                elif vol_20 > 0.01:
                    risk_factor = 0.5
                elif vol_20 < 0.002:
                    risk_factor = 1.2  # slightly larger in very low volatility

            dynamic_position_value = self.capital * base_pct * risk_factor
            # Never exceed available capital
            dynamic_position_value = max(0.0, min(dynamic_position_value, self.capital))

            self._handle_buy(symbol, price, conf, position_value=dynamic_position_value)
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
        usdt_balance = float(balance["USDT"]["total"])
        btc_balance = float(balance.get("BTC", {}).get("total", 0.0))
        return usdt_balance, btc_balance
    else:
        # In paper mode we only care about the starting USDT capital
        return float(settings.BASE_CAPITAL), 0.0


def run_bot_loop() -> None:
    usdt_balance, btc_balance = check_if_balance()
    bot = TradingBot(balance=usdt_balance, initial_btc_amount=btc_balance)
    bot.run()


if __name__ == "__main__":
    print(
        f"[DEBUG CONFIG] BINANCE_TESTNET={settings.BINANCE_TESTNET}, "
        f"TRADING_MODE={settings.TRADING_MODE}"
    )
    run_bot_loop()
