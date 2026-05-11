# Autonomous Signal Service

## Goal

Define the target architecture for turning this repository into an always-on agentic signal service that:

- scans a configured list of instruments and timeframes
- generates structured BUY/SELL/HOLD predictions
- publishes actionable predictions to a Telegram channel
- tracks each prediction against subsequent market behavior
- ingests manual operator feedback when available
- periodically re-evaluates strategy quality
- adapts thresholds, indicator usage, and evaluation terms over time
- improves conservatively while preserving auditability and risk controls

This document is intentionally implementation-oriented. It is not a product pitch; it is a blueprint for building the next real version of the service.

---

## Baseline

The current repository already contains the right core primitives:

- `SignalAgent` can analyze one instrument/timeframe and emit a structured signal
- `SelfAssessmentAgent` can compare parameter sets and persist improved values
- `tools/trading_tools.py` already provides OHLCV loading, indicator calculation, and backtesting
- `memory/store.py` already persists strategy params, trade history, assessments, signal notifications, and operator outcome reports locally
- `main.py` already exposes CLI modes and HTTP routes

What is still missing is the supervisory loop that turns those primitives into an autonomous service.

---

## Target Outcome

The finished service should behave like this:

1. Wake up on a schedule.
2. Scan a watchlist of instruments and timeframes.
3. Generate signals for each candidate.
4. Filter out weak or invalid setups.
5. Persist each prediction with full context.
6. Publish actionable predictions to Telegram.
7. Revisit each prediction after a defined evaluation horizon.
8. Score prediction quality against realized market behavior.
9. Periodically run self-assessment using both backtests and realized prediction outcomes.
10. Propose and validate small strategy mutations.
11. Adopt only changes that improve stability and predictive quality.
12. Continue running with the updated configuration.

This is a prediction-and-learning service, not an auto-execution bot.

---

## System Model

### Core loops

The service should have four recurring loops.

### 1. Scan loop

Purpose:
- find current candidate setups across a configured instrument universe

Responsibilities:
- iterate `TRADING_SYMBOLS` x `TRADING_TIMEFRAMES`
- call `SignalAgent.analyze()`
- reject malformed or low-confidence responses
- rank signals by quality and relevance
- persist every scan result or, at minimum, every actionable result

Recommended cadence:
- every 5m for `5m` and `15m`
- every 15m for `1h`
- every 1h for `4h`
- every 4h for `1d`

### 2. Prediction evaluation loop

Purpose:
- determine whether prior predictions were directionally or economically correct

Responsibilities:
- identify predictions whose evaluation horizon has elapsed
- fetch realized candles after the prediction timestamp
- compute outcome against a clear scoring model
- update prediction records with measured outcome

Examples of evaluation terms:
- direction correct after horizon close
- max favorable excursion
- max adverse excursion
- whether TP1 was reachable
- whether SL was reachable first
- whether predicted regime matched realized regime
- whether confidence was well calibrated

This is the loop that turns "signals" into measurable learning data.

### 3. Strategy adaptation loop

Purpose:
- improve the prediction engine without overfitting

Responsibilities:
- aggregate recent prediction outcomes
- compare recent realized quality to configured minimum thresholds
- run `SelfAssessmentAgent` when performance degrades
- mutate only a small set of terms at a time
- validate proposals with backtests and recent realized outcomes
- adopt only if the improvement is robust

Examples of mutable terms:
- confidence threshold
- minimum ADX
- RSI boundaries
- ATR stop and target multipliers
- whether volume filter is active
- whether selected indicator families are enabled
- scoring weights used to rank candidate signals

### 4. Publication loop

Purpose:
- notify humans about actionable predictions and major strategy changes

Responsibilities:
- publish BUY/SELL signals to Telegram
- publish strategy-change summaries when parameters are updated
- publish degraded-performance alerts if prediction quality drops sharply
- persist delivery outcomes for auditing and replay

---

## Proposed Components

The following components should be added on top of the current codebase.

### `InstrumentUniverseScheduler`

Responsibility:
- schedule analysis jobs for the configured watchlist

Suggested location:
- `workflows/scheduler.py`

Inputs:
- `TRADING_SYMBOLS`
- `TRADING_TIMEFRAMES`
- per-timeframe cadence config

Outputs:
- scan jobs

### `PredictionRegistry`

Responsibility:
- persist every prediction with enough context to evaluate it later

Suggested location:
- `memory/store.py`
- thin tool wrappers in `tools/trading_tools.py`

Each prediction record should include:
- `prediction_id`
- `timestamp`
- `symbol`
- `timeframe`
- `source`
- `signal`
- `confidence`
- `entry_price`
- `stop_loss`
- `take_profit_1`
- `take_profit_2`
- `strategy_params_snapshot`
- `indicator_snapshot`
- `model`
- `correlation_id`
- `evaluation_due_at`
- `telegram_message_id` if published

### `PredictionEvaluator`

Responsibility:
- turn historical predictions into scored outcomes

Suggested location:
- `tools/evaluation_tools.py`

Inputs:
- prior prediction record
- future OHLCV after prediction timestamp
- evaluation config

Outputs:
- `direction_correct`
- `tp1_reached`
- `tp2_reached`
- `sl_reached`
- `mfe_pct`
- `mae_pct`
- `outcome_score`
- `confidence_calibration_bucket`
- `evaluation_notes`

### `AdaptiveStrategySupervisor`

Responsibility:
- decide when to invoke self-assessment
- mediate which changes are safe to adopt

Suggested location:
- `agents/strategy_supervisor.py`

Responsibilities:
- compute rolling KPIs
- compare to thresholds
- invoke `SelfAssessmentAgent`
- enforce mutation budget
- require shadow validation before adoption
- support rollback if recent outcomes deteriorate after adoption

### `TelegramPublisher`

Responsibility:
- publish predictions and status updates to Telegram

Suggested location:
- `tools/notification_tools.py`

Responsibilities:
- format human-readable messages
- post to Telegram channel
- retry on transient failures
- persist delivery status

Required credentials:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHANNEL_ID`

Optional:
- `TELEGRAM_THREAD_ID`
- `TELEGRAM_PUBLISH_SIGNALS=true`
- `TELEGRAM_PUBLISH_STRATEGY_CHANGES=true`
- `TELEGRAM_PUBLISH_DEGRADATION_ALERTS=true`

---

## Prediction Lifecycle

### Step 1. Analyze

For each symbol/timeframe:
- fetch current candles
- compute indicators
- run `SignalAgent`

### Step 2. Persist

Persist the full prediction before publication.

This must happen even if Telegram fails. Prediction storage is the source of truth; Telegram is only a delivery channel.

### Step 3. Publish

If the signal is actionable and passes publication filters:
- send to Telegram
- store Telegram delivery metadata

Suggested publish filter:
- only `BUY` and `SELL`
- only if confidence >= `MIN_CONFIDENCE`
- only if risk/reward passes the configured threshold
- optionally only top N signals per cycle

### Step 4. Evaluate later

After the evaluation horizon:
- compare the prediction with realized market behavior
- update the prediction record with evaluation results

### Step 5. Learn

On a longer cadence:
- aggregate recent evaluated predictions
- trigger adaptation if KPIs deteriorate

---

## Evaluation Framework

The system should not learn from a single scalar like win rate alone. It should optimize a compact but multi-dimensional score.

### Required metrics

- directional accuracy
- precision for published actionable signals
- average favorable excursion before invalidation
- average adverse excursion after publication
- TP1 reach rate
- TP2 reach rate
- stop-loss first-hit rate
- false-positive rate
- HOLD quality
  HOLD quality means: did a skipped market actually avoid low-quality conditions?
- confidence calibration
  High-confidence signals should outperform low-confidence ones consistently.

### Recommended rolling windows

- short window: last 25 evaluated predictions
- medium window: last 100 evaluated predictions
- long window: last 250 evaluated predictions

### Trigger examples for adaptation

- directional accuracy in the short window < 50%
- TP1 reach rate drops below 35%
- false-positive rate exceeds 55%
- high-confidence bucket underperforms medium-confidence bucket
- average adverse excursion worsens materially vs prior window

---

## Adaptation Policy

The service should evolve, but slowly and with controls.

### Rules

- never change more than 2 or 3 parameters per adaptation cycle
- never relax risk controls and entry filters all at once
- maintain a changelog of every adopted mutation
- keep a full snapshot of the prior parameter set for rollback
- require both backtest support and realized-outcome support before permanent adoption

### Mutation classes

Safe first-wave mutations:
- `min_confidence`
- `min_adx`
- `rsi_oversold`
- `rsi_overbought`
- `use_volume_filter`
- `atr_sl_multiplier`
- `atr_tp1_multiplier`
- `atr_tp2_multiplier`

Second-wave mutations:
- indicator weighting
- candidate ranking logic
- publication thresholding
- per-timeframe evaluation horizons

Later mutations:
- indicator family activation/deactivation
- symbol-specific parameter overrides
- regime-specific parameter sets

### Adoption model

Use a three-stage promotion model.

1. `candidate`
   Proposed by self-assessment but not active.

2. `shadow`
   Evaluated in backtests and optionally compared against the current config in live prediction scoring without publication changes.

3. `active`
   Promoted after passing validation.

---

## Telegram Publishing Model

Telegram should be treated as a notification sink, not the source of record.

### Message types

#### Actionable signal

Suggested content:
- symbol and timeframe
- signal direction
- confidence
- entry, SL, TP1, TP2
- regime and trend summary
- top confluence points
- prediction id

#### Evaluation summary

Suggested content:
- prediction id
- outcome
- max favorable excursion
- max adverse excursion
- whether TP1/TP2/SL was hit

#### Strategy update

Suggested content:
- previous params vs new params
- why the change was adopted
- validation summary

### Delivery guarantees

- publish idempotently when possible
- record failed sends for replay
- never lose the prediction if Telegram is unavailable

---

## Data Model

The local backend can support the first production-like version if the file model is expanded.

### Existing files

- `strategy_params.json`
- `trade_signals.jsonl`
- `assessments.jsonl`
- `signal_notifications.jsonl`
- `manual_trade_reviews.jsonl`

### Recommended new files

- `predictions.jsonl`
- `prediction_evaluations.jsonl`
- `strategy_versions.jsonl`
- `telegram_deliveries.jsonl`
- `supervisor_events.jsonl`

### Why separate files

- predictions and evaluations should be append-only
- strategy versioning should be auditable
- Telegram delivery should be replayable independently of trading logic
- supervisor decisions should be inspectable during failures

---

## Scheduling and Orchestration

The service needs a persistent orchestrator. There are two realistic options.

### Option A. In-process scheduler

Suggested for the first autonomous version.

Use:
- `asyncio`
- a background cadence manager
- one process running scan, evaluate, adapt, and publish loops

Pros:
- simple
- low operational overhead
- fits the current architecture

Cons:
- less resilient for large universes
- weaker concurrency controls

### Option B. External job orchestration

Suggested once the service grows.

Use:
- CronJobs
- a queue
- distributed workers

Pros:
- better scaling
- clearer separation of loop types

Cons:
- more operational complexity

Recommendation:
- start with Option A
- move to Option B only after the prediction registry and evaluation model are stable

---

## Configuration Additions

The following environment variables should be added.

### Universe and scheduling

- `TRADING_SYMBOLS`
- `TRADING_TIMEFRAMES`
- `SCAN_INTERVAL_5M`
- `SCAN_INTERVAL_15M`
- `SCAN_INTERVAL_1H`
- `SCAN_INTERVAL_4H`
- `SCAN_INTERVAL_1D`

### Prediction evaluation

- `PREDICTION_EVAL_HORIZON_5M`
- `PREDICTION_EVAL_HORIZON_15M`
- `PREDICTION_EVAL_HORIZON_1H`
- `PREDICTION_EVAL_HORIZON_4H`
- `PREDICTION_EVAL_HORIZON_1D`
- `MIN_EVALUATED_PREDICTIONS_FOR_ADAPTATION`

### Adaptation control

- `ENABLE_AUTONOMOUS_ADAPTATION`
- `ADAPTATION_INTERVAL_HOURS`
- `MAX_PARAMETER_MUTATIONS_PER_CYCLE`
- `ROLLBACK_ON_SHORT_WINDOW_DEGRADATION`

### Telegram

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHANNEL_ID`
- `TELEGRAM_THREAD_ID`
- `TELEGRAM_PUBLISH_SIGNALS`
- `TELEGRAM_PUBLISH_EVALUATIONS`
- `TELEGRAM_PUBLISH_STRATEGY_CHANGES`

---

## Safety Constraints

This service should remain conservative.

### Hard rules

- do not auto-execute trades in this phase
- do not publish low-confidence signals
- do not mutate too many parameters at once
- do not adopt a new strategy version without audit history
- do not erase historical predictions or evaluations

### Recommended safeguards

- symbol cooldown after repeated low-quality predictions
- timeframe cooldown after repeated degradation
- freeze adaptation after N failed candidate promotions
- alert to Telegram when the system enters degraded mode

---

## Implementation Roadmap

### Phase 1. Prediction registry and supervisor skeleton

Deliver:
- `predictions.jsonl`
- prediction persistence tools
- evaluation horizon config
- a supervisor loop that scans a watchlist

Exit criteria:
- the service can scan a list of instruments on a schedule
- every prediction is persisted with a unique id

### Phase 2. Telegram publishing

Deliver:
- Telegram publishing adapter
- signal message templates
- delivery log persistence

Exit criteria:
- actionable signals are published automatically
- delivery failures are visible and replayable

### Phase 3. Prediction evaluation

Deliver:
- evaluation engine
- scored prediction outcomes
- rolling KPI summaries

Exit criteria:
- every matured prediction receives a measured outcome
- the service can report short/medium/long window quality

### Phase 4. Controlled adaptation

Deliver:
- strategy supervisor
- candidate/shadow/active version lifecycle
- rollback support

Exit criteria:
- the service can propose, validate, and adopt controlled mutations
- changes are auditable and reversible

### Phase 5. Reliability and production hardening

Deliver:
- durable delivery retries
- richer readiness checks
- service-level alerts
- Kubernetes deployment path for long-running mode

Exit criteria:
- the service can run unattended for extended periods without losing state

---

## Acceptance Criteria

The service can be considered "agentically operational" when all of the following are true:

- it scans a configured instrument universe without manual triggering
- it publishes actionable signals to Telegram automatically
- every published prediction is persisted and later evaluated
- recent prediction quality can be measured from stored records
- adaptation decisions are driven by both backtests and realized outcomes
- parameter changes are versioned, conservative, and reversible
- the system can continue running without manual babysitting

---

## Immediate Next Build Step

The next implementation step should be:

1. add a `SupervisorLoop` that scans `TRADING_SYMBOLS` and `TRADING_TIMEFRAMES`
2. persist every actionable prediction into `predictions.jsonl`
3. add a Telegram publisher with delivery logging
4. add a prediction evaluator for horizon-based scoring

That is the smallest slice that turns the current project from "single-run analyst" into "autonomous signal service."
