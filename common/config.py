import os
from dotenv import load_dotenv

load_dotenv()  # carga .env en desarrollo

class Settings:
    BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")
    BINANCE_TESTNET: bool = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
    TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")  # "paper" | "live"
    SYMBOLS = ["BTC/USDT"]
    BASE_CAPITAL = float(os.getenv("BASE_CAPITAL", "1000"))
    POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.1"))  # 10%

    TIMEFRAME = "5m" # 15m
    TRAIN_HISTORY_LIMIT = int(os.getenv("TRAIN_HISTORY_LIMIT", "6000"))
    USE_MODEL_PREDICTION: str = os.getenv("USE_MODEL_PREDICTION", "false")

    # default symbol BTC for foundational bot
    MODEL_URL = os.getenv("MODEL_URL_BTC", "http://localhost:8001/predict")

    REBALANCING_SYMBOLS = ["BTC/USDT", "ETH/USDT"]

    # Bot Foundational Constants
    DRASTIC_MOVE_THRESHOLD = 0.0005
    HOLD_CONF_MARGIN = 0.10
    HOLD_SAMPLE_EVERY_MIN = 10
    DEFAULT_CASH_BUFFER_PCT = 0.05
    DEFAULT_FEE_RESERVE_USDT = 2.0
    DEFAULT_TP_PCT = 0.003
    DEFAULT_SL_PCT = -0.004
    MIN_TRADE_AMOUNT = {"BTC/USDT": 0.00001}

settings = Settings()