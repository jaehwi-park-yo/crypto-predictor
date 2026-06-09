"""
intraday.py - 일봉 OHLC → 분(分) 단위 가격 경로 합성

외부 분봉 API가 차단된 환경에서, 일봉의 (시가/고가/저가/종가)를 만족하는
고해상도 일중 경로를 브라운 브리지(Brownian Bridge)로 생성한다.
이 경로로 그리드 라인의 '실제 통과 횟수'를 세어 일봉 휴리스틱이 놓치는
미세 진동(분봉 이하 왕복 체결)을 반영한다.

핵심 함수:
  - generate_intraday_path(o,h,l,c, n_steps) -> np.ndarray
       시가→종가를 잇고 고가·저가를 정확히 터치하는 일중 경로
  - count_grid_crossings(path, box_lower, box_upper, gi_pct) -> float
       박스 내에서 발생한 그리드 라인 통과(편도 체결) 총수
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np


def generate_intraday_path(
    open_: float,
    high: float,
    low: float,
    close: float,
    n_steps: int = 288,           # 5분봉 기준 하루 288개
    rng: Optional[np.random.Generator] = None,
    intraday_vol: float = 1.0,    # 일중 변동성 강도 배수
) -> np.ndarray:
    """
    일봉 OHLC를 만족하는 일중 가격 경로.
    브라운 브리지(양끝 고정) + 아핀 스케일로 min=low, max=high 정확히 맞춤.
    """
    if rng is None:
        rng = np.random.default_rng()
    if n_steps < 2:
        return np.array([open_, close], dtype=float)

    # 1) 브라운 브리지: 양끝(0)으로 고정된 랜덤 경로
    incr = rng.normal(0.0, 1.0, n_steps)
    walk = np.cumsum(incr)
    t = np.linspace(0.0, 1.0, n_steps)
    bridge = walk - t * walk[-1]   # 끝점 0으로 고정

    # 2) 시가→종가 선형 추세 + 브리지(일중 변동폭 스케일)
    span = max(high - low, 1e-9)
    trend = open_ + (close - open_) * t
    raw = trend + bridge * span * 0.5 * intraday_vol

    # 3) 아핀 변환으로 실제 고가/저가에 정확히 맞춤
    m, M = float(raw.min()), float(raw.max())
    if M - m < 1e-9:
        return np.full(n_steps, close, dtype=float)
    a = (high - low) / (M - m)
    b = low - a * m
    path = a * raw + b
    return path


def count_grid_crossings(
    path: np.ndarray,
    box_lower: float,
    box_upper: float,
    gi_pct: float,
) -> tuple[float, float]:
    """
    박스 내 그리드 라인(기하 간격 gi_pct%) 통과를 분해.

    반환: (total_crossings, net_crossings)
      - total : 전체 라인 통과 횟수 (Σ|Δidx|)
      - net   : 순(추세) 이동 = |idx_last - idx_first|

    진동분(oscillation) = total - net 이 실제 그리드 왕복 수익의 원천이다.
    (순수 추세 구간은 total=net → 진동분 0 → 그리드 수익 0, 인벤토리만 누적)
    """
    if gi_pct <= 0 or box_lower <= 0 or len(path) < 2:
        return 0.0, 0.0
    clamped = np.clip(path, box_lower, box_upper)
    step = math.log(1.0 + gi_pct / 100.0)
    idx = np.floor(np.log(clamped / box_lower) / step)
    total = float(np.sum(np.abs(np.diff(idx))))
    net = float(abs(idx[-1] - idx[0]))
    return total, net
