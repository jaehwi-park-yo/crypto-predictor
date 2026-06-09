"""
⑤ 리워드 계산 에이전트 (RewardCalculatorAgent)
----------------------------------------------
역할: 직전월 거래량 기반 리워드 산출, 목표 구간 추천, 월중 페이스 점검.

[중요 구조] 빗썸 왕복 수수료 0.08% > 리워드 0.008~0.02%.
즉 리워드는 '수수료 환급' 성격이며, 실제 수익은 그리드 스프레드에서 나온다.
리워드는 현금흐름의 보조 축으로 본다.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from config import (
    REWARD_TIERS, MAX_REWARD_KRW, COMFORTABLE_DAILY_TURNOVER,
    TRADING_DAYS_PER_MONTH,
)

logger = logging.getLogger("reward_calculator")


class RewardCalculatorAgent:
    name = "⑤ 리워드계산"

    # ------------------------------------------------------------------
    def get_tier(self, volume_krw: float) -> Tuple[float, float]:
        """거래량 → (구간 임계값, 리워드율). 미달 시 (0, 0)."""
        for threshold, rate in REWARD_TIERS:   # 내림차순 정렬 가정
            if volume_krw >= threshold:
                return threshold, rate
        return 0.0, 0.0

    def calculate_reward(self, volume_krw: float) -> Dict[str, float]:
        """거래량 → 리워드 산출 결과 dict."""
        threshold, rate = self.get_tier(volume_krw)
        raw = volume_krw * rate
        reward = min(raw, MAX_REWARD_KRW)
        return {
            "volume_krw": volume_krw,
            "tier_threshold": threshold,
            "rate": rate,
            "raw_reward": raw,
            "reward_krw": reward,
            "capped": raw > MAX_REWARD_KRW,
        }

    # ------------------------------------------------------------------
    def recommend_target_tier(self, capital_krw: float) -> Dict[str, object]:
        """
        현재 자본으로 '현실적으로 도달 가능한' 목표 구간을 추천.
        기준: 필요 일일회전율 <= COMFORTABLE_DAILY_TURNOVER.
        반환: 안전목표(가장 안정) + 스트레치목표(상한 근접).
        """
        feasible = []
        for threshold, rate in sorted(REWARD_TIERS):   # 오름차순
            req_monthly_turnover = threshold / capital_krw if capital_krw else float("inf")
            req_daily = req_monthly_turnover / TRADING_DAYS_PER_MONTH
            reward = min(threshold * rate, MAX_REWARD_KRW)
            entry = {
                "threshold": threshold, "rate": rate, "reward": reward,
                "req_monthly_turnover": req_monthly_turnover,
                "req_daily_turnover": req_daily,
                "feasible": req_daily <= COMFORTABLE_DAILY_TURNOVER,
            }
            feasible.append(entry)

        # 안전 = '매우 여유롭게'(상한의 1/3) 도달 가능한 가장 높은 구간
        # 스트레치 = 상한 이내 도달 가능한 가장 높은 구간
        very_comfortable = [e for e in feasible if e["req_daily_turnover"] <= COMFORTABLE_DAILY_TURNOVER / 3]
        comfortable = [e for e in feasible if e["feasible"]]
        safe = very_comfortable[-1] if very_comfortable else (comfortable[0] if comfortable else feasible[0])
        stretch = comfortable[-1] if comfortable else feasible[0]
        logger.info(
            "[%s] 자본 %s원 → 안전목표 %s원(리워드 %s) / 스트레치 %s원(리워드 %s)",
            self.name, f"{capital_krw:,.0f}",
            f"{safe['threshold']:,.0f}", f"{safe['reward']:,.0f}",
            f"{stretch['threshold']:,.0f}", f"{stretch['reward']:,.0f}",
        )
        return {"all": feasible, "safe": safe, "stretch": stretch}

    # ------------------------------------------------------------------
    def pace_check(
        self, current_volume_krw: float, days_elapsed: int,
        days_in_month: int = TRADING_DAYS_PER_MONTH,
    ) -> Dict[str, object]:
        """월중 거래량 페이스 → 월말 예상 거래량 및 도달 구간."""
        d = max(days_elapsed, 1)
        projected = current_volume_krw / d * days_in_month
        proj_reward = self.calculate_reward(projected)
        return {
            "current_volume": current_volume_krw,
            "days_elapsed": days_elapsed,
            "projected_volume": projected,
            "projected_reward": proj_reward["reward_krw"],
            "projected_tier": proj_reward["tier_threshold"],
        }

    def days_to_next_tier(
        self, current_volume_krw: float, days_elapsed: int, target_threshold: float,
    ) -> Optional[float]:
        """현재 페이스로 목표 구간 도달까지 남은 일수 추정."""
        d = max(days_elapsed, 1)
        daily_rate = current_volume_krw / d
        if daily_rate <= 0:
            return None
        remaining = target_threshold - current_volume_krw
        if remaining <= 0:
            return 0.0
        return remaining / daily_rate
