# Rebalancing Bot Tech Stack & Business Rules

This document provides a comprehensive overview of the **Rebalancing Trading Bot**, detailing its technical architecture, data flow, business logic, and maintenance requirements.

## 1. Overview
The Rebalancing Bot is designed to maintain a diversified crypto portfolio by automatically adjusting asset weights based on target allocations. It integrates machine learning predictions to gate trades, ensuring that rebalancing only occurs when aligned with market sentiment.

## 2. Tech Stack
- **Language**: Python 3.10+
- **Database**: MariaDB / MySQL (via SQLAlchemy & SQLModel)
- **Data Processing**: Pandas, NumPy
- **ML Integration**: FastAPI-based Model Servers (BTC/ETH models)
- **Exchange Integration**: CCXT (via a custom `DataClient`)
- **Infrastructure**: Docker & Docker Compose
- **Migrations**: Alembic

## 3. Data Flow

### 3.1 Data Writing (Outputs)
The bot writes several types of data to the database for tracking, auditing, and future UI visualization:
- **Trades (`trade` table)**: Records every buy/sell execution, including price, amount, fees, PnL, and USDT equivalents at the time of trade.
- **Equity (`equity` table)**: Periodic snapshots of total portfolio value in USDT.
- **Decisions (`decision` table)**: Logs every iteration's analysis for each symbol, including model predictions, technical indicators (RSI, MA Ratio, Volatility), and whether an action was taken or skipped.
- **Drift Logs (`driftlog` table)**: Tracks "Portfolio Drift" — the cumulative deviation of current weights from target weights.
- **Open Positions (`openposition` table)**: Maintains a real-time view of current holdings and their entry prices for PnL calculation.

### 3.2 Data Processing & Reading (Inputs)
- **Market Data**: Fetches OHLCV (Open, High, Low, Close, Volume) data from exchanges via `DataClient`.
- **ML Predictions**: Calls external Model Servers to get "Buy/Sell/Hold" signals and confidence scores.
- **Bot Configuration**: Reads operational parameters (thresholds, buffers, etc.) from the `botconfig` table.
- **Target Weights**: Dynamically calculates targets by reading the `targetweightschedule` table and fallback weights in `botconfig`.
- **Account Balances**: Queries the exchange for current asset and USDT balances.

## 4. Business Rules

### 4.1 Core Rebalancing Logic
- **Target Allocation**: The bot attempts to keep each asset at a specific percentage of the total portfolio value.
- **Rebalance Threshold**: A trade is only triggered if the deviation from the target weight exceeds a threshold (e.g., 2%).
- **Dynamic Thresholds**: During high volatility (measured by `vol_20`), the threshold automatically expands to reduce "churn" (unnecessary trading fees).

### 4.2 Trade Gating & Safety
- **ML Gating**: If the ML model strongly contradicts the rebalancing direction (e.g., model says "Sell" but bot wants to "Buy" to rebalance), the trade is skipped or reduced.
- **Circuit Breaker (Max Drawdown)**: If the portfolio loses more than a configured percentage (e.g., 20%) from its peak, the bot stops trading and enters a safety mode.
- **Cash Buffer**: The bot always maintains a minimum USDT reserve (e.g., 5 USDT or 1% of equity) to cover trading fees and prevent execution failures.
- **Smart Scaling**: The trade size is scaled based on how far the asset has drifted. Small deviations result in smaller trades, while large deviations trigger larger corrections.

### 4.3 Realistic Simulation (Dry Run)
- When running in "Paper" mode, the bot simulates **slippage** and **transaction fees** to provide a realistic performance estimate.

## 5. Maintenance & User Configuration

The following tables and parameters are intended to be managed by the user (or via a future Web UI).

### 5.1 Parameter Table (`botconfig`)
The bot reads its behavior from the `botconfig` table. Users should maintain these keys:
- `sleep_seconds`: How long the bot waits between iterations.
- `rebalance_threshold_pct`: The base sensitivity for rebalancing (e.g., `0.02` for 2%).
- `max_trade_pct`: Maximum percentage of equity to trade in a single order.
- `cash_buffer_min_usdt`: Minimum USDT to keep for fees.
- `dynamic_threshold_enabled`: Set to `true` to enable volatility-aware thresholds.
- `target_weight_[SYMBOL]`: Default target weight for an asset (e.g., `target_weight_BTC` = `0.4`).

### 5.2 Weight Scheduling (`targetweightschedule`)
Used to plan portfolio shifts over time (e.g., increasing ETH exposure for a specific week).
- **Users must populate**: `symbol`, `target_weight`, `start_time`, and `end_time`.
- The bot automatically detects active schedules; if multiple overlap, it uses the one with the latest `start_time`.

### 5.3 Operational Readiness
- **Database Migrations**: Ensure `alembic upgrade head` is run after updates to apply schema changes for drift tracking and scheduling.
- **Model Servers**: Ensure model servers for all configured symbols are reachable at the URLs defined in environment variables.
