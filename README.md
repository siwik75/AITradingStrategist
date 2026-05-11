# Trading Intelligence Agent

A self-assessing market signal agent that generates trading signals, backtests them, and autonomously evolves its own strategy parameters when performance degrades.

## What It Does

1. **Analyzes** market conditions using 50+ technical indicators (EMA, RSI, MACD, ATR, Bollinger Bands, Ichimoku, ADX, volume profile)
2. **Generates** structured BUY/SELL/HOLD signals with entry, stop-loss, TP1, TP2
3. **Backtests** the current strategy parameters against configurable OHLCV data using a two-phase partial-exit model (TP1 = 50% exit + move SL to breakeven)
4. **Evolves** strategy parameters autonomously via a 7-step self-assessment loop — proposes mutations, A/B tests them, adopts the winner

Strategy parameters persist across runs (`~/.trading-agent/strategy_params.json` by default), so each run builds on the last.

---

## Actual Architecture

Three agents are implemented:

```
┌─────────────────────────────────────────┐
│              BaseAgent                  │
│   ReAct loop · dual-mode LLM · tools   │
└───────────┬──────────────┬──────────────┘
            │              │
     ┌──────┴────┐  ┌──────┴──────────────┐
     │SignalAgent│  │ SelfAssessmentAgent  │
     │ analyze() │  │ assess_and_evolve()  │
     └───────────┘  └──────────────────────┘
```

**Tools available to agents:**
- `get_ohlcv` — configurable OHLCV loader with `MARKET_DATA_SOURCE=synthetic|auto|ccxt|yfinance`
- `calculate_indicators` — 50+ indicators via the `ta` library
- `run_backtest` — trade simulation with two-phase TP1 partial exit
- `get_strategy_params` / `save_strategy_params` — file-backed persistence
- `get_trade_history` — JSONL trade history for assessment context

**Not yet implemented:** MarketDataAgent, StrategyAgent, BacktestAgent, RiskAgent, ExecutionAgent, MonitorAgent (planned).

---

## Quick Start

### Standard Python

```bash
# 1. Bootstrap (creates .venv, installs deps, copies .env.example → .env)
make bootstrap

# 2. Add your API key
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env

# 3. Smoke test — no API key needed for backtest mode
python main.py --mode backtest --symbol BTC/USDT --timeframe 4h --days 5

# 3b. Market-data smoke test — prints resolved source and first/last candle
python main.py --mode candles --symbol BTC/USDT --timeframe 4h --limit 5 --source default

# 4. Run all tests
make test
```

### Using uv

```bash
# 1. Create the virtualenv with Python 3.12
uv venv --python 3.12 .venv

# 2. Install the project with dev tools
uv pip install --python .venv/bin/python -e ".[dev]"

# 3. Copy the example env file
cp -n .env.example .env

# 4. Add your API key
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env

# 5. Smoke test
uv run --python .venv/bin/python python main.py --mode backtest --symbol BTC/USDT --timeframe 4h --days 5

# 6. Run tests
uv run --python .venv/bin/python pytest -q
```

If you want live market data through `ccxt` or `yfinance`, install the extra dependencies:

```bash
uv pip install --python .venv/bin/python -e ".[dev,live-data]"
```

If you prefer not to keep a dedicated `.venv`, you can also run directly with `uv`:

```bash
uv run --with '.[dev]' python main.py --mode backtest --symbol BTC/USDT --timeframe 4h --days 5
```

### Operation Modes

| Mode | Command | Requires API Key |
|---|---|---|
| `candles` | `python main.py --mode candles --symbol BTC/USDT --limit 5` | No |
| `backtest` | `python main.py --mode backtest` | No |
| `analysis` | `python main.py --mode analysis --symbol BTC/USDT` | Yes |
| `assess` | `python main.py --mode assess --symbol BTC/USDT --days 30` | Yes |
| `full` | `python main.py --mode full --symbol BTC/USDT --days 30` | Yes |
| `signals` | `python main.py --mode signals --status pending --limit 10` | No |
| `report` | `python main.py --mode report --signal-id abc123 --result won --pnl-pct 2.4` | No |
| `server` | `python main.py --mode server` | Yes (for /analyze, /assess) |

### Manual Execution Workflow

If you want the agent to suggest trades and keep you in the loop for execution:

1. Run `analysis` for a symbol/timeframe.
2. If the result is actionable (`BUY` or `SELL`), the app stores it locally and returns a `signal_id`.
3. Review pending suggestions with `python main.py --mode signals --status pending`.
4. Execute the trade manually outside the app.
5. Report the outcome back with `python main.py --mode report --signal-id <id> --result won|lost|breakeven|skipped|cancelled`.

This keeps a local audit trail of suggested signals and your real-world outcomes without enabling auto-execution.

---

## Requirements

- Python 3.12 (see `.python-version`)
- `uv` optional, if you prefer it over `venv` + `pip`
- `ANTHROPIC_API_KEY` or `LLM_GATEWAY_URL` + `LLM_API_KEY` for LLM-backed modes

Optional dependencies (install via `pip install -e ".[live-data]"`):
- `ccxt` — live exchange data
- `yfinance` — stock/ETF data

Market data selection:
- `MARKET_DATA_SOURCE=synthetic` keeps runs deterministic and offline-friendly
- `MARKET_DATA_SOURCE=auto` routes slash-form symbols like `BTC/USDT` to `ccxt` and ticker-style symbols like `AAPL` to `yfinance`
- `MARKET_DATA_FALLBACK_TO_SYNTHETIC=true` falls back to synthetic candles if a live provider fails or is unavailable

Recommended live-candle smoke check:
```bash
python main.py --mode candles --symbol BTC/USDT --timeframe 4h --limit 5
```
The output includes:
- the resolved `source`
- candle `count`
- the `first_candle`
- the `last_candle`

---

## Implemented vs Planned

### Implemented
- CLI modes: analysis, backtest, assess, full, server
- FastAPI server with `/health`, `/ready`, `/analyze`, `/assess`
- ReAct-loop base agent with Anthropic-native and OpenAI-compatible client support
- 50+ technical indicators
- Configurable OHLCV source resolution with live-provider fallback to synthetic candles
- Backtest engine with two-phase partial exit (TP1 + breakeven SL + TP2)
- Self-assessment loop with 7-step parameter evolution
- File-based persistence for strategy params, trade history, assessments
- Kubernetes deployment via Kustomize (dev + prod overlays)
- Graceful shutdown (SIGTERM → drain → exit)
- Config validation with actionable error messages

### Planned
- Live trade execution (currently dry-run only)
- Additional agents: Risk, Execution, Monitor
- Redis/DynamoDB/PostgreSQL production persistence backends
- Scheduled self-assessment in server mode
- Multi-symbol concurrent analysis

### Current Runtime Notes
- `BaseAgent` now auto-selects Anthropic direct access when `ANTHROPIC_API_KEY` is present, and uses the OpenAI-compatible gateway when only `LLM_API_KEY` + `LLM_GATEWAY_URL` are configured.
- If Anthropic authentication fails and an OpenAI-compatible gateway is configured, the agent retries through the gateway automatically using `OPENAI_LLM_MODEL` rather than the Anthropic `LLM_MODEL`.
- Docker and Kustomize deployment assets are in place, but Vault-side secret sourcing still depends on the entrypoint pattern documented in `docs/DEPLOYMENT.md`.

---

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — component diagram, data flow per mode, extension points
- [docs/WORKFLOW.md](docs/WORKFLOW.md) — step-by-step sequences for each operation mode
- [docs/DESIGN_PATTERNS.md](docs/DESIGN_PATTERNS.md) — ReAct loop, dual-mode LLM, self-assessment loop, 16-Factor compliance
- [docs/API.md](docs/API.md) — HTTP endpoint reference with request/response schemas
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Docker, Kustomize, EKS, Vault injection walkthrough
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — complete environment variable reference
- [docs/AUTONOMOUS_SIGNAL_SERVICE.md](docs/AUTONOMOUS_SIGNAL_SERVICE.md) — target design for autonomous scanning, evaluation, adaptation, and Telegram publishing

---

## Tech Stack

| Layer | Technology |
|---|---|
| LLM | Anthropic Claude (direct or via OpenAI-compatible gateway) |
| Technical analysis | `ta` library (pandas-based) |
| Market data | Synthetic · `ccxt` · `yfinance` with config-based source selection |
| API server | FastAPI + uvicorn |
| Persistence | JSON/JSONL files (local) · Redis/DynamoDB/PostgreSQL (stubs) |
| Orchestration | LangGraph (optional) · sequential fallback |
| Observability | structlog JSON → stdout |
| Container | Docker (Python 3.12-slim) |
| Deployment | Kubernetes (EKS/Fargate) via Kustomize |
| Secrets | HashiCorp Vault agent sidecar |
