"""
historical_data.py - 장기 히스토리컬 BTC/KRW 일봉 수집 유틸

수집 우선순위:
  1. 업비트 공개 API (KRW-BTC, 200개/요청, 페이지네이션으로 최대치)
  2. CoinGecko v3 (BTC/KRW, days=max)
  3. Binance BTCUSDT → 환율 환산 (days=max)
  4. 모든 외부 소스 실패 → 리얼리스틱 합성 (2021-01 ~ 현재)

반환 포맷(공통):
  [{"date": "YYYY-MM-DD", "open": float, "high": float, "low": float,
    "close": float, "volume": float}, ...]  oldest → newest
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import List, Dict, Any

import numpy as np
import requests

logger = logging.getLogger("historical_data")

# 연결 타임아웃
_TIMEOUT = 12
# 요청 간 Rate-limit 대기
_SLEEP = 0.3


# ─────────────────────────────────────────────────────────
# 소스 1: 업비트 (KRW-BTC, 200개/요청 → 페이지네이션)
# ─────────────────────────────────────────────────────────
def _fetch_upbit(max_candles: int = 3000) -> List[Dict]:
    base = "https://api.upbit.com/v1/candles/days"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    all_candles: List[Dict] = []
    to_param = None

    while len(all_candles) < max_candles:
        params: Dict[str, Any] = {"market": "KRW-BTC", "count": 200}
        if to_param:
            params["to"] = to_param
        try:
            r = requests.get(base, params=params, headers=headers, timeout=_TIMEOUT)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            logger.warning("업비트 요청 실패: %s", e)
            break
        if not batch:
            break
        all_candles.extend(batch)
        # 업비트 응답: 최신→과거 정렬, 마지막 항목이 가장 오래된 날짜
        oldest = batch[-1]["candle_date_time_kst"][:10]
        to_param = oldest + "T00:00:00"
        time.sleep(_SLEEP)
        if len(batch) < 200:
            break

    # 최신→과거 → 과거→최신으로 뒤집기
    all_candles.reverse()
    return [
        {
            "date": c["candle_date_time_kst"][:10],
            "open": float(c["opening_price"]),
            "high": float(c["high_price"]),
            "low": float(c["low_price"]),
            "close": float(c["trade_price"]),
            "volume": float(c["candle_acc_trade_volume"]),
        }
        for c in all_candles
    ]


# ─────────────────────────────────────────────────────────
# 소스 2: CoinGecko (BTC/KRW, days=max, 일봉)
# ─────────────────────────────────────────────────────────
def _fetch_coingecko() -> List[Dict]:
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    try:
        r = requests.get(
            url,
            params={"vs_currency": "krw", "days": "max", "interval": "daily"},
            timeout=_TIMEOUT * 2,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("CoinGecko 요청 실패: %s", e)
        return []

    prices = data.get("prices", [])
    total_volumes = {int(v[0]): float(v[1]) for v in data.get("total_volumes", [])}
    out = []
    for i, (ts, close) in enumerate(prices):
        dt = datetime.fromtimestamp(ts / 1000)
        prev_close = float(prices[i - 1][1]) if i > 0 else close
        vol = total_volumes.get(int(ts), 0.0) / close if close else 0.0
        out.append({
            "date": dt.strftime("%Y-%m-%d"),
            "open": prev_close,
            "high": max(prev_close, close) * 1.005,   # 근사
            "low": min(prev_close, close) * 0.995,
            "close": close,
            "volume": vol,
        })
    return out


# ─────────────────────────────────────────────────────────
# 소스 3: Binance (BTCUSDT 일봉 → 고정 환율 환산)
# ─────────────────────────────────────────────────────────
def _fetch_binance(usd_krw: float = 1350.0, limit: int = 1000) -> List[Dict]:
    url = "https://api.binance.com/api/v3/klines"
    try:
        r = requests.get(
            url,
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": limit},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        logger.warning("Binance 요청 실패: %s", e)
        return []

    return [
        {
            "date": datetime.fromtimestamp(row[0] / 1000).strftime("%Y-%m-%d"),
            "open":  float(row[1]) * usd_krw,
            "high":  float(row[2]) * usd_krw,
            "low":   float(row[3]) * usd_krw,
            "close": float(row[4]) * usd_krw,
            "volume": float(row[5]),
        }
        for row in rows
    ]


# ─────────────────────────────────────────────────────────
# 소스 4: 리얼리스틱 합성 (실제 BTC 통계 기반)
# ─────────────────────────────────────────────────────────
# BTC/KRW 레짐별 실측 통계 (2021-01 ~ 2026-06 대략적 특성)
_REGIMES = [
    # (시작, 종료,  기준가,      일간σ,   드리프트/일)
    ("2020-01-01", "2020-12-31",  9_000_000,  0.040,  +0.0020),  # 2020 회복장
    ("2021-01-01", "2021-11-30", 40_000_000,  0.045,  +0.0025),  # 2021 강세
    ("2021-12-01", "2022-06-30", 60_000_000,  0.050,  -0.0030),  # 2022 하락
    ("2022-07-01", "2023-12-31", 25_000_000,  0.038,  -0.0008),  # 2022-23 침체
    ("2024-01-01", "2024-12-31", 55_000_000,  0.042,  +0.0018),  # 2024 반감기 장
    ("2025-01-01", "2025-12-31",100_000_000,  0.035,  +0.0010),  # 2025 강세 지속
    ("2026-01-01", "2026-12-31",140_000_000,  0.025,  +0.0005),  # 2026 현재
]


def _synthetic_realistic(
    start: str = "2017-09-01",
    end: str = None,
    seed: int = 777,
) -> List[Dict]:
    """레짐별 드리프트·변동성을 가진 GBM 합성 BTC/KRW 일봉."""
    if end is None:
        end = date.today().isoformat()
    rng = np.random.default_rng(seed)

    # 레짐 테이블에서 시작가 결정
    start_price = _REGIMES[0][2]
    for r_start, r_end, base_p, _, _ in _REGIMES:
        if start >= r_start:
            start_price = base_p

    out: List[Dict] = []
    cur_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()
    price = float(start_price)

    while cur_date <= end_date:
        ds = cur_date.isoformat()
        # 레짐 찾기
        mu, sigma = 0.0, 0.030
        for r_start, r_end, _, r_sigma, r_mu in _REGIMES:
            if r_start <= ds <= r_end:
                mu, sigma = r_mu, r_sigma
                break

        log_ret = rng.normal(mu, sigma)
        prev = price
        price = prev * math.exp(log_ret)

        # 일중 고저 모사 (HL 폭 ≈ 1.5σ 정규)
        hl_half = abs(rng.normal(0, sigma * 1.5)) * price
        hi = max(prev, price) + hl_half * 0.5
        lo = min(prev, price) - hl_half * 0.5
        vol = abs(rng.normal(800, 300))  # BTC 단위

        out.append({
            "date": ds,
            "open": round(prev),
            "high": round(hi),
            "low": round(max(lo, prev * 0.5)),  # 하한 안전장치
            "close": round(price),
            "volume": round(vol, 2),
        })
        cur_date += timedelta(days=1)

    return out


# ─────────────────────────────────────────────────────────
# 소스 5: 업비트 USDT/KRW 일봉 (KRW-USDT)
# ─────────────────────────────────────────────────────────
def _fetch_upbit_usdt(max_candles: int = 3000) -> List[Dict]:
    """업비트 KRW-USDT 일봉 수집 — BTC와 동일한 페이지네이션 방식."""
    base = "https://api.upbit.com/v1/candles/days"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    all_candles: List[Dict] = []
    to_param = None

    while len(all_candles) < max_candles:
        params: Dict[str, Any] = {"market": "KRW-USDT", "count": 200}
        if to_param:
            params["to"] = to_param
        try:
            r = requests.get(base, params=params, headers=headers, timeout=_TIMEOUT)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            logger.warning("업비트 USDT 요청 실패: %s", e)
            break
        if not batch:
            break
        all_candles.extend(batch)
        oldest = batch[-1]["candle_date_time_kst"][:10]
        to_param = oldest + "T00:00:00"
        time.sleep(_SLEEP)
        if len(batch) < 200:
            break

    all_candles.reverse()
    return [
        {
            "date": c["candle_date_time_kst"][:10],
            "open": float(c["opening_price"]),
            "high": float(c["high_price"]),
            "low": float(c["low_price"]),
            "close": float(c["trade_price"]),
            "volume": float(c["candle_acc_trade_volume"]),
        }
        for c in all_candles
    ]


def fetch_usdt_history(
    start: str = "2017-09-01",
    use_synthetic_fallback: bool = False,
) -> List[Dict]:
    """USDT/KRW 일봉 히스토리 수집 (업비트 단일 소스)."""
    logger.info("[히스토리] USDT 일봉 수집 시작 (목표 시작: %s)", start)
    data = _fetch_upbit_usdt(max_candles=4000)
    if len(data) >= 30:
        data = [d for d in data if d["date"] >= start]
        logger.info("[히스토리] ✅ USDT 업비트 %d일봉 확보 (%s ~ %s)",
                    len(data), data[0]["date"], data[-1]["date"])
        return data
    logger.warning("[히스토리] USDT 데이터 없음 (업비트 응답 부족)")
    return []


# ─────────────────────────────────────────────────────────
# 퍼블릭 인터페이스
# ─────────────────────────────────────────────────────────
def fetch_max_history(
    start: str = "2017-09-01",
    use_synthetic_fallback: bool = True,
) -> List[Dict]:
    """
    가용한 최대 BTC/KRW 일봉 히스토리 수집.
    성공한 첫 번째 소스의 데이터를 반환한다.
    """
    logger.info("[히스토리] 최대 일봉 수집 시작 (목표 시작: %s)", start)

    # 소스 1: 업비트 KRW 직접
    logger.info("[히스토리] 소스1 업비트 시도...")
    data = _fetch_upbit(max_candles=4000)
    if len(data) >= 100:
        data = [d for d in data if d["date"] >= start]
        logger.info("[히스토리] ✅ 업비트 %d일봉 확보 (%s ~ %s)", len(data), data[0]["date"], data[-1]["date"])
        return data

    # 소스 2: CoinGecko KRW
    logger.info("[히스토리] 소스2 CoinGecko 시도...")
    data = _fetch_coingecko()
    if len(data) >= 100:
        data = [d for d in data if d["date"] >= start]
        logger.info("[히스토리] ✅ CoinGecko %d일봉 확보", len(data))
        return data

    # 소스 3: Binance USD → 환산
    logger.info("[히스토리] 소스3 Binance 시도...")
    data = _fetch_binance()
    if len(data) >= 100:
        data = [d for d in data if d["date"] >= start]
        logger.info("[히스토리] ✅ Binance 환산 %d일봉 확보", len(data))
        return data

    # 소스 4: 합성
    if use_synthetic_fallback:
        logger.warning("[히스토리] 모든 외부 소스 실패 → 리얼리스틱 합성 데이터 사용")
        data = _synthetic_realistic(start=start)
        logger.info(
            "[히스토리] ✅ 합성 %d일봉 생성 (%s ~ %s)",
            len(data), data[0]["date"], data[-1]["date"],
        )
        return data

    logger.error("[히스토리] 데이터 없음")
    return []
