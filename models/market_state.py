"""
market_state.py - Core data models for market data and analysis results.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional


@dataclass
class MarketData:
    """Raw collected market data from all sources."""
    symbol: str
    current_price_krw: float
    daily_ohlcv: List[Dict[str, Any]]  # list of dicts with date, open, high, low, close, volume
    global_btc_usd: float
    usd_krw_rate: float
    fear_greed_index: int
    fear_greed_label: str
    collected_at: datetime

    def get_closes(self) -> List[float]:
        """Return list of closing prices (oldest first)."""
        return [candle["close"] for candle in self.daily_ohlcv]

    def get_highs(self) -> List[float]:
        """Return list of high prices."""
        return [candle["high"] for candle in self.daily_ohlcv]

    def get_lows(self) -> List[float]:
        """Return list of low prices."""
        return [candle["low"] for candle in self.daily_ohlcv]

    def get_volumes(self) -> List[float]:
        """Return list of trading volumes."""
        return [candle["volume"] for candle in self.daily_ohlcv]

    def get_opens(self) -> List[float]:
        """Return list of opening prices."""
        return [candle["open"] for candle in self.daily_ohlcv]


@dataclass
class MarketAnalysis:
    """Computed technical indicators and market scenario classification."""
    ma7: float
    ma20: float
    ma60: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    atr_14: float
    rsi_14: float
    historical_volatility_30d: float  # annualized (e.g., 0.80 = 80%)
    kimchi_premium_pct: float
    scenario: str  # BULLISH / BEARISH / SIDEWAYS_NARROW / SIDEWAYS_WIDE / HIGH_RISK
    scenario_confidence: float  # 0.0 ~ 1.0
    analyzed_at: datetime

    # Extended fields
    ma7_trend: str = "NEUTRAL"       # UP / DOWN / NEUTRAL
    ma20_trend: str = "NEUTRAL"
    rsi_signal: str = "NEUTRAL"      # OVERBOUGHT / OVERSOLD / NEUTRAL
    volume_trend: str = "NEUTRAL"    # HIGH / LOW / NORMAL
    bb_width_pct: float = 0.0        # Bollinger Band width as % of mid
    price_vs_ma20_pct: float = 0.0   # % deviation from MA20
