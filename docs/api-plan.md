# API Plan for Crypto Bot Dashboard

## Overview
This document outlines the expected REST API endpoints that will connect the Crypto Bot Dashboard to real data sources. The API design follows standard REST principles with consistent naming and structure.

## Base URL
```
https://api.crypto-bot.com/v1/
```

## Authentication
All endpoints require Bearer token authentication:
```
Authorization: Bearer <access_token>
```

## API Endpoints

### 1. Bot Management Endpoints

#### Get All Bots
```
GET /bots
```
**Response:**
```json
{
  "data": [
    {
      "id": "bot-001",
      "name": "AI Trading Bot 1",
      "status": "active",
      "created_at": "2023-01-15T10:30:00Z",
      "updated_at": "2023-01-15T10:30:00Z"
    }
  ],
  "meta": {
    "total": 1,
    "page": 1,
    "per_page": 10
  }
}
```

#### Get Bot Details
```
GET /bots/{bot_id}
```
**Response:**
```json
{
  "data": {
    "id": "bot-001",
    "name": "AI Trading Bot 1",
    "status": "active",
    "strategy": "mean_reversion",
    "risk_level": "medium",
    "created_at": "2023-01-15T10:30:00Z",
    "updated_at": "2023-01-15T10:30:00Z",
    "config": {
      "stop_loss": 0.05,
      "take_profit": 0.10,
      "max_position_size": 0.15
    }
  }
}
```

### 2. Portfolio and Performance Data

#### Get Portfolio Summary
```
GET /bots/{bot_id}/portfolio-summary
```
**Response:**
```json
{
  "data": {
    "total_value": 125430.50,
    "total_return": 15.20,
    "daily_change": 2.45,
    "equity_curve": [
      {"date": "2023-01-15", "value": 120000.00},
      {"date": "2023-01-16", "value": 122300.50}
    ],
    "allocation": {
      "bitcoin": 40.0,
      "ethereum": 30.0,
      "ripple": 20.0,
      "other": 10.0
    },
    "risk_metrics": {
      "volatility": 0.12,
      "sharpe_ratio": 1.85,
      "max_drawdown": 0.08
    }
  }
}
```

#### Get Holdings
```
GET /bots/{bot_id}/holdings
```
**Response:**
```json
{
  "data": [
    {
      "id": "hold-001",
      "symbol": "BTC",
      "name": "Bitcoin",
      "amount": 1.25,
      "value": 43750.00,
      "allocation_percentage": 40.0,
      "price_change_24h": -2.35,
      "status": "active"
    }
  ]
}
```

#### Get Transaction History
```
GET /bots/{bot_id}/transactions
```
**Response:**
```json
{
  "data": [
    {
      "id": "tx-001",
      "type": "buy",
      "symbol": "BTC",
      "amount": 0.5,
      "price": 42500.00,
      "total_value": 21250.00,
      "timestamp": "2023-01-15T10:30:00Z",
      "status": "completed"
    }
  ]
}
```

### 3. Real-time Market Data

#### Get Market Price
```
GET /market/prices/{symbol}
```
**Response:**
```json
{
  "data": {
    "symbol": "BTC",
    "price": 42500.00,
    "change_24h": -2.35,
    "volume_24h": 25430000000,
    "timestamp": "2023-01-15T10:30:00Z"
  }
}
```

#### Get Multiple Prices
```
GET /market/prices?symbols=BTC,ETH,XRP
```
**Response:**
```json
{
  "data": {
    "BTC": {"price": 42500.00, "change_24h": -2.35},
    "ETH": {"price": 2500.00, "change_24h": 1.25},
    "XRP": {"price": 0.50, "change_24h": 0.75}
  }
}
```

### 4. Configuration Management

#### Get Bot Configuration
```
GET /bots/{bot_id}/config
```
**Response:**
```json
{
  "data": {
    "strategy": {
      "name": "mean_reversion",
      "parameters": {
        "lookback_period": 30,
        "z_score_threshold": 2.0
      }
    },
    "risk_parameters": {
      "max_daily_loss": 0.15,
      "position_size_limit": 0.10,
      "stop_loss_percentage": 0.05
    },
    "trading_pairs": ["BTC_USD", "ETH_USD"]
  }
}
```

#### Update Bot Configuration  
```
PUT /bots/{bot_id}/config
```
**Request Body:**
```json
{
  "strategy": {
    "name": "mean_reversion",
    "parameters": {
      "lookback_period": 20
    }
  },
  "risk_parameters": {
    "max_daily_loss": 0.20
  }
}
```

### 5. System Monitoring

#### Get System Status
```
GET /system/status
```
**Response:**
```json
{
  "data": {
    "status": "healthy",
    "uptime": "7d 12h 34m",
    "cpu_usage": 45.2,
    "memory_usage": 68.7,
    "active_bots": 3,
    "last_updated": "2023-01-15T10:30:00Z"
  }
}
```

## Error Handling

All API errors follow this structure:
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request parameters",
    "details": [
      "Field 'symbol' is required"
    ]
  },
  "timestamp": "2023-01-15T10:30:00Z"
}
```

## Rate Limiting

- **Standard rate limit**: 100 requests per minute
- **Premium rate limit**: 1000 requests per minute  
- **Response headers**:
  ```
  X-RateLimit-Limit: 100
  X-RateLimit-Remaining: 99
  X-RateLimit-Reset: 1673841600
  ```

## Versioning

API version is specified in the URL path:
```
https://api.crypto-bot.com/v1/bots
```

## Security Considerations

1. **HTTPS only** required for all endpoints
2. **Input validation** on all parameters
3. **Output sanitization** to prevent XSS
4. **Audit logging** of critical operations
5. **CORS policy** configured for dashboard domain only

## Integration Points

The dashboard will connect to these endpoints through:
1. **BotDataService**: For fetching bot status, configuration, and portfolio data
2. **AnalyticsService**: For processing performance metrics and generating charts  
3. **MarketService**: For real-time price feeds and market data

This API structure enables the dashboard to display real-time trading analytics, portfolio performance, transaction history, and bot configuration management.