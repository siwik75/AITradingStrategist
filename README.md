# Trading Intelligence Agent — Self-Assessing Agentic System

## Overview

A production-ready, self-assessing trading intelligence agent built on **Strands Agents SDK** patterns,
deployed on **AWS EKS**, and aligned with **Generali's 16-Factor App** principles.

The agent analyzes stock/crypto markets, generates trading signals (Entry, SL, TP1, TP2),
runs synthetic backtests, and **autonomously adjusts its strategy** based on historical performance.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                TRADING INTELLIGENCE SYSTEM                       │
│               (Strands Agents SDK + Anthropic)                  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  [Market Data Feed]  ──→  [MarketDataAgent]                     │
│       (ccxt/yfinance)           │                               │
│                                 ↓                               │
│                         [StrategyAgent]                         │
│                    (LLM + TA indicators)                        │
│                              │                                  │
│                    ┌─────────┴─────────┐                        │
│                    ↓                   ↓                        │
│            [SignalAgent]       [BacktestAgent]                  │
│        (Entry/SL/TP1/TP2)   (synthetic validation)             │
│                    │                   │                        │
│                    ↓                   ↓                        │
│              [RiskAgent]     [SelfAssessmentAgent]              │
│          (position sizing)   (performance review +             │
│                    │          strategy mutation)                │
│                    ↓                                           │
│            [ExecutionAgent]                                     │
│            (dry_run / live)                                     │
│                    ↓                                           │
│           [MonitorAgent]                                       │
│         (P&L + alerting)                                       │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Self-Assessment Loop**: The agent reviews its own past signals, runs backtesting,
  and mutates its indicator weights and strategy parameters autonomously
- **16-Factor App Compliant**: Stateless, externalized config, structured logging,
  health endpoints, graceful shutdown
- **LLM Gateway Compatible**: Uses OpenAI-compatible API (Generali GHO pattern)
- **Multi-provider**: Supports Anthropic Claude, OpenAI, Bedrock via unified gateway
- **Production Ready**: Docker, K8s manifests, HPA, liveness/readiness probes

## Quick Start

```bash
# Setup
cp .env.example .env  # configure API keys
pip install -r requirements.txt

# Run analysis
python main.py --symbol BTC/USDT --timeframe 4h --mode analysis

# Run full cycle with self-assessment
python main.py --symbol BTC/USDT --timeframe 4h --mode full

# Run backtest only
python main.py --symbol BTC/USDT --timeframe 4h --mode backtest --days 30
```

## Stack

- **SDK**: Strands Agents patterns + Anthropic SDK
- **LLM**: Claude via OpenAI-compatible Gateway (GHO)
- **Data**: ccxt, yfinance, pandas, ta
- **Infra**: Docker, EKS, Fargate, Aurora PostgreSQL
- **Observability**: structlog, CloudWatch (FluentBit)
- **Secrets**: HashiCorp Vault (IRSA)
