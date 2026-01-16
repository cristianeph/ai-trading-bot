# bot_foundational_futures.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Literal

import math
import pandas as pd

from common.config import settings
from common.storage import Storage
from common.futures_base_bot import (
    FuturesTradingBotBase,
    FuturesBotCommonConfig,
    FuturesPosition,
)

DEFAULT_CASH_BUFFER_PCT = 0.05
DEFAULT_FEE_RESERVE_USDT = 2.0

DEFAULT_LEVERAGE = 2
DEFAULT_MARGIN_MODE = "isolated"  # recomendado

# TP/SL (ejemplo conservador, ajusta a tu gusto)
DEFAULT_TP_PCT = 0.003   # +0.3%
DEFAULT_SL_PCT = -0.004  # -0.4%


@dataclass
class FoundationalFuturesConfig(FuturesBotCommonConfig):
    tp_pct: float
    sl_pct: float

    # Política: arrancar conservador (solo long)
    allow_shorts: bool


class FoundationalFuturesBot(FuturesTradingBotBase):
    def load_config(self, default_min_confidence: float, default_sleep_seconds: int) -> FoundationalFuturesConfig:
        storage: Storage = self.storage

        min_confidence = storage.get_bot_config_float("min_confidence", default=default_min_confidence)
        sleep_seconds = storage.get_bot_config_int("sleep_seconds", default=default_sleep_seconds)

        cash_buffer_pct = storage.get_bot_config_float("cash_buffer_pct", default=DEFAULT_CASH_BUFFER_PCT)
        fee_reserve_usdt = storage.get_bot_config_float("fee_reserve_usdt", default=DEFAULT_FEE_RESERVE_USDT)

        leverage = storage.get_bot_config_int("futures.leverage", default=DEFAULT_LEVERAGE)
        margin_mode = storage.get_bot_config_str("futures.margin_mode", default=DEFAULT_MARGIN_MODE)

        tp_pct = storage.get_bot_config_float("tp_pct", default=DEFAULT_TP_PCT)
        sl_pct = storage.get_bot_config_float("sl_pct", default=DEFAULT_SL_PCT)

        allow_shorts = storage.get_bot_config_bool("futures.allow_shorts", default=False)

        return FoundationalFuturesConfig(
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
        )

    # -----------------------------
    # Strategy logic
    # -----------------------------
    def _predict_action(self, symbol: str, df: pd.DataFrame) -> tuple[str, float]:
        """
        Usa tu ModelClient. Ajusta esto a la forma real de tu ModelClient.
        Retorna action in {"buy","sell","hold"} + confidence.
        """
        # Ejemplo: tu modelo probablemente requiere features cols
        feature_cols = ["ma_ratio", "rsi_14", "vol_20"]
        X = df[feature_cols].iloc[[-1]]

        pred = self.model.predict(symbol, X)  # <- adapta: puede ser predict_proba, etc.
        # Esperado: {"action":"buy","confidence":0.62} o similar.
        action = str(pred["action"])
        conf = float(pred["confidence"])
        return action, conf

    def _process_symbol_decision(self, symbol: str, latest_row: pd.Series, price: float, features: pd.DataFrame) -> None:
        action, conf = self._predict_action(symbol, features)
        self.log.info(f"[{symbol}] model action: {action}, conf={conf:.2f}, price={price:.2f}")

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
            # si ya hay long/short, por ahora no re-scale (conservador)

        elif action == "sell":
            if pos is not None:
                # si hay long, cerrar
                if pos.get("side") == "long":
                    self._close_position(symbol, price)
                elif pos.get("side") == "short":
                    self._close_position(symbol, price)
            else:
                # Si no hay posición, solo abrimos short si allow_shorts=True
                if self.config.allow_shorts and conf >= self.config.min_confidence:
                    self._open_short(symbol, price, conf)
                else:
                    self.log.info(f"[{symbol}] SELL ignored (no position / shorts disabled).")

        else:
            # hold
            return

    # -----------------------------
    # Position management
    # -----------------------------
    def _position_value_usdt(self, confidence: float) -> float:
        """
        Sizing conservador: usa POSITION_SIZE_PCT del capital disponible (buffers ya aplicados).
        Puedes mejorar luego con vol/risk-factor como en spot.
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

        # En USDT-M futures, amount típicamente es cantidad del asset (BTC) en contratos equivalentes
        amount = position_value / price
        if amount <= 0 or not math.isfinite(amount):
            self.log.info(f"[{symbol}] Invalid amount for LONG: {amount}")
            return

        order = self._create_order(symbol, "buy", float(amount), reduce_only=False)
        avg_price = float(order.get("average") or price)

        self.positions[symbol] = FuturesPosition(
            symbol=symbol,
            side="long",
            amount=float(amount),
            entry_price=avg_price,
            entry_fee_usdt=float((order.get("fee") or {}).get("cost") or 0.0),
            opened_at_ts=time.time(),
            leverage=int(self.config.leverage),
            margin_mode=str(self.config.margin_mode),
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

        # En futures, vender abre short
        order = self._create_order(symbol, "sell", float(amount), reduce_only=False)
        avg_price = float(order.get("average") or price)

        self.positions[symbol] = FuturesPosition(
            symbol=symbol,
            side="short",
            amount=float(amount),
            entry_price=avg_price,
            entry_fee_usdt=float((order.get("fee") or {}).get("cost") or 0.0),
            opened_at_ts=time.time(),
            leverage=int(self.config.leverage),
            margin_mode=str(self.config.margin_mode),
        )
        self.log.info(f"[{symbol}] Open SHORT amount={amount:.8f} entry={avg_price:.2f}")

    def _close_position(self, symbol: str, price: float) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return

        side = pos.get("side")
        amount = float(pos["amount"])
        if amount <= 0:
            self.positions[symbol] = None
            return

        # Cerrar long => sell reduceOnly
        # Cerrar short => buy reduceOnly
        if side == "long":
            order = self._create_order(symbol, "sell", amount, reduce_only=True)
        else:
            order = self._create_order(symbol, "buy", amount, reduce_only=True)

        exit_price = float(order.get("average") or price)
        entry_price = float(pos["entry_price"])

        pnl = (exit_price - entry_price) * amount if side == "long" else (entry_price - exit_price) * amount
        self.log.info(f"[{symbol}] Close {side.upper()}: amount={amount:.8f} entry={entry_price:.2f} exit={exit_price:.2f} pnl≈{pnl:.4f} USDT")

        self.positions[symbol] = None

    def _maybe_close_by_tp_sl(self, symbol: str, price: float, pos: FuturesPosition) -> None:
        entry = float(pos["entry_price"])
        side = pos.get("side")

        if side == "long":
            pct = (price - entry) / entry
        else:
            pct = (entry - price) / entry  # short: gana si baja

        if pct >= float(self.config.tp_pct):
            self.log.info(f"[{symbol}] TP reached ({pct*100:.3f}%), closing position.")
            self._close_position(symbol, price)
        elif pct <= float(self.config.sl_pct):
            self.log.info(f"[{symbol}] SL reached ({pct*100:.3f}%), closing position.")
            self._close_position(symbol, price)


def run_bot_loop() -> None:
    # OJO: en futures el balance se consulta diferente (futures wallet).
    # Por ahora asumimos que ya pasas el balance_usdt correcto.
    ex = get_binance_client(trading_mode="futures")
    bal = ex.fetch_balance()
    # en ccxt futures suele venir en total/free de USDT bajo bal["USDT"]
    usdt_free = float(((bal.get("free") or {}).get("USDT")) or 0.0)

    bot = FoundationalFuturesBot(
        balance_usdt=usdt_free,
        min_confidence=0.55,
        sleep_seconds=60,
    )
    bot.run()