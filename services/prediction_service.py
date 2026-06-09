"""
prediction_service.py - 시점 고정(as-of) 박스권 예측 서비스
=============================================================
"4월까지 데이터로 5월 박스권 예측"처럼, 임의 시점(as_of)을 기준으로
그 시점까지의 히스토리만 사용해 익월 박스권 + 그리드 설정을 산출한다.

실시간 수집기에 의존하는 OrchestratorAgent와 달리,
백테스트/GUI에서 과거 어느 시점이든 재현 가능한 결정론적 예측을 제공한다.

핵심:
  predict_as_of(history, as_of="2026-04-30") -> PredictionSnapshot
    1. history를 as_of 이하로 슬라이스
    2. 최근 31일 종가로 일간 σ 추정
    3. sigma_band로 1σ/2σ 밴드 투영 (drift=0, 평균회귀 가정)
    4. σ 레벨 선택 (override 또는 변동성 기반 자동)
    5. GridOptimizerAgent로 그리드 설정 산출
    6. 리워드 목표 추천 + (옵션) USDT 복합전략
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from config import (
    CAPITAL_KRW, KRW_HOLD_RATIO, HORIZON_DAYS, PREDICTION_DRIFT,
    GRID_AGGRESSIVENESS,
)
from models.prediction_result import BoxPrediction
from utils.statistics import (
    compute_log_returns, daily_volatility, sigma_band,
    containment_probability,
)
from agents.grid_optimizer import GridOptimizerAgent
from agents.reward_calculator import RewardCalculatorAgent

logger = logging.getLogger("prediction_service")

# 변동성 자동 σ 선택 임계 (일간 σ ≥ 이 값이면 2σ 박스 채택)
_AUTO_SIGMA_VOL_THRESHOLD = 0.045


@dataclass
class PredictionSnapshot:
    """특정 시점 기준 익월 예측 스냅샷 (GUI/리포트 공통 입력)."""
    as_of_date: str               # 예측 기준 시점 "YYYY-MM-DD"
    target_month: str             # 예측 대상 월 "YYYY-MM"
    reference_price: float        # 기준가 (as_of 종가)
    daily_sigma: float            # 일간 변동성
    monthly_sigma_pct: float      # 월간 σ (%)

    # 박스 밴드
    box_upper_1s: float
    box_lower_1s: float
    box_upper_2s: float
    box_lower_2s: float
    recommended_upper: float
    recommended_lower: float
    sigma_level_used: float
    box_range_pct: float
    confidence_pct: float

    # 입력 파라미터
    capital_krw: float
    aggressiveness: str
    krw_hold_ratio: float

    # 그리드 설정 (GridConfig 핵심 필드만 평탄화)
    grid_interval_pct: float
    grid_interval_krw: float
    bot_count: int
    capital_deployed_krw: float
    krw_reserve_krw: float
    capital_per_bot_krw: float
    estimated_monthly_volume_krw: float
    estimated_reward_krw: float
    estimated_reward_rate: float

    # 리워드 목표
    safe_target: Dict[str, object] = field(default_factory=dict)
    stretch_target: Dict[str, object] = field(default_factory=dict)

    # 차트용 히스토리 (as_of 이전 N일)
    history_tail: List[Dict] = field(default_factory=list)

    # 원본 BoxPrediction (재사용용)
    box_prediction: Optional[BoxPrediction] = None


def _slice_history(history: List[Dict], as_of: str) -> List[Dict]:
    """history를 as_of(YYYY-MM-DD) 이하로 슬라이스."""
    return [c for c in history if c["date"][:10] <= as_of]


def _next_month_str(as_of: str) -> str:
    """as_of 다음 달 "YYYY-MM"."""
    d = datetime.strptime(as_of[:10], "%Y-%m-%d")
    year, month = d.year, d.month + 1
    if month > 12:
        year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


def predict_as_of(
    history: List[Dict],
    as_of: str,
    capital_krw: float = CAPITAL_KRW,
    aggressiveness: str = GRID_AGGRESSIVENESS,
    krw_hold_ratio: float = KRW_HOLD_RATIO,
    use_sigma: Optional[float] = None,        # None=자동, 1.0, 2.0
    horizon_days: int = HORIZON_DAYS,
    lookback: int = 31,
    chart_tail: int = 120,
    manual_box: Optional[Dict[str, float]] = None,  # {"upper":..,"lower":..} 수동 덮어쓰기
) -> PredictionSnapshot:
    """
    as_of 시점까지의 히스토리로 익월 박스권 + 그리드 설정을 예측.
    """
    sliced = _slice_history(history, as_of)
    if len(sliced) < lookback:
        raise ValueError(
            f"as_of={as_of} 이전 데이터가 부족합니다 "
            f"({len(sliced)}일 < 필요 {lookback}일)."
        )

    closes = [c["close"] for c in sliced]
    ref = closes[-1]
    returns = compute_log_returns(closes[-lookback:])
    dsig = daily_volatility(returns)

    u1, l1 = sigma_band(ref, dsig, horizon_days, 1.0, PREDICTION_DRIFT)
    u2, l2 = sigma_band(ref, dsig, horizon_days, 2.0, PREDICTION_DRIFT)

    # σ 레벨 선택
    if use_sigma is not None:
        sigma_used = float(use_sigma)
    else:
        sigma_used = 2.0 if dsig >= _AUTO_SIGMA_VOL_THRESHOLD else 1.0

    rec_u, rec_l = (u1, l1) if sigma_used == 1.0 else (u2, l2)

    # 수동 박스 덮어쓰기 (GUI 미세조정)
    if manual_box:
        rec_u = manual_box.get("upper", rec_u)
        rec_l = manual_box.get("lower", rec_l)

    box_range_pct = (rec_u - rec_l) / rec_l * 100 if rec_l else 0.0
    confidence = containment_probability(sigma_used) * 100
    monthly_sigma_pct = dsig * math.sqrt(horizon_days) * 100
    target_month = _next_month_str(as_of)

    box_pred = BoxPrediction(
        symbol="BTC_KRW",
        reference_price=ref,
        box_upper_1sigma=u1, box_lower_1sigma=l1,
        box_upper_2sigma=u2, box_lower_2sigma=l2,
        recommended_upper=rec_u, recommended_lower=rec_l,
        sigma_level_used=sigma_used,
        box_range_pct=box_range_pct,
        confidence_pct=confidence,
        scenario="AS_OF",
        scenario_probabilities={},
        predicted_for_month=target_month,
        created_at=datetime.now(),
    )

    # 그리드 최적화
    optimizer = GridOptimizerAgent()
    grid = optimizer.optimize(box_pred, dsig, capital_krw, aggressiveness, krw_hold_ratio)

    # 리워드 목표
    reward_agent = RewardCalculatorAgent()
    tier_rec = reward_agent.recommend_target_tier(capital_krw)

    snapshot = PredictionSnapshot(
        as_of_date=as_of,
        target_month=target_month,
        reference_price=ref,
        daily_sigma=dsig,
        monthly_sigma_pct=monthly_sigma_pct,
        box_upper_1s=u1, box_lower_1s=l1,
        box_upper_2s=u2, box_lower_2s=l2,
        recommended_upper=rec_u, recommended_lower=rec_l,
        sigma_level_used=sigma_used,
        box_range_pct=box_range_pct,
        confidence_pct=confidence,
        capital_krw=capital_krw,
        aggressiveness=aggressiveness,
        krw_hold_ratio=krw_hold_ratio,
        grid_interval_pct=grid.grid_interval_pct,
        grid_interval_krw=grid.grid_interval_krw,
        bot_count=grid.bot_count,
        capital_deployed_krw=grid.capital_deployed_krw,
        krw_reserve_krw=grid.krw_reserve_krw,
        capital_per_bot_krw=grid.capital_per_bot_krw,
        estimated_monthly_volume_krw=grid.estimated_monthly_volume_krw,
        estimated_reward_krw=grid.estimated_reward_krw,
        estimated_reward_rate=grid.estimated_reward_rate,
        safe_target=tier_rec["safe"],
        stretch_target=tier_rec["stretch"],
        history_tail=sliced[-chart_tail:],
        box_prediction=box_pred,
    )

    logger.info(
        "[예측서비스] as_of=%s → %s 박스 %.0fσ [%s ~ %s] 폭 %.1f%% / 봇 %d개",
        as_of, target_month, sigma_used,
        f"{rec_l:,.0f}", f"{rec_u:,.0f}", box_range_pct, grid.bot_count,
    )
    return snapshot


def grid_lines(lower: float, upper: float, interval_pct: float, max_lines: int = 200) -> List[float]:
    """박스 [lower, upper]를 기하 간격(interval_pct%)으로 분할한 그리드 라인 가격들."""
    if lower <= 0 or interval_pct <= 0 or upper <= lower:
        return []
    step = 1.0 + interval_pct / 100.0
    lines = []
    price = lower
    while price <= upper and len(lines) < max_lines:
        lines.append(price)
        price *= step
    return lines
