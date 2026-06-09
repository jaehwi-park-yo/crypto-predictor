"""
🧪 백테스팅 실행기 (백테스트 전용 진입점)
========================================
사용:
    python backtest.py                          # 기본 설정
    python backtest.py --capital 60000000       # 자본 변경
    python backtest.py --aggressiveness aggressive
    python backtest.py --sigma 1               # σ 레벨 고정
    python backtest.py --save                  # CSV 저장

출력:
    - 콘솔: 월별 상세 테이블 + 전체 요약
    - (--save) reports/backtest_{date}.csv / reports/backtest_{date}_summary.txt
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime

from config import (
    CAPITAL_KRW, LOG_LEVEL, LOG_FORMAT, REPORT_DIR, GRID_AGGRESSIVENESS,
    AGGRESSIVENESS_INTERVAL_PCT, MIN_GRID_INTERVAL_PCT,
    GRID_BUY_INTERVAL_PCT, GRID_SELL_INTERVAL_PCT,
    COMPOSITE_BTC_RATIO, COMPOSITE_USDT_RATIO,
    USDT_REFERENCE_PRICE_KRW, USDT_DAILY_RANGE_KRW,
    USDT_BUY_INTERVAL_KRW, USDT_SELL_INTERVAL_KRW,
)
from utils.historical_data import fetch_max_history
from agents.backtester import BacktestAgent, BacktestSummary
from agents.composite_strategy import CompositeStrategyAgent

logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)
logger = logging.getLogger("backtest_runner")


def _won(x: float) -> str:
    return f"{x:,.0f}원"


def _pct(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def _eok(x: float) -> str:
    return f"{x / 1e8:,.1f}억"


def generate_report(summary: BacktestSummary, aggressiveness: str, granularity: str = "daily") -> str:
    L = []
    bar = "═" * 72
    thin = "─" * 72

    gran_label = {"daily": "일봉 휴리스틱", "intraday": "분 경로 합성(미세진동 반영)"}.get(granularity, granularity)
    L.append(bar)
    L.append("  🧪 BTC/KRW 박스권 그리드 전략 백테스팅 결과 리포트")
    L.append(f"  기간: {summary.start_date} ~ {summary.end_date}  ({summary.total_months}개월)")
    L.append(f"  자본: {_won(summary.initial_capital)}  |  공격성: {aggressiveness}  |  분해능: {gran_label}")
    L.append(bar)

    # ── A. 통계 정확도 ──────────────────────────────────────
    L.append("\n[ A. 박스권 통계 정확도 (이론 vs 실제) ]")
    L.append(f"  {'':20s} {'이론(이상적)':>14s} {'실제(측정)':>14s} {'평가':>10s}")
    L.append("  " + thin[:60])
    c1_ok = summary.avg_containment_1s >= 0.55   # 허용 기준 (이론보다 낮을 수 있음)
    c2_ok = summary.avg_containment_2s >= 0.80
    L.append(
        f"  {'1σ 박스 containment':20s}"
        f" {'68.27%':>14s}"
        f" {summary.avg_containment_1s*100:>13.1f}%"
        f" {'✅' if c1_ok else '⚠️':>10s}"
    )
    L.append(
        f"  {'2σ 박스 containment':20s}"
        f" {'95.45%':>14s}"
        f" {summary.avg_containment_2s*100:>13.1f}%"
        f" {'✅' if c2_ok else '⚠️':>10s}"
    )
    L.append(f"\n  1σ 목표(≥68%) 달성 월: {summary.months_1s_above_target}/{summary.total_months}")
    L.append(f"  2σ 목표(≥95%) 달성 월: {summary.months_2s_above_target}/{summary.total_months}")

    # ── B. 이탈 통계 ────────────────────────────────────────
    L.append("\n[ B. 박스권 이탈 통계 ]")
    n = summary.total_months
    L.append(f"  1σ 상방 이탈: {summary.upside_breach_1s_count}회 ({summary.upside_breach_1s_count/n*100:.0f}%)  → 관망 유지")
    L.append(f"  1σ 하방 이탈: {summary.downside_breach_1s_count}회 ({summary.downside_breach_1s_count/n*100:.0f}%)  → 모니터링")
    L.append(f"  2σ 상방 이탈: {summary.upside_breach_2s_count}회 ({summary.upside_breach_2s_count/n*100:.0f}%)  → 관망 유지")
    L.append(f"  2σ 하방 이탈: {summary.downside_breach_2s_count}회 ({summary.downside_breach_2s_count/n*100:.0f}%)  → 부분손절+재설정")
    L.append(f"  평균 최대 하방 이탈폭: {summary.avg_max_downside_dev_pct:.2f}%")

    # ── C. 수익 요약 ────────────────────────────────────────
    L.append("\n[ C. 수익 요약 (자본 재투자 포함) ]")
    L.append(f"  • 총 그리드 스프레드 수익: {_won(summary.total_grid_profit)}")
    L.append(f"  • 총 수수료 비용         : {_won(summary.total_fee_cost)}")
    L.append(f"  • 총 순 그리드 수익      : {_won(summary.total_net_grid)}")
    L.append(f"  • 총 리워드              : {_won(summary.total_reward)}")
    L.append(f"  • 합산 총 수익           : {_won(summary.total_profit)}")
    L.append(thin)
    L.append(f"  • 월평균 수익            : {_won(summary.avg_monthly_profit)}")
    L.append(f"  • 최고 월 수익           : {_won(summary.best_month_profit)}")
    L.append(f"  • 최악 월 수익           : {_won(summary.worst_month_profit)}")
    L.append(f"  • 승률 (수익>0 월)       : {summary.win_rate*100:.0f}%")

    # ── D. 자본 성장 ─────────────────────────────────────────
    L.append("\n[ D. 자본 성장 ]")
    L.append(f"  • 초기 자본              : {_won(summary.initial_capital)}")
    L.append(f"  • 최종 자본(재투자)       : {_won(summary.final_capital)}")
    L.append(f"  • 전체 수익률             : {_pct(summary.total_return_pct)}")
    L.append(f"  • 연환산 수익률(CAGR)     : {_pct(summary.annualized_return_pct)}")

    # ── E. 월별 상세 ─────────────────────────────────────────
    L.append("\n[ E. 월별 상세 ]")
    hdr = (
        f"  {'월':7s} {'기준가':>13s} {'박스1σ상':>13s} {'박스1σ하':>13s} "
        f"{'C-1σ':>6s} {'C-2σ':>6s} {'하방이탈':>8s} "
        f"{'그리드순익':>12s} {'리워드':>10s} {'총수익':>12s} {'월수익률':>8s}"
    )
    L.append(hdr)
    L.append("  " + thin)
    for r in summary.monthly_results:
        dn_flag = "⚠️ " if r.downside_breach_2s else ("△ " if r.downside_breach_1s else "   ")
        L.append(
            f"  {r.month:7s}"
            f" {r.ref_price:>13,.0f}"
            f" {r.box_upper_1s:>13,.0f}"
            f" {r.box_lower_1s:>13,.0f}"
            f" {r.containment_1s*100:>5.0f}%"
            f" {r.containment_2s*100:>5.0f}%"
            f" {dn_flag:>8s}"
            f" {r.net_grid_profit_krw:>12,.0f}"
            f" {r.reward_krw:>10,.0f}"
            f" {r.total_profit_krw:>12,.0f}"
            f" {r.return_pct:>+7.2f}%"
        )

    # ── F. 해석 ──────────────────────────────────────────────
    L.append("\n[ F. 핵심 해석 및 시사점 ]")
    if summary.avg_containment_1s < 0.60:
        L.append("  ⚠️  1σ containment가 60% 미만 → BTC의 팻테일(급변) 특성상 정상 범위.")
        L.append("      그리드 간격 확대 또는 2σ 박스 우선 채택 검토.")
    else:
        L.append("  ✅ 1σ containment 양호 → 대부분의 월에 박스 내 진동이 발생해 그리드 체결 원활.")

    if summary.downside_breach_2s_count > 0:
        L.append(
            f"  ⚠️  2σ 하방 이탈 {summary.downside_breach_2s_count}회 → 부분손절 발동 가능성 있는 달 존재."
        )
        L.append("      하방 이탈 달의 손실은 별도 시나리오 분석 필요 (본 모델은 부분손절 수익 미반영).")

    if summary.annualized_return_pct > 0:
        L.append(
            f"  📈 연환산 CAGR {_pct(summary.annualized_return_pct)} (그리드+리워드, 부분손절 미포함)."
        )
        L.append("      실제 수익은 그리드 스프레드가 주, 리워드는 보조 현금흐름.")

    L.append("\n[ G. 그리드 간격별 수익 구조 해석 (중요) ]")
    L.append("  수익 = round_trips × 간격 × 봇당자본 = HL폭 × 봇당자본 × 효율 (간격 상쇄)")
    L.append("  수수료 = round_trips × 왕복수수료 × 봇당자본  ← 간격이 클수록 유리")
    L.append("  → 넓은 간격(conservative): 수수료 부담↓, 봇당자본↑ → 같은 HL에서 수익↑")
    L.append("  → 좁은 간격(aggressive):  수수료 부담↑, 봇당자본↓ → 일봉 시뮬에서 불리")
    L.append("")
    L.append("  [주의] 이 시뮬레이션은 일봉 데이터 기반으로, 분봉 이하 미세 진동을 반영 못 함.")
    L.append("        실제로는 aggressive(좁은 간격)가 미세 진동을 더 많이 잡아 차이가 줄어듦.")
    L.append("        또한 BTC 가격 등락에 따른 포지션 평가손익(미실현손익)은 미포함.")
    L.append("        수치는 '그리드 스프레드 순수익'만의 참고치로 활용할 것.")

    L.append(bar)
    return "\n".join(L)


def save_csv(summary: BacktestSummary, path: str) -> None:
    fieldnames = [
        "month", "ref_price", "actual_open", "actual_close",
        "actual_high", "actual_low",
        "box_upper_1s", "box_lower_1s", "box_upper_2s", "box_lower_2s",
        "containment_1s", "containment_2s",
        "upside_breach_1s", "downside_breach_1s",
        "upside_breach_2s", "downside_breach_2s",
        "max_upside_dev_pct", "max_downside_dev_pct",
        "grid_interval_pct", "bot_count", "deployed_capital",
        "estimated_monthly_volume",
        "grid_profit_krw", "fee_cost_krw", "net_grid_profit_krw",
        "reward_krw", "total_profit_krw", "capital_end", "return_pct",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in summary.monthly_results:
            writer.writerow({k: getattr(r, k) for k in fieldnames})
    logger.info("CSV 저장: %s", path)


def run_sweep(history, capital: float, sigma) -> str:
    """반응빈도(n_steps) × 공격성 민감도 매트릭스 — 분해능 의존성을 명시적으로 노출."""
    agent = BacktestAgent()
    levels = ["conservative", "balanced", "aggressive"]
    grids = {"conservative": "1.0%", "balanced": "0.5%", "aggressive": "0.3%"}
    # (라벨, granularity, n_steps)
    resolutions = [
        ("일봉(하한)", "daily", 0),
        ("60분(24)", "intraday", 24),
        ("30분(48)", "intraday", 48),
        ("15분(96)", "intraday", 96),
        ("5분(288)", "intraday", 288),
    ]
    # 결과 캐시: cagr[res][level]
    cagr = {}
    for label, gran, steps in resolutions:
        cagr[label] = {}
        for lvl in levels:
            s = agent.run(
                history=history, capital_krw=capital, aggressiveness=lvl,
                use_sigma=sigma, granularity=gran, n_steps=max(steps, 2),
            )
            cagr[label][lvl] = s.annualized_return_pct

    L = []
    bar = "═" * 72
    L.append(bar)
    L.append("  🧪 민감도 매트릭스: 봇 반응빈도(행) × 그리드 공격성(열)")
    L.append("  값 = 연환산 CAGR % (그리드 스프레드 + 리워드, BTC 평가손익 미포함)")
    L.append(bar)
    header = f"  {'반응빈도':12s}" + "".join(
        f"{lvl[:4]+'('+grids[lvl]+')':>16s}" for lvl in levels
    )
    L.append(header)
    L.append("  " + "─" * 66)
    for label, _, _ in resolutions:
        row = f"  {label:12s}" + "".join(
            f"{cagr[label][lvl]:>15.1f}%" for lvl in levels
        )
        L.append(row)
    L.append(bar)
    L.append("\n[ 해석 ]")
    L.append("  1) 같은 행(반응빈도 고정)에서 → conservative(넓은간격)일수록 CAGR↑")
    L.append("     : 일봉/저빈도에서는 넓은 간격이 수수료를 아껴 유리.")
    L.append("  2) 행을 내려갈수록(고빈도) → 모든 열의 CAGR이 √빈도에 비례해 급증")
    L.append("     : 미세진동을 더 잡지만, 이는 합성 브라운 경로의 분해능 artifact.")
    L.append("  3) ⚠️ 절대 CAGR은 '봇이 실제로 몇 분 간격으로 반응/체결되는가'에 좌우됨.")
    L.append("     실거래 틱/분봉 데이터 없이는 절대값 확정 불가 (현재 환경은 외부 API 차단).")
    L.append("  4) ✅ 신뢰 가능한 결론:")
    L.append("     - 일봉 기준 CAGR(보수 12% / 균형 6% / 공격 3%)은 '하한선'.")
    L.append("     - 고빈도일수록 좁은 간격의 상대적 불리함이 줄어듦(수수료 대비 체결 증가).")
    L.append("     - 실제 채택 전 반드시 빗썸 분봉 데이터로 재보정 필요.")
    return "\n".join(L)


def _print_asymmetric_comparison(asym: BacktestSummary, sym: BacktestSummary, args) -> None:
    """비대칭 vs 대칭 그리드 결과 비교 출력."""
    bar = "═" * 68
    thin = "─" * 68
    print(bar)
    print("  📐 비대칭 vs 대칭 그리드 비교")
    print(f"  비대칭: 매수 {args.buy_interval}% / 매도 {args.sell_interval}%")
    print(f"  대칭:   {args.aggressiveness} ({AGGRESSIVENESS_INTERVAL_PCT.get(args.aggressiveness, 0.5)}%)")
    print(thin)
    print(f"  {'항목':28s} {'비대칭':>16s} {'대칭':>16s} {'차이':>10s}")
    print("  " + thin)

    def row(label, a_val, s_val, fmt="{:,.0f}"):
        diff = a_val - s_val
        sign = "+" if diff >= 0 else ""
        print(f"  {label:28s} {fmt.format(a_val):>16s} {fmt.format(s_val):>16s} {sign}{fmt.format(diff):>10s}")

    row("총 그리드 스프레드 수익(원)", asym.total_grid_profit, sym.total_grid_profit)
    row("총 수수료(원)", asym.total_fee_cost, sym.total_fee_cost)
    row("총 순 그리드 수익(원)", asym.total_net_grid, sym.total_net_grid)
    row("총 리워드(원)", asym.total_reward, sym.total_reward)
    row("총 수익(원)", asym.total_profit, sym.total_profit)
    row("월평균 수익(원)", asym.avg_monthly_profit, sym.avg_monthly_profit)
    row("연환산 CAGR(%)", asym.annualized_return_pct, sym.annualized_return_pct, "{:.2f}")
    print(bar)
    print()


def main():
    parser = argparse.ArgumentParser(description="BTC/KRW 박스권 그리드 전략 백테스팅")
    parser.add_argument("--capital", type=float, default=CAPITAL_KRW, help="초기 자본(원)")
    parser.add_argument("--start", type=str, default="2020-01-01",
                        help="히스토리 시작일 (기본: 2020-01-01)")
    parser.add_argument("--aggressiveness", type=str, default=GRID_AGGRESSIVENESS,
                        choices=["conservative", "balanced", "aggressive"])
    parser.add_argument("--sigma", type=float, default=None, choices=[1.0, 2.0],
                        help="박스 σ 레벨 고정 (기본: 시나리오 자동)")
    parser.add_argument("--granularity", type=str, default="daily",
                        choices=["daily", "intraday"],
                        help="분해능: daily(일봉) | intraday(분 경로 합성)")
    parser.add_argument("--steps", type=int, default=288,
                        help="일중 경로 분해능 (intraday, 기본 288=5분봉)")
    parser.add_argument("--no-fallback", action="store_true", help="합성데이터 폴백 비활성화")
    parser.add_argument("--save", action="store_true", help="CSV + 텍스트 리포트 저장")
    parser.add_argument("--sweep", action="store_true",
                        help="반응빈도 × 공격성 민감도 매트릭스 출력")
    parser.add_argument("--asymmetric", action="store_true",
                        help="비대칭 그리드 활성화 (--buy-interval / --sell-interval 적용)")
    parser.add_argument("--buy-interval", type=float, default=GRID_BUY_INTERVAL_PCT,
                        help=f"비대칭 매수 간격 %% (기본 {GRID_BUY_INTERVAL_PCT})")
    parser.add_argument("--sell-interval", type=float, default=GRID_SELL_INTERVAL_PCT,
                        help=f"비대칭 매도 간격 %% (기본 {GRID_SELL_INTERVAL_PCT})")
    parser.add_argument("--strategy", type=str, default="btc",
                        choices=["btc", "composite"],
                        help="전략: btc(기본) | composite(BTC+USDT 복합)")
    parser.add_argument("--usdt-sell-interval", type=float, default=USDT_SELL_INTERVAL_KRW,
                        help=f"USDT 매도 간격 KRW (기본 {USDT_SELL_INTERVAL_KRW}원)")
    args = parser.parse_args()

    # ① 히스토리 수집
    logger.info("장기 히스토리 수집 중 (최대치)...")
    history = fetch_max_history(
        start=args.start,
        use_synthetic_fallback=not args.no_fallback,
    )
    if not history:
        logger.error("히스토리 없음 → 종료")
        return

    logger.info("총 %d일 히스토리 확보 (%s ~ %s)", len(history), history[0]["date"], history[-1]["date"])

    # 민감도 매트릭스 모드
    if args.sweep:
        print(run_sweep(history, args.capital, args.sigma))
        return

    # ─── 복합전략 모드 ───────────────────────────────────────
    if args.strategy == "composite":
        btc_cap = args.capital * COMPOSITE_BTC_RATIO
        usdt_cap = args.capital * COMPOSITE_USDT_RATIO

        # BTC 레그: conservative 백테스트
        btc_agent = BacktestAgent()
        btc_summary = btc_agent.run(
            history=history,
            capital_krw=btc_cap,
            aggressiveness="conservative",
            use_sigma=args.sigma,
            granularity=args.granularity,
            n_steps=args.steps,
        )
        btc_report = generate_report(btc_summary, "conservative (BTC 레그)", args.granularity)
        print(btc_report)

        # 복합전략 분석 (USDT 레그 포함)
        composite = CompositeStrategyAgent()
        comp_result = composite.analyze(
            total_capital_krw=args.capital,
            btc_monthly_net_grid=btc_summary.avg_monthly_profit,
            btc_monthly_fee=btc_summary.total_fee_cost / max(btc_summary.total_months, 1),
            btc_monthly_reward=btc_summary.total_reward / max(btc_summary.total_months, 1),
            usdt_sell_interval=args.usdt_sell_interval,
            usdt_ratio=COMPOSITE_USDT_RATIO,
        )
        print(composite.format_report(comp_result))
        return

    # ─── BTC 단일 전략 모드 (기본) ──────────────────────────
    agent = BacktestAgent()
    summary = agent.run(
        history=history,
        capital_krw=args.capital,
        aggressiveness=args.aggressiveness,
        use_sigma=args.sigma,
        granularity=args.granularity,
        n_steps=args.steps,
        asymmetric=args.asymmetric,
        buy_interval_pct=args.buy_interval,
        sell_interval_pct=args.sell_interval,
    )

    # ─── 비대칭 그리드: 대칭 대비 비교 출력 ──────────────────
    if args.asymmetric:
        sym_summary = BacktestAgent().run(
            history=history,
            capital_krw=args.capital,
            aggressiveness=args.aggressiveness,
            use_sigma=args.sigma,
            granularity=args.granularity,
            n_steps=args.steps,
            asymmetric=False,
        )
        _print_asymmetric_comparison(summary, sym_summary, args)

    # ③ 리포트 출력
    label = f"{args.aggressiveness}"
    if args.asymmetric:
        label += f" [비대칭 buy={args.buy_interval}%/sell={args.sell_interval}%]"
    report = generate_report(summary, label, args.granularity)
    print(report)

    # ④ 저장
    if args.save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        csv_path = os.path.join(REPORT_DIR, f"backtest_{ts}.csv")
        txt_path = os.path.join(REPORT_DIR, f"backtest_{ts}_summary.txt")
        save_csv(summary, csv_path)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info("텍스트 리포트 저장: %s", txt_path)


if __name__ == "__main__":
    main()
