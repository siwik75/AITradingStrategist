"""
Liquidity Tools — Order book depth, liquidity zones, slippage estimation.

Uses ccxt (optional dep via `live-data` extra). If ccxt is unavailable, the
tools return a structured "unavailable" response rather than raising.

The "liquidity zones" output identifies large bid/ask clusters that act as
support/resistance walls — a useful overlay on top of technical levels.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from config.settings import get_config

log = structlog.get_logger()


# =============================================================================
# IN-PROCESS TTL CACHE — order book is volatile, short TTL
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
# ORDER BOOK FETCH
# =============================================================================


async def fetch_order_book(
    symbol: str,
    exchange: str | None = None,
    depth: int | None = None,
) -> dict:
    """
    Fetch L2 order book via ccxt. Returns:
        {
          "available": bool,
          "symbol": str,
          "exchange": str,
          "bids": [[price, size], ...] (best-first),
          "asks": [[price, size], ...] (best-first),
          "mid": float,
          "spread": float,
          "spread_bps": float,
          "fetched_at": ISO-8601,
        }
    """
    cfg = get_config()
    if not cfg.liquidity.enabled:
        return {"available": False, "reason": "disabled", "symbol": symbol}

    ex_name = (exchange or cfg.market_data.ccxt_exchange).lower()
    d = depth or cfg.liquidity.depth

    cache_key = f"orderbook:{ex_name}:{symbol}:{d}"
    cached = _cache_get(cache_key, cfg.liquidity.cache_ttl_seconds)
    if cached is not None:
        return cached

    try:
        import ccxt  # type: ignore
    except ImportError:
        log.warning("liquidity.ccxt_missing", hint="install with: pip install ccxt")
        return {"available": False, "reason": "ccxt_not_installed", "symbol": symbol}

    if not hasattr(ccxt, ex_name):
        return {"available": False, "reason": f"unknown_exchange:{ex_name}", "symbol": symbol}

    def _sync_fetch() -> dict:
        client = getattr(ccxt, ex_name)({"enableRateLimit": True})
        try:
            return client.fetch_order_book(symbol, limit=d)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    try:
        ob = await asyncio.to_thread(_sync_fetch)
    except Exception as exc:  # noqa: BLE001
        log.warning("liquidity.fetch_failed", error=str(exc), symbol=symbol, exchange=ex_name)
        return {"available": False, "reason": str(exc), "symbol": symbol, "exchange": ex_name}

    bids = [[float(p), float(s)] for p, s in (ob.get("bids") or [])[:d]]
    asks = [[float(p), float(s)] for p, s in (ob.get("asks") or [])[:d]]

    if not bids or not asks:
        return {"available": False, "reason": "empty_book", "symbol": symbol, "exchange": ex_name}

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread = best_ask - best_bid
    spread_bps = (spread / mid) * 10000.0 if mid else 0.0

    result = {
        "available": True,
        "symbol": symbol,
        "exchange": ex_name,
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "spread_bps": round(spread_bps, 3),
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    _cache_set(cache_key, result)
    return result


# =============================================================================
# LIQUIDITY ZONES — detect large walls
# =============================================================================


def compute_liquidity_zones(order_book: dict, top_n: int = 5) -> dict:
    """
    Identify the largest bid/ask "walls" — clusters where size is significantly
    above the average. These act as short-term support/resistance.

    :param order_book: Result from fetch_order_book()
    :param top_n: Return up to N walls per side
    """
    if not order_book.get("available"):
        return {"available": False, "reason": order_book.get("reason", "no_book")}

    cfg = get_config().liquidity
    mult = cfg.wall_threshold_multiplier
    bids: list[list[float]] = order_book.get("bids", [])
    asks: list[list[float]] = order_book.get("asks", [])
    if not bids or not asks:
        return {"available": False, "reason": "empty_book"}

    def _walls(side: list[list[float]]) -> list[dict]:
        sizes = [s for _, s in side]
        if not sizes:
            return []
        avg = sum(sizes) / len(sizes)
        threshold = avg * mult
        out = [
            {"price": p, "size": s, "size_x_avg": round(s / avg, 2) if avg else 0.0}
            for p, s in side
            if s >= threshold
        ]
        out.sort(key=lambda x: x["size"], reverse=True)
        return out[:top_n]

    bid_walls = _walls(bids)
    ask_walls = _walls(asks)

    # Compute total notional and depth-imbalance within ±1% of mid
    mid = float(order_book["mid"])
    band_lo, band_hi = mid * 0.99, mid * 1.01
    bid_notional = sum(p * s for p, s in bids if band_lo <= p <= mid)
    ask_notional = sum(p * s for p, s in asks if mid <= p <= band_hi)
    total = bid_notional + ask_notional
    imbalance = ((bid_notional - ask_notional) / total) if total else 0.0

    return {
        "available": True,
        "symbol": order_book.get("symbol"),
        "exchange": order_book.get("exchange"),
        "mid": mid,
        "support_walls": bid_walls,
        "resistance_walls": ask_walls,
        "bid_notional_1pct": round(bid_notional, 2),
        "ask_notional_1pct": round(ask_notional, 2),
        "depth_imbalance_1pct": round(imbalance, 3),  # +ve = more bids, bullish microstructure
        "imbalance_label": (
            "bullish" if imbalance > 0.1 else "bearish" if imbalance < -0.1 else "balanced"
        ),
        "computed_at": datetime.now(UTC).isoformat(),
    }


# =============================================================================
# SLIPPAGE ESTIMATOR
# =============================================================================


def estimate_slippage(order_book: dict, size_quote: float, side: str = "buy") -> dict:
    """
    Estimate average fill price and slippage in bps to execute `size_quote`
    (in quote currency, e.g. USDT) as a market order.

    :param order_book: Result from fetch_order_book()
    :param size_quote: Notional size to execute, in quote currency
    :param side: "buy" walks asks, "sell" walks bids
    """
    if not order_book.get("available"):
        return {"available": False, "reason": order_book.get("reason", "no_book")}

    side = side.lower()
    levels: list[list[float]] = order_book["asks"] if side == "buy" else order_book["bids"]
    if not levels:
        return {"available": False, "reason": "empty_side"}

    remaining = size_quote
    spent_quote = 0.0
    filled_base = 0.0
    walked_to: float = levels[0][0]

    for price, base_size in levels:
        level_quote = price * base_size
        take = min(remaining, level_quote)
        take_base = take / price if price else 0.0
        spent_quote += take
        filled_base += take_base
        walked_to = price
        remaining -= take
        if remaining <= 0:
            break

    if filled_base == 0:
        return {"available": False, "reason": "no_fill"}

    avg_price = spent_quote / filled_base
    mid = float(order_book["mid"])
    slippage_bps = ((avg_price - mid) / mid) * 10000.0 if mid else 0.0
    if side == "sell":
        slippage_bps = -slippage_bps

    return {
        "available": True,
        "side": side,
        "requested_quote": size_quote,
        "filled_quote": round(spent_quote, 2),
        "filled_base": round(filled_base, 8),
        "avg_price": round(avg_price, 4),
        "walked_to": round(walked_to, 4),
        "slippage_bps": round(slippage_bps, 2),
        "fully_filled": remaining <= 0,
    }


# =============================================================================
# UNIFIED SUMMARY (handy for SignalAgent context)
# =============================================================================


async def get_liquidity_snapshot(
    symbol: str,
    exchange: str | None = None,
    probe_size_quote: float | None = None,
) -> dict:
    """
    One-call snapshot for the SignalAgent prompt: order book stats + zones +
    a slippage probe (default $50k buy and $50k sell).
    """
    book = await fetch_order_book(symbol, exchange=exchange)
    if not book.get("available"):
        return {"available": False, "reason": book.get("reason"), "symbol": symbol}

    zones = compute_liquidity_zones(book)
    probe = probe_size_quote or 50_000.0
    buy_probe = estimate_slippage(book, probe, side="buy")
    sell_probe = estimate_slippage(book, probe, side="sell")

    return {
        "available": True,
        "symbol": symbol,
        "exchange": book["exchange"],
        "mid": book["mid"],
        "spread_bps": book["spread_bps"],
        "zones": {
            "support_walls": zones.get("support_walls", []),
            "resistance_walls": zones.get("resistance_walls", []),
            "depth_imbalance_1pct": zones.get("depth_imbalance_1pct"),
            "imbalance_label": zones.get("imbalance_label"),
        },
        "slippage_probe": {
            "size_quote": probe,
            "buy_bps": buy_probe.get("slippage_bps"),
            "sell_bps": sell_probe.get("slippage_bps"),
            "buy_fully_filled": buy_probe.get("fully_filled"),
            "sell_fully_filled": sell_probe.get("fully_filled"),
        },
        "fetched_at": book["fetched_at"],
    }
