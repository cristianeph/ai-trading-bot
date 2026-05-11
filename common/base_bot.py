from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional, Any, TypedDict

import pandas as pd

from common.config import settings
from common.data_client import get_historical_ohlcv
from common.model_client import ModelClient
from common.logger import BotLogger
from common.features import build_features_for_symbol
from common.storage import Storage


class Position(TypedDict, total=False):
    side: str
    amount: float
    entry_price: float
    entry_fee_usdt: float
    last_price: float


class DecisionState(TypedDict, total=False):
    candle_ts: Any
    price: Optional[float]


@dataclass
class PnL:
    invested_usdt: float
    current_value_usdt: float
    unrealized_pnl_usdt: float
    unrealized_pnl_pct: float


def compute_equity(cash: float, positions: Dict[str, Optional[Position]]) -> float:
    """
    Equity = cash + valor de todas las posiciones abiertas.
    """
    equity = cash
    for pos in positions.values():
        if pos is not None:
            amount = pos.get("amount", 0.0)
            last_price = pos.get("last_price", 0.0)
            equity += amount * last_price
    return equity


class BaseTradingBot(ABC):
    """
    Motor base para bots de trading.

    Implementa:
      - tracking de capital y posiciones,
      - almacenamiento en DB (Storage),
      - fetch de mercado + features,
      - logging de equity y decisiones,
      - bucle principal `run`.

    Las estrategias concretas (foundational, scalping...) heredan de aquí
    e implementan `_process_symbol_decision(...)`.
    """

    def __init__(
        self,
        *,
        balance: float = 0.0,
        storage: Optional[Storage] = None,
        model_client: Optional[ModelClient] = None,
        bot_type: str = "base",
    ) -> None:
        self.bot_type = bot_type
        self.log = BotLogger(self.__class__.__name__)
        self.storage = storage or Storage(bot_type=bot_type)
        self.model_client = model_client or ModelClient(settings.MODEL_URL)

        self.config = self.load_config()

        self.symbols = self.config.symbols

        self.capital: float = balance
        self.positions: Dict[str, Optional[Position]] = {
            symbol: None for symbol in self.symbols
        }

        # equity inicial
        self.initial_equity: float = compute_equity(self.capital, self.positions)

        # track del último candle y precio de decisión por símbolo
        self.last_decision_state: Dict[str, DecisionState] = {
            symbol: {"candle_ts": None, "price": None}
            for symbol in self.symbols
        }

        self.log.info(
            f"[BOT:{bot_type}] Inicializado. Capital inicial: {self.capital}, "
            f"Equity inicial≈{self.initial_equity:.2f} USDT"
        )

    @abstractmethod
    def load_config(self) -> Any:
        """
        Cada estrategia debe implementar la carga de su configuración desde Storage.
        """
        raise NotImplementedError

    @abstractmethod
    def _liquidate_position_to_base(self, symbol: str, price: float) -> None:
        """Liquida (vende) una posición abierta de `symbol` hacia la moneda base (USDT).

        La estrategia concreta define cómo ejecutar el SELL (market/limit), fees, logging, etc.
        """
        raise NotImplementedError

    def _fetch_latest_market_state(
        self, symbol: str
    ) -> Optional[tuple[pd.Series, float, list[float]]]:
        """
        Descarga OHLCV, construye features y devuelve
        (latest_row, price, features) o None si falla algo.
        """
        try:
            ohlcv = get_historical_ohlcv(symbol, settings.TIMEFRAME, limit=200)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error fetching OHLCV data: {exc}")
            return None

        if not ohlcv:
            self.log.info(f"[{symbol}] No OHLCV data received.")
            return None

        try:
            df: pd.DataFrame = build_features_for_symbol(
                ohlcv, symbol, settings.TIMEFRAME
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error building feature DataFrame: {exc}")
            return None

        if df.empty:
            self.log.info(f"[{symbol}] Feature DataFrame is empty.")
            return None

        latest_row = df.iloc[-1]
        features = latest_row[settings.FEATURE_COLUMNS].tolist()
        price: float = float(latest_row["close"])

        return latest_row, price, features

    def _update_position_price(self, symbol: str, price: float) -> None:
        current_pos = self.positions.get(symbol)
        if current_pos is not None:
            current_pos["last_price"] = price

    def _check_max_drawdown(self) -> None:
        """
        Implement a maximum drawdown circuit breaker to stop the bot if losses
        exceed a certain threshold.
        """
        # Fetch threshold from config, default to something safe if not set
        max_drawdown_pct = self.storage.get_bot_config_float("max_drawdown_pct", 0.1)

        equity = compute_equity(self.capital, self.positions)
        drawdown_pct = (self.initial_equity - equity) / self.initial_equity if self.initial_equity > 0 else 0.0

        if drawdown_pct >= max_drawdown_pct:
            self.log.error(
                f"[CIRCUIT BREAKER] Max Drawdown reached: {drawdown_pct:.2%}. "
                f"Threshold: {max_drawdown_pct:.2%}. "
                f"Initial Equity: {self.initial_equity:.2f}, Current Equity: {equity:.2f}. "
                f"Stopping bot."
            )
            # Liquidate all positions and exit
            try:
                self.liquidate_all_positions_to_base()
            except Exception as e:
                self.log.error(f"Failed to liquidate all positions during emergency stop: {e}")
            raise SystemExit(f"Max Drawdown circuit breaker triggered: {drawdown_pct:.2%}")

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
        return PnL(
            invested_usdt, current_value_usdt, unrealized_pnl_usdt, unrealized_pnl_pct
        )

    def _compute_realized_pnl(
        self,
        amount: float,
        entry_price: float,
        entry_fee_usdt: float,
        exit_price: float,
        exit_fee_usdt: float,
    ) -> tuple[float, float]:
        """
        Calcula el PnL realizado y los ingresos netos tras una venta.
        """
        invested_usdt = amount * entry_price + entry_fee_usdt
        revenue_usdt = amount * exit_price - exit_fee_usdt
        pnl = revenue_usdt - invested_usdt
        return pnl, revenue_usdt

    def _log_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        amount: float,
        pnl: Optional[float] = None,
        reference_id: Optional[str] = None,
        fee_usdt: float = 0.0,
    ) -> None:
        """
        Helper to log trades with rich metadata.
        Calculates usdt_rate automatically.
        """
        # For simplicity, if we are trading BTC/USDT, the price IS the usdt_rate.
        # For more complex pairs (e.g. BTC/ETH), we'd need to fetch ETH/USDT price.
        # Here we assume the base currency of symbols is USDT.
        usdt_rate = price
        invested_usdt_equivalent = amount * price

        self.storage.log_trade(
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
            mode=settings.TRADING_MODE,
            pnl=pnl,
            bot_type=self.bot_type,
            invested_usdt_equivalent=invested_usdt_equivalent,
            usdt_rate=usdt_rate,
            fee_usdt=fee_usdt,
            reference_id=reference_id,
        )

    def _log_equity(self) -> None:
        equity = compute_equity(self.capital, self.positions)
        self.storage.log_equity(equity)

        pnl = equity - getattr(self, "initial_equity", equity)
        base = getattr(self, "initial_equity", 0)
        pnl_pct = (pnl / base * 100.0) if base else 0.0

        self.log.info(
            f"[BOT] Current equity (if fully liquidated): {equity:.2f} USDT, "
            f"PnL={pnl:+.2f} USDT ({pnl_pct:+.2f}%)"
        )

    def _log_position_status(self, symbol: str) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            self.log.info(
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

        self.log.info(
            f"[{symbol}] Position: {amount:.6f}, "
            f"invested≈{invested_usdt:.2f} USDT, "
            f"current value≈{current_value_usdt:.2f} USDT, "
            f"unrealized PnL≈{unrealized_pnl:.2f} USDT, "
            f"free capital={self.capital:.2f} USDT"
        )

    def _maybe_log_decision(
        self,
        *,
        symbol: str,
        action: str,
        confidence: float,
        latest_row: pd.Series,
        equity_before: float,
        price: float,
        candle_ts: Any,
        hold_conf_margin: float,
        hold_sample_every_min: int,
    ) -> None:
        """
        Lógica genérica de logging de decisiones, con muestreo para 'hold'.
        Los parámetros de sampling los decide cada estrategia.
        """
        import time as _time

        should_log = action in ("buy", "sell")

        if action == "hold":
            if abs(confidence - 0.5) > hold_conf_margin:
                should_log = True
            else:
                current_minute = int(_time.time() // 60)
                if current_minute % hold_sample_every_min == 0:
                    should_log = True

        if not should_log:
            return

        feature_values = {feat: float(latest_row.get(feat, 0.0)) for feat in settings.FEATURE_COLUMNS}

        candle_ts_str = str(candle_ts) if candle_ts is not None else None

        self.storage.log_decision(
            symbol=symbol,
            action=action,
            confidence=confidence,
            ma_ratio=feature_values.get("ma_ratio", 0.0),
            rsi_14=feature_values.get("rsi_14", 0.0),
            vol_20=feature_values.get("vol_20", 0.0),
            mode=settings.TRADING_MODE,
            equity_before=equity_before,
            price=price,
            candle_ts=candle_ts_str,
        )

    def _should_skip_decision_same_candle(
        self,
        symbol: str,
        candle_ts: Any,
        price: float,
    ) -> bool:
        """
        Generic helper used by concrete strategies to optionally skip
        re-processing a symbol when we are still on the same candle and
        the price move has not been "drastic" enough.

        - Uses self.last_decision_state to detect same-candle decisions.
        - Uses self.config.drastic_move_threshold as a relative price-change threshold.
        - If it decides to skip, it updates last price, logs position status
          and equity, and returns True.
        """
        state = self.last_decision_state.get(symbol) or {}
        last_candle_ts = state.get("candle_ts")
        last_price = state.get("price")

        # If we do not have a previous decision for this symbol,
        # or we are on a different candle, do not skip.
        if last_candle_ts is None or last_candle_ts != candle_ts:
            return False
        if last_price is None or last_price <= 0:
            return False

        # Compute relative price move since the last decision for this candle.
        price_change_pct = abs(price - float(last_price)) / float(last_price)

        threshold = self.config.drastic_move_threshold
        if price_change_pct < threshold:
            # Small move within the same candle: skip re-processing, but
            # keep internal state (price, position, equity) up to date.
            self.last_decision_state[symbol]["price"] = price
            self._update_position_price(symbol, price)

            self._log_position_status(symbol)
            self._log_equity()

            self.log.info(
                f"[{symbol}] Same candle, price move {price_change_pct:.6f} "
                f"< threshold {threshold:.6f}. Skipping decision."
            )
            return True

        return False

    def _process_symbol(self, symbol: str) -> None:
        """
        Hook de alto nivel:
        - fetch de mercado + features
        - delega en la estrategia concreta la decisión
        """
        market_state = self._fetch_latest_market_state(symbol)
        if market_state is None:
            return

        latest_row, price, features = market_state
        candle_ts = getattr(latest_row, "name", None)

        self._process_symbol_decision(
            symbol=symbol,
            latest_row=latest_row,
            price=price,
            features=features,
            candle_ts=candle_ts,
        )

    @abstractmethod
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
        Cada estrategia debe implementar qué hacer a partir del estado de mercado:
        - llamar al modelo,
        - decidir cuándo comprar/vender/hold,
        - actualizar posiciones y capital,
        - llamar a _log_position_status / _log_equity según convenga.
        """
        raise NotImplementedError

    def liquidate_all_positions_to_base(self) -> None:
        """Vende TODAS las posiciones abiertas para quedar en USDT (moneda base)."""
        self.log.info("[BOT] Liquidating ALL open positions into base coin (USDT)...")

        # Iteramos sobre una lista estable por si la estrategia muta `self.positions`.
        for symbol in list(self.symbols):
            pos = self.positions.get(symbol)
            if pos is None:
                continue

            market_state = self._fetch_latest_market_state(symbol)
            if market_state is None:
                self.log.error(f"[{symbol}] Cannot liquidate: failed to fetch latest market state")
                continue

            _latest_row, price, _features = market_state

            try:
                self._liquidate_position_to_base(symbol, float(price))
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"[{symbol}] Error liquidating position: {exc}")

        # Snapshot final de equity post-liquidación
        self._log_equity()
        self.log.info("[BOT] Liquidation process finished.")

    def run(self) -> None:
        self.log.info("[BOT] Starting trading loop...")
        try:
            while True:
                # REL-003: Check circuit breakers at start of loop
                self._check_max_drawdown()

                for symbol in self.symbols:
                    try:
                        self._process_symbol(symbol)
                    except Exception as symbol_exc:  # noqa: BLE001
                        self.log.error(f"[{symbol}] Error in symbol loop: {symbol_exc}")

                time.sleep(self.config.sleep_seconds)

        except KeyboardInterrupt:
            self.log.error("[BOT] Keyboard interrupt. Shutting down bot...")

        finally:
            try:
                self.storage.close()
            except Exception:
                pass
            self.log.info("[BOT] Bot stopped cleanly.")