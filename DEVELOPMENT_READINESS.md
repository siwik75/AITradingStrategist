# Development Readiness Audit

## Goal

This document identifies the concrete gaps preventing this repository from becoming a fully working local and deployment-ready environment. It is blocker-first and intended to streamline future implementation work.

## Current State Summary

The repository is a solid prototype, but it is not yet a fully working environment. The main issues are:

1. The local Python environment is not bootstrapped, so the app cannot start.
2. Several production-facing integrations are stubbed or only described in docs.
3. The Kubernetes manifest is not directly deployable as committed.
4. The README overstates implemented architecture versus actual code.
5. Test execution is not currently wired into the repository setup.

## Verified Local Findings

- `python3 main.py --mode backtest --symbol BTC/USDT --timeframe 4h --days 5` fails immediately with `ModuleNotFoundError: No module named 'structlog'`.
- `python3 -m pytest -q` fails because `pytest` is not installed in the current environment.
- The host `python3` is `3.9.6`, while the Docker image targets Python `3.12`.
- The repository contains a stray literal directory named `{agents,tools,workflows,memory,config,tests,manifests}`, which is not part of the actual package layout and should be removed or documented.

## Priority 0: Immediate Blockers

### 1. Local environment is not reproducibly bootstrapped

Evidence:
- [`requirements.txt`](/Users/simonsiwik/Git/trading-intelligence-agent/requirements.txt#L1) defines runtime packages, but there is no lockfile, `pyproject.toml`, `Makefile`, `pytest.ini`, or setup script in the repo.
- [`README.md`](/Users/simonsiwik/Git/trading-intelligence-agent/README.md#L55) assumes `pip install -r requirements.txt` is sufficient, but the current environment has almost none of the required packages installed.
- [`Dockerfile`](/Users/simonsiwik/Git/trading-intelligence-agent/Dockerfile#L6) uses Python 3.12, while the checked host runtime is Python 3.9.6.

Impact:
- The app does not start locally.
- Tests do not run locally.
- Behavior may diverge between host and container due to Python version mismatch.

Required work:
- Standardize the local runtime to Python 3.12.
- Add a reproducible developer bootstrap path.
- Add a dev dependency definition including `pytest`.

Recommended implementation:
- Add `pyproject.toml` or a pinned `requirements-dev.txt`.
- Add a short bootstrap command in README or a `Makefile`.
- Document the supported Python version explicitly.

### 2. LLM-backed flows are not runnable without manual, undocumented secret provisioning

Evidence:
- [`agents/base.py`](/Users/simonsiwik/Git/trading-intelligence-agent/agents/base.py#L60) initializes Anthropic or OpenAI clients directly during agent construction.
- [`config/settings.py`](/Users/simonsiwik/Git/trading-intelligence-agent/config/settings.py#L18) defaults the API key to an empty string if unset.
- [`main.py`](/Users/simonsiwik/Git/trading-intelligence-agent/main.py#L57) and [`main.py`](/Users/simonsiwik/Git/trading-intelligence-agent/main.py#L201) instantiate agents without validating config first.
- [`.env.example`](/Users/simonsiwik/Git/trading-intelligence-agent/.env.example#L8) exists, but the application never loads `.env` via `python-dotenv`.

Impact:
- A user can follow the documented Quick Start and still fail if the shell environment is not explicitly exported.
- Failures occur late and indirectly, during agent construction or request execution.

Required work:
- Load `.env` in the application startup path.
- Validate required config before constructing LLM clients.
- Provide a fallback mode for local development that can run without live LLM credentials.

Recommended implementation:
- Call `load_dotenv()` at process startup.
- Add a config validation function with actionable error messages.
- Add a mock or offline analysis mode for dev/test.

### 3. Tests are incomplete as a working verification system

Evidence:
- [`tests/test_trading_agent.py`](/Users/simonsiwik/Git/trading-intelligence-agent/tests/test_trading_agent.py#L12) requires `pytest`, which is not present in runtime dependencies.
- The repo has no dedicated test dependency group or test runner config.
- Tests cover synthetic tools and one mocked signal-agent case, but there are no tests for FastAPI routes, config validation, deployment assumptions, or self-assessment persistence behavior.

Impact:
- There is no reliable way to verify the repo end-to-end after changes.
- Regressions in server mode, deployment config, and env handling would go undetected.

Required work:
- Add test dependencies and test runner instructions.
- Expand test coverage to startup, API endpoints, and config validation.

## Priority 1: Functional Gaps

### 4. Strategy persistence and trade-history integrations are mostly stubs

Evidence:
- [`tools/trading_tools.py`](/Users/simonsiwik/Git/trading-intelligence-agent/tools/trading_tools.py#L521) always returns hardcoded strategy params.
- [`tools/trading_tools.py`](/Users/simonsiwik/Git/trading-intelligence-agent/tools/trading_tools.py#L540) `save_strategy_params` only logs and returns `"saved"`.
- [`tools/trading_tools.py`](/Users/simonsiwik/Git/trading-intelligence-agent/tools/trading_tools.py#L551) `get_trade_history` always returns an empty trade list.
- [`memory/store.py`](/Users/simonsiwik/Git/trading-intelligence-agent/memory/store.py#L145) backend implementations for Redis, DynamoDB, Postgres, and S3 are placeholders.

Impact:
- The self-assessment loop is not truly stateful.
- Strategy evolution is not persisted across runs.
- Historical trade analysis is effectively absent.

Required work:
- Decide on one real local backend first.
- Route strategy and trade-history tools through `MemoryStore`.
- Persist assessment outputs and active strategy params.

### 5. “Autonomous strategy evolution” is only partially implemented

Evidence:
- [`agents/self_assessment.py`](/Users/simonsiwik/Git/trading-intelligence-agent/agents/self_assessment.py#L17) claims a full improvement loop.
- In practice, the agent depends on tool functions that do not persist state and return no real history.
- [`workflows/trading_workflow.py`](/Users/simonsiwik/Git/trading-intelligence-agent/workflows/trading_workflow.py#L146) stores `assessment` and `final_params` in transient workflow state only.

Impact:
- The system can propose changes, but “learned” behavior is not durable.
- Re-running the app resets it back to defaults.

Required work:
- Persist approved params.
- Re-read persisted params on startup and before backtests.
- Add tests proving params survive process restarts.

### 6. Market-data integrations are optional in docs but not implemented as a clean runtime choice

Evidence:
- [`tools/trading_tools.py`](/Users/simonsiwik/Git/trading-intelligence-agent/tools/trading_tools.py#L20) defaults `get_ohlcv` to `source="synthetic"`.
- [`requirements.txt`](/Users/simonsiwik/Git/trading-intelligence-agent/requirements.txt#L13) comments out `ccxt` and `yfinance`.
- [`README.md`](/Users/simonsiwik/Git/trading-intelligence-agent/README.md#L74) presents live market integrations as part of the stack.

Impact:
- The repo behaves like a synthetic-data demo, not a live-capable agent environment.
- Switching to live data requires manual dependency edits and has no environment-driven configuration path.

Required work:
- Add a configurable data-source setting.
- Move optional providers behind explicit extras or dependency groups.
- Validate unsupported combinations at startup.

## Priority 1: Deployment Gaps

### 7. Kubernetes manifest is not deployable as committed

Evidence:
- [`manifests/deployment.yaml`](/Users/simonsiwik/Git/trading-intelligence-agent/manifests/deployment.yaml#L14) contains unresolved placeholders like `${NAMESPACE}`, `${IMAGE_TAG}`, `${ECR_REGISTRY}`, and `${AWS_ACCOUNT_ID}`.
- The repo contains no `kustomization.yaml`, Helm chart, or manifest-rendering pipeline to resolve them.
- Vault annotations export secrets in a template, but there is no command/entrypoint wrapper to source those exports into the Python process environment.

Impact:
- Applying the manifest directly will not work.
- Even if rendered, the container may start without injected env vars actually loaded into the process.

Required work:
- Add a manifest rendering mechanism.
- Confirm the Vault injection pattern matches the cluster runtime.
- Add a deployment path that can be executed from the repository.

Recommended implementation:
- Use Kustomize overlays or Helm.
- If Vault agent writes env exports to a file, wrap the container startup to source that file before `python main.py`.

### 8. Readiness checks do not verify real dependencies

Evidence:
- [`main.py`](/Users/simonsiwik/Git/trading-intelligence-agent/main.py#L183) `/ready` always returns success unless the function itself raises.
- It does not validate LLM credentials, market-data providers, or backing stores.

Impact:
- Kubernetes may mark the pod ready even when requests will fail immediately.

Required work:
- Define what “ready” means for this service.
- Add checks for required config and optionally backing services depending on mode.

## Priority 2: Architecture and Documentation Drift

### 9. README describes components that do not exist in code

Evidence:
- [`README.md`](/Users/simonsiwik/Git/trading-intelligence-agent/README.md#L19) references `MarketDataAgent`, `StrategyAgent`, `BacktestAgent`, `RiskAgent`, `ExecutionAgent`, and `MonitorAgent`.
- The actual repository contains only `BaseAgent`, `SignalAgent`, and `SelfAssessmentAgent`.

Impact:
- New contributors will assume capabilities and extension points that are not implemented.
- Roadmapping becomes harder because architecture docs do not match the codebase.

Required work:
- Either implement the documented components or rewrite the README to reflect the actual system.

### 10. Claimed framework alignment is aspirational, not implemented

Evidence:
- [`README.md`](/Users/simonsiwik/Git/trading-intelligence-agent/README.md#L5) claims Strands Agents SDK patterns.
- The repository does not include Strands SDK dependencies or code.
- [`workflows/trading_workflow.py`](/Users/simonsiwik/Git/trading-intelligence-agent/workflows/trading_workflow.py#L218) treats LangGraph as optional, but `langgraph` is commented out in [`requirements.txt`](/Users/simonsiwik/Git/trading-intelligence-agent/requirements.txt#L5).

Impact:
- The stack description is misleading.
- Developers cannot tell which abstractions are authoritative.

Required work:
- Choose one orchestration story and document it honestly.

## Priority 2: Code-Level Risks

### 11. `.env` support is declared as a dependency but unused

Evidence:
- [`requirements.txt`](/Users/simonsiwik/Git/trading-intelligence-agent/requirements.txt#L24) includes `python-dotenv`.
- No file in the repo calls `load_dotenv`.

Impact:
- `.env.example` gives a false sense of setup completeness.

### 12. Graceful shutdown handling is incomplete

Evidence:
- [`main.py`](/Users/simonsiwik/Git/trading-intelligence-agent/main.py#L232) creates `shutdown_event`, but nothing awaits it or uses it to drain work.
- `uvicorn.run(...)` is called directly with no lifespan integration.

Impact:
- The code signals shutdown intent but does not implement graceful task draining.

### 13. Backtest engine is demo-grade and does not model stated position management

Evidence:
- [`agents/signal_agent.py`](/Users/simonsiwik/Git/trading-intelligence-agent/agents/signal_agent.py#L38) states TP1 should partially exit and move SL to breakeven.
- [`tools/trading_tools.py`](/Users/simonsiwik/Git/trading-intelligence-agent/tools/trading_tools.py#L358) treats `tp1` as a full exit, not a partial exit.

Impact:
- Reported backtest behavior does not match the signal methodology.
- Self-assessment is optimizing against a simplified execution model.

## Missing Repository Pieces

The following pieces are missing if the goal is a fully working environment:

- A reproducible local setup contract:
  Python version pin, dev dependencies, and a single documented bootstrap command.
- A real persistence path:
  local file/SQLite/Redis for strategy params, trade history, and assessment history.
- Runtime config validation:
  clear failure modes for missing API keys and invalid environment values.
- A deployment rendering path:
  Kustomize or Helm files to turn placeholders into deployable manifests.
- API verification:
  tests for `/health`, `/ready`, `/analyze`, and `/assess`.
- CI basics:
  lint/test workflow to prevent regressions.
- Basic repository hygiene:
  remove accidental directories and ensure only intended project paths are committed.

## Streamlined Development Plan

### Phase 1: Make local development deterministic

1. Pin Python 3.12 for local development.
2. Add dev dependencies including `pytest`.
3. Load `.env` at startup.
4. Add startup config validation.
5. Document one bootstrap path and one smoke-test path.

Definition of done:
- `python -m pytest` works.
- `python main.py --mode backtest` works on a clean machine after following README only.
- `python main.py --mode server` starts locally.

### Phase 2: Make the core system actually stateful

1. Wire `tools/trading_tools.py` strategy and history functions into `MemoryStore`.
2. Implement one local persistence backend first.
3. Persist self-assessment decisions and rehydrate active params on startup.

Definition of done:
- Running self-assessment can change params.
- A second run sees the updated params.

### Phase 3: Make deployment artifacts real

1. Introduce Kustomize or Helm.
2. Resolve placeholder variables through committed templates.
3. Verify Vault secret injection actually populates process env.
4. Upgrade readiness checks to validate runtime prerequisites.

Definition of done:
- A rendered manifest can be applied without manual editing.
- A pod marked ready can serve analysis requests.

### Phase 4: Align docs with reality

1. Rewrite README architecture to match implemented modules.
2. Separate “implemented now” from “planned next”.
3. Document synthetic-data mode versus live-data mode.

## Recommended First Fix Order

1. Add Python/runtime bootstrap and dev dependencies.
2. Add `.env` loading plus config validation.
3. Get tests running in CI and locally.
4. Implement local persistence for strategy params and assessment history.
5. Fix manifest rendering and secret injection.
6. Update README to remove architecture drift.

## Bottom Line

This repository is closest to a demoable prototype with good structure, not a fully working environment. The fastest path to a stable development setup is to solve packaging/runtime consistency first, then persistence, then deployment templating, then documentation alignment.
