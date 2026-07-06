"""
utils/interval_optimizer.py — 1분봉 기반 최적 매수/매도 간격 추정
==================================================================
1분봉 단일인벤토리 시뮬레이션으로 시장별 운영 전략을 산출한다.

BTC (3모드 탐색):
  · 부스트 모드: 월초 목표 거래량 조기 달성 목적. 간격을 좁혀 거래량 극대화.
  · 일반 모드:  목표 달성 후 매매 스프레드 수익 극대화. 간격을 넓혀 왕복 순익 극대화.
  · 듀얼 모드:  1σ 밴드=부스트 + 2σ 밴드=일반 자본분할 병행.

USDT (고정 트리플 오버레이 — 탐색 없음):
  · T1: 1σ 밴드 · 1원/1원  (거래량·리워드)
  · T2: 1.5σ 밴드 · 1원/2원 (균형)
  · T3: 2σ 밴드 · 1원/3원  (스프레드 수익)
  세 leg 동시 중복 운영, 자본 3등분, 거래량 합산 후 리워드 1회 적용.

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

# 단일인벤토리 시뮬 거래량 보정계수 (실측 기반, config에서 로드)
try:
    from config import VOLUME_SIM_CALIB as _SIM_CALIB
except Exception:
    _SIM_CALIB = 1.6

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
# BTC: 퍼센트(%) 단위.
#   부스트(거래량 목적): 실측 운영(매수 2만원≈0.017%, 매도 0.08%)을 반영해
#     초촘촘 간격까지 탐색. 매도 하한은 왕복수수료(0.08%) 수준.
#   일반(순익 목적): 넓은 간격으로 스프레드 수익 극대화.
_BTC_SEARCH = {
    "boost":  {"buy": (0.02, 0.30, 0.01), "sell_pct": (0.08, 0.80)},  # 초촘촘
    "normal": {"buy": (0.30, 1.20, 0.05), "sell_pct": (0.30, 3.0)},   # 넓은 간격
}
# USDT: 고정 트리플 오버레이 전략 (탐색 없음 — 2026-06 재구성).
#   부스트/일반 간격 탐색을 폐지하고, 예측 박스권 기반 3개 leg를 동시에 중복 운영:
#     T1: 1σ   밴드 · 1원 매수 / 1원 매도  (거래량·리워드 담당, 그리드 자체는 수수료 상쇄)
#     T2: 1.5σ 밴드 · 1원 매수 / 2원 매도  (균형)
#     T3: 2σ   밴드 · 1원 매수 / 3원 매도  (스프레드 수익 담당)
#   자본은 3등분(기본). 거래량은 합산 후 리워드 1회(월 300만 상한) 적용.
_USDT_BUY_KRW = 1                       # 매수 간격 고정 (최소틱)
_USDT_TRIPLE = [
    # (이름, 매도간격 KRW, 밴드 종류)
    ("1σ",   1, "1s"),
    ("1.5σ", 2, "15"),
    ("2σ",   3, "2s"),
]
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
    objective: str = "net",                  # "net"=매매순익 최대 / "volume"=거래량 최대
) -> Optional[Dict]:
    """
    (buy%, sell%) 격자 탐색 → objective 기준 최적 조합 반환.

    objective="net"    : 월 순익(그리드+리워드+MTM) 최대 — 일반 모드(스프레드 수익).
    objective="volume" : 월 거래량 최대 (단, 매매순익이 깨지지 않는 범위:
                         grid-fee >= 0) — 부스트 모드(리워드 티어 조기달성).
                         실측 검증: 부스트는 거래량이 목적이므로 매도≈수수료(0.08%)
                         초촘촘 간격을 허용해야 실제 운영(2만원 매수/0.08% 매도)과 일치.
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
                    tr *= _SIM_CALIB   # 실측 보정 (단일인벤토리 과소측정 교정)
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

                # objective 별 선택 기준
                if objective == "volume":
                    # 거래량 최대화 — 단 그리드 매매가 수수료를 까먹지 않는 범위(grid>=0)
                    feasible = avg_grid >= 0
                    better = best is None or (feasible and avg_vol > best["vol"])
                    # 적격 후보가 하나도 없을 때를 대비해 net 기준 폴백도 허용
                    if best is not None and not best.get("_feasible", True):
                        if feasible:
                            better = True
                        else:
                            # 전 조합 infeasible이면 net 최대 조합을 폴백으로 유지
                            # (최초 후보 고수 시 최악의 수수료 잠식 간격이 추천될 수 있음)
                            better = avg_net > best["net"]
                else:
                    feasible = True
                    better = best is None or avg_net > best["net"]

                if better:
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
                        "_feasible": feasible,
                    }
            m += 1

        b = round(b + b_step, 4)

    if best is not None:
        best.pop("_feasible", None)
    return best


def _optimize_triple_alloc(legs: List[Dict], deployed: float, rate: float,
                           vol_target: float = 0.0,
                           step: float = 0.05, min_w: float = 0.10) -> Dict:
    """트리플 leg 최적 자본배분 탐색 (가중치 격자, 기본 5% 스텝 · leg 최소 10%).

    leg 시뮬 결과(grid/mtm/vol)는 자본에 선형 → 균등배분(1/3) 결과를 단위환산해
    가중치 조합만 평가한다. 리워드는 합산 거래량에 티어 요율 1회(비선형: 300만 상한).

    목적함수:
      vol_target > 0 : 거래량 ≥ 목표 조합 중 순익 최대 (미달 시 거래량 최대 폴백)
      vol_target = 0 : 순익(그리드+MTM+리워드) 최대
    """
    # 균등배분 시뮬의 자본 → 자본 1원당 계수
    per = []
    for l in legs:
        c = max(l["cap"], 1e-9)
        per.append({"vol": l["vol"] / c, "gm": (l["grid"] + l["mtm"]) / c})

    def _eval(ws):
        vol = sum(per[i]["vol"] * ws[i] * deployed for i in range(3))
        gm  = sum(per[i]["gm"]  * ws[i] * deployed for i in range(3))
        reward = min(vol * rate, MAX_REWARD)
        return vol, gm + reward, reward

    best = None          # 목적 충족 최적
    best_vol = None      # 폴백: 거래량 최대
    n_steps = int(round((1 - 3 * min_w) / step)) + 1
    for i in range(n_steps):
        w1 = min_w + i * step
        for j in range(n_steps):
            w2 = min_w + j * step
            w3 = 1.0 - w1 - w2
            if w3 < min_w - 1e-9:
                continue
            ws = (round(w1, 2), round(w2, 2), round(w3, 2))
            vol, net, reward = _eval(ws)
            cand = {"weights": list(ws), "vol": vol, "net": net, "reward": reward}
            if best_vol is None or vol > best_vol["vol"]:
                best_vol = cand
            feasible = vol_target <= 0 or vol >= vol_target
            if feasible and (best is None or net > best["net"]):
                best = cand

    pick = best or best_vol
    reached = best is not None
    # 균등배분 대비 개선폭
    eq_vol, eq_net, _ = _eval((1/3, 1/3, 1/3))
    pick["caps"] = [round(w * deployed) for w in pick["weights"]]
    pick["vol_target"] = vol_target
    pick["target_reached"] = reached
    pick["vs_equal"] = {"net_delta": pick["net"] - eq_net, "vol_delta": pick["vol"] - eq_vol}
    return pick


def _combine_dual(inner: Optional[Dict], outer: Optional[Dict],
                  prev_vol_monthly: float) -> Optional[Dict]:
    """듀얼모드 결합: 1σ 부스트(inner) + 2σ 일반(outer) leg을 합산.

    거래량은 두 leg 합산, 리워드는 합산 거래량에 1회만 요율·상한 적용(중복 방지).
    grid/mtm은 리워드와 무관하므로 단순 합산.
    """
    if not inner and not outer:
        return None
    legs = [x for x in (inner, outer) if x]
    total_vol  = sum(x["vol"]  for x in legs)
    total_grid = sum(x["grid"] for x in legs)   # 이미 수수료 차감됨
    total_mtm  = sum(x["mtm"]  for x in legs)
    rate = _reward_rate(prev_vol_monthly)
    reward = min(total_vol * rate, MAX_REWARD)
    net = total_grid + reward + total_mtm
    return {
        "net":    net,
        "grid":   total_grid,
        "reward": reward,
        "mtm":    total_mtm,
        "vol":    total_vol,
        "rate":   rate,
        "n_months": max((x.get("n_months", 0) for x in legs), default=0),
        # 1σ(부스트) / 2σ(일반) leg 상세 — UI 표시·캘리브레이션용
        "inner":  inner,   # 1σ 부스트 leg
        "outer":  outer,   # 2σ 일반 leg
    }


def optimize_intervals(
    market: str = "KRW-BTC",
    as_of: Optional[str] = None,
    box_lower: Optional[float] = None,
    box_upper: Optional[float] = None,
    deployed_krw: float = 16_800_000,
    lookback_months: int = 3,
    prev_vol_monthly: float = 1e9,
    dual_inner_ratio: float = 0.6,
    usdt_vol_target: float = 0.0,   # USDT 트리플 배분 탐색용 월 거래량 목표(KRW, 0=순익최대)
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

    # 듀얼모드용 1σ/2σ 밴드 (자동산출 시 snap에서 확보). 명시 박스만 주면 None → 듀얼 생략.
    band_1s = band_2s = None

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
            # 비대칭 σ 우선, 없으면 대칭 σ 밴드
            l1 = snap.box_lower_1s_asym or snap.box_lower_1s
            u1 = snap.box_upper_1s_asym or snap.box_upper_1s
            l2 = snap.box_lower_2s_asym or snap.box_lower_2s
            u2 = snap.box_upper_2s_asym or snap.box_upper_2s
            if l1 and u1 and l2 and u2 and u1 > l1 and u2 > l2:
                band_1s = (l1, u1)
                band_2s = (l2, u2)
            logger.info("[optimizer] 박스 자동산출: %.0f ~ %.0f (1σ %.0f~%.0f / 2σ %.0f~%.0f)",
                        box_lower, box_upper, l1 or 0, u1 or 0, l2 or 0, u2 or 0)
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
        # ── USDT: 고정 트리플 오버레이 (탐색 없음) ──────────────────────
        # 1σ 1/1원 + 1.5σ 1/2원 + 2σ 1/3원을 동시에 중복 운영. 자본 3등분.
        # ref는 박스 중점 사용 — 상수(1450) 고정 시 실가격과 어긋나 "1원 간격"의
        # % 환산이 편향돼 거래량·순익이 체계적으로 왜곡됨(±3%대).
        # buy/sell 목표가 같은 ref를 쓰므로 m 스텝 매핑은 ref와 무관하게 정확.
        ref = (box_lower + box_upper) / 2 if (box_lower and box_upper) else _USDT_REF
        def _krw_to_pct(krw): return krw / ref * 100

        # 밴드: 자동산출 시 band_1s/2s 확보. 1.5σ는 선형 중간값(밴드가 ref(1±k·σ)라 정확).
        if band_1s and band_2s:
            bands = {
                "1s": band_1s,
                "15": ((band_1s[0] + band_2s[0]) / 2, (band_1s[1] + band_2s[1]) / 2),
                "2s": band_2s,
            }
        else:
            # 명시 박스만 주어진 경우: 세 leg 모두 같은 박스에서 평가 (밴드 정보 없음)
            bands = {k: (box_lower, box_upper) for k in ("1s", "15", "2s")}

        buy_pct = _krw_to_pct(_USDT_BUY_KRW)
        fixed_buy = (buy_pct, buy_pct, max(buy_pct, 1e-6))
        cap_leg = deployed_krw / len(_USDT_TRIPLE)

        legs = []
        for name, sell_krw, band_key in _USDT_TRIPLE:
            lo, hi = bands[band_key]
            target = _krw_to_pct(sell_krw)
            # 매도 목표 주변만 허용 → m(매도 스텝)이 정확히 sell_krw원에 대응하는 1점만 평가
            leg = _best_interval(monthly, lo, hi, cap_leg,
                                 fixed_buy, (target * 0.75, target * 1.25),
                                 prev_vol_monthly, objective="net")
            if leg:
                leg["sigma"]    = name
                leg["buy_krw"]  = float(_USDT_BUY_KRW)
                leg["sell_krw"] = float(sell_krw)
                leg["cap"]      = cap_leg
                leg["band"]     = [lo, hi]
                # leg 순익에서 리워드 제외 (리워드는 합산 거래량에 1회만 적용)
                leg["net_excl_reward"] = leg["grid"] + leg["mtm"]
            legs.append(leg)

        valid = [l for l in legs if l]
        rate = _reward_rate(prev_vol_monthly)
        total_vol  = sum(l["vol"]  for l in valid)
        total_grid = sum(l["grid"] for l in valid)
        total_mtm  = sum(l["mtm"]  for l in valid)
        reward = min(total_vol * rate, MAX_REWARD)
        triple = {
            "legs":   legs,
            "vol":    total_vol,
            "grid":   total_grid,
            "mtm":    total_mtm,
            "reward": reward,
            "net":    total_grid + reward + total_mtm,
            "rate":   rate,
            "cap_per_leg": cap_leg,
            "n_months": max((l.get("n_months", 0) for l in valid), default=0),
        } if valid else None

        # 최적 자본배분 탐색 — leg 결과가 자본에 선형(거래횟수는 자본과 무관,
        # 봇당자본 ∝ 자본)이므로 leg당 1회 시뮬 값으로 가중치만 탐색하면 된다.
        if triple and len(valid) == len(legs):
            triple["alloc"] = _optimize_triple_alloc(
                legs, deployed_krw, rate, vol_target=usdt_vol_target)

        return {
            "market":      market,
            "as_of":       as_of,
            "box_lower":   box_lower,
            "box_upper":   box_upper,
            "unit":        unit_used,
            "months_used": months_used,
            "strategy":    "triple",
            "triple":      triple,
            # 구 모드 폐지 — 프론트 하위호환용 키 유지
            "boost":       None,
            "normal":      None,
            "dual":        None,
        }

    # ── BTC: 기존 일반/부스트/듀얼 3모드 ────────────────────────────────
    boost_buy_range  = tuple(_BTC_SEARCH["boost"]["buy"])
    boost_sell_range = tuple(_BTC_SEARCH["boost"]["sell_pct"])
    normal_buy_range = tuple(_BTC_SEARCH["normal"]["buy"])
    normal_sell_range = tuple(_BTC_SEARCH["normal"]["sell_pct"])

    boost  = _best_interval(monthly, box_lower, box_upper, deployed_krw,
                            boost_buy_range,  boost_sell_range,  prev_vol_monthly,
                            objective="volume")   # 부스트 = 거래량 최대화
    normal = _best_interval(monthly, box_lower, box_upper, deployed_krw,
                            normal_buy_range, normal_sell_range, prev_vol_monthly,
                            objective="net")       # 일반 = 매매순익 최대화

    # 듀얼모드: 1σ 밴드=부스트(거래량) + 2σ 밴드=일반(순익), 자본 분할 후 거래량 합산.
    dual = None
    if band_1s and band_2s:
        r = min(0.95, max(0.05, dual_inner_ratio))
        dual_inner = _best_interval(monthly, band_1s[0], band_1s[1], deployed_krw * r,
                                    boost_buy_range, boost_sell_range, prev_vol_monthly,
                                    objective="volume")
        dual_outer = _best_interval(monthly, band_2s[0], band_2s[1], deployed_krw * (1 - r),
                                    normal_buy_range, normal_sell_range, prev_vol_monthly,
                                    objective="net")
        dual = _combine_dual(dual_inner, dual_outer, prev_vol_monthly)
        if dual:
            dual["inner_ratio"] = r
            dual["band_1s"] = list(band_1s)
            dual["band_2s"] = list(band_2s)

    return {
        "market":      market,
        "as_of":       as_of,
        "box_lower":   box_lower,
        "box_upper":   box_upper,
        "unit":        unit_used,
        "months_used": months_used,
        "strategy":    "modes",
        "boost":       boost,
        "normal":      normal,
        "dual":        dual,
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
    elif r.get("error"):
        print(f"\n⚠️ {r['error']}")
    else:
        is_u = args.market.endswith("USDT")
        tag  = "1m" if r.get("unit") == 1 else f"{r.get('unit','?')}m(폴백)"
        print(f"\n{'='*64}")
        print(f"  {args.market}  [{tag}]  {r.get('months_used',['?'])[0]}~{r.get('months_used',['?'])[-1]}")
        print(f"  박스: {r['box_lower']:,.0f} ~ {r['box_upper']:,.0f}  투입자본: {deployed/1e6:.1f}M")
        print(f"{'='*64}")
        tp = r.get("triple")
        if tp:
            print("▶ USDT 트리플 오버레이 (1σ 1/1 + 1.5σ 1/2 + 2σ 1/3 동시 운영)")
            for leg in tp["legs"]:
                if not leg:
                    print("  · leg 결과 없음 (데이터 부족)")
                    continue
                print(f"  · {leg['sigma']:4} 매수 {leg['buy_krw']:.0f}원/매도 {leg['sell_krw']:.0f}원 "
                      f"[{leg['band'][0]:,.0f}~{leg['band'][1]:,.0f}] "
                      f"→ 거래량 {leg['vol']/1e8:.2f}억 · 그리드+MTM {leg['net_excl_reward']/1e4:+.1f}만")
            print(f"  → 합산 월순익 {tp['net']/1e4:+.1f}만 "
                  f"(그리드 {tp['grid']/1e4:.1f} + 리워드 {tp['reward']/1e4:.1f} + MTM {tp['mtm']/1e4:+.1f})"
                  f" · 월거래량 {tp['vol']/1e8:.2f}억")
        else:
            print("▶ 부스트 모드 (거래량 극대화 — 월초 목표 조기 달성)")
            print(_fmt(r.get("boost"), is_u))
            print("▶ 일반 모드  (스프레드 수익 극대화 — 목표 달성 후)")
            print(_fmt(r.get("normal"), is_u))
            dl = r.get("dual")
            if dl:
                print(f"▶ 듀얼 모드  (1σ 부스트 + 2σ 일반 · 1σ자본 {dl.get('inner_ratio',0)*100:.0f}%)")
                print(f"  · 1σ 부스트 leg:\n{_fmt(dl.get('inner'), is_u)}")
                print(f"  · 2σ 일반 leg:\n{_fmt(dl.get('outer'), is_u)}")
                print(f"  → 합산 월순익 {dl['net']/1e4:+.1f}만 "
                      f"(그리드 {dl['grid']/1e4:.1f} + 리워드 {dl['reward']/1e4:.1f} + MTM {dl['mtm']/1e4:+.1f})"
                      f" · 월거래량 {dl['vol']/1e8:.2f}억")
        print(f"{'='*64}")
        print(f"  소요: {time.time()-t0:.1f}s")
