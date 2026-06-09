"""
① 데이터 수집 에이전트 (DataCollectorAgent)
----------------------------------------------
역할: 해외시장(글로벌 BTC), 국내시장(빗썸 BTC_KRW), 매크로(공포탐욕지수)를 수집.
원칙: 네트워크 실패 시 합성 데이터로 폴백하여 파이프라인이 오프라인에서도
      end-to-end 동작하도록 보장한다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests

from config import (
    SYMBOL, COINGECKO_BASE_URL, FEAR_GREED_URL, MAX_RETRIES, BACKOFF_FACTOR,
)
from models.market_state import MarketData
from utils import bithumb_api

logger = logging.getLogger("data_collector")


def _safe_get(url: str, params: Optional[dict] = None) -> Optional[dict]:
    import time
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_FACTOR ** attempt)
    return None


class DataCollectorAgent:
    """월간 파이프라인 1단계: 모든 외부 데이터를 모아 MarketData로 반환."""

    name = "① 데이터수집"

    def __init__(self, use_synthetic_fallback: bool = True):
        self.use_synthetic_fallback = use_synthetic_fallback

    # ------------------------------------------------------------------
    # 국내 시장 (빗썸)
    # ------------------------------------------------------------------
    def collect_domestic_market(self, count: int = 90) -> List[Dict[str, Any]]:
        logger.info("[%s] 빗썸 BTC_KRW 일봉 %d개 수집 시도...", self.name, count)
        candles = bithumb_api.get_candlestick(SYMBOL, "24h", count)
        if candles:
            logger.info("[%s] 빗썸 캔들 %d개 확보", self.name, len(candles))
            return candles
        if self.use_synthetic_fallback:
            logger.warning("[%s] 빗썸 수집 실패 → 합성 데이터 폴백", self.name)
            return self._synthetic_candles(count)
        return []

    # ------------------------------------------------------------------
    # 해외 시장 (글로벌 BTC/USD)
    # ------------------------------------------------------------------
    def collect_global_market(self, current_price_krw: float = 0.0, usd_krw: float = 1350.0) -> float:
        logger.info("[%s] 글로벌 BTC/USD 수집 시도...", self.name)
        data = _safe_get(
            f"{COINGECKO_BASE_URL}/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
        )
        if data and "bitcoin" in data:
            usd = float(data["bitcoin"]["usd"])
            logger.info("[%s] BTC/USD = $%s", self.name, f"{usd:,.0f}")
            return usd
        fallback = (current_price_krw / usd_krw) if current_price_krw else 0.0
        logger.warning("[%s] 글로벌 수집 실패 → 환산 추정 $%s", self.name, f"{fallback:,.0f}")
        return fallback

    # ------------------------------------------------------------------
    # 매크로 (공포·탐욕 지수)
    # ------------------------------------------------------------------
    def collect_macro_indicators(self) -> Tuple[int, str]:
        logger.info("[%s] 공포·탐욕 지수 수집 시도...", self.name)
        data = _safe_get(FEAR_GREED_URL)
        if data and data.get("data"):
            entry = data["data"][0]
            idx = int(entry.get("value", 50))
            label = entry.get("value_classification", "Neutral")
            logger.info("[%s] F&G = %d (%s)", self.name, idx, label)
            return idx, label
        logger.warning("[%s] F&G 수집 실패 → 중립 50 사용", self.name)
        return 50, "Neutral"

    def collect_usd_krw(self) -> float:
        """원/달러 환율. (간이 고정값 — 추후 한국은행 ECOS API 연동 예정)"""
        return 1350.0

    # ------------------------------------------------------------------
    # 통합 수집
    # ------------------------------------------------------------------
    def collect_all(self, count: int = 90) -> MarketData:
        logger.info("[%s] === 데이터 수집 시작 ===", self.name)
        candles = self.collect_domestic_market(count)
        current = candles[-1]["close"] if candles else 0.0
        usd_krw = self.collect_usd_krw()
        btc_usd = self.collect_global_market(current, usd_krw)
        fng, fng_label = self.collect_macro_indicators()

        md = MarketData(
            symbol=SYMBOL,
            current_price_krw=current,
            daily_ohlcv=candles,
            global_btc_usd=btc_usd,
            usd_krw_rate=usd_krw,
            fear_greed_index=fng,
            fear_greed_label=fng_label,
            collected_at=datetime.now(),
        )
        logger.info(
            "[%s] === 수집 완료: 현재가 %s원 / BTC $%s / F&G %d ===",
            self.name, f"{current:,.0f}", f"{btc_usd:,.0f}", fng,
        )
        return md

    # ------------------------------------------------------------------
    # 합성 데이터 (오프라인 폴백 / 테스트)
    # ------------------------------------------------------------------
    def _synthetic_candles(
        self, count: int, start_price: float = 150_000_000,
        daily_sigma: float = 0.025, seed: Optional[int] = 42,
    ) -> List[Dict[str, Any]]:
        """기하 브라운운동 기반 BTC_KRW 합성 일봉 생성."""
        rng = np.random.default_rng(seed)
        prices = [start_price]
        for _ in range(count):
            prices.append(prices[-1] * np.exp(rng.normal(0, daily_sigma)))
        candles: List[Dict[str, Any]] = []
        base_date = datetime.now() - timedelta(days=count)
        for i in range(1, len(prices)):
            o, c = prices[i - 1], prices[i]
            hi = max(o, c) * (1 + abs(rng.normal(0, daily_sigma / 2)))
            lo = min(o, c) * (1 - abs(rng.normal(0, daily_sigma / 2)))
            vol = float(abs(rng.normal(500, 150)))
            candles.append({
                "date": int((base_date + timedelta(days=i)).timestamp() * 1000),
                "open": float(o), "close": float(c),
                "high": float(hi), "low": float(lo), "volume": vol,
            })
        return candles
