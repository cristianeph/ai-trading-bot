# common/trainer.py

import joblib
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from bot.features import build_features_for_symbol
from common.data_client import get_historical_ohlcv
from common.config import settings

MODEL_PATH = Path("model") / "latest_model.pkl"


def load_data_for_symbols(symbols: list[str], limit: int) -> pd.DataFrame:
    dfs = []
    for symbol in symbols:
        ohlcv = get_historical_ohlcv(symbol, settings.TIMEFRAME, limit=limit)
        df = build_features_for_symbol(ohlcv, symbol, settings.TIMEFRAME)
        print("Data loaded by a single df: ", len(df))
        dfs.append(df)
    print("DFs loaded: ", len(dfs))
    return pd.concat(dfs)


def train_and_save_model():
    # 1) Cargar datos históricos
    df = load_data_for_symbols(
        settings.SYMBOLS,
        limit=settings.TRAIN_HISTORY_LIMIT
    )

    feature_cols = ["ma_ratio", "rsi_14", "vol_20"]

    # 2) Limpiar NaN e infinitos
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=feature_cols + ["y"])

    print(f"[trainer] Dataset después de limpieza: {len(df)} filas")

    # 2b) Recortar valores extremos (clipping por percentiles)
    for col in feature_cols:
        q_low = df[col].quantile(0.01)
        q_high = df[col].quantile(0.99)
        df[col] = df[col].clip(lower=q_low, upper=q_high)

    # 2c) Límites duros por dominio
    # RSI siempre entre 0 y 100
    df["rsi_14"] = df["rsi_14"].clip(lower=0.0, upper=100.0)

    # ma_ratio en un rango razonable, por ejemplo [0, 5]
    df["ma_ratio"] = df["ma_ratio"].clip(lower=0.0, upper=5.0)

    # vol_20 no negativa y acotada; aquí usamos percentil 99.5 como techo adicional
    vol_high = df["vol_20"].quantile(0.995)
    df["vol_20"] = df["vol_20"].clip(lower=0.0, upper=vol_high)

    # 2d) Tirar filas con valores todavía absurdos por seguridad
    max_abs = 1e4
    mask = (df[feature_cols].abs() < max_abs).all(axis=1)
    df = df[mask]

    # Asegurarnos de nuevo de que no quedan NaNs
    df = df.dropna(subset=feature_cols + ["y"])

    print(f"[trainer] Dataset después de clip: {len(df)} filas")

    # 3) Dividir en X/y
    X = df[feature_cols].values
    y = df["y"].values

    # 4) Split train/test sin mezclar temporalmente
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=False
    )

    # 5) Pipeline: escalado + logistic regression robusta
    model = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    max_iter=1000,
                    class_weight="balanced",
                ),
            ),
        ]
    )

    model.fit(X_train, y_train)

    score = model.score(X_test, y_test)
    print(f"[trainer] Accuracy test: {score:.3f}")

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    print(f"[trainer] Modelo guardado en: {MODEL_PATH.absolute()}")


if __name__ == "__main__":
    train_and_save_model()
