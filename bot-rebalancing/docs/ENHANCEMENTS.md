# Bot Rebalancing Strategy Assessment & Enhancements

This document provides a comprehensive assessment of the cryptocurrency rebalancing bot, including critical analysis, technical debt identification, and a roadmap for enhancements in two parts: common layer and bot-specific layer.

---

## 1. Critical Assessment

### Why a user might reject this bot:
1.  **Lack of Transparency in Trade History:** Currently, the `Trade` log is minimal. It doesn't capture the USDT exchange rate at the time of trade for non-USDT pairs (if any were added), nor does it explicitly link trades to specific rebalancing cycles.
2.  **Performance Blindness:** There is no easy way to see cumulative PnL, win/loss ratios, or realized vs. unrealized gains without manually querying the database.
3.  **Risk of "Wash Trading" Fees:** If the rebalancing threshold is too low or price volatility is high, the bot might trade too frequently, eating up all profits in exchange fees.
4.  **No Market Impact Consideration:** The bot uses Market Orders (`create_market_order`). For large portfolios, this can lead to significant slippage, especially in less liquid pairs.
5.  **Fixed Thresholds:** The `rebalance_threshold_pct` is static. In extremely sideways markets, it might never trigger; in highly volatile markets, it might trigger too often.
6.  **Dependency on External Models:** While optional, the model gating adds a layer of failure. If the model server is down or returns garbage, rebalancing (a safety operation) might be improperly "soft-gated" or delayed.

### Bad Scenarios & Risks:
*   **Flash Crashes:** The bot might try to rebalance during a flash crash, selling at the bottom to maintain a weight, only to see the market rebound immediately (selling low, buying high).
*   **API Failures during Partial Execution:** If a multi-leg rebalance (e.g., sell BTC to buy ETH) fails halfway due to API limits or connectivity, the portfolio ends up in an unintended state (overweight in cash).
*   **Rounding Errors:** Accumulation of small rounding differences between the bot's tracked `Position` and the actual exchange balance can lead to "Insufficient Balance" errors over time (partially mitigated by the current `0.999` safety factor).
*   **Database Desync:** If the database becomes unreachable, the bot loses its "source of truth" for entry prices and invested amounts, making PnL calculations impossible.

---

## 2. Common Layer Enhancements (1st Part)

These changes affect the `common/` package and will benefit all bots (Foundational, Futures, Rebalancing, Scalping).

### A. Enhanced Trade Tracking (UI/UX Readiness)
*   **Update `Trade` Model:**
    *   Add `invested_usdt_equivalent`: The USDT value of the base asset at the time of trade.
    *   Add `usdt_rate`: The price of the asset in USDT at that moment.
    *   Add `fee_usdt`: Fee converted to USDT for easier accounting.
    *   Add `reference_id`: To group multiple trades belonging to one "rebalancing event" or "strategy signal".
*   **Centralized PnL Calculator:** Move PnL logic from `BaseTradingBot` to a dedicated `PerformanceService` that can handle complex scenarios like weighted average entry prices across multiple partial fills.

### B. Robust Execution Layer
*   **Limit Orders Support:** Enhance `place_order` to support Limit Orders with "Post-Only" or "IOC" (Immediate or Cancel) options to reduce slippage and fees.
*   **Standardized Error Handling:** Create common exception classes (e.g., `ExchangeConnectivityError`, `InsufficientBalanceError`) so all bots can react consistently (e.g., wait vs. stop).

### C. Unified Monitoring & Circuit Breakers (Phase 2 for API)
*   **Centralized Max Drawdown:** Move the circuit breaker logic into `BaseTradingBot` so it's guaranteed for every bot (Phase 1).
*   **Health Check Endpoint (Phase 2):** Add a simple HTTP server in `common/monitoring.py` that reports bot status (Running, Error, Last Heartbeat) for external monitoring tools.

---

## 3. Bot Rebalancing Enhancements (2nd Part)

Targeted specifically for `bot-rebalancing/`.

### A. Strategy Improvements
*   **Dynamic Thresholds:** Implement volatility-based thresholds. Increase the `rebalance_threshold_pct` during high volatility to avoid "churning" the portfolio.
*   **Target Weight Scheduling:** Allow weights to change over time (e.g., trend-following rebalancing where weights shift towards winning assets).
*   **Smart Cash Buffer:** Instead of a fixed `%` of equity, use a "Minimum USDT for Fees" buffer + a dynamic `%` based on pending buy orders.

### B. Operational Improvements
*   **Dry Run Mode Improvements:** Enhance `paper` mode to simulate slippage and realistic fee structures based on current order book depth (requires fetching order book).
*   **Drift Reporting:** Log the "Portfolio Drift" metric (the distance from the ideal target weights) even when it's below the threshold. This is crucial for UI visualization.

---

## 4. Web UI Readiness (Phase 2 - Deferred)

The implementation of the User UI and its supporting API endpoints has been moved to a second phase which will be worked later in another project.

1.  **Dashboard View:**
    *   Current Total Equity (USDT).
    *   24h / 7d / Total PnL (%).
    *   Current Allocation Chart (Target vs. Actual).
2.  **Operations Log:**
    *   List of rebalancing events.
    *   "Before" and "After" snapshots of each event.
    *   Individual trades with their realized PnL.
3.  **Config Management:**
    *   Ability to update `target_weights` and `rebalance_threshold_pct` via UI (requires the bot to re-load config from DB periodically, which it already does via `load_config`).

### Recommended Tech Stack for UI:
*   **Backend:** FastAPI (already partially used in model servers) to expose the `Storage` data.
*   **Frontend:** Streamlit (for fast internal tools) or React/Tailwind (for a polished user experience).
*   **Communication:** WebSockets for real-time equity and trade updates.

---

## 5. Log Analysis Summary

From the Docker logs, we observed:
*   **High frequency of "Same candle" skips:** This indicates the bot is checking very frequently (likely every few seconds) but the `sleep_seconds` or timeframe logic keeps it idle.
*   **Threshold Efficiency:** Most checks result in "Allocation within threshold". This is good as it saves fees, but suggests we could optimize the polling frequency to save API weight.
*   **Model Inactivity:** `use_model_prediction=False` is common. If this is the intended "Pure Rebalancing" mode, we should ensure the bot isn't making unnecessary calls to model endpoints.

---

## 6. Release Plan

The following tables prioritize the enhancements split into two phases.

### Phase 1: Core Trading, Stability & Transparency
Focuses on technical debt, risk mitigation, and data integrity to ensure the bot is ready for production and future UI integration.

| Priority | Item | Status | Component | Dependency | DB Migration | Description |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Critical** | **Enhanced Trade Tracking** | COMPLETED | Common | None | **Yes** | Add `invested_usdt_equivalent`, `usdt_rate`, `fee_usdt`, and `reference_id` to `Trade` table. Essential for UI and PnL accuracy. |
| **2. High** | **Circuit Breakers** | COMPLETED | Common | None | No | Centralize Max Drawdown logic in `BaseTradingBot` to protect against catastrophic losses across all bots. |
| **3. High** | **Robust Execution Layer** | COMPLETED | Common | None | No | Support for Limit Orders and standardized error handling to reduce slippage and improve reliability. |
| **4. Medium** | **Performance Service** | COMPLETED | Common | Trade Tracking | No | New service to calculate complex PnL (weighted averages) using the enhanced trade data. |
| **5. Medium** | **Dynamic Thresholds** | COMPLETED | Rebalancing | None | No | Volatility-based rebalancing thresholds to reduce fee churn in volatile markets. |
| **6. Medium** | **Drift Reporting** | COMPLETED | Rebalancing | None | No | Logging portfolio drift even when below threshold. Crucial for data visibility. |
| **7. Low** | **Smart Cash Buffer** | COMPLETED | Rebalancing | None | No | Advanced cash management for fees and pending orders. |
| **8. Low** | **Target Weight Scheduling** | COMPLETED | Rebalancing | None | **Yes** | Support for time-based or trend-following weight shifts. |
| **9. Low** | **Dry Run Improvements** | COMPLETED | Common/Rebal | None | No | Realistic slippage and fee simulation for paper trading. |
| **10. Low** | **Global Kill Switch** | COMPLETED | Common/Rebalancing | None | **Yes** | `bot_enabled` flag to turn trading activity ON/OFF via database. |

### Phase 2: Monitoring, API & UI
Focuses on user experience, real-time monitoring, and external integrations (deferred to a separate project/stage).

| Priority | Item | Status | Component | Dependency | DB Migration | Description |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1. High** | **Monitoring API** | Deferred (Phase 2) | Common | Phase 1 | No | FastAPI health check and status endpoints for external monitoring and UI. |
| **2. Medium** | **Web UI (v1)** | Deferred (Phase 2) | UI | Phase 1 + API | No | Initial dashboard showing equity curve, current allocation, and trade logs. |

### Migration Notes:
*   **Trade Table Migration:** A new Alembic migration is required to add the four columns to the `Trade` model. Existing records should have these values backfilled or set to `NULL`/`0.0` to maintain compatibility.
*   **Reference ID:** This field will allow grouping multiple trades into a single "Event" (e.g., a rebalance involving 3 pairs), which is critical for the UI "Operations Log" view.
*   **Target Weight Schedule:** May require a new table or a JSON field in `bot_config` to store scheduled changes.
