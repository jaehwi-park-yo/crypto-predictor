"""
services/prediction_monitor.py - 예측→측정 전환 엔진
=====================================================
월중에 실제 데이터가 쌓이면서 예측값이 점진적으로 측정값으로 전환되고,
남은 기간에 대해서는 업데이트된 σ로 수정 예측을 산출한다.

핵심 메커니즘:
  ┌─────────────────────────────────────────────────────┐
  │  월 1일         today          월 말일              │
  │  |──[측정 완료]──|────[수정예측]────|                │
  │     실제 캔들       갱신된 σ로                       │
  │     (박스 내외 판정)  재투영된 밴드                   │
  └─────────────────────────────────────────────────────┘

  원본 예측 σ: lookback(월 시작 전 31일)
  수정 예측 σ: lookback + 실제 경과일 (데이터 증가 → σ 추정 정밀도 향상)

  박스 이탈이 발생하면:
    - 상방: 관망 유지 (그리드 전량 매도 완료)
    - 하방: 30% 손절 + 박스 하향 재설정 권고

공개 함수:
  monitor(snapshot, full_history, today_str, live_price) -> MonitorStatus
  get_month_actuals(full_history, year, month) -> List[Dict]   # 히스토리 슬라이스
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

from config import HORIZON_DAYS, PREDICTION_DRIFT, KRW_HOLD_RATIO, GRID_AGGRESSIVENESS
from services.prediction_service import predict_as_of, PredictionSnapshot
from utils.statistics import (
    compute_log_returns, daily_volatility, sigma_band, containment_probability,
)

logger = logging.getLogger("prediction_monitor")


# ──────────────────────────────────────────────────────────────
# 상태 데이터 모델
# ──────────────────────────────────────────────────────────────
@dataclass
class DayStatus:
    """측정 완료된 하루의 상태."""
    date: str
    close: float
    high: float
    low: float
    in_box_1s: bool           # 1σ 박스 내
    in_box_2s: bool           # 2σ 박스 내 (권장 박스)
    in_recommended: bool      # 권장 박스 내
    zone: str                 # "inner" / "warning" / "breach_upper" / "breach_lower"
    deviation_pct: float      # 박스 이탈폭 (%)


@dataclass
class MonitorStatus:
    """예측→측정 전환 월중 상태 스냅샷."""

    target_month: str          # "YYYY-MM"
    today: str                 # "YYYY-MM-DD" (기준일)
    total_days: int            # 해당 월 총 일수
    elapsed_days: int          # 측정 완료 일수
    remaining_days: int        # 남은 일수
    progress_pct: float        # elapsed / total * 100

    # ── 원본 예측 (변경 없음) ──────────────────────────────
    original: PredictionSnapshot
    original_sigma: float

    # ── 측정 구간 (실제 데이터) ────────────────────────────
    measured_candles: List[DayStatus] = field(default_factory=list)
    current_price: float = 0.0
    current_source: str = "fallback"

    # containment 통계
    days_in_recommended: int = 0
    days_warning: int = 0
    days_breach_upper: int = 0
    days_breach_lower: int = 0
    measured_containment_pct: float = 0.0  # 권장 박스 containment
    max_upside_dev_pct: float = 0.0
    max_downside_dev_pct: float = 0.0

    # ── 수정 예측 (경과 데이터로 σ 갱신) ─────────────────
    revised: Optional[PredictionSnapshot] = None
    revised_sigma: float = 0.0
    sigma_change_pct: float = 0.0    # (revised - original) / original * 100
    box_center_shift_pct: float = 0.0  # 박스 중심 이동(%)

    # ── 종합 상태 ──────────────────────────────────────────
    overall_status: str = "PREDICTED"  # PREDICTED / ON_TRACK / WARNING / BREACH
    status_detail: str = ""
    recommendation: str = ""

    # ── 차트용 데이터 ──────────────────────────────────────
    # 예측 구간(remaining days)에 투영할 수정 예측 밴드
    forecast_upper_1s: float = 0.0
    forecast_lower_1s: float = 0.0
    forecast_upper_2s: float = 0.0
    forecast_lower_2s: float = 0.0
    forecast_revised_upper: float = 0.0
    forecast_revised_lower: float = 0.0


# ──────────────────────────────────────────────────────────────
# 핵심 함수
# ──────────────────────────────────────────────────────────────
def get_month_actuals(
    full_history: List[Dict],
    year: int,
    month: int,
    cutoff: Optional[str] = None,   # None = 오늘까지
) -> List[Dict]:
    """
    히스토리에서 특정 월의 실제 일봉을 추출.
    cutoff: 이 날짜 이하까지만 포함 (월중 현재 시점 기준으로 자를 때 사용).
    """
    prefix = f"{year:04d}-{month:02d}-"
    candles = [c for c in full_history if c["date"].startswith(prefix)]
    if cutoff:
        candles = [c for c in candles if c["date"] <= cutoff]
    return sorted(candles, key=lambda x: x["date"])


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return (date(year + 1, 1, 1) - date(year, month, 1)).days
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _classify_day(
    close: float, high: float, low: float,
    rec_upper: float, rec_lower: float,
    upper_1s: float, lower_1s: float,
    upper_2s: float, lower_2s: float,
) -> DayStatus:
    """하루치 캔들을 박스 기준으로 분류."""
    in_rec = rec_lower <= close <= rec_upper
    in_1s  = lower_1s  <= close <= upper_1s
    in_2s  = lower_2s  <= close <= upper_2s

    if close > rec_upper:
        if close > upper_2s:
            zone = "breach_upper"
            dev  = (close - upper_2s) / upper_2s * 100
        else:
            zone = "warning"
            dev  = (close - rec_upper) / rec_upper * 100
    elif close < rec_lower:
        if close < lower_2s:
            zone = "breach_lower"
            dev  = (close - lower_2s) / lower_2s * 100   # 음수
        else:
            zone = "warning"
            dev  = (close - rec_lower) / rec_lower * 100  # 음수
    else:
        zone = "inner"
        dev  = 0.0

    return DayStatus(
        date="", close=close, high=high, low=low,
        in_box_1s=in_1s, in_box_2s=in_2s, in_recommended=in_rec,
        zone=zone, deviation_pct=dev,
    )


def monitor(
    snapshot: PredictionSnapshot,
    full_history: List[Dict],
    today_str: str,
    live_price: Optional[float] = None,
    live_source: str = "fallback",
    lookback: int = 31,
) -> MonitorStatus:
    """
    예측→측정 전환 상태 계산.

    Parameters
    ----------
    snapshot    : predict_as_of()로 생성된 원본 예측
    full_history: 전체 히스토리 (as_of 이전 + 대상 월 실제 포함)
    today_str   : 오늘 날짜 "YYYY-MM-DD"
    live_price  : 실시간 현재가 (None이면 오늘 종가 사용)
    live_source : "upbit" / "bithumb" / "fallback"
    """
    # 대상 월 파싱
    year  = int(snapshot.target_month[:4])
    month = int(snapshot.target_month[5:7])
    total_days = _days_in_month(year, month)
    month_start = f"{year:04d}-{month:02d}-01"
    month_end   = f"{year:04d}-{month:02d}-{total_days:02d}"

    # 측정 완료 일봉 (대상 월 중 today 이하)
    actuals_raw = get_month_actuals(full_history, year, month, cutoff=today_str)
    elapsed = len(actuals_raw)
    remaining = total_days - elapsed

    # 현재가: 실시간 제공 시 우선, 없으면 오늘 종가 또는 원본 기준가
    if live_price and live_price > 0:
        cur_price = live_price
    elif actuals_raw:
        cur_price = actuals_raw[-1]["close"]
    else:
        cur_price = snapshot.reference_price

    # 일별 상태 분류
    measured: List[DayStatus] = []
    for c in actuals_raw:
        ds = _classify_day(
            c["close"], c["high"], c["low"],
            snapshot.recommended_upper, snapshot.recommended_lower,
            snapshot.box_upper_1s, snapshot.box_lower_1s,
            snapshot.box_upper_2s, snapshot.box_lower_2s,
        )
        ds.date = c["date"]
        measured.append(ds)

    # 통계 집계
    n_in = sum(1 for d in measured if d.in_recommended)
    n_warn = sum(1 for d in measured if d.zone == "warning")
    n_bu   = sum(1 for d in measured if d.zone == "breach_upper")
    n_bl   = sum(1 for d in measured if d.zone == "breach_lower")
    containment_pct = n_in / elapsed * 100 if elapsed else 0.0

    up_devs  = [d.deviation_pct for d in measured if d.deviation_pct > 0]
    dn_devs  = [abs(d.deviation_pct) for d in measured if d.deviation_pct < 0]
    max_up   = max(up_devs)  if up_devs  else 0.0
    max_down = max(dn_devs)  if dn_devs  else 0.0

    # ── 수정 예측 (경과 데이터로 σ 갱신) ─────────────────────
    # lookback(원본 기준 전 31일) + 실제 경과일 = 확장 데이터셋으로 재추정
    revised = None
    revised_sigma = snapshot.daily_sigma
    sigma_change = 0.0
    box_shift = 0.0
    forecast_u1 = snapshot.box_upper_1s
    forecast_l1 = snapshot.box_lower_1s
    forecast_u2 = snapshot.box_upper_2s
    forecast_l2 = snapshot.box_lower_2s
    forecast_ru = snapshot.recommended_upper
    forecast_rl = snapshot.recommended_lower

    if elapsed >= 3 and remaining > 0:
        # 원본 lookback + 실제 경과일을 이어붙인 히스토리로 σ 재추정
        extended_as_of = today_str
        try:
            revised = predict_as_of(
                full_history,
                as_of=today_str,
                capital_krw=snapshot.capital_krw,
                aggressiveness=snapshot.aggressiveness,
                krw_hold_ratio=snapshot.krw_hold_ratio,
                use_sigma=snapshot.sigma_level_used,
                horizon_days=remaining,        # 남은 일수로 지평 축소
                lookback=lookback,
            )
            revised_sigma = revised.daily_sigma
            sigma_change  = (revised_sigma - snapshot.daily_sigma) / snapshot.daily_sigma * 100

            orig_mid = (snapshot.recommended_upper + snapshot.recommended_lower) / 2
            rev_mid  = (revised.recommended_upper  + revised.recommended_lower)  / 2
            box_shift = (rev_mid - orig_mid) / orig_mid * 100

            # 수정 예측 밴드: 현재가 기준으로 남은 기간 투영
            forecast_u1 = revised.box_upper_1s
            forecast_l1 = revised.box_lower_1s
            forecast_u2 = revised.box_upper_2s
            forecast_l2 = revised.box_lower_2s
            forecast_ru = revised.recommended_upper
            forecast_rl = revised.recommended_lower
        except Exception as e:
            logger.warning("[Monitor] 수정 예측 실패: %s", e)

    # ── 종합 상태 판정 ─────────────────────────────────────────
    if elapsed == 0:
        overall = "PREDICTED"
        detail  = f"대상 월({snapshot.target_month}) 아직 시작 전"
        rec     = "월 초 봇 배포 후 관망 유지"
    elif n_bu > 0:
        overall = "BREACH"
        detail  = f"상방 2σ 이탈 {n_bu}일 (현재가 박스 위)"
        rec     = "관망 유지 — 그리드 매도 완료 상태. 추격 매수 금지."
    elif n_bl > 0:
        overall = "BREACH"
        detail  = f"하방 2σ 이탈 {n_bl}일 (현재가 박스 아래)"
        rec     = "30% 부분손절 + 박스 하향 재설정. KRW 예비금으로 회복 그리드 배치 검토."
    elif n_warn > 0:
        overall = "WARNING"
        detail  = f"경고 구간(1σ~2σ) {n_warn}일 — 박스 경계 접근 중"
        rec     = "모니터링 강화. 2σ 이탈 시 리스크 프로토콜 즉시 발동."
    else:
        overall = "ON_TRACK"
        pct_done = elapsed / total_days * 100
        detail   = (
            f"{elapsed}/{total_days}일 경과 ({pct_done:.0f}%) — "
            f"containment {containment_pct:.0f}%"
        )
        rec = "정상 운영 유지."

    progress = elapsed / total_days * 100 if total_days else 0.0

    logger.info(
        "[Monitor] %s → 경과 %d일 / σ변화 %+.1f%% / 박스이동 %+.1f%% / 상태 %s",
        snapshot.target_month, elapsed, sigma_change, box_shift, overall,
    )

    return MonitorStatus(
        target_month=snapshot.target_month,
        today=today_str,
        total_days=total_days,
        elapsed_days=elapsed,
        remaining_days=remaining,
        progress_pct=progress,
        original=snapshot,
        original_sigma=snapshot.daily_sigma,
        measured_candles=measured,
        current_price=cur_price,
        current_source=live_source,
        days_in_recommended=n_in,
        days_warning=n_warn,
        days_breach_upper=n_bu,
        days_breach_lower=n_bl,
        measured_containment_pct=containment_pct,
        max_upside_dev_pct=max_up,
        max_downside_dev_pct=max_down,
        revised=revised,
        revised_sigma=revised_sigma,
        sigma_change_pct=sigma_change,
        box_center_shift_pct=box_shift,
        overall_status=overall,
        status_detail=detail,
        recommendation=rec,
        forecast_upper_1s=forecast_u1,
        forecast_lower_1s=forecast_l1,
        forecast_upper_2s=forecast_u2,
        forecast_lower_2s=forecast_l2,
        forecast_revised_upper=forecast_ru,
        forecast_revised_lower=forecast_rl,
    )
