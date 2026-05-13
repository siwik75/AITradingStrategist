"""
Vector Store — Chroma-backed RAG layer for signal knowledge.

Stores embeddings of past signal setups + outcomes so that the SignalAgent can
retrieve semantically similar prior signals during analysis.

Embedder backends:
  - sentence_transformers (local, free)  — default
  - openai (uses gateway/OpenAI client)  — alternative

If chromadb is not installed, the store degrades to a no-op stub so the rest
of the system continues to function.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import structlog

from config.settings import get_config

log = structlog.get_logger()


# =============================================================================
# EMBEDDER PROTOCOL + IMPLEMENTATIONS
# =============================================================================


class Embedder(Protocol):
    name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformersEmbedder:
    """Local, free embedder via sentence-transformers."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._model = SentenceTransformer(model_name)
        self.name = model_name
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]


class OpenAIEmbedder:
    """Embed via OpenAI-compatible /v1/embeddings (works with Anthropic-style gateways too if supported)."""

    def __init__(self, model: str, api_key: str, base_url: str | None = None):
        from openai import OpenAI

        self._client = (
            OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        )
        self._model = model
        self.name = model
        self.dimension = 1536  # text-embedding-3-small default; updated on first call

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._model, input=texts)
        out = [d.embedding for d in resp.data]
        if out:
            self.dimension = len(out[0])
        return out


def build_embedder() -> Embedder | None:
    """Construct the configured embedder. Returns None if unavailable."""
    cfg = get_config()
    provider = cfg.embedding.provider

    if provider == "sentence_transformers":
        try:
            return SentenceTransformersEmbedder(cfg.embedding.local_model)
        except ImportError:
            log.warning("vector_store.sentence_transformers_missing")
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("vector_store.embedder_init_failed", error=str(exc))
            return None

    if provider == "openai":
        api_key = cfg.llm.gateway_api_key or os.getenv("OPENAI_API_KEY", "")
        base_url = cfg.llm.gateway_url if cfg.llm.gateway_url else None
        if not api_key:
            log.warning("vector_store.openai_no_key")
            return None
        try:
            return OpenAIEmbedder(cfg.embedding.openai_model, api_key=api_key, base_url=base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("vector_store.openai_embedder_failed", error=str(exc))
            return None

    log.warning("vector_store.unknown_embed_provider", provider=provider)
    return None


# =============================================================================
# CHROMA WRAPPER
# =============================================================================


class _NoOpVectorStore:
    """Fallback when chromadb is not installed — keeps the rest of the system running."""

    available = False

    def index_outcome(self, *args, **kwargs) -> str | None:  # noqa: D401
        return None

    def query_similar(self, *args, **kwargs) -> list[dict]:
        return []

    def count(self) -> int:
        return 0

    def prune_older_than(self, *args, **kwargs) -> int:
        return 0


class ChromaVectorStore:
    """Persistent Chroma store with pluggable embedder."""

    available = True

    def __init__(
        self,
        persist_dir: str,
        collection: str,
        embedder: Embedder,
    ):
        import chromadb  # type: ignore
        from chromadb.config import Settings  # type: ignore

        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self._embedder = embedder
        self._collection_name = collection
        self._collection = self._client.get_or_create_collection(
            name=collection,
            metadata={"embedder": embedder.name, "dimension": str(embedder.dimension)},
        )

    # ---------- write ----------

    def index_outcome(
        self,
        document: str,
        metadata: dict[str, Any],
        record_id: str | None = None,
    ) -> str:
        """Embed `document` and upsert with metadata. Returns the record id."""
        rid = record_id or str(uuid.uuid4())
        flat_meta = _flatten_metadata(metadata)
        vec = self._embedder.embed([document])
        if not vec:
            raise RuntimeError("embedder returned empty vector")
        self._collection.upsert(
            ids=[rid],
            embeddings=vec,
            documents=[document],
            metadatas=[flat_meta],
        )
        return rid

    # ---------- read ----------

    def query_similar(
        self,
        query_text: str,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict]:
        if not query_text:
            return []
        vec = self._embedder.embed([query_text])
        if not vec:
            return []
        result = self._collection.query(
            query_embeddings=vec,
            n_results=top_k,
            where=_chroma_where(where),
        )
        return _unpack_query_result(result)

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception:  # noqa: BLE001
            return 0

    # ---------- maintenance ----------

    def prune_older_than(self, days: int) -> int:
        """Delete records older than `days`. Returns count deleted."""
        if days <= 0:
            return 0
        cutoff_ts = (datetime.now(UTC) - timedelta(days=days)).timestamp()
        # Pull all metadata (cheap for small collections) and filter ids
        try:
            data = self._collection.get(include=["metadatas"])
            ids = data.get("ids", []) or []
            metas = data.get("metadatas", []) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("vector_store.prune_get_failed", error=str(exc))
            return 0

        stale = [
            rid
            for rid, meta in zip(ids, metas, strict=False)
            if isinstance(meta, dict) and float(meta.get("indexed_at_ts", 0.0) or 0.0) < cutoff_ts
        ]
        if stale:
            try:
                self._collection.delete(ids=stale)
            except Exception as exc:  # noqa: BLE001
                log.warning("vector_store.prune_delete_failed", error=str(exc))
                return 0
        return len(stale)

    def reset(self) -> None:
        """Drop and recreate the collection. Test helper."""
        try:
            self._client.delete_collection(self._collection_name)
        except Exception:  # noqa: BLE001
            pass
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"embedder": self._embedder.name, "dimension": str(self._embedder.dimension)},
        )


# =============================================================================
# FACTORY
# =============================================================================

_singleton: ChromaVectorStore | _NoOpVectorStore | None = None


def get_vector_store() -> ChromaVectorStore | _NoOpVectorStore:
    """Return the configured singleton vector store (or a no-op stub)."""
    global _singleton
    if _singleton is not None:
        return _singleton

    cfg = get_config()
    if not cfg.vector_store.enabled:
        _singleton = _NoOpVectorStore()
        return _singleton

    try:
        import chromadb  # type: ignore  # noqa: F401
    except ImportError:
        log.warning("vector_store.chromadb_missing", hint="pip install '.[rag]'")
        _singleton = _NoOpVectorStore()
        return _singleton

    embedder = build_embedder()
    if embedder is None:
        log.warning("vector_store.no_embedder_falling_back_to_noop")
        _singleton = _NoOpVectorStore()
        return _singleton

    try:
        _singleton = ChromaVectorStore(
            persist_dir=cfg.vector_store.resolved_persist_dir(),
            collection=cfg.vector_store.outcomes_collection,
            embedder=embedder,
        )
        log.info(
            "vector_store.ready",
            persist_dir=cfg.vector_store.resolved_persist_dir(),
            collection=cfg.vector_store.outcomes_collection,
            embedder=embedder.name,
            dimension=embedder.dimension,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("vector_store.init_failed", error=str(exc))
        _singleton = _NoOpVectorStore()
    return _singleton


def reset_vector_store() -> None:
    """Test helper — drop the singleton."""
    global _singleton
    _singleton = None


# =============================================================================
# SETUP / OUTCOME → DOCUMENT FORMATTER
# =============================================================================


def format_setup_document(prediction: dict, evaluation: dict | None = None) -> str:
    """
    Render a stable, embedding-friendly description of a signal setup + outcome.

    The text encodes the salient features of the setup (regime, trend, key indicator
    states) plus the realized outcome. Stability matters more than verbosity —
    similar setups must produce similar text so embeddings cluster well.
    """
    sig = prediction.get("signal", {}) if isinstance(prediction.get("signal"), dict) else prediction
    indicators = prediction.get("indicators") or sig.get("indicators") or {}
    market = prediction.get("market_context") or {}

    parts: list[str] = []
    parts.append(f"symbol={prediction.get('symbol', sig.get('symbol', '?'))}")
    parts.append(f"timeframe={prediction.get('timeframe', sig.get('timeframe', '?'))}")
    parts.append(f"signal={sig.get('signal', 'HOLD')}")
    parts.append(f"confidence={sig.get('confidence', 0)}")
    parts.append(f"regime={sig.get('market_regime', '?')}")
    parts.append(f"trend={sig.get('trend_direction', '?')}")
    if sig.get("confluence_indicators"):
        parts.append("confluence=" + ",".join(sig["confluence_indicators"]))
    if sig.get("divergent_indicators"):
        parts.append("divergent=" + ",".join(sig["divergent_indicators"]))

    # Selected indicator snapshots (keep terse so similar setups cluster)
    trend = indicators.get("trend") or {}
    momentum = indicators.get("momentum") or {}
    volatility = indicators.get("volatility") or {}
    volume = indicators.get("volume") or {}

    def _add(prefix: str, d: dict, keys: list[str]) -> None:
        for k in keys:
            if k in d and d[k] is not None:
                parts.append(f"{prefix}.{k}={d[k]}")

    _add("trend", trend, ["ema_alignment", "adx_strength", "macd_signal"])
    _add("mom", momentum, ["rsi_state", "stoch_state", "mfi_state"])
    _add("vol", volatility, ["regime", "squeeze"])
    _add("volume", volume, ["price_vs_vwap", "obv_trend"])

    # Market context (news/F&G/liquidity)
    if market:
        if fg := market.get("fear_greed"):
            parts.append(f"fg={fg.get('classification', '?')}({fg.get('value', '?')})")
        if ns := market.get("news_sentiment"):
            parts.append(f"news={ns.get('overall_sentiment', '?')}")
        if liq := market.get("liquidity", {}).get("zones", {}):
            parts.append(f"micro={liq.get('imbalance_label', '?')}")

    # Evaluation block (the outcome)
    if evaluation:
        parts.append(f"outcome.dir_correct={evaluation.get('direction_correct', '?')}")
        parts.append(f"outcome.tp1_hit={evaluation.get('tp1_hit', '?')}")
        parts.append(f"outcome.tp2_hit={evaluation.get('tp2_hit', '?')}")
        parts.append(f"outcome.sl_hit={evaluation.get('sl_hit', '?')}")
        if (pnl := evaluation.get("pnl_pct")) is not None:
            parts.append(f"outcome.pnl_pct={round(float(pnl), 3)}")
        if label := evaluation.get("result_label"):
            parts.append(f"outcome.label={label}")

    return " | ".join(parts)


def build_outcome_metadata(prediction: dict, evaluation: dict) -> dict:
    """Construct Chroma-safe (flat, primitive-typed) metadata for an outcome."""
    sig = prediction.get("signal", {}) if isinstance(prediction.get("signal"), dict) else prediction
    now = datetime.now(UTC)
    return {
        "symbol": prediction.get("symbol", sig.get("symbol", "?")),
        "timeframe": prediction.get("timeframe", sig.get("timeframe", "?")),
        "signal_type": sig.get("signal", "HOLD"),
        "confidence": int(sig.get("confidence", 0) or 0),
        "regime": sig.get("market_regime", "?"),
        "trend": sig.get("trend_direction", "?"),
        "direction_correct": bool(evaluation.get("direction_correct", False)),
        "tp1_hit": bool(evaluation.get("tp1_hit", False)),
        "tp2_hit": bool(evaluation.get("tp2_hit", False)),
        "sl_hit": bool(evaluation.get("sl_hit", False)),
        "pnl_pct": float(evaluation.get("pnl_pct", 0.0) or 0.0),
        "result_label": evaluation.get("result_label", "unknown"),
        "indexed_at": now.isoformat(),
        "indexed_at_ts": now.timestamp(),
        "prediction_id": prediction.get("prediction_id") or prediction.get("id") or "",
    }


# =============================================================================
# INTERNAL HELPERS
# =============================================================================


def _chroma_where(where: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Chroma requires multi-key filters to be wrapped with an explicit $and operator.
    Single-key filters can be passed as-is. Returns None for empty/missing filters.
    """
    if not where:
        return None
    if len(where) == 1:
        return where
    return {"$and": [{k: v} for k, v in where.items()]}


def _flatten_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Chroma metadata values must be str/int/float/bool. Coerce or drop the rest."""
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, (list, dict)):
            try:
                out[k] = json.dumps(v, default=str)[:1000]
            except (TypeError, ValueError):
                out[k] = str(v)[:1000]
        else:
            out[k] = str(v)[:1000]
    return out


def _unpack_query_result(result: dict) -> list[dict]:
    """Convert Chroma's column-oriented response into a list of row dicts."""
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    rows: list[dict] = []
    for i, rid in enumerate(ids):
        rows.append(
            {
                "id": rid,
                "document": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "distance": float(dists[i]) if i < len(dists) and dists[i] is not None else None,
                "similarity": (
                    (1.0 - float(dists[i])) if i < len(dists) and dists[i] is not None else None
                ),
            }
        )
    return rows
