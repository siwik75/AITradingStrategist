"""
Tests for the v2 trading intelligence layer:
- Enhanced VWAP block (session + anchored + bands + slope)
- News tools (mocked HTTP)
- Sentiment tools (F&G + heuristic summarizer)
- Liquidity tools (mocked ccxt)
- Knowledge tools (RAG-style retrieval over mocked vector store)
- MarketContextBuilder
- Knowledge indexer
"""
# ruff: noqa: I001

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Reset config + memory + vector store for each test."""
    monkeypatch.setenv("TRADING_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MARKET_DATA_SOURCE", "synthetic")
    monkeypatch.setenv("MARKET_DATA_FALLBACK_TO_SYNTHETIC", "true")
    monkeypatch.setenv("VECTOR_STORE_DIR", str(tmp_path / "vector"))
    # Disable real network access by default
    monkeypatch.delenv("CRYPTOPANIC_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    from config.settings import reset_config
    from memory.store import reset_memory_store
    from memory.vector_store import reset_vector_store

    reset_config()
    reset_memory_store()
    reset_vector_store()

    # Clear in-process caches in news/sentiment/liquidity modules
    from tools import news_tools, sentiment_tools, liquidity_tools
    news_tools._clear_cache()
    sentiment_tools._clear_cache()
    liquidity_tools._clear_cache()

    yield
    reset_config()
    reset_memory_store()
    reset_vector_store()


# =============================================================================
# 1. VWAP ENHANCEMENTS
# =============================================================================

class TestVwapBlock:
    @pytest.mark.asyncio
    async def test_vwap_block_present_in_full_indicators(self):
        from tools.trading_tools import calculate_indicators

        result = await calculate_indicators("BTC/USDT", "4h", "full")
        vol = result["volume"]

        assert "vwap" in vol
        assert "vwap_session" in vol
        assert "vwap_anchored" in vol

        session = vol["vwap_session"]
        for k in ("vwap", "upper_1s", "lower_1s", "upper_2s", "lower_2s", "slope_pct", "band_position"):
            assert k in session

        assert session["upper_2s"] >= session["upper_1s"] >= session["vwap"]
        assert session["lower_2s"] <= session["lower_1s"] <= session["vwap"]
        assert session["band_position"] in {
            "above_2s",
            "between_1s_2s_upper",
            "inside_1s",
            "between_1s_2s_lower",
            "below_2s",
        }

    @pytest.mark.asyncio
    async def test_vwap_bands_classify_extreme_prices(self):
        """A price set well above VWAP should land in 'above_2s'."""
        import pandas as pd
        from tools.trading_tools import _compute_vwap_block

        # Many flat bars at 100 (heavy volume) then a single thin-volume spike to 1000
        # → VWAP stays near 100, std stays small, current close is many sigma above.
        idx = pd.date_range("2024-01-01", periods=60, freq="1h", tz="UTC")
        prices = [100.0] * 59 + [1000.0]
        df = pd.DataFrame(
            {
                "high": [p + 0.1 for p in prices],
                "low": [p - 0.1 for p in prices],
                "close": prices,
                "open": prices,
                "volume": [100.0] * 59 + [0.01],
            },
            index=idx,
        )
        block = _compute_vwap_block(df)
        assert block["session"]["band_position"] == "above_2s"
        assert block["session"]["price_vs_vwap"] == "above"


# =============================================================================
# 2. NEWS TOOLS — mocked HTTP
# =============================================================================

class TestNewsTools:
    @pytest.mark.asyncio
    async def test_fetch_news_returns_empty_when_no_keys(self):
        from tools.news_tools import fetch_news

        result = await fetch_news("BTC/USDT")
        assert result["articles"] == []
        assert {"CRYPTOPANIC_API_KEY", "ALPHA_VANTAGE_API_KEY", "NEWSAPI_API_KEY"} == set(result["missing_keys"])

    @pytest.mark.asyncio
    async def test_cryptopanic_fetch_normalises_articles(self, monkeypatch):
        monkeypatch.setenv("CRYPTOPANIC_API_KEY", "test-key")
        from config.settings import reset_config
        reset_config()

        sample = {
            "results": [
                {
                    "title": "BTC rallies on ETF inflows",
                    "url": "https://example.com/a",
                    "published_at": "2024-01-01T00:00:00Z",
                    "votes": {"positive": 10, "negative": 1, "important": 0},
                    "currencies": [{"code": "BTC"}],
                }
            ]
        }

        class FakeResp:
            def __init__(self, payload):
                self._p = payload
                self.status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return self._p

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, params=None):
                return FakeResp(sample)

        with patch("tools.news_tools.httpx.AsyncClient", FakeClient):
            from tools.news_tools import fetch_cryptopanic
            articles = await fetch_cryptopanic("BTC/USDT")

        assert len(articles) == 1
        a = articles[0]
        assert a["source"] == "cryptopanic"
        assert a["sentiment"] == "bullish"
        assert a["sentiment_score"] is not None and a["sentiment_score"] > 0


# =============================================================================
# 3. SENTIMENT TOOLS — F&G + heuristic summarizer
# =============================================================================

class TestSentimentTools:
    @pytest.mark.asyncio
    async def test_fear_greed_crypto_parses_payload(self, monkeypatch):
        payload = {
            "data": [
                {"value": "78", "timestamp": "1700000000"},
                {"value": "65", "timestamp": "1699913600"},
            ]
        }

        class FakeResp:
            status_code = 200
            def raise_for_status(self): return None
            def json(self): return payload

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False
            async def get(self, url, params=None): return FakeResp()

        with patch("tools.sentiment_tools.httpx.AsyncClient", FakeClient):
            from tools.sentiment_tools import get_fear_greed_crypto
            fg = await get_fear_greed_crypto()

        assert fg["available"] is True
        assert fg["value"] == 78
        assert fg["classification"] == "extreme_greed"
        assert fg["previous_value"] == 65
        assert fg["delta"] == 13

    @pytest.mark.asyncio
    async def test_summarize_falls_back_to_heuristic_without_llm(self):
        from tools.sentiment_tools import summarize_news_sentiment

        articles = [
            {"title": "ETF approval bullish", "sentiment_score": 0.6, "sentiment": "bullish", "source": "x"},
            {"title": "Hack drains exchange", "sentiment_score": -0.5, "sentiment": "bearish", "source": "y"},
            {"title": "BTC sideways", "sentiment_score": 0.05, "sentiment": "neutral", "source": "z"},
        ]
        result = await summarize_news_sentiment("BTC/USDT", articles)
        assert result["method"] == "heuristic_average"
        assert result["overall_sentiment"] in {"bullish", "bearish", "neutral"}
        assert isinstance(result["sentiment_score"], float)


# =============================================================================
# 4. LIQUIDITY TOOLS — zones + slippage
# =============================================================================

class TestLiquidityTools:
    @pytest.mark.asyncio
    async def test_fetch_order_book_handles_missing_ccxt(self, monkeypatch):
        # Force the import inside fetch_order_book to fail
        import sys
        monkeypatch.setitem(sys.modules, "ccxt", None)
        from tools.liquidity_tools import fetch_order_book

        result = await fetch_order_book("BTC/USDT")
        assert result["available"] is False
        assert result["reason"] in {"ccxt_not_installed", "disabled"}

    def test_liquidity_zones_and_slippage(self, monkeypatch):
        # Use a small wall multiplier so the synthetic walls easily clear the threshold
        monkeypatch.setenv("LIQUIDITY_WALL_MULTIPLIER", "2.0")
        from config.settings import reset_config
        reset_config()
        from tools.liquidity_tools import compute_liquidity_zones, estimate_slippage

        # Synthetic L2 book: clear walls at 99 and 101
        book = {
            "available": True,
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "bids": [[100.0, 1.0], [99.5, 1.0], [99.0, 50.0], [98.5, 1.0], [98.0, 1.0]],
            "asks": [[100.5, 1.0], [101.0, 50.0], [101.5, 1.0], [102.0, 1.0], [102.5, 1.0]],
            "best_bid": 100.0,
            "best_ask": 100.5,
            "mid": 100.25,
            "spread": 0.5,
            "spread_bps": 49.875,
            "fetched_at": "now",
        }

        zones = compute_liquidity_zones(book)
        assert zones["available"]
        assert any(w["price"] == 99.0 for w in zones["support_walls"])
        assert any(w["price"] == 101.0 for w in zones["resistance_walls"])

        # Walking $200 of asks: 1 BTC at 100.5 + chunk at 101 ≈ avg ~100.6
        slip = estimate_slippage(book, size_quote=200.0, side="buy")
        assert slip["available"]
        assert slip["fully_filled"]
        assert slip["slippage_bps"] > 0  # buying costs more than mid


# =============================================================================
# 5. VECTOR STORE — degrades gracefully without chromadb
# =============================================================================

class TestVectorStoreFallback:
    def test_noop_store_when_chromadb_missing(self, monkeypatch):
        # Simulate chromadb being unavailable by stubbing the import
        import sys
        monkeypatch.setitem(sys.modules, "chromadb", None)
        from memory.vector_store import get_vector_store, reset_vector_store

        reset_vector_store()
        store = get_vector_store()
        assert store.available is False
        assert store.count() == 0
        assert store.query_similar("any query") == []


# =============================================================================
# 6. KNOWLEDGE TOOLS — RAG + KPIs over mocked persistence
# =============================================================================

class TestKnowledgeTools:
    @pytest.mark.asyncio
    async def test_kpi_summary_aggregates_evaluations(self):
        from memory.store import MemoryStore
        from tools.knowledge_tools import get_kpi_summary

        store = MemoryStore(agent_id="test-agent")
        # Seed two predictions + two evaluations
        for i, (correct, pnl) in enumerate([(True, 1.5), (False, -0.9)], start=1):
            pid = f"p-{i}"
            await store.save_prediction(
                {
                    "prediction_id": pid,
                    "symbol": "BTC/USDT",
                    "timeframe": "4h",
                    "signal": {
                        "signal": "BUY",
                        "confidence": 80 if correct else 85,
                        "market_regime": "trending",
                        "trend_direction": "bullish",
                        "confluence_indicators": ["EMA", "RSI", "MACD"],
                    },
                }
            )
            await store.save_prediction_evaluation(
                pid,
                {
                    "direction_correct": correct,
                    "tp1_hit": correct,
                    "tp2_hit": False,
                    "sl_hit": not correct,
                    "pnl_pct": pnl,
                    "result_label": "win" if correct else "loss",
                    "timeframe": "4h",
                },
            )

        kpis = await get_kpi_summary(symbol="BTC/USDT", timeframe="4h", window=10)
        assert kpis["available"]
        assert kpis["window"] == 2
        assert kpis["directional_accuracy"] == 0.5
        assert kpis["wins"] == 1 and kpis["losses"] == 1
        assert "trending" in kpis["regime_breakdown"]
        assert "high_80_plus" in kpis["confidence_calibration"]

    @pytest.mark.asyncio
    async def test_get_failure_modes_highlights_overconfident_losses(self):
        from memory.store import MemoryStore
        from tools.knowledge_tools import get_failure_modes

        store = MemoryStore(agent_id="test-agent")
        await store.save_prediction(
            {
                "prediction_id": "p-loser",
                "symbol": "ETH/USDT",
                "timeframe": "1h",
                "signal": {
                    "signal": "BUY",
                    "confidence": 90,
                    "market_regime": "volatile",
                    "trend_direction": "bearish",
                    "confluence_indicators": ["RSI", "MACD"],
                    "reasoning": "high conviction wrong call",
                },
            }
        )
        await store.save_prediction_evaluation(
            "p-loser",
            {
                "direction_correct": False,
                "pnl_pct": -2.0,
                "result_label": "loss",
                "timeframe": "1h",
            },
        )

        result = await get_failure_modes(symbol="ETH/USDT", timeframe="1h")
        assert result["available"]
        assert result["count"] == 1
        oc = result["patterns"]["overconfident_losses"]
        assert oc and oc[0]["confidence"] == 90

    @pytest.mark.asyncio
    async def test_query_similar_setups_falls_back_to_noop(self, monkeypatch):
        # No vector store available → returns hits=[], available=False
        import sys
        monkeypatch.setitem(sys.modules, "chromadb", None)
        from memory.vector_store import reset_vector_store
        from tools.knowledge_tools import query_similar_setups

        reset_vector_store()
        result = await query_similar_setups("any setup", symbol="BTC/USDT", timeframe="4h")
        assert result["available"] is False
        assert result["hits"] == []


# =============================================================================
# 7. MARKET CONTEXT BUILDER
# =============================================================================

class TestMarketContext:
    @pytest.mark.asyncio
    async def test_market_context_aggregates_with_disabled_sources(self, monkeypatch):
        """With no API keys and no ccxt, build_market_context should still return a structured dict."""
        monkeypatch.setenv("NEWS_ENABLED", "false")
        monkeypatch.setenv("LIQUIDITY_ENABLED", "false")
        from config.settings import reset_config
        reset_config()

        # Stub get_fear_greed to avoid network
        from workflows import market_context as mc_module

        async def fake_fg(asset_class="crypto"):
            return {"available": False, "reason": "stub", "source": "test"}

        monkeypatch.setattr(mc_module, "get_fear_greed", fake_fg)

        ctx = await mc_module.build_market_context("BTC/USDT", "4h")
        assert ctx["symbol"] == "BTC/USDT"
        assert ctx["timeframe"] == "4h"
        assert ctx["fear_greed"]["available"] is False
        assert ctx["news"]["article_count"] == 0
        assert ctx["liquidity"]["available"] is False

    @pytest.mark.asyncio
    async def test_format_market_context_text_renders_all_sections(self):
        from workflows.market_context import format_market_context_text

        ctx = {
            "fear_greed": {"available": True, "value": 72, "classification": "greed", "source": "alternative.me", "delta": 5},
            "news_sentiment": {
                "overall_sentiment": "bullish",
                "sentiment_score": 0.4,
                "confidence": 65,
                "method": "llm_anthropic",
                "summary": "Generally bullish on ETF news.",
                "themes": ["ETF", "macro"],
                "bullish_drivers": ["ETF inflows accelerating"],
                "bearish_drivers": [],
            },
            "news": {"missing_keys": []},
            "liquidity": {
                "available": True,
                "exchange": "binance",
                "mid": 50000.0,
                "spread_bps": 1.2,
                "zones": {
                    "imbalance_label": "bullish",
                    "depth_imbalance_1pct": 0.18,
                    "support_walls": [{"price": 49500, "size_x_avg": 8.2}],
                    "resistance_walls": [{"price": 50500, "size_x_avg": 6.1}],
                },
                "slippage_probe": {"size_quote": 50000, "buy_bps": 1.5, "sell_bps": 1.3,
                                   "buy_fully_filled": True, "sell_fully_filled": True},
            },
        }
        text = format_market_context_text(ctx)
        assert "Fear & Greed" in text
        assert "News sentiment" in text
        assert "Liquidity" in text
        assert "imbalance=bullish" in text


# =============================================================================
# 8. KNOWLEDGE INDEXER — wired through save_prediction_evaluation flow
# =============================================================================

class TestKnowledgeIndexer:
    def test_index_evaluation_noop_when_store_unavailable(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "chromadb", None)
        from memory.vector_store import reset_vector_store
        from workflows.knowledge_indexer import index_evaluation

        reset_vector_store()
        rid = index_evaluation(
            {"prediction_id": "p-1", "symbol": "BTC/USDT", "timeframe": "4h", "signal": {"signal": "BUY"}},
            {"direction_correct": True, "pnl_pct": 1.0, "result_label": "win"},
        )
        assert rid is None  # graceful no-op

    def test_format_setup_document_includes_outcome(self):
        from memory.vector_store import format_setup_document

        text = format_setup_document(
            {
                "symbol": "BTC/USDT",
                "timeframe": "4h",
                "signal": {
                    "signal": "BUY",
                    "confidence": 75,
                    "market_regime": "trending",
                    "trend_direction": "bullish",
                    "confluence_indicators": ["EMA", "RSI"],
                },
                "indicators": {
                    "trend": {"ema_alignment": "bullish"},
                    "volume": {"price_vs_vwap": "above"},
                },
            },
            {"direction_correct": True, "tp1_hit": True, "tp2_hit": False, "sl_hit": False, "pnl_pct": 1.5,
             "result_label": "win"},
        )
        assert "signal=BUY" in text
        assert "outcome.dir_correct=True" in text
        assert "trend.ema_alignment=bullish" in text
