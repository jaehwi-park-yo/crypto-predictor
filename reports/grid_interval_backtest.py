"""
그리드 매수/매도 간격 최적화 백테스트 (5분봉 실측 단일인벤토리 시뮬)
======================================================================
data/dataset_seed.zip 의 5분봉(KRW-BTC, KRW-USDT)으로 실제 그리드 봇 체결을
재현하여, 리워드 포함 월간 순익이 최대가 되는 매수/매도 간격을 탐색한다.

시뮬레이션 모델 (실제 그리드 봇 동작 충실 재현):
  - 라인 j 에 매수 주문. 가격이 라인 j 로 하락 → 매수(인벤토리 1단위 보유).
  - 보유 중 라인 j 재매수 금지(단일 인벤토리). 가격이 j+m 라인 도달 시에만 익절 매도.
  - 매도 후 재무장. m = 매도거리(매수스텝 수), 실제 매도% = (1+buy)^m − 1.
  - 월말 미청산 인벤토리는 종가 기준 평가손익(MTM)으로 정산 → 광폭매도의
    인벤토리 적체 리스크를 공정하게 반영(이게 없으면 매도간격이 ∞로 발산).

박스권: monthly_labels(as-of 예측, 미래정보 누설 없음)의 1σ 박스.
자본:   기본값 4,000만 × (1−KRW보유 0.30) × (BTC 60% / USDT 40%).
리워드: 전월 거래량 티어 → 10억↑ 0.015% / 100억↑ 0.018%, 월 300만원 캡.

사용:  python3 reports/grid_interval_backtest.py
"""
from __future__ import annotations
import csv, io, math, time, zipfile
from collections import defaultdict
import numpy as np

ZIP = "data/dataset_seed.zip"
FEE = 0.0004
MAX_REWARD = 3_000_000
INVESTABLE = 40_000_000 * (1 - 0.30)
BTC_CAP, USDT_CAP = INVESTABLE * 0.60, INVESTABLE * 0.40
RATE_10E, RATE_100E = 0.00015, 0.00018


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


def sim(seq, K, m, lower, b, final_price):
    """반환: (완료왕복수, 월말 미청산 MTM 비율합)."""
    if len(seq) < 2 or m < 1:
        return 0, 0.0
    held = np.zeros(K + 2, dtype=bool)
    trips, prev = 0, seq[0]
    for cur in seq[1:]:
        if cur < prev:
            held[cur + 1: prev + 1] = True
        elif cur > prev:
            a, h = max(0, prev + 1 - m), cur - m
            if h >= 0:
                seg = held[a:h + 1]
                if seg.any():
                    trips += int(seg.sum()); held[a:h + 1] = False
        prev = cur
    hl = np.nonzero(held)[0]
    mtm = float(np.sum(final_price / (lower * (1 + b) ** hl) - 1.0)) if len(hl) else 0.0
    return trips, mtm


# 정산 기준 3종 (리워드는 전월 10억↑ 0.015% 기준 — 100억↑도 월 0.1~0.3만 차이로 사실상 동일)
BASES = ["실현", "이월", "월MTM"]


def analyze(candles, labels, deployed, buy_list, sell_lo, sell_hi, periods, rate=RATE_10E):
    """각 (기간 × 정산기준)별 최적 (buy, sell) 탐색."""
    allm = [mo for mo in sorted(set(candles) & set(labels)) if labels[mo]["days"] >= 20]
    best = {lab: {bz: None for bz in BASES} for lab in periods}
    for buy in buy_list:
        b = buy / 100.0
        md = {}
        for mo in allm:
            lab = labels[mo]; lo, hi = lab["l1"], lab["u1"]
            seq, K = compress_levels(candles[mo], lo, hi, b)
            bots = max(1, min(2000, math.ceil((hi - lo) / lo * 100 / buy)))
            md[mo] = (seq, K, deployed / bots, lo, float(np.clip(candles[mo][-1], lo, hi)))
        m = 1
        while True:
            sp = ((1 + b) ** m - 1) * 100
            if sp > sell_hi:
                break
            if sp < sell_lo:
                m += 1; continue
            permo = {}
            for mo in allm:
                seq, K, cpb, lo, fp = md[mo]
                tr, mtm = sim(seq, K, m, lo, b, fp)
                permo[mo] = (tr * (sp / 100) * cpb, tr * 2 * FEE * cpb, tr * 2 * cpb, mtm * cpb)
            for plab, since in periods.items():
                mos = [mo for mo in allm if since is None or mo >= since]
                n = len(mos)
                if not n:
                    continue
                ag = sum(permo[mo][0] for mo in mos) / n
                af = sum(permo[mo][1] for mo in mos) / n
                av = sum(permo[mo][2] for mo in mos) / n
                am = sum(permo[mo][3] for mo in mos) / n          # 월평균 MTM
                last_mtm = permo[mos[-1]][3]                       # 기간말 미청산 1회분
                rw = min(av * rate, MAX_REWARD)
                realized = ag - af + rw
                nets = {"실현": realized,                          # MTM 무시
                        "이월": realized + last_mtm / n,           # 기간말 1회만 분산
                        "월MTM": realized + am}                    # 매월 청산
                for bz in BASES:
                    row = (nets[bz], buy, sp, ag, af, av, am, rw, n)
                    if best[plab][bz] is None or nets[bz] > best[plab][bz][0]:
                        best[plab][bz] = row
            m += 1
    return best


def fmt(row, ref=None):
    net, buy, sp, ag, af, av, am, rw, n = row
    bs = (f"매수 {round(buy/100*ref)}원/매도 {round(sp/100*ref)}원" if ref
          else f"매수 {buy:.2f}%/매도 {sp:.2f}%")
    return (f"{bs:24s} 월순익 {net/1e4:+6.1f}만 (그리드 {ag/1e4:4.1f} −수수료 {af/1e4:.1f} "
            f"+리워드 {rw/1e4:.1f} | 월평균MTM {am/1e4:+5.1f}) 거래량 {av/1e8:.2f}억/월")


def main():
    t0 = time.time()
    with zipfile.ZipFile(ZIP) as zf:
        bc = load_5m(zf, "minute_KRW-BTC_5m.csv");  bl = load_labels(zf, "monthly_labels.csv")
        uc = load_5m(zf, "minute_KRW-USDT_5m.csv"); ul = load_labels(zf, "monthly_labels_usdt.csv")

    print("=" * 96)
    print("₿ BTC/KRW 최적 그리드 간격 — 정산기준 3종 비교 (1σ박스·자본 16.8M·매도 0.2~2.5%·0.01%스윕)")
    print("  [실현]=MTM무시(앱P&L)  [이월]=기간말1회정산  [월MTM]=매월청산(보수)")
    print("=" * 96)
    buy_btc = [round(0.10 + 0.01 * i, 2) for i in range(141)]
    pb = {"전체 2017-12~2026-05": None, "최근 2023-01~": "2023-01", "최근 2024-06~": "2024-06"}
    bb = analyze(bc, bl, BTC_CAP, buy_btc, 0.2, 2.5, pb)
    for plab in pb:
        print(f"[{plab}]")
        for bz in BASES:
            print(f"  {bz:5s}:", fmt(bb[plab][bz]))

    print("\n" + "=" * 96)
    print("💵 USDT/KRW 최적 그리드 간격 — 정산기준 3종 (1σ박스·자본 11.2M·환산 1450원·매도 0.3~3원·1원스윕)")
    print("=" * 96)
    URef = 1450
    buy_u = [round(0.03 + 0.005 * i, 3) for i in range(140)]
    slo, shi = 0.3 / URef * 100, 3.0 / URef * 100
    pu = {"전체 2024-08~2026-05": None, "최근 2025-06~": "2025-06"}
    ub = analyze(uc, ul, USDT_CAP, buy_u, slo, shi, pu)
    for plab in pu:
        print(f"[{plab}]")
        for bz in BASES:
            print(f"  {bz:5s}:", fmt(ub[plab][bz], URef))
    print(f"\n(리워드 전월10억↑ 0.015% 기준 · 100억↑은 월 0.1~0.3만 차이로 최적간격 동일 · 소요 {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
