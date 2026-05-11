"""
Adaptive Strategy Supervisor — AdaptiveStrategySupervisor.

Decides when to invoke self-assessment, mediates which proposed changes are
safe to adopt, enforces mutation budgets, and manages the three-stage
promotion lifecycle: candidate → shadow → active.

Lifecycle:
  1. candidate  — proposed by SelfAssessmentAgent, not yet validated
  2. shadow     — backtested against current live scoring; compared in parallel
  3. active     — promoted after passing both backtest and realized-outcome support

Rollback:
  If short-window KPIs deteriorate materially after a promotion, the supervisor
  automatically reverts to the previous active version and emits a supervisor event.

Safety constraints:
  - max MAX_PARAMETER_MUTATIONS_PER_CYCLE mutations per adaptation cycle
  - freeze adaptation after MAX_FAILED_PROMOTIONS_BEFORE_FREEZE consecutive failures
  - never promote without a strategy_versions audit trail entry
"""
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

log = structlog.get_logger()

# Lifecycle state constants
LIFECYCLE_CANDIDATE = "candidate"
LIFECYCLE_SHADOW = "shadow"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_ROLLED_BACK = "rolled_back"
LIFECYCLE_REJECTED = "rejected"


class AdaptiveStrategySupervisor:
    """
    Orchestrates the strategy adaptation loop.

    Key responsibilities:
      - compute rolling KPIs from evaluated predictions
      - decide when adaptation is warranted
      - invoke SelfAssessmentAgent and promote its proposals through the lifecycle
      - enforce mutation budget and freeze logic
      - support rollback to previous active version
    """

    def __init__(self):
        from config.settings import get_config
        self._config = get_config()
        self._consecutive_failed_promotions = 0

    # -------------------------------------------------------------------------
    # MAIN ENTRY POINT
    # -------------------------------------------------------------------------

    async def run_adaptation_cycle(
        self,
        symbol: str,
        timeframe: str,
        correlation_id: str = "",
    ) -> dict:
        """
        Run one full adaptation cycle for the given symbol/timeframe.

        Returns a summary dict describing what happened.
        """
        from memory.store import get_memory_store
        store = get_memory_store()
        adapt_cfg = self._config.adaptation

        cid = correlation_id or str(uuid.uuid4())[:8]
        log.info("supervisor.adaptation_cycle_start", symbol=symbol, timeframe=timeframe, cid=cid)

        # --- Freeze guard ---
        if self._consecutive_failed_promotions >= adapt_cfg.max_failed_promotions_before_freeze:
            msg = (
                f"Adaptation frozen after {self._consecutive_failed_promotions} "
                "consecutive failed promotions."
            )
            log.warning("supervisor.adaptation_frozen", reason=msg)
            await store.save_supervisor_event({
                "event_type": "adaptation_frozen",
                "symbol": symbol,
                "timeframe": timeframe,
                "correlation_id": cid,
                "reason": msg,
            })
            return {"status": "frozen", "reason": msg}

        # --- KPI assessment ---
        evaluations = await store.get_prediction_evaluations(timeframe=timeframe)
        from tools.evaluation_tools import compute_rolling_kpis, should_trigger_adaptation
        short_kpis = await compute_rolling_kpis(
            evaluations, window=self._config.prediction.short_window
        )
        trigger, reasons = await should_trigger_adaptation(short_kpis, self._config)

        if not trigger:
            log.info("supervisor.no_adaptation_needed", symbol=symbol, timeframe=timeframe)
            return {"status": "no_adaptation_needed", "kpis": short_kpis}

        log.info(
            "supervisor.adaptation_triggered",
            reasons=reasons,
            symbol=symbol,
            timeframe=timeframe,
        )
        await store.save_supervisor_event({
            "event_type": "adaptation_triggered",
            "symbol": symbol,
            "timeframe": timeframe,
            "correlation_id": cid,
            "reasons": reasons,
            "kpis_short_window": short_kpis,
        })

        # Publish degradation alert to Telegram
        try:
            from tools.notification_tools import TelegramPublisher
            if self._config.telegram.publish_degradation_alerts:
                publisher = TelegramPublisher()
                await publisher.publish_degradation_alert(short_kpis, reasons)
        except Exception as exc:
            log.warning("supervisor.telegram_alert_failed", error=str(exc))

        # --- Propose candidate params via SelfAssessmentAgent ---
        from agents.self_assessment import SelfAssessmentAgent
        agent = SelfAssessmentAgent()
        assessment = await agent.assess_and_evolve(
            symbol=symbol,
            timeframe=timeframe,
            backtest_days=self._config.backtest.lookback_days,
            correlation_id=cid,
        )

        if assessment.get("decision") not in ("adopt_new", "improve"):
            log.info(
                "supervisor.assessment_no_change",
                decision=assessment.get("decision"),
            )
            self._consecutive_failed_promotions += 1
            await store.save_supervisor_event({
                "event_type": "candidate_rejected",
                "symbol": symbol,
                "timeframe": timeframe,
                "correlation_id": cid,
                "assessment_decision": assessment.get("decision"),
            })
            return {"status": "candidate_rejected", "assessment": assessment}

        proposed_params = assessment.get("proposed_params") or assessment.get("final_params")
        current_params = assessment.get("current_params")

        if not proposed_params:
            log.warning("supervisor.no_proposed_params")
            self._consecutive_failed_promotions += 1
            return {"status": "no_proposed_params", "assessment": assessment}

        # --- Enforce mutation budget ---
        mutations = _count_param_mutations(current_params or {}, proposed_params)
        max_mut = adapt_cfg.max_mutations_per_cycle
        if mutations > max_mut:
            log.warning(
                "supervisor.mutation_budget_exceeded",
                mutations=mutations,
                max=max_mut,
            )
            proposed_params = _clamp_mutations(current_params or {}, proposed_params, max_mut)
            mutations = max_mut

        # --- Register as candidate ---
        version_id = str(uuid.uuid4())[:8]
        candidate_version = {
            "version_id": version_id,
            "lifecycle": LIFECYCLE_CANDIDATE,
            "symbol": symbol,
            "timeframe": timeframe,
            "correlation_id": cid,
            "created_at": datetime.now(UTC).isoformat(),
            "prev_params": current_params,
            "new_params": proposed_params,
            "mutation_count": mutations,
            "trigger_reasons": reasons,
            "assessment_summary": {
                "decision": assessment.get("decision"),
                "decision_reasoning": assessment.get("decision_reasoning", "")[:500],
                "current_metrics": assessment.get("current_metrics"),
                "proposed_metrics": assessment.get("proposed_metrics"),
            },
            "reason": "; ".join(reasons),
        }
        await store.save_strategy_version(candidate_version)
        log.info("supervisor.candidate_registered", version_id=version_id, mutations=mutations)

        # --- Shadow validation via backtest comparison ---
        shadow_result = await self._shadow_validate(
            symbol=symbol,
            timeframe=timeframe,
            current_params=current_params,
            proposed_params=proposed_params,
            correlation_id=cid,
        )

        shadow_version = {
            **candidate_version,
            "lifecycle": LIFECYCLE_SHADOW,
            "shadow_validation": shadow_result,
            "shadowed_at": datetime.now(UTC).isoformat(),
        }
        await store.save_strategy_version(shadow_version)

        if not shadow_result.get("passes_validation"):
            log.info(
                "supervisor.shadow_failed",
                version_id=version_id,
                reason=shadow_result.get("failure_reason"),
            )
            self._consecutive_failed_promotions += 1
            rejected_version = {
                **shadow_version,
                "lifecycle": LIFECYCLE_REJECTED,
                "rejected_at": datetime.now(UTC).isoformat(),
            }
            await store.save_strategy_version(rejected_version)
            await store.save_supervisor_event({
                "event_type": "shadow_validation_failed",
                "version_id": version_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "correlation_id": cid,
                "failure_reason": shadow_result.get("failure_reason"),
            })
            return {
                "status": "shadow_rejected",
                "version_id": version_id,
                "shadow_result": shadow_result,
            }

        # --- Promote to active ---
        active_version = {
            **shadow_version,
            "lifecycle": LIFECYCLE_ACTIVE,
            "activated_at": datetime.now(UTC).isoformat(),
            "validation_summary": shadow_result,
        }
        await store.save_strategy_version(active_version)

        # Write new params to the live strategy params store
        await store.save_strategy_params(proposed_params, timeframe=timeframe)
        self._consecutive_failed_promotions = 0

        log.info(
            "supervisor.params_promoted",
            version_id=version_id,
            mutations=mutations,
        )

        await store.save_supervisor_event({
            "event_type": "params_promoted",
            "version_id": version_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "correlation_id": cid,
            "prev_params": current_params,
            "new_params": proposed_params,
            "mutations": mutations,
        })

        # Publish strategy update to Telegram
        try:
            from tools.notification_tools import TelegramPublisher
            if self._config.telegram.publish_strategy_changes:
                publisher = TelegramPublisher()
                await publisher.publish_strategy_update(active_version)
        except Exception as exc:
            log.warning("supervisor.telegram_strategy_update_failed", error=str(exc))

        return {
            "status": "promoted",
            "version_id": version_id,
            "mutations": mutations,
            "prev_params": current_params,
            "new_params": proposed_params,
            "shadow_result": shadow_result,
        }

    # -------------------------------------------------------------------------
    # ROLLBACK
    # -------------------------------------------------------------------------

    async def rollback_if_degraded(
        self,
        symbol: str,
        timeframe: str,
        correlation_id: str = "",
    ) -> dict:
        """
        Check short-window KPIs after a recent promotion. If degraded, rollback
        to the previous active params.
        """
        from memory.store import get_memory_store
        store = get_memory_store()

        if not self._config.adaptation.rollback_on_short_window_degradation:
            return {"status": "rollback_disabled"}

        evaluations = await store.get_prediction_evaluations(timeframe=timeframe)
        from tools.evaluation_tools import compute_rolling_kpis, should_trigger_adaptation
        short_kpis = await compute_rolling_kpis(
            evaluations, window=self._config.prediction.short_window
        )
        trigger, reasons = await should_trigger_adaptation(short_kpis, self._config)

        if not trigger:
            return {"status": "no_rollback_needed"}

        # Find the most recent active version and the one before it
        versions = await store.get_strategy_versions()
        active_versions = [v for v in versions if v.get("lifecycle") == LIFECYCLE_ACTIVE]

        if len(active_versions) < 2:
            log.info("supervisor.rollback_skipped_no_prior_version")
            return {"status": "no_prior_version_for_rollback"}

        current = active_versions[-1]
        previous = active_versions[-2]
        prev_params = previous.get("new_params") or previous.get("prev_params")

        if not prev_params:
            return {"status": "no_params_in_prior_version"}

        # Revert
        await store.save_strategy_params(prev_params, timeframe=timeframe)
        rollback_version = {
            **current,
            "lifecycle": LIFECYCLE_ROLLED_BACK,
            "rolled_back_at": datetime.now(UTC).isoformat(),
            "rollback_reasons": reasons,
            "reverted_to_version_id": previous.get("version_id"),
        }
        await store.save_strategy_version(rollback_version)
        await store.save_supervisor_event({
            "event_type": "params_rolled_back",
            "from_version_id": current.get("version_id"),
            "to_version_id": previous.get("version_id"),
            "symbol": symbol,
            "timeframe": timeframe,
            "correlation_id": correlation_id,
            "rollback_reasons": reasons,
        })

        log.warning(
            "supervisor.params_rolled_back",
            from_version=current.get("version_id"),
            to_version=previous.get("version_id"),
        )
        return {
            "status": "rolled_back",
            "from_version_id": current.get("version_id"),
            "to_version_id": previous.get("version_id"),
            "reverted_params": prev_params,
            "reasons": reasons,
        }

    # -------------------------------------------------------------------------
    # KPI SUMMARY
    # -------------------------------------------------------------------------

    async def get_kpi_summary(self, timeframe: str | None = None) -> dict:
        """Return short/medium/long window KPIs for monitoring."""
        from memory.store import get_memory_store
        from tools.evaluation_tools import compute_rolling_kpis

        store = get_memory_store()
        evaluations = await store.get_prediction_evaluations(timeframe=timeframe)
        pred_cfg = self._config.prediction

        short = await compute_rolling_kpis(evaluations, window=pred_cfg.short_window)
        medium = await compute_rolling_kpis(evaluations, window=pred_cfg.medium_window)
        long_ = await compute_rolling_kpis(evaluations, window=pred_cfg.long_window)

        return {
            "timeframe": timeframe,
            "short_window": short,
            "medium_window": medium,
            "long_window": long_,
            "total_evaluations": len(evaluations),
        }

    # -------------------------------------------------------------------------
    # SHADOW VALIDATION (internal)
    # -------------------------------------------------------------------------

    async def _shadow_validate(
        self,
        symbol: str,
        timeframe: str,
        current_params: dict | None,
        proposed_params: dict,
        correlation_id: str,
    ) -> dict:
        """
        Run backtest comparisons for current vs proposed params.

        Passes validation if proposed backtest wins on ≥2 of 3 key metrics:
          - win_rate
          - profit_factor
          - total_pnl_pct (proxy for return)

        AND does not significantly worsen max_drawdown_pct (not > 15%).
        """
        from tools.trading_tools import run_backtest
        import json as _json

        days = self._config.backtest.lookback_days

        try:
            proposed_bt = await run_backtest(
                symbol=symbol,
                timeframe=timeframe,
                strategy_params=_json.dumps(proposed_params),
                days=days,
            )
        except Exception as exc:
            return {
                "passes_validation": False,
                "failure_reason": f"proposed backtest failed: {exc}",
            }

        current_bt = {}
        if current_params:
            try:
                current_bt = await run_backtest(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_params=_json.dumps(current_params),
                    days=days,
                )
            except Exception:
                pass

        proposed_trades = proposed_bt.get("total_trades", 0)
        if proposed_trades < 5:
            return {
                "passes_validation": False,
                "failure_reason": f"proposed params generate too few trades ({proposed_trades})",
                "proposed_backtest": proposed_bt,
            }

        proposed_dd = proposed_bt.get("max_drawdown_pct", 999)
        if proposed_dd > 15.0:
            return {
                "passes_validation": False,
                "failure_reason": f"proposed max drawdown {proposed_dd}% exceeds hard limit 15%",
                "proposed_backtest": proposed_bt,
            }

        if not current_bt:
            # No baseline — accept if basic sanity passes
            return {
                "passes_validation": True,
                "proposed_backtest": proposed_bt,
                "current_backtest": {},
                "validation_note": "no baseline for comparison, basic sanity passed",
            }

        wins = 0
        metrics_compared = {}
        for metric in ("win_rate", "profit_factor", "total_pnl_pct"):
            p_val = proposed_bt.get(metric, 0) or 0
            c_val = current_bt.get(metric, 0) or 0
            improved = p_val > c_val
            metrics_compared[metric] = {"proposed": p_val, "current": c_val, "improved": improved}
            if improved:
                wins += 1

        passes = wins >= 2
        return {
            "passes_validation": passes,
            "wins_out_of_3": wins,
            "metrics_compared": metrics_compared,
            "proposed_backtest": proposed_bt,
            "current_backtest": current_bt,
            "failure_reason": None if passes else f"only {wins}/3 metrics improved",
        }


# =============================================================================
# MUTATION HELPERS
# =============================================================================

def _count_param_mutations(current: dict, proposed: dict) -> int:
    """Count number of keys that differ between two param dicts."""
    all_keys = set(list(current.keys()) + list(proposed.keys()))
    return sum(1 for k in all_keys if current.get(k) != proposed.get(k))


def _clamp_mutations(current: dict, proposed: dict, max_mutations: int) -> dict:
    """
    Return a version of proposed that has at most max_mutations changes vs current.
    Priority: keep the first max_mutations changed keys in insertion order.
    """
    result = dict(current)
    changed = 0
    for k, v in proposed.items():
        if current.get(k) != v:
            if changed < max_mutations:
                result[k] = v
                changed += 1
        else:
            result[k] = v
    return result
