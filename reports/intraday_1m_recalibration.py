"""
reports/intraday_1m_recalibration.py — 1분봉 거래량 진동 재보정
================================================================
1분봉(unit=1)이 DB에 적재된 뒤 실행. 최근 N개월 박스에 대해 5m·1m 해상도의
oscillation_per_day를 각각 측정하고, 1m/5m 진동 배율을 산출한다.
이 배율이 5m 단일인벤토리 거래량 추정의 과소 보정계수가 된다.

배경: 해상도 스케일링 실측 osc ∝ Δt^-0.60 → 1m은 5m 대비 ~2.7배 진동 포착.
      5m 캔들이 흡수하던 캔들 내부 왕복분을 1m가 직접 계상.

실행:
    python -m reports.intraday_1m_recalibration
    python -m reports.intraday_1m_recalibration --market KRW-USDT --gi 0.1
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime

from utils.intraday_vol import oscillation_per_day
from utils.minute_data import get_stats
from services.prediction_service import predict_as_of
from utils.data_cache import get_history, get_usdt_history

logger = logging.getLogger("intraday_1m_recalibration")


def _recent_month_ends(history, n: int = 6):
    months = sorted({c["date"][:7] for c in history})
    ends = []
    for ym in months[-(n + 1):-1]:
        days = [c["date"] for c in history if c["date"][:7] == ym]
        if days:
            ends.append(max(days))
    return ends


def run(market: str, gi: float, months: int) -> None:
    stats = get_stats()
    have_1m = any(s.get("unit") == 1 and s.get("market") == market for s in stats.values())
    if not have_1m:
        print(f"[경고] {market} 1분봉이 DB에 없습니다. 먼저 수집/업로드하세요:")
        print("        python -m utils.minute_data --bootstrap --unit 1")
        print("   또는 minute_<market>_1m.csv 포함 dataset_seed.zip 업로드 후 재기동.")
        print(f"[현황] {market} 분봉:",
              {k: v["count"] for k, v in stats.items() if v.get("market") == market})
        return

    history = get_usdt_history() if market.endswith("USDT") else get_history()
    ends = _recent_month_ends(history, months)
    if not ends:
        print("[경고] 박스 산출용 일봉 히스토리 부족")
        return

    print(f"=== {market} 거래량 진동 재보정 (gi={gi}%) ===")
    print(f"{'as_of':<12}{'osc_5m':>10}{'osc_1m':>10}{'배율':>8}")
    ratios = []
    for ao in ends:
        try:
            snap = predict_as_of(history, ao,
                                 symbol=("USDT" if market.endswith("USDT") else "BTC"))
            lo, hi = snap.recommended_lower, snap.recommended_upper
        except Exception as e:
            print(f"{ao:<12}  예측 실패: {e}")
            continue
        o5 = oscillation_per_day(market, ao, gi, lo, hi, unit=5)
        o1 = oscillation_per_day(market, ao, gi, lo, hi, unit=1)
        if o5 and o1 and o5 > 0:
            r = o1 / o5
            ratios.append(r)
            print(f"{ao:<12}{o5:>10.1f}{o1:>10.1f}{r:>8.2f}")
        else:
            print(f"{ao:<12}{(o5 or 0):>10.1f}{(o1 or 0):>10.1f}{'  n/a':>8}")

    if ratios:
        avg = sum(ratios) / len(ratios)
        print("-" * 40)
        print(f"평균 1m/5m 진동 배율 = {avg:.2f}x  (n={len(ratios)})")
        print(f"→ 권장 거래량 보정계수: index.html BTC_COEF/USDT_EFF에 ×{avg:.2f} 적용")
        print(f"  (예: BTC inner volPerCap 3.11 → {3.11 * avg:.2f})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="1분봉 거래량 진동 재보정")
    ap.add_argument("--market", default="KRW-BTC")
    ap.add_argument("--gi", type=float, default=0.5, help="그리드 간격 %%")
    ap.add_argument("--months", type=int, default=6)
    args = ap.parse_args()
    run(args.market, args.gi, args.months)
