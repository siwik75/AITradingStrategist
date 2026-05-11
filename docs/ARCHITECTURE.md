# Architecture

## System Overview

The Trading Intelligence Agent is a self-assessing market signal system. It generates trading signals using multi-confluence technical analysis, backtests those signals against historical data, and autonomously evolves its own strategy parameters when performance drops below configured thresholds.

The core loop:
1. Analyze market conditions with 50+ indicators
2. Generate a structured BUY/SELL/HOLD signal with entry, stop-loss, and two profit targets
3. Backtest the current strategy parameters against configured OHLCV data
4. If performance is poor, propose and test parameter mutations
5. Adopt better parameters and persist them across runs

---

## Component Inventory

Three agents, five modules:

| Component | File | Responsibility |
|---|---|---|
| `BaseAgent` | [agents/base.py](../agents/base.py) | ReAct loop, dual-mode LLM, tool execution |
| `SignalAgent` | [agents/signal_agent.py](../agents/signal_agent.py) | Market analysis, signal generation |
| `SelfAssessmentAgent` | [agents/self_assessment.py](../agents/self_assessment.py) | Autonomous strategy evolution |
| Trading Tools | [tools/trading_tools.py](../tools/trading_tools.py) | OHLCV fetch, indicators, backtest engine |
| Schema Builder | [tools/schema_builder.py](../tools/schema_builder.py) | Auto-generates Anthropic/OpenAI tool schemas |
| Memory Store | [memory/store.py](../memory/store.py) | File-backed persistence (local) + production backend stubs |
| Workflow | [workflows/trading_workflow.py](../workflows/trading_workflow.py) | LangGraph StateGraph orchestration |
| Config | [config/settings.py](../config/settings.py) | Environment-based config, validation |
| Entry Point | [main.py](../main.py) | CLI modes + FastAPI server |

---

## Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        main.py                              │
│              CLI modes + FastAPI server                     │
└───────────┬──────────────────────┬──────────────────────────┘
            │                      │
            ▼                      ▼
┌─────────────────┐    ┌─────────────────────────┐
│  SignalAgent    │    │  SelfAssessmentAgent    │
│  (analyze)      │    │  (assess_and_evolve)    │
└────────┬────────┘    └──────────┬──────────────┘
         │                        │
         └──────────┬─────────────┘
                    ▼
         ┌──────────────────┐
         │    BaseAgent     │  ← ReAct loop
         │  (LLM + tools)   │
         └──────┬───────────┘
                │
       ┌────────┼────────────────────┐
       ▼        ▼                    ▼
┌──────────┐ ┌─────────────┐ ┌──────────────┐
│ get_ohlcv│ │ calculate_  │ │ run_backtest │
│          │ │ indicators  │ │              │
└──────────┘ └─────────────┘ └──────────────┘
                                      │
                            ┌─────────┼──────────┐
                            ▼         ▼           ▼
                     ┌──────────┐ ┌────────┐ ┌───────────┐
                     │get/save_ │ │ get_   │ │ Memory    │
                     │strategy_ │ │ trade_ │ │ Store     │
                     │params    │ │ history│ │ (files)   │
                     └──────────┘ └────────┘ └───────────┘
```

---

## Data Flow by Operation Mode

### `--mode analysis`
```
CLI → SignalAgent.analyze()
    → BaseAgent.run(task)
    → LLM (first turn): decide which tool to call
    → calculate_indicators(symbol, timeframe)
    → get_ohlcv() → configured OHLCV source (synthetic/ccxt/yfinance)
    → TA library computes 50+ indicators
    → LLM (second turn): interpret indicators, emit signal JSON
    → return {"signal": "BUY"|"SELL"|"HOLD", "confidence": ..., ...}
```

### `--mode backtest`
```
CLI → get_strategy_params() → MemoryStore (file) or defaults
    → run_backtest(symbol, timeframe, strategy_params, days)
    → get_ohlcv() → 500 candles from the configured source
    → compute EMA/RSI/ATR/ADX/MACD/Volume indicators
    → simulate trades: EMA crossover signals with multi-filter entry
    → two-phase partial exit: TP1=50% + move SL to breakeven, TP2=50%
    → compute win_rate, profit_factor, max_drawdown, sharpe
    → persist each trade via MemoryStore
    → return metrics + completed trades
```

### `--mode assess`
```
CLI → SelfAssessmentAgent.assess_and_evolve()
    → BaseAgent.run(7-step task prompt)
    → Step 1: get_strategy_params() — load current params from file
    → Step 2: run_backtest(current_params)
    → Step 3–4: LLM analyzes performance + market regime
    → Step 5: LLM proposes 2-3 parameter mutations
    → Step 6: run_backtest(proposed_params)
    → Step 7: LLM compares, decides ADOPT/REJECT/PARTIAL
    → if ADOPT/PARTIAL: save_strategy_params() + save_assessment()
    → return assessment JSON with decision + final_params
```

### `--mode full`
```
CLI → build_trading_workflow() → LangGraph StateGraph (or sequential fallback)
    → Node 1: analyze_market (calculate_indicators)
    → Node 2: generate_signal (SignalAgent.analyze)
    → Node 3: run_current_backtest
    → Conditional: should_self_assess?
        → win_rate < 55% OR profit_factor < 1.5 OR max_dd > 10% OR trades < 5
        → Yes: Node 4 self_assess (SelfAssessmentAgent.assess_and_evolve)
            → persist params if ADOPT/PARTIAL
        → No: skip self-assessment
    → return full TradingState
```

### `--mode server`
```
uvicorn (asyncio) → FastAPI app
    GET  /health  → 200 {"status": "healthy"}    (liveness)
    GET  /ready   → 200 or 503 (LLM key check, storage check)   (readiness)
    POST /analyze → SignalAgent.analyze()
    POST /assess  → SelfAssessmentAgent.assess_and_evolve()
SIGTERM → server.should_exit = True → uvicorn drains in-flight requests → exit
```

---

## Extension Points

### Adding a new agent

1. Create `agents/my_agent.py`, subclassing `BaseAgent`
2. Define a system prompt describing the agent's specialty
3. List the tools the agent can call (from `tools/`)
4. Build schemas: `[build_tool_schema(fn) for fn in my_tools]`
5. Call `self.run(task, correlation_id=cid)` to invoke the ReAct loop

### Adding a new tool

1. Add an `async def my_tool(param: type) -> dict` function in `tools/trading_tools.py`
2. Add a `:param name: description` docstring for each parameter
3. Import the function into any agent that should use it
4. Pass `build_tool_schema(my_tool)` in the agent's tools list

The schema is derived automatically from the type annotations and docstring — no manual schema writing needed.

### Switching to a production persistence backend

In `memory/store.py`, the stub methods `_save_redis`, `_save_dynamodb`, `_save_postgres`, `_save_s3` need to be implemented. Set `MEMORY_BACKEND=redis` (or `dynamodb`, `postgres`) in the environment. The `MemoryStore` methods route to the correct backend.
