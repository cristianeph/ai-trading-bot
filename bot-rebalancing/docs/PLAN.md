# Project Enhancement Plan - Release 1.0

This document tracks the release items for the crypto bot project enhancements, including technical task breakdowns, database migrations, and cross-bot impact analysis.

---

## 1. Release Tracking Table

### Phase 1: Core Trading, Stability & Transparency

| Item ID | Priority | Task Name | Status | Component |
| :--- | :--- | :--- | :--- | :--- |
| REL-001 | **Critical** | Enhanced Trade Tracking | COMPLETED | Common (Storage/Base) |
| REL-003 | **High** | Centralized Circuit Breakers | COMPLETED | Common (Base Bot) |
| REL-002 | **High** | Robust Execution Layer | COMPLETED | Common (Data Client) |
| REL-004 | **Medium** | Performance Service | COMPLETED | Common (New Service) |
| REL-005 | **Medium** | Dynamic Rebalancing Thresholds | COMPLETED | Bot Rebalancing |
| REL-011 | **Medium** | Drift Reporting | COMPLETED | Bot Rebalancing |
| REL-007 | **Low** | Smart Cash Buffer | COMPLETED | Bot Rebalancing |
| REL-008 | **Low** | Target Weight Scheduling | COMPLETED | Bot Rebalancing |
| REL-009 | **Low** | Dry Run Improvements | COMPLETED | Common/Rebal |

### Phase 2: Monitoring, API & UI

| Item ID | Priority | Task Name | Status | Component |
| :--- | :--- | :--- | :--- | :--- |
| REL-006 | **High** | Monitoring & Health API | Deferred (Phase 2) | Common (Monitoring) |
| REL-010 | **Medium** | Web UI (v1) Dashboard | Deferred (Phase 2) | UI (New) |

---

## 2. Technical Task Breakdown

### Phase 1: Core Trading, Stability & Transparency

### REL-001: Enhanced Trade Tracking
**Objective:** Capture rich metadata for every trade to support PnL analysis and UI visualization.

- **Storage Changes (Common Layer):**
    - Update `Trade` model in `common/storage.py` to include:
        - `invested_usdt_equivalent`: float
        - `usdt_rate`: float (price of asset in USDT at trade time)
        - `fee_usdt`: float
        - `reference_id`: string (UUID for grouping trades)
    - Update `Storage.log_trade()` signature to accept these new fields.
    - **Backward Compatibility:** Default new fields to `None` or `0.0` to avoid breaking existing calls.
- **Base Bot Changes (Common Layer):**
    - Update `BaseTradingBot` and `FuturesTradingBotBase` to calculate or fetch `usdt_rate` before logging trades.
- **Migration:**
    - Create Alembic migration: `migrations/versions/XXXX_enhanced_trade_tracking.py`.
    - Script to add columns to `trade` table.

### REL-002: Robust Execution Layer
**Objective:** Support Limit Orders and standardize error handling.

- **Data Client Changes (Common Layer):**
    - Modify `common/data_client.py:place_order()` to support `order_type` (market/limit).
    - Add support for `params` (e.g., `timeInForce`: `PostOnly`, `IOC`).
- **Standardized Exceptions:**
    - Define custom exceptions in `common/exceptions.py` (e.g., `InsufficientBalanceError`, `ExchangeError`).
    - Wrap `ccxt` calls in `place_order` with try-except blocks that map to these custom exceptions.
- **Impact on Bots:**
    - `bot-foundational`, `bot-rebalancing`, and `bot-futures` need to be updated if they rely on the return structure of `place_order` or if they want to use Limit orders.

### REL-003: Centralized Circuit Breakers
**Objective:** Unify Max Drawdown and safety logic.

- **Base Bot Changes (Common Layer):**
    - Move `_check_max_drawdown` logic from `bot_rebalancing.py` and `bot_foundational.py` to `BaseTradingBot`.
    - Use `self.storage.get_bot_config_float("max_drawdown_pct", ...)` to make it configurable per bot.
- **Refactor:**
    - Remove redundant implementations in individual bots.

### REL-004: Performance Service
**Objective:** Advanced PnL calculation.

- **New File:** `common/performance.py`.
- **Logic:**
    - Calculate Weighted Average Entry Price (WAEP).
    - Calculate Realized PnL using the new `Trade` metadata.
    - Function to aggregate trades by `reference_id`.

### REL-005: Dynamic Rebalancing Thresholds
**Objective:** Reduce churn in volatile markets.

- **Bot Rebalancing Changes:**
    - Modify `bot_rebalancing.py` to calculate current volatility (e.g., using ATR or StdDev of recent OHLCV).
    - Adjust `rebalance_threshold_pct` dynamically: `effective_threshold = base_threshold * (1 + volatility_factor)`.

### REL-011: Drift Reporting
**Objective:** Monitor portfolio health between rebalances.

- **Bot Rebalancing Changes:**
    - Calculate "Portfolio Drift" (Sum of absolute differences between actual and target weights).
    - Log this metric in every loop iteration, even if no trade is made.
    - Update `Storage` to have a `drift_log` table.

### REL-007: Smart Cash Buffer
**Objective:** Optimize cash reserves for fees and future trades.

- **Bot Rebalancing Changes:**
    - Logic to calculate "Required Fee Reserve" based on planned trade volume and current exchange fee rates.
    - Implement `calculate_available_cash()` that subtracts this reserve before allocating to assets.

### REL-008: Target Weight Scheduling
**Objective:** Enable time-based or trend-following allocation shifts.

- **Storage Changes:**
    - New table `target_weight_schedule` (or JSON field in `bot_config`).
- **Bot Rebalancing Changes:**
    - Add logic to check for active schedules in `load_config()`.
    - If a schedule is active for current timestamp, override `target_weights`.

### REL-009: Dry Run Improvements
**Objective:** Realistic simulation of trading costs.

- **Common Layer Changes (`BaseTradingBot` / `DataClient`):**
    - Add `simulate_slippage` and `simulate_fees` parameters to paper trading logic.
    - Fetch current Order Book depth to estimate slippage for large orders in paper mode.

---

### Phase 2: Monitoring, API & UI

### REL-006: Monitoring & Health API
**Objective:** Provide a real-time health check and status interface (Deferred to Phase 2).

- **Implementation (Deferred):**
    - Implement a lightweight FastAPI or Flask app (run in a separate thread/process).
    - Endpoints:
        - `GET /health`: Returns 200 OK if bot is alive.
        - `GET /status`: Returns JSON with `bot_id`, `last_run_timestamp`, `status` (IDLE, TRADING, ERROR), and current equity.
- **Base Bot Integration (Phase 2):**
    - Add `self.monitoring.update_status(...)` calls in `BaseTradingBot` loop.

### REL-010: Web UI (v1) Dashboard
**Objective:** Visualization for the end-user (Deferred to a separate project).

- **New Component:** `ui/` directory.
    - Tech: Streamlit or React.
    - Features:
        - Equity curve (historical data from `Position` logs).
        - Asset distribution pie chart.
        - Recent trades table (from REL-001).

---

## 3. Database Migrations

| Migration Name | Table | Changes |
| :--- | :--- | :--- |
| `enhanced_trade_tracking` | `trade` | Add `invested_usdt_equivalent`, `usdt_rate`, `fee_usdt`, `reference_id`. |
| `target_weight_scheduling` | `bot_config` / New Table | Add `target_weight_schedule` table or JSON in `bot_config`. |
| `drift_reporting` | `drift_log` (New) | New table for tracking portfolio drift over time. |

---

## 4. Cross-Bot Impact Analysis

| Bot | Affected By | Action Needed |
| :--- | :--- | :--- |
| **Foundational** | REL-001, REL-003, REL-006, REL-009 | Update `log_trade` calls; remove local `_check_max_drawdown`; integrate health checks; support realistic paper trading. |
| **Futures** | REL-001, REL-003, REL-006, REL-009 | Update `log_trade` calls; ensure `FuturesTradingBotBase` uses centralized logic; integrate health checks. |
| **Scalping** | REL-001, REL-002, REL-006 | Ensure it doesn't break with new `place_order` signature; integrate health checks. |
| **Rebalancing** | All | Main target for enhancements. |

---

## 5. Web UI Readiness Requirements (Phase 2)

**Note:** The implementation of the Web UI and its dedicated API endpoints are moved to a second phase/separate project.

- **API Endpoints (FastAPI):**
    - `GET /status`: Health and current heartbeat.
    - `GET /equity`: History for chart.
    - `GET /trades`: List of recent trades with rich metadata.
    - `GET /positions`: Current allocation vs Target.
- **Storage Integration:**
    - UI will query `Storage` directly or via a new `ApiService`.
