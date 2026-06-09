"""
🎯 오케스트레이터 에이전트 (OrchestratorAgent)
====================================================
역할(사용자 지정): 전체 구조 관리 · 운영지시 · 일정관리.
매월 파이프라인을 지휘한다:
   ① 수집 → ② 분석 → ③ 박스권 예측 → ④ 그리드 최적화 → ⑤ 리워드 → ⑥ 리스크
그리고 한국어 월간 리포트를 생성/저장한다.

사용:
    python main.py                      # 기본 자본(4천만원)으로 익월 예측
    python main.py --capital 60000000   # 자본 변경(재투자 반영)
    python main.py --month 2026-07       # 대상 월 지정
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime

from config import (
    CAPITAL_KRW, FEE_RATE, ROUND_TRIP_FEE, MAX_REWARD_KRW, KRW_HOLD_RATIO,
    MIN_GRID_INTERVAL_PCT, LOG_LEVEL, LOG_FORMAT, REPORT_DIR, HORIZON_DAYS,
    GRID_AGGRESSIVENESS,
)
from agents import (
    DataCollectorAgent, MarketAnalyzerAgent, BoxPredictorAgent,
    GridOptimizerAgent, RewardCalculatorAgent, RiskMonitorAgent,
)
from utils.statistics import (
    compute_log_returns, daily_volatility, boundary_touch_probability,
)

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format=LOG_FORMAT)
logger = logging.getLogger("orchestrator")


def _won(x: float) -> str:
    """원화 가독 포맷 (억/만원 단위 보조 표기)."""
    return f"{x:,.0f}원"


def _eok(x: float) -> str:
    """억원 단위 표기."""
    return f"{x / 1e8:,.1f}억"


class OrchestratorAgent:
    name = "🎯 오케스트레이터"

    def __init__(self, capital_krw: float = CAPITAL_KRW, use_synthetic_fallback: bool = True):
        self.capital_krw = capital_krw
        self.collector = DataCollectorAgent(use_synthetic_fallback=use_synthetic_fallback)
        self.analyzer = MarketAnalyzerAgent()
        self.predictor = BoxPredictorAgent()
        self.optimizer = GridOptimizerAgent()
        self.reward = RewardCalculatorAgent()
        self.risk = RiskMonitorAgent()

    def update_capital(self, new_capital_krw: float) -> None:
        """재투자 반영용 자본 갱신."""
        logger.info("[%s] 자본 갱신: %s → %s", self.name, _won(self.capital_krw), _won(new_capital_krw))
        self.capital_krw = new_capital_krw

    # ------------------------------------------------------------------
    def run_monthly_prediction(
        self, target_month: str = None, save: bool = True,
        aggressiveness: str = GRID_AGGRESSIVENESS,
    ) -> str:
        logger.info("[%s] ====== 월간 박스권 예측 파이프라인 개시 ======", self.name)

        # ① 수집
        market_data = self.collector.collect_all()
        # ② 분석
        analysis = self.analyzer.analyze(market_data)
        # ③ 박스권 예측
        prediction = self.predictor.predict(market_data, analysis, HORIZON_DAYS, target_month)
        # 일간 변동성(그리드 최적화/거래량 추정에 재사용)
        dsig = daily_volatility(compute_log_returns(market_data.get_closes()[-31:]))
        # ④ 그리드 최적화
        grid = self.optimizer.optimize(prediction, dsig, self.capital_krw, aggressiveness)
        # ⑤ 리워드 목표 추천
        tier_rec = self.reward.recommend_target_tier(self.capital_krw)
        # ⑥ 리스크 임계 (현재가 기준 점검)
        breakout = self.risk.check_breakout(market_data.current_price_krw, prediction)

        report = self.generate_report(
            market_data, analysis, prediction, grid, tier_rec, breakout
        )
        print(report)

        if save:
            self._save_report(report, prediction.predicted_for_month)
        logger.info("[%s] ====== 파이프라인 완료 ======", self.name)
        return report

    # ------------------------------------------------------------------
    def generate_report(self, md, an, pred, grid, tier_rec, breakout) -> str:
        L = []
        bar = "═" * 60
        L.append(bar)
        L.append(f"  📊 가상화폐 박스권 예측 월간 리포트  [{pred.predicted_for_month}]")
        L.append(f"  생성: {datetime.now():%Y-%m-%d %H:%M}  |  거래소: 빗썸  |  종목: {md.symbol}")
        L.append(bar)

        # 1. 시장 요약
        L.append("\n[ 1. 시장 요약 ]")
        L.append(f"  • 현재가          : {_won(md.current_price_krw)}")
        L.append(f"  • 글로벌 BTC/USD  : ${md.global_btc_usd:,.0f}  (환율 {md.usd_krw_rate:,.0f})")
        L.append(f"  • 김치 프리미엄    : {an.kimchi_premium_pct:+.2f}%")
        L.append(f"  • 공포·탐욕 지수   : {md.fear_greed_index} ({md.fear_greed_label})")
        L.append(f"  • 30일 변동성(연율): {an.historical_volatility_30d*100:.1f}%")
        L.append(f"  • RSI(14)         : {an.rsi_14:.0f} ({an.rsi_signal})")
        L.append(f"  • 추세            : MA7 {an.ma7_trend} / MA20 {an.ma20_trend}")
        L.append(f"  • 📌 시나리오     : {an.scenario} (확신 {an.scenario_confidence*100:.0f}%)")

        # 2. 박스권 예측
        L.append("\n[ 2. 박스권 예측 (통계 기반 1σ/2σ) ]")
        L.append(f"  • 기준가          : {_won(pred.reference_price)}")
        L.append(f"  • 1σ 박스(68.3%)  : {_won(pred.box_lower_1sigma)} ~ {_won(pred.box_upper_1sigma)}")
        L.append(f"  • 2σ 박스(95.5%)  : {_won(pred.box_lower_2sigma)} ~ {_won(pred.box_upper_2sigma)}")
        L.append(f"  • ✅ 권장 박스    : {_won(pred.recommended_lower)} ~ {_won(pred.recommended_upper)}")
        L.append(f"                     ({pred.sigma_level_used:.0f}σ 채택 / 폭 {pred.box_range_pct:.1f}%)")
        L.append(f"  • 경계 터치확률    : 1σ {boundary_touch_probability(1.0)*100:.0f}% / "
                 f"2σ {boundary_touch_probability(2.0)*100:.0f}%  (높을수록 그리드 체결 빈번)")
        probs = "  ".join(f"{k} {v*100:.0f}%" for k, v in pred.scenario_probabilities.items())
        L.append(f"  • 시나리오 확률    : {probs}")

        # 3. 그리드 설정
        L.append("\n[ 3. 그리드 봇 설정 ]")
        L.append(f"  • 공격성 다이얼    : {grid.aggressiveness}")
        L.append(f"  • 총 자본         : {_won(grid.capital_total_krw)}")
        L.append(f"  • 그리드 투입      : {_won(grid.capital_deployed_krw)} "
                 f"({(1-KRW_HOLD_RATIO)*100:.0f}%)")
        L.append(f"  • 원화 예비(헷지)  : {_won(grid.krw_reserve_krw)} ({KRW_HOLD_RATIO*100:.0f}%)")
        L.append(f"  • 그리드 간격      : {grid.grid_interval_pct:.2f}% ({_won(grid.grid_interval_krw)})")
        L.append(f"  • 봇 수           : {grid.bot_count}개 / 1,000개")
        L.append(f"  • 봇당 자본       : {_won(grid.capital_per_bot_krw)}")
        L.append(f"  • 일 예상 왕복     : {grid.round_trips_per_day:.1f}회/봇")
        L.append(f"  • 📌 수수료 최소간격 제약: {MIN_GRID_INTERVAL_PCT:.2f}% 이상 "
                 f"(왕복수수료 {ROUND_TRIP_FEE*100:.2f}%의 3배)")

        # 4. 리워드 전망
        L.append("\n[ 4. 리워드 전망 (직전월 거래량 기준) ]")
        L.append(f"  • 예상 월 거래량   : {_eok(grid.estimated_monthly_volume_krw)} "
                 f"({_won(grid.estimated_monthly_volume_krw)})")
        L.append(f"  • 도달 구간       : {_eok(grid.estimated_reward_tier_threshold)} 이상 "
                 f"(리워드율 {grid.estimated_reward_rate*100:.3f}%)")
        L.append(f"  • 📌 예상 리워드   : {_won(grid.estimated_reward_krw)} / 월 "
                 f"(상한 {_won(MAX_REWARD_KRW)})")
        safe, stretch = tier_rec["safe"], tier_rec["stretch"]
        L.append(f"  • 안전 목표        : {_eok(safe['threshold'])} → 리워드 {_won(safe['reward'])} "
                 f"(일회전 {safe['req_daily_turnover']:.2f}배)")
        L.append(f"  • 스트레치 목표    : {_eok(stretch['threshold'])} → 리워드 {_won(stretch['reward'])} "
                 f"(일회전 {stretch['req_daily_turnover']:.2f}배)")

        # 4-b. 수수료 vs 리워드 구조 경고
        L.append("\n[ 4-b. ⚠️ 수익 구조 ]")
        L.append(f"  • 왕복 수수료 0.08% > 리워드 최대 0.02% → 리워드는 '수수료 환급' 성격")
        L.append(f"  • 실제 수익원은 그리드 스프레드(매매수익). 간격은 수수료를 넘겨야 함")
        L.append(f"  • 예상 수수료 비용 : {_won(grid.estimated_monthly_volume_krw * FEE_RATE)} / 월")

        # 5. 리스크 임계
        L.append("\n[ 5. 리스크 임계 & 대응 ]")
        L.append(f"  • 현재 상태        : {breakout.severity} "
                 f"({breakout.breakout_direction or 'IN-BOX'}) → {breakout.action_required}")
        L.append(f"  • 정상(1σ 이내)    : {_won(pred.box_lower_1sigma)} ~ {_won(pred.box_upper_1sigma)} → 관망/유지")
        L.append(f"  • 경고(1σ~2σ)      : 모니터링 강화")
        L.append(f"  • 상방 이탈(2σ↑)   : 관망 유지 (추격 금지)")
        L.append(f"  • 하방 이탈(2σ↓)   : 30% 부분손절 + 박스 하향 재설정")

        # 6. 운영 일정 (오케스트레이터)
        L.append("\n[ 6. 운영 일정 (오케스트레이터) ]")
        L.append("  • D-7  : 데이터 수집·예측·파라미터 산출")
        L.append("  • D-1  : 직전월 거래량 확정 → 리워드 구간 확인 → 봇 설정 확정")
        L.append("  • D+0  : 봇 배포 / 그리드 매매 개시")
        L.append("  • 월중 : 일별 거래량 페이스 점검 + 박스 이탈 경보")
        L.append(bar)
        return "\n".join(L)

    # ------------------------------------------------------------------
    def _save_report(self, report: str, month: str) -> None:
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = os.path.join(REPORT_DIR, f"{month}_report.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info("[%s] 리포트 저장: %s", self.name, path)


def main():
    parser = argparse.ArgumentParser(description="가상화폐 박스권 예측 & 그리드 매매 기획 시스템")
    parser.add_argument("--capital", type=float, default=CAPITAL_KRW, help="투입 자본(원)")
    parser.add_argument("--month", type=str, default=None, help="대상 월 (YYYY-MM)")
    parser.add_argument("--aggressiveness", type=str, default=GRID_AGGRESSIVENESS,
                        choices=["conservative", "balanced", "aggressive"],
                        help="그리드 공격성 (기본: balanced)")
    parser.add_argument("--no-save", action="store_true", help="리포트 저장 안 함")
    parser.add_argument("--no-fallback", action="store_true", help="합성 데이터 폴백 비활성화")
    args = parser.parse_args()

    orchestrator = OrchestratorAgent(
        capital_krw=args.capital,
        use_synthetic_fallback=not args.no_fallback,
    )
    orchestrator.run_monthly_prediction(
        target_month=args.month, save=not args.no_save,
        aggressiveness=args.aggressiveness,
    )


if __name__ == "__main__":
    main()
