# common/trainer.py

import joblib
from pathlib import Path

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split

from bot.features import build_features_for_symbol
from .data_client import get_historical_ohlcv
from .config import settings

MODEL_PATH = Path("model") / "latest_model.pkl"


def load_data_for_symbols(symbols: list[str], limit: int = 1000) -> pd.DataFrame:
    dfs = []
    for symbol in symbols:
        ohlcv = get_historical_ohlcv(symbol, settings.TIMEFRAME, limit=limit)
        df = build_features_for_symbol(ohlcv, symbol, settings.TIMEFRAME)
        print("Data loaded by a single df: ", len(df))
        dfs.append(df)
    print("DFs loaded: ", len(dfs))
    return pd.concat(dfs)


def train_and_save_model():
    df = load_data_for_symbols(settings.SYMBOLS, limit=1000)

    feature_cols = ["ma_ratio", "rsi_14", "vol_20"]
    X = df[feature_cols]
    y = df["y"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=False
    )

    model = LogisticRegression()
    model.fit(X_train, y_train)

    score = model.score(X_test, y_test)
    print(f"Accuracy test: {score:.3f}")

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    print(f"Modelo guardado en: {MODEL_PATH.absolute()}")


if __name__ == "__main__":
    train_and_save_model()
