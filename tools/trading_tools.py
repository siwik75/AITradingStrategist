"""
Trading Tools — Technical analysis, market data, backtesting.
Each tool is a standalone function with typed parameters and docstrings
for automatic schema generation (Anthropic tool_use / Strands SDK).
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional
import json
import structlog

log = structlog.get_logger()


# =============================================================================
# MARKET DATA TOOLS
# =============================================================================

async def get_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 200,
    source: str = "synthetic"
) -> dict:
    """
    Fetch OHLCV candle data for a trading pair.
    :param symbol: Trading symbol (e.g., BTC/USDT, AAPL)
    :param timeframe: Candle timeframe (1m, 5m, 15m, 1h, 4h, 1d)
    :param limit: Number of candles to fetch
    :param source: Data source (ccxt, yfinance, synthetic)
    """
    log.info("tool.get_ohlcv", symbol=symbol, timeframe=timeframe, limit=limit)

    if source == "ccxt":
        return await _fetch_ccxt(symbol, timeframe, limit)
    elif source == "yfinance":
        return _fetch_yfinance(symbol, timeframe, limit)
    else:
        return _generate_synthetic(symbol, timeframe, limit)


def _generate_synthetic(symbol: str, timeframe: str, limit: int) -> dict:
    """Generate realistic synthetic OHLCV data for backtesting demos."""
    np.random.seed(42)  # reproducible for demos
    
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    minutes = tf_minutes.get(timeframe, 60)
    
    base_price = {"BTC/USDT": 67500, "ETH/USDT": 3800, "AAPL": 195}.get(symbol, 100)
    volatility = base_price * 0.002  # 0.2% per candle
    
    timestamps = [
        datetime.utcnow() - timedelta(minutes=minutes * (limit - i))
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
        records.append({
            "timestamp": ts.isoformat(),
            "open": round(open_p, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": round(volume, 2)
        })
    
    return {"symbol": symbol, "timeframe": timeframe, "candles": records}


async def _fetch_ccxt(symbol: str, timeframe: str, limit: int) -> dict:
    """Fetch from exchange via ccxt (production)."""
    import ccxt
    exchange = ccxt.binance({"enableRateLimit": True})
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms").dt.strftime("%Y-%m-%dT%H:%M:%S")
    return {"symbol": symbol, "timeframe": timeframe, "candles": df.to_dict("records")}


def _fetch_yfinance(symbol: str, timeframe: str, limit: int) -> dict:
    """Fetch from Yahoo Finance (stocks)."""
    import yfinance as yf
    tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}
    ticker = yf.Ticker(symbol.replace("/", "-"))
    df = ticker.history(period="3mo", interval=tf_map.get(timeframe, "1d"))
    df = df.tail(limit)
    records = []
    for idx, row in df.iterrows():
        records.append({
            "timestamp": idx.strftime("%Y-%m-%dT%H:%M:%S"),
            "open": round(row["Open"], 2),
            "high": round(row["High"], 2),
            "low": round(row["Low"], 2),
            "close": round(row["Close"], 2),
            "volume": round(row["Volume"], 2)
        })
    return {"symbol": symbol, "timeframe": timeframe, "candles": records}


# =============================================================================
# TECHNICAL ANALYSIS TOOLS
# =============================================================================

async def calculate_indicators(
    symbol: str,
    timeframe: str = "1h",
    indicator_set: str = "full"
) -> dict:
    """
    Calculate technical indicators on market data.
    :param symbol: Trading symbol
    :param timeframe: Analysis timeframe
    :param indicator_set: Which indicators to compute (full, trend, momentum, volatility, volume)
    """
    log.info("tool.calculate_indicators", symbol=symbol, timeframe=timeframe, set=indicator_set)
    
    data = await get_ohlcv(symbol, timeframe, limit=200)
    df = pd.DataFrame(data["candles"])
    
    import ta
    
    results = {"symbol": symbol, "timeframe": timeframe, "price": df["close"].iloc[-1]}
    
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
    days: int = 30
) -> dict:
    """
    Run a synthetic backtest on historical data with given strategy parameters.
    :param symbol: Trading symbol to backtest
    :param timeframe: Timeframe for the backtest
    :param strategy_params: JSON string with strategy configuration
    :param days: Number of days to backtest
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
    candles_needed = int(days * 24 * 60 / {"1h": 60, "4h": 240, "1d": 1440}.get(timeframe, 60))
    data = await get_ohlcv(symbol, timeframe, limit=min(candles_needed, 500))
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
    
    # Simulate trades
    trades = []
    in_position = False
    
    for i in range(1, len(df)):
        if in_position:
            # Check exits
            trade = trades[-1]
            current = df.iloc[i]
            
            if trade["direction"] == "LONG":
                if current["low"] <= trade["stop_loss"]:
                    trade["exit_price"] = trade["stop_loss"]
                    trade["exit_reason"] = "stop_loss"
                    trade["pnl_pct"] = (trade["exit_price"] / trade["entry_price"] - 1) * 100
                    in_position = False
                elif current["high"] >= trade["tp2"]:
                    trade["exit_price"] = trade["tp2"]
                    trade["exit_reason"] = "tp2"
                    trade["pnl_pct"] = (trade["exit_price"] / trade["entry_price"] - 1) * 100
                    in_position = False
                elif current["high"] >= trade["tp1"]:
                    trade["exit_price"] = trade["tp1"]
                    trade["exit_reason"] = "tp1"
                    trade["pnl_pct"] = (trade["exit_price"] / trade["entry_price"] - 1) * 100
                    in_position = False
            
            elif trade["direction"] == "SHORT":
                if current["high"] >= trade["stop_loss"]:
                    trade["exit_price"] = trade["stop_loss"]
                    trade["exit_reason"] = "stop_loss"
                    trade["pnl_pct"] = (1 - trade["exit_price"] / trade["entry_price"]) * 100
                    in_position = False
                elif current["low"] <= trade["tp2"]:
                    trade["exit_price"] = trade["tp2"]
                    trade["exit_reason"] = "tp2"
                    trade["pnl_pct"] = (1 - trade["exit_price"] / trade["entry_price"]) * 100
                    in_position = False
                elif current["low"] <= trade["tp1"]:
                    trade["exit_price"] = trade["tp1"]
                    trade["exit_reason"] = "tp1"
                    trade["pnl_pct"] = (1 - trade["exit_price"] / trade["entry_price"]) * 100
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
                "exit_price": None, "exit_reason": None, "pnl_pct": None,
            })
            in_position = True
    
    # Close any open position at last price
    if trades and trades[-1]["exit_price"] is None:
        last_price = df.iloc[-1]["close"]
        trade = trades[-1]
        trade["exit_price"] = round(last_price, 2)
        trade["exit_reason"] = "end_of_data"
        if trade["direction"] == "LONG":
            trade["pnl_pct"] = round((last_price / trade["entry_price"] - 1) * 100, 3)
        else:
            trade["pnl_pct"] = round((1 - last_price / trade["entry_price"]) * 100, 3)
    
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
    
    return {
        "symbol": symbol,
        "timeframe": timeframe,
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


# =============================================================================
# STRATEGY PARAMETER TOOLS
# =============================================================================

async def get_strategy_params() -> dict:
    """
    Retrieve current active strategy parameters.
    In production, reads from Redis/DynamoDB. Here returns defaults.
    """
    return {
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


async def save_strategy_params(params_json: str) -> dict:
    """
    Persist updated strategy parameters after self-assessment.
    :param params_json: JSON string with the new strategy parameters
    """
    params = json.loads(params_json) if isinstance(params_json, str) else params_json
    log.info("tool.save_strategy_params", params=params)
    # In production: write to Redis / DynamoDB / S3
    return {"status": "saved", "params": params}


async def get_trade_history(days: int = 30) -> dict:
    """
    Retrieve past trade signals and their outcomes for self-assessment.
    :param days: Number of days of history to retrieve
    """
    # In production: read from Aurora PostgreSQL / DynamoDB
    # For demo: return synthetic history
    return {
        "trades": [],
        "period_days": days,
        "message": "No historical trades yet — run backtest to generate synthetic history"
    }
