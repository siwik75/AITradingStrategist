"""
Knowledge Indexer — embed evaluated predictions into the vector store.

Designed for two trigger points:

1. **Synchronous** — `index_evaluation(prediction, evaluation)` is called right
   after `MemoryStore.save_prediction_evaluation()` in the evaluation pipeline.
2. **Batch backfill** — `backfill_all()` iterates over historical evaluations
   the first time the vector store is enabled.

The indexer is idempotent: re-indexing the same prediction_id upserts.
"""
from __future__ import annotations

import structlog

from config.settings import get_config
from memory.store import MemoryStore
from memory.vector_store import (
    build_outcome_metadata,
    format_setup_document,
    get_vector_store,
)

log = structlog.get_logger()


def index_evaluation(prediction: dict, evaluation: dict) -> str | None:
    """
    Embed and store a single evaluated prediction. Returns the record id, or
    None if the vector store is unavailable.
    """
    store = get_vector_store()
    if not getattr(store, "available", False):
        return None

    try:
        doc = format_setup_document(prediction, evaluation)
        meta = build_outcome_metadata(prediction, evaluation)
        rid = evaluation.get("prediction_id") or prediction.get("prediction_id")
        return store.index_outcome(doc, meta, record_id=rid)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "knowledge_indexer.index_failed",
            error=str(exc),
            prediction_id=prediction.get("prediction_id"),
        )
        return None


async def backfill_all(limit: int = 0) -> dict:
    """
    Backfill the vector store from all historical evaluations.

    :param limit: 0 = unlimited; otherwise only the last N evaluations
    """
    cfg = get_config()
    memory = MemoryStore(agent_id=cfg.agent_id)
    store = get_vector_store()
    if not getattr(store, "available", False):
        return {"indexed": 0, "skipped": 0, "reason": "vector_store_unavailable"}

    evaluations = await memory.get_prediction_evaluations(limit=limit)
    predictions = await memory.get_predictions(limit=0, status="all")
    pred_by_id = {p.get("prediction_id"): p for p in predictions if p.get("prediction_id")}

    indexed = 0
    skipped = 0
    for ev in evaluations:
        pid = ev.get("prediction_id")
        pred = pred_by_id.get(pid)
        if not pred:
            skipped += 1
            continue
        rid = index_evaluation(pred, ev)
        if rid:
            indexed += 1
        else:
            skipped += 1

    log.info("knowledge_indexer.backfill_complete", indexed=indexed, skipped=skipped)
    return {"indexed": indexed, "skipped": skipped, "total_count": store.count()}


def prune_old_outcomes(days: int | None = None) -> int:
    """Delete outcomes older than the configured retention window."""
    cfg = get_config()
    store = get_vector_store()
    if not getattr(store, "available", False):
        return 0
    retention = days if days is not None else cfg.vector_store.retention_days
    return store.prune_older_than(retention)
