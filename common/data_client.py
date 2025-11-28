# common/data_client.py
import time
import ccxt
from common.config import settings


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


def place_order(symbol: str, side: str, amount: float):
    """
    Crea una orden en Binance SOLO si TRADING_MODE='live'.
    Si TRADING_MODE='paper', simula la orden y no toca el exchange.
    side: 'buy' o 'sell'
    """
    if settings.TRADING_MODE == "paper":
        print(f"[PAPER] {side.upper()} {amount} {symbol} (no se envía a Binance)")
        return {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "amount": float(amount),
            "price": None,
            "average": None,
            "cost": None,
            "fee": {"currency": "USDT", "cost": 0.0},
        }

    exchange = get_binance_client()

    # En un MVP usamos órdenes de mercado
    print(f"[LIVE] Enviando orden {side.upper()} {amount} {symbol}...")
    order = exchange.create_market_order(symbol, side, amount)
    print(f"[LIVE] Orden ejecutada: {order}")
    return order
