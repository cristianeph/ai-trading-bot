import time
from typing import Dict, Optional, Any

import requests
import pandas as pd

from common.config import settings
from common.data_client import get_historical_ohlcv, place_order
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

        amount = position_value / price

        # Envío de orden (paper o live según settings)
        place_order(symbol, "buy", amount)

        # Reducimos el capital en el valor de la posición (lo invertimos)
        self.capital -= position_value

        self.positions[symbol] = {
            "side": "buy",
            "amount": amount,
            "entry_price": price,
            "last_price": price,
        }

        self.storage.log_trade(
            symbol=symbol,
            side="buy",
            price=price,
            amount=amount,
            mode=settings.TRADING_MODE,
            pnl=0.0,
        )
        print(f"[{symbol}] Apertura long: amount={amount:.6f}, capital={self.capital:.2f}")

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

        place_order(symbol, "sell", amount)

        proceeds = amount * price
        cost = amount * entry_price
        pnl = proceeds - cost

        # Recuperamos el capital invertido más el PnL
        self.capital += proceeds

        self.positions[symbol] = None

        self.storage.log_trade(
            symbol=symbol,
            side="sell",
            price=price,
            amount=amount,
            mode=settings.TRADING_MODE,
            pnl=pnl,
        )
        print(
            f"[{symbol}] Cierre long: amount={amount:.6f}, "
            f"entry={entry_price:.2f}, exit={price:.2f}, pnl={pnl:.2f}, capital={self.capital:.2f}"
        )

    def _log_equity(self) -> None:
        """
        Calcula y registra el equity actual.
        """
        equity = compute_equity(self.capital, self.positions)
        self.storage.log_equity(equity)
        print(f"[BOT] Equity actual: {equity:.2f}")

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
    import ccxt
    exchange = ccxt.binance({
        "apiKey": settings.BINANCE_API_KEY,
        "secret": settings.BINANCE_API_SECRET,
    })
    exchange.set_sandbox_mode(True)
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

    run_bot_loop()
