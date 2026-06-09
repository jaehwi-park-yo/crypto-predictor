"""
복합 전략 에이전트 (CompositeStrategyAgent)
=============================================
BTC-KRW Conservative  +  USDT-KRW Aggressive 조합

설계 의도:
  ① BTC-KRW (보수):  넓은 그리드 간격(1%) → 스프레드 수익 중심, 수수료 절약
  ② USDT-KRW (공격): 1원 단위 촘촘한 그리드 → 대량 거래량으로 리워드 극대화
  전체적으로 '균형(balanced)' 성격이 유지되도록 자본 배분 비율을 조절.

자본 배분:
  기본 60% BTC / 40% USDT (config.COMPOSITE_BTC_RATIO / COMPOSITE_USDT_RATIO)
  각 자산별 KRW 예비금(30%)은 별도 보유.

평가 기준:
  - BTC: 월 그리드 스프레드 순수익 / 자본수익률
  - USDT: 월 리워드 + 스프레드 순수익 / 자본수익률
  - 복합: 합산 수익 / 총자본
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from config import (
    CAPITAL_KRW, KRW_HOLD_RATIO,
    COMPOSITE_BTC_RATIO, COMPOSITE_USDT_RATIO,
    COMPOSITE_BTC_AGGRESSIVENESS, COMPOSITE_USDT_AGGRESSIVENESS,
    USDT_REFERENCE_PRICE_KRW, USDT_DAILY_RANGE_KRW,
    USDT_BUY_INTERVAL_KRW, USDT_SELL_INTERVAL_KRW,
    REWARD_CAP_VOLUME_KRW, MAX_REWARD_KRW,
)
from agents.usdt_grid import UsdtGridAgent, UsdtGridConfig, _calc_reward

logger = logging.getLogger("composite_strategy")


@dataclass
class CompositeResult:
    total_capital_krw: float

    # BTC 레그
    btc_capital_krw: float
    btc_net_grid_krw: float
    btc_fee_krw: float
    btc_reward_krw: float
    btc_total_krw: float
    btc_return_pct: float

    # USDT 레그
    usdt_capital_krw: float
    usdt_net_grid_krw: float
    usdt_fee_krw: float
    usdt_reward_krw: float
    usdt_total_krw: float
    usdt_return_pct: float
    usdt_stop_day: Optional[int]

    # 복합 합계
    combined_net_grid_krw: float
    combined_reward_krw: float
    combined_total_krw: float
    combined_return_pct: float

    # 전략 평가
    evaluation: List[str] = field(default_factory=list)


class CompositeStrategyAgent:
    """BTC conservative + USDT aggressive 복합 전략 분석."""
    name = "복합전략"

    def __init__(self):
        self._usdt = UsdtGridAgent()

    def analyze(
        self,
        total_capital_krw: float = CAPITAL_KRW,
        btc_monthly_net_grid: float = 0.0,    # BTC 레그 월 순수익 (백테스터 결과 입력)
        btc_monthly_fee: float = 0.0,
        btc_monthly_reward: float = 0.0,
        btc_ratio: float = COMPOSITE_BTC_RATIO,
        usdt_ratio: float = COMPOSITE_USDT_RATIO,
        usdt_reference_price: float = USDT_REFERENCE_PRICE_KRW,
        usdt_daily_range: float = USDT_DAILY_RANGE_KRW,
        usdt_buy_interval: float = USDT_BUY_INTERVAL_KRW,
        usdt_sell_interval: float = USDT_SELL_INTERVAL_KRW,  # 1 or 2 KRW
        krw_hold_ratio: float = KRW_HOLD_RATIO,
    ) -> CompositeResult:
        btc_cap = total_capital_krw * btc_ratio
        usdt_cap = total_capital_krw * usdt_ratio

        # BTC 레그 수익 (외부에서 주입 또는 기본 추정치 사용)
        btc_total = btc_monthly_net_grid + btc_monthly_reward
        btc_ret = btc_total / btc_cap * 100 if btc_cap else 0.0

        # USDT 레그 분석
        usdt_cfg = self._usdt.analyze(
            capital_krw=usdt_cap,
            krw_hold_ratio=krw_hold_ratio,
            reference_price=usdt_reference_price,
            daily_range_krw=usdt_daily_range,
            buy_interval_krw=usdt_buy_interval,
            sell_interval_krw=usdt_sell_interval,
        )
        usdt_total = usdt_cfg.monthly_net_krw + usdt_cfg.monthly_reward_krw
        usdt_ret = usdt_total / usdt_cap * 100 if usdt_cap else 0.0

        combined_net = btc_monthly_net_grid + usdt_cfg.monthly_net_krw
        combined_reward = btc_monthly_reward + usdt_cfg.monthly_reward_krw
        combined_total = combined_net + combined_reward
        combined_ret = combined_total / total_capital_krw * 100 if total_capital_krw else 0.0

        evaluation = self._evaluate(
            btc_cap, usdt_cap, btc_ret, usdt_ret, combined_ret,
            usdt_cfg, btc_monthly_net_grid,
        )

        return CompositeResult(
            total_capital_krw=total_capital_krw,
            btc_capital_krw=btc_cap,
            btc_net_grid_krw=btc_monthly_net_grid,
            btc_fee_krw=btc_monthly_fee,
            btc_reward_krw=btc_monthly_reward,
            btc_total_krw=btc_total,
            btc_return_pct=btc_ret,
            usdt_capital_krw=usdt_cap,
            usdt_net_grid_krw=usdt_cfg.monthly_net_krw,
            usdt_fee_krw=usdt_cfg.monthly_fee_krw,
            usdt_reward_krw=usdt_cfg.monthly_reward_krw,
            usdt_total_krw=usdt_total,
            usdt_return_pct=usdt_ret,
            usdt_stop_day=usdt_cfg.recommended_stop_day,
            combined_net_grid_krw=combined_net,
            combined_reward_krw=combined_reward,
            combined_total_krw=combined_total,
            combined_return_pct=combined_ret,
            evaluation=evaluation,
        )

    def format_report(self, r: CompositeResult) -> str:
        L = []
        thin = "─" * 68
        bar = "═" * 68

        L.append(bar)
        L.append("  📊 복합전략 분석: BTC-KRW Conservative + USDT-KRW Aggressive")
        L.append(f"  총 자본: {r.total_capital_krw:,.0f}원")
        L.append(bar)

        # BTC 레그
        L.append(f"\n  [ BTC-KRW 보수 레그 (자본 {r.btc_capital_krw:,.0f}원) ]")
        L.append(f"  그리드 간격: 1.0% (conservative) | 배치: {r.btc_capital_krw * 0.7:,.0f}원")
        L.append(f"  월 스프레드 순수익   : {r.btc_net_grid_krw:,.0f}원")
        L.append(f"  월 리워드            : {r.btc_reward_krw:,.0f}원")
        L.append(f"  BTC 월 합산          : {r.btc_total_krw:,.0f}원 ({r.btc_return_pct:+.2f}%)")

        # USDT 레그
        L.append(f"\n  [ USDT-KRW 공격 레그 (자본 {r.usdt_capital_krw:,.0f}원) ]")
        L.append(f"  그리드 간격: 1원/{r.usdt_capital_krw * 0.7:.0f}원 배치 | 1원 buy / 1원 sell")
        L.append(f"  월 스프레드 순수익   : {r.usdt_net_grid_krw:,.0f}원")
        L.append(f"  월 리워드            : {r.usdt_reward_krw:,.0f}원")
        if r.usdt_stop_day:
            L.append(f"  ⚡ 리워드 캡 도달     : {r.usdt_stop_day}일차 → 봇 중단 권고")
        L.append(f"  USDT 월 합산         : {r.usdt_total_krw:,.0f}원 ({r.usdt_return_pct:+.2f}%)")

        # 복합 합계
        L.append(f"\n  {thin}")
        L.append(f"  [ 복합 합산 ]")
        L.append(f"  스프레드 순수익      : {r.combined_net_grid_krw:,.0f}원")
        L.append(f"  리워드               : {r.combined_reward_krw:,.0f}원")
        L.append(f"  월 총 수익           : {r.combined_total_krw:,.0f}원")
        L.append(f"  총 자본 대비 월수익률: {r.combined_return_pct:+.2f}%")
        L.append(f"  연환산 (단순):        {r.combined_return_pct * 12:+.1f}%")

        # 평가
        if r.evaluation:
            L.append(f"\n  [ 전략 평가 ]")
            for line in r.evaluation:
                L.append(f"  • {line}")
        L.append(bar)
        return "\n".join(L)

    def _evaluate(
        self, btc_cap, usdt_cap, btc_ret, usdt_ret, combined_ret,
        usdt_cfg: UsdtGridConfig, btc_net_grid,
    ) -> List[str]:
        notes = []

        # 수익 구조 평가
        if usdt_cfg.monthly_reward_krw > usdt_cfg.monthly_net_krw:
            notes.append(
                "USDT 레그: 리워드 > 스프레드 순수익 → 리워드가 주 수익원. "
                "리워드 정책 변경 리스크에 주의."
            )
        else:
            notes.append("USDT 레그: 스프레드 순수익 > 리워드 → 수익 구조 건전.")

        # BTC vs USDT 레그 효율
        if usdt_ret > btc_ret and usdt_ret > 0:
            notes.append(
                f"USDT 레그 월 수익률({usdt_ret:.2f}%) > BTC 레그({btc_ret:.2f}%) "
                "→ USDT 비중 확대 검토 가능."
            )
        elif btc_ret > usdt_ret:
            notes.append(
                f"BTC 레그 우위({btc_ret:.2f}% vs {usdt_ret:.2f}%) "
                "→ 현 60/40 배분이 보수적으로 유리."
            )

        # 리워드 캡 운영 전략
        if usdt_cfg.recommended_stop_day:
            notes.append(
                f"USDT {usdt_cfg.recommended_stop_day}일차 캡 도달 후: "
                "남은 자본을 BTC conservative 또는 KRW 예비금으로 전환해 "
                "불필요한 수수료 소비를 막을 것."
            )

        # 복합 수익률 총평
        ann = combined_ret * 12
        if ann >= 15:
            notes.append(f"연환산 {ann:.1f}% — 양호한 복합 수익. 세금·슬리피지 감안 후 실행 검토.")
        elif ann >= 8:
            notes.append(f"연환산 {ann:.1f}% — 적정 수준. BTC 평가손익 시나리오를 별도 분석 권장.")
        else:
            notes.append(
                f"연환산 {ann:.1f}% — 낮음. USDT 일중 변동폭 가정({USDT_DAILY_RANGE_KRW}원) "
                "재점검 또는 자본 확대 필요."
            )

        # 리스크 요소
        notes.append(
            "BTC 하방 이탈 시: 30% 손절(BTC 레그) + KRW 예비금으로 회복 그리드 배치. "
            "USDT는 안정적이므로 독립 운영 유지."
        )
        notes.append(
            "USDT/KRW 패그 리스크: USDT 탈페깅(de-peg) 발생 시 즉시 중단. "
            "1원 그리드는 급락에 취약 (전체 포지션 매수 쏠림 가능)."
        )

        return notes
