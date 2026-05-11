# Workflow Reference

## Operation Modes

The agent has five operation modes, all driven by `main.py`:

| Mode | Command | Description |
|---|---|---|
| `candles` | `python main.py --mode candles` | OHLCV smoke check with source resolution |
| `analysis` | `python main.py --mode analysis` | Single market analysis → signal JSON |
| `backtest` | `python main.py --mode backtest` | Historical simulation with current strategy |
| `assess` | `python main.py --mode assess` | Self-assessment + strategy evolution |
| `full` | `python main.py --mode full` | Orchestrated analysis → backtest → assess |
| `server` | `python main.py --mode server` | FastAPI HTTP server |

---

## Candles Mode

```
User invokes: python main.py --mode candles --symbol BTC/USDT --timeframe 4h --limit 5

main() → run_cli(args)
  get_ohlcv(symbol, timeframe, limit, source)
    → resolve source from --source or MARKET_DATA_SOURCE
    → fetch candles from synthetic, ccxt, or yfinance
    → normalize and validate OHLCV records

Output JSON:
  {
    "symbol": "BTC/USDT",
    "timeframe": "4h",
    "source": "ccxt",
    "requested_source": "default",
    "count": 5,
    "first_candle": {...},
    "last_candle": {...}
  }
```

This is the recommended smoke test for verifying that live crypto candles are really coming from `ccxt`.

---

## Analysis Mode

```
User invokes: python main.py --mode analysis --symbol BTC/USDT --timeframe 4h

main() → run_cli(args)
  config.validate()          # ensure API key set
  SignalAgent()              # load agent with tools
  agent.analyze(symbol, tf)  # invoke ReAct loop

SignalAgent.analyze()
  task = "Analyze BTC/USDT 4h market conditions..."
  BaseAgent.run(task)

BaseAgent.run()
  Loop (max_iterations=10):
    LLM call with system prompt + tools + user message
    If stop_reason == "end_turn":
      return text content
    If stop_reason == "tool_use":
      for each tool_use block:
        call tool(input_args)
        append tool_result to messages
      continue loop

Tool calls during analysis (typical):
  1. calculate_indicators(BTC/USDT, 4h, full)
     → get_ohlcv(BTC/USDT, 4h, 200, synthetic)
     → ta library computes trend + momentum + volatility + volume + levels
     → return 50+ indicators dict
  2. LLM synthesizes signal

Output JSON:
  {
    "signal": "BUY" | "SELL" | "HOLD",
    "confidence": 0-100,
    "entry_price": float,
    "stop_loss": float,
    "take_profit_1": float,
    "take_profit_2": float,
    "confluence_factors": [...],
    "divergences": [...],
    "market_regime": "trending" | "ranging" | "volatile" | "squeeze",
    "reasoning": "..."
  }
```

---

## Backtest Mode

```
User invokes: python main.py --mode backtest --symbol BTC/USDT --days 30

main() → run_cli(args)
  get_strategy_params()
    → MemoryStore.get_strategy_params()
    → reads ~/.trading-agent/strategy_params.json
    → returns file content OR defaults if file absent

  run_backtest(symbol, tf, params, days)

run_backtest() simulation loop:
  get_ohlcv(symbol, tf, limit=min(days*candles_per_day, 500))
  compute indicators: EMA(fast/slow), RSI, ATR, ADX, MACD, volume SMA

  For each candle:
    If in_position:
      LONG two-phase exit:
        Phase 1 (full position):
          low <= stop_loss → stop_loss exit, full loss
          high >= tp2 (before tp1) → model as 50%@tp1 + 50%@tp2
          high >= tp1 → tp1_hit=True, move stop_loss=entry_price
        Phase 2 (50% position, SL at breakeven):
          low <= stop_loss(=entry) → breakeven_sl exit: 50%×tp1_pnl + 50%×0
          high >= tp2 → tp2 exit: 50%×tp1_pnl + 50%×tp2_pnl
      SHORT direction: symmetric (comparisons inverted)
    Else:
      Check long_signal: EMA crossover up + RSI in range + ADX > min + MACD + volume
      Check short_signal: EMA crossover down + RSI in range + ADX > min + MACD + volume
      Enter position with ATR-based SL/TP1/TP2

  Close open position at last candle (end_of_data exit)
  Compute metrics: win_rate, profit_factor, max_drawdown, sharpe
  Persist completed trades to ~/.trading-agent/trade_signals.jsonl

Output JSON:
  {
    "total_trades": int,
    "win_rate": float,
    "profit_factor": float,
    "max_drawdown_pct": float,
    "sharpe_approx": float,
    "exit_distribution": {"stop_loss": n, "tp1": n, "tp2": n, "breakeven_sl": n, ...},
    "trades": [...up to 20 trades...]
  }
```

---

## Self-Assessment Mode (7-Step Loop)

```
User invokes: python main.py --mode assess --symbol BTC/USDT --days 30

SelfAssessmentAgent.assess_and_evolve()
  Builds 7-step task prompt for BaseAgent

Step 1: get_strategy_params()
  → current params from file or defaults

Step 2: run_backtest(current_params)
  → baseline performance metrics

Step 3: Analyze baseline
  → LLM examines win_rate, profit_factor, max_drawdown, exit_distribution

Step 4: calculate_indicators()
  → check if market regime has changed (trending/ranging/volatile)

Step 5: Propose 2-3 mutations
  Rules:
    - Never change >3 parameters at once
    - Never exceed 2% max risk per trade
    - SL multiplier: 1.0–3.0 range
    - RSI bounds: oversold 20–35, overbought 65–80

Step 6: run_backtest(proposed_params)
  → A/B comparison

Step 7: Compare and decide
  ADOPT → proposed is better across all metrics
  PARTIAL → mixed improvement; adopt conservatively
  REJECT → current params win

  If ADOPT or PARTIAL:
    save_strategy_params(final_params)
      → writes ~/.trading-agent/strategy_params.json atomically
    save_assessment(result)
      → appends to ~/.trading-agent/assessments.jsonl

Output JSON:
  {
    "decision": "ADOPT" | "REJECT" | "PARTIAL",
    "current_performance": {...},
    "proposed_performance": {...},
    "proposed_params": {...},
    "final_params": {...},
    "decision_reasoning": "...",
    "improvement_metrics": {...}
  }
```

---

## Full Mode (Orchestrated Workflow)

```
python main.py --mode full --symbol BTC/USDT --days 30

build_trading_workflow()
  → LangGraph StateGraph if langgraph is installed
  → sequential fallback otherwise

TradingState carries all data between nodes:
  {symbol, timeframe, backtest_days, correlation_id,
   indicators, signal, backtest_current, assessment,
   current_params, final_params, errors, completed_steps}

Node 1: analyze_market
  → calculate_indicators(symbol, timeframe)
  → state.indicators = result

Node 2: generate_signal
  → SignalAgent.analyze(symbol, timeframe)
  → state.signal = result

Node 3: run_current_backtest
  → get_strategy_params() then run_backtest()
  → state.backtest_current = result

Conditional: should_self_assess?
  Triggers if any of:
    win_rate < 55%
    profit_factor < 1.5
    max_drawdown > 10%
    total_trades < 5

  → "self_assess": Node 4 runs
  → "end": workflow ends

Node 4: self_assess (conditional)
  → SelfAssessmentAgent.assess_and_evolve()
  → If ADOPT/PARTIAL: save_strategy_params(final_params)
  → state.assessment = result, state.final_params = final_params

Output: full TradingState dict
```

---

## Server Mode

```
python main.py --mode server

Startup:
  load_dotenv()             # .env → os.environ
  get_config()              # reads env vars
  config.validate()         # check LLM key, risk %, port
  create_app()              # FastAPI factory

  SIGTERM handler:
    server.should_exit = True   # stops accepting new connections
    shutdown_event.set()        # signals lifespan context

  asyncio.run(server.serve())

Request handling:
  GET  /health  → always 200 (liveness)
  GET  /ready   → LLM key check + storage write check → 200 or 503
  POST /analyze → SignalAgent.analyze() per request
  POST /assess  → SelfAssessmentAgent.assess_and_evolve() per request

Graceful shutdown:
  1. SIGTERM received
  2. server.should_exit = True
  3. uvicorn stops accepting new connections
  4. in-flight requests complete (up to terminationGracePeriodSeconds=30)
  5. process exits cleanly
```

---

## Correlation ID Tracking

Every CLI invocation and server request generates a correlation ID (8-char UUID prefix). This ID propagates through:
- `main.py` → agent → tools → structlog fields
- `X-Correlation-ID` request header in server mode
- Persisted in `MemoryStore` records for cross-run tracing

All structlog output is JSON, suitable for FluentBit → CloudWatch aggregation.
