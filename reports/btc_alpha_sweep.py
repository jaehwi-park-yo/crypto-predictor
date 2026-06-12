"""
reports/btc_alpha_sweep.py — BTC 하방 집중 배분 α 최적화 백테스트
=================================================================
실행:
    python reports/btc_alpha_sweep.py
    python reports/btc_alpha_sweep.py --save

비교 대상:
  A. 현재 전략: 대칭 progressive (중심→경계 봇에 가중치, α_sym=0.15)
  B. 하방 집중: 상단→하단 선형 점증 (USDT와 동일 방식, α_bot=0~1.5)
  C. 균등: α=0 (기준선)

방법론:
  - BTC/KRW 일봉 실데이터(최대 8.7년) 기반 롤링 월별 백테스트
  - lookback 30일 EWMA σ → 1σ 박스 예측 → 이후 30일 실가격 검증
  - 그리드 레벨(0.5% 간격) 체결 횟수: 일봉 고저 내 레벨 통과 횟수 × 효율
  - 봇당 자본 3가지 방식 비교:
      균등: 모두 동일
      대칭: |i - center| 비례 가중 (BTC 현재 방식)
      하방: i/(N-1) 비례 가중 (하단 봇이 더 큰 자본)
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
    SIGMA_UP_BTC, SIGMA_DN_BTC,
    LAYER_A_RATIO_BTC, PROGRESSIVE_ALPHA,
)
from utils.statistics import compute_log_returns, ewma_daily_volatility, project_sigma

# ─── 상수 ────────────────────────────────────────────────────────────────────
BUY_INTERVAL_PCT  = 0.5    # 매수 간격 %
SELL_INTERVAL_PCT = 1.0    # 매도 간격 %
GRID_FILL_EFF     = 0.50
KRW_HOLD_RATIO    = 1.0 - LAYER_A_RATIO_BTC   # 40%

ALPHA_BOT_LIST = [0.0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5]
SYM_ALPHA = PROGRESSIVE_ALPHA   # 현재 대칭 progressive α = 0.15

LOOKBACK = 30
HORIZON  = 30


# ─── 데이터 로드 ──────────────────────────────────────────────────────────────
def load_btc() -> list[dict]:
    path = ROOT / "data" / "btc_history.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─── 리워드 ───────────────────────────────────────────────────────────────────
def calc_reward(vol: float) -> float:
    for threshold, rate in sorted(REWARD_TIERS, key=lambda x: -x[0]):
        if vol >= threshold:
            return min(vol * rate, MAX_REWARD_KRW)
    return 0.0


# ─── 봇당 자본 3가지 방식 ─────────────────────────────────────────────────────
def caps_uniform(N: int, deployed: float) -> list[float]:
    return [deployed / N] * N

def caps_symmetric(N: int, deployed: float) -> list[float]:
    """BTC 현재: 중심 기준 경계 봇에 가중치."""
    if N <= 1:
        return [deployed]
    half = N / 2.0
    weights = [1.0 + SYM_ALPHA * abs(i - half + 0.5) for i in range(N)]
    w = sum(weights)
    return [ww * deployed / w for ww in weights]

def caps_bottom(N: int, deployed: float, alpha: float) -> list[float]:
    """하방 집중: i=0 상단, i=N-1 하단. α=0이면 균등."""
    if N <= 1 or alpha == 0:
        return [deployed / N] * N
    weights = [1.0 + alpha * i / (N - 1) for i in range(N)]
    w = sum(weights)
    return [ww * deployed / w for ww in weights]


# ─── 레벨별 체결 횟수 ─────────────────────────────────────────────────────────
def rt_per_level_btc(
    N: int, box_upper: float, gi_pct: float, forward: list[dict]
) -> list[float]:
    """i=0 상단 → i=N-1 하단. 일봉 고저 내에 level 포함 시 1회 왕복."""
    cycle_pct = BUY_INTERVAL_PCT + SELL_INTERVAL_PCT
    rts = []
    for i in range(N):
        level = box_upper * (1 - i * gi_pct / 100)
        days_hit = sum(1 for d in forward if d["low"] <= level <= d["high"])
        rts.append(days_hit * GRID_FILL_EFF)
    return rts


# ─── 월별 백테스트 ────────────────────────────────────────────────────────────
@dataclass
class MonthResult:
    month: str
    ref: float
    box_upper: float
    box_lower: float
    box_range_pct: float
    n_bots: int
    contained: bool
    lower_half_days: int
    total_days: int
    lower_bias: float

    sym_grid_net: float
    sym_vol: float
    sym_total: float

    alpha_grid_net: dict
    alpha_vol: dict
    alpha_total: dict


def backtest_month(lookback: list[dict], forward: list[dict], capital: float) -> MonthResult | None:
    closes = [d["close"] for d in lookback]
    log_ret = compute_log_returns(closes)
    if len(log_ret) < 5:
        return None

    daily_sig = ewma_daily_volatility(log_ret)
    monthly_sig = project_sigma(daily_sig, HORIZON)
    ref = closes[-1]

    box_upper = ref * (1 + SIGMA_UP_BTC * monthly_sig)
    box_lower = ref * (1 - SIGMA_DN_BTC * monthly_sig)
    box_range_pct = (box_upper - box_lower) / ref * 100
    if box_range_pct < BUY_INTERVAL_PCT * 2:
        return None

    deployed = capital * (1 - KRW_HOLD_RATIO)
    N = max(1, int(box_range_pct / BUY_INTERVAL_PCT))
    N = min(N, 1000)

    mid = (box_upper + box_lower) / 2
    lower_half_days = sum(1 for d in forward if d["close"] < mid)
    contained = all(box_lower <= d["close"] <= box_upper for d in forward)
    total_days = len(forward)

    rts = rt_per_level_btc(N, box_upper, BUY_INTERVAL_PCT, forward)
    sell_pct = SELL_INTERVAL_PCT / 100

    def calc_profit(caps_list):
        total_profit = total_fee = total_vol = 0.0
        for cap, rt in zip(caps_list, rts):
            total_profit += rt * sell_pct * cap
            total_fee    += rt * 2 * FEE_RATE * cap
            total_vol    += rt * cap * 2
        net  = total_profit - total_fee
        vol  = total_vol
        rwd  = calc_reward(vol)
        return net, vol, net + rwd

    # 대칭 progressive (현재 BTC 방식)
    sym_net, sym_vol, sym_total = calc_profit(caps_symmetric(N, deployed))

    # 하방 집중 α별
    alpha_grid_net, alpha_vol, alpha_total = {}, {}, {}
    for alpha in ALPHA_BOT_LIST:
        g, v, t = calc_profit(caps_bottom(N, deployed, alpha))
        alpha_grid_net[alpha] = g
        alpha_vol[alpha] = v
        alpha_total[alpha] = t

    return MonthResult(
        month=lookback[-1]["date"][:7],
        ref=ref, box_upper=box_upper, box_lower=box_lower,
        box_range_pct=box_range_pct, n_bots=N,
        contained=contained,
        lower_half_days=lower_half_days, total_days=total_days,
        lower_bias=lower_half_days / max(1, total_days),
        sym_grid_net=sym_net, sym_vol=sym_vol, sym_total=sym_total,
        alpha_grid_net=alpha_grid_net, alpha_vol=alpha_vol, alpha_total=alpha_total,
    )


def run_backtest(capital: float) -> list[MonthResult]:
    data = load_btc()
    results = []
    i = LOOKBACK
    while i + HORIZON <= len(data):
        r = backtest_month(data[i-LOOKBACK:i], data[i:i+HORIZON], capital)
        if r:
            results.append(r)
        i += HORIZON
    return results


# ─── 출력 ────────────────────────────────────────────────────────────────────
def summarize(results: list[MonthResult]) -> None:
    if not results:
        print("결과 없음"); return

    n = len(results)
    avg_bias = sum(r.lower_bias for r in results) / n
    containment = sum(1 for r in results if r.contained) / n

    print("\n" + "═" * 90)
    print("  BTC 하방 집중 배분 α 최적화 백테스트")
    print(f"  기간: {results[0].month} ~ {results[-1].month}  ({n}개월)")
    print(f"  가격 분포: 하반부 체류 {avg_bias*100:.1f}%  "
          f"{'(하방 편중)' if avg_bias > 0.52 else '(상방 편중)' if avg_bias < 0.48 else '(균등)'}")
    print(f"  1σ 박스 포함률: {containment*100:.1f}%")
    print("═" * 90)

    fmt_m  = lambda n: f"{n/1e4:+.1f}만"
    fmt_ok = lambda n: f"{n/1e8:.2f}억"

    # 현재 대칭 progressive 기준값
    sym_cum = sum(r.sym_total for r in results)
    sym_avg = sym_cum / n

    print(f"\n{'방식':>16} | {'월평균 그리드':>12} | {'월평균 거래량':>11} | {'월평균 총순익':>12} | {'누적 총순익':>12} | 현재 대비")
    print("-" * 95)

    # 현재 대칭 progressive
    print(f"{'현재(대칭 sym)':>16} | {fmt_m(sum(r.sym_grid_net for r in results)/n):>12} | "
          f"{fmt_ok(sum(r.sym_vol for r in results)/n):>11} | "
          f"{fmt_m(sym_avg):>12} | {fmt_m(sym_cum):>12} | 기준")

    best_alpha = None
    best_total = -1e18
    for alpha in ALPHA_BOT_LIST:
        g_nets = [r.alpha_grid_net[alpha] for r in results]
        totals = [r.alpha_total[alpha] for r in results]
        vols   = [r.alpha_vol[alpha] for r in results]
        cum    = sum(totals)
        avg    = cum / n
        diff   = cum - sym_cum
        label  = f"하방α={alpha:.1f}"
        if cum > best_total:
            best_total = cum
            best_alpha = alpha
        print(f"{label:>16} | {fmt_m(sum(g_nets)/n):>12} | "
              f"{fmt_ok(sum(vols)/n):>11} | {fmt_m(avg):>12} | "
              f"{fmt_m(cum):>12} | {fmt_m(diff):>10}")

    # 하방 편중 vs 상방 조건부 분석
    biased  = [r for r in results if r.lower_bias >= 0.55]
    neutral = [r for r in results if r.lower_bias <  0.55]

    print(f"\n{'시나리오':>14} | {'월수':>4} | {'현재(대칭)':>10} | "
          + " | ".join(f"α={a:.1f}" for a in [0.0, 0.5, 1.0, 1.5]))
    print("-" * 85)
    for label, subset in [("하방편중(≥55%)", biased), ("균등/상방(<55%)", neutral), ("전체", results)]:
        if not subset:
            continue
        sym_a = sum(r.sym_total for r in subset) / len(subset)
        vals  = [sum(r.alpha_total[a] for r in subset)/len(subset) for a in [0.0, 0.5, 1.0, 1.5]]
        print(f"{label:>14} | {len(subset):>4} | {fmt_m(sym_a):>10} | "
              + " | ".join(f"{fmt_m(v):>8}" for v in vals))

    # 월별 상세 (최근 24개월)
    print(f"\n{'월':>8} | {'하반부%':>7} | {'박스%':>6} | {'현재(대칭)':>10} | α=0.0 | α=0.5 | α=1.0 | α=1.5")
    print("-" * 80)
    for r in results[-24:]:
        print(
            f"{r.month:>8} | {r.lower_bias*100:>6.1f}% | {r.box_range_pct:>5.1f}% | "
            f"{fmt_m(r.sym_total):>10} | "
            + " | ".join(f"{fmt_m(r.alpha_total[a]):>6}" for a in [0.0, 0.5, 1.0, 1.5])
        )

    print(f"\n✅ 최적 하방 α = {best_alpha:.1f}  (누적 {fmt_m(best_total)}, 현재 대칭 대비 {fmt_m(best_total-sym_cum)})")
    print(f"   현재 대칭 progressive 누적 = {fmt_m(sym_cum)}")
    if abs(best_total - sym_cum) < sym_cum * 0.01:
        print("   → 차이가 1% 미만 — 현재 전략 유지 권고")
    elif best_total > sym_cum:
        print(f"   → 하방 집중 α={best_alpha:.1f}로 교체 검토")
    else:
        print("   → 현재 대칭 방식이 우월 — 변경 불필요")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=40_000_000)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    print(f"⏳ BTC α 백테스트 실행 중 ({len(load_btc())}일 데이터)...")
    results = run_backtest(args.capital)
    print(f"   → {len(results)}개월 분석 완료")
    summarize(results)

    if args.save:
        out = ROOT / "reports" / "btc_alpha_sweep_result.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["month","lower_bias","box_pct","sym_total"]
                       + [f"bot_alpha{a}" for a in ALPHA_BOT_LIST])
            for r in results:
                w.writerow([r.month, f"{r.lower_bias:.3f}", f"{r.box_range_pct:.1f}",
                            f"{r.sym_total:.0f}"]
                           + [f"{r.alpha_total[a]:.0f}" for a in ALPHA_BOT_LIST])
        print(f"💾 CSV: {out}")
