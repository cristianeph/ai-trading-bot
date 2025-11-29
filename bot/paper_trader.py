import time
from typing import Dict, Optional, Any

import requests
import pandas as pd

from common.config import settings
from common.data_client import get_historical_ohlcv, place_order, get_binance_client
from bot.features import build_features_for_symbol
from bot.storage import Storage

MODEL_URL = "http://localhost:8000/predict"  # nombre del servicio en docker-compose


def get_model_action(features: list[float]) -> tuple[str, float]:
    """
    Envía el vector de features al microservicio del modelo y devuelve
    (acción, confianza).

    acción: "buy" | "sell" | "hold"
    confianza: probabilidad asociada a la acción elegida.Ò
    """
    payload = {"features": features}
    try:
        resp = requests.post(MODEL_URL, json=payload, timeout=2)
        resp.raise_for_status()
    except requests.RequestException as exc:
        # En un MVP preferimos no romper el loop; devolvemos "hold"
        print(f"[MODEL] Error al consultar el modelo: {exc}")
        return "hold", 0.0

    data = resp.json()
    action = data.get("action", "hold")
    confidence = float(data.get("confidence", 0.0))
    return action, confidence


def compute_equity(cash: float, positions: Dict[str, Optional[Dict[str, Any]]]) -> float:
    """
    Equity = efectivo (cash) + valor de todas las posiciones abiertas.

    Cada posición es un dict con:
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
    Bot de trading principal.

    Encapsula:
      - capital (cash),
      - posiciones por símbolo,
      - acceso a storage,
      - el loop principal de ejecución.
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
        Descarga OHLCV, construye el DataFrame de features y devuelve
        (latest_row, price, features) o None si algo falla.
        """
        ohlcv = get_historical_ohlcv(symbol, settings.TIMEFRAME, limit=200)
        if not ohlcv:
            print(f"[{symbol}] No se recibieron datos OHLCV.")
            return None

        df: pd.DataFrame = build_features_for_symbol(
            ohlcv, symbol, settings.TIMEFRAME
        )
        if df.empty:
            print(f"[{symbol}] DataFrame de features vacío.")
            return None

        latest_row = df.iloc[-1]
        features = latest_row[["ma_ratio", "rsi_14", "vol_20"]].tolist()
        price: float = float(latest_row["close"])

        return latest_row, price, features

    def _update_position_price(self, symbol: str, price: float) -> None:
        """
        Actualiza el último precio conocido de la posición de un símbolo, si existe.
        """
        current_pos = self.positions.get(symbol)
        if current_pos is not None:
            current_pos["last_price"] = price

    def _maybe_close_position_by_pnl(self, symbol: str, price: float) -> bool:
        """
        Revisa el PnL latente de la posición y, si supera ciertos umbrales
        de take profit (tp_pct) o stop loss (sl_pct), fuerza un cierre (SELL).

        Devuelve True si se cerró la posición, False en caso contrario.
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
                f"[{symbol}] TP alcanzado ({unrealized_pnl_pct:.3%}), "
                f"forzando SELL por gestión de riesgo."
            )
            # Forzamos un sell con confianza 1.0 (bypass del modelo)
            self._handle_sell(symbol, price, confidence=1.0)
            return True

        # Stop loss
        if unrealized_pnl_pct <= self.sl_pct:
            print(
                f"[{symbol}] SL alcanzado ({unrealized_pnl_pct:.3%}), "
                f"forzando SELL por gestión de riesgo."
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
        Intenta abrir una posición long si no existe ya una posición para el símbolo.
        """
        if confidence <= self.min_confidence:
            return

        current_pos = self.positions.get(symbol)
        if current_pos is not None:
            # Ya hay posición abierta, no abrimos otra
            return

        position_value = self.capital * settings.POSITION_SIZE_PCT
        if position_value <= 0:
            print(f"[{symbol}] position_value no válido: {position_value}")
            return

        # Cantidad teórica que queremos comprar
        amount = position_value / price

        # Envío de orden (paper o live según settings)
        order = place_order(symbol, "buy", amount)

        # Determinar datos reales de ejecución (si los hay)
        if isinstance(order, dict):
            executed_amount = float(order.get("amount") or amount)
            avg_price = float(order.get("average") or order.get("price") or price)
            cost = float(order.get("cost") or (executed_amount * avg_price))
            fee_info = order.get("fee") or {}
            fee_currency = fee_info.get("currency")
            fee_cost = float(fee_info.get("cost") or 0.0)
            # Para el MVP solo descontamos fee si viene en USDT
            entry_fee_usdt = fee_cost if fee_currency == "USDT" else 0.0
        else:
            executed_amount = amount
            avg_price = price
            cost = executed_amount * avg_price
            entry_fee_usdt = 0.0

        # Reducimos el capital en el costo real de la operación + fee en USDT
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
        Intenta cerrar una posición long existente.
        """
        if confidence <= self.min_confidence:
            return

        current_pos = self.positions.get(symbol)
        if current_pos is None or current_pos.get("side") != "buy":
            # No hay posición long que cerrar
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

        # Coste total de entrada (incluyendo fee de entrada)
        cost = amount * entry_price + entry_fee_usdt

        # Recuperamos el capital neto de fees de salida
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
        Calcula y registra el equity actual.
        """
        equity = compute_equity(self.capital, self.positions)
        self.storage.log_equity(equity)
        print(f"[BOT] Equity actual: {equity:.2f}")

    def _log_position_status(self, symbol: str) -> None:
        """
        Muestra un resumen legible del estado de la posición para un símbolo:
          - capital libre en USDT,
          - BTC invertido (si lo hay) y su valor actual en USDT,
          - PnL latente de esa posición.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            print(
                f"[{symbol}] Sin posición abierta. "
                f"Capital libre: {self.capital:.2f} USDT"
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
            f"[{symbol}] Posición: {amount:.6f} BTC, "
            f"invertido≈{invested_usdt:.2f} USDT, "
            f"valor actual≈{current_value_usdt:.2f} USDT, "
            f"PnL latente≈{unrealized_pnl:.2f} USDT, "
            f"capital libre={self.capital:.2f} USDT"
        )

    def _process_symbol(self, symbol: str) -> None:
        """
        Ejecuta un ciclo completo de:
          - obtener mercado,
          - consultar modelo,
          - actualizar posición,
          - registrar equity
        para un símbolo concreto.
        """
        market_state = self._fetch_latest_market_state(symbol)
        if market_state is None:
            return

        latest_row, price, features = market_state

        # Acción del modelo
        action, conf = get_model_action(features)
        print(
            f"[{symbol}] acción modelo: {action}, conf={conf:.2f}, precio={price:.2f}"
        )

        # Actualizar último precio de posición (si existe)
        self._update_position_price(symbol, price)

        # Lógica de trading
        if action == "buy":
            self._handle_buy(symbol, price, conf)
        elif action == "sell":
            self._handle_sell(symbol, price, conf)

        # Registrar equity después de procesar el símbolo
        self._log_position_status(symbol)
        self._log_equity()

    def run(self) -> None:
        """
        Loop principal del bot. Recorre los símbolos definidos en settings.SYMBOLS
        y ejecuta la lógica de trading en intervalos definidos por self.sleep_seconds.
        """
        print("[BOT] Iniciando loop de trading...")
        try:
            while True:
                for symbol in settings.SYMBOLS:
                    try:
                        self._process_symbol(symbol)
                    except Exception as symbol_exc:  # noqa: BLE001
                        # No queremos que un símbolo rompa todo el loop
                        print(f"[{symbol}] Error en el loop del símbolo: {symbol_exc}")

                # Esperar al siguiente ciclo (ej. cada 5 min)
                time.sleep(self.sleep_seconds)

        except KeyboardInterrupt:
            print("[BOT] Interrupción por teclado. Cerrando bot...")

        finally:
            try:
                self.storage.close()
            except Exception:
                pass
            print("[BOT] Bot detenido limpiamente.")


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
