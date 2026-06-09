"""
⑥ 리스크 모니터링 에이전트 (RiskMonitorAgent)
----------------------------------------------
역할: 월중 현재가를 박스권과 비교해 이탈 여부·심각도·대응을 판정한다.
비대칭 원칙(사용자 확정):
  - 정상(1σ 이내): 관망/유지
  - 경고(1σ~2σ): 모니터링 강화
  - 이탈(2σ 초과) 상방: 관망 유지 (그리드가 매도 완료, 손실 없음)
  - 이탈(2σ 초과) 하방: 30% 부분손절 → 박스 하향 재설정
"""
from __future__ import annotations

import logging

from config import PARTIAL_STOP_RATIO, HORIZON_DAYS, PREDICTION_DRIFT
from models.market_state import MarketData
from models.prediction_result import BoxPrediction, BreakoutStatus
from utils.statistics import sigma_band

logger = logging.getLogger("risk_monitor")


class RiskMonitorAgent:
    name = "⑥ 리스크모니터"

    # ------------------------------------------------------------------
    def check_breakout(self, current_price: float, box: BoxPrediction) -> BreakoutStatus:
        u1, l1 = box.box_upper_1sigma, box.box_lower_1sigma
        u2, l2 = box.box_upper_2sigma, box.box_lower_2sigma
        mid = (box.recommended_upper + box.recommended_lower) / 2

        within = l1 <= current_price <= u1

        # 심각도
        if within:
            severity = "NORMAL"
        elif l2 <= current_price <= u2:
            severity = "WARNING"
        else:
            severity = "CRITICAL"

        # 방향
        if current_price > u1:
            direction = "UPSIDE"
        elif current_price < l1:
            direction = "DOWNSIDE"
        else:
            direction = None

        # 이탈 폭(%)
        if current_price > box.recommended_upper:
            deviation = (current_price - box.recommended_upper) / box.recommended_upper * 100
        elif current_price < box.recommended_lower:
            deviation = (current_price - box.recommended_lower) / box.recommended_lower * 100
        else:
            deviation = 0.0

        status = BreakoutStatus(
            symbol=box.symbol,
            current_price=current_price,
            box_upper=box.recommended_upper,
            box_lower=box.recommended_lower,
            is_within_box=within,
            breakout_direction=direction,
            severity=severity,
            action_required="MONITOR",
            deviation_pct=deviation,
        )

        # 대응 결정
        if severity == "CRITICAL" and direction == "UPSIDE":
            self._apply_upside(status)
        elif severity == "CRITICAL" and direction == "DOWNSIDE":
            self._apply_downside(status, box)
        elif severity == "WARNING":
            status.action_required = "MONITOR"
        else:
            status.action_required = "HOLD"

        logger.info(
            "[%s] 현재가 %s원 → %s/%s → 조치: %s",
            self.name, f"{current_price:,.0f}", severity, direction or "IN-BOX",
            status.action_required,
        )
        return status

    # ------------------------------------------------------------------
    def _apply_upside(self, status: BreakoutStatus) -> None:
        """상방 2σ 이탈 → 관망 유지(그리드 매도 완료 상태, 손실 없음)."""
        status.action_required = "HOLD"
        logger.info("[%s] 상방 이탈 → 관망 유지 (추격 금지)", self.name)

    def _apply_downside(self, status: BreakoutStatus, box: BoxPrediction) -> None:
        """하방 2σ 이탈 → 30% 부분손절 + 박스 하향 재설정 제안."""
        status.action_required = "PARTIAL_STOP"
        new_u, new_l = self.reset_box(status.current_price, box)
        status.suggested_new_upper = new_u
        status.suggested_new_lower = new_l
        logger.warning(
            "[%s] 하방 이탈 → %.0f%% 부분손절 + 박스 재설정 [%s ~ %s]",
            self.name, PARTIAL_STOP_RATIO * 100,
            f"{new_l:,.0f}", f"{new_u:,.0f}",
        )

    def reset_box(self, current_price: float, box: BoxPrediction):
        """
        현재가를 새 기준점으로 박스 재계산.
        기존 박스 폭의 절반을 일간 σ로 역산해 동일 시그마 밴드를 재투영.
        """
        # 기존 1σ 폭 → 일간 σ 역산: ln(u1/l1) = 2·σ_월 = 2·σ_일·√h
        import math
        ratio = box.box_upper_1sigma / box.box_lower_1sigma
        sig_month_half = math.log(ratio) / 2 if ratio > 0 else 0.0
        daily_sigma = sig_month_half / math.sqrt(HORIZON_DAYS) if HORIZON_DAYS else 0.0
        # 재설정은 2σ 밴드로 보수적으로 (하향 변동성 흡수)
        return sigma_band(current_price, daily_sigma, HORIZON_DAYS, 2.0, PREDICTION_DRIFT)

    @staticmethod
    def is_within_box(price: float, upper: float, lower: float) -> bool:
        return lower <= price <= upper
