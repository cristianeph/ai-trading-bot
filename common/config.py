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

    MODEL_URL = os.getenv("MODEL_URL", "http://localhost:8000/predict")
    REBALANCING_SYMBOLS = ["BTC/USDT", "ETH/USDT"]

settings = Settings()