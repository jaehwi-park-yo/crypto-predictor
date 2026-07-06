"""
MTM 서킷브레이커 백테스트 (5분봉 그리드 시뮬, walk-forward 1σ 박스)
====================================================================
문제의식: 박스 실패월의 그리드 손실은 "지속 드리프트 → 하락매수 인벤토리 누적 →
MTM 손실"이 지배한다 (2026-06 시뮬 MTM −32.6만). 과열 사전필터는 데이터가 기각
(docs/box_model_review_202607.md) → 사후 대응인 MTM 서킷브레이커를 검증한다.

규칙:
  - 미실현 MTM 손실이 투입자본의 θ%를 넘으면 "신규 매수 정지" (보유분 익절 매도는 계속).
  - MTM이 θ×RESUME(기본 0.5)% 이내로 회복되면 매수 재개 (히스테리시스).
  - θ=∞ 가 베이스라인(현행, 서킷 없음).

평가 (103개월 walk-forward, 1σ 박스, buy 0.2%):
  - 월평균 순익 = 그리드 − 수수료 + 월말 MTM (+리워드 0.015% 별도 표기)
  - 최악월 순익, 월중 최저 MTM(드로다운), 발동 월 수, 거래량 변화

사용: python3 reports/mtm_circuit_backtest.py [--zip PATH]
"""
from __future__ import annotations
import argparse, csv, io, math, zipfile
from collections import defaultdict
import numpy as np

FEE = 0.0004
RATE = 0.00015          # 전월 10억↑ 리워드
MAX_REWARD = 3_000_000
DEPLOYED = 15_679_334   # 사용자 6월 BTC 1σ 실투입 자본
RESUME = 0.5            # 재개 히스테리시스: θ의 50% 이내로 회복 시


def load_5m(zf, name):
    by = defaultdict(list)
    with zf.open(name) as raw:
        r = csv.reader(io.TextIOWrapper(raw, "utf-8"))
        next(r)
        for row in r:
            by[row[0][:7]].append(float(row[4]))
    return {m: np.asarray(v, float) for m, v in by.items()}


def load_labels(zf, name):
    out = {}
    with zf.open(name) as raw:
        for row in csv.DictReader(io.TextIOWrapper(raw, "utf-8")):
            out[row["target_month"]] = {
                "l1": float(row["box_l1"]), "u1": float(row["box_u1"]),
                "days": int(row["realized_days"])}
    return out


def compress_levels(prices, lower, upper, b):
    p = np.clip(prices, lower, upper)
    idx = np.floor(np.log(p / lower) / math.log(1 + b)).astype(np.int64)
    seq = idx[np.concatenate(([True], idx[1:] != idx[:-1]))]
    return seq, (int(seq.max()) if len(seq) else 0)


def sim_cb(seq, K, m, b, cpb, deployed, theta, final_level_price_ratio):
    """서킷브레이커 포함 단일인벤토리 그리드 시뮬.

    seq: 레벨 시퀀스, m: 매도 스텝, cpb: 봇당 자본, theta: 자본 대비 정지 임계(0~, None=없음)
    반환: (왕복수, 월말MTM_krw, 월중최저MTM_krw, 발동여부, 정지스텝비율)
    MTM(krw) = Σ_보유레벨 ((1+b)^(cur-lvl) − 1) × cpb
    """
    if len(seq) < 2 or m < 1:
        return 0, 0.0, 0.0, False, 0.0
    held = np.zeros(K + 2, dtype=bool)
    trips, prev = 0, int(seq[0])
    paused = False
    tripped = False
    paused_steps = 0
    min_mtm = 0.0

    def mtm_at(cur):
        hl = np.nonzero(held)[0]
        if not len(hl):
            return 0.0
        return float(np.sum((1 + b) ** (cur - hl) - 1.0)) * cpb

    for cur_ in seq[1:]:
        cur = int(cur_)
        if cur < prev:
            if not paused:
                held[cur + 1: prev + 1] = True
            else:
                paused_steps += 1
        elif cur > prev:
            a, h = max(0, prev + 1 - m), cur - m
            if h >= 0:
                seg = held[a:h + 1]
                if seg.any():
                    trips += int(seg.sum()); held[a:h + 1] = False
        prev = cur
        mtm = mtm_at(cur)
        min_mtm = min(min_mtm, mtm)
        if theta is not None:
            if not paused and mtm <= -theta * deployed:
                paused = True; tripped = True
            elif paused and mtm >= -theta * RESUME * deployed:
                paused = False
    final_mtm = mtm_at(prev) * final_level_price_ratio  # 레벨가≈종가 근사 보정(≈1)
    return trips, final_mtm, min_mtm, tripped, paused_steps / max(1, len(seq) - 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default="data/dataset_seed.zip")
    ap.add_argument("--buy", type=float, default=0.2)
    ap.add_argument("--m", type=int, default=2, help="매도 스텝 (2 → 매도 0.4%%, 실전 0.3%%의 근사 상계)")
    args = ap.parse_args()

    with zipfile.ZipFile(args.zip) as zf:
        candles = load_5m(zf, "minute_KRW-BTC_5m.csv")
        labels = load_labels(zf, "monthly_labels.csv")

    months = [mo for mo in sorted(set(candles) & set(labels)) if labels[mo]["days"] >= 20]
    b = args.buy / 100.0
    thetas = [0.005, 0.01, 0.02, 0.03, 0.05, None]  # None = 베이스라인

    print(f"MTM 서킷브레이커 백테스트 — {len(months)}개월, buy {args.buy}% / m={args.m}"
          f"(매도 {((1+b)**args.m-1)*100:.2f}%), 자본 {DEPLOYED/1e6:.1f}M, 재개 히스테리시스 {RESUME}")
    hdr = f"{'θ(자본%)':>9} {'월평균순익':>10} {'+리워드':>9} {'최악월':>10} {'월중최저MTM평균':>14} {'최악드로다운':>11} {'발동월':>6} {'거래량Δ':>8}"
    print(hdr); print("-" * len(hdr))

    base_vol = None
    for theta in thetas:
        rows = []
        for mo in months:
            lab = labels[mo]; lo, hi = lab["l1"], lab["u1"]
            seq, K = compress_levels(candles[mo], lo, hi, b)
            bots = max(1, min(2000, math.ceil((hi - lo) / lo * 100 / args.buy)))
            cpb = DEPLOYED / bots
            tr, fmtm, mnm, tripped, pfrac = sim_cb(seq, K, args.m, b, cpb, DEPLOYED, theta, 1.0)
            sp = (1 + b) ** args.m - 1
            grid = tr * sp * cpb
            fee = tr * 2 * FEE * cpb
            vol = tr * 2 * cpb
            rows.append(dict(net=grid - fee + fmtm, vol=vol, mnm=mnm, tripped=tripped))
        n = len(rows)
        avg_net = sum(r["net"] for r in rows) / n
        avg_vol = sum(r["vol"] for r in rows) / n
        rw = min(avg_vol * RATE, MAX_REWARD)
        worst = min(r["net"] for r in rows)
        avg_mnm = sum(r["mnm"] for r in rows) / n
        worst_mnm = min(r["mnm"] for r in rows)
        n_trip = sum(1 for r in rows if r["tripped"])
        if base_vol is None and theta is None:
            base_vol = avg_vol
        lbl = "없음(현행)" if theta is None else f"{theta*100:.1f}%"
        vol_d = ""  # 베이스라인 대비 거래량 변화는 마지막에 일괄 계산 못 하므로 저장
        print(f"{lbl:>9} {avg_net/1e4:>+9.1f}만 {(avg_net+rw)/1e4:>+8.1f}만 {worst/1e4:>+9.1f}만 "
              f"{avg_mnm/1e4:>+13.1f}만 {worst_mnm/1e4:>+10.1f}만 {n_trip:>4}/{n} {avg_vol/1e8:>7.2f}억")


if __name__ == "__main__":
    main()
