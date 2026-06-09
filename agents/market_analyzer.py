"""
② 시장 분석 에이전트 (MarketAnalyzerAgent)
----------------------------------------------
역할: 수집된 데이터로 기술적 지표를 산출하고 익월 시나리오를 분류한다.
시나리오: BULLISH / BEARISH / SIDEWAYS_NARROW / SIDEWAYS_WIDE / HIGH_RISK
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Tuple

from models.market_state import MarketData, MarketAnalysis
from utils.statistics import (
    compute_ma, compute_bollinger_bands, compute_rsi, compute_atr,
    compute_log_returns, daily_volatility, annualize_volatility,
)

logger = logging.getLogger("market_analyzer")

# 시나리오 분류 임계치
HIGH_RISK_DAILY_SIGMA = 0.045        # 일간 변동성 4.5% 이상 → 고위험
NARROW_BB_WIDTH_PCT = 8.0            # 볼린저 폭 8% 미만 → 좁은 박스
TREND_DEVIATION_PCT = 2.0            # MA20 대비 ±2% → 추세 판단


class MarketAnalyzerAgent:
    name = "② 시장분석"

    def analyze(self, md: MarketData) -> MarketAnalysis:
        logger.info("[%s] === 시장 분석 시작 ===", self.name)
        closes = md.get_closes()
        highs = md.get_highs()
        lows = md.get_lows()
        volumes = md.get_volumes()

        if len(closes) < 2:
            raise ValueError("분석에 필요한 가격 데이터가 부족합니다.")

        ma7 = compute_ma(closes, 7)
        ma20 = compute_ma(closes, 20)
        ma60 = compute_ma(closes, 60)
        bb_u, bb_m, bb_l = compute_bollinger_bands(closes, 20, 2.0)
        atr = compute_atr(highs, lows, closes, 14)
        rsi = compute_rsi(closes, 14)

        returns = compute_log_returns(closes[-31:])   # 최근 30 수익률
        dsig = daily_volatility(returns)
        hv = annualize_volatility(dsig, 365)

        kimchi = self._kimchi_premium(md)
        price = md.current_price_krw or closes[-1]

        scenario, conf = self._classify(price, ma7, ma20, ma60, rsi, bb_u, bb_l, dsig)

        bb_width_pct = (bb_u - bb_l) / bb_m * 100 if bb_m else 0.0
        price_vs_ma20 = (price - ma20) / ma20 * 100 if ma20 else 0.0

        analysis = MarketAnalysis(
            ma7=ma7, ma20=ma20, ma60=ma60,
            bb_upper=bb_u, bb_middle=bb_m, bb_lower=bb_l,
            atr_14=atr, rsi_14=rsi,
            historical_volatility_30d=hv,
            kimchi_premium_pct=kimchi,
            scenario=scenario, scenario_confidence=conf,
            analyzed_at=datetime.now(),
            ma7_trend=self._trend(price, ma7),
            ma20_trend=self._trend(price, ma20),
            rsi_signal=self._rsi_signal(rsi),
            volume_trend=self._volume_trend(volumes),
            bb_width_pct=bb_width_pct,
            price_vs_ma20_pct=price_vs_ma20,
        )
        logger.info(
            "[%s] === 분석 완료: 시나리오=%s (확신 %.0f%%), 일변동성=%.2f%%, RSI=%.0f ===",
            self.name, scenario, conf * 100, dsig * 100, rsi,
        )
        return analysis

    # ------------------------------------------------------------------
    def _classify(
        self, price, ma7, ma20, ma60, rsi, bb_u, bb_l, dsig
    ) -> Tuple[str, float]:
        """규칙 기반 시나리오 분류 + 확신도(0~1)."""
        # 1) 고위험: 변동성 급등 우선
        if dsig >= HIGH_RISK_DAILY_SIGMA:
            conf = min(1.0, dsig / HIGH_RISK_DAILY_SIGMA * 0.6)
            return "HIGH_RISK", conf

        dev = (price - ma20) / ma20 * 100 if ma20 else 0.0
        up_stack = ma7 > ma20 > ma60
        down_stack = ma7 < ma20 < ma60

        # 2) 추세
        if up_stack and dev > TREND_DEVIATION_PCT:
            conf = min(1.0, 0.5 + abs(dev) / 20)
            return "BULLISH", conf
        if down_stack and dev < -TREND_DEVIATION_PCT:
            conf = min(1.0, 0.5 + abs(dev) / 20)
            return "BEARISH", conf

        # 3) 횡보 — 볼린저 폭으로 좁음/넓음 구분
        bb_width_pct = (bb_u - bb_l) / ((bb_u + bb_l) / 2) * 100 if (bb_u + bb_l) else 0.0
        if bb_width_pct < NARROW_BB_WIDTH_PCT:
            return "SIDEWAYS_NARROW", 0.6
        return "SIDEWAYS_WIDE", 0.55

    def _kimchi_premium(self, md: MarketData) -> float:
        """김치 프리미엄(%) = (국내가 / (글로벌가×환율) − 1) × 100."""
        global_krw = md.global_btc_usd * md.usd_krw_rate
        if global_krw <= 0 or md.current_price_krw <= 0:
            return 0.0
        return (md.current_price_krw / global_krw - 1.0) * 100

    @staticmethod
    def _trend(price: float, ma: float) -> str:
        if ma <= 0:
            return "NEUTRAL"
        diff = (price - ma) / ma * 100
        if diff > 1.0:
            return "UP"
        if diff < -1.0:
            return "DOWN"
        return "NEUTRAL"

    @staticmethod
    def _rsi_signal(rsi: float) -> str:
        if rsi >= 70:
            return "OVERBOUGHT"
        if rsi <= 30:
            return "OVERSOLD"
        return "NEUTRAL"

    @staticmethod
    def _volume_trend(volumes) -> str:
        if len(volumes) < 10:
            return "NORMAL"
        recent = sum(volumes[-5:]) / 5
        baseline = sum(volumes[-20:]) / min(20, len(volumes))
        if baseline <= 0:
            return "NORMAL"
        ratio = recent / baseline
        if ratio > 1.3:
            return "HIGH"
        if ratio < 0.7:
            return "LOW"
        return "NORMAL"
