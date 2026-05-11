# Design Patterns

## ReAct Loop (Reason + Act)

All agents use the ReAct pattern via `BaseAgent._run_anthropic()` ([agents/base.py](../agents/base.py)).

```
ReAct iteration:
  1. Send messages + tools to LLM
  2. Receive response
  3. If stop_reason == "end_turn":
       return text content   (agent is done)
  4. If stop_reason == "tool_use":
       for each tool_use block:
         execute tool(input)
         append tool_result message
       goto 1

Safety: max_iterations=10 prevents infinite loops
```

The LLM uses tool calls to gather evidence before forming conclusions. A `SignalAgent` will typically call `calculate_indicators` once, read the results, then emit its signal JSON in the final `end_turn` response. A `SelfAssessmentAgent` calls `run_backtest` twice (before and after proposing mutations) and `save_strategy_params` once.

---

## Dual-Mode LLM Integration

`BaseAgent` supports two LLM protocol modes ([agents/base.py](../agents/base.py)):

| Mode | When used | Protocol |
|---|---|---|
| Anthropic native | `LLM_GATEWAY_URL == "https://api.anthropic.com"` | `tool_use` stop reason, `tool_result` message blocks |
| OpenAI-compatible | Any other gateway URL (GHO, Bedrock, etc.) | `tool_calls` finish reason, `tool` role messages |

The dual-mode design allows the same agent code to work against:
- Direct Anthropic API (development, low-volume)
- A corporate LLM gateway (GHO) that presents an OpenAI-compatible interface

Tool schemas are generated in both formats by `tools/schema_builder.py`.

---

## Automatic Tool Schema Generation

`tools/schema_builder.py` ([tools/schema_builder.py](../tools/schema_builder.py)) derives tool schemas from Python function signatures at runtime.

```python
async def get_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 200,
    source: str = "synthetic"
) -> dict:
    """
    Fetch OHLCV candle data.
    :param symbol: Trading symbol (e.g., BTC/USDT)
    :param timeframe: Candle timeframe (1m, 5m, 15m, 1h, 4h, 1d)
    :param limit: Number of candles to fetch
    :param source: Data source (ccxt, yfinance, synthetic)
    """
```

`build_tool_schema(get_ohlcv)` produces:
```json
{
  "name": "get_ohlcv",
  "description": "Fetch OHLCV candle data.",
  "input_schema": {
    "type": "object",
    "properties": {
      "symbol":    {"type": "string", "description": "Trading symbol (e.g., BTC/USDT)"},
      "timeframe": {"type": "string", "description": "Candle timeframe (1m, 5m, 15m, 1h, 4h, 1d)"},
      "limit":     {"type": "integer", "description": "Number of candles to fetch"},
      "source":    {"type": "string", "description": "Data source (ccxt, yfinance, synthetic)"}
    },
    "required": ["symbol"]
  }
}
```

Rules:
- Parameters with defaults → optional (not in `required`)
- Parameters without defaults → required
- Type mapping: `str→string`, `int→integer`, `float→number`, `bool→boolean`, `dict/list→object/array`
- Description comes from `:param name: text` in the docstring

---

## Self-Assessment Loop

The 7-step self-assessment cycle in `SelfAssessmentAgent` ([agents/self_assessment.py](../agents/self_assessment.py)):

```
┌─────────────────────────────────────────────────────┐
│  1. Load current strategy params (file or defaults) │
│  2. Backtest with current params → baseline metrics │
│  3. Analyze: is win_rate < 55%? drawdown > 10%?    │
│  4. Check market regime (trending/ranging/volatile) │
│  5. Propose 2-3 targeted param mutations            │
│  6. Backtest with proposed params → comparison      │
│  7. Decide: ADOPT | PARTIAL | REJECT                │
│     ADOPT/PARTIAL → persist params + audit record  │
└─────────────────────────────────────────────────────┘
           ▲                              │
           └──────── Next run ────────────┘
```

Safety constraints enforced in the system prompt:
- Maximum 3 parameters changed at once
- `max_risk_pct` never exceeds 2%
- `atr_sl_multiplier` bounded to [1.0, 3.0]
- RSI oversold bounded to [20, 35]; overbought to [65, 80]
- TP1 multiplier always > SL multiplier; TP2 always > TP1

---

## 16-Factor App Compliance

| Factor | Status | Implementation |
|---|---|---|
| I. Codebase | ✅ | Single git repo |
| II. Dependencies | ✅ | `pyproject.toml` with version pins |
| III. Config | ✅ | All config via env vars (`config/settings.py`) |
| IV. Backing services | ✅ | MemoryStore swappable via `MEMORY_BACKEND` |
| V. Build, release, run | ✅ | Docker image + Kustomize overlays |
| VI. Processes | ✅ | Stateless process; state in file/Redis |
| VII. Port binding | ✅ | `PORT` env var, uvicorn |
| VIII. Concurrency | ✅ | async FastAPI, HPA for scale-out |
| IX. Disposability | ✅ | SIGTERM → graceful drain → exit |
| X. Dev/prod parity | ✅ | Kustomize overlays; same image across envs |
| XI. Logs | ✅ | structlog JSON to stdout, FluentBit → CloudWatch |
| XII. Admin processes | Partial | `--mode assess` is the admin task; no scheduled runner |

---

## Singleton Patterns

Two module-level singletons keep state coherent within a process:

### `get_config()` — [config/settings.py](../config/settings.py)

```python
_config: Optional[AppConfig] = None

def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()  # reads os.environ at construction time
    return _config
```

`load_dotenv()` must be called before the first `get_config()` so env vars are present when `AppConfig`'s `default_factory` lambdas evaluate. This is why `main.py` calls `load_dotenv()` at the very top, before any project imports.

### `get_memory_store()` — [memory/store.py](../memory/store.py)

```python
_store: Optional[MemoryStore] = None

def get_memory_store() -> MemoryStore:
    global _store
    if _store is None:
        config = get_config()
        backend = os.getenv("MEMORY_BACKEND", "local")
        _store = MemoryStore(agent_id=config.agent_id, backend=backend)
    return _store
```

Using a singleton means all agent and tool calls within one process share the same `MemoryStore` instance and data directory. Without a singleton, each `MemoryStore()` construction created its own in-memory dict that was immediately discarded.

`reset_config()` and `reset_memory_store()` are provided for test isolation — they set the singletons back to `None`.

---

## Atomic File Writes

The local persistence backend uses atomic writes to prevent corrupt files on process crash mid-write:

```python
def _atomic_write(self, path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX; near-atomic on Windows
```

`os.replace()` is atomic on all POSIX-compliant filesystems. The strategy params file is either the old complete version or the new complete version — never a partial write.

Trade signals and assessments use append-only JSONL files. Each append is a single `fh.write(line)` call — a single-line write is effectively atomic at OS level.
