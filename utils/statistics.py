"""
statistics.py - 통계 헬퍼 모듈
박스권 예측의 수학적 핵심. 로그수익률 기반으로 월간 지평에 투영한
1σ/2σ 밴드를 계산한다. (외부 TA 라이브러리 없이 numpy + math 만 사용)
"""
from __future__ import annotations

import math
from typing import Sequence, Tuple

import numpy as np


# ----------------------------------------------------------------------
# 수익률 · 변동성
# ----------------------------------------------------------------------
def compute_log_returns(prices: Sequence[float]) -> np.ndarray:
    """로그수익률 r_t = ln(P_t / P_{t-1})."""
    arr = np.asarray(prices, dtype=float)
    arr = arr[arr > 0]
    if arr.size < 2:
        return np.array([])
    return np.diff(np.log(arr))


def daily_volatility(log_returns: np.ndarray) -> float:
    """일간 변동성 = 로그수익률 표준편차(표본)."""
    if log_returns.size < 2:
        return 0.0
    return float(np.std(log_returns, ddof=1))


def annualize_volatility(daily_sigma: float, periods: int = 365) -> float:
    """일간 변동성 → 연율화 (코인은 365일 거래)."""
    return daily_sigma * math.sqrt(periods)


def project_sigma(daily_sigma: float, horizon_days: int) -> float:
    """√t 스케일링: σ_h = σ_일 × √h."""
    return daily_sigma * math.sqrt(max(horizon_days, 0))


def sigma_band(
    reference_price: float,
    daily_sigma: float,
    horizon_days: int,
    sigma_mult: float,
    drift: float = 0.0,
) -> Tuple[float, float]:
    """
    로그정규 가정 하에서 horizon_days 후 가격의 ±(sigma_mult·σ) 밴드.

        upper = P0 · exp(μ_h + k·σ_h)
        lower = P0 · exp(μ_h − k·σ_h)

    박스권 매매는 평균회귀를 가정하므로 drift=0 사용을 권장한다.
    """
    sig_h = project_sigma(daily_sigma, horizon_days)
    mu_h = drift * horizon_days
    upper = reference_price * math.exp(mu_h + sigma_mult * sig_h)
    lower = reference_price * math.exp(mu_h - sigma_mult * sig_h)
    return upper, lower


# ----------------------------------------------------------------------
# 확률 (정규 근사)
# ----------------------------------------------------------------------
def normal_cdf(x: float) -> float:
    """표준정규 누적분포 Φ(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def containment_probability(sigma_mult: float) -> float:
    """
    종가가 [−kσ, +kσ] 구간 안에 머무를 확률.
    1σ → 0.6827, 2σ → 0.9545.
    """
    return normal_cdf(sigma_mult) - normal_cdf(-sigma_mult)


def boundary_touch_probability(sigma_mult: float) -> float:
    """
    월 중 '한 번이라도' ±kσ 경계를 터치할 근사 확률.
    브라운운동 first-passage 반사원리 1차 근사(양측):
        P_touch ≈ 4 · (1 − Φ(k))
    그리드 매매에선 이 값이 높을수록(경계 체결 잦음) 유리하다.
    """
    one_side = 1.0 - normal_cdf(sigma_mult)
    return min(1.0, 4.0 * one_side)


def fit_normal(returns: Sequence[float]) -> Tuple[float, float]:
    """수익률 분포에 정규분포 적합 → (평균, 표준편차)."""
    arr = np.asarray(returns, dtype=float)
    if arr.size == 0:
        return 0.0, 0.0
    std = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
    return float(np.mean(arr)), std


# ----------------------------------------------------------------------
# 기술적 지표
# ----------------------------------------------------------------------
def compute_ma(prices: Sequence[float], window: int) -> float:
    """단순이동평균(최근 window개). 데이터 부족 시 가용 전체 평균."""
    arr = np.asarray(prices, dtype=float)
    if arr.size == 0 or window <= 0:
        return float(arr[-1]) if arr.size else 0.0
    w = min(window, arr.size)
    return float(np.mean(arr[-w:]))


def compute_bollinger_bands(
    prices: Sequence[float], window: int = 20, num_std: float = 2.0
) -> Tuple[float, float, float]:
    """볼린저 밴드 → (상단, 중심, 하단)."""
    arr = np.asarray(prices, dtype=float)
    if arr.size == 0:
        return 0.0, 0.0, 0.0
    w = min(window, arr.size)
    seg = arr[-w:]
    mid = float(np.mean(seg))
    sd = float(np.std(seg, ddof=1)) if w > 1 else 0.0
    return mid + num_std * sd, mid, mid - num_std * sd


def compute_rsi(prices: Sequence[float], period: int = 14) -> float:
    """RSI(기본 14). 데이터 부족 시 중립 50 반환."""
    arr = np.asarray(prices, dtype=float)
    if arr.size < period + 1:
        return 50.0
    deltas = np.diff(arr)
    seed = deltas[-period:]
    gains = seed[seed > 0].sum() / period
    losses = -seed[seed < 0].sum() / period
    if losses == 0:
        return 100.0
    rs = gains / losses
    return float(100.0 - (100.0 / (1.0 + rs)))


def compute_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float:
    """ATR(평균진폭, 기본 14). 그리드 간격 산정의 보조 지표."""
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = min(h.size, l.size, c.size)
    if n < 2:
        return 0.0
    h, l, c = h[-n:], l[-n:], c[-n:]
    prev_close = c[:-1]
    tr = np.maximum.reduce([
        h[1:] - l[1:],
        np.abs(h[1:] - prev_close),
        np.abs(l[1:] - prev_close),
    ])
    p = min(period, tr.size)
    return float(np.mean(tr[-p:]))
