"""
utils/live_data.py - 실시간 BTC/KRW 데이터 수집기
===================================================
수집 우선순위 (일봉/현재가):
  1. 업비트 공개 API (KRW-BTC) — 최우선, 인증 불필요
  2. 빗썸 공개 API (BTC_KRW)
  3. 마지막 히스토리 종가 (오프라인 폴백)

분봉 (실운영 연동 시):
  - 업비트 /v1/candles/minutes/{unit}

반환:
  - LiveQuote: 현재가 + 전일 대비 + 소스 정보
  - fetch_recent_daily(days)  → 최근 N일 일봉 (historical_data와 동일 포맷)
  - fetch_month_candles(year, month) → 특정 월 일봉
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional

import requests

logger = logging.getLogger("live_data")

_TIMEOUT = 8
_UPBIT_BASE  = "https://api.upbit.com/v1"
_BITHUMB_BASE = "https://api.bithumb.com/public"


@dataclass
class LiveQuote:
    price: float
    prev_close: float
    change_pct: float          # (price - prev_close) / prev_close * 100
    timestamp: str             # ISO-8601
    source: str                # "upbit" | "bithumb" | "fallback"
    is_live: bool              # False = 폴백(합성/캐시)


# ──────────────────────────────────────────────────────────────
# 현재가 수집
# ──────────────────────────────────────────────────────────────
def fetch_current_price(fallback_price: Optional[float] = None) -> LiveQuote:
    """
    BTC/KRW 현재가 수집. 업비트 → 빗썸 → 폴백 순서.
    fallback_price: 모든 API 실패 시 사용할 가격 (히스토리 마지막 종가 권장).
    """
    # 1) 업비트 ticker
    try:
        r = requests.get(
            f"{_UPBIT_BASE}/ticker",
            params={"markets": "KRW-BTC"},
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()[0]
        price      = float(data["trade_price"])
        prev_close = float(data["prev_closing_price"])
        return LiveQuote(
            price=price, prev_close=prev_close,
            change_pct=(price - prev_close) / prev_close * 100 if prev_close else 0.0,
            timestamp=datetime.now().isoformat(),
            source="upbit", is_live=True,
        )
    except Exception as e:
        logger.warning("[LiveData] 업비트 ticker 실패: %s", e)

    # 2) 빗썸 ticker
    try:
        r = requests.get(
            f"{_BITHUMB_BASE}/ticker/BTC_KRW",
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json().get("data", {})
        price = float(d.get("closing_price", 0))
        prev  = float(d.get("prev_closing_price", price))
        if price > 0:
            return LiveQuote(
                price=price, prev_close=prev,
                change_pct=(price - prev) / prev * 100 if prev else 0.0,
                timestamp=datetime.now().isoformat(),
                source="bithumb", is_live=True,
            )
    except Exception as e:
        logger.warning("[LiveData] 빗썸 ticker 실패: %s", e)

    # 3) 폴백
    p = fallback_price or 0.0
    logger.warning("[LiveData] 모든 실시간 API 실패 → 폴백 가격 사용 (%s)", f"{p:,.0f}")
    return LiveQuote(
        price=p, prev_close=p, change_pct=0.0,
        timestamp=datetime.now().isoformat(),
        source="fallback", is_live=False,
    )


# ──────────────────────────────────────────────────────────────
# 최근 N일 일봉
# ──────────────────────────────────────────────────────────────
def fetch_recent_daily(days: int = 90) -> List[Dict]:
    """
    최근 N일 일봉. 업비트 공개 API 우선.
    반환 포맷: [{"date","open","high","low","close","volume"}, ...] oldest→newest
    """
    # 업비트 일봉 (최대 200개/요청)
    count = min(days, 200)
    try:
        r = requests.get(
            f"{_UPBIT_BASE}/candles/days",
            params={"market": "KRW-BTC", "count": count},
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        batch = r.json()
        if len(batch) >= 10:
            batch.reverse()  # 최신→과거 → 과거→최신
            return [
                {
                    "date":  c["candle_date_time_kst"][:10],
                    "open":  float(c["opening_price"]),
                    "high":  float(c["high_price"]),
                    "low":   float(c["low_price"]),
                    "close": float(c["trade_price"]),
                    "volume": float(c["candle_acc_trade_volume"]),
                }
                for c in batch
            ]
    except Exception as e:
        logger.warning("[LiveData] 업비트 일봉 실패: %s", e)

    return []


def fetch_month_candles(year: int, month: int) -> List[Dict]:
    """
    특정 월의 일봉 데이터 수집.
    업비트 API → 실패 시 빈 리스트 반환 (caller가 히스토리 슬라이스로 폴백).
    """
    # 대상 월의 마지막 날 계산
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    last_day = (date(next_year, next_month, 1) - timedelta(days=1)).day
    month_start = f"{year:04d}-{month:02d}-01"
    month_end   = f"{year:04d}-{month:02d}-{last_day:02d}"

    try:
        # 업비트: to 파라미터로 월말부터 최대 31개
        r = requests.get(
            f"{_UPBIT_BASE}/candles/days",
            params={
                "market": "KRW-BTC",
                "count": last_day,
                "to": f"{month_end}T23:59:59",
            },
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        batch = r.json()
        if batch:
            batch.reverse()
            result = [
                {
                    "date":  c["candle_date_time_kst"][:10],
                    "open":  float(c["opening_price"]),
                    "high":  float(c["high_price"]),
                    "low":   float(c["low_price"]),
                    "close": float(c["trade_price"]),
                    "volume": float(c["candle_acc_trade_volume"]),
                }
                for c in batch
                if month_start <= c["candle_date_time_kst"][:10] <= month_end
            ]
            logger.info("[LiveData] 업비트 %d-%02d 일봉 %d개 확보", year, month, len(result))
            return result
    except Exception as e:
        logger.warning("[LiveData] 업비트 월봉 실패: %s", e)

    return []


# ──────────────────────────────────────────────────────────────
# 업비트 분봉 (실운영 연동용)
# ──────────────────────────────────────────────────────────────
def fetch_minute_candles(unit: int = 1, count: int = 200) -> List[Dict]:
    """
    업비트 분봉 (unit: 1/3/5/10/15/30/60).
    실운영 시 분봉 기반 그리드 체결 시뮬레이션에 사용.
    반환: [{"datetime","open","high","low","close","volume"}, ...] oldest→newest
    """
    try:
        r = requests.get(
            f"{_UPBIT_BASE}/candles/minutes/{unit}",
            params={"market": "KRW-BTC", "count": min(count, 200)},
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        batch = r.json()
        batch.reverse()
        return [
            {
                "datetime": c["candle_date_time_kst"],
                "open":  float(c["opening_price"]),
                "high":  float(c["high_price"]),
                "low":   float(c["low_price"]),
                "close": float(c["trade_price"]),
                "volume": float(c["candle_acc_trade_volume"]),
            }
            for c in batch
        ]
    except Exception as e:
        logger.warning("[LiveData] 업비트 %d분봉 실패: %s", unit, e)
        return []
