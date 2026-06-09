"""
USDT-KRW 1원 그리드 전략 에이전트
===================================
역할:
  - USDT/KRW 시장에서 1원 단위 초단기 그리드로 대량 거래량 생성 → 리워드 극대화
  - 매수/매도 간격 비대칭 지원: 1원 매수 / 2원 매도 (수익↑ / 거래량↓)
  - 월간 거래량이 리워드 캡 기준(150억)에 도달하면 봇 중단 권고
  - BTC conservative 전략과 짝을 이뤄 복합전략을 구성

핵심 공식 (비대칭 그리드):
  avg_interval = (buy_interval + sell_interval) / 2
  round_trips  = daily_range / avg_interval × efficiency
  profit_per_rt = sell_interval / price   (매도 레그에서 확정)
  fee_per_rt   = 2 × FEE_RATE × cap_per_bot  (매수 + 매도 각 1회)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import (
    FEE_RATE, KRW_HOLD_RATIO,
    USDT_REFERENCE_PRICE_KRW, USDT_DAILY_RANGE_KRW,
    USDT_BUY_INTERVAL_KRW, USDT_SELL_INTERVAL_KRW,
    REWARD_TIERS, MAX_REWARD_KRW,
    REWARD_CAP_VOLUME_KRW, REWARD_CAP_STOP_BOTS,
    TRADING_DAYS_PER_MONTH, GRID_FILL_EFFICIENCY,
)

logger = logging.getLogger("usdt_grid")


@dataclass
class UsdtGridConfig:
    """USDT-KRW 1원 그리드 파라미터."""
    capital_krw: float
    deployed_krw: float
    reference_price_krw: float

    buy_interval_krw: float        # 매수 간격 (원)
    sell_interval_krw: float       # 매도 간격 (원)
    buy_interval_pct: float        # 매수 간격 (%)
    sell_interval_pct: float       # 매도 간격 (%)
    avg_interval_pct: float        # 평균 간격 (%)

    estimated_range_krw: float     # 예상 일중 변동폭 (원)
    bot_count: int                 # 봇 수 (박스폭 / 매수간격)

    daily_round_trips: float       # 일 예상 왕복 수
    monthly_volume_krw: float      # 월 예상 거래량
    monthly_profit_krw: float      # 월 그리드 스프레드 수익
    monthly_fee_krw: float         # 월 수수료
    monthly_net_krw: float         # 월 순수익
    monthly_reward_krw: float      # 월 리워드

    reward_cap_reached: bool       # 리워드 캡 도달 여부
    recommended_stop_day: Optional[int]  # 몇 일차에 봇 중단 권고 (None=도달 못 함)

    # 대안 시나리오 (1원 매수 / 2원 매도)
    alt_monthly_profit_krw: float = 0.0
    alt_monthly_volume_krw: float = 0.0
    alt_monthly_net_krw: float = 0.0
    notes: List[str] = field(default_factory=list)


class UsdtGridAgent:
    """USDT-KRW 1원 그리드 전략 분석 에이전트."""
    name = "USDT-KRW 그리드"

    def analyze(
        self,
        capital_krw: float,
        krw_hold_ratio: float = KRW_HOLD_RATIO,
        reference_price: float = USDT_REFERENCE_PRICE_KRW,
        daily_range_krw: float = USDT_DAILY_RANGE_KRW,
        buy_interval_krw: float = USDT_BUY_INTERVAL_KRW,
        sell_interval_krw: float = USDT_SELL_INTERVAL_KRW,
        box_range_krw: float = 40.0,   # 월간 예상 USDT 박스 폭 (원)
    ) -> UsdtGridConfig:

        deployed = capital_krw * (1 - krw_hold_ratio)
        avg_interval_krw = (buy_interval_krw + sell_interval_krw) / 2.0

        buy_pct  = buy_interval_krw  / reference_price * 100
        sell_pct = sell_interval_krw / reference_price * 100
        avg_pct  = avg_interval_krw  / reference_price * 100

        # 봇 수: 박스폭 / 매수간격
        bots = max(1, math.ceil(box_range_krw / buy_interval_krw))
        cap_per_bot = deployed / bots

        # 일 왕복 수 (비대칭 공식):
        #   round_trips = (daily_range / avg_interval) × efficiency
        #   day_range = daily_range_krw / reference_price × 100 → %
        daily_range_pct = daily_range_krw / reference_price * 100
        daily_rt = (daily_range_pct / avg_pct) * GRID_FILL_EFFICIENCY

        monthly_rt = daily_rt * TRADING_DAYS_PER_MONTH

        # 거래량: 매수 + 매도 각 1회 = 2 × round_trips × cap_per_bot × bots
        monthly_volume = monthly_rt * 2 * cap_per_bot * bots

        # 스프레드 수익: sell_interval 폭이 확정 수익
        profit_per_rt = (sell_pct / 100) * cap_per_bot
        monthly_profit = monthly_rt * profit_per_rt * bots

        # 수수료: 2회(매수+매도) × FEE_RATE × cap_per_bot
        monthly_fee = monthly_rt * 2 * FEE_RATE * cap_per_bot * bots
        monthly_net = monthly_profit - monthly_fee

        # 리워드 계산
        monthly_reward = _calc_reward(monthly_volume)

        # 리워드 캡 도달 판단
        cap_reached = monthly_volume >= REWARD_CAP_VOLUME_KRW
        stop_day = None
        if REWARD_CAP_STOP_BOTS:
            # 캡 도달까지 걸리는 일수 추정
            daily_vol = monthly_volume / TRADING_DAYS_PER_MONTH
            if daily_vol > 0:
                days_to_cap = REWARD_CAP_VOLUME_KRW / daily_vol
                if days_to_cap <= TRADING_DAYS_PER_MONTH:
                    stop_day = int(math.ceil(days_to_cap))

        # 대안 시나리오: 1원 매수 / 2원 매도
        alt_buy_krw, alt_sell_krw = buy_interval_krw, 2.0
        alt_avg_pct = ((alt_buy_krw + alt_sell_krw) / 2) / reference_price * 100
        alt_sell_pct = alt_sell_krw / reference_price * 100
        alt_daily_rt = (daily_range_pct / alt_avg_pct) * GRID_FILL_EFFICIENCY
        alt_monthly_rt = alt_daily_rt * TRADING_DAYS_PER_MONTH
        alt_vol = alt_monthly_rt * 2 * cap_per_bot * bots
        alt_profit = alt_monthly_rt * (alt_sell_pct / 100) * cap_per_bot * bots
        alt_fee = alt_monthly_rt * 2 * FEE_RATE * cap_per_bot * bots
        alt_net = alt_profit - alt_fee

        notes = _build_notes(
            buy_interval_krw, sell_interval_krw, monthly_volume,
            monthly_net, monthly_reward, stop_day, cap_per_bot,
            alt_net, alt_vol,
        )

        config = UsdtGridConfig(
            capital_krw=capital_krw,
            deployed_krw=deployed,
            reference_price_krw=reference_price,
            buy_interval_krw=buy_interval_krw,
            sell_interval_krw=sell_interval_krw,
            buy_interval_pct=buy_pct,
            sell_interval_pct=sell_pct,
            avg_interval_pct=avg_pct,
            estimated_range_krw=daily_range_krw,
            bot_count=bots,
            daily_round_trips=daily_rt,
            monthly_volume_krw=monthly_volume,
            monthly_profit_krw=monthly_profit,
            monthly_fee_krw=monthly_fee,
            monthly_net_krw=monthly_net,
            monthly_reward_krw=monthly_reward,
            reward_cap_reached=cap_reached,
            recommended_stop_day=stop_day,
            alt_monthly_profit_krw=alt_profit,
            alt_monthly_volume_krw=alt_vol,
            alt_monthly_net_krw=alt_net,
            notes=notes,
        )

        logger.info(
            "[%s] USDT 1원 그리드: 봇 %d개 / 월거래량 %s억원 / 월순익 %s원 / 리워드 %s원",
            self.name, bots,
            f"{monthly_volume / 1e8:.1f}",
            f"{monthly_net:,.0f}",
            f"{monthly_reward:,.0f}",
        )
        return config

    def format_report(self, cfg: UsdtGridConfig) -> str:
        L = []
        thin = "─" * 60
        L.append("[ USDT-KRW 1원 그리드 전략 분석 ]")
        L.append(thin)
        L.append(f"  참고 USDT/KRW 가격     : {cfg.reference_price_krw:,.0f}원")
        L.append(f"  배치 자본              : {cfg.deployed_krw:,.0f}원")
        L.append(f"  봇 수                 : {cfg.bot_count}개")
        L.append(f"  매수 간격             : {cfg.buy_interval_krw:.0f}원 ({cfg.buy_interval_pct:.4f}%)")
        L.append(f"  매도 간격             : {cfg.sell_interval_krw:.0f}원 ({cfg.sell_interval_pct:.4f}%)")
        L.append(f"  평균 간격             : {cfg.avg_interval_pct:.4f}%")
        L.append(thin)
        L.append(f"  일 예상 왕복 수        : {cfg.daily_round_trips:.1f}회")
        L.append(f"  월 예상 거래량         : {cfg.monthly_volume_krw / 1e8:.1f}억원")
        L.append(f"  월 스프레드 수익       : {cfg.monthly_profit_krw:,.0f}원")
        L.append(f"  월 수수료             : {cfg.monthly_fee_krw:,.0f}원")
        L.append(f"  월 순수익             : {cfg.monthly_net_krw:,.0f}원")
        L.append(f"  월 리워드             : {cfg.monthly_reward_krw:,.0f}원")
        L.append(f"  합계 (순익+리워드)     : {cfg.monthly_net_krw + cfg.monthly_reward_krw:,.0f}원")
        L.append(thin)

        if cfg.recommended_stop_day:
            L.append(
                f"  ⚡ 리워드 캡(150억) 도달 예상: {cfg.recommended_stop_day}일차"
                f" → 이후 봇 중단 권고"
            )
        else:
            L.append(f"  ℹ️  이 자본으로는 리워드 캡(150억) 미도달")

        L.append("")
        L.append("  [ 대안: 1원 매수 / 2원 매도 비교 ]")
        L.append(f"  거래량  : {cfg.alt_monthly_volume_krw / 1e8:.1f}억원  (vs {cfg.monthly_volume_krw / 1e8:.1f}억원)")
        L.append(f"  월 순익 : {cfg.alt_monthly_net_krw:,.0f}원  (vs {cfg.monthly_net_krw:,.0f}원)")
        delta = cfg.alt_monthly_net_krw - cfg.monthly_net_krw
        L.append(f"  수익 차 : {delta:+,.0f}원 {'(2원매도 유리)' if delta > 0 else '(1원매도 유리)'}")

        if cfg.notes:
            L.append("")
            L.append("  [ 시사점 ]")
            for note in cfg.notes:
                L.append(f"  • {note}")
        return "\n".join(L)


def _calc_reward(volume_krw: float) -> float:
    for threshold, rate in REWARD_TIERS:
        if volume_krw >= threshold:
            return min(volume_krw * rate, MAX_REWARD_KRW)
    return 0.0


def _build_notes(
    buy_int, sell_int, volume, net, reward, stop_day,
    cap_per_bot, alt_net, alt_vol,
) -> List[str]:
    notes = []
    if buy_int < sell_int:
        notes.append(
            f"매도 간격({sell_int:.0f}원)이 매수({buy_int:.0f}원)보다 넓어 "
            "완성 사이클당 수익이 크지만 왕복 속도는 느림."
        )
    fee_ratio = (2 * FEE_RATE * cap_per_bot) / (sell_int / USDT_REFERENCE_PRICE_KRW * cap_per_bot)
    if fee_ratio > 0.3:
        notes.append(
            f"수수료/수익 비율 {fee_ratio:.0%} — 스프레드가 수수료 대비 얇음. "
            "리워드가 순수익의 주요 원천."
        )
    if stop_day:
        notes.append(
            f"{stop_day}일차 이후 봇 중단: 추가 거래량은 리워드 없음."
            " 중단 후 자본을 BTC 쪽으로 전환 고려."
        )
    if volume >= REWARD_CAP_VOLUME_KRW * 0.5:
        notes.append("월 거래량이 리워드 캡의 50% 이상 — 상위 티어 리워드 달성 유력.")
    if alt_net > net * 1.1:
        notes.append(
            f"1원 매수 / 2원 매도 전환 시 수익 {alt_net - net:+,.0f}원 개선 "
            f"(거래량 {alt_vol / 1e8:.1f}억원으로 감소 감수)."
        )
    return notes
