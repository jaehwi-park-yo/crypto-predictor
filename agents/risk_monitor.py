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

from config import (
    PARTIAL_STOP_RATIO, HORIZON_DAYS, PREDICTION_DRIFT,
    CASH_DEPLOY_ON_BREAKOUT, CASH_DEPLOY_RATIO,
    CASH_DEPLOY_GRID_MULT, CASH_DEPLOY_SIGMA_LEVEL,
    AGGRESSIVENESS_INTERVAL_PCT, GRID_AGGRESSIVENESS,
)
from models.market_state import MarketData
from models.prediction_result import BoxPrediction, BreakoutStatus
from utils.statistics import sigma_band, containment_probability

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
        """하방 2σ 이탈 → 30% 부분손절 + 박스 하향 재설정 + 현금 배치 전략 제안."""
        status.action_required = "PARTIAL_STOP"
        new_u, new_l = self.reset_box(status.current_price, box)
        status.suggested_new_upper = new_u
        status.suggested_new_lower = new_l
        logger.warning(
            "[%s] 하방 이탈 → %.0f%% 부분손절 + 박스 재설정 [%s ~ %s]",
            self.name, PARTIAL_STOP_RATIO * 100,
            f"{new_l:,.0f}", f"{new_u:,.0f}",
        )
        if CASH_DEPLOY_ON_BREAKOUT:
            plan = self.cash_deploy_plan(status.current_price, box, new_u, new_l)
            status.cash_deploy_plan = plan
            logger.info(
                "[%s] 현금 배치 전략: %.0f%% KRW 예비금 → 회복 그리드 [%s ~ %s], 간격 %.2f%%",
                self.name, CASH_DEPLOY_RATIO * 100,
                f"{new_l:,.0f}", f"{new_u:,.0f}",
                plan["recovery_grid_interval_pct"],
            )

    def reset_box(self, current_price: float, box: BoxPrediction):
        """
        현재가를 새 기준점으로 박스 재계산.
        기존 박스 폭의 절반을 일간 σ로 역산해 동일 시그마 밴드를 재투영.
        """
        import math
        ratio = box.box_upper_1sigma / box.box_lower_1sigma
        sig_month_half = math.log(ratio) / 2 if ratio > 0 else 0.0
        daily_sigma = sig_month_half / math.sqrt(HORIZON_DAYS) if HORIZON_DAYS else 0.0
        # 재설정은 2σ 밴드로 보수적으로 (하향 변동성 흡수)
        return sigma_band(current_price, daily_sigma, HORIZON_DAYS, 2.0, PREDICTION_DRIFT)

    def cash_deploy_plan(
        self,
        current_price: float,
        box: BoxPrediction,
        new_upper: float,
        new_lower: float,
    ) -> dict:
        """
        하방 이탈 시 KRW 현금 예비금 배치 전략 계산.

        원리:
          2σ 하단을 이탈한 후 평균회귀(mean-reversion) 확률이 통계적으로 높음.
          - 1σ 내 복귀 확률 ≈ 68.3%  (정규분포 가정)
          - 현재 위치(2σ 이탈 지점)는 GBM 기준 약 97.7% 분위수 하단
          → KRW 예비금 일부를 넓은 간격의 회복 그리드로 배치해 반등 수익 노림

        반환:
          dict {
            'deploy_ratio': float,         # KRW 예비금 배치 비율
            'recovery_grid_interval_pct',  # 회복 그리드 간격 (평소 × 배수)
            'recovery_box_upper',          # 회복 박스 상단
            'recovery_box_lower',          # 회복 박스 하단
            'mean_reversion_prob_pct',     # 1σ 복귀 확률(%)
            'rationale',                   # 전략 근거 설명
          }
        """
        import math

        base_gi = AGGRESSIVENESS_INTERVAL_PCT.get(GRID_AGGRESSIVENESS, 0.5)
        recovery_gi = base_gi * CASH_DEPLOY_GRID_MULT

        # 1σ 복귀 확률 = 2σ 이탈 후 박스 안으로 되돌아올 확률 (보수 추정)
        # GBM에서 2σ 이탈 자체가 약 2.3% 확률 → 그 이후 복귀는 조건부로 높음
        # 실용 근사: 이탈 폭에 따라 50~80% 구간 추정
        deviation_pct = abs((current_price - box.recommended_lower) / box.recommended_lower * 100)
        if deviation_pct < 5:
            reversion_prob = 75.0
        elif deviation_pct < 15:
            reversion_prob = 65.0
        else:
            reversion_prob = 50.0  # 급락 구간 → 확률 하향

        rationale = (
            f"2σ 하방 이탈({deviation_pct:.1f}%)은 GBM 기준 약 2.3% 발생 확률의 극단 이벤트. "
            f"이후 1σ 박스 복귀 확률 추정 {reversion_prob:.0f}%. "
            f"KRW 예비금 {CASH_DEPLOY_RATIO*100:.0f}%를 "
            f"넓은 간격({recovery_gi:.2f}%, 평소 {CASH_DEPLOY_GRID_MULT:.0f}배)으로 배치 → "
            "반등 시 그리드 스프레드 수익 + 보유 단가 하락(물타기 효과). "
            "단, 추가 하락 시 손실 확대 가능 → 잔여 예비금(KRW) 보존 필수."
        )

        return {
            "deploy_ratio": CASH_DEPLOY_RATIO,
            "recovery_grid_interval_pct": recovery_gi,
            "recovery_box_upper": new_upper,
            "recovery_box_lower": new_lower,
            "mean_reversion_prob_pct": reversion_prob,
            "deviation_pct": deviation_pct,
            "rationale": rationale,
        }

    @staticmethod
    def is_within_box(price: float, upper: float, lower: float) -> bool:
        return lower <= price <= upper
