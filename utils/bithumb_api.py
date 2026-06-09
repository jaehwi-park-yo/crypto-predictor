"""
bithumb_api.py - 빗썸 공개 API 래퍼 (시세 데이터, 인증 불필요)
거래(주문)는 별도 비공개 API 키가 필요하며 이 모듈 범위 밖이다.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from config import BITHUMB_BASE_URL, MAX_RETRIES, BACKOFF_FACTOR

logger = logging.getLogger("bithumb_api")


def _request(url: str, params: Optional[dict] = None) -> Optional[dict]:
    """지수 백오프 재시도가 적용된 GET 요청."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - 네트워크/파싱 모든 예외 흡수
            wait = BACKOFF_FACTOR ** attempt
            logger.warning(
                "요청 실패 (%d/%d) %s — %.0fs 후 재시도", attempt + 1, MAX_RETRIES, exc, wait
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)
    logger.error("요청 최종 실패: %s", url)
    return None


def get_candlestick(symbol: str = "BTC_KRW", interval: str = "24h", count: int = 90) -> List[Dict[str, Any]]:
    """
    일봉(기본) 캔들 조회.
    빗썸 응답 배열 포맷: [기준시각, 시가, 종가, 고가, 저가, 거래량]
    """
    url = f"{BITHUMB_BASE_URL}/public/candlestick/{symbol}/{interval}"
    data = _request(url)
    if not data or data.get("status") != "0000":
        return []
    rows = data.get("data", [])
    out: List[Dict[str, Any]] = []
    for r in rows[-count:]:
        try:
            out.append({
                "date": int(r[0]),
                "open": float(r[1]),
                "close": float(r[2]),
                "high": float(r[3]),
                "low": float(r[4]),
                "volume": float(r[5]),
            })
        except (ValueError, IndexError, TypeError):
            continue
    return out


def get_ticker(symbol: str = "BTC_KRW") -> Dict[str, Any]:
    """현재가/체결 요약 조회."""
    url = f"{BITHUMB_BASE_URL}/public/ticker/{symbol}"
    data = _request(url)
    if not data or data.get("status") != "0000":
        return {}
    return data.get("data", {})


def get_orderbook(symbol: str = "BTC_KRW") -> Dict[str, Any]:
    """호가창 조회 (슬리피지 검증용)."""
    url = f"{BITHUMB_BASE_URL}/public/orderbook/{symbol}"
    data = _request(url)
    if not data or data.get("status") != "0000":
        return {}
    return data.get("data", {})
