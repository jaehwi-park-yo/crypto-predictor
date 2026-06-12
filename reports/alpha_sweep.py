"""
reports/alpha_sweep.py — USDT 하방 집중 배분 α 최적화 백테스트
==============================================================
실행:
    python reports/alpha_sweep.py
    python reports/alpha_sweep.py --save      # CSV 저장
    python reports/alpha_sweep.py --capital 60000000

방법론:
  - USDT/KRW 일봉 실데이터(734일) 기반 롤링 월별 백테스트
  - lookback 30일 → EWMA σ 추정 → 1σ 박스 예측 → 이후 30일 실제 가격으로 검증
  - 각 그리드 레벨(1원 간격)의 실제 체결 횟수를 일봉 고저로 추정
    (level ∈ [일봉low, 일봉high] → round-trip 1회 × GRID_FILL_EFFICIENCY)
  - 봇당 자본: α=0 균등 / α>0 상단→하단 선형 점증
    weight_i = 1 + α × i / (N-1)  (i=0 상단 고가, i=N-1 하단 저가)
  - 리워드: 거래량 → REWARD_TIERS 매핑
  - 총 수익 = 그리드 순익 + 리워드
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config import (
    FEE_RATE, REWARD_TIERS, MAX_REWARD_KRW,
    SIGMA_UP_USDT, SIGMA_DN_USDT, HORIZON_DAYS,
    LAYER_A_RATIO_USDT,
)
from utils.statistics import compute_log_returns, ewma_daily_volatility, project_sigma

# ─── 상수 ────────────────────────────────────────────────────────────────────
BUY_INTERVAL_KRW  = 1.0
SELL_INTERVAL_KRW = 3.0
GRID_FILL_EFF     = 0.50   # 일봉 고저 기반 체결률
INNER_ACTIVE_RATIO = 1.0   # 내부존 상시 활성
KRW_HOLD_RATIO    = 1.0 - LAYER_A_RATIO_USDT   # 20%

ALPHA_LIST = [0.0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5]

LOOKBACK  = 30
HORIZON   = 30
MIN_PERIODS = LOOKBACK + HORIZON   # 최소 필요 일수


# ─── 데이터 로드 ──────────────────────────────────────────────────────────────
def load_usdt() -> list[dict]:
    path = ROOT / "data" / "usdt_history.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─── 리워드 계산 ──────────────────────────────────────────────────────────────
def calc_reward(vol: float) -> float:
    for threshold, rate in sorted(REWARD_TIERS, key=lambda x: -x[0]):
        if vol >= threshold:
            return min(vol * rate, MAX_REWARD_KRW)
    return 0.0


# ─── 자본 배분 weights ────────────────────────────────────────────────────────
def bot_caps(alpha: float, N: int, deployed: float) -> list[float]:
    """i=0 상단(고가), i=N-1 하단(저가). α>0: 하단 봇이 (1+α)배 자본."""
    if N <= 0:
        return []
    if alpha == 0.0 or N == 1:
        return [deployed / N] * N
    weights = [1.0 + alpha * i / (N - 1) for i in range(N)]
    w_sum = sum(weights)
    return [w * deployed / w_sum for w in weights]


# ─── 월별 백테스트 ────────────────────────────────────────────────────────────
@dataclass
class MonthResult:
    month: str
    ref: float
    box_upper: float
    box_lower: float
    box_range: float
    n_bots: int
    contained: bool             # 모든 종가가 1σ 내에 있는지
    lower_half_days: int        # 박스 하반부(하위 50%)에 머문 날
    total_days: int
    lower_bias: float           # lower_half_days / total_days

    # α별 결과
    alpha_grid_net: dict        # alpha → 그리드 순익(원)
    alpha_vol: dict             # alpha → 월 거래량(원)
    alpha_reward: dict          # alpha → 리워드(원)
    alpha_total: dict           # alpha → 총 순익


def backtest_month(
    lookback_data: list[dict],
    forward_data: list[dict],
    capital: float,
) -> MonthResult | None:
    closes = [d["close"] for d in lookback_data]
    log_ret = compute_log_returns(closes)
    if len(log_ret) < 5:
        return None

    daily_sig = ewma_daily_volatility(log_ret)
    monthly_sig = project_sigma(daily_sig, HORIZON)
    ref = closes[-1]

    box_upper = ref * (1 + SIGMA_UP_USDT * monthly_sig)
    box_lower = ref * (1 - SIGMA_DN_USDT * monthly_sig)
    box_range = box_upper - box_lower
    if box_range < BUY_INTERVAL_KRW * 2:
        return None

    deployed = capital * (1 - KRW_HOLD_RATIO)
    N = max(1, int(box_range / BUY_INTERVAL_KRW))

    # 실제 가격 분석 ─ 상/하반부 체류 일수
    mid = (box_upper + box_lower) / 2
    lower_half_days = sum(1 for d in forward_data if d["close"] < mid)
    total_days = len(forward_data)
    contained = all(box_lower <= d["close"] <= box_upper for d in forward_data)

    # 레벨별 실제 체결 횟수 계산
    # level_j: 하단에서 j번째 레벨 (= box_lower + j원), j=0이 최저가
    # bot 인덱스 i=0이 상단(고가), i=N-1이 하단(저가)
    #   level[i] = box_upper - i × BUY_INTERVAL
    rt_per_level: list[float] = []  # index i=0 상단 → i=N-1 하단
    sell_cycle = BUY_INTERVAL_KRW + SELL_INTERVAL_KRW
    for i in range(N):
        level = box_upper - i * BUY_INTERVAL_KRW
        days_hit = sum(
            1 for d in forward_data
            if d["low"] <= level <= d["high"]
        )
        # 하루에 가격이 해당 레벨을 지나가면 1회 왕복 × 효율
        rt = days_hit * GRID_FILL_EFF
        rt_per_level.append(rt)

    # α별 수익 계산
    alpha_grid_net: dict = {}
    alpha_vol: dict = {}
    alpha_reward: dict = {}
    alpha_total: dict = {}

    sell_pct = SELL_INTERVAL_KRW / ref  # 매도 수익률

    for alpha in ALPHA_LIST:
        caps = bot_caps(alpha, N, deployed)
        total_profit = 0.0
        total_fee = 0.0
        total_vol = 0.0
        for i, (cap, rt) in enumerate(zip(caps, rt_per_level)):
            profit = rt * sell_pct * cap
            fee = rt * 2 * FEE_RATE * cap
            vol = rt * cap * 2
            total_profit += profit
            total_fee += fee
            total_vol += vol

        net = total_profit - total_fee
        reward = calc_reward(total_vol)
        alpha_grid_net[alpha] = net
        alpha_vol[alpha] = total_vol
        alpha_reward[alpha] = reward
        alpha_total[alpha] = net + reward

    return MonthResult(
        month=lookback_data[-1]["date"][:7],
        ref=ref,
        box_upper=box_upper,
        box_lower=box_lower,
        box_range=box_range,
        n_bots=N,
        contained=contained,
        lower_half_days=lower_half_days,
        total_days=total_days,
        lower_bias=lower_half_days / max(1, total_days),
        alpha_grid_net=alpha_grid_net,
        alpha_vol=alpha_vol,
        alpha_reward=alpha_reward,
        alpha_total=alpha_total,
    )


# ─── 전체 백테스트 ────────────────────────────────────────────────────────────
def run_backtest(capital: float) -> list[MonthResult]:
    data = load_usdt()
    results: list[MonthResult] = []

    step = HORIZON  # 월 단위 슬라이딩
    i = LOOKBACK
    while i + HORIZON <= len(data):
        lookback = data[i - LOOKBACK: i]
        forward  = data[i: i + HORIZON]
        r = backtest_month(lookback, forward, capital)
        if r is not None:
            results.append(r)
        i += step

    return results


# ─── 결과 집계 · 출력 ────────────────────────────────────────────────────────
def summarize(results: list[MonthResult]) -> None:
    if not results:
        print("결과 없음")
        return

    n = len(results)
    avg_bias = sum(r.lower_bias for r in results) / n
    containment = sum(1 for r in results if r.contained) / n

    # ── 헤더 ──
    print("\n" + "═" * 80)
    print("  USDT 하방 집중 배분 α 최적화 백테스트")
    print(f"  기간: {results[0].month} ~ {results[-1].month}  ({n}개월)")
    print(f"  가격 분포: 하반부 체류 {avg_bias*100:.1f}%  "
          f"{'(하방 편중 ↓)' if avg_bias > 0.52 else '(상방 편중 ↑)' if avg_bias < 0.48 else '(균등 ≈)'}")
    print(f"  1σ 박스 포함률: {containment*100:.1f}%")
    print("═" * 80)

    # ── α별 누적 수익 표 ──
    fmt_m = lambda n: f"{n/1e4:+.1f}만"
    fmt_ok = lambda n: f"{n/1e8:.2f}억"

    print(f"\n{'α':>5} | {'월평균 그리드순익':>14} | {'월평균 거래량':>12} | "
          f"{'월평균 리워드':>10} | {'월평균 총순익':>12} | {'누적 총순익':>12} | {'CAGR':>7}")
    print("-" * 90)

    best_alpha = None
    best_total = -1e18

    for alpha in ALPHA_LIST:
        grid_nets = [r.alpha_grid_net[alpha] for r in results]
        vols      = [r.alpha_vol[alpha] for r in results]
        rewards   = [r.alpha_reward[alpha] for r in results]
        totals    = [r.alpha_total[alpha] for r in results]

        avg_grid   = sum(grid_nets) / n
        avg_vol    = sum(vols) / n
        avg_reward = sum(rewards) / n
        avg_total  = sum(totals) / n
        cum_total  = sum(totals)

        months = n
        cagr = (1 + cum_total / (capital := 40_000_000)) ** (12 / max(1, months)) - 1

        marker = " ◀ 최적" if alpha == best_alpha else ""
        print(
            f"{alpha:>5.1f} | {fmt_m(avg_grid):>14} | {fmt_ok(avg_vol):>12} | "
            f"{fmt_m(avg_reward):>10} | {fmt_m(avg_total):>12} | "
            f"{fmt_m(cum_total):>12} | {cagr*100:>6.1f}%{marker}"
        )

        if cum_total > best_total:
            best_total = cum_total
            best_alpha = alpha

    # ── 월별 상세 (하방 편중 구간 vs 균등 구간) ──
    print(f"\n{'월':>8} | {'하반부%':>7} | {'박스범위':>7} | "
          + " | ".join(f"α={a:.1f}" for a in [0.0, 0.5, 1.0, 1.5]))
    print("-" * (30 + 12 * 4))
    for r in results:
        row = (
            f"{r.month:>8} | {r.lower_bias*100:>6.1f}% | {r.box_range:>6.1f}원 | "
            + " | ".join(
                f"{fmt_m(r.alpha_total[a]):>10}" for a in [0.0, 0.5, 1.0, 1.5]
            )
        )
        print(row)

    # ── 하방 편중 월 vs 균등 월 분리 비교 ──
    biased   = [r for r in results if r.lower_bias >= 0.55]
    neutral  = [r for r in results if r.lower_bias <  0.55]

    print(f"\n{'시나리오':>12} | {'월 수':>5} | α=0.0 평균순익 | α=0.5 평균순익 | α=1.0 평균순익 | α=1.5 평균순익")
    print("-" * 80)
    for label, subset in [("하방편중(≥55%)", biased), ("균등/상방(<55%)", neutral), ("전체", results)]:
        if not subset:
            continue
        row_vals = []
        for a in [0.0, 0.5, 1.0, 1.5]:
            avg = sum(r.alpha_total[a] for r in subset) / len(subset)
            row_vals.append(fmt_m(avg))
        print(f"{label:>12} | {len(subset):>5} | "
              + " | ".join(f"{v:>14}" for v in row_vals))

    print(f"\n✅ 최적 α = {best_alpha:.1f}  (누적 총순익 {fmt_m(best_total)})")
    bias_msg = (
        "하방 편중 구간이 많아 집중 배분 효과가 큽니다." if avg_bias > 0.52
        else "가격 분포가 균등하거나 상방 편중 → α 0~0.5 범위가 적합합니다."
    )
    print(f"   → {bias_msg}")
    print()


# ─── CSV 저장 ─────────────────────────────────────────────────────────────────
def save_csv(results: list[MonthResult], capital: float) -> Path:
    out = ROOT / "reports" / "alpha_sweep_result.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        header = [
            "month", "ref", "box_upper", "box_lower", "box_range",
            "n_bots", "contained", "lower_half_pct",
        ] + [f"total_alpha{a}" for a in ALPHA_LIST] \
          + [f"vol_alpha{a}"   for a in ALPHA_LIST]
        w.writerow(header)
        for r in results:
            row = [
                r.month, f"{r.ref:.1f}", f"{r.box_upper:.1f}", f"{r.box_lower:.1f}",
                f"{r.box_range:.1f}", r.n_bots, int(r.contained),
                f"{r.lower_bias*100:.1f}",
            ] + [f"{r.alpha_total[a]:.0f}"  for a in ALPHA_LIST] \
              + [f"{r.alpha_vol[a]:.0f}"    for a in ALPHA_LIST]
            w.writerow(row)
    return out


# ─── 진입점 ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="α 최적화 백테스트")
    ap.add_argument("--capital", type=float, default=40_000_000)
    ap.add_argument("--save",    action="store_true")
    args = ap.parse_args()

    print(f"⏳ 백테스트 실행 중 (자본 {args.capital/1e4:.0f}만원)...")
    results = run_backtest(args.capital)
    print(f"   → {len(results)}개월 분석 완료")
    summarize(results)

    if args.save:
        path = save_csv(results, args.capital)
        print(f"💾 CSV 저장: {path}")
