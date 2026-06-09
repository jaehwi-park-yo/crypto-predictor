"""
④ 그리드 최적화 에이전트 (GridOptimizerAgent)
----------------------------------------------
역할: 박스권 예측 + 자본을 받아 그리드 봇 파라미터를 산출한다.
판단 반영:
  - 수수료 인지 최소 간격(왕복수수료×3 ≈ 0.24%)을 하드 제약
  - 목표 거래량은 자본의 함수(리워드 에이전트 추천)로 자동 설정
  - 거래량 추정은 명시적 가정 기반 휴리스틱(실데이터로 보정)
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

from config import (
    KRW_HOLD_RATIO, MAX_BOTS, FEE_RATE, MIN_GRID_INTERVAL_PCT,
    GRID_INTERVAL_CANDIDATES_PCT, TRADING_DAYS_PER_MONTH,
    GRID_FILL_EFFICIENCY, DAILY_RANGE_SIGMA_MULT,
)
from models.market_state import MarketData
from models.prediction_result import BoxPrediction, GridConfig
from agents.reward_calculator import RewardCalculatorAgent

logger = logging.getLogger("grid_optimizer")


class GridOptimizerAgent:
    name = "④ 그리드최적화"

    def __init__(self):
        self.reward_agent = RewardCalculatorAgent()

    # ------------------------------------------------------------------
    def optimize(
        self,
        box: BoxPrediction,
        daily_sigma: float,
        capital_krw: float,
        target_volume_krw: Optional[float] = None,
        krw_hold_ratio: float = KRW_HOLD_RATIO,
    ) -> GridConfig:
        logger.info("[%s] === 그리드 최적화 시작 (자본 %s원) ===", self.name, f"{capital_krw:,.0f}")

        upper, lower = box.recommended_upper, box.recommended_lower
        ref = box.reference_price
        box_range_pct = (upper - lower) / lower * 100 if lower else 0.0

        deployed = capital_krw * (1 - krw_hold_ratio)
        reserve = capital_krw * krw_hold_ratio

        # 목표 거래량: 미지정 시 자본 기반 안전목표 사용
        if target_volume_krw is None:
            rec = self.reward_agent.recommend_target_tier(capital_krw)
            target_volume_krw = rec["safe"]["threshold"]

        # 후보 그리드 간격 평가 (수수료 인지 최소 간격 적용)
        best = None
        for gi in sorted(GRID_INTERVAL_CANDIDATES_PCT):
            gi_eff = max(gi, MIN_GRID_INTERVAL_PCT)
            bots = max(1, math.ceil(box_range_pct / gi_eff))
            capped = bots > MAX_BOTS
            bots = min(bots, MAX_BOTS)
            est_vol = self._estimate_volume(deployed, gi_eff, daily_sigma, box_range_pct)
            cand = {
                "gi": gi_eff, "bots": bots, "est_vol": est_vol, "capped": capped,
            }
            # 선정: 목표 충족(est_vol>=target) 우선, 그 중 봇 적은 것 → 아니면 거래량 최대
            if best is None:
                best = cand
            else:
                best = self._pick(best, cand, target_volume_krw)

        gi_eff = best["gi"]
        bots = best["bots"]
        est_vol = best["est_vol"]
        cap_per_bot = deployed / bots if bots else 0.0
        gi_krw = ref * gi_eff / 100

        reward = self.reward_agent.calculate_reward(est_vol)
        fee_cost = est_vol * FEE_RATE
        net_reward = reward["reward_krw"] - fee_cost  # 리워드만으로 본 순효과(보통 음수)

        config = GridConfig(
            symbol=box.symbol,
            box_upper=upper, box_lower=lower,
            grid_interval_pct=gi_eff,
            grid_interval_krw=gi_krw,
            bot_count=bots,
            capital_total_krw=capital_krw,
            capital_deployed_krw=deployed,
            krw_reserve_krw=reserve,
            capital_per_bot_krw=cap_per_bot,
            estimated_monthly_volume_krw=est_vol,
            estimated_reward_tier_threshold=reward["tier_threshold"],
            estimated_reward_rate=reward["rate"],
            estimated_reward_krw=reward["reward_krw"],
            fee_rate=FEE_RATE,
            created_at=datetime.now(),
            net_reward_after_fee_krw=net_reward,
            grid_count=bots,
            round_trips_per_day=self._daily_round_trips(gi_eff, daily_sigma, box_range_pct),
        )

        logger.info(
            "[%s] === 최적화 완료: 간격 %.2f%%(%s원) / 봇 %d개 / 예상거래량 %s원 → 리워드 %s원 ===",
            self.name, gi_eff, f"{gi_krw:,.0f}", bots,
            f"{est_vol:,.0f}", f"{reward['reward_krw']:,.0f}",
        )
        if best["capped"]:
            logger.warning(
                "[%s] 박스 폭 대비 봇 수가 상한(%d) 초과 → 간격 자동 확대됨", self.name, MAX_BOTS
            )
        return config

    # ------------------------------------------------------------------
    @staticmethod
    def _pick(a: dict, b: dict, target: float) -> dict:
        """목표 거래량 충족을 우선하는 후보 선택."""
        a_ok, b_ok = a["est_vol"] >= target, b["est_vol"] >= target
        if a_ok and b_ok:
            # 둘 다 충족 → 봇 적은(간격 넓은) 쪽이 안정적
            return a if a["bots"] <= b["bots"] else b
        if a_ok != b_ok:
            return a if a_ok else b
        # 둘 다 미달 → 거래량 큰 쪽
        return a if a["est_vol"] >= b["est_vol"] else b

    def _daily_round_trips(self, grid_pct: float, daily_sigma: float, box_range_pct: float) -> float:
        """하루 예상 왕복 체결 수 (전체 라인 수로 상한)."""
        daily_range_pct = daily_sigma * 100 * DAILY_RANGE_SIGMA_MULT
        crossings = daily_range_pct / grid_pct if grid_pct else 0.0
        max_lines = box_range_pct / grid_pct if grid_pct else 0.0
        crossings = min(crossings, max_lines)
        return crossings * GRID_FILL_EFFICIENCY

    def _estimate_volume(
        self, deployed: float, grid_pct: float, daily_sigma: float, box_range_pct: float
    ) -> float:
        """
        월간 예상 거래량 (휴리스틱):
            일왕복수 × 30일 × 배치자본 × 2(매수+매도 양다리)
        ※ 미끄럼/미체결 미반영 → 계획용 추정치. 실거래로 보정 필요.
        """
        daily_rt = self._daily_round_trips(grid_pct, daily_sigma, box_range_pct)
        monthly_turnover = daily_rt * TRADING_DAYS_PER_MONTH
        return deployed * monthly_turnover * 2
