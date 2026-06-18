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
    SIGMA_UP_BTC, SIGMA_DN_BTC, SIGMA_UP_2_BTC, SIGMA_DN_2_BTC,
    SIGMA_UP_USDT, SIGMA_DN_USDT, SIGMA_UP_2_USDT, SIGMA_DN_2_USDT,
    LAYER_A_RATIO_BTC, LAYER_B_RATIO_BTC, LAYER_C_RATIO_BTC,
    LAYER_A_RATIO_USDT, LAYER_B_RATIO_USDT, LAYER_C_RATIO_USDT,
    DCA_TRIGGER_PCT, DCA_TRIGGER_PCT_USDT, DCA_SIZE_EACH_PCT, DEAD_ZONE_RESET_DAYS,
    FX_SIGMA_BLEND_WEIGHT,
)
from models.prediction_result import BoxPrediction
from utils.statistics import (
    compute_log_returns, daily_volatility, ewma_daily_volatility,
    sigma_band, asymmetric_sigma_band, containment_probability,
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

    # 비대칭 그리드 간격
    buy_interval_pct: Optional[float] = None
    sell_interval_pct: Optional[float] = None

    # 비대칭 σ 밴드 (상방/하방 독립 배수)
    sigma_up: float = 1.0
    sigma_dn: float = 1.0
    sigma_up_2: float = 2.0   # 2σ 상방 배수 (1σ×2 아님 — config 독립값)
    sigma_dn_2: float = 2.0   # 2σ 하방 배수
    box_upper_1s_asym: float = 0.0   # asymmetric 1σ 상단
    box_lower_1s_asym: float = 0.0   # asymmetric 1σ 하단
    box_upper_2s_asym: float = 0.0   # asymmetric 2σ 상단
    box_lower_2s_asym: float = 0.0   # asymmetric 2σ 하단

    # 듀얼레이어 자본 배분
    layer_a_krw: float = 0.0    # 1σ 그리드 운용 자본
    layer_b_krw: float = 0.0    # 하단 DCA 예비 현금
    layer_c_krw: float = 0.0    # 2σ 외부 역추세 자본
    dca_levels: List[float] = field(default_factory=list)  # DCA 트리거 가격 리스트
    dead_zone_reset_days: int = 8  # 연속 이탈 후 재설정 권고 기준일


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
    buy_interval_pct: Optional[float] = None,
    sell_interval_pct: Optional[float] = None,
    use_ml_sigma: bool = False,  # ML σ 보정 (라벨 생성 시에는 반드시 False — 순환 학습 방지)
    sigma_up: Optional[float] = None,   # 비대칭 상방 σ 배수 (None=config 기본값)
    sigma_dn: Optional[float] = None,   # 비대칭 하방 σ 배수 (None=config 기본값)
    symbol: str = "BTC",   # "BTC" | "USDT" — σ 기본값 및 레이어 비율 선택
) -> PredictionSnapshot:
    """
    as_of 시점까지의 히스토리로 익월 박스권 + 그리드 설정을 예측.
    use_ml_sigma=True면 학습된 Ridge 모델로 σ를 보정한다
    (백테스트: 박스 적중 71% → 78%).
    sigma_up/sigma_dn: 비대칭 밴드 배수 — None이면 config 기본값(BTC 1.1/1.2) 사용.
    """
    sliced = _slice_history(history, as_of)
    if len(sliced) < lookback:
        raise ValueError(
            f"as_of={as_of} 이전 데이터가 부족합니다 "
            f"({len(sliced)}일 < 필요 {lookback}일)."
        )

    closes = [c["close"] for c in sliced]
    ref = closes[-1]
    # EWMA σ — 최근 90일 수익률 기반, span=60 지수가중 (realized σ 추정 MAE ~13% 개선)
    ewma_lookback = max(lookback, 90)
    returns_ewma = compute_log_returns(closes[-ewma_lookback:])
    dsig_daily = ewma_daily_volatility(returns_ewma, span=60)
    dsig = dsig_daily

    # Tier1: 5분봉 일별 실현변동성(RV) EWMA를 기본 σ로 사용 (검증: 월간 실현 σ MAE −10.6%)
    dsig_intraday: Optional[float] = None
    try:
        from config import (USE_INTRADAY_SIGMA, INTRADAY_RV_EWMA_SPAN,
                            INTRADAY_RV_WINDOW_DAYS, INTRADAY_SIGMA_ML_BLEND)
    except Exception:
        USE_INTRADAY_SIGMA = False
        INTRADAY_SIGMA_ML_BLEND = 0.5
    _market = "KRW-USDT" if symbol.upper() == "USDT" else "KRW-BTC"
    if USE_INTRADAY_SIGMA:
        try:
            from utils.intraday_vol import rv_ewma_daily_sigma
            s5 = rv_ewma_daily_sigma(_market, as_of, span=INTRADAY_RV_EWMA_SPAN,
                                     window_days=INTRADAY_RV_WINDOW_DAYS)
            if s5 and s5 > 0:
                dsig_intraday = s5
                dsig = s5
                logger.info("[예측서비스] 5m RV-EWMA σ: 일봉 %.4f → 5m %.4f", dsig_daily, s5)
        except Exception as e:
            logger.debug("[예측서비스] 5m σ 산출 생략 (일봉 σ 사용): %s", e)

    if use_ml_sigma:
        # ML σ 보정: 모델이 있으면 dsig를 보정 σ로 치환
        # (ML은 일봉 기반 학습 → 학습 일관성 위해 일봉 σ를 특징으로 투입.
        #  5m σ가 있으면 ML 예측과 INTRADAY_SIGMA_ML_BLEND 비율로 블렌드)
        try:
            from utils.ml_sigma import predict_monthly_sigma_pct
            tail31 = closes[-32:]
            ret31 = (tail31[-1] / tail31[0] - 1) * 100 if len(tail31) >= 2 else 0.0
            stat_ms = dsig_daily * math.sqrt(horizon_days) * 100
            ml_ms = predict_monthly_sigma_pct(history, as_of, dsig_daily, stat_ms, ret31)
            if ml_ms is not None and ml_ms > 0:
                ml_dsig = ml_ms / 100 / math.sqrt(horizon_days)
                if dsig_intraday is not None:
                    w = INTRADAY_SIGMA_ML_BLEND
                    dsig = (1 - w) * ml_dsig + w * dsig_intraday
                    logger.info("[예측서비스] σ 블렌드: ML %.1f%% × %.0f%% + 5m %.1f%% × %.0f%%",
                                ml_ms, (1 - w) * 100,
                                dsig_intraday * math.sqrt(horizon_days) * 100, w * 100)
                else:
                    dsig = ml_dsig
                    logger.info("[예측서비스] ML σ 보정: %.1f%% → %.1f%%", stat_ms, ml_ms)
        except Exception as e:
            logger.warning("[예측서비스] ML σ 보정 실패 (통계 σ 사용): %s", e)

    # USDT: 원/달러 환율 σ 블렌딩 — USDT/KRW 표본 부족 보완 (FX 데이터 없으면 무시)
    if symbol.upper() == "USDT" and FX_SIGMA_BLEND_WEIGHT > 0:
        try:
            from utils.fx_data import fx_daily_sigma_asof
            fx_sig = fx_daily_sigma_asof(as_of)
            if fx_sig and fx_sig > 0:
                blended = (1 - FX_SIGMA_BLEND_WEIGHT) * dsig + FX_SIGMA_BLEND_WEIGHT * fx_sig
                logger.info("[예측서비스] USDT σ FX 블렌딩: %.4f → %.4f (FX σ=%.4f)",
                            dsig, blended, fx_sig)
                dsig = blended
        except Exception as e:
            logger.debug("[예측서비스] FX σ 블렌딩 생략: %s", e)

    # 대칭 밴드 (기존 호환용 — 리포트/라벨 생성에 사용)
    u1, l1 = sigma_band(ref, dsig, horizon_days, 1.0, PREDICTION_DRIFT)
    u2, l2 = sigma_band(ref, dsig, horizon_days, 2.0, PREDICTION_DRIFT)

    # 비대칭 밴드 (실운용 권장값) — symbol별 config 기본값 또는 호출자 지정값
    _is_usdt = symbol.upper() == "USDT"
    _default_su = SIGMA_UP_USDT if _is_usdt else SIGMA_UP_BTC
    _default_sd = SIGMA_DN_USDT if _is_usdt else SIGMA_DN_BTC
    _su = sigma_up if sigma_up is not None else _default_su
    _sd = sigma_dn if sigma_dn is not None else _default_sd
    u1a, l1a = asymmetric_sigma_band(ref, dsig, horizon_days, _su, _sd, PREDICTION_DRIFT)
    # 2σ 배수: config 독립 상수 (단순 ×2는 과대 — 스윕 결과 BTC 1.8/2.0, USDT 1.5/1.65)
    # 호출자가 1σ 배수를 덮어쓴 경우 같은 비율로 2σ도 스케일
    _default_su2 = SIGMA_UP_2_USDT if _is_usdt else SIGMA_UP_2_BTC
    _default_sd2 = SIGMA_DN_2_USDT if _is_usdt else SIGMA_DN_2_BTC
    _su2 = _default_su2 * (_su / _default_su) if _default_su else _default_su2
    _sd2 = _default_sd2 * (_sd / _default_sd) if _default_sd else _default_sd2
    u2a, l2a = asymmetric_sigma_band(ref, dsig, horizon_days,
                                     _su2, _sd2, PREDICTION_DRIFT)

    # σ 레벨 선택
    if use_sigma is not None:
        sigma_used = float(use_sigma)
    else:
        sigma_used = 2.0 if dsig >= _AUTO_SIGMA_VOL_THRESHOLD else 1.0

    # 권장 박스: 비대칭 밴드 사용 (대칭 1σ/2σ 대신)
    rec_u, rec_l = (u1a, l1a) if sigma_used == 1.0 else (u2a, l2a)

    # 수동 박스 덮어쓰기 (GUI 미세조정)
    if manual_box:
        rec_u = manual_box.get("upper", rec_u)
        rec_l = manual_box.get("lower", rec_l)

    # 듀얼레이어 자본 배분 (symbol별 비율)
    _la_ratio = LAYER_A_RATIO_USDT if _is_usdt else LAYER_A_RATIO_BTC
    _lb_ratio = LAYER_B_RATIO_USDT if _is_usdt else LAYER_B_RATIO_BTC
    _lc_ratio = LAYER_C_RATIO_USDT if _is_usdt else LAYER_C_RATIO_BTC
    layer_a = capital_krw * _la_ratio
    layer_b = capital_krw * _lb_ratio
    layer_c = capital_krw * _lc_ratio
    # DCA 트리거 가격: 비대칭 1σ 하단 기준 (symbol별 트리거 %)
    _dca_triggers = DCA_TRIGGER_PCT_USDT if _is_usdt else DCA_TRIGGER_PCT
    dca_prices = [round(l1a * (1 + pct / 100)) for pct in _dca_triggers]

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

    # 그리드 최적화: Layer A 자본만 투입 (Layer B/C는 DCA 예비)
    optimizer = GridOptimizerAgent()
    grid = optimizer.optimize(box_pred, dsig, layer_a, aggressiveness, 0.0,
                              buy_interval_pct=buy_interval_pct, sell_interval_pct=sell_interval_pct,
                              as_of=as_of, market=_market)

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
        krw_reserve_krw=layer_b + layer_c,  # B+C 합산 표시
        capital_per_bot_krw=grid.capital_per_bot_krw,
        estimated_monthly_volume_krw=grid.estimated_monthly_volume_krw,
        estimated_reward_krw=grid.estimated_reward_krw,
        estimated_reward_rate=grid.estimated_reward_rate,
        safe_target=tier_rec["safe"],
        stretch_target=tier_rec["stretch"],
        history_tail=sliced[-chart_tail:],
        box_prediction=box_pred,
        buy_interval_pct=grid.buy_interval_pct,
        sell_interval_pct=grid.sell_interval_pct,
        # 비대칭 σ
        sigma_up=_su, sigma_dn=_sd,
        sigma_up_2=_su2, sigma_dn_2=_sd2,
        box_upper_1s_asym=u1a, box_lower_1s_asym=l1a,
        box_upper_2s_asym=u2a, box_lower_2s_asym=l2a,
        # 듀얼레이어 자본 배분
        layer_a_krw=layer_a, layer_b_krw=layer_b, layer_c_krw=layer_c,
        dca_levels=dca_prices,
        dead_zone_reset_days=DEAD_ZONE_RESET_DAYS,
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
