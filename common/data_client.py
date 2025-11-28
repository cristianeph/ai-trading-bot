# common/data_client.py

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
    Descarga OHLCV histórico desde Binance.
    Para backtests / features usamos siempre el cliente de lectura.
    """
    exchange = get_binance_client()
    tf = timeframe or settings.TIMEFRAME
    ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=limit)
    return ohlcv


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
