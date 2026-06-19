"""
utils/intraday_vol.py — 분봉 기반 일중 변동성·진동 통계 (as-of 재현 가능)
==========================================================================
박스권 예측과 그리드 거래량 추정을 같은 분봉 경로에서 동시에 산출하는 공용 유틸.
모든 함수는 as_of 시점 이전 분봉만 사용(미래정보 누설 없음).

해상도(unit) 전략 — 1분봉 통합 재설계:
  · σ(박스):     INTRADAY_SIGMA_UNIT  (기본 5m — 마이크로구조 노이즈 회피)
  · 거래량(진동): INTRADAY_VOLUME_UNIT (기본 1m — 캔들 내부 왕복 포착)
  요청 단위 분봉이 부족하면 INTRADAY_FALLBACK_UNIT(5m) → 호출측 일봉 순으로 폴백.

데이터 소스: utils.minute_data.get_candles (data/candles.db). 분봉 부족 시 None 반환 →
호출측에서 일봉 기반 폴백.

제공 함수:
  rv_ewma_daily_sigma(market, as_of)    — Tier1: 일별 실현변동성(RV)의 EWMA 일간 σ
  semivariance_ratio(market, as_of)     — Tier2: 상승/하락 실현 반변동성 비율
  oscillation_per_day(market, as_of, …) — Tier3: 박스 내 그리드 라인 일 평균 진동수(거래량 보정)
  analyze_intraday_path(market, as_of, …) — 통합: 1회 로드로 σ·반변동성·진동 동시 산출
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("intraday_vol")

_TRADING_H = 24  # 암호화폐 24h


def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _min_candles_per_day(unit: int) -> int:
    """단위별 하루 최소 캔들 수 게이트. config 매핑 우선, 없으면 1440/unit의 60%."""
    table = _cfg("INTRADAY_MIN_CANDLES_PER_DAY", {})
    if unit in table:
        return table[unit]
    return max(20, int((1440 / unit) * 0.6))


def _fetch(market: str, as_of: str, lookback_days: int, unit: int) -> Tuple[List[Dict], int]:
    """
    as_of(YYYY-MM-DD) 이전 lookback_days일치 분봉. 요청 unit이 비면 폴백 단위로 재시도.
    반환: (candles, used_unit). 모두 실패하면 ([], unit).
    """
    fallback = int(_cfg("INTRADAY_FALLBACK_UNIT", 5))
    units = [unit] if unit == fallback else [unit, fallback]
    try:
        from utils import minute_data
    except Exception as e:
        logger.debug("[intraday_vol] minute_data 임포트 실패: %s", e)
        return [], unit
    start_dt = datetime.strptime(as_of[:10], "%Y-%m-%d") - timedelta(days=lookback_days + 5)
    start = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    end = as_of[:10] + "T23:59:59"
    for u in units:
        try:
            rows = minute_data.get_candles(market, unit=u, start=start, end=end)
        except Exception as e:
            logger.debug("[intraday_vol] %dm 조회 실패 (%s): %s", u, market, e)
            rows = []
        if rows:
            if u != unit:
                logger.info("[intraday_vol] %s %dm 분봉 부족 → %dm 폴백", market, unit, u)
            return rows, u
    return [], unit


def _daily_rv(candles: List[Dict], unit: int) -> List[Tuple[str, float]]:
    """분봉 → 일별 실현변동성 RV = sqrt(Σ r^2). (날짜오름차순) 리스트."""
    if len(candles) < 2:
        return []
    closes = np.array([c["close"] for c in candles], dtype=float)
    days = [c["ts"][:10] for c in candles]
    rets = np.diff(np.log(np.where(closes > 0, closes, np.nan)))
    acc: Dict[str, float] = {}
    cnt: Dict[str, int] = {}
    for d, r in zip(days[1:], rets):
        if not math.isfinite(r):
            continue
        acc[d] = acc.get(d, 0.0) + r * r
        cnt[d] = cnt.get(d, 0) + 1
    gate = _min_candles_per_day(unit)
    # 캔들 수가 너무 적은 날(데이터 결손)은 제외
    return [(d, math.sqrt(acc[d])) for d in sorted(acc) if cnt[d] >= gate]


def _ewma_last(vals: List[float], span: int) -> Optional[float]:
    if not vals:
        return None
    a = 2.0 / (span + 1)
    m = vals[0]
    for v in vals[1:]:
        m = a * v + (1 - a) * m
    return m


# ──────────────────────────────────────────────────────────────
# Tier 1 — RV-EWMA 일간 σ
# ──────────────────────────────────────────────────────────────
def rv_ewma_daily_sigma(market: str, as_of: str,
                        span: int = 20, window_days: int = 30,
                        lookback_days: int = 45, min_days: int = 20,
                        unit: Optional[int] = None,
                        _candles: Optional[List[Dict]] = None) -> Optional[float]:
    """
    as_of 직전 일별 RV 계열에 EWMA(span)를 적용한 일간 σ. 분봉 부족 시 None.
    unit: σ 산출 해상도(기본 INTRADAY_SIGMA_UNIT). _candles 주입 시 그 단위로 간주.
    """
    u = int(unit if unit is not None else _cfg("INTRADAY_SIGMA_UNIT", 5))
    if _candles is not None:
        candles = _candles
    else:
        candles, u = _fetch(market, as_of, lookback_days, u)
    rv = _daily_rv(candles, u)
    rv = [(d, v) for d, v in rv if d <= as_of[:10]]
    if len(rv) < min_days:
        return None
    vals = [v for _, v in rv[-window_days:]]
    return _ewma_last(vals, span)


# ──────────────────────────────────────────────────────────────
# Tier 2 — 상승/하락 실현 반변동성 비율
# ──────────────────────────────────────────────────────────────
def semivariance_ratio(market: str, as_of: str, lookback_days: int = 45,
                       min_obs: int = 2000, unit: Optional[int] = None,
                       _candles: Optional[List[Dict]] = None) -> Optional[float]:
    """
    상승 반변동성 / 하락 반변동성 = sqrt(Σ r+^2) / sqrt(Σ r-^2).
    >1: 상방 변동 우세, <1: 하방 변동 우세. 분봉 부족 시 None.
    """
    u = int(unit if unit is not None else _cfg("INTRADAY_SIGMA_UNIT", 5))
    if _candles is not None:
        candles = _candles
    else:
        candles, u = _fetch(market, as_of, lookback_days, u)
    if len(candles) < min_obs:
        return None
    closes = np.array([c["close"] for c in candles], dtype=float)
    days = [c["ts"][:10] for c in candles]
    keep = [i for i in range(len(days)) if days[i] <= as_of[:10]]
    if len(keep) < min_obs:
        return None
    closes = closes[keep]
    r = np.diff(np.log(closes[closes > 0]))
    up = r[r > 0]; dn = r[r < 0]
    if up.size < 50 or dn.size < 50:
        return None
    rv_up = math.sqrt(float(np.sum(up * up)))
    rv_dn = math.sqrt(float(np.sum(dn * dn)))
    if rv_dn <= 0:
        return None
    return rv_up / rv_dn


# ──────────────────────────────────────────────────────────────
# Tier 3 — 박스 내 일 평균 그리드 교차수 (거래량 보정)
# ──────────────────────────────────────────────────────────────
def oscillation_per_day(market: str, as_of: str, gi_pct: float,
                        box_lower: float, box_upper: float,
                        lookback_days: int = 30, min_days: int = 15,
                        unit: Optional[int] = None,
                        _candles: Optional[List[Dict]] = None) -> Optional[float]:
    """
    as_of 직전 lookback_days간 분봉 경로가 [box_lower, box_upper] 안에서
    기하 간격 gi_pct% 그리드 라인을 통과한 '진동분(total−net)' 일 평균.
    실제 왕복 가능 횟수의 직접 측정치 → 거래량 추정 보정용. 분봉 부족 시 None.
    unit: 진동 산출 해상도(기본 INTRADAY_VOLUME_UNIT — 1m 권장).
    """
    if gi_pct <= 0 or box_lower <= 0 or box_upper <= box_lower:
        return None
    u = int(unit if unit is not None else _cfg("INTRADAY_VOLUME_UNIT", 1))
    if _candles is not None:
        candles = _candles
    else:
        candles, u = _fetch(market, as_of, lookback_days, u)
    # 하루 최소 캔들의 lookback_days/2 정도는 있어야 의미
    if len(candles) < _min_candles_per_day(u) * max(5, min_days // 3):
        return None
    step = math.log(1.0 + gi_pct / 100.0)
    gate = max(int(_min_candles_per_day(u) * 0.2), 50)
    by_day: Dict[str, List[float]] = {}
    for c in candles:
        d = c["ts"][:10]
        if d > as_of[:10]:
            continue
        by_day.setdefault(d, []).append(c["close"])
    osc_days: List[float] = []
    for d in sorted(by_day)[-lookback_days:]:
        p = np.clip(np.asarray(by_day[d], float), box_lower, box_upper)
        if len(p) < gate:
            continue
        idx = np.floor(np.log(p / box_lower) / step)
        total = float(np.sum(np.abs(np.diff(idx))))
        net = float(abs(idx[-1] - idx[0]))
        osc_days.append(max(0.0, total - net))
    if len(osc_days) < min_days:
        return None
    return float(np.mean(osc_days))


# ──────────────────────────────────────────────────────────────
# 통합 — 단일 로드로 σ·반변동성·진동 동시 산출
# ──────────────────────────────────────────────────────────────
def analyze_intraday_path(
    market: str, as_of: str,
    gi_pct: Optional[float] = None,
    box_lower: Optional[float] = None,
    box_upper: Optional[float] = None,
    sigma_unit: Optional[int] = None,
    volume_unit: Optional[int] = None,
    span: int = 20, window_days: int = 30,
    lookback_days: int = 45,
) -> Dict[str, Optional[float]]:
    """
    박스 σ와 그리드 거래량 진동을 같은 as_of 경로에서 한 번에 산출.
    σ는 sigma_unit(기본 5m), 진동은 volume_unit(기본 1m)으로 분리 계산하되,
    두 단위가 같으면 분봉을 1회만 로드해 재사용한다(단일 패스).

    반환 dict:
      daily_sigma        : Tier1 RV-EWMA 일간 σ (None=분봉부족)
      semivariance_ratio : Tier2 상승/하락 반변동성 비율 (None 가능)
      oscillation_per_day: Tier3 박스 내 일진동 (gi/box 미지정 시 None)
      sigma_unit / volume_unit : 실제 사용된 해상도(폴백 반영)
    """
    su = int(sigma_unit if sigma_unit is not None else _cfg("INTRADAY_SIGMA_UNIT", 5))
    vu = int(volume_unit if volume_unit is not None else _cfg("INTRADAY_VOLUME_UNIT", 1))
    want_osc = gi_pct is not None and box_lower is not None and box_upper is not None
    osc_lb = max(lookback_days, 30)

    out: Dict[str, Optional[float]] = {
        "daily_sigma": None, "semivariance_ratio": None,
        "oscillation_per_day": None, "sigma_unit": su, "volume_unit": vu,
    }

    # σ·반변동성 경로 (sigma_unit)
    sig_lb = max(lookback_days, 45)
    sig_candles, su_used = _fetch(market, as_of, sig_lb, su)
    out["sigma_unit"] = su_used
    out["daily_sigma"] = rv_ewma_daily_sigma(
        market, as_of, span=span, window_days=window_days,
        min_days=20, _candles=sig_candles)
    out["semivariance_ratio"] = semivariance_ratio(
        market, as_of, _candles=sig_candles)

    # 진동 경로 (volume_unit) — 단위 동일하면 σ 경로 캔들 재사용
    if want_osc:
        if vu == su_used and sig_candles:
            vol_candles, vu_used = sig_candles, su_used
        else:
            vol_candles, vu_used = _fetch(market, as_of, osc_lb, vu)
        out["volume_unit"] = vu_used
        out["oscillation_per_day"] = oscillation_per_day(
            market, as_of, gi_pct, box_lower, box_upper,
            lookback_days=osc_lb, _candles=vol_candles)
    return out
