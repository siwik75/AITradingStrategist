# Configuration Reference

All configuration is read from environment variables at process startup. Copy `.env.example` to `.env` and fill in the required values. The application calls `load_dotenv()` at startup, so `.env` is read automatically.

Run `config.validate()` is called at startup and raises a descriptive error if required values are missing.

---

## LLM Configuration

| Variable | Default | Required | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | — | Yes* | Anthropic direct API key (starts with `sk-ant-`) |
| `LLM_API_KEY` | — | Yes* | API key for an OpenAI-compatible gateway such as OpenAI or GHO |
| `LLM_GATEWAY_URL` | `https://api.anthropic.com` | No | LLM endpoint URL. Change to GHO gateway URL for corporate access |
| `LLM_MODEL` | `claude-sonnet-4-20250514` | No | Primary model for analysis and assessment |
| `OPENAI_LLM_MODEL` | `gpt-5-mini` | No | Model used for OpenAI-compatible gateways and Anthropic-to-gateway fallback |
| `LLM_MODEL_FAST` | `claude-haiku-4-5-20251001` | No | Fast model (reserved for future lightweight tasks) |

You can configure either provider independently:
- `ANTHROPIC_API_KEY` enables Anthropic direct access
- `LLM_API_KEY` + `LLM_GATEWAY_URL` enable the OpenAI-compatible gateway

If both are configured, the app starts with Anthropic direct access first. If Anthropic authentication fails and the gateway is configured, the agent retries through the OpenAI-compatible gateway.

`OPENAI_LLM_MODEL` is intentionally separate from `LLM_MODEL` so Anthropic can use a Claude model id while the gateway uses an OpenAI model id. As of the current OpenAI models docs, examples include `gpt-5.1`, `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5-pro`, and `gpt-4.1`.

**Direct Anthropic (development):**
```
ANTHROPIC_API_KEY=sk-ant-api03-...
LLM_GATEWAY_URL=https://api.anthropic.com
```

**GHO Gateway (corporate):**
```
LLM_API_KEY=<gho-token>
LLM_GATEWAY_URL=https://gho.internal.example.com/v1
OPENAI_LLM_MODEL=gpt-5-mini
```

---

## Trading Configuration

| Variable | Default | Required | Description |
|---|---|---|---|
| `TRADING_SYMBOLS` | `["BTC/USDT","ETH/USDT"]` | No | JSON array of symbols to trade |
| `TRADING_TIMEFRAMES` | `["1h","4h","1d"]` | No | JSON array of timeframes |
| `MAX_RISK_PCT` | `1.0` | No | Maximum risk per trade as % of capital. Must be in (0, 10] |
| `MAX_POSITION_PCT` | `5.0` | No | Maximum single position as % of capital |
| `MIN_CONFIDENCE` | `70` | No | Minimum signal confidence to act (0–100) |
| `MIN_RISK_REWARD` | `2.0` | No | Minimum risk:reward ratio to take a trade |
| `DRY_RUN` | `true` | No | `true` = log signals only, never execute orders |

---

## Market Data

| Variable | Default | Required | Description |
|---|---|---|---|
| `MARKET_DATA_SOURCE` | `synthetic` | No | Default OHLCV source: `synthetic`, `ccxt`, `yfinance`, or `auto` |
| `MARKET_DATA_FALLBACK_TO_SYNTHETIC` | `true` | No | If `true`, provider failures fall back to synthetic candles |
| `CCXT_EXCHANGE` | `binance` | No | Exchange id used when `MARKET_DATA_SOURCE=ccxt` |

`MARKET_DATA_SOURCE=auto` uses a simple routing rule:
- slash-form symbols like `BTC/USDT` go to `ccxt`
- ticker-style symbols like `AAPL` go to `yfinance`

---

## Backtesting & Self-Assessment

| Variable | Default | Required | Description |
|---|---|---|---|
| `BACKTEST_LOOKBACK_DAYS` | `30` | No | Default lookback period for backtests |
| `MIN_TRADES_ASSESSMENT` | `10` | No | Minimum trade count before self-assessment is meaningful |
| `WIN_RATE_THRESHOLD` | `0.55` | No | Win rate below which self-assessment triggers (as decimal) |
| `MAX_DRAWDOWN_PCT` | `10.0` | No | Drawdown above which self-assessment triggers (%) |
| `ASSESSMENT_INTERVAL_HOURS` | `24` | No | Minimum hours between automatic assessments (server mode) |

---

## Infrastructure

| Variable | Default | Required | Description |
|---|---|---|---|
| `PORT` | `8080` | No | HTTP server port |
| `LOG_LEVEL` | `INFO` | No | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ENVIRONMENT` | `development` | No | Environment name. Set to `production` to disable Swagger UI |
| `AGENT_ID` | `trading-intelligence-agent` | No | Agent identifier used in logs and persistence keys |

---

## Persistence

| Variable | Default | Required | Description |
|---|---|---|---|
| `TRADING_AGENT_DATA_DIR` | `~/.trading-agent` | No | Directory for local file persistence (strategy params, trade history, assessments). Set to `/tmp/trading-agent` in Docker/K8s |
| `MEMORY_BACKEND` | `local` | No | Persistence backend: `local` (file), `redis`, `dynamodb`, `postgres`. Only `local` is fully implemented |
| `REDIS_URL` | `redis://localhost:6379` | No | Redis connection URL (used when `MEMORY_BACKEND=redis`) |
| `DATABASE_URL` | — | No | PostgreSQL connection URL (future, for `postgres` backend) |
| `S3_CONVERSATION_BUCKET` | `advisor-conversations` | No | S3 bucket for conversation history (future, for `s3` backend) |

---

## Example .env Files

### Minimal local development
```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
```

### Full development
```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
LLM_MODEL=claude-sonnet-4-20250514
OPENAI_LLM_MODEL=gpt-5-mini
ENVIRONMENT=development
LOG_LEVEL=DEBUG
DRY_RUN=true
TRADING_SYMBOLS=["BTC/USDT"]
MARKET_DATA_SOURCE=synthetic
BACKTEST_LOOKBACK_DAYS=14
TRADING_AGENT_DATA_DIR=/tmp/trading-agent-dev
```

### Production (values injected by Vault/Kustomize — do not commit)
```bash
LLM_API_KEY=<vault-injected>
LLM_GATEWAY_URL=<vault-injected>
OPENAI_LLM_MODEL=gpt-5-mini
ENVIRONMENT=production
LOG_LEVEL=INFO
DRY_RUN=false
PORT=8080
MARKET_DATA_SOURCE=ccxt
CCXT_EXCHANGE=binance
TRADING_AGENT_DATA_DIR=/tmp/trading-agent
AGENT_ID=trading-intelligence-agent
```
