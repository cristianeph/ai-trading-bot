# common/exceptions.py

class TradingBotError(Exception):
    """Base exception for all trading bot errors."""
    pass

class ExchangeError(TradingBotError):
    """Raised when the exchange returns an error."""
    pass

class InsufficientBalanceError(TradingBotError):
    """Raised when there is not enough balance to place an order."""
    pass

class ConnectivityError(TradingBotError):
    """Raised when there are network/connectivity issues with the exchange."""
    pass

class OrderError(TradingBotError):
    """Raised when an order fails for other reasons (e.g. min notional)."""
    pass
