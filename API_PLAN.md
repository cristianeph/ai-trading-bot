# Backend API Plan for AI Trading Bot Dashboard

This document outlines the backend API endpoints and data structures required for the frontend dashboard to present trading bot data effectively.

## Overview

The backend API will serve as the data layer for the trading bot dashboard, providing access to:
- Real-time portfolio status
- Trade history and performance metrics
- Drift monitoring data
- Configuration settings
- Weight scheduling information

## Authentication & Security

All endpoints will require authentication with a valid JWT token.
Basic rate limiting will be implemented to prevent abuse.

## Core Endpoints

### 1. Portfolio Status Endpoint
**GET /api/portfolio/status**

Returns current portfolio metrics including:
- Total equity  
- Equity history chart data
- Position details per symbol
- Cash buffer status
- Current portfolio drift

### 2. Trade History Endpoint
**GET /api/trades**

Returns paginated trade history with filtering options:
- Symbol filter
- Date range filter  
- Trade type (buy/sell)
- Mode (paper/live)

Fields include:
- Timestamp
- Symbol
- Side (buy/sell)
- Price
- Amount
- PnL
- Fee details
- Reference ID

### 3. Drift Log Endpoint
**GET /api/drift/logs**

Returns portfolio drift history with filtering:
- Date range filter
- Mode filter (paper/live)

Fields include:
- Timestamp
- Total drift percentage
- Detailed breakdown by symbol
- Mode (paper/live)

### 4. Configuration Endpoint
**GET /api/config**

Returns current configuration for all bots including:
- Global settings (bot_enabled, sleep_seconds)
- Per-symbol settings (target weights, min trade amounts)
- Dynamic threshold settings
- Volatility parameters

### 5. Weight Schedules Endpoint
**GET /api/weights/schedules**

Returns active weight schedules with:
- Symbol 
- Target weight
- Start/end timestamps
- Current status (active/inactive)

### 6. Performance Metrics Endpoint
**GET /api/metrics/performance**

Returns performance statistics:
- Total PnL
- Daily/weekly/monthly returns
- Drawdown statistics
- Sharpe ratio

### 7. Decision History Endpoint
**GET /api/decisions**

Returns prediction decisions with outcome tracking:
- Timestamp
- Symbol
- Action (buy/sell/hold)
- Confidence level
- Feature values used
- Outcome PnL and equity deltas
- Candle timestamp

## Data Structures

### PortfolioPosition
```json
{
  "symbol": "BTC/USDT",
  "amount": 0.5,
  "entry_price": 32000.0,
  "entry_fee_usdt": 15.0,
  "last_price": 34000.0,
  "pnl": 1000.0
}
```

### TradeRecord
```json
{
  "id": 12345,
  "timestamp": "2023-06-15T10:30:00Z",
  "symbol": "BTC/USDT", 
  "side": "buy",
  "price": 32000.0,
  "amount": 0.5,
  "mode": "paper",
  "pnl": 0.0,
  "fee_usdt": 15.0,
  "reference_id": "ref-12345"
}
```

### DriftRecord
```json
{
  "timestamp": "2023-06-15T10:30:00Z",
  "total_drift": 0.15,
  "details": {
    "BTC/USDT": {"current_pct": 0.4, "target_pct": 0.3, "drift": 0.1},
    "ETH/USDT": {"current_pct": 0.3, "target_pct": 0.4, "drift": -0.1}
  },
  "mode": "paper"
}
```

### WeightSchedule
```json
{
  "symbol": "BTC/USDT",
  "target_weight": 0.4,
  "start_timestamp": "2023-06-01T00:00:00Z",
  "end_timestamp": "2023-06-30T23:59:59Z",
  "status": "active"
}
```

## Implementation Considerations

### Database Queries
- All endpoints should use optimized database queries with appropriate indexing
- Implement pagination for large datasets (>100 records)
- Support datetime filtering with efficient SQL clauses
- Use connection pooling for database access

### Caching Strategy  
- Cache frequently accessed data (portfolio status) with 5-10 second TTL
- Implement Redis caching for heavy computation results (performance metrics)

### Error Handling
- Return appropriate HTTP status codes (400, 401, 403, 404, 500)
- Standard error response format
- Rate limiting with retry-after header

## Future Enhancements

### Real-time Updates
- WebSockets for live portfolio updates
- Push notifications for significant events

### Advanced Analytics
- Risk metrics
- Correlation analysis
- Forecasting models

This API will provide the comprehensive data layer needed for a robust trading dashboard while maintaining good performance and security practices.