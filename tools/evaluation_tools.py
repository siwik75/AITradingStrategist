"""
Prediction Evaluation Tools — PredictionEvaluator and rolling KPI engine.

Turns historical predictions into scored outcomes and aggregates rolling
quality metrics used by the AdaptiveStrategySupervisor to decide whether
strategy adaptation is warranted.

Evaluation model:
  - direction_correct    — was the signal direction correct at horizon close?
  - tp1_reached          — did price reach TP1 before SL?
  - tp2_reached          — did price reach TP2 before SL?
  - sl_reached_first     — did price hit SL before TP1?
  - mfe_pct              — max favorable excursion as % of entry
  - mae_pct              — max adverse excursion as % of entry
  - outcome_score        — composite 0-1 score
  - confidence_calibration_bucket — high / medium / low
"""
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

log = structlog.get_logger()


# =============================================================================
# SINGLE PREDICTION EVALUATOR
# =============================================================================

async def evaluate_prediction(
    prediction: dict,
    future_candles: list[dict],
) -> dict:
    """
    Score a single prediction against realized future OHLCV candles.

    Args:
        prediction: Full prediction record from predictions.jsonl.
        future_candles: List of OHLCV dicts (open, high, low, close, volume)
                        starting immediately after the prediction timestamp.
                        Each dict must have keys: open, high, low, close.

    Returns:
        Evaluation dict with all scored fields.
    """
    if not future_candles:
        return {
            "evaluation_id": str(uuid.uuid4())[:8],
            "prediction_id": prediction.get("prediction_id"),
            "error": "no_future_candles",
            "outcome_score": None,
        }

    signal = prediction.get("signal", "HOLD")
    entry = _safe_float(prediction.get("entry_price"))
    sl = _safe_float(prediction.get("stop_loss"))
    tp1 = _safe_float(prediction.get("take_profit_1"))
    tp2 = _safe_float(prediction.get("take_profit_2"))
    confidence = _safe_float(prediction.get("confidence"), default=0.0)

    if entry is None or entry == 0:
        return {
            "evaluation_id": str(uuid.uuid4())[:8],
            "prediction_id": prediction.get("prediction_id"),
            "error": "no_entry_price",
            "outcome_score": None,
        }

    is_long = signal == "BUY"
    is_short = signal == "SELL"

    # --- Candle walk ---
    horizon_close = future_candles[-1]["close"] if future_candles else entry
    max_high = max(c["high"] for c in future_candles)
    min_low = min(c["low"] for c in future_candles)

    # MFE / MAE
    if is_long:
        mfe_pct = ((max_high - entry) / entry) * 100 if entry else 0.0
        mae_pct = ((entry - min_low) / entry) * 100 if entry else 0.0
    elif is_short:
        mfe_pct = ((entry - min_low) / entry) * 100 if entry else 0.0
        mae_pct = ((max_high - entry) / entry) * 100 if entry else 0.0
    else:
        mfe_pct = 0.0
        mae_pct = 0.0

    # Direction
    if is_long:
        direction_correct = horizon_close > entry
    elif is_short:
        direction_correct = horizon_close < entry
    else:
        direction_correct = None  # HOLD — evaluated separately

    # TP1 / TP2 / SL sequential walk
    tp1_reached = False
    tp2_reached = False
    sl_reached_first = False

    if signal in ("BUY", "SELL") and tp1 is not None and sl is not None:
        for candle in future_candles:
            h, l = candle["high"], candle["low"]
            if is_long:
                if not sl_reached_first and l <= sl:
                    sl_reached_first = True
                    break
                if tp1 and h >= tp1:
                    tp1_reached = True
                if tp1_reached and tp2 and h >= tp2:
                    tp2_reached = True
                    break
            elif is_short:
                if not sl_reached_first and h >= sl:
                    sl_reached_first = True
                    break
                if tp1 and l <= tp1:
                    tp1_reached = True
                if tp1_reached and tp2 and l <= tp2:
                    tp2_reached = True
                    break

    # Composite outcome score (0–1)
    outcome_score = _compute_outcome_score(
        direction_correct=direction_correct,
        tp1_reached=tp1_reached,
        tp2_reached=tp2_reached,
        sl_reached_first=sl_reached_first,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        signal=signal,
    )

    # Confidence calibration bucket
    if confidence >= 80:
        cal_bucket = "high"
    elif confidence >= 60:
        cal_bucket = "medium"
    else:
        cal_bucket = "low"

    evaluation = {
        "evaluation_id": str(uuid.uuid4())[:8],
        "prediction_id": prediction.get("prediction_id"),
        "evaluated_at": datetime.now(UTC).isoformat(),
        "symbol": prediction.get("symbol"),
        "timeframe": prediction.get("timeframe"),
        "signal": signal,
        "confidence": confidence,
        "entry_price": entry,
        "horizon_close": horizon_close,
        "candles_evaluated": len(future_candles),
        "direction_correct": direction_correct,
        "tp1_reached": tp1_reached,
        "tp2_reached": tp2_reached,
        "sl_reached_first": sl_reached_first,
        "mfe_pct": round(mfe_pct, 4),
        "mae_pct": round(mae_pct, 4),
        "outcome_score": round(outcome_score, 4),
        "confidence_calibration_bucket": cal_bucket,
        "evaluation_notes": _build_notes(
            direction_correct, tp1_reached, tp2_reached, sl_reached_first, signal
        ),
    }

    log.info(
        "evaluation.prediction_scored",
        prediction_id=prediction.get("prediction_id"),
        signal=signal,
        direction_correct=direction_correct,
        outcome_score=round(outcome_score, 3),
    )
    return evaluation


def _safe_float(val: Any, default: float | None = None) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _compute_outcome_score(
    direction_correct: bool | None,
    tp1_reached: bool,
    tp2_reached: bool,
    sl_reached_first: bool,
    mfe_pct: float,
    mae_pct: float,
    signal: str,
) -> float:
    if signal == "HOLD":
        # HOLD quality: penalize if the market moved strongly (missed opportunity)
        if mfe_pct > 3.0:
            return 0.3  # strong move was missed
        return 0.7  # held correctly

    score = 0.0

    # Direction (40% weight)
    if direction_correct is True:
        score += 0.40
    elif direction_correct is False:
        score += 0.0

    # SL avoidance (20% weight)
    if not sl_reached_first:
        score += 0.20

    # TP1 reach (25% weight)
    if tp1_reached:
        score += 0.25

    # TP2 reach (15% weight)
    if tp2_reached:
        score += 0.15

    # MFE bonus: reward clean trades (capped at 0.10 bonus)
    if mfe_pct > 0:
        score = min(1.0, score + min(mfe_pct / 100, 0.10))

    # MAE penalty: penalize deep drawdown against position
    if mae_pct > 2.0:
        score = max(0.0, score - min(mae_pct / 100, 0.10))

    return score


def _build_notes(
    direction_correct: bool | None,
    tp1_reached: bool,
    tp2_reached: bool,
    sl_reached_first: bool,
    signal: str,
) -> str:
    if signal == "HOLD":
        return "HOLD signal — no directional trade evaluated."
    parts = []
    if sl_reached_first:
        parts.append("SL hit before TP1.")
    elif tp2_reached:
        parts.append("Full run to TP2.")
    elif tp1_reached:
        parts.append("TP1 reached; TP2 not reached.")
    else:
        parts.append("No TP or SL hit within horizon.")
    if direction_correct is True:
        parts.append("Direction correct.")
    elif direction_correct is False:
        parts.append("Direction incorrect.")
    return " ".join(parts)


# =============================================================================
# ROLLING KPI ENGINE
# =============================================================================

async def compute_rolling_kpis(
    evaluations: list[dict],
    window: int = 25,
) -> dict:
    """
    Compute rolling quality KPIs from the most recent N evaluated predictions.

    Args:
        evaluations: List of evaluation records from get_prediction_evaluations().
        window: How many recent records to consider (default 25 = short window).

    Returns:
        Dict of KPI metrics used by the supervisor to decide on adaptation.
    """
    # Use only the most recent `window` records
    recent = evaluations[-window:] if len(evaluations) > window else evaluations
    actionable = [e for e in recent if e.get("signal") in ("BUY", "SELL")]
    hold_evals = [e for e in recent if e.get("signal") == "HOLD"]

    total = len(recent)
    actionable_count = len(actionable)

    if actionable_count == 0:
        return {
            "window": window,
            "total_evaluated": total,
            "actionable_count": 0,
            "directional_accuracy": None,
            "tp1_reach_rate": None,
            "tp2_reach_rate": None,
            "sl_first_hit_rate": None,
            "false_positive_rate": None,
            "avg_mfe_pct": None,
            "avg_mae_pct": None,
            "avg_outcome_score": None,
            "hold_quality": None,
            "confidence_calibration": {},
            "insufficient_data": True,
        }

    directional_correct = [
        e for e in actionable if e.get("direction_correct") is True
    ]
    tp1_hit = [e for e in actionable if e.get("tp1_reached")]
    tp2_hit = [e for e in actionable if e.get("tp2_reached")]
    sl_first = [e for e in actionable if e.get("sl_reached_first")]

    directional_accuracy = len(directional_correct) / actionable_count
    tp1_reach_rate = len(tp1_hit) / actionable_count
    tp2_reach_rate = len(tp2_hit) / actionable_count
    sl_first_hit_rate = len(sl_first) / actionable_count
    # false positive = no direction correct AND no TP1 reached AND SL hit
    false_positives = [
        e for e in actionable
        if not e.get("direction_correct") and not e.get("tp1_reached")
    ]
    false_positive_rate = len(false_positives) / actionable_count

    mfe_values = [e["mfe_pct"] for e in actionable if e.get("mfe_pct") is not None]
    mae_values = [e["mae_pct"] for e in actionable if e.get("mae_pct") is not None]
    score_values = [
        e["outcome_score"] for e in actionable if e.get("outcome_score") is not None
    ]

    avg_mfe_pct = sum(mfe_values) / len(mfe_values) if mfe_values else None
    avg_mae_pct = sum(mae_values) / len(mae_values) if mae_values else None
    avg_outcome_score = sum(score_values) / len(score_values) if score_values else None

    # HOLD quality
    hold_quality = None
    if hold_evals:
        hold_scores = [e.get("outcome_score", 0) for e in hold_evals]
        hold_quality = sum(hold_scores) / len(hold_scores)

    # Confidence calibration: compare outcome_score by bucket
    cal: dict[str, list] = {"high": [], "medium": [], "low": []}
    for e in actionable:
        bucket = e.get("confidence_calibration_bucket", "low")
        score = e.get("outcome_score")
        if score is not None and bucket in cal:
            cal[bucket].append(score)
    confidence_calibration = {
        bucket: round(sum(scores) / len(scores), 4) if scores else None
        for bucket, scores in cal.items()
    }

    return {
        "window": window,
        "computed_at": datetime.now(UTC).isoformat(),
        "total_evaluated": total,
        "actionable_count": actionable_count,
        "directional_accuracy": round(directional_accuracy, 4),
        "tp1_reach_rate": round(tp1_reach_rate, 4),
        "tp2_reach_rate": round(tp2_reach_rate, 4),
        "sl_first_hit_rate": round(sl_first_hit_rate, 4),
        "false_positive_rate": round(false_positive_rate, 4),
        "avg_mfe_pct": round(avg_mfe_pct, 4) if avg_mfe_pct is not None else None,
        "avg_mae_pct": round(avg_mae_pct, 4) if avg_mae_pct is not None else None,
        "avg_outcome_score": round(avg_outcome_score, 4) if avg_outcome_score is not None else None,
        "hold_quality": round(hold_quality, 4) if hold_quality is not None else None,
        "confidence_calibration": confidence_calibration,
        "insufficient_data": False,
    }


async def should_trigger_adaptation(kpis: dict, config=None) -> tuple[bool, list[str]]:
    """
    Compare KPIs against adaptation thresholds.

    Returns:
        (trigger, reasons) — True if adaptation should run, plus a list of
        human-readable reasons.
    """
    if kpis.get("insufficient_data"):
        return False, ["insufficient evaluated predictions"]

    reasons = []

    if config is None:
        from config.settings import get_config
        config = get_config()

    adapt_cfg = config.adaptation
    da = kpis.get("directional_accuracy")
    tp1 = kpis.get("tp1_reach_rate")
    fp = kpis.get("false_positive_rate")
    cal = kpis.get("confidence_calibration", {})

    if da is not None and da < adapt_cfg.min_directional_accuracy:
        reasons.append(
            f"directional_accuracy={da:.1%} < threshold {adapt_cfg.min_directional_accuracy:.1%}"
        )

    if tp1 is not None and tp1 < adapt_cfg.min_tp1_reach_rate:
        reasons.append(
            f"tp1_reach_rate={tp1:.1%} < threshold {adapt_cfg.min_tp1_reach_rate:.1%}"
        )

    if fp is not None and fp > adapt_cfg.max_false_positive_rate:
        reasons.append(
            f"false_positive_rate={fp:.1%} > threshold {adapt_cfg.max_false_positive_rate:.1%}"
        )

    # High-confidence underperforms medium
    high_score = cal.get("high")
    medium_score = cal.get("medium")
    if high_score is not None and medium_score is not None and high_score < medium_score:
        reasons.append(
            f"high-confidence signals (score={high_score:.3f}) underperform "
            f"medium-confidence (score={medium_score:.3f})"
        )

    return len(reasons) > 0, reasons
