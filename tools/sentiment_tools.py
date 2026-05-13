"""
Sentiment Tools — Fear & Greed indices + LLM-based news sentiment summarization.

Crypto F&G: https://alternative.me/crypto/fear-and-greed-index/
Stock F&G:  CNN endpoint at https://production.dataviz.cnn.io/index/fearandgreed/graphdata

Both are read-only public endpoints. F&G updates daily — long cache TTL is fine.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from config.settings import get_config

log = structlog.get_logger()


# =============================================================================
# IN-PROCESS TTL CACHE (separate from news to allow different TTL)
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
    _cache.clear()


# =============================================================================
# FEAR & GREED — CRYPTO (alternative.me)
# =============================================================================


def _classify_fg(value: int) -> str:
    if value < 25:
        return "extreme_fear"
    if value < 45:
        return "fear"
    if value < 55:
        return "neutral"
    if value < 75:
        return "greed"
    return "extreme_greed"


async def get_fear_greed_crypto(timeout: float | None = None) -> dict:
    """
    Fetch the Crypto Fear & Greed Index from alternative.me (free, no key).

    :returns: {
        "value": int (0-100),
        "classification": str,
        "timestamp": ISO-8601,
        "previous_value": int | None,
        "delta": int | None,
        "source": "alternative.me",
    }
    """
    cfg = get_config().news
    cache_key = "fg:crypto"
    cached = _cache_get(cache_key, cfg.fear_greed_cache_ttl_seconds)
    if cached is not None:
        return cached

    url = "https://api.alternative.me/fng/"
    params = {"limit": "2"}
    try:
        async with httpx.AsyncClient(timeout=timeout or cfg.http_timeout_seconds) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("sentiment.fg_crypto.fetch_failed", error=str(exc))
        return {"available": False, "error": str(exc), "source": "alternative.me"}

    data = payload.get("data") or []
    if not data:
        return {"available": False, "error": "empty_response", "source": "alternative.me"}

    current = data[0]
    try:
        value = int(current.get("value", 0))
    except (TypeError, ValueError):
        return {"available": False, "error": "invalid_value", "source": "alternative.me"}

    previous_value: int | None = None
    if len(data) > 1:
        try:
            previous_value = int(data[1].get("value", 0))
        except (TypeError, ValueError):
            previous_value = None

    result = {
        "available": True,
        "value": value,
        "classification": _classify_fg(value),
        "timestamp": (
            datetime.fromtimestamp(int(current.get("timestamp", 0)), tz=UTC).isoformat()
            if current.get("timestamp")
            else datetime.now(UTC).isoformat()
        ),
        "previous_value": previous_value,
        "delta": (value - previous_value) if previous_value is not None else None,
        "source": "alternative.me",
    }
    _cache_set(cache_key, result)
    return result


# =============================================================================
# FEAR & GREED — STOCKS (CNN)
# =============================================================================


async def get_fear_greed_stocks(timeout: float | None = None) -> dict:
    """
    Fetch the CNN Fear & Greed index (stocks). Free public endpoint.
    """
    cfg = get_config().news
    cache_key = "fg:stocks"
    cached = _cache_get(cache_key, cfg.fear_greed_cache_ttl_seconds)
    if cached is not None:
        return cached

    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    # CNN requires a real user-agent or returns 403
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; trading-intelligence-agent/1.0)",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        async with httpx.AsyncClient(
            timeout=timeout or cfg.http_timeout_seconds, headers=headers
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("sentiment.fg_stocks.fetch_failed", error=str(exc))
        return {"available": False, "error": str(exc), "source": "cnn"}

    fg = payload.get("fear_and_greed") or {}
    try:
        value = int(round(float(fg.get("score", 0))))
    except (TypeError, ValueError):
        return {"available": False, "error": "invalid_value", "source": "cnn"}

    result = {
        "available": True,
        "value": value,
        "classification": _classify_fg(value),
        "rating": fg.get("rating"),
        "timestamp": fg.get("timestamp") or datetime.now(UTC).isoformat(),
        "previous_close": fg.get("previous_close"),
        "previous_1_week": fg.get("previous_1_week"),
        "previous_1_month": fg.get("previous_1_month"),
        "source": "cnn",
    }
    _cache_set(cache_key, result)
    return result


async def get_fear_greed(asset_class: str = "crypto") -> dict:
    """
    Unified accessor. asset_class ∈ {"crypto", "stocks"}.
    """
    if asset_class.lower() == "stocks":
        return await get_fear_greed_stocks()
    return await get_fear_greed_crypto()


# =============================================================================
# LLM-POWERED NEWS SUMMARIZATION
# =============================================================================

_SUMMARIZER_SYSTEM_PROMPT = """You are a market analyst. Given a batch of financial news headlines,
produce a concise sentiment digest as STRICT JSON only — no prose, no markdown.

Output schema:
{
  "overall_sentiment": "bullish" | "bearish" | "neutral",
  "sentiment_score": float in [-1.0, 1.0],
  "confidence": int in [0, 100],
  "themes": ["short theme strings, max 5"],
  "bullish_drivers": ["short phrases, max 3"],
  "bearish_drivers": ["short phrases, max 3"],
  "summary": "2-3 sentence digest written for a trader"
}

Rules:
- Weigh items by recency and explicit sentiment fields when provided.
- If items conflict, set confidence below 50.
- Never invent facts not present in the input."""


def _build_summarizer_user_message(symbol: str, articles: list[dict]) -> str:
    lines = [f"Symbol: {symbol}", f"Articles ({len(articles)}):", ""]
    for i, art in enumerate(articles[:30], 1):
        sentiment = art.get("sentiment") or "n/a"
        score = art.get("sentiment_score")
        score_str = f"{score:+.2f}" if isinstance(score, (int, float)) else "n/a"
        title = (art.get("title") or "").strip().replace("\n", " ")[:200]
        summary = (art.get("summary") or "").strip().replace("\n", " ")[:300]
        lines.append(f"{i}. [{art.get('source','?')}] sentiment={sentiment} score={score_str}")
        lines.append(f"   title: {title}")
        if summary and summary != title:
            lines.append(f"   summary: {summary}")
    lines.append("")
    lines.append("Return the JSON object now.")
    return "\n".join(lines)


def _heuristic_summary(symbol: str, articles: list[dict]) -> dict:
    """Score-only fallback when no LLM credentials are available."""
    scored = [a for a in articles if isinstance(a.get("sentiment_score"), (int, float))]
    if not scored:
        return {
            "overall_sentiment": "neutral",
            "sentiment_score": 0.0,
            "confidence": 0,
            "themes": [],
            "bullish_drivers": [],
            "bearish_drivers": [],
            "summary": f"No scored sentiment available for {symbol}.",
            "method": "no_data",
        }
    avg = sum(a["sentiment_score"] for a in scored) / len(scored)
    label = "bullish" if avg > 0.1 else "bearish" if avg < -0.1 else "neutral"
    return {
        "overall_sentiment": label,
        "sentiment_score": round(avg, 3),
        "confidence": min(100, int(40 + len(scored) * 4)),
        "themes": [],
        "bullish_drivers": [a["title"] for a in scored if a["sentiment_score"] > 0.2][:3],
        "bearish_drivers": [a["title"] for a in scored if a["sentiment_score"] < -0.2][:3],
        "summary": f"Average sentiment across {len(scored)} scored articles: {avg:+.2f} ({label}).",
        "method": "heuristic_average",
    }


async def summarize_news_sentiment(symbol: str, articles: list[dict]) -> dict:
    """
    Summarize a batch of articles into a structured sentiment digest.

    Uses the configured summarizer model (Haiku 4.5 by default) via the same
    LLM credentials BaseAgent uses. Falls back to a heuristic average if no
    credentials or the LLM call fails.
    """
    if not articles:
        return {
            "overall_sentiment": "neutral",
            "sentiment_score": 0.0,
            "confidence": 0,
            "themes": [],
            "bullish_drivers": [],
            "bearish_drivers": [],
            "summary": f"No articles available for {symbol}.",
            "method": "empty",
        }

    cfg = get_config().llm
    model = cfg.summarizer_model
    user_message = _build_summarizer_user_message(symbol, articles)

    # Try Anthropic native first
    if cfg.has_anthropic_credentials():
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
            resp = client.messages.create(
                model=model,
                max_tokens=600,
                temperature=0.0,
                system=_SUMMARIZER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            from anthropic.types import TextBlock as _TextBlock

            text = "".join(block.text for block in resp.content if isinstance(block, _TextBlock))
            parsed = _safe_json(text)
            if parsed:
                parsed["method"] = "llm_anthropic"
                parsed["model"] = model
                return parsed
        except Exception as exc:  # noqa: BLE001
            log.warning("sentiment.summarize.anthropic_failed", error=str(exc))

    # OpenAI-compatible gateway
    if cfg.has_gateway_credentials():
        try:
            from openai import OpenAI

            openai_client = OpenAI(api_key=cfg.gateway_api_key, base_url=cfg.gateway_url)
            openai_resp = openai_client.chat.completions.create(
                model=cfg.openai_model,
                temperature=0.0,
                max_tokens=600,
                messages=[
                    {"role": "system", "content": _SUMMARIZER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
            )
            text = openai_resp.choices[0].message.content if openai_resp.choices else ""
            parsed = _safe_json(text or "")
            if parsed:
                parsed["method"] = "llm_gateway"
                parsed["model"] = cfg.openai_model
                return parsed
        except Exception as exc:  # noqa: BLE001
            log.warning("sentiment.summarize.gateway_failed", error=str(exc))

    return _heuristic_summary(symbol, articles)


def _safe_json(text: str) -> dict | None:
    """Parse a JSON object from text, tolerating leading/trailing prose."""
    text = (text or "").strip()
    if not text:
        return None
    # Fast path
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the first '{' .. last '}' substring
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
