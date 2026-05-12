"""
Knowledge Tools — feedback loop into the SignalAgent.

Surfaces three complementary views of past performance:

1. Temporal (raw recent outcomes)         — get_recent_outcomes
2. Aggregate (KPIs over rolling windows)  — get_kpi_summary
3. Semantic (RAG retrieval of similar setups) — query_similar_setups

The SignalAgent receives these as both:
  - A pre-computed `lessons` block injected into the task prompt
  - Optional tools it can call mid-analysis for deeper drill-down
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

import structlog

from config.settings import get_config
from memory.store import MemoryStore
from memory.vector_store import get_vector_store

log = structlog.get_logger()


# =============================================================================
# MEMORY STORE ACCESS
# =============================================================================

def _store() -> MemoryStore:
    cfg = get_config()
    return MemoryStore(agent_id=cfg.agent_id)


# =============================================================================
# TEMPORAL — get_recent_outcomes
# =============================================================================

async def get_recent_outcomes(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 20,
) -> dict:
    """
    Return the most recent evaluated predictions, paired with their original
    prediction record for setup context.

    :param symbol: Filter by symbol (optional)
    :param timeframe: Filter by timeframe (optional)
    :param limit: Max records to return (default 20)
    """
    store = _store()
    evaluations = await store.get_prediction_evaluations(limit=0, timeframe=timeframe)
    if not evaluations:
        return {"count": 0, "outcomes": []}

    predictions = await store.get_predictions(limit=0, status="all")
    pred_by_id = {
        p.get("prediction_id"): p
        for p in predictions
        if p.get("prediction_id")
    }

    outcomes: list[dict] = []
    for ev in reversed(evaluations):  # newest first
        pred = pred_by_id.get(ev.get("prediction_id"), {})
        if symbol and pred.get("symbol") != symbol:
            continue

        sig = pred.get("signal") or {}
        outcomes.append(
            {
                "prediction_id": ev.get("prediction_id"),
                "evaluated_at": ev.get("evaluated_at"),
                "symbol": pred.get("symbol"),
                "timeframe": pred.get("timeframe"),
                "signal": sig.get("signal"),
                "confidence": sig.get("confidence"),
                "regime": sig.get("market_regime"),
                "trend_direction": sig.get("trend_direction"),
                "confluence_indicators": sig.get("confluence_indicators", []),
                "direction_correct": ev.get("direction_correct"),
                "tp1_hit": ev.get("tp1_hit"),
                "tp2_hit": ev.get("tp2_hit"),
                "sl_hit": ev.get("sl_hit"),
                "pnl_pct": ev.get("pnl_pct"),
                "result_label": ev.get("result_label"),
                "outcome_score": ev.get("outcome_score"),
            }
        )
        if len(outcomes) >= limit:
            break

    return {"count": len(outcomes), "outcomes": outcomes}


# =============================================================================
# AGGREGATE — get_kpi_summary
# =============================================================================

async def get_kpi_summary(
    symbol: str | None = None,
    timeframe: str | None = None,
    window: int = 50,
) -> dict:
    """
    Compute aggregate KPIs over the most recent `window` evaluations.

    Returns win-rate, direction-accuracy, TP1/TP2/SL hit rates, average PnL,
    confidence calibration, and a breakdown of best/worst regimes.
    """
    store = _store()
    evaluations = await store.get_prediction_evaluations(limit=0, timeframe=timeframe)
    if not evaluations:
        return {"available": False, "reason": "no_evaluations"}

    predictions = await store.get_predictions(limit=0, status="all")
    pred_by_id = {p.get("prediction_id"): p for p in predictions if p.get("prediction_id")}

    # Filter symbol if needed, then keep last `window`
    filtered: list[tuple[dict, dict]] = []
    for ev in evaluations:
        pred = pred_by_id.get(ev.get("prediction_id"), {})
        if symbol and pred.get("symbol") != symbol:
            continue
        filtered.append((pred, ev))
    filtered = filtered[-window:]
    if not filtered:
        return {"available": False, "reason": "no_evaluations_for_filter"}

    n = len(filtered)
    dir_correct = sum(1 for _, e in filtered if e.get("direction_correct"))
    tp1 = sum(1 for _, e in filtered if e.get("tp1_hit"))
    tp2 = sum(1 for _, e in filtered if e.get("tp2_hit"))
    sl = sum(1 for _, e in filtered if e.get("sl_hit"))
    pnls = [float(e.get("pnl_pct") or 0.0) for _, e in filtered]
    avg_pnl = sum(pnls) / n if n else 0.0
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    # Confidence calibration: bucket by reported confidence vs realized direction
    buckets: dict[str, dict[str, int]] = {
        "low<60": {"n": 0, "correct": 0},
        "mid_60_79": {"n": 0, "correct": 0},
        "high_80_plus": {"n": 0, "correct": 0},
    }
    regime_stats: dict[str, dict[str, int]] = {}
    signal_stats: dict[str, dict[str, int]] = {}

    for pred, ev in filtered:
        sig = pred.get("signal") or {}
        conf = int(sig.get("confidence", 0) or 0)
        if conf < 60:
            b = "low<60"
        elif conf < 80:
            b = "mid_60_79"
        else:
            b = "high_80_plus"
        buckets[b]["n"] += 1
        if ev.get("direction_correct"):
            buckets[b]["correct"] += 1

        regime = sig.get("market_regime") or "?"
        regime_stats.setdefault(regime, {"n": 0, "correct": 0, "pnl": 0.0})
        regime_stats[regime]["n"] += 1
        regime_stats[regime]["pnl"] += float(ev.get("pnl_pct") or 0.0)
        if ev.get("direction_correct"):
            regime_stats[regime]["correct"] += 1

        s = sig.get("signal") or "HOLD"
        signal_stats.setdefault(s, {"n": 0, "correct": 0})
        signal_stats[s]["n"] += 1
        if ev.get("direction_correct"):
            signal_stats[s]["correct"] += 1

    return {
        "available": True,
        "window": n,
        "symbol": symbol,
        "timeframe": timeframe,
        "directional_accuracy": round(dir_correct / n, 3),
        "tp1_hit_rate": round(tp1 / n, 3),
        "tp2_hit_rate": round(tp2 / n, 3),
        "sl_hit_rate": round(sl / n, 3),
        "avg_pnl_pct": round(avg_pnl, 3),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 3),
        "best_trade_pct": round(max(pnls), 3) if pnls else 0.0,
        "worst_trade_pct": round(min(pnls), 3) if pnls else 0.0,
        "confidence_calibration": {
            k: {
                "n": v["n"],
                "accuracy": round(v["correct"] / v["n"], 3) if v["n"] else None,
            }
            for k, v in buckets.items()
        },
        "regime_breakdown": {
            k: {
                "n": v["n"],
                "accuracy": round(v["correct"] / v["n"], 3) if v["n"] else None,
                "avg_pnl_pct": round(v["pnl"] / v["n"], 3) if v["n"] else None,
            }
            for k, v in regime_stats.items()
        },
        "signal_breakdown": {
            k: {
                "n": v["n"],
                "accuracy": round(v["correct"] / v["n"], 3) if v["n"] else None,
            }
            for k, v in signal_stats.items()
        },
        "computed_at": datetime.now(UTC).isoformat(),
    }


# =============================================================================
# AGGREGATE — get_failure_modes
# =============================================================================

async def get_failure_modes(
    symbol: str | None = None,
    timeframe: str | None = None,
    window: int = 100,
    top_k: int = 5,
) -> dict:
    """
    Identify the most common patterns in losing trades:
    - Regime / trend combos that lose most often
    - Indicator combinations present in losses
    - High-confidence-but-wrong setups (over-confidence patterns)
    """
    store = _store()
    evaluations = await store.get_prediction_evaluations(limit=0, timeframe=timeframe)
    if not evaluations:
        return {"available": False, "reason": "no_evaluations"}

    predictions = await store.get_predictions(limit=0, status="all")
    pred_by_id = {p.get("prediction_id"): p for p in predictions if p.get("prediction_id")}

    losing: list[tuple[dict, dict]] = []
    for ev in evaluations:
        pred = pred_by_id.get(ev.get("prediction_id"), {})
        if symbol and pred.get("symbol") != symbol:
            continue
        if ev.get("direction_correct") is False or (ev.get("pnl_pct") or 0.0) < 0:
            losing.append((pred, ev))
    losing = losing[-window:]
    if not losing:
        return {"available": True, "count": 0, "patterns": {}}

    regime_trend_counter: Counter[str] = Counter()
    indicator_counter: Counter[str] = Counter()
    overconfident: list[dict] = []

    for pred, ev in losing:
        sig = pred.get("signal") or {}
        regime = sig.get("market_regime") or "?"
        trend = sig.get("trend_direction") or "?"
        regime_trend_counter[f"{regime}/{trend}"] += 1
        for ind in sig.get("confluence_indicators", []) or []:
            indicator_counter[ind] += 1
        if int(sig.get("confidence", 0) or 0) >= 80:
            overconfident.append(
                {
                    "prediction_id": ev.get("prediction_id"),
                    "symbol": pred.get("symbol"),
                    "timeframe": pred.get("timeframe"),
                    "signal": sig.get("signal"),
                    "confidence": sig.get("confidence"),
                    "regime": regime,
                    "trend": trend,
                    "pnl_pct": ev.get("pnl_pct"),
                    "reasoning": (sig.get("reasoning") or "")[:200],
                }
            )

    return {
        "available": True,
        "count": len(losing),
        "patterns": {
            "frequent_regime_trend_combos": regime_trend_counter.most_common(top_k),
            "frequent_confluence_indicators_in_losses": indicator_counter.most_common(top_k),
            "overconfident_losses": overconfident[:top_k],
        },
        "computed_at": datetime.now(UTC).isoformat(),
    }


# =============================================================================
# SEMANTIC — query_similar_setups (RAG)
# =============================================================================

async def query_similar_setups(
    query_text: str,
    symbol: str | None = None,
    timeframe: str | None = None,
    top_k: int | None = None,
) -> dict:
    """
    Retrieve historically similar setups + their outcomes from the vector store.

    The agent should pass a terse description of the *current* setup (regime,
    trend, key indicator states) — output of `format_setup_document` works well.
    """
    cfg = get_config()
    store = get_vector_store()
    if not getattr(store, "available", False):
        return {"available": False, "reason": "vector_store_unavailable", "hits": []}

    k = top_k or cfg.vector_store.default_top_k
    where: dict[str, Any] = {}
    if symbol:
        where["symbol"] = symbol
    if timeframe:
        where["timeframe"] = timeframe

    try:
        hits = store.query_similar(query_text, top_k=k, where=where or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge.rag_query_failed", error=str(exc))
        return {"available": False, "reason": str(exc), "hits": []}

    # Summary stats over the hits
    n = len(hits)
    if n == 0:
        return {"available": True, "hits": [], "summary": {"count": 0}}

    correct = sum(1 for h in hits if h.get("metadata", {}).get("direction_correct"))
    pnls = [float(h.get("metadata", {}).get("pnl_pct", 0.0) or 0.0) for h in hits]
    avg_pnl = sum(pnls) / n if n else 0.0

    return {
        "available": True,
        "query": query_text,
        "filters": {"symbol": symbol, "timeframe": timeframe},
        "hits": [
            {
                "id": h.get("id"),
                "similarity": h.get("similarity"),
                "document": h.get("document"),
                "metadata": h.get("metadata", {}),
            }
            for h in hits
        ],
        "summary": {
            "count": n,
            "directional_accuracy": round(correct / n, 3),
            "avg_pnl_pct": round(avg_pnl, 3),
        },
    }


# =============================================================================
# LESSON CARD — pre-computed prompt block
# =============================================================================

async def build_lesson_card(
    symbol: str,
    timeframe: str,
    current_setup_text: str | None = None,
    top_k: int = 5,
) -> dict:
    """
    Pre-compute a compact 'lessons' block to inject into the SignalAgent's task prompt.
    Combines KPI summary, top failure modes, and (if available) RAG-retrieved similar setups.
    """
    kpis = await get_kpi_summary(symbol=symbol, timeframe=timeframe, window=50)
    failures = await get_failure_modes(symbol=symbol, timeframe=timeframe, window=100, top_k=3)
    similar = (
        await query_similar_setups(
            current_setup_text, symbol=symbol, timeframe=timeframe, top_k=top_k
        )
        if current_setup_text
        else {"available": False, "hits": []}
    )

    return {
        "kpis": kpis,
        "failure_modes": failures,
        "similar_setups": similar,
        "built_at": datetime.now(UTC).isoformat(),
    }


def format_lesson_card_text(card: dict) -> str:
    """Render the lesson card as a compact human/LLM-readable block."""
    lines: list[str] = ["=== LESSONS FROM PAST SIGNALS ==="]

    kpis = card.get("kpis") or {}
    if kpis.get("available"):
        lines.append(
            f"- KPI window={kpis.get('window')}: "
            f"dir_acc={kpis.get('directional_accuracy')} "
            f"win_rate={kpis.get('win_rate')} "
            f"avg_pnl%={kpis.get('avg_pnl_pct')} "
            f"tp1={kpis.get('tp1_hit_rate')} tp2={kpis.get('tp2_hit_rate')} sl={kpis.get('sl_hit_rate')}"
        )
        regimes = kpis.get("regime_breakdown") or {}
        if regimes:
            best_regime = max(regimes.items(), key=lambda kv: (kv[1].get("accuracy") or 0))[0]
            worst_regime = min(regimes.items(), key=lambda kv: (kv[1].get("accuracy") or 1))[0]
            lines.append(f"  best_regime={best_regime}  worst_regime={worst_regime}")
        calib = kpis.get("confidence_calibration") or {}
        if calib:
            calib_str = ", ".join(
                f"{k}={v.get('accuracy')}({v.get('n')})" for k, v in calib.items() if v.get("n")
            )
            lines.append(f"  confidence_calibration: {calib_str}")
    else:
        lines.append("- KPIs: not enough evaluated history yet")

    failures = (card.get("failure_modes") or {}).get("patterns") or {}
    rt = failures.get("frequent_regime_trend_combos") or []
    inds = failures.get("frequent_confluence_indicators_in_losses") or []
    if rt:
        lines.append("- Top losing regime/trend combos: " + ", ".join(f"{k}({n})" for k, n in rt))
    if inds:
        lines.append("- Indicators most present in losses: " + ", ".join(f"{k}({n})" for k, n in inds))
    overconf = failures.get("overconfident_losses") or []
    if overconf:
        lines.append(f"- Beware: {len(overconf)} high-confidence (>=80) losses recorded recently")

    similar = card.get("similar_setups") or {}
    if similar.get("available") and similar.get("hits"):
        hits = similar["hits"]
        summary = similar.get("summary") or {}
        lines.append(
            f"- Similar past setups (RAG, k={len(hits)}): "
            f"dir_acc={summary.get('directional_accuracy')} avg_pnl%={summary.get('avg_pnl_pct')}"
        )
        for i, h in enumerate(hits[:5], 1):
            md = h.get("metadata") or {}
            lines.append(
                f"   {i}. sim={round(h.get('similarity') or 0, 3)} "
                f"{md.get('signal')} {md.get('symbol')} {md.get('timeframe')} "
                f"correct={md.get('direction_correct')} pnl%={md.get('pnl_pct')} "
                f"label={md.get('result_label')}"
            )
    elif similar.get("available"):
        lines.append("- No similar past setups in the knowledge store yet.")

    lines.append("=== END LESSONS ===")
    return "\n".join(lines)
