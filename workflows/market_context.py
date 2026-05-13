"""
MarketContextBuilder — pre-fetches external context for a signal cycle.

Bundles news, sentiment, F&G, and liquidity into a single dict that the
SignalAgent receives as pre-computed context. This avoids forcing the LLM
to spend tool-call rounds on data fetching it can't reason about.

Each sub-fetcher fails soft: if a provider is unconfigured or errors, that
section is marked unavailable but the rest of the context still flows.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from config.settings import get_config
from tools.liquidity_tools import get_liquidity_snapshot
from tools.news_tools import _is_crypto_symbol, fetch_news
from tools.sentiment_tools import get_fear_greed, summarize_news_sentiment

log = structlog.get_logger()


async def build_market_context(
    symbol: str,
    timeframe: str,
    *,
    asset_class: str | None = None,
    include_liquidity: bool = True,
    include_news: bool = True,
    include_sentiment: bool = True,
    correlation_id: str | None = None,
) -> dict:
    """
    Fetch news + F&G + liquidity in parallel and return a single context dict.

    :param symbol: Trading symbol (e.g. BTC/USDT or AAPL)
    :param timeframe: Analysis timeframe (used for metadata only)
    :param asset_class: "crypto" | "stocks" (auto-detected if None)
    :param include_liquidity: If False, skip the order-book fetch
    :param include_news: If False, skip news fetch
    :param include_sentiment: If False, skip F&G fetch
    :param correlation_id: Tracing ID (logged)
    """
    cfg = get_config()
    asset = asset_class or ("crypto" if _is_crypto_symbol(symbol) else "stocks")

    news_task = (
        fetch_news(symbol)
        if include_news and cfg.news.enabled
        else _placeholder({"enabled": False})
    )
    fg_task = (
        get_fear_greed(asset)
        if include_sentiment
        else _placeholder({"available": False, "reason": "disabled"})
    )
    liq_task = (
        get_liquidity_snapshot(symbol)
        if include_liquidity and cfg.liquidity.enabled and asset == "crypto"
        else _placeholder({"available": False, "reason": "disabled_or_non_crypto"})
    )

    news_raw, fg, liquidity = await asyncio.gather(
        news_task, fg_task, liq_task, return_exceptions=True
    )

    if isinstance(news_raw, Exception):
        log.warning(
            "market_context.news_failed", error=str(news_raw), correlation_id=correlation_id
        )
        news_raw = {"articles": [], "enabled": True, "error": str(news_raw)}
    if isinstance(fg, Exception):
        log.warning("market_context.fg_failed", error=str(fg), correlation_id=correlation_id)
        fg = {"available": False, "error": str(fg)}
    if isinstance(liquidity, Exception):
        log.warning(
            "market_context.liquidity_failed", error=str(liquidity), correlation_id=correlation_id
        )
        liquidity = {"available": False, "error": str(liquidity)}

    # Summarize news only if we have articles
    news_sentiment = None
    if news_raw and news_raw.get("articles"):
        try:
            news_sentiment = await summarize_news_sentiment(symbol, news_raw["articles"])
        except Exception as exc:  # noqa: BLE001
            log.warning("market_context.news_summarize_failed", error=str(exc))
            news_sentiment = {"overall_sentiment": "neutral", "confidence": 0, "method": "error"}

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "asset_class": asset,
        "built_at": datetime.now(UTC).isoformat(),
        "correlation_id": correlation_id,
        "fear_greed": fg,
        "news": {
            "enabled": news_raw.get("enabled", True) if isinstance(news_raw, dict) else False,
            "article_count": (
                len(news_raw.get("articles", []) or []) if isinstance(news_raw, dict) else 0
            ),
            "by_source": news_raw.get("by_source", {}) if isinstance(news_raw, dict) else {},
            "missing_keys": news_raw.get("missing_keys", []) if isinstance(news_raw, dict) else [],
            "top_articles": _trim_articles(
                news_raw.get("articles", []) if isinstance(news_raw, dict) else [], n=8
            ),
        },
        "news_sentiment": news_sentiment,
        "liquidity": liquidity,
    }


# =============================================================================
# PROMPT FORMATTING
# =============================================================================


def format_market_context_text(ctx: dict) -> str:
    """Render the context dict as a compact block for prompt injection."""
    lines: list[str] = ["=== MARKET CONTEXT ==="]

    fg = ctx.get("fear_greed") or {}
    if fg.get("available"):
        delta = fg.get("delta")
        delta_str = f" (Δ={delta:+d})" if isinstance(delta, int) else ""
        lines.append(
            f"- Fear & Greed [{fg.get('source')}]: {fg.get('value')} "
            f"({fg.get('classification')}){delta_str}"
        )
    else:
        lines.append(
            f"- Fear & Greed: unavailable ({fg.get('reason') or fg.get('error') or 'n/a'})"
        )

    ns = ctx.get("news_sentiment") or {}
    if ns and ns.get("overall_sentiment"):
        themes = ", ".join((ns.get("themes") or [])[:5]) or "—"
        lines.append(
            f"- News sentiment: {ns.get('overall_sentiment')} "
            f"(score={ns.get('sentiment_score')}, conf={ns.get('confidence')}, "
            f"method={ns.get('method', 'n/a')})"
        )
        if ns.get("summary"):
            lines.append(f"  summary: {ns['summary']}")
        if themes != "—":
            lines.append(f"  themes: {themes}")
        bd = ns.get("bullish_drivers") or []
        rd = ns.get("bearish_drivers") or []
        if bd:
            lines.append("  bullish_drivers: " + " | ".join(d[:120] for d in bd[:3]))
        if rd:
            lines.append("  bearish_drivers: " + " | ".join(d[:120] for d in rd[:3]))
    else:
        news_meta = ctx.get("news") or {}
        if news_meta.get("missing_keys"):
            lines.append(
                "- News sentiment: no provider keys configured "
                f"({', '.join(news_meta['missing_keys'])})"
            )
        else:
            lines.append("- News sentiment: no articles available")

    liq = ctx.get("liquidity") or {}
    if liq.get("available"):
        zones = liq.get("zones") or {}
        slip = liq.get("slippage_probe") or {}
        lines.append(
            f"- Liquidity [{liq.get('exchange')}]: mid={liq.get('mid')} "
            f"spread_bps={liq.get('spread_bps')} imbalance={zones.get('imbalance_label')}"
            f"({zones.get('depth_imbalance_1pct')})"
        )
        sw = zones.get("support_walls") or []
        rw = zones.get("resistance_walls") or []
        if sw:
            lines.append(
                "  support_walls: " + ", ".join(f"{w['price']}@x{w['size_x_avg']}" for w in sw[:3])
            )
        if rw:
            lines.append(
                "  resistance_walls: "
                + ", ".join(f"{w['price']}@x{w['size_x_avg']}" for w in rw[:3])
            )
        if slip:
            lines.append(
                f"  slippage_probe(${int(slip.get('size_quote', 0)):,}): "
                f"buy={slip.get('buy_bps')}bps sell={slip.get('sell_bps')}bps"
            )
    else:
        lines.append(f"- Liquidity: unavailable ({liq.get('reason') or liq.get('error') or 'n/a'})")

    lines.append("=== END MARKET CONTEXT ===")
    return "\n".join(lines)


# =============================================================================
# INTERNAL HELPERS
# =============================================================================


async def _placeholder(value: dict) -> dict:
    return value


def _trim_articles(articles: list[dict], n: int = 8) -> list[dict]:
    """Keep only the fields a downstream summarizer or human needs."""
    trimmed: list[dict] = []
    for a in articles[:n]:
        trimmed.append(
            {
                "source": a.get("source"),
                "title": (a.get("title") or "")[:200],
                "published_at": a.get("published_at"),
                "sentiment": a.get("sentiment"),
                "sentiment_score": a.get("sentiment_score"),
                "url": a.get("url"),
            }
        )
    return trimmed
