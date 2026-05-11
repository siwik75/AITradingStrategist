# Project Context

## Purpose

This repository implements a self-assessing trading intelligence service. Its current goal is not live order execution; it is market analysis, signal generation, backtesting, and strategy-parameter evolution with persistence across runs.

## Project Standard

- Python application packaged through `pyproject.toml`
- Target runtime: Python 3.12
- 16-factor style configuration via environment variables
- Structured JSON logging via `structlog`
- Local developer workflow via `Makefile`
- CI gates for linting, type-checking, tests, and Docker smoke build
- Docker runtime plus Kubernetes deployment through Kustomize overlays

## Implementation Logic

### Runtime entrypoints

- `main.py` provides CLI modes: `analysis`, `backtest`, `assess`, `full`, `server`
- In server mode it exposes `/health`, `/ready`, `/analyze`, `/assess`

### Core components

- `BaseAgent`: ReAct loop, tool execution, LLM-client abstraction
- `SignalAgent`: generates structured BUY/SELL/HOLD signals
- `SelfAssessmentAgent`: runs the 7-step parameter evolution loop
- `tools/trading_tools.py`: OHLCV loading, indicators, backtesting, strategy/history tools
- `memory/store.py`: local file persistence plus production-backend stubs
- `workflows/trading_workflow.py`: LangGraph orchestration with sequential fallback

### Execution flow

1. Load `.env` and config
2. Validate runtime requirements
3. Load OHLCV candles from the configured source
4. Compute technical indicators
5. Generate a signal or run a backtest
6. Persist strategy parameters, trade history, and assessment records locally
7. Optionally run self-assessment when performance is below thresholds

## Instruments Adopted

### Runtime and APIs

- `anthropic` for Anthropic-native LLM calls
- `openai` for OpenAI-compatible gateway support
- `fastapi` and `uvicorn` for HTTP serving
- `python-dotenv` for local env loading
- `structlog` for structured logging

### Trading and data processing

- `pandas` for dataframe and OHLC manipulation
- `numpy` for synthetic data generation and metrics
- `ta` for indicator computation
- `ccxt` for live exchange OHLCV data
- `yfinance` for ticker-based OHLCV data

### Quality and delivery

- `pytest` and `pytest-asyncio`
- `ruff`, `black`, `mypy`
- Docker
- Kubernetes + Kustomize
- Vault sidecar-based secret injection in deployment manifests

## API Dependencies

### APIs exposed by this service

- `GET /health`
- `GET /ready`
- `POST /analyze`
- `POST /assess`

### External APIs and services consumed

- Anthropic Messages API
- OpenAI-compatible chat completions API
- CCXT-supported exchange REST APIs
- Yahoo Finance market-data endpoints through `yfinance`
- Redis, S3, DynamoDB, and PostgreSQL are modeled as future/optional backing services

## Current Data-Source Behavior

- `MARKET_DATA_SOURCE=synthetic` keeps execution deterministic and offline-friendly
- `MARKET_DATA_SOURCE=auto` routes slash-form symbols like `BTC/USDT` to `ccxt` and ticker symbols like `AAPL` to `yfinance`
- `MARKET_DATA_FALLBACK_TO_SYNTHETIC=true` falls back to synthetic candles if a live source fails
- `CCXT_EXCHANGE` selects the exchange id for live exchange candles

## Current Nuances

- OpenAI-compatible client support exists in `BaseAgent`, but the concrete agents still default to the Anthropic client path unless explicitly constructed otherwise
- Local file persistence is implemented; Redis, DynamoDB, PostgreSQL, and S3 remain stubbed
- Deployment manifests assume Vault injection, but secret sourcing still depends on the documented entrypoint pattern
