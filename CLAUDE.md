# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Bootstrap environment (creates venv, installs deps, copies .env.example → .env)
make bootstrap

# Run all tests
make test

# Run a single test file
.venv/bin/pytest tests/test_market_context_and_rag.py -v

# Run a single test function
.venv/bin/pytest tests/test_market_context_and_rag.py::TestVwapBlock::test_vwap_block_present_in_full_indicators -v

# Lint (ruff + black --check)
make lint

# Start development server (FastAPI on :8000)
make dev   # equivalent: python main.py --mode server

# Smoke test without an API key
python main.py --mode backtest --symbol BTC/USDT --timeframe 4h --days 5

# Single analysis (requires ANTHROPIC_API_KEY)
python main.py --mode analysis --symbol BTC/USDT --timeframe 1h
```

All modes: `candles | analysis | backtest | assess | full | server | signals | report`

## Architecture

The system is a multi-agent trading intelligence service with a ReAct (reason + act) loop at its core, augmented by a market-context pre-fetcher and a RAG knowledge layer. Config comes from env vars; state persists to JSON/JSONL files locally plus a Chroma vector store (Redis/DynamoDB/PostgreSQL stubs exist for production).

### Layer map

```
main.py                          ← CLI entry point; dispatches to modes
config/settings.py               ← LLM, Trading, MarketData, News, Liquidity, Embedding, VectorStore
agents/
  base.py                        ← BaseAgent: ReAct loop, dual LLM (Anthropic/OpenAI-compat), correlation tracking
  signal_agent.py                ← SignalAgent: confluence + pre-injected market context + RAG-aware
  self_assessment.py             ← SelfAssessmentAgent: 7-step parameter evolution (uses Sonnet)
  strategy_supervisor.py         ← StrategyAdaptiveSupervisor: candidate → shadow → active lifecycle
tools/
  trading_tools.py               ← OHLCV, 50+ indicators (via `ta`), VWAP bands, backtester
  evaluation_tools.py            ← Prediction outcome scoring
  notification_tools.py          ← Telegram publisher
  news_tools.py                  ← CryptoPanic + Alpha Vantage + NewsAPI fetchers (normalized schema)
  sentiment_tools.py             ← Crypto/stock Fear & Greed + LLM-based news sentiment digest
  liquidity_tools.py             ← ccxt order book → liquidity zones + slippage probe
  knowledge_tools.py             ← KPI summary, failure modes, RAG retrieval, lesson card
  schema_builder.py              ← JSON schema generation for tool_use
memory/
  store.py                       ← File-backed persistence (JSON + JSONL)
  vector_store.py                ← Chroma wrapper, pluggable embedder (sentence-transformers or OpenAI)
workflows/
  trading_workflow.py            ← LangGraph-based analysis → backtest → assess pipeline
  scheduler.py                   ← SupervisorLoop: 4 async loops (scan, evaluate, adapt, publish)
  market_context.py              ← Per-cycle pre-fetcher: news + F&G + liquidity (parallel)
  knowledge_indexer.py           ← Embeds evaluated predictions into the vector store
```

### LLM configuration & per-agent routing

Two transport modes (selected automatically from env):

- **Anthropic native**: set `ANTHROPIC_API_KEY` → uses `anthropic` SDK
- **OpenAI-compatible gateway**: set `LLM_API_KEY` + `LLM_GATEWAY_URL` → uses `openai` SDK with a custom `base_url` (works with any OpenAI-compatible endpoint, including internal corporate gateways)

Per-agent model assignment (config/settings.py → LLMConfig):

| Agent / use            | Env var               | Default                          | Rationale                          |
|------------------------|-----------------------|----------------------------------|------------------------------------|
| SignalAgent            | `LLM_MODEL_SIGNAL`    | `claude-haiku-4-5-20251001`      | Per-cycle hot path; cheap + fast   |
| SelfAssessmentAgent    | `LLM_MODEL_ASSESSMENT`| `claude-sonnet-4-6`              | Heavy reasoning, runs hourly       |
| News sentiment digest  | `LLM_MODEL_SUMMARIZER`| `claude-haiku-4-5-20251001`      | Single-shot structured output      |

The auth-failure path also falls back from Anthropic to the gateway (see [agents/base.py:136-145](agents/base.py#L136-L145)).

### Signal generation pipeline

`SignalAgent.analyze()` runs three phases:

1. **Pre-fetch context** — `workflows/market_context.py:build_market_context()` fetches news (CryptoPanic / Alpha Vantage / NewsAPI), summarizes sentiment with Haiku, fetches Fear & Greed (alternative.me / CNN), and pulls order-book liquidity via ccxt. All in parallel; each section fails soft.
2. **Pre-compute lessons** — `tools/knowledge_tools.py:build_lesson_card()` aggregates recent KPIs and failure modes from `prediction_evaluations.jsonl`.
3. **Run the agent** — The task prompt embeds both blocks as text. Available tools: `calculate_indicators`, `get_ohlcv`, `get_strategy_params`, `get_recent_outcomes`, `get_kpi_summary`, `get_failure_modes`, `query_similar_setups` (RAG). The agent calls `query_similar_setups` mid-analysis once it has indicator data.

Signal rules: ≥3 confluent indicators; two-phase exit (TP1=50% at 2×ATR → breakeven SL, TP2=50% at 3.5×ATR). Confidence calibrated against the lessons block.

### Knowledge feedback loop (RAG)

`workflows/knowledge_indexer.py:index_evaluation()` runs synchronously after every `MemoryStore.save_prediction_evaluation()` call in [workflows/scheduler.py](workflows/scheduler.py) — it formats the setup+outcome into a stable text document and upserts into Chroma with rich metadata (symbol, timeframe, regime, direction_correct, pnl_pct, etc).

`memory/vector_store.py` is pluggable:
- **Embedder**: `sentence_transformers` (free, local, default model `all-MiniLM-L6-v2`, 384-dim) or `openai` (uses the OpenAI/gateway client)
- **Store**: ChromaDB persistent client; falls back to a no-op stub if `chromadb` isn't installed
- **Retention**: `VECTOR_RETENTION_DAYS` (default 180); call `prune_old_outcomes()` to enforce

The optional dependencies live under the `rag` extra: `pip install -e '.[rag]'`. Without it the system runs unchanged, just without similarity retrieval.

### External providers (free tiers)

| Source             | Type           | Key var                  | Free tier   |
|--------------------|----------------|--------------------------|-------------|
| CryptoPanic        | Crypto news    | `CRYPTOPANIC_API_KEY`    | Yes         |
| Alpha Vantage      | News+sentiment | `ALPHA_VANTAGE_API_KEY`  | 25 req/day  |
| NewsAPI.org        | Broad news     | `NEWSAPI_API_KEY`        | 100 req/day |
| alternative.me F&G | Crypto F&G     | (none)                   | Free        |
| CNN F&G            | Stocks F&G     | (none)                   | Free        |
| ccxt → exchange    | Order book     | (none)                   | Public REST |

All fetchers have a TTL in-process cache (`NEWS_CACHE_TTL_SECONDS`, `FEAR_GREED_CACHE_TTL_SECONDS`, `LIQUIDITY_CACHE_TTL_SECONDS`).

### Self-assessment / adaptation

`SelfAssessmentAgent` runs the 7-step loop (collect → score → generate candidates → A/B backtest → promote winner → persist). `StrategyAdaptiveSupervisor` wraps it with a `candidate → shadow → active` state machine and auto-rollback on short-window degradation.

### Testing conventions

- Synthetic OHLCV; LLM client mocked; env vars isolated per-test (see `isolated_env` fixture).
- External HTTP (news, F&G) mocked via `unittest.mock.patch("...httpx.AsyncClient", FakeClient)`.
- ccxt absence simulated with `monkeypatch.setitem(sys.modules, "ccxt", None)`.
- Vector-store fallback tested by stubbing out `chromadb` similarly.
- Use `make test` for the full suite; `pytest path::Class::test -v` for a single test.
