"""
🧪 백테스팅 에이전트 (BacktestAgent)
======================================
역할: 과거 월별 데이터로 박스권 예측 전략의 성과를 역산·검증한다.

검증 항목:
  A. 통계 정확도  — 1σ/2σ 박스의 실제 containment 비율 (목표: 68%/95%)
  B. 그리드 수익  — 박스 내 진동에서 발생하는 그리드 스프레드 수익
  C. 리워드 수익  — 예상 거래량 기반 리워드 추정
  D. 리스크 지표  — 이탈 빈도, 최대 이탈폭, 월별 최악 손실 시나리오

방법론:
  - 롤링 윈도우: lookback 30일로 박스 예측 → 이후 horizon 30일 실제 가격으로 검증
  - 그리드 수익 계산: 실제 일봉 고저를 이용한 경로 재현
    grid_profit = Σ(round_trips_per_day × grid_interval × deployed_capital)
    round_trips = min(일중_범위/그리드_간격, 박스_범위/그리드_간격) × efficiency
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import (
    CAPITAL_KRW, KRW_HOLD_RATIO, FEE_RATE, ROUND_TRIP_FEE,
    HORIZON_DAYS, AGGRESSIVENESS_INTERVAL_PCT, GRID_AGGRESSIVENESS,
    MIN_GRID_INTERVAL_PCT, GRID_FILL_EFFICIENCY, DAILY_RANGE_SIGMA_MULT,
    REWARD_TIERS, MAX_REWARD_KRW,
    GRID_BUY_INTERVAL_PCT, GRID_SELL_INTERVAL_PCT, ASYMMETRIC_GRID,
)
from utils.statistics import (
    compute_log_returns, daily_volatility, sigma_band,
    containment_probability,
)

logger = logging.getLogger("backtester")


# ──────────────────────────────────────────────
# 결과 데이터 모델
# ──────────────────────────────────────────────
@dataclass
class MonthlyBacktestResult:
    """단일 월 백테스팅 결과."""
    month: str                    # "YYYY-MM"
    ref_price: float              # 예측 기준가(lookback 마지막 종가)
    actual_open: float            # 실제 월초 가격
    actual_close: float           # 실제 월말 가격
    actual_high: float            # 실제 월중 최고가
    actual_low: float             # 실제 월중 최저가

    # 예측 박스
    box_upper_1s: float
    box_lower_1s: float
    box_upper_2s: float
    box_lower_2s: float

    # containment (해당 월 일봉이 박스 안에 머문 비율)
    containment_1s: float         # 0.0 ~ 1.0
    containment_2s: float

    # 상하단 이탈 여부
    upside_breach_1s: bool
    downside_breach_1s: bool
    upside_breach_2s: bool
    downside_breach_2s: bool
    max_upside_dev_pct: float     # 박스 상단 초과 최대폭(%)
    max_downside_dev_pct: float   # 박스 하단 이탈 최대폭(%)

    # 그리드 수익
    grid_interval_pct: float
    deployed_capital: float
    bot_count: int
    estimated_monthly_volume: float
    grid_profit_krw: float        # 그리드 스프레드 수익(수수료 차감 전)
    fee_cost_krw: float
    net_grid_profit_krw: float    # 수수료 차감 후 순수익

    # 리워드
    reward_krw: float

    # 총 수익
    total_profit_krw: float       # net_grid + reward
    capital_end: float            # 자본 말잔(재투자용)
    return_pct: float             # 총 수익 / 투입자본


@dataclass
class BacktestSummary:
    """전 기간 백테스팅 요약."""
    total_months: int
    start_date: str
    end_date: str

    # 통계 정확도
    avg_containment_1s: float
    avg_containment_2s: float
    months_1s_above_target: int   # containment >= 68.27%인 월수
    months_2s_above_target: int   # containment >= 95.45%인 월수

    # 이탈 통계
    upside_breach_1s_count: int
    downside_breach_1s_count: int
    upside_breach_2s_count: int
    downside_breach_2s_count: int
    avg_max_downside_dev_pct: float

    # 수익 통계
    total_grid_profit: float
    total_fee_cost: float
    total_net_grid: float
    total_reward: float
    total_profit: float
    avg_monthly_profit: float
    best_month_profit: float
    worst_month_profit: float
    win_rate: float               # 수익 > 0 인 월 비율

    # 자본 성장
    initial_capital: float
    final_capital: float
    total_return_pct: float
    annualized_return_pct: float

    monthly_results: List[MonthlyBacktestResult] = field(default_factory=list)


# ──────────────────────────────────────────────
# 핵심 계산 함수
# ──────────────────────────────────────────────

def _grid_profit_from_path(
    daily_ohlcv: List[Dict],
    box_upper: float,
    box_lower: float,
    grid_interval_pct: float,
    deployed: float,
    granularity: str = "daily",
    n_steps: int = 288,
    rng: Optional[np.random.Generator] = None,
    buy_interval_pct: Optional[float] = None,   # 비대칭 그리드: 매수 간격
    sell_interval_pct: Optional[float] = None,  # 비대칭 그리드: 매도 간격
) -> Tuple[float, float, float]:
    """
    그리드 수익·수수료·거래량 추정. 공통 변수 C = '그리드 라인 통과 총수'.

    대칭 그리드 (기본):
        round_trips = C / 2
        수익  = round_trips × gi_pct × cap_per_bot
        수수료 = C × FEE_RATE × cap_per_bot

    비대칭 그리드 (buy_interval ≠ sell_interval):
        교차수 C는 매수 간격 기준 → 이동거리 T ≈ C × buy_gi
        왕복 1회 = (buy + sell) 거리 이동 필요
        round_trips  = T / (buy + sell) = C × buy_gi / (buy_gi + sell_gi)
        수익  = round_trips × sell_interval × cap_per_bot  (매도 폭이 확정 수익)
        수수료 = round_trips × 2 × FEE_RATE × cap_per_bot
        거래량 = round_trips × 2 × cap_per_bot
    """
    box_range_pct = (box_upper - box_lower) / box_lower * 100 if box_lower else 0.0
    asymmetric = (buy_interval_pct is not None and sell_interval_pct is not None)

    if asymmetric:
        buy_gi  = max(buy_interval_pct,  MIN_GRID_INTERVAL_PCT / 2)
        sell_gi = max(sell_interval_pct, MIN_GRID_INTERVAL_PCT / 2)
        gi_pct  = buy_gi   # bot spacing and crossings based on BUY interval
    else:
        gi_pct  = max(grid_interval_pct, MIN_GRID_INTERVAL_PCT)
        buy_gi  = gi_pct
        sell_gi = gi_pct

    bot_count = max(1, min(1000, math.ceil(box_range_pct / gi_pct)))
    cap_per_bot = deployed / bot_count
    max_lines = box_range_pct / gi_pct

    total_crossings = 0.0

    if granularity == "intraday":
        from utils.intraday import generate_intraday_path, count_grid_crossings
        if rng is None:
            rng = np.random.default_rng()
        for candle in daily_ohlcv:
            o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
            if min(h, box_upper) <= max(l, box_lower):
                continue
            path = generate_intraday_path(o, h, l, c, n_steps=n_steps, rng=rng)
            total, net = count_grid_crossings(path, box_lower, box_upper, gi_pct)
            total_crossings += max(0.0, total - net)
    else:  # daily
        for candle in daily_ohlcv:
            c_high, c_low = candle["high"], candle["low"]
            c_close = candle.get("close", (c_high + c_low) / 2)
            eff_high = min(c_high, box_upper)
            eff_low  = max(c_low, box_lower)
            if eff_high <= eff_low:
                continue
            hl_pct = (eff_high - eff_low) / c_close * 100 if c_close else 0.0
            round_trips = min(hl_pct / gi_pct, max_lines) * GRID_FILL_EFFICIENCY
            total_crossings += round_trips * 2

    if asymmetric:
        # 왕복 1회 = 매수+매도 거리 이동 필요 → 교차수에 buy/(buy+sell) 보정
        round_trips = total_crossings * buy_gi / (buy_gi + sell_gi)
        total_profit = round_trips * (sell_gi / 100) * cap_per_bot
        total_fee    = round_trips * 2 * FEE_RATE * cap_per_bot
        total_volume = round_trips * 2 * cap_per_bot
    else:
        round_trips = total_crossings / 2.0
        total_profit = round_trips * (gi_pct / 100) * cap_per_bot
        total_fee    = total_crossings * FEE_RATE * cap_per_bot
        total_volume = total_crossings * cap_per_bot
    return total_profit, total_fee, total_volume


def _reward_from_volume(volume_krw: float) -> float:
    """거래량 → 리워드(원)."""
    for threshold, rate in REWARD_TIERS:
        if volume_krw >= threshold:
            return min(volume_krw * rate, MAX_REWARD_KRW)
    return 0.0


def _containment(
    daily_closes: List[float],
    lower: float,
    upper: float,
) -> Tuple[float, bool, bool, float, float]:
    """
    일봉 종가들이 박스 안에 머문 비율 + 이탈 통계.
    반환: (containment, upside_breach, downside_breach, max_up_dev%, max_down_dev%)
    """
    if not daily_closes:
        return 0.0, False, False, 0.0, 0.0
    n = len(daily_closes)
    n_in = sum(1 for c in daily_closes if lower <= c <= upper)
    max_up = max((c - upper) / upper * 100 for c in daily_closes if c > upper) if any(c > upper for c in daily_closes) else 0.0
    max_down = max((lower - c) / lower * 100 for c in daily_closes if c < lower) if any(c < lower for c in daily_closes) else 0.0
    return (
        n_in / n,
        any(c > upper for c in daily_closes),
        any(c < lower for c in daily_closes),
        max_up,
        max_down,
    )


# ──────────────────────────────────────────────
# 메인 에이전트 클래스
# ──────────────────────────────────────────────

class BacktestAgent:
    """
    과거 일봉 히스토리 전체로 롤링 월간 백테스팅 수행.

    흐름:
      for each month in history:
          lookback 30일 → σ 추정 → 1σ/2σ 박스 예측
          실제 그달 일봉 → containment + 그리드 수익 계산
          리워드 추정 → 총 수익 / 자본 성장
    """
    name = "🧪 백테스터"

    def run(
        self,
        history: List[Dict],
        capital_krw: float = CAPITAL_KRW,
        aggressiveness: str = GRID_AGGRESSIVENESS,
        lookback: int = 30,
        horizon: int = HORIZON_DAYS,
        use_sigma: Optional[float] = None,  # None = 시나리오 자동 선택, 1.0 또는 2.0
        granularity: str = "daily",         # "daily" | "intraday"
        n_steps: int = 288,                 # 일중 경로 분해능 (intraday)
        seed: int = 2024,
        asymmetric: bool = ASYMMETRIC_GRID,
        buy_interval_pct: float = GRID_BUY_INTERVAL_PCT,
        sell_interval_pct: float = GRID_SELL_INTERVAL_PCT,
    ) -> BacktestSummary:

        gi_pct = max(
            AGGRESSIVENESS_INTERVAL_PCT.get(aggressiveness, 0.5),
            MIN_GRID_INTERVAL_PCT,
        )
        # 비대칭 그리드 설정
        _buy_gi  = buy_interval_pct  if asymmetric else None
        _sell_gi = sell_interval_pct if asymmetric else None

        rng = np.random.default_rng(seed)
        asym_label = f"[비대칭 buy={buy_interval_pct}%/sell={sell_interval_pct}%]" if asymmetric else ""
        logger.info(
            "[%s] === 백테스팅 시작: %d일봉 / 자본 %s원 / 간격 %.2f%% / 분해능=%s %s ===",
            self.name, len(history), f"{capital_krw:,.0f}", gi_pct, granularity, asym_label,
        )

        results: List[MonthlyBacktestResult] = []
        capital = capital_krw

        # 충분한 lookback이 확보된 지점부터 시작
        i = lookback
        while i + horizon <= len(history):
            lb = history[i - lookback: i]
            test = history[i: i + horizon]

            lb_closes = [c["close"] for c in lb]
            test_closes = [c["close"] for c in test]
            test_ohlcv = test

            if len(lb_closes) < 10 or not test_closes:
                i += horizon
                continue

            returns = compute_log_returns(lb_closes)
            dsig = daily_volatility(returns)
            ref = lb_closes[-1]

            # σ 수준 결정 (기본: 1σ, 변동성 높으면 2σ)
            if use_sigma is not None:
                sigma = use_sigma
            else:
                sigma = 2.0 if dsig >= 0.045 else 1.0

            u1, l1 = sigma_band(ref, dsig, horizon, 1.0)
            u2, l2 = sigma_band(ref, dsig, horizon, 2.0)

            if sigma == 1.0:
                rec_u, rec_l = u1, l1
            else:
                rec_u, rec_l = u2, l2

            # containment
            cont1, up1, dn1, mud1, mdd1 = _containment(test_closes, l1, u1)
            cont2, up2, dn2, mud2, mdd2 = _containment(test_closes, l2, u2)

            # 그리드 수익 (권장 박스 기준)
            deployed = capital * (1 - KRW_HOLD_RATIO)
            box_range_pct = (rec_u - rec_l) / rec_l * 100 if rec_l else 0.0
            bot_count = max(1, min(1000, math.ceil(box_range_pct / gi_pct)))

            grid_profit, fee_cost, volume = _grid_profit_from_path(
                test_ohlcv, rec_u, rec_l, gi_pct, deployed,
                granularity=granularity, n_steps=n_steps, rng=rng,
                buy_interval_pct=_buy_gi, sell_interval_pct=_sell_gi,
            )
            net_grid = grid_profit - fee_cost
            reward = _reward_from_volume(volume)
            total = net_grid + reward
            capital_end = capital + total
            ret_pct = total / capital * 100 if capital else 0.0

            month_str = test[0]["date"][:7]
            results.append(MonthlyBacktestResult(
                month=month_str,
                ref_price=ref,
                actual_open=test_closes[0],
                actual_close=test_closes[-1],
                actual_high=max(c["high"] for c in test_ohlcv),
                actual_low=min(c["low"] for c in test_ohlcv),
                box_upper_1s=u1, box_lower_1s=l1,
                box_upper_2s=u2, box_lower_2s=l2,
                containment_1s=cont1, containment_2s=cont2,
                upside_breach_1s=up1, downside_breach_1s=dn1,
                upside_breach_2s=up2, downside_breach_2s=dn2,
                max_upside_dev_pct=mud1, max_downside_dev_pct=mdd1,
                grid_interval_pct=gi_pct,
                deployed_capital=deployed,
                bot_count=bot_count,
                estimated_monthly_volume=volume,
                grid_profit_krw=grid_profit,
                fee_cost_krw=fee_cost,
                net_grid_profit_krw=net_grid,
                reward_krw=reward,
                total_profit_krw=total,
                capital_end=capital_end,
                return_pct=ret_pct,
            ))

            capital = capital_end  # 재투자
            i += horizon

        return self._summarize(results, capital_krw)

    # ──────────────────────────────────────────────
    def _summarize(
        self, results: List[MonthlyBacktestResult], initial_capital: float
    ) -> BacktestSummary:
        if not results:
            return BacktestSummary(
                total_months=0, start_date="", end_date="",
                avg_containment_1s=0, avg_containment_2s=0,
                months_1s_above_target=0, months_2s_above_target=0,
                upside_breach_1s_count=0, downside_breach_1s_count=0,
                upside_breach_2s_count=0, downside_breach_2s_count=0,
                avg_max_downside_dev_pct=0, total_grid_profit=0,
                total_fee_cost=0, total_net_grid=0, total_reward=0,
                total_profit=0, avg_monthly_profit=0, best_month_profit=0,
                worst_month_profit=0, win_rate=0,
                initial_capital=initial_capital, final_capital=initial_capital,
                total_return_pct=0, annualized_return_pct=0,
            )

        TARGET_1S = containment_probability(1.0)
        TARGET_2S = containment_probability(2.0)

        n = len(results)
        profits = [r.total_profit_krw for r in results]
        final_capital = results[-1].capital_end

        total_months_years = n / 12

        total_profit = sum(profits)
        total_return_pct = (final_capital - initial_capital) / initial_capital * 100
        # 연율화 (CAGR 근사)
        if total_months_years > 0:
            ann_ret = ((final_capital / initial_capital) ** (1 / total_months_years) - 1) * 100
        else:
            ann_ret = 0.0

        return BacktestSummary(
            total_months=n,
            start_date=results[0].month,
            end_date=results[-1].month,

            avg_containment_1s=np.mean([r.containment_1s for r in results]),
            avg_containment_2s=np.mean([r.containment_2s for r in results]),
            months_1s_above_target=sum(1 for r in results if r.containment_1s >= TARGET_1S),
            months_2s_above_target=sum(1 for r in results if r.containment_2s >= TARGET_2S),

            upside_breach_1s_count=sum(r.upside_breach_1s for r in results),
            downside_breach_1s_count=sum(r.downside_breach_1s for r in results),
            upside_breach_2s_count=sum(r.upside_breach_2s for r in results),
            downside_breach_2s_count=sum(r.downside_breach_2s for r in results),
            avg_max_downside_dev_pct=np.mean([r.max_downside_dev_pct for r in results]),

            total_grid_profit=sum(r.grid_profit_krw for r in results),
            total_fee_cost=sum(r.fee_cost_krw for r in results),
            total_net_grid=sum(r.net_grid_profit_krw for r in results),
            total_reward=sum(r.reward_krw for r in results),
            total_profit=total_profit,
            avg_monthly_profit=np.mean(profits),
            best_month_profit=max(profits),
            worst_month_profit=min(profits),
            win_rate=sum(1 for p in profits if p > 0) / n,

            initial_capital=initial_capital,
            final_capital=final_capital,
            total_return_pct=total_return_pct,
            annualized_return_pct=ann_ret,

            monthly_results=results,
        )
