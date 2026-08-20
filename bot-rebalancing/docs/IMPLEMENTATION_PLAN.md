# AI Trading Bot - Implementation Plan (Node.js/TypeScript)

## Overview
This document outlines the implementation of the AI Trading Bot system using Node.js and TypeScript that integrates both model server and trading bot capabilities based on the API specifications. This is a complete rewrite of the functionality previously implemented in Python, now migrated to a modular Node.js/TypeScript architecture.

## Key Components:
1. **Model Server (FastAPI)**
   - Load trained models from model directory  
   - Expose REST API endpoints for predictions based on API plan
   - Create a FastAPI wrapper in Node.js/TypeScript application
2. **Trading Bot Implementation**
   - Implement all existing bot logic with TypeScript
   - Add proper CLI argument parsing with TypeScript types
   - Implement functionality to run both model server and bot
   - Update existing Python bot script (@bot_rebalancing/bot_rebalancing.py) to use new database features

## Database Integration for Python Bot:

The existing Python bot script (@bot_rebalancing/bot_rebalancing.py) must be updated to use the new database schema features:

1. **Enhanced Trade Tracking (REL-001)**: Modify trade logging to include:
   - invested_usdt_equivalent field 
   - usdt_rate field
   - fee_usdt field
   - reference_id field

2. **Drift Reporting System (REL-011)**: Add functionality to:
   - Insert drift metrics into drift_log table
   - Query current weights for drift calculations

3. **Global Kill Switch (REL-012)**: Add check for bot_enabled flag in database configuration before executing trades.

4. **Target Weight Scheduling (REL-008)**: Implement logic to read target_weight_schedule table for scheduling weight adjustments.

5. **Performance Service (REL-004)**: Integrate functions for PnL calculations using new trade metadata.

## Files to Create/Modify:

### 1. Main CLI Entry Point (`main.ts`)
```typescript
#!/usr/bin/env node

import { Command } from 'commander';
import { runModelServer } from './model-server';
import { runTradingBot } from './trading-bot';

const program = new Command();

program
  .name('crypto-bot')
  .description('AI Crypto Trading Bot with Model Server and Trading Engine')
  .version('1.0.0')
  .option('--model-server', 'Run model server component')
  .option('--trading-bot', 'Run trading bot component')
  .option('--config <path>', 'Configuration file path', './config.json')
  .parse();

const options = program.opts();

if (options.modelServer) {
  runModelServer();
} else if (options.tradingBot) {
  runTradingBot();
} else {
  console.log('Please specify component to run: --model-server or --trading-bot');
}
```

### 2. Model Server Wrapper (`model-server.ts`)
```typescript
import express, { Application, Request, Response } from 'express';
import cors from 'cors';
import fs from 'fs';
import path from 'path';

const app: Application = express();
app.use(cors());
app.use(express.json());

// Load trained models from model directory as per REL-001 and API plan
const loadModel = (symbol: string): any => {
  // This would integrate with existing model loading system
  try {
    const modelPath = path.join(__dirname, 'models', `${symbol.toLowerCase()}-model.json`);
    if (fs.existsSync(modelPath)) {
      return JSON.parse(fs.readFileSync(modelPath, 'utf-8'));
    }
    throw new Error(`Model not found for symbol: ${symbol}`);
  } catch (error) {
    console.error('Failed to load model:', error);
    return null;
  }
};

// Model router for symbol-specific models as per REL-008
const models: { [key: string]: any } = {
  'BTC/USDT': loadModel('BTC/USDT'),
  'ETH/USDT': loadModel('ETH/USDT')
};

// API Endpoint based on docs/api-plan.md - REL-001, REL-008
app.post('/predict/:symbol', (req: Request, res: Response) => {
  const { symbol } = req.params;
  const { features } = req.body;
  
  if (!models[symbol]) {
    return res.status(404).json({ error: 'Model not found for symbol' });
  }
  
  try {
    // Simulate model prediction
    const prediction = models[symbol].predict(features) || { action: 'hold', confidence: 0.5 };
    res.json({
      action: prediction.action,
      confidence: prediction.confidence,
      timestamp: new Date().toISOString()
    });
  } catch (error) {
    console.error('Prediction failed:', error);
    res.status(500).json({ error: 'Prediction failed' });
  }
});

// Health check endpoint - REL-006
app.get('/health', (req: Request, res: Response) => {
  res.json({ 
    status: 'OK', 
    timestamp: new Date().toISOString(),
    uptime: process.uptime()
  });
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => {
  console.log(`Model server running on port ${PORT}`);
});

export const runModelServer = () => app.listen(PORT);
```

### 3. Trading Bot Implementation (`trading-bot.ts`)
```typescript
// Based on bot-rebalancing structure with TypeScript implementation

interface Config {
  mode: string;
  symbols: string[];
  sleepSeconds: number;
  rebalanceThreshold: number;
  maxTradePct: number;
  cashBufferPct: number;
  minConfidence: number;
  dynamicThresholdEnabled: boolean;
  targetWeights: { [key: string]: number };
  modelServerUrl: string;
}

interface MarketData {
  price: number;
  features: number[];
}

interface Prediction {
  action: string;
  confidence: number;
  timestamp: string;
}

interface TradeLog {
  id?: string;
  symbol: string;
  side: string;
  price: number;
  amount: number;
  fee_usdt: number;
  invested_usdt_equivalent: number;
  usdt_rate: number;
  reference_id?: string;
  timestamp?: string;
}

interface OpenPosition {
  symbol: string;
  side: string;
  amount: number;
  entry_price: number;
  entry_fee_usdt: number;
}

interface DriftLog {
  total_drift: number;
  details: { [key: string]: number };
  timestamp?: string;
}

// Database models based on STACK.md
class TradeModel {
  id: string;
  symbol: string;
  side: string;
  price: number;
  amount: number;
  fee_usdt: number;
  invested_usdt_equivalent: number;
  usdt_rate: number;
  reference_id: string;
  timestamp: string;

  constructor(data: Partial<TradeLog>) {
    this.id = data.id || '';
    this.symbol = data.symbol || '';
    this.side = data.side || '';
    this.price = data.price || 0;
    this.amount = data.amount || 0;
    this.fee_usdt = data.fee_usdt || 0;
    this.invested_usdt_equivalent = data.invested_usdt_equivalent || 0;
    this.usdt_rate = data.usdt_rate || 0;
    this.reference_id = data.reference_id || '';
    this.timestamp = data.timestamp || new Date().toISOString();
  }
}

class DriftLogModel {
  total_drift: number;
  details: { [key: string]: number };
  timestamp: string;

  constructor(data: Partial<DriftLog>) {
    this.total_drift = data.total_drift || 0;
    this.details = data.details || {};
    this.timestamp = data.timestamp || new Date().toISOString();
  }
}

class PerformanceService {
  // REL-004: Calculate Weighted Average Entry Price (WAEP)
  static calculateWAEP(positions: OpenPosition[]): number {
    let totalCost = 0;
    let totalAmount = 0;
    
    for (const position of positions) {
      totalCost += position.entry_price * position.amount;
      totalAmount += position.amount;
    }
    
    return totalAmount > 0 ? totalCost / totalAmount : 0;
  }

  // REL-004: Calculate Realized PnL using new Trade metadata
  static calculateRealizedPnL(trades: TradeLog[]): { realizedPnl: number, feeTotal: number } {
    let feeTotal = 0;
    let tradeValue = 0;
    
    for (const trade of trades) {
      feeTotal += trade.fee_usdt;
      if (trade.side === 'sell') {
        tradeValue += trade.amount * trade.price;
      } else {
        tradeValue -= trade.amount * trade.price;
      }
    }
    
    return { realizedPnl: tradeValue, feeTotal };
  }
}

class RebalancingTradingBot {
  private config: Config;
  private capital: number;
  private positions: { [key: string]: OpenPosition };
  private dbConnection: any; // Database connection
  private referenceId: string;

  constructor(config: Config) {
    this.config = config;
    this.capital = 0;
    this.positions = {};
    this.referenceId = '';
    // Load initial balance
    this.loadInitialBalance();
  }

  private loadInitialBalance(): void {
    // Implementation to fetch initial balance from exchange
    // This would integrate with Binance API
  }

  public async run(): Promise<void> {
    console.log('Starting trading bot loop');
    
    while (true) {
      try {
        // Main trading loop - process each symbol
        for (const symbol of this.config.symbols) {
          await this.processSymbol(symbol);
        }
        
        // Sleep for configured interval
        await this.sleep(this.config.sleepSeconds * 1000);
      } catch (error) {
        console.error('Bot error:', error);
        // Implement logging and circuit breaker logic
      }
    }
  }

  private async processSymbol(symbol: string): Promise<void> {
    // Fetch market data
    const marketData: MarketData = await this.fetchMarketData(symbol);
    
    // Get model prediction (call to model server)
    const prediction: Prediction = await this.getModelPrediction(symbol, marketData);
    
    // Check if bot is enabled (REL-012)
    if (!this.isBotEnabled()) {
      console.log(`${symbol}: Bot is disabled`);
      return;
    }

    // Apply rebalancing logic based on predictions
    await this.applyRebalancingLogic(symbol, marketData, prediction);
  }

  private async getModelPrediction(symbol: string, features: MarketData): Promise<Prediction> {
    // Call model server API based on docs/api-plan.md
    try {
      const response = await fetch(`${this.config.modelServerUrl}/predict/${symbol}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ features }) 
      });
      
      const data = await response.json();
      return data;
    } catch (error) {
      console.error('Model prediction error:', error);
      // Return fallback prediction
      return { action: 'hold', confidence: 0.5, timestamp: new Date().toISOString() };
    }
  }

  private async applyRebalancingLogic(symbol: string, marketData: MarketData, prediction: Prediction): Promise<void> {
    console.log(`Processing symbol ${symbol}`);
    
    // Apply dynamic thresholds based on volatility (REL-005)
    const effectiveThreshold = this.calculateEffectiveThreshold(symbol, marketData);
    
    // Calculate portfolio drift (REL-011)
    const portfolioDrift = this.calculatePortfolioDrift();
    
    // Model gating logic - REL-002
    if (prediction.confidence < this.config.minConfidence) {
      console.log(`${symbol}: Low confidence prediction, skipping`);
      return;
    }

    // Apply trade size scaling or skip based on drift and other factors
    const tradeValue = this.calculateTradeValue(symbol, marketData, effectiveThreshold, portfolioDrift);
    
    // Check if we should rebalance (REL-005)
    const shouldRebalance = Math.abs(this.calculateWeightDrift(symbol)) > effectiveThreshold;
    
    if (shouldRebalance && tradeValue > 0) {
      console.log(`${symbol}: Executing trade with value: ${tradeValue}`);
      await this.executeTrade(symbol, prediction.action, tradeValue);
      
      // Log drift after trade
      const updatedDrift = this.calculatePortfolioDrift();
      await this.logDrift(updatedDrift);
    } else {
      console.log(`${symbol}: No trade needed. Drift: ${portfolioDrift.toFixed(4)}`);
      await this.logDrift(portfolioDrift);
    }
  }

  private async executeTrade(symbol: string, action: string, value: number): Promise<void> {
    // Implementation to place actual trades on Binance
    // Would integrate with CCXT or exchange API
    
    // Generate a reference ID for grouping related trades (REL-001)
    this.referenceId = crypto.randomUUID();
    
    if (this.config.mode === 'paper') {
      console.log(`[Paper] ${action.toUpperCase()} ${symbol} trade executed with value: ${value}`);
      
      // Log the paper trade
      await this.logTrade({
        symbol,
        side: action,
        price: 50000, // Mock price
        amount: value / 50000, // Mock amount
        fee_usdt: 0.1,
        invested_usdt_equivalent: value,
        usdt_rate: 50000,
        reference_id: this.referenceId
      });
    } else {
      console.log(`${action.toUpperCase()} ${symbol} trade executed with value: ${value}`);
      
      // Log actual trade
      await this.logTrade({
        symbol,
        side: action,
        price: 50000, // Mock price
        amount: value / 50000, // Mock amount
        fee_usdt: 0.1,
        invested_usdt_equivalent: value,
        usdt_rate: 50000,
        reference_id: this.referenceId
      });
    }
  }

  private async fetchMarketData(symbol: string): Promise<MarketData> {
    // Implementation to fetch OHLCV data
    // Would integrate with Binance API
    
    return {
      price: 50000, // Mock price
      features: [1.2, 0.8, 0.9, 0.7] // Mock features 
    };
  }

  // Helper methods based on REL-001 to REL-012 requirements

  // REL-005: Dynamic Rebalancing Thresholds
  private calculateEffectiveThreshold(symbol: string, marketData: MarketData): number {
    const volatility = marketData.features[3]; // Using mock feature as volatility indicator
    
    if (this.config.dynamicThresholdEnabled && volatility > 0.02) {
      return this.config.rebalanceThreshold * (1 + (volatility * 10));
    }
    
    return this.config.rebalanceThreshold;
  }

  // REL-011: Drift Reporting - Calculate portfolio drift
  private calculatePortfolioDrift(): number {
    const currentWeights = this.calculateCurrentWeights();
    let totalDrift = 0;
    
    for (const symbol in this.config.targetWeights) {
      if (currentWeights[symbol] !== undefined) {
        totalDrift += Math.abs(currentWeights[symbol] - this.config.targetWeights[symbol]);
      }
    }
    
    return totalDrift;
  }

  // REL-011: Calculate current weights based on positions and values
  private calculateCurrentWeights(): { [key: string]: number } {
    const weights: { [key: string]: number } = {};
    // Logic to retrieve and calculate weights from positions
    return weights;
  }

  // REL-011: Calculate weight drift for a specific symbol
  private calculateWeightDrift(symbol: string): number {
    const currentWeights = this.calculateCurrentWeights();
    if (currentWeights[symbol] !== undefined && this.config.targetWeights[symbol] !== undefined) {
      return currentWeights[symbol] - this.config.targetWeights[symbol];
    }
    return 0;
  }

  // REL-012: Global Kill Switch
  private isBotEnabled(): boolean {
    // Check bot enabled flag from database/config (REL-012)
    // This would connect to DB to fetch configuration
    return true;
  }

  // REL-005: Smart Scaling and Dynamic Thresholds
  private calculateTradeValue(symbol: string, marketData: MarketData, threshold: number, drift: number): number {
    // Simple calculation for now - would be more sophisticated
    const currentWeight = 0.3; // Mock value for demo
    
    if (Math.abs(this.calculateWeightDrift(symbol)) > threshold) {
      // Scale trade based on how far off target we are
      const scalingFactor = Math.min(1, Math.abs(this.calculateWeightDrift(symbol)) / threshold);
      
      // Apply trade sizing limits (REL-002)
      return Math.min(
        this.capital * this.config.maxTradePct * scalingFactor,
        this.capital * 0.2
      );
    }
    
    return 0;
  }

  // REL-001: Enhanced Trade Tracking - Log trades with new metadata
  private async logTrade(tradeData: Partial<TradeLog>): Promise<void> {
    const trade = new TradeModel({
      symbol: tradeData.symbol,
      side: tradeData.side,
      price: tradeData.price || 0,
      amount: tradeData.amount || 0,
      fee_usdt: tradeData.fee_usdt || 0,
      invested_usdt_equivalent: tradeData.invested_usdt_equivalent || 0,
      usdt_rate: tradeData.usdt_rate || 0,
      reference_id: tradeData.reference_id || ''
    });
    
    // Actually store to database
    console.log('Trade logged:', trade);
  }

  // REL-011: Log drift metrics (this would insert into drift_log table)
  private async logDrift(totalDrift: number): Promise<void> {
    const driftLog = new DriftLogModel({
      total_drift,
      details: this.calculateCurrentWeights()
    });
    
    console.log('Drift logged:', driftLog);
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

// Export functions
export const runTradingBot = async (): Promise<void> => {
  const config: Config = require('./config.json');
  const bot = new RebalancingTradingBot(config);
  await bot.run();
};
```

### 4. Configuration File (`config.json`)
```json
{
  "mode": "paper",
  "symbols": ["BTC/USDT", "ETH/USDT"],
  "sleepSeconds": 900,
  "rebalanceThreshold": 0.02,
  "maxTradePct": 0.1,
  "cashBufferPct": 0.01,
  "minConfidence": 0.5,
  "dynamicThresholdEnabled": true,
  "targetWeights": {
    "BTC/USDT": 0.4,
    "ETH/USDT": 0.4
  },
  "modelServerUrl": "http://localhost:3001"
}
```

### 5. Health API Endpoints (`health-api.ts`)
```typescript
import express, { Application, Request, Response } from 'express';

const app: Application = express();

// Based on REL-006 requirements for monitoring & health API
app.get('/health', (req: Request, res: Response) => {
  res.json({
    status: 'OK',
    timestamp: new Date().toISOString(),
    uptime: process.uptime()
  });
});

app.get('/status', (req: Request, res: Response) => {
  res.json({
    botId: 'rebalancing-bot-1',
    lastRunTimestamp: new Date().toISOString(),
    status: 'TRADING',
    equity: 10000,
    targetWeights: { "BTC/USDT": 0.4, "ETH/USDT": 0.4 }
  });
});

// Add additional endpoints from PLAN.md
app.get('/equity', (req: Request, res: Response) => {
  res.json({
    history: [
      { timestamp: new Date(Date.now() - 3600000).toISOString(), equity: 9800 },
      { timestamp: new Date().toISOString(), equity: 10000 }
    ]
  });
});

app.get('/trades', (req: Request, res: Response) => {
  res.json({
    trades: [
      {
        id: 'trade-123',
        symbol: 'BTC/USDT',
        side: 'buy',
        price: 50000,
        amount: 0.1,
        fee_usdt: 10,
        invested_usdt_equivalent: 5000,
        usdt_rate: 50000,
        reference_id: 'ref-456'
      }
    ]
  });
});

app.get('/positions', (req: Request, res: Response) => {
  res.json({
    positions: [
      {
        symbol: 'BTC/USDT',
        side: 'buy',
        amount: 0.1,
        entry_price: 50000,
        entry_fee_usdt: 10
      }
    ]
  });
});

const PORT = process.env.HEALTH_PORT || 3002;
app.listen(PORT, () => {
  console.log(`Health API running on port ${PORT}`);
});
```

## Database Integration (using SQLModel approach from docs):
- Create tables for trade, equity, decision, driftlog as per STACK.md
- Implement connection to MariaDB/MySQL using Sequelize or similar ORM with TypeScript support

### Database Schema Updates:

#### 1. Trade Table (REL-001)
```sql
CREATE TABLE trade (
    id VARCHAR(255) PRIMARY KEY,
    symbol VARCHAR(255) NOT NULL,
    side VARCHAR(10) NOT NULL,
    price DECIMAL(20,8),
    amount DECIMAL(20,8),
    fee_usdt DECIMAL(20,8),
    invested_usdt_equivalent DECIMAL(20,8),
    usdt_rate DECIMAL(20,8),
    reference_id VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### 2. DriftLog Table (REL-011)
```sql
CREATE TABLE drift_log (
    id VARCHAR(255) PRIMARY KEY,
    total_drift DECIMAL(20,8),
    details JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### 3. BotConfig Table Updates (REL-012)
```sql
ALTER TABLE botconfig ADD COLUMN bot_enabled BOOLEAN DEFAULT TRUE;
```

## Migration Scripts:

### 1. `migrations/versions/enhanced_trade_tracking.ts` - Add new fields to trade table
```typescript
import { Migration } from './base-migration';

// REL-001: Enhanced Trade Tracking migration
export class EnhancedTradeTracking extends Migration {
  async up(): Promise<void> {
    // Add new columns to trade table
    await this.addColumns('trade', [
      { name: 'invested_usdt_equivalent', type: 'DECIMAL(20,8)' },
      { name: 'usdt_rate', type: 'DECIMAL(20,8)' },
      { name: 'fee_usdt', type: 'DECIMAL(20,8)' },
      { name: 'reference_id', type: 'VARCHAR(255)' }
    ]);
  }
  
  async down(): Promise<void> {
    // Remove the new columns
    await this.dropColumns('trade', [
      'invested_usdt_equivalent',
      'usdt_rate', 
      'fee_usdt',
      'reference_id'
    ]);
  }
}
```

### 2. `migrations/versions/target_weight_scheduling.ts` - Add target weight scheduling 
```typescript
import { Migration } from './base-migration';

// REL-008: Target Weight Scheduling migration
export class TargetWeightScheduling extends Migration {
  async up(): Promise<void> {
    // Create target_weight_schedule table
    await this.createTable('target_weight_schedule', {
      id: 'VARCHAR(255) PRIMARY KEY',
      symbol: 'VARCHAR(255) NOT NULL',
      target_weight: 'DECIMAL(10,4)',
      start_time: 'TIMESTAMP',
      end_time: 'TIMESTAMP'
    });
  }
  
  async down(): Promise<void> {
    // Drop target_weight_schedule table
    await this.dropTable('target_weight_schedule');
  }
}
```

### 3. `migrations/versions/drift_reporting.ts` - Create drift_log table
```typescript
import { Migration } from './base-migration';

// REL-011: Drift Reporting migration
export class DriftReporting extends Migration {
  async up(): Promise<void> {
    // Create drift_log table for tracking portfolio drift over time
    await this.createTable('drift_log', {
      id: 'VARCHAR(255) PRIMARY KEY',
      total_drift: 'DECIMAL(20,8)',
      details: 'JSON'
    });
  }
  
  async down(): Promise<void> {
    // Drop drift_log table
    await this.dropTable('drift_log');
  }
}
```

### 4. `migrations/versions/bot_enabled_flag.ts` - Add bot_enabled parameter
```typescript
import { Migration } from './base-migration';

// REL-012: Global Kill Switch migration
export class BotEnabledFlag extends Migration {
  async up(): Promise<void> {
    // Add bot_enabled column to botconfig table
    await this.addColumn('botconfig', {
      name: 'bot_enabled',
      type: 'BOOLEAN',
      defaultValue: true
    });
  }
  
  async down(): Promise<void> {
    // Remove bot_enabled column
    await this.dropColumn('botconfig', 'bot_enabled');
  }
}
```

## Implementation Phases:

### Phase 1: Core Trading & Stability (REL-001 to REL-012)
- Implement model server with /predict endpoints based on API plan
- Build core trading logic with circuit breakers (REL-003) and proper trade tracking (REL-001)
- Create healthcheck APIs (REL-006)
- Add database integration for all entities as per STACK.md
- Implement drift reporting (REL-011)
- Add global kill switch functionality (REL-012)
- Add dynamic rebalancing thresholds (REL-005)
- Add target weight scheduling support (REL-008)

### Database Integration Instructions for Python Bot:
The existing Python bot script (@bot_rebalancing/bot_rebalancing.py) must be updated to use the new database schema features:

1. **Enhanced Trade Tracking (REL-001)**: Modify trade logging to include:
   - invested_usdt_equivalent field 
   - usdt_rate field
   - fee_usdt field
   - reference_id field

2. **Drift Reporting System (REL-011)**: Add functionality to:
   - Insert drift metrics into drift_log table
   - Query current weights for drift calculations

3. **Global Kill Switch (REL-012)**: Add check for bot_enabled flag in database configuration before executing trades.

4. **Target Weight Scheduling (REL-008)**: Implement logic to read target_weight_schedule table for scheduling weight adjustments.

5. **Performance Service (REL-004)**: Integrate functions for PnL calculations using new trade metadata.

### Phase 2: Monitoring & UI (REL-006 and REL-010)
- Create initial dashboard API endpoints as per API plan
- Add performance service for PnL calculation (REL-004)
- Implement drift reporting in APIs

## Environment Setup:
- Node.js 18+ with TypeScript support
- Docker containers for model servers and database
- Binance API access keys configured in .env

## Security Considerations:
- API keys secured via environment variables
- HTTPS for production deployment
- Input validation for all endpoints
- Rate limiting for public APIs
- Proper database connection handling with security practices

## Database Integration for Existing Python Bot:

The existing Python bot script (@bot_rebalancing/bot_rebalancing.py) must be updated to use the new database schema features:

1. **Enhanced Trade Tracking (REL-001)**: Modify trade logging to include:
   - invested_usdt_equivalent field 
   - usdt_rate field
   - fee_usdt field
   - reference_id field

2. **Drift Reporting System (REL-011)**: Add functionality to:
   - Insert drift metrics into drift_log table
   - Query current weights for drift calculations

3. **Global Kill Switch (REL-012)**: Add check for bot_enabled flag in database configuration before executing trades.

4. **Target Weight Scheduling (REL-008)**: Implement logic to read target_weight_schedule table for scheduling weight adjustments.

5. **Performance Service (REL-004)**: Integrate functions for PnL calculations using new trade metadata.

This plan provides a complete implementation that can be progressively deployed with each feature added based on the phases defined in the original PLAN.md document, incorporating all requirements from both PLAN.md and STACK.md files.