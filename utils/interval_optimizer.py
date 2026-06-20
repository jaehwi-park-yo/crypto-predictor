"""
utils/interval_optimizer.py — 1분봉 기반 최적 매수/매도 간격 추정
==================================================================
1분봉 단일인벤토리 시뮬레이션으로 두 가지 운영 모드의 최적 간격을 산출한다.

  · 부스트 모드: 월초 목표 거래량 조기 달성 목적. 간격을 좁혀 거래량 극대화.
                탐색 공간을 '좁은 간격' 영역으로 제한.
  · 일반 모드:  목표 달성 후 매매 스프레드 수익 극대화. 간격을 넓혀 왕복 순익 극대화.
                탐색 공간을 '넓은 간격' 영역으로 제한.

시뮬 모델 (grid_interval_backtest.py 와 동일):
  - 라인 j 하락 → 매수 (단일인벤토리, 동일 라인 재매수 금지)
  - j+m 라인 도달 → 익절 매도 후 재무장
  - 월말 미청산 인벤토리 MTM 정산 (광폭매도 발산 방지)

데이터 소스: utils.minute_data.get_candles (data/candles.db, unit=1)
             1분봉 없으면 5분봉 폴백 (결과 정밀도는 낮아짐)

API:
    from utils.interval_optimizer import optimize_intervals
    result = optimize_intervals(
        market="KRW-BTC",
        as_of="2026-06-30",          # 예측 기준일 (이 날까지의 데이터 사용)
        box_lower=95_000_000,
        box_upper=115_000_000,
        deployed_krw=16_800_000,
        lookback_months=3,            # 최근 N개월 1m 데이터 사용
    )
    print(result["boost"])   # 부스트 모드 최적 결과
    print(result["normal"])  # 일반 모드 최적 결과

CLI:
    python -m utils.interval_optimizer
    python -m utils.interval_optimizer --market KRW-USDT --months 2
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("interval_optimizer")

FEE = 0.0004          # 빗썸 편도 수수료 0.04%
MAX_REWARD = 3_000_000

# 리워드 티어 [최소거래량, 요율]
_REWARD_TIERS = [
    (1e11, 0.0002),
    (1e10, 0.00018),
    (1e9,  0.00015),
    (1e8,  0.00008),
    (1e7,  0.00005),
    (0,    0.00003),
]

# 모드별 간격 탐색 범위 정의
# BTC: 퍼센트(%) 단위
_BTC_SEARCH = {
    "boost":  {"buy": (0.10, 0.40, 0.01), "sell_pct": (0.1, 1.0)},   # 좁은 간격
    "normal": {"buy": (0.30, 1.20, 0.05), "sell_pct": (0.3, 3.0)},   # 넓은 간격
}
# USDT: 원 단위 (내부는 % 환산)
_USDT_SEARCH = {
    "boost":  {"buy_krw": (1, 5,  1), "sell_krw": (1, 8)},
    "normal": {"buy_krw": (3, 15, 1), "sell_krw": (3, 20)},
}
_USDT_REF = 1450.0   # 원/달러 환산 기준 (BTC 단위 통일용)


def _reward_rate(vol: float) -> float:
    for threshold, rate in _REWARD_TIERS:
        if vol >= threshold:
            return rate
    return _REWARD_TIERS[-1][1]


def _fetch_1m(market: str, as_of: str, lookback_months: int) -> List[Dict]:
    """1분봉 우선, 없으면 5분봉 폴백. as_of 이전 lookback_months개월치."""
    from datetime import datetime, timedelta
    try:
        from utils.minute_data import get_candles, has_data
    except Exception as e:
        logger.warning("[optimizer] minute_data 임포트 실패: %s", e)
        return []

    end_dt = datetime.strptime(as_of[:10], "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=lookback_months * 31 + 5)
    start = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    end   = end_dt.strftime("%Y-%m-%dT23:59:59")

    for unit in (1, 5):
        try:
            rows = get_candles(market, unit=unit, start=start, end=end)
        except Exception:
            rows = []
        if rows:
            if unit != 1:
                logger.info("[optimizer] %s 1분봉 부재 → %dm 폴백 (정밀도 낮음)", market, unit)
            return rows
    return []


def _split_by_month(candles: List[Dict]) -> Dict[str, np.ndarray]:
    """캔들 리스트 → {YYYY-MM: 종가 배열}."""
    by: Dict[str, List[float]] = {}
    for c in candles:
        m = c["ts"][:7]
        by.setdefault(m, []).append(float(c["close"]))
    return {m: np.array(v, dtype=float) for m, v in by.items()}


def _compress(prices: np.ndarray, lower: float, b: float, upper: float
              ) -> Tuple[np.ndarray, int]:
    """가격 시계열 → 그리드 라인 인덱스 시계열 (중복 제거)."""
    p = np.clip(prices, lower, upper)
    idx = np.floor(np.log(p / lower) / math.log(1 + b)).astype(np.int64)
    seq = idx[np.concatenate(([True], idx[1:] != idx[:-1]))]
    return seq, int(seq.max()) if len(seq) else 0


def _sim(seq: np.ndarray, K: int, m: int, lower: float,
         b: float, final_price: float) -> Tuple[int, float]:
    """
    단일인벤토리 그리드 시뮬. 반환: (완료왕복수, 월말 MTM 비율합).
    grid_interval_backtest.py 의 sim() 와 동일 알고리즘.
    """
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
                seg = held[a: h + 1]
                if seg.any():
                    trips += int(seg.sum())
                    held[a: h + 1] = False
        prev = cur
    hl = np.nonzero(held)[0]
    mtm = float(np.sum(final_price / (lower * (1 + b) ** hl) - 1.0)) if len(hl) else 0.0
    return trips, mtm


def _best_interval(
    monthly: Dict[str, np.ndarray],
    box_lower: float,
    box_upper: float,
    deployed: float,
    buy_range: Tuple[float, float, float],   # (min%, max%, step%)
    sell_pct_range: Tuple[float, float],
    prev_vol_monthly: float = 1e9,
) -> Optional[Dict]:
    """
    (buy%, sell%) 격자 탐색 → 월MTM 기준 최적 조합 반환.
    prev_vol_monthly: 전월 거래량 (리워드 티어 판정용).
    """
    rate = _reward_rate(prev_vol_monthly)
    months = sorted(monthly)
    if not months:
        return None

    b_min, b_max, b_step = buy_range
    sell_lo, sell_hi = sell_pct_range
    best = None

    b = b_min
    while b <= b_max + 1e-9:
        b = round(b, 4)
        # 월별 시뮬 준비
        prepped = {}
        for mo in months:
            prices = monthly[mo]
            if len(prices) < 60:
                continue
            seq, K = _compress(prices, box_lower, b / 100, box_upper)
            bots = max(1, min(2000, math.ceil((box_upper - box_lower) / box_lower * 100 / b)))
            cap_per_bot = deployed / bots
            final = float(np.clip(prices[-1], box_lower, box_upper))
            prepped[mo] = (seq, K, cap_per_bot, box_lower, b / 100, final)

        if not prepped:
            b += b_step
            continue

        # m=1,2,3,... : 매도 스텝 탐색
        m = 1
        while True:
            sell_pct = ((1 + b / 100) ** m - 1) * 100
            if sell_pct > sell_hi:
                break
            if sell_pct >= sell_lo:
                nets, vols, grids, mtms = [], [], [], []
                for mo, (seq, K, cpb, lo, br, fp) in prepped.items():
                    tr, mtm = _sim(seq, K, m, lo, br, fp)
                    sp = sell_pct / 100
                    grid_profit  = tr * sp * cpb
                    fee_cost     = tr * 2 * FEE * cpb
                    vol          = tr * 2 * cpb
                    mtm_krw      = mtm * cpb
                    rw = min(vol * rate, MAX_REWARD)
                    net = grid_profit - fee_cost + rw + mtm_krw
                    nets.append(net); vols.append(vol)
                    grids.append(grid_profit - fee_cost); mtms.append(mtm_krw)

                n = len(nets)
                avg_net  = sum(nets)  / n
                avg_vol  = sum(vols)  / n
                avg_grid = sum(grids) / n
                avg_mtm  = sum(mtms)  / n
                avg_rw   = min(avg_vol * rate, MAX_REWARD)

                if best is None or avg_net > best["net"]:
                    best = {
                        "buy_pct":   b,
                        "sell_pct":  round(sell_pct, 4),
                        "sell_m":    m,
                        "net":       avg_net,
                        "grid":      avg_grid,
                        "reward":    avg_rw,
                        "mtm":       avg_mtm,
                        "vol":       avg_vol,
                        "n_months":  n,
                        "rate":      rate,
                    }
            m += 1

        b = round(b + b_step, 4)

    return best


def optimize_intervals(
    market: str = "KRW-BTC",
    as_of: Optional[str] = None,
    box_lower: Optional[float] = None,
    box_upper: Optional[float] = None,
    deployed_krw: float = 16_800_000,
    lookback_months: int = 3,
    prev_vol_monthly: float = 1e9,
) -> Dict:
    """
    1분봉(또는 5분봉 폴백)으로 부스트/일반 모드 최적 간격을 각각 산출.

    box_lower/upper 미지정 시 prediction_service.predict_as_of로 자동 산출.
    반환 dict:
        unit         : 실제 사용 분봉 단위 (1 또는 5)
        months_used  : 시뮬에 사용된 월 목록
        boost        : 부스트 모드 최적 결과 (buy_pct, sell_pct, net, vol, …)
        normal       : 일반 모드 최적 결과
    """
    if as_of is None:
        as_of = datetime.now().strftime("%Y-%m-%d")

    # 박스 자동 산출
    if box_lower is None or box_upper is None:
        try:
            from utils.data_cache import get_history, get_usdt_history
            from services.prediction_service import predict_as_of
            hist = get_usdt_history() if market.endswith("USDT") else get_history()
            symbol = "USDT" if market.endswith("USDT") else "BTC"
            snap = predict_as_of(hist, as_of, symbol=symbol)
            box_lower = snap.recommended_lower
            box_upper = snap.recommended_upper
            logger.info("[optimizer] 박스 자동산출: %.0f ~ %.0f", box_lower, box_upper)
        except Exception as e:
            raise ValueError(f"box_lower/upper 미지정이고 자동산출 실패: {e}")

    # 1분봉 로드
    candles = _fetch_1m(market, as_of, lookback_months)
    unit_used = 1
    if candles:
        # 실제 사용 단위 추정 (ts 간격으로)
        if len(candles) > 1:
            ts0 = datetime.strptime(candles[0]["ts"][:16], "%Y-%m-%dT%H:%M")
            ts1 = datetime.strptime(candles[1]["ts"][:16], "%Y-%m-%dT%H:%M")
            gap = int((ts1 - ts0).total_seconds() / 60)
            unit_used = gap if gap in (1, 3, 5, 10, 15) else 5
    else:
        return {"error": "분봉 데이터 없음. 1분봉 수집 후 재시도.", "unit": 0,
                "months_used": [], "boost": None, "normal": None}

    monthly = _split_by_month(candles)
    months_used = sorted(monthly)
    logger.info("[optimizer] %s: %dm %d개월(%s~%s) %d캔들",
                market, unit_used, len(months_used),
                months_used[0] if months_used else "-",
                months_used[-1] if months_used else "-", len(candles))

    is_usdt = market.endswith("USDT")

    if is_usdt:
        ref = _USDT_REF
        def _krw_to_pct(krw): return krw / ref * 100

        boost_buy_range = (
            _krw_to_pct(_USDT_SEARCH["boost"]["buy_krw"][0]),
            _krw_to_pct(_USDT_SEARCH["boost"]["buy_krw"][1]),
            _krw_to_pct(_USDT_SEARCH["boost"]["buy_krw"][2]),
        )
        boost_sell_range = (
            _krw_to_pct(_USDT_SEARCH["boost"]["sell_krw"][0]),
            _krw_to_pct(_USDT_SEARCH["boost"]["sell_krw"][1]),
        )
        normal_buy_range = (
            _krw_to_pct(_USDT_SEARCH["normal"]["buy_krw"][0]),
            _krw_to_pct(_USDT_SEARCH["normal"]["buy_krw"][1]),
            _krw_to_pct(_USDT_SEARCH["normal"]["buy_krw"][2]),
        )
        normal_sell_range = (
            _krw_to_pct(_USDT_SEARCH["normal"]["sell_krw"][0]),
            _krw_to_pct(_USDT_SEARCH["normal"]["sell_krw"][1]),
        )
    else:
        boost_buy_range  = tuple(_BTC_SEARCH["boost"]["buy"])
        boost_sell_range = tuple(_BTC_SEARCH["boost"]["sell_pct"])
        normal_buy_range = tuple(_BTC_SEARCH["normal"]["buy"])
        normal_sell_range = tuple(_BTC_SEARCH["normal"]["sell_pct"])

    boost  = _best_interval(monthly, box_lower, box_upper, deployed_krw,
                            boost_buy_range,  boost_sell_range,  prev_vol_monthly)
    normal = _best_interval(monthly, box_lower, box_upper, deployed_krw,
                            normal_buy_range, normal_sell_range, prev_vol_monthly)

    # USDT는 원 단위로 역환산해서 표시 필드 추가
    for res in (boost, normal):
        if res and is_usdt:
            res["buy_krw"]  = round(res["buy_pct"]  / 100 * ref, 1)
            res["sell_krw"] = round(res["sell_pct"] / 100 * ref, 1)

    return {
        "market":      market,
        "as_of":       as_of,
        "box_lower":   box_lower,
        "box_upper":   box_upper,
        "unit":        unit_used,
        "months_used": months_used,
        "boost":       boost,
        "normal":      normal,
    }


def _fmt(res: Optional[Dict], is_usdt: bool = False) -> str:
    if not res:
        return "  결과 없음 (데이터 부족)"
    if is_usdt:
        interval = f"매수 {res['buy_krw']}원 / 매도 {res['sell_krw']}원"
    else:
        interval = f"매수 {res['buy_pct']:.2f}% / 매도 {res['sell_pct']:.2f}%"
    return (
        f"  {interval}\n"
        f"  → 월순익 {res['net']/1e4:+.1f}만  "
        f"(그리드 {res['grid']/1e4:.1f} + 리워드 {res['reward']/1e4:.1f} + MTM {res['mtm']/1e4:+.1f})\n"
        f"  → 월거래량 {res['vol']/1e8:.2f}억  ({res['n_months']}개월 평균)"
    )


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, json, time
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

    ap = argparse.ArgumentParser(description="1분봉 기반 최적 매수/매도 간격 추정")
    ap.add_argument("--market",  default="KRW-BTC")
    ap.add_argument("--as-of",   default=None, help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--months",  type=int, default=3, help="사용할 과거 개월수")
    ap.add_argument("--capital", type=float, default=40_000_000)
    ap.add_argument("--btc-ratio", type=float, default=0.60)
    ap.add_argument("--krw-hold", type=float, default=0.30)
    ap.add_argument("--prev-vol", type=float, default=1e9,
                    help="전월 누적 거래량 (리워드 티어 판정, 기본 10억)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    args = ap.parse_args()

    deployed = args.capital * (1 - args.krw_hold)
    if args.market.endswith("USDT"):
        deployed *= (1 - args.btc_ratio)
    else:
        deployed *= args.btc_ratio

    t0 = time.time()
    r = optimize_intervals(
        market=args.market,
        as_of=args.as_of,
        deployed_krw=deployed,
        lookback_months=args.months,
        prev_vol_monthly=args.prev_vol,
    )

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    else:
        is_u = args.market.endswith("USDT")
        tag  = "1m" if r.get("unit") == 1 else f"{r.get('unit','?')}m(폴백)"
        print(f"\n{'='*64}")
        print(f"  {args.market}  [{tag}]  {r.get('months_used',['?'])[0]}~{r.get('months_used',['?'])[-1]}")
        print(f"  박스: {r['box_lower']:,.0f} ~ {r['box_upper']:,.0f}  투입자본: {deployed/1e6:.1f}M")
        print(f"{'='*64}")
        print("▶ 부스트 모드 (거래량 극대화 — 월초 목표 조기 달성)")
        print(_fmt(r.get("boost"), is_u))
        print("▶ 일반 모드  (스프레드 수익 극대화 — 목표 달성 후)")
        print(_fmt(r.get("normal"), is_u))
        print(f"{'='*64}")
        print(f"  소요: {time.time()-t0:.1f}s")
