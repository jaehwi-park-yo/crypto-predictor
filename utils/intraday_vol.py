"""
utils/intraday_vol.py — 5분봉 기반 일중 변동성·진동 통계 (as-of 재현 가능)
==========================================================================
박스권 예측과 그리드 거래량 추정에 5분봉을 활용하는 공용 유틸.
모든 함수는 as_of 시점 이전 분봉만 사용(미래정보 누설 없음).
데이터 소스: utils.minute_data.get_candles (data/candles.db). 분봉 부족 시 None 반환 →
호출측에서 일봉 기반 폴백.

거래량 과소측정 보정:
  단일인벤토리 5m 시뮬은 캔들 내부 왕복을 흡수해 과소측정.
  해상도 스케일링 실측 osc ∝ Δt^-0.60 → 1m은 5m 대비 약 2.7배 진동.
  oscillation_per_day()가 config.VOLUME_RESOLUTION_COEF(기본 2.7)를 곱해 반환.
  index.html의 BTC_COEF/USDT_EFF에도 동일 계수가 반영됨.

제공 함수:
  rv_ewma_daily_sigma(market, as_of)    — Tier1: 일별 실현변동성(RV)의 EWMA 일간 σ
  semivariance_ratio(market, as_of)     — Tier2: 상승/하락 실현 반변동성 비율
  oscillation_per_day(market, as_of, …) — Tier3: 박스 내 그리드 라인 일 평균 진동수 × 보정계수
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("intraday_vol")


def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _fetch_5m(market: str, as_of: str, lookback_days: int) -> List[Dict]:
    """as_of(YYYY-MM-DD) 이전 lookback_days일치 5분봉. 실패 시 []."""
    try:
        from utils import minute_data
        start_dt = datetime.strptime(as_of[:10], "%Y-%m-%d") - timedelta(days=lookback_days + 5)
        start = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        end = as_of[:10] + "T23:59:59"
        return minute_data.get_candles(market, unit=5, start=start, end=end)
    except Exception as e:
        logger.debug("[intraday_vol] 5분봉 조회 실패 (%s): %s", market, e)
        return []


def _daily_rv(candles: List[Dict]) -> List[Tuple[str, float]]:
    """5분봉 → 일별 실현변동성 RV = sqrt(Σ r_5m^2). (날짜오름차순) 리스트."""
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
    # 최소 캔들 수가 너무 적은 날(데이터 결손)은 제외
    return [(d, math.sqrt(acc[d])) for d in sorted(acc) if cnt[d] >= 100]


def _ewma_last(vals: List[float], span: int) -> Optional[float]:
    if not vals:
        return None
    a = 2.0 / (span + 1)
    m = vals[0]
    for v in vals[1:]:
        m = a * v + (1 - a) * m
    return m


# ──────────────────────────────────────────────────────────────
# Tier 1 — 5m RV-EWMA 일간 σ
# ──────────────────────────────────────────────────────────────
def rv_ewma_daily_sigma(market: str, as_of: str,
                        span: int = 20, window_days: int = 30,
                        lookback_days: int = 45, min_days: int = 20,
                        _candles: Optional[List[Dict]] = None) -> Optional[float]:
    """
    as_of 직전 일별 RV 계열에 EWMA(span)를 적용한 일간 σ. 분봉 부족 시 None.
    window_days: EWMA에 사용할 최근 일수(가장 최근 window_days개 RV).
    """
    candles = _candles if _candles is not None else _fetch_5m(market, as_of, lookback_days)
    rv = _daily_rv(candles)
    rv = [(d, v) for d, v in rv if d <= as_of[:10]]
    if len(rv) < min_days:
        return None
    vals = [v for _, v in rv[-window_days:]]
    return _ewma_last(vals, span)


# ──────────────────────────────────────────────────────────────
# Tier 2 — 상승/하락 실현 반변동성 비율
# ──────────────────────────────────────────────────────────────
def semivariance_ratio(market: str, as_of: str, lookback_days: int = 45,
                       min_obs: int = 2000,
                       _candles: Optional[List[Dict]] = None) -> Optional[float]:
    """
    상승 반변동성 / 하락 반변동성 = sqrt(Σ r+^2) / sqrt(Σ r-^2).
    >1: 상방 변동 우세, <1: 하방 변동 우세. 분봉 부족 시 None.
    """
    candles = _candles if _candles is not None else _fetch_5m(market, as_of, lookback_days)
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
# Tier 3 — 박스 내 일 평균 그리드 교차수 × 해상도 보정계수
# ──────────────────────────────────────────────────────────────
def oscillation_per_day(market: str, as_of: str, gi_pct: float,
                        box_lower: float, box_upper: float,
                        lookback_days: int = 30, min_days: int = 15,
                        _candles: Optional[List[Dict]] = None) -> Optional[float]:
    """
    as_of 직전 lookback_days간 5분봉 경로가 [box_lower, box_upper] 안에서
    기하 간격 gi_pct% 그리드 라인을 통과한 '진동분(total−net)' 일 평균.
    반환값에 VOLUME_RESOLUTION_COEF(기본 2.7)를 곱해 1m 수준 진동으로 보정.
    분봉 부족 시 None.
    """
    if gi_pct <= 0 or box_lower <= 0 or box_upper <= box_lower:
        return None
    candles = _candles if _candles is not None else _fetch_5m(market, as_of, lookback_days)
    if len(candles) < 500:
        return None
    step = math.log(1.0 + gi_pct / 100.0)
    by_day: Dict[str, List[float]] = {}
    for c in candles:
        d = c["ts"][:10]
        if d > as_of[:10]:
            continue
        by_day.setdefault(d, []).append(c["close"])
    osc_days: List[float] = []
    for d in sorted(by_day)[-lookback_days:]:
        p = np.clip(np.asarray(by_day[d], float), box_lower, box_upper)
        if len(p) < 100:
            continue
        idx = np.floor(np.log(p / box_lower) / step)
        total = float(np.sum(np.abs(np.diff(idx))))
        net = float(abs(idx[-1] - idx[0]))
        osc_days.append(max(0.0, total - net))
    if len(osc_days) < min_days:
        return None
    raw = float(np.mean(osc_days))
    coef = float(_cfg("VOLUME_RESOLUTION_COEF", 2.7))
    return raw * coef
