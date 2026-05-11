# Production Roadmap — Autonomous Signal Service

This document describes the end-to-end path from the current local-development
state to a production-grade, always-on autonomous signal service. It covers
infrastructure requirements, environment configuration, deployment steps, and
the phased rollout plan mapped to the four implementation phases defined in
`AUTONOMOUS_SIGNAL_SERVICE.md`.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                     Supervisor Process                           │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────┐  ┌────────┐  │
│  │  Scan Loop   │  │  Evaluation  │  │ Adapt.   │  │ Pub.   │  │
│  │ (per pair)   │  │    Loop      │  │  Loop    │  │ Retry  │  │
│  └──────┬───────┘  └──────┬───────┘  └────┬─────┘  └───┬────┘  │
│         │                 │               │             │        │
└─────────┼─────────────────┼───────────────┼─────────────┼────────┘
          │                 │               │             │
          ▼                 ▼               ▼             ▼
    ┌─────────────────────────────────────────────────────────┐
    │                   Memory Store                           │
    │  predictions.jsonl          prediction_evaluations.jsonl │
    │  strategy_versions.jsonl    telegram_deliveries.jsonl    │
    │  supervisor_events.jsonl    strategy_params.json         │
    └───────────────────────┬─────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                  ▼
    ┌──────────┐    ┌──────────────┐   ┌─────────────┐
    │ Telegram │    │  LLM API     │   │  Market Data │
    │ Channel  │    │  (Anthropic) │   │  (ccxt/yf)   │
    └──────────┘    └──────────────┘   └─────────────┘
```

---

## Phase 1 — Prediction Registry and Supervisor Skeleton

**Goal:** Turn the single-run analyst into a scheduled scanner that persists
every prediction with a unique ID and an evaluation deadline.

### What was built

| Component | Location |
|---|---|
| `ScanConfig` | `config/settings.py` |
| `PredictionConfig` | `config/settings.py` |
| Prediction registry | `memory/store.py` — `save_prediction`, `get_predictions`, `update_prediction` |
| `SupervisorLoop._scan_loop` | `workflows/scheduler.py` |
| `predictions.jsonl` | `~/.trading-agent/predictions.jsonl` |
| CLI mode `supervisor` | `main.py` — `python main.py --mode supervisor` |
| CLI mode `predictions` | `main.py` — `python main.py --mode predictions` |
| HTTP route `GET /predictions` | `main.py` |

### Required environment variables

```env
TRADING_SYMBOLS=["BTC/USDT","ETH/USDT","SOL/USDT"]
TRADING_TIMEFRAMES=["1h","4h","1d"]
SCAN_INTERVAL_5M=300
SCAN_INTERVAL_15M=900
SCAN_INTERVAL_1H=3600
SCAN_INTERVAL_4H=14400
SCAN_INTERVAL_1D=86400
PREDICTION_EVAL_HORIZON_5M=6
PREDICTION_EVAL_HORIZON_15M=6
PREDICTION_EVAL_HORIZON_1H=8
PREDICTION_EVAL_HORIZON_4H=6
PREDICTION_EVAL_HORIZON_1D=5
MIN_EVALUATED_PREDICTIONS_FOR_ADAPTATION=25
MARKET_DATA_SOURCE=ccxt
CCXT_EXCHANGE=binance
```

### Exit criteria

- `python main.py --mode supervisor` scans all configured symbols without
  manual triggering.
- Every prediction written to `predictions.jsonl` has a unique `prediction_id`
  and an `evaluation_due_at` timestamp.
- `python main.py --mode predictions` lists persisted predictions.

---

## Phase 2 — Telegram Publishing

**Goal:** Publish actionable BUY/SELL signals automatically. Never lose a
prediction if Telegram is unavailable.

### What was built

| Component | Location |
|---|---|
| `TelegramConfig` | `config/settings.py` |
| `TelegramPublisher` | `tools/notification_tools.py` |
| Signal message formatter | `tools/notification_tools.py` — `_format_signal_message` |
| Strategy update formatter | `tools/notification_tools.py` — `_format_strategy_update_message` |
| Degradation alert formatter | `tools/notification_tools.py` — `_format_degradation_alert` |
| Delivery log | `memory/store.py` — `save_telegram_delivery` |
| `telegram_deliveries.jsonl` | `~/.trading-agent/telegram_deliveries.jsonl` |
| Publication retry loop | `workflows/scheduler.py` — `_publication_retry_loop` |

### Required environment variables

```env
TELEGRAM_BOT_TOKEN=<token from @BotFather>
TELEGRAM_CHANNEL_ID=-100xxxxxxxxx
TELEGRAM_THREAD_ID=                        # optional, for forum channels
TELEGRAM_PUBLISH_SIGNALS=true
TELEGRAM_PUBLISH_EVALUATIONS=false
TELEGRAM_PUBLISH_STRATEGY_CHANGES=true
TELEGRAM_PUBLISH_DEGRADATION_ALERTS=true
MIN_CONFIDENCE=70
MIN_RISK_REWARD=2.0
```

### Telegram bot setup

1. Open Telegram and start a chat with @BotFather.
2. Run `/newbot` and follow prompts to get your `TELEGRAM_BOT_TOKEN`.
3. Add the bot as an admin to your target channel.
4. Get the channel ID: forward a message from the channel to @userinfobot or
   use the Telegram API `getUpdates` endpoint.
5. Set `TELEGRAM_CHANNEL_ID` to the numeric ID (e.g. `-1001234567890`).

### Publish filter

A signal is published only when all conditions are true:
- `signal` in `(BUY, SELL)`
- `confidence >= MIN_CONFIDENCE`
- `risk_reward_ratio >= MIN_RISK_REWARD`
- `TELEGRAM_PUBLISH_SIGNALS=true`

### Exit criteria

- Actionable signals appear in the Telegram channel automatically.
- Failed deliveries appear in `telegram_deliveries.jsonl` with `success=false`.
- The retry loop re-attempts failed short messages every 10 minutes.

---

## Phase 3 — Prediction Evaluation

**Goal:** Score every matured prediction against realized market behavior to
create measurable learning data.

### What was built

| Component | Location |
|---|---|
| `evaluate_prediction` | `tools/evaluation_tools.py` |
| `compute_rolling_kpis` | `tools/evaluation_tools.py` |
| `should_trigger_adaptation` | `tools/evaluation_tools.py` |
| `SupervisorLoop._evaluation_loop` | `workflows/scheduler.py` |
| `prediction_evaluations.jsonl` | `~/.trading-agent/prediction_evaluations.jsonl` |
| CLI mode `kpis` | `main.py` — `python main.py --mode kpis` |
| HTTP route `GET /kpis` | `main.py` |

### Evaluation metrics

| Metric | Description |
|---|---|
| `direction_correct` | Signal direction matches horizon close |
| `tp1_reached` | TP1 was reachable before SL |
| `tp2_reached` | TP2 was reachable |
| `sl_reached_first` | SL was hit before TP1 |
| `mfe_pct` | Max favorable excursion % |
| `mae_pct` | Max adverse excursion % |
| `outcome_score` | Composite 0–1 score |
| `confidence_calibration_bucket` | high / medium / low |

### Rolling KPI windows

| Window | Records |
|---|---|
| Short | Last 25 evaluated predictions |
| Medium | Last 100 evaluated predictions |
| Long | Last 250 evaluated predictions |

### Adaptation triggers (short window)

| Condition | Threshold |
|---|---|
| Directional accuracy below | `MIN_DIRECTIONAL_ACCURACY=0.50` |
| TP1 reach rate below | `MIN_TP1_REACH_RATE=0.35` |
| False-positive rate above | `MAX_FALSE_POSITIVE_RATE=0.55` |
| High-confidence underperforms medium-confidence | automatic |

### Exit criteria

- Every prediction with elapsed `evaluation_due_at` receives a scored
  evaluation record in `prediction_evaluations.jsonl`.
- `python main.py --mode kpis` prints short/medium/long window quality.

---

## Phase 4 — Controlled Adaptation

**Goal:** Propose, validate, and adopt conservative parameter mutations.
All changes are versioned, auditable, and reversible.

### What was built

| Component | Location |
|---|---|
| `AdaptationConfig` | `config/settings.py` |
| `AdaptiveStrategySupervisor` | `agents/strategy_supervisor.py` |
| Candidate/shadow/active lifecycle | `agents/strategy_supervisor.py` |
| Shadow validation via backtest comparison | `agents/strategy_supervisor.py` — `_shadow_validate` |
| Rollback support | `agents/strategy_supervisor.py` — `rollback_if_degraded` |
| `strategy_versions.jsonl` | `~/.trading-agent/strategy_versions.jsonl` |
| `supervisor_events.jsonl` | `~/.trading-agent/supervisor_events.jsonl` |
| `SupervisorLoop._adaptation_loop` | `workflows/scheduler.py` |
| CLI mode `adapt` | `main.py` — `python main.py --mode adapt` |
| HTTP routes `POST /adapt`, `GET /strategy/versions`, `GET /supervisor/events` | `main.py` |

### Adaptation lifecycle

```
SelfAssessmentAgent proposes params
         │
         ▼
    [candidate]  — registered in strategy_versions.jsonl
         │
         ▼  shadow validation:
         │   • backtest proposed params
         │   • compare to current params on 3 key metrics
         │   • enforce drawdown hard limit (15%)
         │   • enforce mutation budget
         │
    [shadow] — registered with validation results
         │
    passes? ─── NO ──→ [rejected] + consecutive_failed_promotions++
         │
        YES
         │
         ▼
    [active] — params written to strategy_params.json
         │
     post-promotion rollback check (5-minute delay)
         │
    short-window KPIs still degrade? ─── YES ──→ [rolled_back] → revert params
```

### Required environment variables

```env
ENABLE_AUTONOMOUS_ADAPTATION=false          # set to true to activate
ADAPTATION_INTERVAL_HOURS=24
MAX_PARAMETER_MUTATIONS_PER_CYCLE=2
ROLLBACK_ON_SHORT_WINDOW_DEGRADATION=true
MIN_DIRECTIONAL_ACCURACY=0.50
MIN_TP1_REACH_RATE=0.35
MAX_FALSE_POSITIVE_RATE=0.55
MAX_FAILED_PROMOTIONS_BEFORE_FREEZE=3
```

### Safety constraints (hard-coded)

- Maximum 2 parameter mutations per cycle (configurable via `MAX_PARAMETER_MUTATIONS_PER_CYCLE`).
- Shadow validation must improve ≥ 2 of 3 backtest metrics.
- Proposed drawdown must not exceed 15%.
- Proposed params must generate ≥ 5 backtest trades.
- Adaptation freezes after `MAX_FAILED_PROMOTIONS_BEFORE_FREEZE` consecutive failures.
- Full audit trail in `strategy_versions.jsonl` and `supervisor_events.jsonl`.

### Exit criteria

- The service can propose, validate, and adopt controlled mutations.
- Every adopted change is in `strategy_versions.jsonl` with `lifecycle=active`.
- A degraded short window after promotion triggers automatic rollback.

---

## Phase 5 — Production Hardening

### Infrastructure requirements

#### Minimum production setup

| Resource | Specification |
|---|---|
| Compute | 1 CPU, 512 MB RAM (single supervisor process) |
| Storage | 1 GB persistent volume for `~/.trading-agent/` |
| Network | Outbound HTTPS to Anthropic API, Binance/ccxt, Telegram |
| Process manager | systemd or Kubernetes Deployment |

#### Recommended production setup

| Resource | Specification |
|---|---|
| Compute | 2 CPU, 1 GB RAM |
| Storage | 10 GB persistent volume (1 year of JSONL files) |
| Redis | Optional — for fast strategy_params cache |
| PostgreSQL / Aurora | Optional — for structured analytics queries |

### Docker deployment

```bash
# Build
docker build -t trading-intelligence-agent:latest .

# Run supervisor (autonomous mode)
docker run -d \
  --name trading-agent-supervisor \
  --restart unless-stopped \
  -v trading-agent-data:/root/.trading-agent \
  --env-file .env \
  trading-intelligence-agent:latest \
  python main.py --mode supervisor

# Run API server (alongside supervisor)
docker run -d \
  --name trading-agent-api \
  --restart unless-stopped \
  -v trading-agent-data:/root/.trading-agent \
  -p 8080:8080 \
  --env-file .env \
  trading-intelligence-agent:latest \
  python main.py --mode server
```

### Kubernetes deployment

The [manifests/](../manifests/) directory contains Kustomize overlays for
base, staging, and production environments.

#### Key manifests

```
manifests/
  base/
    deployment.yaml      # supervisor + API server containers
    service.yaml         # ClusterIP for API
    configmap.yaml       # non-secret env vars
    pvc.yaml             # PersistentVolumeClaim for data dir
  overlays/
    staging/             # reduced replicas, synthetic data
    production/          # full config, real market data, Telegram enabled
```

#### Supervisor deployment pattern

Run the supervisor as a long-lived `Deployment` (not a `CronJob`), because:
- The scan loop must stay alive between cycles to maintain per-pair state.
- The retry loop needs a persistent process.
- The evaluation loop checks every 5 minutes.

```yaml
# manifests/base/deployment.yaml (excerpt)
containers:
  - name: supervisor
    image: trading-intelligence-agent:latest
    command: ["python", "main.py", "--mode", "supervisor"]
    env:
      - name: ENVIRONMENT
        value: production
      - name: MARKET_DATA_SOURCE
        value: ccxt
      - name: ENABLE_AUTONOMOUS_ADAPTATION
        value: "false"    # enable only after Phase 3 KPIs stabilize
    volumeMounts:
      - name: data
        mountPath: /root/.trading-agent
    livenessProbe:
      # Supervisor has no HTTP; use a file-mtime probe
      exec:
        command:
          - sh
          - -c
          - "find /root/.trading-agent -name 'predictions.jsonl' -newer /tmp/.probe -type f | grep -q ."
      initialDelaySeconds: 120
      periodSeconds: 600

  - name: api
    image: trading-intelligence-agent:latest
    command: ["python", "main.py", "--mode", "server"]
    ports:
      - containerPort: 8080
    livenessProbe:
      httpGet:
        path: /health
        port: 8080
    readinessProbe:
      httpGet:
        path: /ready
        port: 8080
```

#### Secrets management

Never store credentials in ConfigMaps. Use Kubernetes Secrets or an external
secrets manager (e.g. AWS Secrets Manager, Vault):

```bash
kubectl create secret generic trading-agent-secrets \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=TELEGRAM_BOT_TOKEN=... \
  --from-literal=TELEGRAM_CHANNEL_ID=...
```

Reference in the Deployment:

```yaml
envFrom:
  - secretRef:
      name: trading-agent-secrets
```

### Persistent Volume

All JSONL files and `strategy_params.json` must survive pod restarts.

```yaml
# manifests/base/pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: trading-agent-data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
```

Set `TRADING_AGENT_DATA_DIR=/data` and mount the PVC at `/data`.

---

## Full Environment Variable Reference

### LLM

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Anthropic direct API key |
| `LLM_GATEWAY_URL` | `https://api.anthropic.com` | LLM gateway URL |
| `LLM_MODEL` | `claude-sonnet-4-6` | Primary model |
| `LLM_MODEL_FAST` | `claude-haiku-4-5-20251001` | Fast model for light tasks |

### Trading

| Variable | Default | Description |
|---|---|---|
| `TRADING_SYMBOLS` | `["BTC/USDT","ETH/USDT"]` | Instrument watchlist (JSON array) |
| `TRADING_TIMEFRAMES` | `["1h","4h","1d"]` | Timeframes to scan (JSON array) |
| `MIN_CONFIDENCE` | `70` | Minimum confidence to publish a signal |
| `MIN_RISK_REWARD` | `2.0` | Minimum risk/reward ratio to publish |
| `DRY_RUN` | `true` | Never place real orders |

### Market Data

| Variable | Default | Description |
|---|---|---|
| `MARKET_DATA_SOURCE` | `synthetic` | `synthetic`, `ccxt`, `yfinance`, `auto` |
| `MARKET_DATA_FALLBACK_TO_SYNTHETIC` | `true` | Fall back to synthetic on provider failure |
| `CCXT_EXCHANGE` | `binance` | Exchange for ccxt source |

### Scan Cadence

| Variable | Default | Description |
|---|---|---|
| `SCAN_INTERVAL_5M` | `300` | Seconds between 5m scans |
| `SCAN_INTERVAL_15M` | `900` | Seconds between 15m scans |
| `SCAN_INTERVAL_1H` | `3600` | Seconds between 1h scans |
| `SCAN_INTERVAL_4H` | `14400` | Seconds between 4h scans |
| `SCAN_INTERVAL_1D` | `86400` | Seconds between 1d scans |

### Prediction Evaluation

| Variable | Default | Description |
|---|---|---|
| `PREDICTION_EVAL_HORIZON_5M` | `6` | Candles after signal to evaluate 5m predictions |
| `PREDICTION_EVAL_HORIZON_15M` | `6` | Candles after signal to evaluate 15m predictions |
| `PREDICTION_EVAL_HORIZON_1H` | `8` | Candles after signal to evaluate 1h predictions |
| `PREDICTION_EVAL_HORIZON_4H` | `6` | Candles after signal to evaluate 4h predictions |
| `PREDICTION_EVAL_HORIZON_1D` | `5` | Candles after signal to evaluate 1d predictions |
| `MIN_EVALUATED_PREDICTIONS_FOR_ADAPTATION` | `25` | Minimum evaluated predictions before adaptation |

### Adaptation Control

| Variable | Default | Description |
|---|---|---|
| `ENABLE_AUTONOMOUS_ADAPTATION` | `false` | Enable autonomous strategy mutation |
| `ADAPTATION_INTERVAL_HOURS` | `24` | Hours between adaptation cycles |
| `MAX_PARAMETER_MUTATIONS_PER_CYCLE` | `2` | Max params changed per cycle |
| `ROLLBACK_ON_SHORT_WINDOW_DEGRADATION` | `true` | Auto-rollback after promotion if KPIs worsen |
| `MIN_DIRECTIONAL_ACCURACY` | `0.50` | Trigger adaptation below this accuracy |
| `MIN_TP1_REACH_RATE` | `0.35` | Trigger adaptation below this TP1 rate |
| `MAX_FALSE_POSITIVE_RATE` | `0.55` | Trigger adaptation above this rate |
| `MAX_FAILED_PROMOTIONS_BEFORE_FREEZE` | `3` | Freeze adaptation after N consecutive failures |

### Telegram

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | — | Target channel ID |
| `TELEGRAM_THREAD_ID` | — | Forum thread ID (optional) |
| `TELEGRAM_PUBLISH_SIGNALS` | `true` | Publish BUY/SELL signals |
| `TELEGRAM_PUBLISH_EVALUATIONS` | `false` | Publish evaluation summaries |
| `TELEGRAM_PUBLISH_STRATEGY_CHANGES` | `true` | Publish strategy updates |
| `TELEGRAM_PUBLISH_DEGRADATION_ALERTS` | `true` | Publish degradation alerts |

### Infrastructure

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | FastAPI server port |
| `LOG_LEVEL` | `INFO` | Structured log level |
| `ENVIRONMENT` | `development` | `development` or `production` |
| `TRADING_AGENT_DATA_DIR` | `~/.trading-agent` | Data directory for all JSONL files |
| `MEMORY_BACKEND` | `local` | `local`, `redis`, `dynamodb`, `postgres` |
| `AGENT_ID` | `trading-intelligence-agent` | Agent identifier |

---

## Data File Reference

| File | Purpose | Retention |
|---|---|---|
| `strategy_params.json` | Active strategy parameters | Overwritten on each adoption |
| `trade_signals.jsonl` | Backtest trade records | Append-only |
| `signal_notifications.jsonl` | Manual operator suggestions | Append-only |
| `manual_trade_reviews.jsonl` | Operator outcome reports | Append-only |
| `assessments.jsonl` | Self-assessment results | Append-only |
| `predictions.jsonl` | Full prediction records | Append-only, status updated in-place |
| `prediction_evaluations.jsonl` | Scored prediction outcomes | Append-only |
| `strategy_versions.jsonl` | Strategy version lifecycle audit | Append-only |
| `telegram_deliveries.jsonl` | Telegram delivery log | Append-only |
| `supervisor_events.jsonl` | Supervisor decision log | Append-only |

All files are plain newline-delimited JSON. They are human-readable and can be
inspected with `jq` or imported into any analytics tool.

---

## API Reference

### Health

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness probe |
| `/ready` | GET | Readiness probe (LLM key, storage, Redis) |

### Analysis

| Endpoint | Method | Description |
|---|---|---|
| `/analyze` | POST | Trigger a single market analysis |
| `/assess` | POST | Trigger a self-assessment cycle |
| `/predictions` | GET | List persisted predictions |
| `/kpis` | GET | Rolling quality KPIs |

### Signals and Operator Feedback

| Endpoint | Method | Description |
|---|---|---|
| `/signals` | GET | List actionable signal notifications |
| `/report` | POST | Record manual trade outcome |

### Strategy Governance

| Endpoint | Method | Description |
|---|---|---|
| `/adapt` | POST | Trigger one adaptation cycle immediately |
| `/strategy/versions` | GET | Strategy version audit trail |
| `/supervisor/events` | GET | Supervisor decision log |

---

## Operational Runbook

### Starting the service

```bash
# Development (local file backend, synthetic data)
python main.py --mode supervisor

# Production (Kubernetes)
kubectl apply -k manifests/overlays/production/
```

### Checking signal quality

```bash
python main.py --mode kpis
# or
curl http://localhost:8080/kpis | jq
```

### Inspecting predictions

```bash
python main.py --mode predictions --limit 20
# or
curl "http://localhost:8080/predictions?limit=20&status=active" | jq
```

### Manually triggering adaptation

```bash
python main.py --mode adapt --symbol BTC/USDT --timeframe 4h
# or
curl -X POST http://localhost:8080/adapt \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC/USDT","timeframe":"4h"}'
```

### Viewing strategy version history

```bash
curl http://localhost:8080/strategy/versions | jq
```

### Reviewing failed Telegram deliveries

```bash
python -c "
import asyncio
from tools.notification_tools import get_failed_deliveries
print(asyncio.run(get_failed_deliveries()))
"
```

### Reading supervisor events (debugging)

```bash
tail -f ~/.trading-agent/supervisor_events.jsonl | jq
```

---

## Recommended Rollout Sequence

1. **Week 1** — Deploy Phase 1. Run `supervisor` mode with `MARKET_DATA_SOURCE=ccxt`.
   Verify predictions are persisted. Monitor logs.

2. **Week 2** — Configure Telegram. Set `TELEGRAM_PUBLISH_SIGNALS=true`.
   Verify signals arrive in the channel. Confirm `telegram_deliveries.jsonl`
   records each send.

3. **Week 3–4** — Accumulate evaluated predictions (wait for
   `MIN_EVALUATED_PREDICTIONS_FOR_ADAPTATION=25` evaluations). Inspect KPIs
   via `/kpis`. Validate evaluation logic against known candle data.

4. **Month 2** — Enable autonomous adaptation with caution:
   `ENABLE_AUTONOMOUS_ADAPTATION=true`. Keep `MAX_PARAMETER_MUTATIONS_PER_CYCLE=2`
   and `ROLLBACK_ON_SHORT_WINDOW_DEGRADATION=true`. Monitor
   `strategy_versions.jsonl` after the first promotion.

5. **Month 3+** — Production hardening. Add alerting on `supervisor_events.jsonl`
   entries with `event_type=adaptation_frozen`. Set up log aggregation. Consider
   migrating from local file backend to PostgreSQL for analytics.

---

## Acceptance Criteria

The service is considered agentically operational when all of the following hold:

- [ ] Scans the configured instrument universe without manual triggering.
- [ ] Publishes actionable signals to Telegram automatically.
- [ ] Every published prediction is persisted in `predictions.jsonl`.
- [ ] Every matured prediction receives a scored evaluation.
- [ ] Rolling KPIs are available via `/kpis` at any time.
- [ ] Adaptation decisions are driven by both backtest comparisons and
      realized prediction outcomes.
- [ ] Parameter changes appear in `strategy_versions.jsonl` with full audit
      trail and are reversible.
- [ ] The service can run unattended for extended periods without losing state.
