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

from config import (
    KRW_HOLD_RATIO, MAX_BOTS, FEE_RATE, MIN_GRID_INTERVAL_PCT,
    TRADING_DAYS_PER_MONTH, GRID_FILL_EFFICIENCY, DAILY_RANGE_SIGMA_MULT,
    GRID_AGGRESSIVENESS, AGGRESSIVENESS_INTERVAL_PCT,
    ASYMMETRIC_GRID, GRID_BUY_INTERVAL_PCT, GRID_SELL_INTERVAL_PCT,
    PROGRESSIVE_SIZING, PROGRESSIVE_ALPHA,
    ASYM_TP_ENABLED, ASYM_TP_MULT,
)
from typing import Optional
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
        aggressiveness: str = GRID_AGGRESSIVENESS,
        krw_hold_ratio: float = KRW_HOLD_RATIO,
        buy_interval_pct: Optional[float] = None,
        sell_interval_pct: Optional[float] = None,
    ) -> GridConfig:
        logger.info(
            "[%s] === 그리드 최적화 시작 (자본 %s원 / 공격성 %s) ===",
            self.name, f"{capital_krw:,.0f}", aggressiveness,
        )

        upper, lower = box.recommended_upper, box.recommended_lower
        ref = box.reference_price
        box_range_pct = (upper - lower) / lower * 100 if lower else 0.0

        deployed = capital_krw * (1 - krw_hold_ratio)
        reserve = capital_krw * krw_hold_ratio

        # 공격성 다이얼 → 기준 그리드 간격 (수수료 인지 최소 간격으로 하한 보정)
        if buy_interval_pct is None and sell_interval_pct is None and ASYMMETRIC_GRID:
            buy_interval_pct  = GRID_BUY_INTERVAL_PCT
            # ASYM_TP: take-profit placed ASYM_TP_MULT × buy-interval above entry
            sell_interval_pct = (
                buy_interval_pct * ASYM_TP_MULT if ASYM_TP_ENABLED
                else GRID_SELL_INTERVAL_PCT
            )
        asymmetric = (buy_interval_pct is not None and sell_interval_pct is not None)
        if asymmetric:
            buy_gi  = max(buy_interval_pct,  MIN_GRID_INTERVAL_PCT / 2)
            sell_gi = max(sell_interval_pct, MIN_GRID_INTERVAL_PCT / 2)
            gi_eff  = buy_gi   # bot count based on buy spacing
        else:
            base_gi = AGGRESSIVENESS_INTERVAL_PCT.get(aggressiveness, 0.5)
            gi_eff  = max(base_gi, MIN_GRID_INTERVAL_PCT)
            buy_gi  = gi_eff
            sell_gi = gi_eff
        bots = max(1, math.ceil(box_range_pct / gi_eff))

        # 봇 상한(1,000) 초과 시 간격 자동 확대
        capped = bots > MAX_BOTS
        if capped:
            bots = MAX_BOTS
            gi_eff = box_range_pct / MAX_BOTS

        # 왕복 추정용 간격: 비대칭이면 사이클(매수+매도)/2 — 대칭 관례(cycle=2×gi)와 동일 규약
        rt_gi = (buy_gi + sell_gi) / 2 if asymmetric else gi_eff

        # Progressive sizing: boundary bots get more capital → higher fill probability
        # Weight_i = 1 + α × level, where level = distance from center (0-indexed)
        if PROGRESSIVE_SIZING and bots > 1:
            half = bots / 2.0
            weights = [1.0 + PROGRESSIVE_ALPHA * abs(i - half + 0.5) for i in range(bots)]
            w_sum = sum(weights)
            cap_per_bot = deployed / w_sum   # center-level unit capital
            # volume estimation uses effective "average weight" (weighted turnover)
            avg_weight = w_sum / bots
            est_vol = self._estimate_volume(deployed * avg_weight, rt_gi, daily_sigma, box_range_pct)
        else:
            cap_per_bot = deployed / bots if bots else 0.0
            est_vol = self._estimate_volume(deployed, rt_gi, daily_sigma, box_range_pct)

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
            round_trips_per_day=self._daily_round_trips(rt_gi, daily_sigma, box_range_pct),
            aggressiveness=aggressiveness,
            buy_interval_pct=buy_gi if asymmetric else None,
            sell_interval_pct=sell_gi if asymmetric else None,
        )

        logger.info(
            "[%s] === 최적화 완료: 간격 %.2f%%(%s원) / 봇 %d개 / 예상거래량 %s원 → 리워드 %s원 ===",
            self.name, gi_eff, f"{gi_krw:,.0f}", bots,
            f"{est_vol:,.0f}", f"{reward['reward_krw']:,.0f}",
        )
        if capped:
            logger.warning(
                "[%s] 박스 폭 대비 봇 수가 상한(%d) 초과 → 간격 %.2f%%로 자동 확대됨",
                self.name, MAX_BOTS, gi_eff,
            )
        return config

    # ------------------------------------------------------------------
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
