# src/features.py

import pandas as pd
import numpy as np


def ohlcv_to_df(ohlcv, symbol: str = "UNKNOWN", timeframe: str = "1h") -> pd.DataFrame:
    """
    Convierte la lista OHLCV de ccxt en un DataFrame con índice datetime.
    """
    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df["symbol"] = symbol
    df["timeframe"] = timeframe
    return df


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI simple implementado a mano.
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    rs = avg_gain / (avg_loss.replace(0, np.nan))
    rsi = 100 - (100 / (1 + rs))
    return rsi


def build_features_for_symbol(ohlcv, symbol: str = "BTC/USDT", timeframe: str = "1h") -> pd.DataFrame:
    """
    OHLCV -> DataFrame con columnas de features para el modelo.
    Aquí puedes ir agregando más cosas según necesites.
    """
    df = ohlcv_to_df(ohlcv, symbol, timeframe)

    # Medias móviles
    df["ma_fast"] = df["close"].rolling(window=10).mean()
    df["ma_slow"] = df["close"].rolling(window=50).mean()
    df["ma_ratio"] = df["ma_fast"] / df["ma_slow"]

    # RSI
    df["rsi_14"] = rsi(df["close"], period=14)

    # Volatilidad simple
    df["return_1"] = df["close"].pct_change()
    df["vol_20"] = df["return_1"].rolling(window=20).std()

    # El target para entrenamiento (no se usa en producción)
    # Target: 1 si el retorno de la próxima vela es positivo, 0 si no.
    df["future_return_1"] = df["close"].shift(-1) / df["close"] - 1
    df["y"] = (df["future_return_1"] > 0).astype(int)

    # Drop filas con NaN iniciales
    df = df.dropna()

    return df
