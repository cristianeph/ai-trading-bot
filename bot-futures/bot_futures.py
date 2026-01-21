# bot_foundational_futures.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Literal

import math
import time
import pandas as pd

from common.config import settings
from common.storage import Storage
from common.data_client import get_binance_client
from common.model_client import ModelClient
from common.futures_base_bot import (
    FuturesTradingBotBase,
    FuturesBotCommonConfig,
    FuturesPosition,
)


@dataclass
class FoundationalFuturesConfig(FuturesBotCommonConfig):
    tp_pct: float
    sl_pct: float
    allow_shorts: bool
    drastic_move_threshold: float
    hold_conf_margin: float
    hold_sample_every_min: int
    max_drawdown_pct: float

    def validate(self) -> None:
        if not (0 <= self.min_confidence <= 1):
            raise ValueError(f"min_confidence must be between 0 and 1, got {self.min_confidence}")
        if self.sleep_seconds <= 0:
            raise ValueError(f"sleep_seconds must be positive, got {self.sleep_seconds}")
        if not (0 <= self.cash_buffer_pct <= 1):
            raise ValueError(f"cash_buffer_pct must be between 0 and 1, got {self.cash_buffer_pct}")
        if self.tp_pct <= 0:
            raise ValueError(f"tp_pct must be positive, got {self.tp_pct}")
        if self.sl_pct >= 0:
            raise ValueError(f"sl_pct must be negative, got {self.sl_pct}")
        if self.drastic_move_threshold <= 0:
            raise ValueError(f"drastic_move_threshold must be positive, got {self.drastic_move_threshold}")
        if self.max_drawdown_pct <= 0 or self.max_drawdown_pct >= 1:
            raise ValueError(f"max_drawdown_pct must be between 0 and 1, got {self.max_drawdown_pct}")

    @classmethod
    def from_storage(
        cls,
        storage: Storage,
        default_min_confidence: float,
        default_sleep_seconds: int,
    ) -> FoundationalFuturesConfig:
        min_confidence = storage.get_bot_config_float("min_confidence", default=default_min_confidence)
        sleep_seconds = storage.get_bot_config_int("sleep_seconds", default=default_sleep_seconds)

        cash_buffer_pct = storage.get_bot_config_float("cash_buffer_pct", default=settings.DEFAULT_CASH_BUFFER_PCT)
        fee_reserve_usdt = storage.get_bot_config_float("fee_reserve_usdt", default=settings.DEFAULT_FEE_RESERVE_USDT)

        leverage = storage.get_bot_config_int("futures.leverage", default=settings.FUT_DEFAULT_LEVERAGE)
        margin_mode = storage.get_bot_config_str("futures.margin_mode", default=settings.FUT_DEFAULT_MARGIN_MODE)

        tp_pct = storage.get_bot_config_float("tp_pct", default=settings.FUT_DEFAULT_TP_PCT)
        sl_pct = storage.get_bot_config_float("sl_pct", default=settings.FUT_DEFAULT_SL_PCT)

        allow_shorts = storage.get_bot_config_bool("futures.allow_shorts", default=settings.FUT_ALLOW_SHORTS)

        drastic_move_threshold = storage.get_bot_config_float("drastic_move_threshold", default=settings.DRASTIC_MOVE_THRESHOLD)
        hold_conf_margin = storage.get_bot_config_float("hold_conf_margin", default=settings.HOLD_CONF_MARGIN)
        hold_sample_every_min = storage.get_bot_config_int("hold_sample_every_min", default=settings.HOLD_SAMPLE_EVERY_MIN)
        max_drawdown_pct = storage.get_bot_config_float("max_drawdown_pct", default=settings.DEFAULT_MAX_DRAWDOWN_PCT)

        config = cls(
            min_confidence=min_confidence,
            sleep_seconds=sleep_seconds,
            cash_buffer_pct=cash_buffer_pct,
            fee_reserve_usdt=fee_reserve_usdt,
            symbols=settings.SYMBOLS,
            bot_type="foundational_futures",
            leverage=int(leverage),
            margin_mode=str(margin_mode),
            tp_pct=float(tp_pct),
            sl_pct=float(sl_pct),
            allow_shorts=bool(allow_shorts),
            drastic_move_threshold=drastic_move_threshold,
            hold_conf_margin=hold_conf_margin,
            hold_sample_every_min=hold_sample_every_min,
            max_drawdown_pct=max_drawdown_pct,
        )
        config.validate()
        return config


class FoundationalFuturesBot(FuturesTradingBotBase):
    def __init__(
        self,
        balance_usdt: float,
        storage: Optional[Storage] = None,
        model_client: Optional[ModelClient] = None,
        min_confidence: float = 0.55,
        sleep_seconds: int = 60,
    ) -> None:
        super().__init__(
            balance_usdt=balance_usdt,
            storage=storage,
            model_client=model_client,
            min_confidence=min_confidence,
            sleep_seconds=sleep_seconds,
        )
        self.initial_equity = float(balance_usdt)
        self.last_hold_log_time: Dict[str, float] = {}

    def load_config(self, default_min_confidence: float, default_sleep_seconds: int) -> FoundationalFuturesConfig:
        return FoundationalFuturesConfig.from_storage(
            self.storage,
            default_min_confidence=default_min_confidence,
            default_sleep_seconds=default_sleep_seconds,
        )

    def _check_max_drawdown(self) -> None:
        # TODO: Mejorar estimación de equity con PnL no realizado
        equity = self.capital
        drawdown_pct = (self.initial_equity - equity) / self.initial_equity if self.initial_equity > 0 else 0.0

        if drawdown_pct >= self.config.max_drawdown_pct:
            self.log.error(
                f"[CIRCUIT BREAKER] Max Drawdown reached: {drawdown_pct:.2%}. "
                f"Initial Equity: {self.initial_equity:.2f}, Current Equity: {equity:.2f}. "
                f"Stopping bot."
            )
            for symbol in self.symbols:
                if self.positions.get(symbol):
                    # Fetch current price for liquidation
                    state = self._fetch_latest_market_state(symbol)
                    if state:
                        _, price, _ = state
                        self._close_position(symbol, price)

            raise SystemExit("Max Drawdown circuit breaker triggered.")

    def _should_skip_decision_same_candle(self, symbol: str, latest_row: pd.Series, price: float) -> bool:
        candle_ts = latest_row.name  # Assuming index is timestamp or there's a timestamp
        last_ts = self.last_candle_ts.get(symbol)
        last_price = self.last_decision_price.get(symbol)

        if last_ts != candle_ts:
            return False

        if last_price is None or last_price <= 0:
            return False

        price_change = abs(price - last_price) / last_price
        if self.positions.get(symbol) is not None and price_change < self.config.drastic_move_threshold:
            return True

        return False

    # -----------------------------
    # Strategy logic
    # -----------------------------
    def _predict_action(self, symbol: str, df: pd.DataFrame) -> tuple[str, float]:
        """
        Usa tu ModelClient.
        Retorna action in {"buy","sell","hold"} + confidence.
        """
        # Ajustar a la forma real del ModelClient y features esperadas
        feature_cols = settings.FEATURE_COLUMNS
        X = df[feature_cols].iloc[[-1]]

        # Nota: FuturesTradingBotBase tiene self.model que es un ModelClient
        # El ModelClient.predict usualmente toma una lista de floats (features)
        # o un DataFrame si está adaptado.
        features_list = X.values.flatten().tolist()
        action, conf = self.model.predict(features_list)
        return action, conf

    def _process_symbol_decision(self, symbol: str, latest_row: pd.Series, price: float, features: pd.DataFrame) -> None:
        self._check_max_drawdown()

        if self._should_skip_decision_same_candle(symbol, latest_row, price):
            return

        action, conf = self._predict_action(symbol, features)

        # Update tracking
        self.last_candle_ts[symbol] = latest_row.name
        self.last_decision_price[symbol] = price

        self.log.info(f"[{symbol}] model action: {action}, conf={conf:.2f}, price={price:.2f}")

        # 0) Log decision to DB
        try:
            ma_ratio = float(latest_row.get("ma_ratio", 0))
            rsi_14 = float(latest_row.get("rsi_14", 0))
            vol_20 = float(latest_row.get("vol_20", 0))
            candle_ts_str = str(latest_row.name) if latest_row.name else None

            self.storage.log_decision(
                symbol=symbol,
                action=action,
                confidence=conf,
                ma_ratio=ma_ratio,
                rsi_14=rsi_14,
                vol_20=vol_20,
                mode=settings.TRADING_MODE,
                equity_before=self.capital,
                price=price,
                candle_ts=candle_ts_str,
                bot_type=self.config.bot_type,
            )
        except Exception as e:
            self.log.error(f"[{symbol}] Error logging decision: {e}")

        pos = self.positions.get(symbol)

        # 1) Si hay posición, primero chequea TP/SL (risk management)
        if pos is not None:
            self._maybe_close_by_tp_sl(symbol, price, pos)

        # 2) Aplica acción del modelo
        pos = self.positions.get(symbol)  # re-read por si se cerró
        if action == "buy":
            if conf < self.config.min_confidence:
                self.log.info(f"[{symbol}] Skipping BUY: conf={conf:.2f} < min_conf={self.config.min_confidence:.2f}")
                return
            if pos is None:
                self._open_long(symbol, price, conf)
            elif pos.get("side") == "short":
                self._close_position(symbol, price)
                self._open_long(symbol, price, conf)

        elif action == "sell":
            if conf < self.config.min_confidence:
                 self.log.info(f"[{symbol}] Skipping SELL: conf={conf:.2f} < min_conf={self.config.min_confidence:.2f}")
                 return

            if pos is not None:
                if pos.get("side") == "long":
                    self._close_position(symbol, price)
                    if self.config.allow_shorts:
                        self._open_short(symbol, price, conf)
                elif pos.get("side") == "short":
                    # ya estamos short, podríamos re-scale pero por ahora conservador
                    pass
            else:
                if self.config.allow_shorts:
                    self._open_short(symbol, price, conf)
                else:
                    self.log.info(f"[{symbol}] SELL ignored (no position / shorts disabled).")
        else:
            # hold
            pass

    # -----------------------------
    # Position management
    # -----------------------------
    def _position_value_usdt(self, confidence: float) -> float:
        """
        Sizing conservador: usa POSITION_SIZE_PCT del capital disponible (buffers ya aplicados).
        """
        available = self._available_capital_for_new_trades()
        value = float(available) * float(settings.POSITION_SIZE_PCT)
        return max(0.0, value)

    def _open_long(self, symbol: str, price: float, confidence: float) -> None:
        self._ensure_futures_settings(symbol)

        position_value = self._position_value_usdt(confidence)
        if position_value <= 0:
            self.log.info(f"[{symbol}] Skipping LONG: insufficient available capital.")
            return

        amount = position_value / price
        if amount <= 0 or not math.isfinite(amount):
            self.log.info(f"[{symbol}] Invalid amount for LONG: {amount}")
            return

        order = self._create_order(symbol, "buy", float(amount), reduce_only=False)
        avg_price = float(order.get("average") or price)
        fee = float((order.get("fee") or {}).get("cost") or 0.0)

        self.positions[symbol] = FuturesPosition(
            symbol=symbol,
            side="long",
            amount=float(amount),
            entry_price=avg_price,
            entry_fee_usdt=fee,
            opened_at_ts=time.time(),
            leverage=int(self.config.leverage),
            margin_mode=str(self.config.margin_mode),
        )
        self.capital -= (amount * avg_price / self.config.leverage) + fee

        self.storage.log_trade(
            symbol=symbol,
            side="buy",
            price=avg_price,
            amount=amount,
            mode=settings.TRADING_MODE,
            pnl=0.0,
            bot_type=self.config.bot_type,
        )

        self.log.info(f"[{symbol}] Open LONG amount={amount:.8f} entry={avg_price:.2f}")

    def _open_short(self, symbol: str, price: float, confidence: float) -> None:
        self._ensure_futures_settings(symbol)

        position_value = self._position_value_usdt(confidence)
        if position_value <= 0:
            self.log.info(f"[{symbol}] Skipping SHORT: insufficient available capital.")
            return

        amount = position_value / price
        if amount <= 0 or not math.isfinite(amount):
            self.log.info(f"[{symbol}] Invalid amount for SHORT: {amount}")
            return

        order = self._create_order(symbol, "sell", float(amount), reduce_only=False)
        avg_price = float(order.get("average") or price)
        fee = float((order.get("fee") or {}).get("cost") or 0.0)

        self.positions[symbol] = FuturesPosition(
            symbol=symbol,
            side="short",
            amount=float(amount),
            entry_price=avg_price,
            entry_fee_usdt=fee,
            opened_at_ts=time.time(),
            leverage=int(self.config.leverage),
            margin_mode=str(self.config.margin_mode),
        )
        self.capital -= (amount * avg_price / self.config.leverage) + fee

        self.storage.log_trade(
            symbol=symbol,
            side="sell",
            price=avg_price,
            amount=amount,
            mode=settings.TRADING_MODE,
            pnl=0.0,
            bot_type=self.config.bot_type,
        )

        self.log.info(f"[{symbol}] Open SHORT amount={amount:.8f} entry={avg_price:.2f}")

    def _close_position(self, symbol: str, price: float) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return

        side = pos.get("side")
        amount = float(pos["amount"])
        entry_price = float(pos["entry_price"])

        if side == "long":
            order = self._create_order(symbol, "sell", amount, reduce_only=True)
        else:
            order = self._create_order(symbol, "buy", amount, reduce_only=True)

        exit_price = float(order.get("average") or price)
        fee = float((order.get("fee") or {}).get("cost") or 0.0)

        pnl = (exit_price - entry_price) * amount if side == "long" else (entry_price - exit_price) * amount
        # Devolver margen + PnL - fee
        self.capital += (amount * entry_price / self.config.leverage) + pnl - fee

        self.storage.log_trade(
            symbol=symbol,
            side="sell" if side == "long" else "buy",
            price=exit_price,
            amount=amount,
            mode=settings.TRADING_MODE,
            pnl=pnl,
            bot_type=self.config.bot_type,
        )

        self.log.info(f"[{symbol}] Close {side.upper()}: amount={amount:.8f} entry={entry_price:.2f} exit={exit_price:.2f} pnl≈{pnl:.4f} USDT")
        self.positions[symbol] = None

    def _maybe_close_by_tp_sl(self, symbol: str, price: float, pos: FuturesPosition) -> None:
        entry = float(pos["entry_price"])
        side = pos.get("side")

        if side == "long":
            pct = (price - entry) / entry
        else:
            pct = (entry - price) / entry

        if pct >= float(self.config.tp_pct):
            self.log.info(f"[{symbol}] TP reached ({pct:.3%}), closing position.")
            self._close_position(symbol, price)
        elif pct <= float(self.config.sl_pct):
            self.log.info(f"[{symbol}] SL reached ({pct:.3%}), closing position.")
            self._close_position(symbol, price)


def run_bot_loop() -> None:
    ex = get_binance_client()
    # Para futuros, necesitamos asegurarnos que el balance es del wallet de futuros
    # En CCXT binance.fetch_balance() devuelve todos, pero hay que saber filtrar.
    # Por ahora seguimos la lógica simple o forzamos paper trade con capital inicial.
    if settings.TRADING_MODE == "live":
        bal = ex.fetch_balance({"type": "future"})
        usdt_free = float(((bal.get("free") or {}).get("USDT")) or 0.0)
    else:
        usdt_free = float(settings.BASE_CAPITAL)

    bot = FoundationalFuturesBot(
        balance_usdt=usdt_free,
        min_confidence=0.55,
        sleep_seconds=60,
    )
    bot.run()


if __name__ == "__main__":
    run_bot_loop()