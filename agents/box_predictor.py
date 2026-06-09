"""
③ 박스권 예측 에이전트 (BoxPredictorAgent)
----------------------------------------------
역할: 통계적 추론으로 익월 박스권을 예측한다. (시스템의 핵심)
방법:
  - 최근 30일 로그수익률로 일간 변동성 σ 추정
  - 월간 지평(30일)으로 √t 투영: σ_월 = σ_일 × √30
  - 1σ 박스(정상 운영범위, 68.3%) / 2σ 박스(이탈 경보선, 95.5%)
  - 시나리오에 따라 권장 박스의 시그마 레벨 선택
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Tuple

from config import HORIZON_DAYS, PREDICTION_DRIFT
from models.market_state import MarketData, MarketAnalysis
from models.prediction_result import BoxPrediction
from utils.statistics import (
    compute_log_returns, daily_volatility, sigma_band,
    containment_probability, boundary_touch_probability,
)

logger = logging.getLogger("box_predictor")


class BoxPredictorAgent:
    name = "③ 박스권예측"

    def predict(
        self,
        md: MarketData,
        analysis: MarketAnalysis,
        horizon_days: int = HORIZON_DAYS,
        target_month: str = None,
    ) -> BoxPrediction:
        logger.info("[%s] === 박스권 예측 시작 (지평 %d일) ===", self.name, horizon_days)

        closes = md.get_closes()
        ref = md.current_price_krw or closes[-1]
        returns = compute_log_returns(closes[-31:])
        dsig = daily_volatility(returns)

        # 박스권 매매 = 평균회귀 가정 → drift 0 권장 (config.PREDICTION_DRIFT)
        drift = PREDICTION_DRIFT

        u1, l1 = sigma_band(ref, dsig, horizon_days, 1.0, drift)
        u2, l2 = sigma_band(ref, dsig, horizon_days, 2.0, drift)

        sigma_used, scen_probs = self._scenario_to_sigma(analysis)

        if sigma_used == 1.0:
            rec_u, rec_l = u1, l1
        else:
            rec_u, rec_l = u2, l2

        box_range_pct = (rec_u - rec_l) / rec_l * 100 if rec_l else 0.0
        confidence = containment_probability(sigma_used) * 100

        prediction = BoxPrediction(
            symbol=md.symbol,
            reference_price=ref,
            box_upper_1sigma=u1, box_lower_1sigma=l1,
            box_upper_2sigma=u2, box_lower_2sigma=l2,
            recommended_upper=rec_u, recommended_lower=rec_l,
            sigma_level_used=sigma_used,
            box_range_pct=box_range_pct,
            confidence_pct=confidence,
            scenario=analysis.scenario,
            scenario_probabilities=scen_probs,
            predicted_for_month=target_month or self._next_month(),
            created_at=datetime.now(),
        )

        logger.info(
            "[%s] === 예측 완료: %sσ 박스 [%s ~ %s] 폭 %.1f%% (containment %.1f%%) ===",
            self.name, sigma_used,
            f"{rec_l:,.0f}", f"{rec_u:,.0f}", box_range_pct, confidence,
        )
        # 부가 정보 로그: 경계 터치 확률(그리드 체결 빈도 가늠)
        logger.info(
            "[%s] 경계 터치확률: 1σ %.0f%% / 2σ %.0f%% (높을수록 그리드 체결 빈번)",
            self.name,
            boundary_touch_probability(1.0) * 100,
            boundary_touch_probability(2.0) * 100,
        )
        return prediction

    # ------------------------------------------------------------------
    def _scenario_to_sigma(self, analysis: MarketAnalysis) -> Tuple[float, Dict[str, float]]:
        """
        시나리오 → 권장 시그마 레벨 + 시나리오 확률분포.
        - 횡보: 1σ를 1차 박스로 (그리드 밀집)
        - 추세/고위험: 2σ로 폭 확대 (이탈 리스크 흡수)
        """
        scenario = analysis.scenario
        conf = analysis.scenario_confidence

        base = {
            "BULLISH": 0.10, "BEARISH": 0.10,
            "SIDEWAYS_NARROW": 0.30, "SIDEWAYS_WIDE": 0.30,
            "HIGH_RISK": 0.20,
        }
        # 분류된 시나리오에 확신도만큼 가중치 이동
        probs = dict(base)
        boost = conf * 0.5
        probs[scenario] = probs.get(scenario, 0.0) + boost
        total = sum(probs.values())
        probs = {k: round(v / total, 3) for k, v in probs.items()}

        if scenario in ("SIDEWAYS_NARROW", "SIDEWAYS_WIDE"):
            sigma_used = 1.0
        else:  # BULLISH / BEARISH / HIGH_RISK
            sigma_used = 2.0
        return sigma_used, probs

    @staticmethod
    def _next_month() -> str:
        now = datetime.now()
        year, month = now.year, now.month + 1
        if month > 12:
            year, month = year + 1, 1
        return f"{year:04d}-{month:02d}"
