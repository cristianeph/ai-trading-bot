import math
import os
from dataclasses import dataclass
from typing import Optional, Any, cast, Dict, List

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
from common.storage import Storage, PositionRepository
from common.monitoring import init_sentry

import time

init_sentry()


# ------------------------------------------------------------------ #
# Config object for the rebalancing bot                              #
# ------------------------------------------------------------------ #


@dataclass
class RebalancingConfig:
    """
    Configuration for the rebalancing trading strategy.
    """
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
    symbols: list[str]
    max_drawdown_pct: float

    def validate(self) -> None:
        """
        Validate configuration parameters.
        Raises ValueError if any parameter is out of expected range.
        """
        if not (0 <= self.min_confidence <= 1):
            raise ValueError(f"min_confidence must be between 0 and 1, got {self.min_confidence}")
        if self.sleep_seconds <= 0:
            raise ValueError(f"sleep_seconds must be positive, got {self.sleep_seconds}")
        if not (0 <= self.rebalance_threshold_pct <= 1):
            raise ValueError(f"rebalance_threshold_pct must be between 0 and 1, got {self.rebalance_threshold_pct}")
        if not (0 <= self.max_trade_pct <= 1):
            raise ValueError(f"max_trade_pct must be between 0 and 1, got {self.max_trade_pct}")
        if self.drastic_move_threshold <= 0:
            raise ValueError(f"drastic_move_threshold must be positive, got {self.drastic_move_threshold}")
        if not (0 <= self.hold_conf_margin <= 1):
            raise ValueError(f"hold_conf_margin must be between 0 and 1, got {self.hold_conf_margin}")
        if self.hold_sample_every_min <= 0:
            raise ValueError(f"hold_sample_every_min must be positive, got {self.hold_sample_every_min}")
        if not (0 <= self.cash_buffer_pct <= 1):
            raise ValueError(f"cash_buffer_pct must be between 0 and 1, got {self.cash_buffer_pct}")
        if self.max_drawdown_pct <= 0 or self.max_drawdown_pct >= 1:
            raise ValueError(f"max_drawdown_pct must be between 0 and 1, got {self.max_drawdown_pct}")

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
        Load configuration from storage for the rebalancing bot.
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
            default=settings.REB_REBALANCE_THRESHOLD_PCT,
        )
        max_trade_pct = storage.get_bot_config_float(
            "max_trade_pct",
            default=settings.REB_MAX_TRADE_PCT,
        )

        # Candle-skip / decision sampling
        drastic_move_threshold = storage.get_bot_config_float(
            "drastic_move_threshold",
            default=settings.DRASTIC_MOVE_THRESHOLD,
        )
        hold_conf_margin = storage.get_bot_config_float(
            "hold_conf_margin",
            default=settings.HOLD_CONF_MARGIN,
        )
        hold_sample_every_min = storage.get_bot_config_int(
            "hold_sample_every_min",
            default=settings.HOLD_SAMPLE_EVERY_MIN,
        )

        # Cash buffer
        cash_buffer_pct = storage.get_bot_config_float(
            "cash_buffer_pct",
            default=settings.REB_CASH_BUFFER_PCT,
        )

        # Smart scaling by deviation
        smart_scale_max_delta_pct = storage.get_bot_config_float(
            "smart_scale_max_delta_pct",
            default=settings.REB_SMART_SCALE_MAX_DELTA_PCT,
        )

        # Volatility aware sizing
        raw_smart_vol_enabled = storage.get_bot_config_value(
            "smart_vol_enabled",
            default="true" if settings.REB_SMART_VOL_ENABLED else "false",
        )
        smart_vol_enabled = str(raw_smart_vol_enabled).lower() in ("1", "true", "yes", "y")

        smart_vol_medium = storage.get_bot_config_float(
            "smart_vol_medium",
            default=settings.REB_SMART_VOL_MEDIUM,
        )
        smart_vol_high = storage.get_bot_config_float(
            "smart_vol_high",
            default=settings.REB_SMART_VOL_HIGH,
        )

        # Max Drawdown
        max_drawdown_pct = storage.get_bot_config_float(
            "max_drawdown_pct",
            default=settings.DEFAULT_MAX_DRAWDOWN_PCT,
        )

        # Per-symbol target weights and min amounts
        target_weights: Dict[str, float] = {}
        min_trade_amount: Dict[str, float] = {}

        num_symbols = len(symbols)
        default_equal_weight = 1.0 / num_symbols if num_symbols > 0 else 0.0

        for symbol in symbols:
            target_weights[symbol] = storage.get_bot_config_float(
                f"target_weight.{symbol}",
                default=default_equal_weight,
            )
            min_trade_amount[symbol] = storage.get_bot_config_float(
                f"min_trade_amount.{symbol}",
                default=settings.MIN_TRADE_AMOUNT.get(symbol, 0.00001),
            )

        # Normalize weights (sum=1)
        total = sum(max(w, 0.0) for w in target_weights.values())
        if total > 0:
            for sym, w in list(target_weights.items()):
                target_weights[sym] = max(w, 0.0) / total

        config = cls(
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
            symbols=symbols,
            max_drawdown_pct=max_drawdown_pct,
        )
        config.validate()
        return config


# ------------------------------------------------------------------ #
# Model router por símbolo                                           #
# ------------------------------------------------------------------ #


class ModelRouter:
    """
    Simple router to direct prediction requests to the appropriate model client for each symbol.
    """

    def __init__(self, default_client: ModelClient):
        """
        Initialize the router with a default client.
        """
        self.default_client = default_client
        self.by_symbol: Dict[str, ModelClient] = {}

    def register(self, symbol: str, url: Optional[str]) -> None:
        """
        Register a specific model URL for a given symbol.
        """
        if not url:
            return
        self.by_symbol[symbol] = ModelClient(url)

    def predict(self, symbol: str, features: list[float]) -> tuple[str, float]:
        """
        Get model prediction for a symbol using its specific client if available, or the default.
        """
        client = self.by_symbol.get(symbol, self.default_client)
        return client.predict(features)


class RebalancingTradingBot(BaseTradingBot):
    """
    Portfolio rebalancing bot that maintains a target weight for multiple assets.
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
        """
        Initialize the rebalancing bot.
        """
        self._default_sleep_seconds = sleep_seconds
        self._default_min_confidence = min_confidence

        super().__init__(
            balance=balance,
            storage=storage,
            model_client=model_client,
            bot_type="rebalancing",
        )

        self.log = BotLogger("RebalancingTradingBot")

        # Feature flag: disable model usage (pure rebalancing)
        self.use_model_prediction: bool = str(os.getenv("USE_MODEL_PREDICTION", "false")).lower() in (
            "1", "true", "yes", "y"
        )

        # Model router per symbol (BTC, ETH, etc.)
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

        self.log.info(
            f"[REBALANCING] Target weights: {self.config.target_weights}, "
            f"rebalance_threshold_pct={self.config.rebalance_threshold_pct:.2%}, "
            f"max_trade_pct={self.config.max_trade_pct:.2%}, "
            f"cash_buffer_pct={self.config.cash_buffer_pct:.2%}, "
            f"smart_scale_max_delta_pct={self.config.smart_scale_max_delta_pct:.2%}, "
            f"smart_vol_enabled={self.config.smart_vol_enabled}, "
            f"min_confidence={self.config.min_confidence:.2f}"
        )

        # Position repository as gateway to OpenPosition
        self.position_repo = PositionRepository(
            storage=self.storage,
            symbols=settings.REBALANCING_SYMBOLS,
            mode=settings.TRADING_MODE,
        )

        self._sync_positions_from_db()
        self.initial_equity = compute_equity(self.capital, self.positions)

    def load_config(self) -> RebalancingConfig:
        """
        Load rebalancing strategy configuration from storage.
        """
        return RebalancingConfig.from_storage(
            self.storage,
            default_sleep_seconds=self._default_sleep_seconds,
            default_min_confidence=self._default_min_confidence,
            symbols=settings.REBALANCING_SYMBOLS,
        )

    # ------------------------------------------------------------------ #
    # Hooks del BaseTradingBot                                           #
    # ------------------------------------------------------------------ #

    def _before_symbols_loop(self) -> None:
        """
        Hook executed before processing symbols in the main loop.
        Syncs positions from the database.
        """
        self._sync_positions_from_db()

    def _sync_positions_from_db(self) -> None:
        """
        Synchronize internal position state with the database using PositionRepository.
        """
        self.positions = self.position_repo.load_aggregated_positions()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _compute_current_allocation(
        self,
        symbol: str,
        price: float,
        total_equity: float,
    ) -> tuple[float, float, float, float]:
        """
        Compute current value, percentage, target percentage, and delta for a symbol.
        """
        current_pos = self.positions.get(symbol)
        current_value = 0.0
        if current_pos is not None:
            amount = float(current_pos.get("amount", 0.0))
            current_value = amount * price

        target_pct = self.config.target_weights.get(symbol, 0.0)
        current_pct = current_value / total_equity if total_equity > 0 else 0.0
        delta_pct = target_pct - current_pct
        return current_value, current_pct, target_pct, delta_pct

    def _predict_for_symbol(
        self,
        symbol: str,
        features: list[float],
    ) -> tuple[str, float]:
        """
        Predict model action and confidence for a specific symbol using the router.
        """
        return self.model_router.predict(symbol, features)

    def _execute_exchange_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price_hint: float,
        retries: int = 2,
    ) -> Optional[tuple[float, float, float, str, float]]:
        """
        Execute an order and return (filled_base, avg_price, cost_quote, fee_currency, fee_cost).
        Implements a simple retry logic for connectivity or transient errors.
        """
        last_exception = None
        for attempt in range(retries):
            try:
                order = place_order(symbol, side, amount)

                # CCXT orders typically include: filled (base), cost (quote), average, fee{currency,cost}
                if isinstance(order, dict):
                    filled_base = float(order.get("filled") or order.get("amount") or amount)
                    avg_price = float(order.get("average") or order.get("price") or price_hint)
                    cost_quote = float(order.get("cost") or (filled_base * avg_price))

                    fee_info = order.get("fee") or {}
                    fee_currency = str(fee_info.get("currency") or "")
                    fee_cost = float(fee_info.get("cost") or 0.0)
                else:
                    filled_base = float(amount)
                    avg_price = float(price_hint)
                    cost_quote = float(filled_base * avg_price)
                    fee_currency = ""
                    fee_cost = 0.0

                return filled_base, avg_price, cost_quote, fee_currency, fee_cost

            except Exception as exc:  # noqa: BLE001
                last_exception = exc
                self.log.error(
                    f"[{symbol}] Attempt {attempt + 1}/{retries} failed to place {side.upper()} order: {exc}"
                )
                if attempt < retries - 1:
                    time.sleep(1)  # brief pause before retry

        self.log.error(f"[{symbol}] All {retries} attempts failed to place {side.upper()} order. Final error: {last_exception}")
        return None

    @staticmethod
    def _base_asset(symbol: str) -> str:
        # "BTC/USDT" -> "BTC"
        return symbol.split("/")[0].strip()

    def _get_free_balance_base(self, symbol: str) -> float:
        """Return free balance for the base asset of a symbol (LIVE only safe-guard)."""
        try:
            exchange = get_binance_client()
            bal = exchange.fetch_balance()
            asset = self._base_asset(symbol)
            # CCXT balance shape can be either bal[asset]["free"] or bal["free"][asset]
            if asset in bal and isinstance(bal[asset], dict) and "free" in bal[asset]:
                return float(bal[asset].get("free") or 0.0)
            free_map = bal.get("free") or {}
            return float(free_map.get(asset) or 0.0)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"[{symbol}] Failed to fetch free balance for base asset: {exc}")
            return 0.0

    def _get_min_notional(self, symbol: str) -> float:
        """Best-effort min notional (quote) from exchange market metadata."""
        try:
            exchange = get_binance_client()
            # Ensure markets are loaded
            if not getattr(exchange, "markets", None):
                exchange.load_markets()
            m = exchange.market(symbol)
            limits = (m or {}).get("limits") or {}
            cost = limits.get("cost") or {}
            return float(cost.get("min") or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _gate_trade_by_model(
        *,
        symbol: str,
        direction: str,      # "buy" or "sell" (rebalance direction)
        trade_value: float,
        model_action: str,
        model_conf: float,
        min_confidence: float,
        log: BotLogger,
    ) -> float:
        """
        Decision logic that scales the trade_value based on model prediction and confidence.
        Uses soft gating to avoid blocking rebalancing completely.
        """
        # Hard blocks are dangerous for rebalancing: they can prevent the portfolio
        # from returning to target weights. We use *soft* gating instead.

        # If the model strongly contradicts the rebalance direction, reduce size.
        if model_action == "buy" and direction == "sell" and model_conf >= min_confidence:
            scaled_trade_value = trade_value * 0.25
            log.info(
                f"[{symbol}] Reducing SELL rebalance (model suggests BUY) "
                f"conf={model_conf:.2f} >= min_conf={min_confidence:.2f}. "
                f"trade_value: {trade_value:.2f} -> {scaled_trade_value:.2f}"
            )
            return scaled_trade_value

        if model_action == "sell" and direction == "buy" and model_conf >= min_confidence:
            scaled_trade_value = trade_value * 0.25
            log.info(
                f"[{symbol}] Reducing BUY rebalance (model suggests SELL) "
                f"conf={model_conf:.2f} >= min_conf={min_confidence:.2f}. "
                f"trade_value: {trade_value:.2f} -> {scaled_trade_value:.2f}"
            )
            return scaled_trade_value

        # If model is neutral/low confidence, only lightly reduce (rebalance should still act).
        if model_conf < min_confidence or model_action == "hold":
            scaled_trade_value = trade_value * 0.80
            log.info(
                f"[{symbol}] Model neutral/low confidence (action={model_action}, "
                f"conf={model_conf:.2f}). Executing reduced trade_value: "
                f"{scaled_trade_value:.2f} USDT (full={trade_value:.2f})."
            )
            return scaled_trade_value

        # Aligned or not strongly opposing: execute full.
        log.info(
            f"[{symbol}] Model not opposing. Executing full trade_value: {trade_value:.2f} USDT "
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
        """
        Execute a buy order for rebalancing, respecting cash buffers and minimum amounts.
        """
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
        # Avoid Binance NOTIONAL filter failures (min quote value per order)
        min_notional = self._get_min_notional(symbol)
        notional = amount * price
        if min_notional > 0 and notional < min_notional:
            self.log.info(
                f"[{symbol}] Skipping BUY: notional {notional:.2f} < min_notional {min_notional:.2f}"
            )
            return
        if amount < min_amount:
            self.log.info(
                f"[{symbol}] Skipping BUY: amount {amount:.8f} < "
                f"min_trade_amount {min_amount:.8f}"
            )
            return

        # Sanity: capital might have changed
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
        filled_base, avg_price, cost_quote, fee_currency, fee_cost = result

        # If fee is charged in base (e.g. BTC/ETH), net received base is reduced.
        base_asset = self._base_asset(symbol)
        net_base = filled_base
        if fee_currency == base_asset and fee_cost > 0:
            net_base = max(0.0, filled_base - fee_cost)

        fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        total_cost = cost_quote + fee_usdt
        self.capital -= total_cost

        current_pos = self.positions.get(symbol)

        prev_amount = float(current_pos.get("amount", 0.0)) if current_pos else 0.0
        prev_price = float(current_pos.get("entry_price", avg_price)) if current_pos else avg_price
        prev_fee = float(current_pos.get("entry_fee_usdt", 0.0) if current_pos else 0.0)

        new_amount = prev_amount + net_base
        if new_amount <= 0:
            self.positions[symbol] = None
            self.position_repo.remove_symbol(symbol)
        else:
            new_entry_price = (
                prev_amount * prev_price + net_base * avg_price
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

        self._log_trade(
            symbol=symbol,
            side="buy",
            price=avg_price,
            amount=net_base,
            pnl=0.0,
            fee_usdt=fee_usdt,
        )

        self.log.info(
            f"[{symbol}] Rebalance BUY: amount={net_base:.6f}, "
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
        Execute a sell order for rebalancing, updating position state and calculating PnL.
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

        # In LIVE mode, the DB-tracked position can drift from the real wallet due to fees,
        # manual trades, or rounding/step-size. Always cap SELL to the exchange free balance.
        free_on_exchange = self._get_free_balance_base(symbol)
        if free_on_exchange > 0:
            safety_free = max(0.0, free_on_exchange * 0.999)
            if safety_free < amount_available:
                self.log.warning(
                    f"[{symbol}] Tracked amount ({amount_available:.8f}) > exchange free ({free_on_exchange:.8f}). "
                    f"Capping sells to {safety_free:.8f} to avoid insufficient-balance errors."
                )
                amount_available = safety_free

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
        executed_amount, exit_price, cost_quote, fee_currency, fee_cost = result

        exit_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        proceeds = cost_quote - exit_fee_usdt
        self.capital += proceeds

        prev_amount = float(current_pos.get("amount", 0.0))
        entry_price = float(current_pos.get("entry_price", 0.0))
        entry_fee_usdt = float(current_pos.get("entry_fee_usdt", 0.0))

        # Partial PnL calculation using moved base logic
        fee_allocated = entry_fee_usdt * (executed_amount / prev_amount) if prev_amount > 0 else 0.0
        pnl_realized, _ = self._compute_realized_pnl(
            amount=executed_amount,
            entry_price=entry_price,
            entry_fee_usdt=fee_allocated,
            exit_price=exit_price,
            exit_fee_usdt=exit_fee_usdt,
        )

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

        self._log_trade(
            symbol=symbol,
            side="sell",
            price=exit_price,
            amount=executed_amount,
            pnl=pnl_realized,
            fee_usdt=exit_fee_usdt,
        )

        self.log.info(
            f"[{symbol}] Rebalance SELL: amount={executed_amount:.6f}, "
            f"exit_price={exit_price:.2f}, pnl={pnl_realized:.2f}, "
            f"capital={self.capital:.2f}"
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
        Apply the core rebalancing logic for a symbol.
        """
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

        # Model gating (OPTIONAL)
        # In pure rebalancing mode we do not apply any model-based sizing.
        if self.use_model_prediction:
            trade_value = self._gate_trade_by_model(
                symbol=symbol,
                direction=direction,
                trade_value=trade_value,
                model_action=model_action,
                model_conf=model_conf,
                min_confidence=self.config.min_confidence,
                log=self.log,
            )
        else:
            self.log.info(f"[{symbol}] Pure rebalancing mode: skipping model gating.")

        if trade_value <= 0:
            return

        if direction == "buy":
            self._rebalance_buy(symbol, price, trade_value)
        else:
            self._rebalance_sell(symbol, price, trade_value)

    def _liquidate_position_to_base(self, symbol: str, price: float) -> None:
        """
        Liquidate an open position for a symbol to the base currency (USDT).
        Required by BaseTradingBot.
        """
        current_pos = self.positions.get(symbol)
        if current_pos is None or current_pos.get("side") != "buy":
            return

        amount = float(current_pos.get("amount", 0.0))
        if amount <= 0:
            return

        free_on_exchange = self._get_free_balance_base(symbol)
        if free_on_exchange > 0:
            amount = min(amount, max(0.0, free_on_exchange * 0.999))
        if amount <= 0:
            self.log.warning(f"[{symbol}] [LIQUIDATION] Skipping: no free balance available on exchange.")
            return

        self.log.info(f"[{symbol}] [LIQUIDATION] Selling all managed amount: {amount:.6f} at price≈{price:.2f}")

        # Execute full sell
        result = self._execute_exchange_order(symbol, "sell", amount, price)
        if result is None:
            return

        executed_amount, exit_price, cost_quote, fee_currency, fee_cost = result
        exit_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        proceeds = cost_quote - exit_fee_usdt
        self.capital += proceeds

        # PnL logic
        prev_amount = float(current_pos.get("amount", 0.0))
        entry_price = float(current_pos.get("entry_price", 0.0))
        entry_fee_usdt = float(current_pos.get("entry_fee_usdt", 0.0))

        fee_allocated = entry_fee_usdt * (executed_amount / prev_amount) if prev_amount > 0 else 0.0
        pnl_realized, _ = self._compute_realized_pnl(
            amount=executed_amount,
            entry_price=entry_price,
            entry_fee_usdt=fee_allocated,
            exit_price=exit_price,
            exit_fee_usdt=exit_fee_usdt,
        )

        self.position_repo.remove_symbol(symbol)
        self.positions[symbol] = None

        self._log_trade(
            symbol=symbol,
            side="sell",
            price=exit_price,
            amount=executed_amount,
            pnl=pnl_realized,
            fee_usdt=exit_fee_usdt,
        )

        self.log.info(
            f"[{symbol}] Liquidated: amount={executed_amount:.6f}, pnl={pnl_realized:.2f}, "
            f"capital={self.capital:.2f}"
        )


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
        Process the trading decision for a single symbol.
        """
        if self._should_skip_decision_same_candle(symbol, candle_ts, price):
            return

        self.last_decision_state[symbol]["candle_ts"] = candle_ts
        self.last_decision_state[symbol]["price"] = price

        self._update_position_price(symbol, price)
        equity_before = compute_equity(self.capital, self.positions)

        # --------------------------------------------------------------
        # Model prediction (OPTIONAL)
        # If you want pure rebalancing without any predictive gating,
        # set USE_MODEL_PREDICTION=false in your env (.env.rebalancing).
        # --------------------------------------------------------------
        if self.use_model_prediction:
            # NOTE: You can comment this block to disable the model quickly.
            action, conf = self._predict_for_symbol(symbol, features)
        else:
            # Pure rebalancing mode: use a neutral confidence (0.5) to avoid
            # triggering any "low confidence" heuristics / extra logging.
            action, conf = "hold", 0.5

        self.log.info(
            f"[{symbol}] model action: {action}, conf={conf:.2f}, price={price:.2f}, "
            f"use_model_prediction={self.use_model_prediction}"
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
        if symbol == self.config.symbols[-1]:
            self._log_equity()


# ---------------------------------------------------------------------- #
# Bootstrap                                                              #
# ---------------------------------------------------------------------- #

def check_if_balance():
    exchange = get_binance_client()
    balance = exchange.fetch_balance()

    if settings.TRADING_MODE == "live":
        usdt_balance = float(balance["USDT"]["total"])
    else:
        usdt_balance = float(settings.BASE_CAPITAL)
    return usdt_balance

def run_bot_loop() -> None:
    usdt_balance = check_if_balance()
    bot = RebalancingTradingBot(
        balance=usdt_balance,
        sleep_seconds=420,  # initial hint, overridden by BotConfig if present
    )
    bot.run()

if __name__ == "__main__":
    run_bot_loop()