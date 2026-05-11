"""
Trading Tools — Technical analysis, market data, backtesting.
Each tool is a standalone function with typed parameters and docstrings
for automatic schema generation (Anthropic tool_use / Strands SDK).
"""
import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger()


_TIMEFRAME_TO_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
_SUPPORTED_MARKET_DATA_SOURCES = {"default", "synthetic", "ccxt", "yfinance", "auto"}
_YFINANCE_USD_QUOTES = {"USD", "USDT", "USDC", "BUSD"}


# =============================================================================
# MARKET DATA TOOLS
# =============================================================================

async def get_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 200,
    source: str = "default"
) -> dict:
    """
    Fetch OHLCV candle data for a trading pair.
    :param symbol: Trading symbol (e.g., BTC/USDT, AAPL)
    :param timeframe: Candle timeframe (1m, 5m, 15m, 30m, 1h, 4h, 1d)
    :param limit: Number of candles to fetch
    :param source: Data source (default, auto, ccxt, yfinance, synthetic)
    """
    if limit <= 0:
        raise ValueError("limit must be > 0")

    _timeframe_to_minutes(timeframe)
    resolved_source = _resolve_market_data_source(source, symbol)

    log.info(
        "tool.get_ohlcv",
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
        requested_source=source,
        resolved_source=resolved_source,
    )

    try:
        if resolved_source == "ccxt":
            payload = await _fetch_ccxt(symbol, timeframe, limit)
        elif resolved_source == "yfinance":
            payload = _fetch_yfinance(symbol, timeframe, limit)
        else:
            payload = _generate_synthetic(symbol, timeframe, limit)
    except Exception as exc:
        if resolved_source != "synthetic" and _fallback_to_synthetic_enabled():
            log.warning(
                "tool.get_ohlcv.fallback_to_synthetic",
                symbol=symbol,
                timeframe=timeframe,
                requested_source=resolved_source,
                error=str(exc),
            )
            payload = _generate_synthetic(symbol, timeframe, limit)
            resolved_source = "synthetic"
        else:
            raise

    return _finalize_ohlcv_payload(
        payload=payload,
        symbol=symbol,
        timeframe=timeframe,
        source=resolved_source,
        limit=limit,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timeframe_to_minutes(timeframe: str) -> int:
    if timeframe not in _TIMEFRAME_TO_MINUTES:
        supported = ", ".join(sorted(_TIMEFRAME_TO_MINUTES))
        raise ValueError(f"Unsupported timeframe {timeframe!r}. Supported: {supported}")
    return _TIMEFRAME_TO_MINUTES[timeframe]


def _resolve_market_data_source(requested_source: str, symbol: str) -> str:
    from config.settings import get_config

    source = (requested_source or "default").lower()
    if source not in _SUPPORTED_MARKET_DATA_SOURCES:
        supported = ", ".join(sorted(_SUPPORTED_MARKET_DATA_SOURCES))
        raise ValueError(f"Unsupported source {source!r}. Supported: {supported}")

    if source == "default":
        source = get_config().market_data.source

    if source == "auto":
        return "ccxt" if "/" in symbol else "yfinance"

    return source


def _fallback_to_synthetic_enabled() -> bool:
    from config.settings import get_config

    return get_config().market_data.fallback_to_synthetic


def _finalize_ohlcv_payload(
    payload: dict,
    symbol: str,
    timeframe: str,
    source: str,
    limit: int,
) -> dict:
    required_columns = ["timestamp", "open", "high", "low", "close", "volume"]
    df = pd.DataFrame(payload.get("candles", []))

    if df.empty:
        raise ValueError(f"No OHLCV candles returned for {symbol} on {timeframe}")

    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        raise ValueError(f"OHLCV payload missing columns: {', '.join(missing)}")

    df = df[required_columns].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for column in required_columns[1:]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=required_columns).sort_values("timestamp").tail(limit)
    if df.empty:
        raise ValueError(f"OHLCV payload for {symbol} on {timeframe} became empty after cleanup")

    candles = []
    for candle in df.to_dict("records"):
        candles.append(
            {
                "timestamp": candle["timestamp"].isoformat().replace("+00:00", "Z"),
                "open": round(float(candle["open"]), 8),
                "high": round(float(candle["high"]), 8),
                "low": round(float(candle["low"]), 8),
                "close": round(float(candle["close"]), 8),
                "volume": round(float(candle["volume"]), 8),
            }
        )

    return {
        "symbol": payload.get("symbol", symbol),
        "timeframe": payload.get("timeframe", timeframe),
        "source": source,
        "candles": candles,
    }


def _generate_synthetic(symbol: str, timeframe: str, limit: int) -> dict:
    """Generate realistic synthetic OHLCV data for backtesting demos."""
    np.random.seed(42)  # reproducible for demos

    minutes = _timeframe_to_minutes(timeframe)

    base_price = {"BTC/USDT": 67500, "ETH/USDT": 3800, "AAPL": 195}.get(symbol, 100)
    volatility = base_price * 0.002  # 0.2% per candle

    timestamps = [
        _utc_now() - timedelta(minutes=minutes * (limit - i))
        for i in range(limit)
    ]

    prices = [base_price]
    for _ in range(limit - 1):
        change = np.random.normal(0, volatility)
        # Add slight upward trend
        trend = volatility * 0.05
        prices.append(max(prices[-1] + change + trend, base_price * 0.5))

    records = []
    for i, (ts, close) in enumerate(zip(timestamps, prices)):
        high = close * (1 + abs(np.random.normal(0, 0.003)))
        low = close * (1 - abs(np.random.normal(0, 0.003)))
        open_p = low + (high - low) * np.random.random()
        volume = np.random.uniform(100, 10000) * (base_price / 100)
        records.append(
            {
                "timestamp": ts.isoformat(),
                "open": round(open_p, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close, 2),
                "volume": round(volume, 2),
            }
        )

    return {"symbol": symbol, "timeframe": timeframe, "candles": records}


async def _fetch_ccxt(symbol: str, timeframe: str, limit: int) -> dict:
    """Fetch OHLCV candles from an exchange via ccxt async support."""
    from config.settings import get_config

    try:
        import ccxt.async_support as ccxt_async
    except ImportError as exc:
        raise ImportError(
            "ccxt is not installed. Install live-data extras with `pip install -e \".[live-data]\"`."
        ) from exc

    exchange_id = get_config().market_data.ccxt_exchange
    exchange_cls = getattr(ccxt_async, exchange_id, None)
    if exchange_cls is None:
        raise ValueError(f"Unsupported CCXT exchange {exchange_id!r}")

    exchange = exchange_cls({"enableRateLimit": True})
    try:
        if not exchange.has.get("fetchOHLCV"):
            raise ValueError(f"Exchange {exchange_id!r} does not support fetchOHLCV")

        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    finally:
        await exchange.close()

    if not ohlcv:
        raise ValueError(f"CCXT returned no OHLCV candles for {symbol} on {timeframe}")

    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return {"symbol": symbol, "timeframe": timeframe, "candles": df.to_dict("records")}


def _fetch_yfinance(symbol: str, timeframe: str, limit: int) -> dict:
    """Fetch OHLCV candles from Yahoo Finance with interval-aware resampling."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ImportError(
            "yfinance is not installed. Install live-data extras with `pip install -e \".[live-data]\"`."
        ) from exc

    yf_symbol = _normalize_symbol_for_yfinance(symbol)
    interval, resample_rule = _yfinance_request_plan(timeframe)
    start = _utc_now() - timedelta(days=_yfinance_lookback_days(timeframe, limit))
    end = _utc_now()

    ticker = yf.Ticker(yf_symbol)
    df = ticker.history(
        start=start,
        end=end,
        interval=interval,
        auto_adjust=False,
        actions=False,
        prepost=False,
    )

    if df.empty:
        raise ValueError(f"Yahoo Finance returned no OHLCV candles for {symbol} ({yf_symbol})")

    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if resample_rule:
        df = _resample_ohlcv_frame(df, resample_rule)

    df = df.tail(limit)
    if df.empty:
        raise ValueError(f"Yahoo Finance returned insufficient candles for {symbol} on {timeframe}")

    records = []
    for idx, row in df.iterrows():
        ts = pd.Timestamp(idx)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")

        records.append(
            {
                "timestamp": ts.isoformat(),
                "open": row["Open"],
                "high": row["High"],
                "low": row["Low"],
                "close": row["Close"],
                "volume": row["Volume"],
            }
        )

    return {"symbol": symbol, "timeframe": timeframe, "candles": records}


def _normalize_symbol_for_yfinance(symbol: str) -> str:
    if "/" not in symbol:
        return symbol.replace("/", "-").upper()

    base, quote = symbol.split("/", 1)
    base = base.upper()
    quote = quote.upper()
    if quote in _YFINANCE_USD_QUOTES:
        return f"{base}-USD"
    return f"{base}-{quote}"


def _yfinance_request_plan(timeframe: str) -> tuple[str, str | None]:
    plan = {
        "1m": ("1m", None),
        "5m": ("5m", None),
        "15m": ("15m", None),
        "30m": ("15m", "30min"),
        "1h": ("60m", None),
        "4h": ("60m", "4h"),
        "1d": ("1d", None),
    }
    if timeframe not in plan:
        supported = ", ".join(sorted(plan))
        raise ValueError(f"Unsupported yfinance timeframe {timeframe!r}. Supported: {supported}")
    return plan[timeframe]


def _yfinance_lookback_days(timeframe: str, limit: int) -> int:
    days_needed = math.ceil((limit * _timeframe_to_minutes(timeframe)) / 1440)
    if timeframe == "1m":
        return min(7, max(2, days_needed + 1))
    if _timeframe_to_minutes(timeframe) < 1440:
        return min(60, max(7, days_needed * 3))
    return max(30, days_needed * 2)


def _resample_ohlcv_frame(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    frame = df.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, utc=True)
    elif frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")

    resampled = frame.resample(rule, label="right", closed="right").agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    return resampled.dropna(subset=["Open", "High", "Low", "Close"])


# =============================================================================
# TECHNICAL ANALYSIS TOOLS
# =============================================================================

async def calculate_indicators(
    symbol: str,
    timeframe: str = "1h",
    indicator_set: str = "full",
    source: str = "default",
) -> dict:
    """
    Calculate technical indicators on market data.
    :param symbol: Trading symbol
    :param timeframe: Analysis timeframe
    :param indicator_set: Which indicators to compute (full, trend, momentum, volatility, volume)
    :param source: Market data source (default, auto, ccxt, yfinance, synthetic)
    """
    log.info("tool.calculate_indicators", symbol=symbol, timeframe=timeframe, set=indicator_set)
    
    data = await get_ohlcv(symbol, timeframe, limit=200, source=source)
    df = pd.DataFrame(data["candles"])
    
    import ta
    
    results = {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": data.get("source", source),
        "price": df["close"].iloc[-1],
    }
    
    if indicator_set in ("full", "trend"):
        # EMA crossovers
        df["ema_9"] = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
        df["ema_21"] = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
        df["ema_50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
        df["ema_200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()
        
        # MACD
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()
        
        # ADX (trend strength)
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
        df["adx"] = adx.adx()
        df["di_plus"] = adx.adx_pos()
        df["di_minus"] = adx.adx_neg()
        
        # Ichimoku
        ichi = ta.trend.IchimokuIndicator(df["high"], df["low"])
        df["ichimoku_a"] = ichi.ichimoku_a()
        df["ichimoku_b"] = ichi.ichimoku_b()
        
        last = df.iloc[-1]
        results["trend"] = {
            "ema_9": round(last["ema_9"], 2),
            "ema_21": round(last["ema_21"], 2),
            "ema_50": round(last["ema_50"], 2),
            "ema_200": round(last["ema_200"], 2),
            "ema_cross_9_21": "bullish" if last["ema_9"] > last["ema_21"] else "bearish",
            "ema_cross_21_50": "bullish" if last["ema_21"] > last["ema_50"] else "bearish",
            "above_ema200": last["close"] > last["ema_200"],
            "macd": round(last["macd"], 4),
            "macd_signal": round(last["macd_signal"], 4),
            "macd_histogram": round(last["macd_hist"], 4),
            "macd_cross": "bullish" if last["macd"] > last["macd_signal"] else "bearish",
            "adx": round(last["adx"], 2),
            "adx_trend_strength": (
                "strong" if last["adx"] > 25 else "weak"
            ),
            "di_plus": round(last["di_plus"], 2),
            "di_minus": round(last["di_minus"], 2),
        }
    
    if indicator_set in ("full", "momentum"):
        # RSI
        df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
        
        # Stochastic
        stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"])
        df["stoch_k"] = stoch.stoch()
        df["stoch_d"] = stoch.stoch_signal()
        
        # Williams %R
        df["williams_r"] = ta.momentum.WilliamsRIndicator(
            df["high"], df["low"], df["close"]
        ).williams_r()
        
        # CCI
        df["cci"] = ta.trend.CCIIndicator(
            df["high"], df["low"], df["close"]
        ).cci()
        
        last = df.iloc[-1]
        results["momentum"] = {
            "rsi": round(last["rsi"], 2),
            "rsi_zone": (
                "overbought" if last["rsi"] > 70 else
                "oversold" if last["rsi"] < 30 else "neutral"
            ),
            "stoch_k": round(last["stoch_k"], 2),
            "stoch_d": round(last["stoch_d"], 2),
            "stoch_cross": "bullish" if last["stoch_k"] > last["stoch_d"] else "bearish",
            "williams_r": round(last["williams_r"], 2),
            "cci": round(last["cci"], 2),
        }
    
    if indicator_set in ("full", "volatility"):
        # Bollinger Bands
        bb = ta.volatility.BollingerBands(df["close"])
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_width"] = bb.bollinger_wband()
        
        # ATR
        df["atr"] = ta.volatility.AverageTrueRange(
            df["high"], df["low"], df["close"], window=14
        ).average_true_range()
        
        # Keltner Channel
        kc = ta.volatility.KeltnerChannel(df["high"], df["low"], df["close"])
        df["kc_upper"] = kc.keltner_channel_hband()
        df["kc_lower"] = kc.keltner_channel_lband()
        
        last = df.iloc[-1]
        results["volatility"] = {
            "bb_upper": round(last["bb_upper"], 2),
            "bb_lower": round(last["bb_lower"], 2),
            "bb_mid": round(last["bb_mid"], 2),
            "bb_width": round(last["bb_width"], 4),
            "bb_position": round(
                (last["close"] - last["bb_lower"]) /
                (last["bb_upper"] - last["bb_lower"]), 2
            ) if last["bb_upper"] != last["bb_lower"] else 0.5,
            "atr": round(last["atr"], 2),
            "atr_pct": round(last["atr"] / last["close"] * 100, 3),
            "kc_upper": round(last["kc_upper"], 2),
            "kc_lower": round(last["kc_lower"], 2),
            "squeeze": last["bb_lower"] > last["kc_lower"],  # BB inside KC = squeeze
        }
    
    if indicator_set in ("full", "volume"):
        # OBV
        df["obv"] = ta.volume.OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
        
        # VWAP approx
        df["vwap"] = (df["volume"] * (df["high"] + df["low"] + df["close"]) / 3).cumsum() / df["volume"].cumsum()
        
        # Volume SMA
        df["vol_sma_20"] = df["volume"].rolling(20).mean()
        
        last = df.iloc[-1]
        results["volume"] = {
            "current_volume": round(last["volume"], 2),
            "volume_sma_20": round(last["vol_sma_20"], 2),
            "volume_ratio": round(last["volume"] / last["vol_sma_20"], 2) if last["vol_sma_20"] > 0 else 1.0,
            "obv_trend": "rising" if df["obv"].iloc[-1] > df["obv"].iloc[-5] else "falling",
            "vwap": round(last["vwap"], 2),
            "price_vs_vwap": "above" if last["close"] > last["vwap"] else "below",
        }
    
    # Support/Resistance levels
    results["levels"] = _calculate_support_resistance(df)
    
    return results


def _calculate_support_resistance(df: pd.DataFrame, window: int = 20) -> dict:
    """Identify key support and resistance levels using pivot points."""
    recent = df.tail(window)
    pivot = (recent["high"].max() + recent["low"].min() + recent["close"].iloc[-1]) / 3
    r1 = 2 * pivot - recent["low"].min()
    s1 = 2 * pivot - recent["high"].max()
    r2 = pivot + (recent["high"].max() - recent["low"].min())
    s2 = pivot - (recent["high"].max() - recent["low"].min())
    
    return {
        "pivot": round(pivot, 2),
        "resistance_1": round(r1, 2),
        "resistance_2": round(r2, 2),
        "support_1": round(s1, 2),
        "support_2": round(s2, 2),
    }


# =============================================================================
# BACKTESTING TOOLS
# =============================================================================

async def run_backtest(
    symbol: str,
    timeframe: str = "4h",
    strategy_params: str = "{}",
    days: int = 30,
    source: str = "default",
) -> dict:
    """
    Run a synthetic backtest on historical data with given strategy parameters.
    :param symbol: Trading symbol to backtest
    :param timeframe: Timeframe for the backtest
    :param strategy_params: JSON string with strategy configuration
    :param days: Number of days to backtest
    :param source: Market data source (default, auto, ccxt, yfinance, synthetic)
    """
    log.info("tool.run_backtest", symbol=symbol, days=days)
    
    params = json.loads(strategy_params) if isinstance(strategy_params, str) else strategy_params
    
    # Default strategy params
    rsi_oversold = params.get("rsi_oversold", 30)
    rsi_overbought = params.get("rsi_overbought", 70)
    ema_fast = params.get("ema_fast", 9)
    ema_slow = params.get("ema_slow", 21)
    atr_sl_multiplier = params.get("atr_sl_multiplier", 1.5)
    atr_tp1_multiplier = params.get("atr_tp1_multiplier", 2.0)
    atr_tp2_multiplier = params.get("atr_tp2_multiplier", 3.5)
    min_adx = params.get("min_adx", 20)
    use_macd_filter = params.get("use_macd_filter", True)
    use_volume_filter = params.get("use_volume_filter", True)
    
    # Get historical data
    candles_needed = int(days * 24 * 60 / _timeframe_to_minutes(timeframe))
    data = await get_ohlcv(symbol, timeframe, limit=min(candles_needed, 500), source=source)
    df = pd.DataFrame(data["candles"])
    
    import ta
    
    # Calculate indicators
    df["ema_fast"] = ta.trend.EMAIndicator(df["close"], window=ema_fast).ema_indicator()
    df["ema_slow"] = ta.trend.EMAIndicator(df["close"], window=ema_slow).ema_indicator()
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"]).average_true_range()
    adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
    df["adx"] = adx_ind.adx()
    macd_ind = ta.trend.MACD(df["close"])
    df["macd"] = macd_ind.macd()
    df["macd_signal"] = macd_ind.macd_signal()
    df["vol_sma"] = df["volume"].rolling(20).mean()
    
    df = df.dropna().reset_index(drop=True)
    
    # Simulate trades with two-phase partial exit:
    #   Phase 1 (full position): SL = full loss | TP1 = 50% exit + move SL to breakeven
    #   Phase 2 (50% remaining): SL = breakeven | TP2 = 50% exit
    trades = []
    in_position = False

    for i in range(1, len(df)):
        if in_position:
            trade = trades[-1]
            current = df.iloc[i]
            entry = trade["entry_price"]

            if trade["direction"] == "LONG":
                if not trade["tp1_hit"]:
                    # Phase 1: full position in play
                    if current["low"] <= trade["stop_loss"]:
                        trade["exit_price"] = trade["stop_loss"]
                        trade["exit_reason"] = "stop_loss"
                        trade["pnl_pct"] = round(
                            (trade["stop_loss"] / entry - 1) * 100, 3
                        )
                        in_position = False
                    elif current["high"] >= trade["tp2"]:
                        # Both TP1 and TP2 hit in the same candle: model as 50%+50%
                        tp1_pnl = (trade["tp1"] / entry - 1) * 100
                        tp2_pnl = (trade["tp2"] / entry - 1) * 100
                        trade["tp1_exit_pct"] = round(tp1_pnl, 3)
                        trade["exit_price"] = trade["tp2"]
                        trade["exit_reason"] = "tp2"
                        trade["pnl_pct"] = round(0.5 * tp1_pnl + 0.5 * tp2_pnl, 3)
                        in_position = False
                    elif current["high"] >= trade["tp1"]:
                        # TP1 hit: take 50% profit, move SL to breakeven
                        trade["tp1_hit"] = True
                        trade["tp1_exit_pct"] = round((trade["tp1"] / entry - 1) * 100, 3)
                        trade["stop_loss"] = entry  # breakeven
                else:
                    # Phase 2: 50% position, SL at breakeven
                    if current["low"] <= trade["stop_loss"]:  # stop_loss == entry
                        tp1_pnl = trade["tp1_exit_pct"]
                        trade["exit_price"] = trade["stop_loss"]
                        trade["exit_reason"] = "breakeven_sl"
                        trade["pnl_pct"] = round(0.5 * tp1_pnl + 0.5 * 0.0, 3)
                        in_position = False
                    elif current["high"] >= trade["tp2"]:
                        tp1_pnl = trade["tp1_exit_pct"]
                        tp2_pnl = (trade["tp2"] / entry - 1) * 100
                        trade["exit_price"] = trade["tp2"]
                        trade["exit_reason"] = "tp2"
                        trade["pnl_pct"] = round(0.5 * tp1_pnl + 0.5 * tp2_pnl, 3)
                        in_position = False

            elif trade["direction"] == "SHORT":
                if not trade["tp1_hit"]:
                    # Phase 1: full position in play
                    if current["high"] >= trade["stop_loss"]:
                        trade["exit_price"] = trade["stop_loss"]
                        trade["exit_reason"] = "stop_loss"
                        trade["pnl_pct"] = round(
                            (1 - trade["stop_loss"] / entry) * 100, 3
                        )
                        in_position = False
                    elif current["low"] <= trade["tp2"]:
                        tp1_pnl = (1 - trade["tp1"] / entry) * 100
                        tp2_pnl = (1 - trade["tp2"] / entry) * 100
                        trade["tp1_exit_pct"] = round(tp1_pnl, 3)
                        trade["exit_price"] = trade["tp2"]
                        trade["exit_reason"] = "tp2"
                        trade["pnl_pct"] = round(0.5 * tp1_pnl + 0.5 * tp2_pnl, 3)
                        in_position = False
                    elif current["low"] <= trade["tp1"]:
                        trade["tp1_hit"] = True
                        trade["tp1_exit_pct"] = round((1 - trade["tp1"] / entry) * 100, 3)
                        trade["stop_loss"] = entry  # breakeven
                else:
                    # Phase 2: 50% position, SL at breakeven
                    if current["high"] >= trade["stop_loss"]:  # stop_loss == entry
                        tp1_pnl = trade["tp1_exit_pct"]
                        trade["exit_price"] = trade["stop_loss"]
                        trade["exit_reason"] = "breakeven_sl"
                        trade["pnl_pct"] = round(0.5 * tp1_pnl + 0.5 * 0.0, 3)
                        in_position = False
                    elif current["low"] <= trade["tp2"]:
                        tp1_pnl = trade["tp1_exit_pct"]
                        tp2_pnl = (1 - trade["tp2"] / entry) * 100
                        trade["exit_price"] = trade["tp2"]
                        trade["exit_reason"] = "tp2"
                        trade["pnl_pct"] = round(0.5 * tp1_pnl + 0.5 * tp2_pnl, 3)
                        in_position = False
            continue
        
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        atr = curr["atr"]
        
        # Signal conditions
        long_signal = (
            prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
            and curr["rsi"] < rsi_overbought and curr["rsi"] > rsi_oversold
            and (not use_macd_filter or curr["macd"] > curr["macd_signal"])
            and curr["adx"] > min_adx
            and (not use_volume_filter or curr["volume"] > curr["vol_sma"] * 1.1)
        )
        
        short_signal = (
            prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]
            and curr["rsi"] < rsi_overbought and curr["rsi"] > rsi_oversold
            and (not use_macd_filter or curr["macd"] < curr["macd_signal"])
            and curr["adx"] > min_adx
            and (not use_volume_filter or curr["volume"] > curr["vol_sma"] * 1.1)
        )
        
        if long_signal:
            entry = curr["close"]
            trades.append({
                "direction": "LONG",
                "entry_price": round(entry, 2),
                "stop_loss": round(entry - atr * atr_sl_multiplier, 2),
                "tp1": round(entry + atr * atr_tp1_multiplier, 2),
                "tp2": round(entry + atr * atr_tp2_multiplier, 2),
                "entry_idx": i,
                "timestamp": curr["timestamp"],
                "indicators": {
                    "rsi": round(curr["rsi"], 2),
                    "adx": round(curr["adx"], 2),
                    "atr": round(atr, 2),
                },
                "tp1_hit": False,
                "tp1_exit_pct": None,
                "exit_price": None, "exit_reason": None, "pnl_pct": None,
            })
            in_position = True

        elif short_signal:
            entry = curr["close"]
            trades.append({
                "direction": "SHORT",
                "entry_price": round(entry, 2),
                "stop_loss": round(entry + atr * atr_sl_multiplier, 2),
                "tp1": round(entry - atr * atr_tp1_multiplier, 2),
                "tp2": round(entry - atr * atr_tp2_multiplier, 2),
                "entry_idx": i,
                "timestamp": curr["timestamp"],
                "indicators": {
                    "rsi": round(curr["rsi"], 2),
                    "adx": round(curr["adx"], 2),
                    "atr": round(atr, 2),
                },
                "tp1_hit": False,
                "tp1_exit_pct": None,
                "exit_price": None, "exit_reason": None, "pnl_pct": None,
            })
            in_position = True
    
    # Close any open position at last price
    if trades and trades[-1]["exit_price"] is None:
        last_price = df.iloc[-1]["close"]
        trade = trades[-1]
        trade["exit_price"] = round(last_price, 2)
        trade["exit_reason"] = "end_of_data"
        entry = trade["entry_price"]
        if trade["direction"] == "LONG":
            remainder_pnl = (last_price / entry - 1) * 100
            if trade["tp1_hit"] and trade["tp1_exit_pct"] is not None:
                # 50% already took profit at TP1; remainder closes at market
                trade["pnl_pct"] = round(0.5 * trade["tp1_exit_pct"] + 0.5 * remainder_pnl, 3)
            else:
                trade["pnl_pct"] = round(remainder_pnl, 3)
        else:
            remainder_pnl = (1 - last_price / entry) * 100
            if trade["tp1_hit"] and trade["tp1_exit_pct"] is not None:
                trade["pnl_pct"] = round(0.5 * trade["tp1_exit_pct"] + 0.5 * remainder_pnl, 3)
            else:
                trade["pnl_pct"] = round(remainder_pnl, 3)
    
    # Compute metrics
    completed = [t for t in trades if t["exit_price"] is not None]
    
    if not completed:
        return {
            "symbol": symbol, "timeframe": timeframe, "days": days,
            "total_trades": 0, "message": "No trades generated",
            "strategy_params": params
        }
    
    wins = [t for t in completed if t["pnl_pct"] and t["pnl_pct"] > 0]
    losses = [t for t in completed if t["pnl_pct"] and t["pnl_pct"] <= 0]
    pnl_list = [t["pnl_pct"] for t in completed if t["pnl_pct"] is not None]
    
    # Drawdown calculation
    cumulative = np.cumsum(pnl_list)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = cumulative - running_max
    
    # Exit distribution
    exit_dist = {}
    for t in completed:
        reason = t["exit_reason"]
        exit_dist[reason] = exit_dist.get(reason, 0) + 1
    
    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": data.get("source", source),
        "days": days,
        "strategy_params": params,
        "total_trades": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(completed) * 100, 2) if completed else 0,
        "total_pnl_pct": round(sum(pnl_list), 3),
        "avg_win_pct": round(np.mean([t["pnl_pct"] for t in wins]), 3) if wins else 0,
        "avg_loss_pct": round(np.mean([t["pnl_pct"] for t in losses]), 3) if losses else 0,
        "max_drawdown_pct": round(float(drawdown.min()), 3) if len(drawdown) > 0 else 0,
        "profit_factor": round(
            abs(sum(t["pnl_pct"] for t in wins)) /
            abs(sum(t["pnl_pct"] for t in losses))
            if losses and sum(t["pnl_pct"] for t in losses) != 0 else 0,
            2
        ),
        "sharpe_approx": round(
            np.mean(pnl_list) / np.std(pnl_list) * np.sqrt(len(pnl_list))
            if np.std(pnl_list) > 0 else 0,
            2
        ),
        "exit_distribution": exit_dist,
        "trades": completed[:20],  # cap for LLM context size
    }

    # Persist completed trades for self-assessment history
    try:
        from memory.store import get_memory_store
        store = get_memory_store()
        for trade in completed:
            await store.save_trade_signal(
                signal={**trade, "symbol": symbol, "timeframe": timeframe},
                correlation_id="",
            )
    except Exception as exc:
        log.warning("tool.backtest.persist_trades_failed", error=str(exc))

    return result


# =============================================================================
# STRATEGY PARAMETER TOOLS
# =============================================================================

_DEFAULT_STRATEGY_PARAMS = {
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "ema_fast": 9,
    "ema_slow": 21,
    "atr_sl_multiplier": 1.5,
    "atr_tp1_multiplier": 2.0,
    "atr_tp2_multiplier": 3.5,
    "min_adx": 20,
    "use_macd_filter": True,
    "use_volume_filter": True,
}


async def get_strategy_params(timeframe: str | None = None) -> dict:
    """
    Retrieve current active strategy parameters.
    Reads from the local file store (persisted across runs).
    Falls back to defaults if no params have been saved yet.
    :param timeframe: Optional timeframe scope (e.g. 15m, 1h, 4h)
    """
    from memory.store import get_memory_store
    store = get_memory_store()
    saved = await store.get_strategy_params(timeframe=timeframe)
    if saved is not None:
        log.info("tool.get_strategy_params", source="file", timeframe=timeframe)
        return saved
    log.info("tool.get_strategy_params", source="defaults", timeframe=timeframe)
    return dict(_DEFAULT_STRATEGY_PARAMS)


async def save_strategy_params(
    params_json: str,
    timeframe: str | None = None,
) -> dict:
    """
    Persist updated strategy parameters after self-assessment.
    :param params_json: JSON string with the new strategy parameters
    :param timeframe: Optional timeframe scope (e.g. 15m, 1h, 4h)
    """
    params = json.loads(params_json) if isinstance(params_json, str) else params_json
    from memory.store import get_memory_store
    store = get_memory_store()
    await store.save_strategy_params(params, timeframe=timeframe)
    log.info("tool.save_strategy_params", params=params, timeframe=timeframe)
    return {"status": "saved", "params": params, "timeframe": timeframe}


async def get_trade_history(days: int = 30) -> dict:
    """
    Retrieve past trade signals and their outcomes for self-assessment.
    :param days: Number of days of history to retrieve
    """
    from memory.store import get_memory_store
    store = get_memory_store()
    trades = await store.get_trade_history(days=days)
    return {
        "trades": trades,
        "period_days": days,
        "count": len(trades),
    }


async def save_signal_notification(
    signal_json: str,
    symbol: str,
    timeframe: str = "4h",
    correlation_id: str = "",
) -> dict:
    """
    Persist an actionable BUY/SELL signal for manual execution review.
    :param signal_json: JSON string or dict containing the signal payload
    :param symbol: Trading symbol tied to the recommendation
    :param timeframe: Timeframe used for the signal
    :param correlation_id: Correlation ID for tracing
    """
    signal = json.loads(signal_json) if isinstance(signal_json, str) else signal_json
    signal_type = signal.get("signal", "HOLD")
    if signal_type not in {"BUY", "SELL"}:
        return {
            "status": "skipped",
            "reason": f"Signal {signal_type!r} is not actionable for manual execution",
        }

    from memory.store import get_memory_store

    store = get_memory_store()
    record = await store.save_signal_notification(
        signal=signal,
        symbol=symbol,
        timeframe=timeframe,
        correlation_id=correlation_id,
    )
    return {
        "status": "saved",
        "signal_id": record["signal_id"],
        "notification": record,
    }


async def get_signal_notifications(
    days: int = 30,
    status: str = "all",
    limit: int = 20,
) -> dict:
    """
    Retrieve recent actionable signals for manual follow-up.
    :param days: Number of days of history to inspect
    :param status: Filter by all, pending, or reported
    :param limit: Max number of notifications to return
    """
    if status not in {"all", "pending", "reported"}:
        raise ValueError("status must be one of: all, pending, reported")

    from memory.store import get_memory_store

    store = get_memory_store()
    notifications = await store.get_signal_notifications(days=days, status=status, limit=limit)
    return {
        "notifications": notifications,
        "period_days": days,
        "status": status,
        "count": len(notifications),
    }


async def report_manual_trade_outcome(
    signal_id: str,
    outcome: str,
    notes: str = "",
    pnl_pct: float | None = None,
    execution_price: float | None = None,
    exit_price: float | None = None,
    correlation_id: str = "",
) -> dict:
    """
    Record operator feedback for a suggested trade.
    :param signal_id: The saved signal identifier
    :param outcome: won, lost, breakeven, skipped, or cancelled
    :param notes: Free-form operator notes
    :param pnl_pct: Realized percentage PnL, when known
    :param execution_price: Manual execution price, if entered
    :param exit_price: Manual exit price, if entered
    :param correlation_id: Correlation ID for tracing
    """
    allowed_outcomes = {"won", "lost", "breakeven", "skipped", "cancelled"}
    if outcome not in allowed_outcomes:
        allowed = ", ".join(sorted(allowed_outcomes))
        raise ValueError(f"outcome must be one of: {allowed}")

    from memory.store import get_memory_store

    store = get_memory_store()
    review = await store.save_manual_trade_review(
        signal_id=signal_id,
        outcome=outcome,
        notes=notes,
        pnl_pct=pnl_pct,
        execution_price=execution_price,
        exit_price=exit_price,
        correlation_id=correlation_id,
    )
    return {"status": "saved", "review": review}


async def get_manual_trade_reviews(days: int = 30, limit: int = 50) -> dict:
    """
    Retrieve operator-reported outcomes for suggested trades.
    :param days: Number of days of history to inspect
    :param limit: Max number of reviews to return
    """
    from memory.store import get_memory_store

    store = get_memory_store()
    reviews = await store.get_manual_trade_reviews(days=days, limit=limit)
    return {
        "reviews": reviews,
        "period_days": days,
        "count": len(reviews),
    }
