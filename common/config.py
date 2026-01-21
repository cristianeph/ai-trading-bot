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
    MIN_TRADE_AMOUNT = {"BTC/USDT": 0.00001, "ETH/USDT": 0.01}
    DEFAULT_MAX_DRAWDOWN_PCT = 0.10  # 10% stop bot if equity falls by this much

    # Bot Rebalancing Constants
    REB_REBALANCE_THRESHOLD_PCT = 0.02
    REB_MAX_TRADE_PCT = 0.25
    REB_SMART_SCALE_MAX_DELTA_PCT = 0.10
    REB_SMART_VOL_ENABLED = True
    REB_SMART_VOL_MEDIUM = 0.02
    REB_SMART_VOL_HIGH = 0.04
    REB_CASH_BUFFER_PCT = 0.10

    # Bot Futures Constants
    FUT_DEFAULT_LEVERAGE = 2
    FUT_DEFAULT_MARGIN_MODE = "isolated"
    FUT_DEFAULT_TP_PCT = 0.003
    FUT_DEFAULT_SL_PCT = -0.004
    FUT_ALLOW_SHORTS = False

    # Feature names to avoid magic strings
    FEATURE_COLUMNS = ["ma_ratio", "rsi_14", "vol_20"]

settings = Settings()