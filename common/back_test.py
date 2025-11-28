# common/backtest.py

import joblib
import numpy as np

from pathlib import Path
from bot.features import build_features_for_symbol
from common.data_client import get_historical_ohlcv
from common.config import settings

MODEL_PATH = Path("model") / "latest_model.pkl"


def backtest_symbol(symbol: str):
    model = joblib.load(MODEL_PATH)
    ohlcv = get_historical_ohlcv(symbol, settings.TIMEFRAME, limit=200)
    df = build_features_for_symbol(ohlcv, symbol, settings.TIMEFRAME)

    feature_cols = ["ma_ratio", "rsi_14", "vol_20"]
    X = df[feature_cols]  # DataFrame con nombres de columnas
    closes = df["close"].values

    # Cash disponible en USDT y posición en la coin (BTC)
    cash = settings.BASE_CAPITAL
    position = 0.0  # cantidad de coin (BTC)
    entry_price = 0.0

    trades = 0  # contador de ciclos completos buy → sell
    max_equity = cash
    min_equity = cash

    for i in range(len(df)):
        # conservamos un DataFrame de una sola fila para preservar los nombres de columnas
        x_i = X.iloc[[i]]
        proba = model.predict_proba(x_i)[0]
        # proba[1] = prob de y=1 (sube), proba[0] = y=0 (baja/no sube)
        p_up = proba[1]
        price = closes[i]

        # BUY: abrir long si no hay posición y la probabilidad de subida es suficientemente alta
        if position == 0 and p_up > 0.51:
            position_value = cash * settings.POSITION_SIZE_PCT
            if position_value <= 0:
                continue
            amount = position_value / price

            # invertimos parte del cash en BTC
            cash -= position_value
            position = amount
            entry_price = price
            # BUY abierto, no contamos trade aún

        # SELL: cerrar long si hay posición y la probabilidad de subida es baja
        elif position > 0 and p_up < 0.48:
            proceeds = position * price
            cash += proceeds  # volvemos a USDT
            position = 0.0
            entry_price = 0.0
            trades += 1

        # seguimiento del equity durante el backtest
        equity_now = cash + (position * price)
        if equity_now > max_equity:
            max_equity = equity_now
        if equity_now < min_equity:
            min_equity = equity_now

    # cerrar si queda algo abierto al último precio y calcular capital final
    if position > 0:
        cash += position * closes[-1]
        position = 0.0

    capital_final = cash
    pnl_total = capital_final - settings.BASE_CAPITAL
    pnl_pct = (pnl_total / settings.BASE_CAPITAL) * 100 if settings.BASE_CAPITAL > 0 else 0
    max_drawdown = ((min_equity - max_equity) / max_equity) * 100 if max_equity > 0 else 0

    print(f"Backtest {symbol} -> capital final: {capital_final:.2f}")
    print("--- Resumen ---")
    print(f"Capital inicial: {settings.BASE_CAPITAL:.2f} USDT")
    print(f"Capital final:   {capital_final:.2f} USDT")
    print(f"PNL total:       {pnl_total:.2f} USDT")
    print(f"Rendimiento:     {pnl_pct:.2f}%")
    print(f"Trades ejecutados: {trades}")
    print(f"Max Equity:      {max_equity:.2f}")
    print(f"Max Drawdown:    {max_drawdown:.2f}%")


if __name__ == "__main__":
    for s in settings.SYMBOLS:
        backtest_symbol(s)
