# common/data_client.py
import time
from typing import Optional, List, Tuple, Dict, Any
import ccxt
from common.config import settings
from common.exceptions import (
    ExchangeError,
    InsufficientBalanceError,
    ConnectivityError,
    OrderError,
)


def get_binance_client():
    """
    Crea y devuelve el cliente de ccxt para Binance.
    Usa testnet o producción según settings.BINANCE_TESTNET.
    """
    params = {
        "apiKey": settings.BINANCE_API_KEY,
        "secret": settings.BINANCE_API_SECRET,
        "options": {"defaultType": "spot"},  # spot, no futuros
    }

    exchange = ccxt.binance(params)

    # Testnet (sandbox)
    if settings.BINANCE_TESTNET:
        # Para Binance, ccxt soporta sandbox_mode para testnet
        exchange.set_sandbox_mode(True)

    return exchange


def get_historical_ohlcv(symbol: str, timeframe: str = None, limit: int = 500):
    """
    Descarga OHLCV histórico desde Binance con paginación si es necesario.

    - Binance (vía ccxt) normalmente limita a 1000 velas por llamada.
    - Esta función hace varias llamadas hasta juntar `limit` registros
      (o hasta que el exchange deje de devolver datos).

    Devuelve una lista de velas [timestamp, open, high, low, close, volume].
    """
    exchange = get_binance_client()
    tf = timeframe or settings.TIMEFRAME

    # tamaño máximo por llamada (Binance suele limitar a 1000)
    max_batch = 1000

    # ms por vela según timeframe (parse_timeframe devuelve segundos)
    tf_ms = exchange.parse_timeframe(tf) * 1000

    # Empezamos "limit * timeframe" velas atrás
    now_ms = exchange.milliseconds()
    since = now_ms - limit * tf_ms

    all_candles = []

    while len(all_candles) < limit:
        batch_limit = min(max_batch, limit - len(all_candles))

        ohlcv = exchange.fetch_ohlcv(
            symbol,
            timeframe=tf,
            since=since,
            limit=batch_limit,
        )

        if not ohlcv:
            # No hay más datos
            break

        all_candles.extend(ohlcv)

        # Avanzar el cursor en el tiempo para la siguiente página:
        last_ts = ohlcv[-1][0]
        since = last_ts + tf_ms

        # Si devuelve menos de lo pedido, probablemente no hay más histórico
        if len(ohlcv) < batch_limit:
            break

        # Respetar rate limit para no abusar del exchange
        if exchange.rateLimit:
            time.sleep(exchange.rateLimit / 1000)

    # Asegurar que devolvemos como máximo `limit` velas (por si hay solape)
    if len(all_candles) > limit:
        all_candles = all_candles[-limit:]

    return all_candles


def place_order(
    symbol: str,
    side: str,
    amount: float,
    order_type: str = "market",
    price: Optional[float] = None,
    params: Optional[dict] = None,
):
    """
    Crea una orden en Binance SOLO si TRADING_MODE='live'.
    Si TRADING_MODE='paper', simula la orden y no toca el exchange.

    - side: 'buy' o 'sell'
    - order_type: 'market' o 'limit'
    - price: Requerido para órdenes limit
    - params: Parámetros extra para ccxt (ej: timeInForce, postOnly)
    """
    if params is None:
        params = {}

    if settings.TRADING_MODE == "paper":
        print(f"[PAPER] {side.upper()} {order_type.upper()} {amount} {symbol} @ {price or 'MARKET'} (no se envía a Binance)")
        return {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "amount": float(amount),
            "price": price,
            "average": price,
            "cost": (price * amount) if price else None,
            "fee": {"currency": "USDT", "cost": 0.0},
            "status": "closed",
        }

    exchange = get_binance_client()

    try:
        if order_type.lower() == "market":
            print(f"[LIVE] Enviando orden MARKET {side.upper()} {amount} {symbol}...")
            order = exchange.create_market_order(symbol, side, amount, params)
        elif order_type.lower() == "limit":
            if price is None:
                raise OrderError("Price is required for limit orders")
            print(f"[LIVE] Enviando orden LIMIT {side.upper()} {amount} {symbol} @ {price}...")
            order = exchange.create_limit_order(symbol, side, amount, price, params)
        else:
            raise OrderError(f"Unsupported order type: {order_type}")

        print(f"[LIVE] Orden ejecutada: {order}")
        return order

    except ccxt.InsufficientFunds as e:
        raise InsufficientBalanceError(f"Insufficient funds: {e}") from e
    except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
        raise ConnectivityError(f"Connectivity issue: {e}") from e
    except ccxt.ExchangeError as e:
        raise ExchangeError(f"Exchange error: {e}") from e
    except Exception as e:
        if "minNotional" in str(e):
            raise OrderError(f"Order below minimum notional: {e}") from e
        raise OrderError(f"Unexpected order error: {e}") from e
