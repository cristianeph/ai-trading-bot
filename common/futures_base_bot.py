from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, TypedDict, Literal

import pandas as pd

from common.config import settings
from common.data_client import get_historical_ohlcv, get_binance_client  # <- ajusta si tu firma difiere
from common.features import build_features_for_symbol
from common.logger import BotLogger
from common.model_client import ModelClient
from common.storage import Storage


FuturesSide = Literal["long", "short"]


class FuturesPosition(TypedDict, total=False):
    side: FuturesSide
    amount: float              # contratos o base-asset amount (según cómo operes)
    entry_price: float
    entry_fee_usdt: float
    opened_at_ts: float
    leverage: int
    margin_mode: str           # "isolated" | "cross"
    symbol: str


@dataclass
class FuturesBotCommonConfig:
    min_confidence: float
    sleep_seconds: int
    cash_buffer_pct: float
    fee_reserve_usdt: float
    symbols: list[str]
    bot_type: str

    # Futures-specific
    leverage: int
    margin_mode: str  # "isolated" recomendado


class FuturesTradingBotBase(ABC):
    """
    Base bot para Binance Futures (USDT-M Perpetual idealmente).

    Filosofía:
    - El loop y el fetch de datos/feature/modelo son comunes.
    - La estrategia define: cuándo abrir/cerrar, cómo sizear, TP/SL, etc.
    - Encapsulamos setMarginMode/setLeverage y órdenes reduceOnly.
    """

    def __init__(
        self,
        balance_usdt: float,
        storage: Optional[Storage] = None,
        model_client: Optional[ModelClient] = None,
        logger: Optional[BotLogger] = None,
        min_confidence: float = 0.55,
        sleep_seconds: int = 60,
    ) -> None:
        self.storage = storage or Storage()
        self.model = model_client or ModelClient()
        self.log = logger or BotLogger(self.__class__.__name__)
        self.capital = float(balance_usdt)

        # Estrategia define su config; aquí exigimos que exista un objeto config completo
        self.config = self.load_config(
            default_min_confidence=min_confidence,
            default_sleep_seconds=sleep_seconds,
        )

        self.symbols = list(self.config.symbols)
        self.positions: Dict[str, Optional[FuturesPosition]] = {s: None for s in self.symbols}

        # Tracking de candles/decisiones como en spot
        self.last_candle_ts: Dict[str, Optional[Any]] = {s: None for s in self.symbols}
        self.last_decision_price: Dict[str, Optional[float]] = {s: None for s in self.symbols}

        self.log.info(f"[DEBUG CONFIG] {self.config}")

    # -----------------------------
    # Config / Strategy hooks
    # -----------------------------
    @abstractmethod
    def load_config(self, default_min_confidence: float, default_sleep_seconds: int) -> FuturesBotCommonConfig:
        raise NotImplementedError

    @abstractmethod
    def _process_symbol_decision(
        self,
        symbol: str,
        latest_row: pd.Series,
        price: float,
        features: pd.DataFrame,
    ) -> None:
        raise NotImplementedError

    # -----------------------------
    # Exchange access
    # -----------------------------
    def _check_max_drawdown(self) -> None:
        """
        Implement a maximum drawdown circuit breaker to stop the bot if losses
        exceed a certain threshold.
        """
        # Fetch threshold from storage/config
        max_drawdown_pct = self.storage.get_bot_config_float("max_drawdown_pct", 0.1)

        # Approximate equity (for futures this is often balance + unrealized PnL)
        # For simplicity here we use self.capital
        equity = float(self.capital)
        drawdown_pct = (self.initial_equity - equity) / self.initial_equity if self.initial_equity > 0 else 0.0

        if drawdown_pct >= max_drawdown_pct:
            self.log.error(
                f"[CIRCUIT BREAKER] Max Drawdown reached: {drawdown_pct:.2%}. "
                f"Initial Equity: {self.initial_equity:.2f}, Current Equity: {equity:.2f}. "
                f"Stopping bot."
            )
            # In futures, we should close all positions
            raise SystemExit(f"Max Drawdown circuit breaker triggered: {drawdown_pct:.2%}")

    def _get_exchange(self):
        """
        Retorna un cliente de exchange configurado para FUTURES.
        Idealmente: ccxt binance con options {'defaultType': 'future'} o equivalente.
        """
        # Ajusta esto a tu implementación actual: get_binance_client(mode="futures") o similar
        return get_binance_client(trading_mode="futures")

    def _ensure_futures_settings(self, symbol: str) -> None:
        """
        Setea margin mode y leverage (idempotente).
        Debe correrse antes de abrir posición.
        """
        ex = self._get_exchange()
        try:
            # ccxt: set_margin_mode / set_leverage (si tu wrapper difiere, adapta aquí)
            ex.set_margin_mode(self.config.margin_mode, symbol)
        except Exception as exc:  # noqa: BLE001
            self.log.info(f"[{symbol}] Margin mode set skipped/failed (ok if already set): {exc}")

        try:
            ex.set_leverage(int(self.config.leverage), symbol)
        except Exception as exc:  # noqa: BLE001
            self.log.info(f"[{symbol}] Leverage set skipped/failed (ok if already set): {exc}")

    # -----------------------------
    # Data pipeline
    # -----------------------------
    def _fetch_latest_market_state(
        self, symbol: str
    ) -> Optional[Tuple[pd.Series, float, pd.DataFrame]]:
        ohlcv = get_historical_ohlcv(symbol, settings.TIMEFRAME, limit=1000)
        if not ohlcv:
            return None

        df = build_features_for_symbol(ohlcv, symbol, settings.TIMEFRAME)
        if df.empty:
            return None

        latest_row = df.iloc[-1]
        price = float(latest_row["close"])
        return latest_row, price, df

    def _available_capital_for_new_trades(self) -> float:
        buffer_usdt = max(0.0, float(self.capital) * float(self.config.cash_buffer_pct))
        reserve_usdt = max(0.0, float(self.config.fee_reserve_usdt))
        return max(0.0, float(self.capital) - buffer_usdt - reserve_usdt)

    # -----------------------------
    # Orders (common)
    # -----------------------------
    def _create_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: float,
        reduce_only: bool = False,
    ) -> Any:
        """
        Crea orden market en futures. Para cerrar posición, usa reduceOnly=True.
        """
        ex = self._get_exchange()
        params: Dict[str, Any] = {}
        # ccxt binance futures: reduceOnly suele ir en params
        if reduce_only:
            params["reduceOnly"] = True

        self.log.info(f"[LIVE] Enviando orden {side.upper()} {amount} {symbol} (reduceOnly={reduce_only})...")
        order = ex.create_order(symbol, "market", side, amount, params=params)
        self.log.info(f"[LIVE] Orden ejecutada: {order}")
        return order

    def _log_equity(self) -> None:
        """
        Equity aproximado: capital USDT + (PNL no realizado si decides estimarlo).
        Por simplicidad aquí mantenemos 'capital' como USDT libre, y el bot/estrategia puede ampliarlo.
        """
        self.log.info(f"[BOT] Free capital: {self.capital:.2f} USDT")

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
        Helper to log trades with rich metadata for futures.
        """
        usdt_rate = price  # Assuming quote is USDT
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

    # -----------------------------
    # Main loop
    # -----------------------------
    def run(self) -> None:
        self.log.info("[BOT] Futures bot started.")
        while True:
            # REL-003: Check circuit breakers
            self._check_max_drawdown()

            for symbol in self.symbols:
                state = self._fetch_latest_market_state(symbol)
                if state is None:
                    self.log.error(f"[{symbol}] Failed to fetch market state.")
                    continue

                latest_row, price, df = state
                try:
                    self._process_symbol_decision(symbol, latest_row, price, df)
                except Exception as exc:  # noqa: BLE001
                    self.log.error(f"[{symbol}] Error in decision processing: {exc}")

            self._log_equity()
            time.sleep(int(self.config.sleep_seconds))
