"""
News Tools — Fetch market news from multiple providers and normalize to a common schema.

Providers (free tiers):
- CryptoPanic        — crypto-focused, includes sentiment tags
- Alpha Vantage News — pre-scored sentiment + per-ticker relevance
- NewsAPI.org        — broad coverage, raw text (sentiment must be inferred)

All fetchers return a list of NormalizedArticle dicts:
    {
      "source": "cryptopanic" | "alpha_vantage" | "newsapi",
      "title": str,
      "url": str,
      "published_at": ISO-8601 str (UTC),
      "summary": str,
      "tickers": list[str],
      "sentiment": "bullish" | "bearish" | "neutral" | None,
      "sentiment_score": float | None,   # -1.0..1.0 when available
      "relevance": float | None,         # 0..1 when available (Alpha Vantage)
      "raw": dict,                       # provider-specific original payload
    }
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from config.settings import get_config

log = structlog.get_logger()


# =============================================================================
# IN-PROCESS TTL CACHE
# =============================================================================

_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str, ttl: int) -> Any | None:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > ttl:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


def _clear_cache() -> None:
    """Test helper — clear the in-process cache."""
    _cache.clear()


# =============================================================================
# SYMBOL HELPERS
# =============================================================================

_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "BUSD", "EUR", "GBP")


def _base_symbol(symbol: str) -> str:
    """Extract base asset from 'BTC/USDT' -> 'BTC', 'AAPL' -> 'AAPL'."""
    if "/" in symbol:
        return symbol.split("/", 1)[0].upper()
    s = symbol.upper()
    for q in _QUOTE_SUFFIXES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def _is_crypto_symbol(symbol: str) -> bool:
    """Heuristic: pair-form (BTC/USDT) or known crypto base suggests crypto."""
    if "/" in symbol:
        return True
    base = _base_symbol(symbol)
    return base in {
        "BTC",
        "ETH",
        "SOL",
        "BNB",
        "XRP",
        "ADA",
        "DOGE",
        "AVAX",
        "DOT",
        "MATIC",
        "LINK",
        "TON",
    }


# =============================================================================
# PROVIDER: CRYPTOPANIC
# =============================================================================


# CryptoPanic vote → sentiment heuristic
def _cryptopanic_sentiment(votes: dict | None) -> tuple[str | None, float | None]:
    if not votes:
        return None, None
    positive = int(votes.get("positive", 0) or 0)
    negative = int(votes.get("negative", 0) or 0)
    important = int(votes.get("important", 0) or 0)
    total = positive + negative + max(important, 0)
    if total == 0:
        return None, None
    score = (positive - negative) / max(total, 1)
    if score > 0.15:
        return "bullish", score
    if score < -0.15:
        return "bearish", score
    return "neutral", score


async def fetch_cryptopanic(
    symbol: str,
    limit: int = 15,
    timeout: float | None = None,
) -> list[dict]:
    """
    Fetch crypto news from CryptoPanic. Returns normalized articles.

    Requires CRYPTOPANIC_API_KEY. Returns [] if no key configured.
    Public endpoint: https://cryptopanic.com/api/v1/posts/
    """
    cfg = get_config().news
    if not cfg.cryptopanic_api_key:
        return []

    cache_key = f"cryptopanic:{symbol}:{limit}"
    cached = _cache_get(cache_key, cfg.cache_ttl_seconds)
    if cached is not None:
        return cached

    base = _base_symbol(symbol)
    params = {
        "auth_token": cfg.cryptopanic_api_key,
        "currencies": base,
        "kind": "news",
        "public": "true",
    }
    url = "https://cryptopanic.com/api/v1/posts/"
    try:
        async with httpx.AsyncClient(timeout=timeout or cfg.http_timeout_seconds) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("news.cryptopanic.fetch_failed", error=str(exc), symbol=symbol)
        return []

    articles: list[dict] = []
    for post in (payload.get("results") or [])[:limit]:
        sentiment, score = _cryptopanic_sentiment(post.get("votes"))
        articles.append(
            {
                "source": "cryptopanic",
                "title": post.get("title", ""),
                "url": post.get("url", ""),
                "published_at": post.get("published_at", ""),
                "summary": post.get("title", ""),  # CryptoPanic doesn't return bodies on free tier
                "tickers": [
                    c.get("code", "") for c in (post.get("currencies") or []) if c.get("code")
                ],
                "sentiment": sentiment,
                "sentiment_score": score,
                "relevance": None,
                "raw": post,
            }
        )

    _cache_set(cache_key, articles)
    return articles


# =============================================================================
# PROVIDER: ALPHA VANTAGE NEWS & SENTIMENT
# =============================================================================


def _alpha_vantage_label(score: float) -> str:
    """Alpha Vantage sentiment label thresholds (their published thresholds)."""
    if score <= -0.35:
        return "bearish"
    if score < -0.15:
        return "bearish"
    if score >= 0.35:
        return "bullish"
    if score > 0.15:
        return "bullish"
    return "neutral"


async def fetch_alpha_vantage_news(
    symbol: str,
    limit: int = 15,
    timeout: float | None = None,
) -> list[dict]:
    """
    Fetch news + sentiment from Alpha Vantage NEWS_SENTIMENT endpoint.
    Free tier: 25 requests/day. Returns [] if no key configured.

    Supports both equities tickers (AAPL) and crypto (CRYPTO:BTC).
    """
    cfg = get_config().news
    if not cfg.alpha_vantage_api_key:
        return []

    cache_key = f"alphavantage:{symbol}:{limit}"
    cached = _cache_get(cache_key, cfg.cache_ttl_seconds)
    if cached is not None:
        return cached

    base = _base_symbol(symbol)
    ticker_param = f"CRYPTO:{base}" if _is_crypto_symbol(symbol) else base

    time_from = (datetime.now(UTC) - timedelta(hours=cfg.lookback_hours)).strftime("%Y%m%dT%H%M")
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ticker_param,
        "time_from": time_from,
        "sort": "LATEST",
        "limit": str(min(limit, 50)),
        "apikey": cfg.alpha_vantage_api_key,
    }
    url = "https://www.alphavantage.co/query"
    try:
        async with httpx.AsyncClient(timeout=timeout or cfg.http_timeout_seconds) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("news.alpha_vantage.fetch_failed", error=str(exc), symbol=symbol)
        return []

    if "feed" not in payload:
        # Most often a rate-limit or invalid-key shape; log and bail.
        if payload.get("Information") or payload.get("Note"):
            log.info("news.alpha_vantage.rate_or_info", info=str(payload)[:200])
        return []

    articles: list[dict] = []
    for item in payload["feed"][:limit]:
        # Find the relevance + sentiment scored against our ticker if present
        per_ticker = item.get("ticker_sentiment") or []
        relevance: float | None = None
        score: float | None = None
        for t in per_ticker:
            if t.get("ticker", "").upper() in (ticker_param, base):
                try:
                    relevance = float(t.get("relevance_score") or 0.0) or None
                    score = float(t.get("ticker_sentiment_score") or 0.0)
                except (TypeError, ValueError):
                    pass
                break
        if score is None:
            try:
                score = float(item.get("overall_sentiment_score") or 0.0)
            except (TypeError, ValueError):
                score = None

        articles.append(
            {
                "source": "alpha_vantage",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "published_at": item.get("time_published", ""),
                "summary": item.get("summary", ""),
                "tickers": [t.get("ticker", "") for t in per_ticker if t.get("ticker")],
                "sentiment": _alpha_vantage_label(score) if score is not None else None,
                "sentiment_score": score,
                "relevance": relevance,
                "raw": item,
            }
        )

    _cache_set(cache_key, articles)
    return articles


# =============================================================================
# PROVIDER: NEWSAPI.ORG
# =============================================================================


async def fetch_newsapi(
    symbol: str,
    limit: int = 15,
    timeout: float | None = None,
) -> list[dict]:
    """
    Fetch broad financial news from NewsAPI.org (free tier: 100 req/day).
    Returns articles with no sentiment scoring — caller may summarize via LLM.
    """
    cfg = get_config().news
    if not cfg.newsapi_api_key:
        return []

    cache_key = f"newsapi:{symbol}:{limit}"
    cached = _cache_get(cache_key, cfg.cache_ttl_seconds)
    if cached is not None:
        return cached

    base = _base_symbol(symbol)
    # Build a focused query; for crypto include the asset name + "crypto"
    query = base if not _is_crypto_symbol(symbol) else f"{base} OR crypto"

    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": str(min(limit, 100)),
        "apiKey": cfg.newsapi_api_key,
    }
    if cfg.lookback_hours > 0:
        params["from"] = (datetime.now(UTC) - timedelta(hours=cfg.lookback_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    url = "https://newsapi.org/v2/everything"
    try:
        async with httpx.AsyncClient(timeout=timeout or cfg.http_timeout_seconds) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("news.newsapi.fetch_failed", error=str(exc), symbol=symbol)
        return []

    if payload.get("status") != "ok":
        log.warning("news.newsapi.bad_status", payload=str(payload)[:200])
        return []

    articles: list[dict] = []
    for item in (payload.get("articles") or [])[:limit]:
        articles.append(
            {
                "source": "newsapi",
                "title": item.get("title", "") or "",
                "url": item.get("url", "") or "",
                "published_at": item.get("publishedAt", "") or "",
                "summary": item.get("description", "") or item.get("content", "") or "",
                "tickers": [base],
                "sentiment": None,
                "sentiment_score": None,
                "relevance": None,
                "raw": item,
            }
        )

    _cache_set(cache_key, articles)
    return articles


# =============================================================================
# UNIFIED FETCH
# =============================================================================


async def fetch_news(
    symbol: str,
    sources: list[str] | None = None,
    limit_per_source: int | None = None,
) -> dict:
    """
    Fetch news from all configured providers and merge.

    :param symbol: Trading symbol (e.g., BTC/USDT or AAPL)
    :param sources: Subset of ["cryptopanic", "alpha_vantage", "newsapi"]. None = all.
    :param limit_per_source: Override max articles per source.
    :returns: {"articles": [...], "by_source": {name: count}, "missing_keys": [...]}
    """
    cfg = get_config().news
    if not cfg.enabled:
        return {"articles": [], "by_source": {}, "missing_keys": [], "enabled": False}

    max_per = limit_per_source or cfg.max_articles_per_source
    selected = sources or ["cryptopanic", "alpha_vantage", "newsapi"]
    missing_keys: list[str] = []
    by_source: dict[str, int] = {}
    articles: list[dict] = []

    if "cryptopanic" in selected:
        if not cfg.cryptopanic_api_key:
            missing_keys.append("CRYPTOPANIC_API_KEY")
        else:
            fetched = await fetch_cryptopanic(symbol, limit=max_per)
            by_source["cryptopanic"] = len(fetched)
            articles.extend(fetched)

    if "alpha_vantage" in selected:
        if not cfg.alpha_vantage_api_key:
            missing_keys.append("ALPHA_VANTAGE_API_KEY")
        else:
            fetched = await fetch_alpha_vantage_news(symbol, limit=max_per)
            by_source["alpha_vantage"] = len(fetched)
            articles.extend(fetched)

    if "newsapi" in selected:
        if not cfg.newsapi_api_key:
            missing_keys.append("NEWSAPI_API_KEY")
        else:
            fetched = await fetch_newsapi(symbol, limit=max_per)
            by_source["newsapi"] = len(fetched)
            articles.extend(fetched)

    # Dedupe by URL, preserve first-seen
    seen: set[str] = set()
    deduped: list[dict] = []
    for a in articles:
        u = a.get("url") or a.get("title", "")
        if u in seen:
            continue
        seen.add(u)
        deduped.append(a)

    # Sort newest first; tolerate mixed timestamp formats
    deduped.sort(key=lambda a: a.get("published_at", ""), reverse=True)

    return {
        "articles": deduped,
        "by_source": by_source,
        "missing_keys": missing_keys,
        "enabled": True,
        "symbol": symbol,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
