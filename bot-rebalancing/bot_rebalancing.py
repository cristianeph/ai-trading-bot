import math
import os
import pandas as pd

from dataclasses import dataclass
from typing import Optional, Any, cast, Dict, List

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
from common.storage import Storage, PositionRepository
from common.monitoring import init_sentry

init_sentry()

# ------------------------------------------------------------------ #
# Strategy defaults (all overridden by BotConfig when present)       #
# ------------------------------------------------------------------ #

DEFAULT_REBALANCE_THRESHOLD_PCT: float = 0.02   # 2%
DEFAULT_MAX_TRADE_PCT: float = 0.25             # 25%

DEFAULT_DRASTIC_MOVE_THRESHOLD: float = 0.0005  # 0.05%
DEFAULT_HOLD_CONF_MARGIN: float = 0.10
DEFAULT_HOLD_SAMPLE_EVERY_MIN: int = 10

# Smart rebalance / volatility / cash buffer defaults
DEFAULT_SMART_SCALE_MAX_DELTA_PCT: float = 0.10  # 10% extra deviation -> full size
DEFAULT_SMART_VOL_ENABLED: bool = True
DEFAULT_SMART_VOL_MEDIUM: float = 0.02           # medium volatility level
DEFAULT_SMART_VOL_HIGH: float = 0.04             # high volatility level
DEFAULT_CASH_BUFFER_PCT: float = 0.10            # 10% of equity kept as USDT buffer


# ------------------------------------------------------------------ #
# Config object for the rebalancing bot                              #
# ------------------------------------------------------------------ #


@dataclass
class RebalancingConfig:
    sleep_seconds: int
    min_confidence: float

    rebalance_threshold_pct: float
    max_trade_pct: float

    drastic_move_threshold: float
    hold_conf_margin: float
    hold_sample_every_min: int

    cash_buffer_pct: float

    smart_scale_max_delta_pct: float
    smart_vol_enabled: bool
    smart_vol_medium: float
    smart_vol_high: float

    target_weights: Dict[str, float]
    min_trade_amount: Dict[str, float]

    @classmethod
    def from_storage(
        cls,
        storage: Storage,
        *,
        default_sleep_seconds: int,
        default_min_confidence: float,
        symbols: List[str],
    ) -> "RebalancingConfig":
        """
        Construye toda la configuración del bot leyendo BotConfig
        para bot_type="rebalancing", con defaults saneados.
        """

        # Global trading gate
        min_confidence = storage.get_bot_config_float(
            "min_confidence",
            default=default_min_confidence,
        )
        sleep_seconds = storage.get_bot_config_int(
            "sleep_seconds",
            default=default_sleep_seconds,
        )

        # Rebalancing parameters
        rebalance_threshold_pct = storage.get_bot_config_float(
            "rebalance_threshold_pct",
            default=DEFAULT_REBALANCE_THRESHOLD_PCT,
        )
        max_trade_pct = storage.get_bot_config_float(
            "max_trade_pct",
            default=DEFAULT_MAX_TRADE_PCT,
        )

        # Candle-skip / decision sampling
        drastic_move_threshold = storage.get_bot_config_float(
            "drastic_move_threshold",
            default=DEFAULT_DRASTIC_MOVE_THRESHOLD,
        )
        hold_conf_margin = storage.get_bot_config_float(
            "hold_conf_margin",
            default=DEFAULT_HOLD_CONF_MARGIN,
        )
        hold_sample_every_min = storage.get_bot_config_int(
            "hold_sample_every_min",
            default=DEFAULT_HOLD_SAMPLE_EVERY_MIN,
        )

        # Cash buffer
        raw_cash_buffer = storage.get_bot_config_value("cash_buffer_pct", default=None)
        if raw_cash_buffer is None:
            cash_buffer_pct = DEFAULT_CASH_BUFFER_PCT
        else:
            try:
                cash_buffer_pct = float(raw_cash_buffer)
            except (TypeError, ValueError):
                cash_buffer_pct = DEFAULT_CASH_BUFFER_PCT

        # Smart scaling by deviation
        smart_scale_max_delta_pct = storage.get_bot_config_float(
            "smart_scale_max_delta_pct",
            default=DEFAULT_SMART_SCALE_MAX_DELTA_PCT,
        )

        # Volatility aware sizing
        raw_smart_vol_enabled = storage.get_bot_config_value(
            "smart_vol_enabled",
            default="true" if DEFAULT_SMART_VOL_ENABLED else "false",
        )
        smart_vol_enabled = str(raw_smart_vol_enabled).lower() in ("1", "true", "yes", "y")

        smart_vol_medium = storage.get_bot_config_float(
            "smart_vol_medium",
            default=DEFAULT_SMART_VOL_MEDIUM,
        )
        smart_vol_high = storage.get_bot_config_float(
            "smart_vol_high",
            default=DEFAULT_SMART_VOL_HIGH,
        )

        # Per-symbol target weights and min amounts (prefix-based)
        target_weights: Dict[str, float] = {}
        min_trade_amount: Dict[str, float] = {}

        num_symbols = len(symbols)
        default_equal_weight = 1.0 / num_symbols if num_symbols > 0 else 0.0

        weight_configs = storage.get_bot_config_prefix("target_weight.")
        min_amount_configs = storage.get_bot_config_prefix("min_trade_amount.")

        for symbol in symbols:
            weight_key = f"target_weight.{symbol}"
            raw_weight = weight_configs.get(weight_key)
            if raw_weight is not None:
                try:
                    target_weights[symbol] = float(raw_weight)
                except (TypeError, ValueError):
                    target_weights[symbol] = default_equal_weight
            else:
                target_weights[symbol] = default_equal_weight

            min_amount_key = f"min_trade_amount.{symbol}"
            raw_min_amount = min_amount_configs.get(min_amount_key)
            if raw_min_amount is not None:
                try:
                    min_trade_amount[symbol] = float(raw_min_amount)
                except (TypeError, ValueError):
                    min_trade_amount[symbol] = 0.00001
            else:
                min_trade_amount[symbol] = 0.00001

        # Normalizar pesos (sum=1)
        total = sum(max(w, 0.0) for w in target_weights.values())
        if total > 0:
            for sym, w in list(target_weights.items()):
                target_weights[sym] = max(w, 0.0) / total

        return cls(
            sleep_seconds=sleep_seconds,
            min_confidence=min_confidence,
            rebalance_threshold_pct=rebalance_threshold_pct,
            max_trade_pct=max_trade_pct,
            drastic_move_threshold=drastic_move_threshold,
            hold_conf_margin=hold_conf_margin,
            hold_sample_every_min=hold_sample_every_min,
            cash_buffer_pct=cash_buffer_pct,
            smart_scale_max_delta_pct=smart_scale_max_delta_pct,
            smart_vol_enabled=smart_vol_enabled,
            smart_vol_medium=smart_vol_medium,
            smart_vol_high=smart_vol_high,
            target_weights=target_weights,
            min_trade_amount=min_trade_amount,
        )


# ------------------------------------------------------------------ #
# Model router por símbolo                                           #
# ------------------------------------------------------------------ #


class ModelRouter:
    """
    Router simple:
      - default_client por defecto
      - registro por símbolo => client específico
    """

    def __init__(self, default_client: ModelClient):
        self.default_client = default_client
        self.by_symbol: Dict[str, ModelClient] = {}

    def register(self, symbol: str, url: Optional[str]) -> None:
        if not url:
            return
        self.by_symbol[symbol] = ModelClient(url)

    def predict(self, symbol: str, features: list[float]) -> tuple[str, float]:
        client = self.by_symbol.get(symbol, self.default_client)
        return client.predict(features)


class RebalancingTradingBot(BaseTradingBot):
    """
    Portfolio rebalancing bot:
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

        self.log = BotLogger("RebalancingTradingBot")

        # --------------------------------------------------------------
        # Model router por símbolo (BTC, ETH, etc.)
        # --------------------------------------------------------------
        self.model_router = ModelRouter(self.model_client)

        btc_url = os.getenv("MODEL_URL_BTC")
        eth_url = os.getenv("MODEL_URL_ETH")

        if btc_url:
            self.model_router.register("BTC/USDT", btc_url)
        else:
            self.log.error(
                "[REBALANCING] MODEL_URL_BTC not set; BTC/USDT will use default model_client."
            )

        if eth_url:
            self.model_router.register("ETH/USDT", eth_url)
        else:
            self.log.error(
                "[REBALANCING] MODEL_URL_ETH not set; ETH/USDT will use default model_client."
            )

        self.log.info(
            f"[DEBUG CONFIG] BINANCE_TESTNET={settings.BINANCE_TESTNET}, "
            f"TRADING_MODE={settings.TRADING_MODE}"
        )

        # --------------------------------------------------------------
        # Configuración dinámica desde BotConfig
        # --------------------------------------------------------------
        self.config = RebalancingConfig.from_storage(
            self.storage,
            default_sleep_seconds=sleep_seconds,
            default_min_confidence=min_confidence,
            symbols=settings.REBALANCING_SYMBOLS,
        )

        # Mapear sólo lo necesario hacia BaseTradingBot
        # para que el loop y los thresholds globales usen BotConfig.
        self.min_confidence = self.config.min_confidence
        self.sleep_seconds = self.config.sleep_seconds

        self.log.info(
            f"[REBALANCING] Target weights: {self.config.target_weights}, "
            f"rebalance_threshold_pct={self.config.rebalance_threshold_pct:.2%}, "
            f"max_trade_pct={self.config.max_trade_pct:.2%}, "
            f"cash_buffer_pct={self.config.cash_buffer_pct:.2%}, "
            f"smart_scale_max_delta_pct={self.config.smart_scale_max_delta_pct:.2%}, "
            f"smart_vol_enabled={self.config.smart_vol_enabled}, "
            f"min_confidence={self.config.min_confidence:.2f}"
        )

        # --------------------------------------------------------------
        # Position repository como gateway a OpenPosition
        # --------------------------------------------------------------
        self.position_repo = PositionRepository(
            storage=self.storage,
            symbols=settings.REBALANCING_SYMBOLS,
            mode=settings.TRADING_MODE,
        )

        self._sync_positions_from_db()
        self.initial_equity = compute_equity(self.capital, self.positions)

    # ------------------------------------------------------------------ #
    # Hooks del BaseTradingBot                                           #
    # ------------------------------------------------------------------ #

    def _before_symbols_loop(self) -> None:
        """
        Rebuild positions once per loop via PositionRepository.
        """
        self._sync_positions_from_db()

    def _sync_positions_from_db(self) -> None:
        """
        Usa PositionRepository como única fuente de las posiciones agregadas.
        """
        self.positions = self.position_repo.load_aggregated_positions()

    # ------------------------------------------------------------------ #
    # Helpers internos                                                   #
    # ------------------------------------------------------------------ #

    def _compute_current_allocation(
        self,
        symbol: str,
        price: float,
        total_equity: float,
    ) -> tuple[float, float, float, float]:
        current_pos = self.positions.get(symbol)
        current_value = 0.0
        if current_pos is not None:
            amount = float(current_pos.get("amount", 0.0))
            current_value = amount * price

        target_pct = self.target_weights.get(symbol, 0.0)
        current_pct = current_value / total_equity if total_equity > 0 else 0.0
        delta_pct = target_pct - current_pct
        return current_value, current_pct, target_pct, delta_pct

    def _predict_for_symbol(
        self,
        symbol: str,
        features: list[float],
    ) -> tuple[str, float]:
        """
        Usa el router para elegir el modelo BTC/ETH (o default).
        """
        return self.model_router.predict(symbol, features)

    def _execute_exchange_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price_hint: float,
    ) -> Optional[tuple[float, float, float]]:
        """
        Helper común para ejecutar una orden y parsear el resultado.

        Devuelve:
          (executed_amount, avg_price, fee_usdt)
        o None si hubo error.
        """
        try:
            order = place_order(symbol, side, amount)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Error placing {side.upper()} order: {exc}")
            return None

        if isinstance(order, dict):
            executed_amount = float(order.get("amount") or amount)
            avg_price = float(order.get("average") or order.get("price") or price_hint)
            fee_info = order.get("fee") or {}
            fee_currency = fee_info.get("currency")
            fee_cost = float(fee_info.get("cost") or 0.0)
            fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        else:
            executed_amount = amount
            avg_price = price_hint
            fee_usdt = 0.0

        return executed_amount, avg_price, fee_usdt

    @staticmethod
    def _gate_trade_by_model(
        *,
        symbol: str,
        direction: str,      # "buy" o "sell" (rebalance direction)
        trade_value: float,
        model_action: str,
        model_conf: float,
        min_confidence: float,
        log: BotLogger,
    ) -> float:
        """
        Función "pura" (salvo logs) que decide el trade_value final según el modelo.
        """

        if model_action == "buy" and direction == "sell" and model_conf >= min_confidence:
            log.info(
                f"[{symbol}] Skipping SELL rebalance: model suggests BUY "
                f"with conf={model_conf:.2f} >= min_conf={min_confidence:.2f}"
            )
            return 0.0

        if model_action == "sell" and direction == "buy" and model_conf >= min_confidence:
            log.info(
                f"[{symbol}] Skipping BUY rebalance: model suggests SELL "
                f"with conf={model_conf:.2f} >= min_conf={min_confidence:.2f}"
            )
            return 0.0

        if model_conf < min_confidence or model_action == "hold":
            scaled_trade_value = trade_value * 0.5
            log.info(
                f"[{symbol}] Model neutral/low confidence (action={model_action}, "
                f"conf={model_conf:.2f}). Executing half trade_value: "
                f"{scaled_trade_value:.2f} USDT (full={trade_value:.2f})."
            )
            return scaled_trade_value

        log.info(
            f"[{symbol}] Model aligned or not strongly opposing. "
            f"Executing full trade_value: {trade_value:.2f} USDT "
            f"(direction={direction}, action={model_action}, conf={model_conf:.2f})."
        )
        return trade_value

    # ------------------------------------------------------------------ #
    # Trade execution helpers (partial BUY/SELL)                         #
    # ------------------------------------------------------------------ #

    def _rebalance_buy(
        self,
        symbol: str,
        price: float,
        trade_value: float,
    ) -> None:
        if trade_value <= 0 or not math.isfinite(trade_value):
            return

        # Cash buffer
        total_equity = compute_equity(self.capital, self.positions)
        buffer_amount = max(total_equity * self.config.cash_buffer_pct, 0.0)
        max_spend = max(0.0, self.capital - buffer_amount)

        if max_spend <= 0:
            self.log.info(
                f"[{symbol}] Skipping BUY: capital {self.capital:.2f} <= "
                f"buffer {buffer_amount:.2f} (cash_buffer_pct={self.config.cash_buffer_pct:.2%})."
            )
            return

        if trade_value > max_spend:
            self.log.info(
                f"[{symbol}] Reducing BUY trade_value from {trade_value:.2f} to "
                f"{max_spend:.2f} to respect cash buffer "
                f"(equity={total_equity:.2f}, buffer={buffer_amount:.2f})."
            )
            trade_value = max_spend

        if trade_value <= 0 or not math.isfinite(trade_value):
            return

        min_amount = self.config.min_trade_amount.get(symbol, 0.0)
        amount = trade_value / price
        if amount < min_amount:
            self.log.info(
                f"[{symbol}] Skipping BUY: amount {amount:.8f} < "
                f"min_trade_amount {min_amount:.8f}"
            )
            return

        # Sanity: capital puede haber cambiado
        if trade_value > self.capital:
            trade_value = max(0.0, self.capital)
            amount = trade_value / price
            if amount < min_amount or trade_value <= 0:
                self.log.info(
                    f"[{symbol}] Skipping BUY: not enough capital for minimum trade "
                    f"(capital={self.capital:.2f}, trade_value={trade_value:.2f})"
                )
                return

        result = self._execute_exchange_order(symbol, "buy", amount, price)
        if result is None:
            return
        executed_amount, avg_price, fee_usdt = result

        total_cost = executed_amount * avg_price + fee_usdt
        self.capital -= total_cost

        current_pos = self.positions.get(symbol)

        prev_amount = float(current_pos.get("amount", 0.0)) if current_pos else 0.0
        prev_price = float(current_pos.get("entry_price", avg_price)) if current_pos else avg_price
        prev_fee = float(current_pos.get("entry_fee_usdt", 0.0) if current_pos else 0.0)

        new_amount = prev_amount + executed_amount
        if new_amount <= 0:
            self.positions[symbol] = None
            self.position_repo.remove_symbol(symbol)
        else:
            new_entry_price = (
                prev_amount * prev_price + executed_amount * avg_price
            ) / new_amount
            new_fee = prev_fee + fee_usdt

            pos_dict = cast(
                Position,
                {
                    "side": "buy",
                    "amount": new_amount,
                    "entry_price": new_entry_price,
                    "entry_fee_usdt": new_fee,
                    "last_price": avg_price,
                },
            )
            self.positions[symbol] = pos_dict

            self.position_repo.upsert_symbol(
                symbol,
                side="buy",
                amount=new_amount,
                entry_price=new_entry_price,
                entry_fee_usdt=new_fee,
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
        amount = min(amount, amount_available)

        min_amount = self.config.min_trade_amount.get(symbol, 0.0)
        if amount < min_amount:
            self.log.info(
                f"[{symbol}] Skipping SELL: amount {amount:.8f} < "
                f"min_trade_amount {min_amount:.8f}"
            )
            return

        result = self._execute_exchange_order(symbol, "sell", amount, price)
        if result is None:
            return
        executed_amount, exit_price, exit_fee_usdt = result

        proceeds = executed_amount * exit_price - exit_fee_usdt
        self.capital += proceeds

        prev_amount = float(current_pos.get("amount", 0.0))
        entry_price = float(current_pos.get("entry_price", 0.0))
        entry_fee_usdt = float(current_pos.get("entry_fee_usdt", 0.0))

        fee_allocated = entry_fee_usdt * (executed_amount / prev_amount) if prev_amount > 0 else 0.0
        cost_sold = executed_amount * entry_price + fee_allocated
        pnl_realized = proceeds - cost_sold

        remaining_amount = prev_amount - executed_amount
        remaining_fee = entry_fee_usdt - fee_allocated

        if remaining_amount <= 0 or remaining_amount < min_amount:
            self.position_repo.remove_symbol(symbol)
            self.positions[symbol] = None
        else:
            current_pos["amount"] = remaining_amount
            current_pos["last_price"] = exit_price
            current_pos["entry_fee_usdt"] = remaining_fee
            self.positions[symbol] = current_pos

            self.position_repo.upsert_symbol(
                symbol,
                side="buy",
                amount=remaining_amount,
                entry_price=entry_price,
                entry_fee_usdt=remaining_fee,
            )

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
        total_equity = compute_equity(self.capital, self.positions)
        if total_equity <= 0:
            self.log.info(
                f"[{symbol}] Skipping rebalance: non-positive equity ({total_equity})."
            )
            return

        effective_equity = total_equity * max(0.0, 1.0 - self.config.cash_buffer_pct)
        if effective_equity <= 0:
            self.log.info(
                f"[{symbol}] Skipping rebalance: non-positive effective_equity "
                f"after cash buffer (equity={total_equity:.2f}, "
                f"buffer_pct={self.config.cash_buffer_pct:.2%})."
            )
            return

        (
            current_value,
            current_pct,
            target_pct,
            delta_pct,
        ) = self._compute_current_allocation(symbol, price, effective_equity)

        if abs(delta_pct) < self.config.rebalance_threshold_pct:
            self.log.info(
                f"[{symbol}] Allocation within threshold: "
                f"current={current_pct:.2%}, target={target_pct:.2%}, "
                f"delta={delta_pct:.2%}, threshold={self.config.rebalance_threshold_pct:.2%}"
            )
            return

        desired_value_change = delta_pct * effective_equity

        max_trade_value = self.config.max_trade_pct * effective_equity
        base_trade_value = min(abs(desired_value_change), max_trade_value)

        if base_trade_value <= 0:
            return

        # Smart scaling by deviation
        excess_delta_pct = abs(delta_pct) - self.config.rebalance_threshold_pct
        if excess_delta_pct <= 0:
            return

        if self.config.smart_scale_max_delta_pct > 0:
            scale = min(1.0, max(0.0, excess_delta_pct / self.config.smart_scale_max_delta_pct))
        else:
            scale = 1.0

        trade_value = base_trade_value * scale
        if trade_value <= 0:
            self.log.info(
                f"[{symbol}] Smart scaling reduced trade_value to 0. "
                f"delta_pct={delta_pct:.2%}, excess_delta_pct={excess_delta_pct:.2%}"
            )
            return

        # Volatility-aware adjustment
        if self.config.smart_vol_enabled:
            vol_20 = None
            try:
                vol_20 = float(latest_row["vol_20"])
            except Exception:
                vol_20 = None

            if vol_20 is not None and math.isfinite(vol_20):
                vol_factor = 1.0
                if vol_20 >= self.config.smart_vol_high:
                    vol_factor = 0.4
                elif vol_20 >= self.config.smart_vol_medium:
                    vol_factor = 0.7

                adjusted_trade_value = trade_value * vol_factor
                self.log.info(
                    f"[{symbol}] Volatility-aware sizing: vol_20={vol_20:.6f}, "
                    f"factor={vol_factor:.2f}, trade_value={trade_value:.2f} -> "
                    f"{adjusted_trade_value:.2f}"
                )
                trade_value = adjusted_trade_value

        if trade_value <= 0:
            return

        direction = "buy" if desired_value_change > 0 else "sell"

        # Model gating separado
        trade_value = self._gate_trade_by_model(
            symbol=symbol,
            direction=direction,
            trade_value=trade_value,
            model_action=model_action,
            model_conf=model_conf,
            min_confidence=self.config.min_confidence,
            log=self.log,
        )

        if trade_value <= 0:
            return

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
        if self._should_skip_decision_same_candle(symbol, candle_ts, price):
            return

        self.last_decision_state[symbol]["candle_ts"] = candle_ts
        self.last_decision_state[symbol]["price"] = price

        self._update_position_price(symbol, price)
        equity_before = compute_equity(self.capital, self.positions)

        action, conf = self._predict_for_symbol(symbol, features)
        self.log.info(
            f"[{symbol}] model action: {action}, conf={conf:.2f}, price={price:.2f}"
        )

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

        self._apply_rebalancing_logic(
            symbol=symbol,
            latest_row=latest_row,
            price=price,
            model_action=action,
            model_conf=conf,
        )

        self._log_position_status(symbol)
        if symbol == settings.REBALANCING_SYMBOLS[-1]:
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