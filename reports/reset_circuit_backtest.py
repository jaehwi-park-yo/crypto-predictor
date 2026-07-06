"""
데드존 리셋 단축 × MTM 서킷브레이커 조합 백테스트 (5분봉, walk-forward 1σ 박스)
================================================================================
질문: 현행 데드존 리셋(8일 연속 하단 이탈 → 박스 재배치)을 단축하면 나아지는가?
      MTM 서킷브레이커(θ=2%)와 조합하면 상호보완인가 중복인가?

리셋 규칙: 종가가 박스 하단 미만인 날이 N일 연속 → 보유 인벤토리를 현재가로
  청산(MTM 실현, 보수적 가정)하고 같은 폭의 박스를 현재가 중심으로 재배치.
서킷 규칙: 미실현 MTM ≤ −θ×자본 → 신규 매수 정지, −θ/2 회복 시 재개.

스윕: N ∈ {3, 5, 8(현행), 없음} × 서킷 ∈ {없음, 2%}  (8조합)
지표: 월평균 순익(그리드−수수료+실현/미실현 MTM), 최악월, 최악 드로다운, 리셋 횟수.

사용: python3 reports/reset_circuit_backtest.py [--zip PATH]
"""
from __future__ import annotations
import argparse, csv, io, math, zipfile
from collections import defaultdict

FEE = 0.0004
DEPLOYED = 15_679_334
BUY = 0.2          # %
M_STEP = 2         # 매도 = 2스텝(0.4%)
CAL = 1.6          # 실측 왕복 보정 (그리드/수수료에 적용, MTM은 무보정)
RESUME = 0.5


def load_5m_days(zf, name):
    """월 → [(date, close), ...]"""
    by = defaultdict(list)
    with zf.open(name) as raw:
        r = csv.reader(io.TextIOWrapper(raw, "utf-8"))
        next(r)
        for row in r:
            by[row[0][:7]].append((row[0][:10], float(row[4])))
    return by


def load_labels(zf, name):
    out = {}
    with zf.open(name) as raw:
        for row in csv.DictReader(io.TextIOWrapper(raw, "utf-8")):
            out[row["target_month"]] = {
                "l1": float(row["box_l1"]), "u1": float(row["box_u1"]),
                "days": int(row["realized_days"])}
    return out


def sim_month(ticks, lo, hi, reset_days, theta):
    """경로 의존 시뮬. 반환 dict(trips, mtm_final, min_mtm, realized_reset, resets, paused_frac)."""
    b = BUY / 100.0
    width = hi - lo
    logb = math.log(1 + b)

    def mklvl(lo_):
        return lambda p: int(math.floor(math.log(min(max(p, lo_), lo_ + width) / lo_) / logb))

    lvl = mklvl(lo)
    bots = max(1, math.ceil(width / lo * 100 / BUY))
    cpb = DEPLOYED / bots
    held = {}                 # level -> buy_price
    trips = 0
    realized = 0.0            # 리셋 청산 실현손익(krw)
    resets = 0
    consec = 0
    last_day = None
    paused = False
    paused_n = 0
    min_mtm = 0.0
    prev = lvl(ticks[0][1])
    cur_lo = lo

    def mtm_krw(p):
        return sum(p / bp - 1.0 for bp in held.values()) * cpb

    for day, p in ticks[1:]:
        cur = lvl(p)
        if cur < prev:
            if not paused:
                for l in range(cur + 1, prev + 1):
                    if l not in held:
                        held[l] = cur_lo * (1 + b) ** l
            else:
                paused_n += 1
        elif cur > prev:
            for l in [l for l in held if l <= cur - M_STEP]:
                trips += 1
                del held[l]
        prev = cur
        m = mtm_krw(p)
        min_mtm = min(min_mtm, m)
        # 서킷
        if theta is not None:
            if not paused and m <= -theta * DEPLOYED:
                paused = True
            elif paused and m >= -theta * RESUME * DEPLOYED:
                paused = False
        # 일 경계에서 연속 하단이탈 판정 (그 날 마지막 tick 종가 기준 근사: 새 날 시작 시 전일 판정)
        if reset_days is not None and day != last_day:
            if last_day is not None:
                below = p_prev_close < cur_lo
                consec = consec + 1 if below else 0
                if consec >= reset_days:
                    realized += mtm_krw(p)          # 보유 청산 (보수적)
                    held.clear()
                    resets += 1
                    consec = 0
                    cur_lo = p - width / 2          # 현재가 중심 재배치
                    lvl = mklvl(cur_lo)
                    prev = lvl(p)
            last_day = day
        elif last_day is None:
            last_day = day
        p_prev_close = p

    return dict(trips=trips, mtm_final=mtm_krw(ticks[-1][1]), min_mtm=min_mtm,
                realized=realized, resets=resets,
                paused_frac=paused_n / max(1, len(ticks) - 1), cpb=cpb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default="data/dataset_seed.zip")
    args = ap.parse_args()

    with zipfile.ZipFile(args.zip) as zf:
        months5 = load_5m_days(zf, "minute_KRW-BTC_5m.csv")
        labels = load_labels(zf, "monthly_labels.csv")

    months = [mo for mo in sorted(set(months5) & set(labels)) if labels[mo]["days"] >= 20]
    print(f"리셋×서킷 조합 백테스트 — {len(months)}개월, buy {BUY}%/매도 {(1.002**M_STEP-1)*100:.2f}%, "
          f"자본 {DEPLOYED/1e6:.1f}M, 보정 {CAL}")
    hdr = f"{'리셋N':>5} {'서킷':>5} {'월평균순익':>10} {'최악월':>9} {'최악드로다운':>10} {'리셋합':>6} {'월거래량':>8}"
    print(hdr); print("-" * len(hdr))

    sp = (1 + BUY / 100) ** M_STEP - 1
    for reset_days in (3, 5, 8, None):
        for theta in (None, 0.02):
            nets, mns, vols, rst = [], [], [], 0
            for mo in months:
                lab = labels[mo]
                r = sim_month(months5[mo], lab["l1"], lab["u1"], reset_days, theta)
                tr = r["trips"] * CAL
                grid = tr * sp * r["cpb"]
                fee = tr * 2 * FEE * r["cpb"]
                nets.append(grid - fee + r["mtm_final"] + r["realized"])
                mns.append(min(r["min_mtm"], r["realized"] + r["mtm_final"]))
                vols.append(tr * 2 * r["cpb"])
                rst += r["resets"]
            n = len(nets)
            rl = "없음" if reset_days is None else f"{reset_days}일"
            cl = "없음" if theta is None else f"{theta*100:.0f}%"
            print(f"{rl:>5} {cl:>5} {sum(nets)/n/1e4:>+9.1f}만 {min(nets)/1e4:>+8.1f}만 "
                  f"{min(mns)/1e4:>+9.1f}만 {rst:>5} {sum(vols)/n/1e8:>7.2f}억")


if __name__ == "__main__":
    main()
