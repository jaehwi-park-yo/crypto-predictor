"""
거래량 목표 제약 그리드 간격 최적화 (계정 전체·듀얼존 중복거래 모델)
======================================================================
조건:
  - 월별 거래량 목표를 '우선' 확보 (계정 단위 합산). 목표 2종: 10억↑ / 100억↑.
  - 계정 = BTC + USDT, 각각 1σ inner + 2σ outer 듀얼존 동시 운용.
    2σ outer 봇은 전체 2σ 박스(1σ 구간 포함)에서 동작 → 1σ 구간 중복거래(거래량 증폭).
  - 목표 거래량을 달성하는 (매수,매도) 간격 중 월 순익 추정이 최대가 되는 설정 탐색.
모델: reports/grid_interval_backtest.sim (5분봉 단일인벤토리 실측 시뮬).
자본: 4,000만 × (1−KRW 0.30) × (BTC 60%/USDT 40%) × (inner 60%/outer 40%).
리워드: 목표 10억→0.015% / 100억→0.018%, 월 300만원 캡 (계정 합산 거래량 기준).
"""
from __future__ import annotations
import math, time, zipfile
import numpy as np
from grid_interval_backtest import load_5m, load_labels, compress_levels, sim, FEE, MAX_REWARD

INVESTABLE = 40_000_000 * (1 - 0.30)           # 28.0M
BTC_INNER  = INVESTABLE * 0.60 * 0.60          # 10.08M
BTC_OUTER  = INVESTABLE * 0.60 * 0.40          #  6.72M
USDT_INNER = INVESTABLE * 0.40 * 0.60          #  6.72M
USDT_OUTER = INVESTABLE * 0.40 * 0.40          #  4.48M


def grid_month(prices, lo, hi, cap, buy, m):
    """단일 그리드 월간 (grid익, 수수료, 거래량, MTM손익)."""
    b = buy / 100.0
    seq, K = compress_levels(prices, lo, hi, b)
    bots = max(1, min(2000, math.ceil((hi - lo) / lo * 100 / buy)))
    cpb = cap / bots
    fp = float(np.clip(prices[-1], lo, hi))
    tr, mtm = sim(seq, K, m, lo, b, fp)
    sp = ((1 + b) ** m - 1)
    return tr * sp * cpb, tr * 2 * FEE * cpb, tr * 2 * cpb, mtm * cpb


def account_eval(bc, bl, uc, ul, months, buy, m):
    """계정 전체(4그리드) 월평균 (grid, fee, vol, mtm)."""
    G = F = V = M = 0.0
    for mo in months:
        b1, b2, u1, u2 = bl[mo], bl[mo], ul[mo], ul[mo]
        for prices, lo, hi, cap in [
            (bc[mo], b1["l1"], b1["u1"], BTC_INNER),
            (bc[mo], b2["l2"], b2["u2"], BTC_OUTER),
            (uc[mo], u1["l1"], u1["u1"], USDT_INNER),
            (uc[mo], u2["l2"], u2["u2"], USDT_OUTER),
        ]:
            g, f, v, mt = grid_month(prices, lo, hi, cap, buy, m)
            G += g; F += f; V += v; M += mt
    n = len(months)
    return G / n, F / n, V / n, M / n


def main():
    t0 = time.time()
    with zipfile.ZipFile("data/dataset_seed.zip") as zf:
        bc = load_5m(zf, "minute_KRW-BTC_5m.csv"); bl = load_labels(zf, "monthly_labels.csv")
        uc = load_5m(zf, "minute_KRW-USDT_5m.csv"); ul = load_labels(zf, "monthly_labels_usdt.csv")
    months = [mo for mo in sorted(set(bc) & set(bl) & set(uc) & set(ul))
              if bl[mo]["days"] >= 20 and ul[mo]["days"] >= 20]
    print("=" * 98)
    print("📊 거래량 목표 제약 최적 그리드 간격 (계정 전체 BTC+USDT · 1σ/2σ 듀얼존 중복거래)")
    print(f"   대상 {len(months)}개월 ({months[0]}~{months[-1]}) · 자본배분 BTC inner 10.08M/outer 6.72M, "
          f"USDT inner 6.72M/outer 4.48M")
    print(f"   (모든 존 동일 매수/매도% 적용 = 부스터식 통합간격. USDT 원환산 기준가 1450원)")
    print("=" * 98)

    # 전수 스윕: buy 0.02~1.00% (0.02 step), sell = (1+b)^m-1 ≤ 2.5%
    buys = [round(0.02 + 0.02 * i, 2) for i in range(50)]
    URef = 1450
    rows = []  # (buy, sell%, G, F, V, M)
    for buy in buys:
        b = buy / 100.0
        m = 1
        while True:
            sp = ((1 + b) ** m - 1) * 100
            if sp > 2.5:
                break
            G, F, V, M = account_eval(bc, bl, uc, ul, months, buy, m)
            rows.append((buy, sp, G, F, V, M))
            m += 1

    def best_for(target, rate):
        cand = [r for r in rows if r[4] >= target]
        if not cand:
            mx = max(rows, key=lambda r: r[4])
            return None, mx
        # 순익(실현=grid-fee+reward) 최대
        def net(r):
            return r[2] - r[3] + min(r[4] * rate, MAX_REWARD)
        return max(cand, key=net), None

    def show(tag, target, rate):
        best, mx = best_for(target, rate)
        if best is None:
            buy, sp, G, F, V, M = mx
            print(f"[{tag}] ⚠️ 달성 불가 — 최대 거래량 {V/1e8:.1f}억/월 "
                  f"(매수 {buy:.2f}%/매도 {sp:.2f}%, USDT {round(buy/100*URef)}/{round(sp/100*URef)}원)")
            rw = min(V * rate, MAX_REWARD)
            print(f"          그 지점 순익: 그리드 {G/1e4:.0f} −수수료 {F/1e4:.0f} +리워드 {rw/1e4:.0f} "
                  f"= 실현 {(G-F+rw)/1e4:+.0f}만 (월MTM {M/1e4:+.0f})")
            return
        buy, sp, G, F, V, M = best
        rw = min(V * rate, MAX_REWARD)
        print(f"[{tag}] ★ 매수 {buy:.2f}% / 매도 {sp:.2f}%  (USDT 환산 {round(buy/100*URef)}원/{round(sp/100*URef)}원)")
        print(f"          거래량 {V/1e8:.1f}억/월 (목표 {target/1e8:.0f}억 충족) | "
              f"실현순익 {(G-F+rw)/1e4:+.0f}만 = 그리드 {G/1e4:.0f} −수수료 {F/1e4:.0f} +리워드 {rw/1e4:.0f} | 월MTM {M/1e4:+.0f}만")

    show("목표 10억↑", 1_000_000_000, 0.00015)
    show("목표 100억↑", 10_000_000_000, 0.00018)

    # 거래량-순익 트레이드오프: 거래량 하한별 최적 순익 간격
    print("\n── 거래량 하한별 최적(실현순익 최대) 간격 [달성 가능 범위] ──")
    print(f"   {'거래량하한':>8} {'매수%':>6} {'매도%':>6} {'실현거래량':>9} {'실현순익':>8} {'월MTM':>7} {'수수료':>7}")
    rate = 0.00015
    for thr in [0.3e8, 0.5e8, 1e8, 2e8, 3e8, 4e8, 5e8, 6e8]:
        cand = [r for r in rows if r[4] >= thr]
        if not cand:
            continue
        b = max(cand, key=lambda r: r[2] - r[3] + min(r[4] * rate, MAX_REWARD))
        buy, sp, G, F, V, M = b
        rw = min(V * rate, MAX_REWARD)
        print(f"   {thr/1e8:>7.1f}억 {buy:>6.2f} {sp:>6.2f} {V/1e8:>8.2f}억 "
              f"{(G-F+rw)/1e4:>+7.0f}만 {M/1e4:>+6.0f}만 {-F/1e4:>6.0f}만")

    # 필요 자본 추정 (거래량은 자본에 거의 선형 — 부스터 2.07억/28M 기준)
    print("\n── 거래량 목표 달성에 필요한 추정 자본 (부스터 0.2% 기준, 거래량 ∝ 자본) ──")
    base_vol, base_deployed = 2.07e8, INVESTABLE
    for tgt in [1e9, 1e10]:
        need_deployed = base_deployed * tgt / base_vol
        need_total = need_deployed / 0.70
        print(f"   {tgt/1e8:>4.0f}억/월 → 투입자본 약 {need_deployed/1e8:.1f}억 (총자본 약 {need_total/1e8:.1f}억 필요)")

    print("\n── 참고: 현재/부스터 통합간격의 계정 실제 거래량 ──")
    for label, buy, m in [("초기 0.5/1.0(통합 0.5%근사)", 0.50, None), ("부스터 0.2/0.3(통합 0.2%근사)", 0.20, None)]:
        # 가장 가까운 sell 찾기: 0.5%→sell≈1.0%(m), 0.2%→sell≈0.3%
        b = buy / 100.0
        targ_sell = {0.50: 1.0, 0.20: 0.3}[buy]
        m = max(1, round(math.log(1 + targ_sell / 100) / math.log(1 + b)))
        G, F, V, M = account_eval(bc, bl, uc, ul, months, buy, m)
        sp = ((1 + b) ** m - 1) * 100
        rw = min(V * 0.00015, MAX_REWARD)
        print(f"  {label}: 매수 {buy:.2f}%/매도 {sp:.2f}% → 거래량 {V/1e8:.2f}억/월, "
              f"실현순익 {(G-F+rw)/1e4:+.0f}만 (월MTM {M/1e4:+.0f})")

    print(f"\n(소요 {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
