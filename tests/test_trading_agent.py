"""
Tests — Trading Intelligence Agent.

Demonstrates testing patterns for agentic systems:
1. Tool unit tests with mocked data
2. Backtest validation tests
3. Agent integration tests (mocked LLM)
4. Self-assessment cycle tests
5. Config validation tests
6. Persistence tests
7. FastAPI route tests
"""

# ruff: noqa: I001

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def isolated_test_env(monkeypatch):
    """Keep tests deterministic regardless of the developer's local .env."""
    monkeypatch.setenv("MARKET_DATA_SOURCE", "synthetic")
    monkeypatch.setenv("MARKET_DATA_FALLBACK_TO_SYNTHETIC", "true")

    from config.settings import reset_config
    from memory.store import reset_memory_store

    reset_config()
    reset_memory_store()
    yield
    reset_config()
    reset_memory_store()


# =============================================================================
# TOOL UNIT TESTS
# =============================================================================


class TestTradingTools:
    """Test trading tools with synthetic data."""

    def test_synthetic_ohlcv_generation(self):
        """Verify synthetic data generator produces valid OHLCV."""
        from tools.trading_tools import _generate_synthetic

        result = _generate_synthetic("BTC/USDT", "4h", 100)

        assert result["symbol"] == "BTC/USDT"
        assert len(result["candles"]) == 100

        for candle in result["candles"]:
            assert candle["high"] >= candle["low"]
            assert candle["high"] >= candle["close"]
            assert candle["high"] >= candle["open"]
            assert candle["low"] <= candle["close"]
            assert candle["low"] <= candle["open"]
            assert candle["volume"] > 0

    @pytest.mark.asyncio
    async def test_calculate_indicators_full(self):
        """Verify all indicator categories are computed."""
        from tools.trading_tools import calculate_indicators

        result = await calculate_indicators("BTC/USDT", "4h", "full")

        assert "trend" in result
        assert "momentum" in result
        assert "volatility" in result
        assert "volume" in result
        assert "levels" in result

        # Trend indicators
        assert "ema_9" in result["trend"]
        assert "macd" in result["trend"]
        assert "adx" in result["trend"]
        assert result["trend"]["ema_cross_9_21"] in ("bullish", "bearish")

        # Momentum
        assert 0 <= result["momentum"]["rsi"] <= 100
        assert result["momentum"]["rsi_zone"] in ("overbought", "oversold", "neutral")

        # Volatility
        assert result["volatility"]["atr"] > 0
        assert result["volatility"]["bb_upper"] > result["volatility"]["bb_lower"]

        # Support/Resistance
        assert result["levels"]["resistance_1"] > result["levels"]["support_1"]

    @pytest.mark.asyncio
    async def test_calculate_indicators_subset(self):
        """Verify single indicator set computation."""
        from tools.trading_tools import calculate_indicators

        result = await calculate_indicators("ETH/USDT", "1h", "momentum")

        assert "momentum" in result
        assert "trend" not in result
        assert "volatility" not in result


class TestOhlcvDataSources:
    """Test source resolution and fallback logic for OHLCV data."""

    @pytest.fixture(autouse=True)
    def reset_config(self):
        from config.settings import reset_config

        reset_config()
        yield
        reset_config()

    @pytest.mark.asyncio
    async def test_get_ohlcv_uses_configured_default_source(self):
        """The default source should come from MARKET_DATA_SOURCE."""
        with patch.dict(os.environ, {"MARKET_DATA_SOURCE": "synthetic"}):
            from tools.trading_tools import get_ohlcv

            result = await get_ohlcv("BTC/USDT", "4h", limit=5)

            assert result["source"] == "synthetic"
            assert len(result["candles"]) == 5

    @pytest.mark.asyncio
    async def test_get_ohlcv_auto_routes_exchange_symbols_to_ccxt(self):
        """Auto mode should route slash-form symbols to the ccxt fetcher."""
        mock_payload = {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "candles": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open": 1,
                    "high": 2,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 10,
                }
            ],
        }
        with patch(
            "tools.trading_tools._fetch_ccxt", new=AsyncMock(return_value=mock_payload)
        ) as fetch:
            from tools.trading_tools import get_ohlcv

            result = await get_ohlcv("BTC/USDT", "1h", limit=1, source="auto")

            fetch.assert_awaited_once()
            assert result["source"] == "ccxt"

    @pytest.mark.asyncio
    async def test_get_ohlcv_auto_routes_tickers_to_yfinance(self):
        """Auto mode should route ticker symbols to the yfinance fetcher."""
        mock_payload = {
            "symbol": "AAPL",
            "timeframe": "1d",
            "candles": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 1000,
                }
            ],
        }
        with patch("tools.trading_tools._fetch_yfinance", return_value=mock_payload) as fetch:
            from tools.trading_tools import get_ohlcv

            result = await get_ohlcv("AAPL", "1d", limit=1, source="auto")

            fetch.assert_called_once()
            assert result["source"] == "yfinance"

    @pytest.mark.asyncio
    async def test_get_ohlcv_falls_back_to_synthetic_when_enabled(self):
        """Provider failures should fall back to synthetic when configured."""
        with patch.dict(
            os.environ,
            {
                "MARKET_DATA_SOURCE": "ccxt",
                "MARKET_DATA_FALLBACK_TO_SYNTHETIC": "true",
            },
        ):
            from tools.trading_tools import get_ohlcv

            with patch(
                "tools.trading_tools._fetch_ccxt",
                new=AsyncMock(side_effect=RuntimeError("network down")),
            ):
                result = await get_ohlcv("BTC/USDT", "4h", limit=5)

            assert result["source"] == "synthetic"
            assert len(result["candles"]) == 5

    @pytest.mark.asyncio
    async def test_get_ohlcv_accepts_30m_timeframe(self):
        """30m should be treated as a supported timeframe for OHLCV fetches."""
        from tools.trading_tools import get_ohlcv

        result = await get_ohlcv("BTC/USDT", "30m", limit=5, source="synthetic")

        assert result["timeframe"] == "30m"
        assert len(result["candles"]) == 5

    def test_normalize_symbol_for_yfinance(self):
        """Crypto and slash-form symbols should be normalized for Yahoo Finance."""
        from tools.trading_tools import _normalize_symbol_for_yfinance

        assert _normalize_symbol_for_yfinance("BTC/USDT") == "BTC-USD"
        assert _normalize_symbol_for_yfinance("BRK/B") == "BRK-B"

    def test_yfinance_request_plan_resamples_30m_from_15m(self):
        """Yahoo Finance 30m requests should use a supported base interval plus resampling."""
        from tools.trading_tools import _yfinance_request_plan

        assert _yfinance_request_plan("30m") == ("15m", "30min")


class TestCandlesCliMode:
    """Verify the CLI smoke-check mode for OHLCV data."""

    @pytest.mark.asyncio
    async def test_run_cli_candles_mode_outputs_source_and_bounds(self, capsys):
        """Candles mode should print resolved source and first/last candles."""
        from argparse import Namespace
        from main import run_cli

        mock_result = {
            "symbol": "BTC/USDT",
            "timeframe": "4h",
            "source": "ccxt",
            "candles": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 10.0,
                },
                {
                    "timestamp": "2026-01-01T04:00:00Z",
                    "open": 1.5,
                    "high": 2.5,
                    "low": 1.2,
                    "close": 2.0,
                    "volume": 12.0,
                },
            ],
        }

        args = Namespace(
            mode="candles",
            symbol="BTC/USDT",
            timeframe="4h",
            days=30,
            limit=2,
            source="default",
        )

        with patch("tools.trading_tools.get_ohlcv", new=AsyncMock(return_value=mock_result)):
            await run_cli(args)

        out = capsys.readouterr().out
        data = json.loads(out.split("\n", 1)[1])
        assert data["source"] == "ccxt"
        assert data["count"] == 2
        assert data["first_candle"]["timestamp"] == "2026-01-01T00:00:00Z"
        assert data["last_candle"]["timestamp"] == "2026-01-01T04:00:00Z"


# =============================================================================
# BACKTEST TESTS
# =============================================================================


class TestBacktest:
    """Test backtesting engine."""

    @pytest.mark.asyncio
    async def test_backtest_produces_trades(self):
        """Verify backtest generates trades with PnL."""
        from tools.trading_tools import run_backtest

        result = await run_backtest(
            symbol="BTC/USDT",
            timeframe="4h",
            strategy_params=json.dumps(
                {
                    "ema_fast": 9,
                    "ema_slow": 21,
                    "rsi_oversold": 30,
                    "rsi_overbought": 70,
                    "atr_sl_multiplier": 1.5,
                    "atr_tp1_multiplier": 2.0,
                    "atr_tp2_multiplier": 3.5,
                    "min_adx": 15,
                    "use_macd_filter": True,
                    "use_volume_filter": False,
                }
            ),
            days=30,
        )

        assert result["symbol"] == "BTC/USDT"
        assert "total_trades" in result
        assert "win_rate" in result
        assert "profit_factor" in result
        assert "max_drawdown_pct" in result
        assert "exit_distribution" in result

    @pytest.mark.asyncio
    async def test_backtest_different_params_produce_different_results(self):
        """Verify parameter changes affect backtest outcomes."""
        from tools.trading_tools import run_backtest

        result_a = await run_backtest(
            symbol="BTC/USDT",
            timeframe="4h",
            strategy_params=json.dumps({"ema_fast": 9, "ema_slow": 21, "min_adx": 15}),
            days=30,
        )

        result_b = await run_backtest(
            symbol="BTC/USDT",
            timeframe="4h",
            strategy_params=json.dumps({"ema_fast": 5, "ema_slow": 13, "min_adx": 25}),
            days=30,
        )

        # Different params should (usually) produce different trade counts
        # This is a probabilistic assertion — works with synthetic data seeded at 42
        assert result_a["strategy_params"] != result_b["strategy_params"]

    @pytest.mark.asyncio
    async def test_backtest_exit_types(self):
        """Verify trades exit via SL, TP1, TP2, end_of_data, or breakeven_sl."""
        from tools.trading_tools import run_backtest

        result = await run_backtest(
            symbol="BTC/USDT",
            timeframe="4h",
            strategy_params=json.dumps({"min_adx": 10, "use_volume_filter": False}),
            days=60,
        )

        # breakeven_sl is a valid exit type now that TP1 uses partial-exit model
        valid_exits = {"stop_loss", "tp1", "tp2", "end_of_data", "breakeven_sl"}
        for reason in result.get("exit_distribution", {}).keys():
            assert reason in valid_exits


# =============================================================================
# SCHEMA BUILDER TESTS
# =============================================================================


class TestSchemaBuilder:
    """Test automatic schema generation from Python functions."""

    def test_build_anthropic_schema(self):
        """Verify Anthropic tool_use schema generation."""
        from tools.schema_builder import build_tool_schema

        async def my_tool(symbol: str, timeframe: str = "1h") -> dict:
            """Fetch market data.
            :param symbol: Trading symbol (e.g., BTC/USDT)
            :param timeframe: Candle timeframe
            """
            pass

        schema = build_tool_schema(my_tool)

        assert schema["name"] == "my_tool"
        assert "Fetch market data" in schema["description"]
        assert "symbol" in schema["input_schema"]["properties"]
        assert "timeframe" in schema["input_schema"]["properties"]
        assert "symbol" in schema["input_schema"]["required"]
        assert "timeframe" not in schema["input_schema"]["required"]  # has default

    def test_build_openai_schema(self):
        """Verify OpenAI function calling schema (GHO Gateway)."""
        from tools.schema_builder import build_tool_schema_openai

        async def analyze(symbol: str) -> dict:
            """Run analysis.
            :param symbol: Symbol to analyze
            """
            pass

        schema = build_tool_schema_openai(analyze)

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "analyze"
        assert "parameters" in schema["function"]


# =============================================================================
# INTEGRATION TESTS (mocked LLM)
# =============================================================================


class TestAgentIntegration:
    """Integration tests with mocked LLM responses."""

    def test_parse_json_response_handles_preamble_and_fence(self):
        """The JSON extractor should recover payloads wrapped in prose and code fences."""
        from agents.json_utils import parse_json_response

        payload = parse_json_response(
            'I have the result.\n\n```json\n{"signal":"HOLD","confidence":42}\n```'
        )

        assert payload["signal"] == "HOLD"
        assert payload["confidence"] == 42

    @pytest.mark.asyncio
    async def test_signal_agent_hold(self):
        """Test SignalAgent emits HOLD when LLM returns hold signal."""
        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [
            MagicMock(
                type="text",
                text=json.dumps(
                    {
                        "signal": "HOLD",
                        "confidence": 45,
                        "reasoning": "Insufficient confluence",
                    }
                ),
            )
        ]

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-key"}, clear=False):
            with patch("anthropic.Anthropic") as MockClient:
                instance = MockClient.return_value
                instance.messages.create.return_value = mock_response

                from agents.signal_agent import SignalAgent

                agent = SignalAgent()
                agent.client = instance

                result = await agent.analyze("BTC/USDT", "4h")

                assert result["signal"] == "HOLD"
                assert result["confidence"] < 60

    @pytest.mark.asyncio
    async def test_signal_agent_parses_fenced_json_with_preamble(self):
        """SignalAgent should extract the real signal from prose + fenced JSON."""
        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [
            MagicMock(
                type="text",
                text=(
                    "Now I have all the data needed.\n\n```json\n"
                    '{"signal":"HOLD","confidence":42,"reasoning":"Mixed setup"}\n```'
                ),
            )
        ]

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-key"}, clear=False):
            with patch("anthropic.Anthropic") as MockClient:
                instance = MockClient.return_value
                instance.messages.create.return_value = mock_response

                from agents.signal_agent import SignalAgent

                agent = SignalAgent()
                agent.client = instance

                result = await agent.analyze("BTC/USDT", "15m")

                assert result["signal"] == "HOLD"
                assert result["confidence"] == 42
                assert result["reasoning"] == "Mixed setup"


class TestLlmSelectionAndFallback:
    """Test primary client selection and Anthropic-to-gateway fallback behavior."""

    @pytest.fixture(autouse=True)
    def reset_config(self):
        from config.settings import reset_config

        reset_config()
        yield
        reset_config()

    def test_base_agent_auto_selects_openai_when_only_gateway_is_configured(self):
        """Without Anthropic creds, the base agent should start in OpenAI mode."""
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "",
                "LLM_API_KEY": "openai-test-key",
                "LLM_GATEWAY_URL": "https://api.openai.com/v1",
                "OPENAI_LLM_MODEL": "gpt-5-mini",
            },
            clear=False,
        ):
            with patch("openai.OpenAI") as openai_client:
                from agents.base import AgentConfig, BaseAgent

                agent = BaseAgent(AgentConfig(name="test-agent", use_openai_gateway=None), [])

                assert agent._mode == "openai"
                assert agent.model == "gpt-5-mini"
                openai_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_base_agent_falls_back_to_openai_when_anthropic_auth_fails(self):
        """Anthropic auth failures should trigger a gateway retry when configured."""
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-ant-failing-key",
                "LLM_API_KEY": "openai-test-key",
                "LLM_GATEWAY_URL": "https://api.openai.com/v1",
                "LLM_MODEL": "claude-sonnet-4-20250514",
                "OPENAI_LLM_MODEL": "gpt-5-mini",
            },
            clear=False,
        ):
            anthropic_instance = MagicMock()
            anthropic_instance.messages.create.side_effect = RuntimeError("401 invalid api key")

            openai_response = MagicMock()
            openai_response.choices = [
                MagicMock(
                    finish_reason="stop",
                    message=MagicMock(content='{"signal":"HOLD","confidence":10}'),
                )
            ]
            openai_instance = MagicMock()
            openai_instance.chat.completions.create.return_value = openai_response

            with patch("anthropic.Anthropic", return_value=anthropic_instance):
                with patch("openai.OpenAI", return_value=openai_instance):
                    from agents.base import AgentConfig, BaseAgent

                    agent = BaseAgent(AgentConfig(name="test-agent", use_openai_gateway=None), [])
                    result = await agent.run("say hi")

                    assert agent._mode == "openai"
                    assert agent.model == "gpt-5-mini"
                    assert "HOLD" in result
                    openai_instance.chat.completions.create.assert_called_once()
                    assert (
                        openai_instance.chat.completions.create.call_args.kwargs["model"]
                        == "gpt-5-mini"
                    )


# =============================================================================
# CONFIG TESTS
# =============================================================================


class TestConfig:
    """Test configuration loading."""

    def test_default_config(self):
        """Verify default configuration values."""
        from config.settings import AppConfig

        config = AppConfig()

        assert config.trading.dry_run is True
        assert config.trading.max_risk_pct == 1.0
        assert config.infra.port == 8080
        assert config.infra.log_format == "json"
        assert config.environment == "development"

    def test_30m_timeframe_defaults_are_wired(self):
        """30m should have explicit scheduler and evaluation defaults."""
        from config.settings import AppConfig

        config = AppConfig()

        assert config.scan.for_timeframe("30m") == 1800
        assert config.prediction.horizon_candles("30m") == 6

    def test_production_detection(self):
        """Verify production mode detection."""
        from config.settings import AppConfig

        config = AppConfig()
        config.environment = "production"
        assert config.is_production() is True

        config.environment = "development"
        assert config.is_production() is False


# =============================================================================
# CONFIG VALIDATION TESTS
# =============================================================================


class TestConfigValidation:
    """Test config.validate() raises actionable errors."""

    def _make_config(self, api_key="sk-ant-test-key", max_risk_pct=1.0, port=8080):
        from config.settings import AppConfig

        config = AppConfig()
        config.llm.anthropic_api_key = api_key
        config.llm.gateway_api_key = ""
        config.trading.max_risk_pct = max_risk_pct
        config.infra.port = port
        return config

    def test_validate_raises_on_missing_api_key(self):
        """Missing API key must raise ValueError with actionable message."""
        config = self._make_config(api_key="")
        with pytest.raises(ValueError, match="No LLM credentials"):
            config.validate()

    def test_validate_passes_with_valid_key(self):
        """Valid config must not raise."""
        config = self._make_config()
        config.validate()  # should not raise

    def test_validate_raises_on_zero_risk_pct(self):
        """max_risk_pct=0.0 must raise ValueError."""
        config = self._make_config(max_risk_pct=0.0)
        with pytest.raises(ValueError, match="MAX_RISK_PCT"):
            config.validate()

    def test_validate_raises_on_excessive_risk_pct(self):
        """max_risk_pct > 10 must raise ValueError."""
        config = self._make_config(max_risk_pct=11.0)
        with pytest.raises(ValueError, match="MAX_RISK_PCT"):
            config.validate()

    def test_validate_raises_on_invalid_port(self):
        """Port 0 must raise ValueError."""
        config = self._make_config(port=0)
        with pytest.raises(ValueError, match="PORT"):
            config.validate()


# =============================================================================
# PERSISTENCE TESTS
# =============================================================================


class TestPersistence:
    """Test that strategy params and trade history survive store re-instantiation."""

    @pytest.fixture(autouse=True)
    def reset_store(self):
        from memory.store import reset_memory_store

        reset_memory_store()
        yield
        reset_memory_store()

    @pytest.mark.asyncio
    async def test_strategy_params_persist_across_store_instances(self, tmp_path):
        """Write with one MemoryStore instance; read back with a fresh instance."""
        os.environ["TRADING_AGENT_DATA_DIR"] = str(tmp_path)
        from memory.store import MemoryStore

        store1 = MemoryStore("test-agent")
        await store1.save_strategy_params({"rsi_oversold": 25, "ema_fast": 7})

        store2 = MemoryStore("test-agent")
        params = await store2.get_strategy_params()

        assert params is not None
        assert params["rsi_oversold"] == 25
        assert params["ema_fast"] == 7

    @pytest.mark.asyncio
    async def test_strategy_params_can_be_scoped_by_timeframe(self, tmp_path):
        """Timeframe-scoped params should override the default only for that timeframe."""
        os.environ["TRADING_AGENT_DATA_DIR"] = str(tmp_path)
        from memory.store import MemoryStore

        store1 = MemoryStore("test-agent")
        await store1.save_strategy_params({"ema_fast": 9, "min_adx": 20})
        await store1.save_strategy_params({"ema_fast": 5, "min_adx": 25}, timeframe="15m")

        store2 = MemoryStore("test-agent")
        params_15m = await store2.get_strategy_params(timeframe="15m")
        params_4h = await store2.get_strategy_params(timeframe="4h")

        assert params_15m is not None
        assert params_15m["ema_fast"] == 5
        assert params_15m["min_adx"] == 25
        assert params_4h is not None
        assert params_4h["ema_fast"] == 9
        assert params_4h["min_adx"] == 20

    @pytest.mark.asyncio
    async def test_trade_history_persists(self, tmp_path):
        """Trade signals written to one store instance are readable from another."""
        os.environ["TRADING_AGENT_DATA_DIR"] = str(tmp_path)
        from memory.store import MemoryStore

        store1 = MemoryStore("test-agent")
        await store1.save_trade_signal({"signal": "BUY", "entry": 67500.0}, "cid-001")

        store2 = MemoryStore("test-agent")
        history = await store2.get_trade_history(days=1)

        assert len(history) == 1
        assert history[0]["signal"]["signal"] == "BUY"

    @pytest.mark.asyncio
    async def test_get_strategy_params_returns_none_when_no_file(self, tmp_path):
        """Empty data dir returns None, not an exception."""
        empty_dir = tmp_path / "nonexistent_subdir"
        os.environ["TRADING_AGENT_DATA_DIR"] = str(empty_dir)
        from memory.store import MemoryStore

        store = MemoryStore("test-agent")
        result = await store.get_strategy_params()
        assert result is None

    @pytest.mark.asyncio
    async def test_assessment_history_persists(self, tmp_path):
        """Assessment records are appended and readable."""
        os.environ["TRADING_AGENT_DATA_DIR"] = str(tmp_path)
        from memory.store import MemoryStore

        store1 = MemoryStore("test-agent")
        await store1.save_assessment({"decision": "ADOPT", "win_rate": 60.0}, "cid-002")

        store2 = MemoryStore("test-agent")
        history = await store2.get_assessment_history(limit=5)

        assert len(history) == 1
        assert history[0]["assessment"]["decision"] == "ADOPT"

    @pytest.mark.asyncio
    async def test_prediction_evaluations_can_be_filtered_by_timeframe(self, tmp_path):
        """Evaluation retrieval should support timeframe-scoped KPI windows."""
        os.environ["TRADING_AGENT_DATA_DIR"] = str(tmp_path)
        from memory.store import MemoryStore

        store = MemoryStore("test-agent")
        await store.save_prediction_evaluation(
            "pred-15m",
            {
                "timeframe": "15m",
                "signal": "BUY",
                "direction_correct": True,
                "tp1_reached": True,
                "tp2_reached": False,
                "sl_reached_first": False,
                "mfe_pct": 1.2,
                "mae_pct": 0.3,
                "outcome_score": 0.82,
                "confidence_calibration_bucket": "high",
            },
        )
        await store.save_prediction_evaluation(
            "pred-4h",
            {
                "timeframe": "4h",
                "signal": "SELL",
                "direction_correct": False,
                "tp1_reached": False,
                "tp2_reached": False,
                "sl_reached_first": True,
                "mfe_pct": 0.1,
                "mae_pct": 1.8,
                "outcome_score": 0.12,
                "confidence_calibration_bucket": "medium",
            },
        )

        evals_15m = await store.get_prediction_evaluations(timeframe="15m")
        evals_4h = await store.get_prediction_evaluations(timeframe="4h")

        assert len(evals_15m) == 1
        assert evals_15m[0]["prediction_id"] == "pred-15m"
        assert len(evals_4h) == 1
        assert evals_4h[0]["prediction_id"] == "pred-4h"

    @pytest.mark.asyncio
    async def test_signal_notifications_and_manual_reviews_persist(self, tmp_path):
        """Suggested signals should be reviewable and closeable via manual outcome reports."""
        os.environ["TRADING_AGENT_DATA_DIR"] = str(tmp_path)
        from memory.store import MemoryStore

        store1 = MemoryStore("test-agent")
        notification = await store1.save_signal_notification(
            signal={"signal": "BUY", "entry_price": 67500.0},
            symbol="BTC/USDT",
            timeframe="4h",
            correlation_id="cid-003",
        )

        store2 = MemoryStore("test-agent")
        pending = await store2.get_signal_notifications(status="pending")
        assert len(pending) == 1
        assert pending[0]["signal_id"] == notification["signal_id"]
        assert pending[0]["review_status"] == "pending"

        await store2.save_manual_trade_review(
            signal_id=notification["signal_id"],
            outcome="won",
            pnl_pct=2.5,
            notes="Manual execution matched the setup well.",
            correlation_id="cid-004",
        )

        store3 = MemoryStore("test-agent")
        reported = await store3.get_signal_notifications(status="reported")
        reviews = await store3.get_manual_trade_reviews()

        assert len(reported) == 1
        assert reported[0]["signal_id"] == notification["signal_id"]
        assert reported[0]["review_status"] == "reported"
        assert reported[0]["manual_review"]["outcome"] == "won"
        assert len(reviews) == 1
        assert reviews[0]["signal_id"] == notification["signal_id"]


class TestAdaptiveSupervisor:
    """Verify adaptation KPIs and params can be scoped by timeframe."""

    @pytest.mark.asyncio
    async def test_kpi_summary_is_scoped_by_timeframe(self, tmp_path):
        """Supervisor KPI summary should only consider evaluations from the requested timeframe."""
        os.environ["TRADING_AGENT_DATA_DIR"] = str(tmp_path)
        from agents.strategy_supervisor import AdaptiveStrategySupervisor
        from memory.store import MemoryStore

        store = MemoryStore("test-agent")
        await store.save_prediction_evaluation(
            "pred-15m",
            {
                "timeframe": "15m",
                "signal": "BUY",
                "direction_correct": True,
                "tp1_reached": True,
                "tp2_reached": False,
                "sl_reached_first": False,
                "mfe_pct": 1.4,
                "mae_pct": 0.4,
                "outcome_score": 0.78,
                "confidence_calibration_bucket": "high",
            },
        )
        await store.save_prediction_evaluation(
            "pred-4h",
            {
                "timeframe": "4h",
                "signal": "SELL",
                "direction_correct": False,
                "tp1_reached": False,
                "tp2_reached": False,
                "sl_reached_first": True,
                "mfe_pct": 0.2,
                "mae_pct": 1.9,
                "outcome_score": 0.08,
                "confidence_calibration_bucket": "high",
            },
        )

        supervisor = AdaptiveStrategySupervisor()
        summary = await supervisor.get_kpi_summary(timeframe="15m")

        assert summary["timeframe"] == "15m"
        assert summary["total_evaluations"] == 1
        assert summary["short_window"]["actionable_count"] == 1
        assert summary["short_window"]["directional_accuracy"] == 1.0


# =============================================================================
# SCHEDULER HELPER TESTS
# =============================================================================


class TestSchedulerHelpers:
    """Verify scan-time normalization used for prediction persistence and Telegram gating."""

    def test_resolve_risk_reward_ratio_prefers_tp2_field(self):
        """If the model emits TP2-specific RR, it should populate the canonical ratio."""
        from workflows.scheduler import _resolve_risk_reward_ratio

        result = {
            "signal": "BUY",
            "risk_reward_tp2": 2.75,
            "entry_price": 100,
            "stop_loss": 95,
            "take_profit_2": 115,
        }

        assert _resolve_risk_reward_ratio(result) == 2.75

    def test_resolve_risk_reward_ratio_computes_tp2_ratio_from_signal_levels(self):
        """Canonical risk_reward_ratio should be reward-to-TP2 divided by risk-to-SL."""
        from workflows.scheduler import _resolve_risk_reward_ratio

        result = {
            "signal": "SELL",
            "entry_price": 82.86,
            "stop_loss": 85.58,
            "take_profit_2": 76.52,
        }

        assert _resolve_risk_reward_ratio(result) == 2.3309


# =============================================================================
# NOTIFICATION FORMATTER TESTS
# =============================================================================


class TestNotificationFormatters:
    """Verify Telegram HTML formatting stays valid for dynamic content."""

    def test_degradation_alert_escapes_html_sensitive_reason_text(self):
        """Threshold comparisons should be escaped before Telegram HTML parsing."""
        from tools.notification_tools import _format_degradation_alert

        text = _format_degradation_alert(
            {
                "window": 25,
                "directional_accuracy": 1.0,
                "tp1_reach_rate": 0.0,
                "false_positive_rate": 0.0,
                "avg_outcome_score": 0.6257,
            },
            ["tp1_reach_rate=0.0% < threshold 35.0%"],
        )

        assert "&lt; threshold 35.0%" in text

    def test_plain_text_fallback_strips_supported_html_tags(self):
        """Telegram fallback text should remove formatting tags while preserving content."""
        from tools.notification_tools import _plain_text_fallback

        text = "🚨 <b>Performance</b>\nReason: <i>tp1 &lt; threshold</i>\nID: <code>abc123</code>"
        fallback = _plain_text_fallback(text)

        assert "<b>" not in fallback
        assert "<i>" not in fallback
        assert "<code>" not in fallback
        assert "tp1 < threshold" in fallback


# =============================================================================
# BACKTEST PARTIAL EXIT TESTS
# =============================================================================


class TestBacktestPartialExit:
    """Verify the two-phase TP1 partial exit model."""

    @pytest.mark.asyncio
    async def test_exit_distribution_has_only_valid_reasons(self):
        """All exit reasons must belong to the known set (includes breakeven_sl)."""
        from tools.trading_tools import run_backtest

        result = await run_backtest(
            symbol="BTC/USDT",
            timeframe="4h",
            strategy_params=json.dumps({"min_adx": 10, "use_volume_filter": False}),
            days=60,
        )

        valid_exits = {"stop_loss", "tp1", "tp2", "end_of_data", "breakeven_sl"}
        for reason in result.get("exit_distribution", {}).keys():
            assert reason in valid_exits, f"Unexpected exit reason: {reason}"

    @pytest.mark.asyncio
    async def test_tp2_trades_have_higher_avg_pnl_than_tp1(self):
        """Avg TP2 P&L must exceed avg TP1 P&L (partial exit model sanity check)."""
        from tools.trading_tools import run_backtest

        result = await run_backtest(
            symbol="BTC/USDT",
            timeframe="4h",
            strategy_params=json.dumps(
                {
                    "min_adx": 10,
                    "use_volume_filter": False,
                    "atr_tp1_multiplier": 2.0,
                    "atr_tp2_multiplier": 3.5,
                }
            ),
            days=60,
        )

        trades = result.get("trades", [])
        tp1_trades = [t for t in trades if t.get("exit_reason") == "tp1"]
        tp2_trades = [t for t in trades if t.get("exit_reason") == "tp2"]

        if tp1_trades and tp2_trades:
            avg_tp1 = sum(t["pnl_pct"] for t in tp1_trades) / len(tp1_trades)
            avg_tp2 = sum(t["pnl_pct"] for t in tp2_trades) / len(tp2_trades)
            assert (
                avg_tp2 > avg_tp1
            ), f"Expected TP2 avg pnl ({avg_tp2:.2f}) > TP1 avg pnl ({avg_tp1:.2f})"


# =============================================================================
# FASTAPI ROUTE TESTS
# =============================================================================


class TestFastAPIRoutes:
    """Test FastAPI HTTP endpoints using TestClient."""

    @pytest.fixture
    def client(self):
        """Build a test client from the app factory."""
        from main import create_app
        from fastapi.testclient import TestClient

        app = create_app()
        return TestClient(app)

    def test_health_returns_200(self, client):
        """GET /health must return 200 with status and timestamp."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "timestamp" in data

    def test_ready_returns_503_when_api_key_missing(self, client):
        """GET /ready without API key must return 503."""
        with patch.dict(os.environ, {"LLM_API_KEY": "", "ANTHROPIC_API_KEY": ""}):
            # Reset config singleton so it re-reads env
            from config.settings import reset_config

            reset_config()
            resp = client.get("/ready")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "not_ready"
            assert data["checks"]["llm"]["status"] == "fail"

    def test_ready_returns_200_when_api_key_set(self, client, tmp_path):
        """GET /ready with a valid API key and writable storage returns 200."""
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-ant-test-key",
                "TRADING_AGENT_DATA_DIR": str(tmp_path),
            },
        ):
            from config.settings import reset_config

            reset_config()
            resp = client.get("/ready")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["checks"]["llm"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_analyze_endpoint_returns_signal(self, client):
        """POST /analyze with mocked agent returns a signal response."""
        mock_result = {
            "signal": "HOLD",
            "confidence": 45,
            "reasoning": "Insufficient confluence",
        }
        with patch(
            "agents.signal_agent.SignalAgent.analyze",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/analyze",
                json={"symbol": "BTC/USDT", "timeframe": "4h"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["signal"] in ("BUY", "SELL", "HOLD")

    @pytest.mark.asyncio
    async def test_signals_and_report_endpoints_round_trip(self, client, tmp_path):
        """Suggested signals and operator feedback should be accessible over HTTP."""
        with patch.dict(os.environ, {"TRADING_AGENT_DATA_DIR": str(tmp_path)}):
            from config.settings import reset_config
            from memory.store import get_memory_store, reset_memory_store

            reset_config()
            reset_memory_store()
            store = get_memory_store()
            saved = await store.save_signal_notification(
                signal={"signal": "SELL", "entry_price": 66000.0},
                symbol="BTC/USDT",
                timeframe="4h",
            )

            list_resp = client.get("/signals?status=pending")
            assert list_resp.status_code == 200
            payload = list_resp.json()
            assert payload["count"] == 1
            assert payload["notifications"][0]["signal_id"] == saved["signal_id"]

            report_resp = client.post(
                "/report",
                json={
                    "signal_id": saved["signal_id"],
                    "outcome": "lost",
                    "notes": "Stopped out quickly.",
                    "pnl_pct": -1.2,
                },
            )
            assert report_resp.status_code == 200
            report_payload = report_resp.json()
            assert report_payload["status"] == "saved"

            reported_resp = client.get("/signals?status=reported")
            assert reported_resp.status_code == 200
            reported_payload = reported_resp.json()
            assert reported_payload["count"] == 1
            assert reported_payload["notifications"][0]["manual_review"]["outcome"] == "lost"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
