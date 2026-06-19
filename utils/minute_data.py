"""
utils/minute_data.py — 업비트 분봉 데이터 파이프라인 (F1 개선용 데이터셋)
=======================================================================
- 최초 부팅: 업비트 공개 API에서 가능한 최대 분봉 히스토리 부트스트랩 (상장 시점 ~2017-09까지)
- 이후 부팅: 마지막 저장 시각 이후 누락분만 증분 수집(sync)
- 저장소: SQLite data/candles.db (WAL 모드)

CLI:
    python -m utils.minute_data --bootstrap
    python -m utils.minute_data --sync
    python -m utils.minute_data --stats
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import requests

logger = logging.getLogger("minute_data")

_UPBIT_MINUTES = "https://api.upbit.com/v1/candles/minutes/{unit}"
_TIMEOUT = 12
_REQ_SLEEP = 0.15  # 업비트 레이트리밋 대비

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "candles.db")

MARKETS = ("KRW-BTC", "KRW-USDT")


# ──────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS minute_candles(
            market TEXT NOT NULL,
            unit   INTEGER NOT NULL,
            ts     TEXT NOT NULL,
            open   REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY(market, unit, ts)
        )
    """)
    return conn


# ──────────────────────────────────────────────────────────────
# 업비트 API
# ──────────────────────────────────────────────────────────────
def fetch_minutes_upbit(market: str, unit: int = 5,
                        to: Optional[str] = None, count: int = 200) -> List[Dict]:
    """업비트 분봉 1회 호출. 실패 시 [] (429는 1초 대기 후 1회 재시도)."""
    params = {"market": market, "count": count}
    if to:
        params["to"] = to
    for attempt in range(2):
        try:
            r = requests.get(
                _UPBIT_MINUTES.format(unit=unit),
                params=params,
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                timeout=_TIMEOUT,
            )
            if r.status_code == 429:
                if attempt == 0:
                    logger.warning("[minute_data] 429 rate limit — 1초 대기 후 재시도")
                    time.sleep(1.0)
                    continue
                logger.warning("[minute_data] 429 재시도 실패: %s", market)
                return []
            if r.status_code == 403:
                logger.warning("[minute_data] 403 차단 (네트워크 환경 제한): %s", market)
                return []
            r.raise_for_status()
            data = r.json()
            return [
                {
                    "ts":     c["candle_date_time_kst"],
                    "open":   float(c["opening_price"]),
                    "high":   float(c["high_price"]),
                    "low":    float(c["low_price"]),
                    "close":  float(c["trade_price"]),
                    "volume": float(c["candle_acc_trade_volume"]),
                }
                for c in data
            ]
        except Exception as e:
            logger.warning("[minute_data] 분봉 수집 실패 (%s): %s", market, e)
            return []
    return []


# ──────────────────────────────────────────────────────────────
# 수집 로직
# ──────────────────────────────────────────────────────────────
def _insert(conn: sqlite3.Connection, market: str, unit: int, rows: List[Dict]) -> int:
    cur = conn.executemany(
        "INSERT OR IGNORE INTO minute_candles(market, unit, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(market, unit, r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"])
         for r in rows],
    )
    conn.commit()
    return cur.rowcount


def _max_ts(conn: sqlite3.Connection, market: str, unit: int) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(ts) FROM minute_candles WHERE market=? AND unit=?",
        (market, unit)).fetchone()
    return row[0] if row and row[0] else None


def bootstrap(market: str, unit: int = 5, max_days: int = 3650,
              progress_cb: Optional[Callable] = None) -> int:
    """과거 방향으로 페이지네이션하며 최대 max_days까지 전체 수집.

    업비트는 상장 시점(KRW-BTC: 2017-09)까지 분봉을 제공하므로
    max_days=3650(10년)이면 사실상 전체 히스토리를 받는다.
    상장일 이전에 도달하면 API가 빈 응답을 반환해 자동 종료된다.
    """
    conn = _connect()
    try:
        cutoff = (datetime.now() - timedelta(days=max_days)).strftime("%Y-%m-%dT%H:%M:%S")
        inserted = 0
        fetched = 0
        to: Optional[str] = None
        page = 0
        while True:
            rows = fetch_minutes_upbit(market, unit, to=to)
            if not rows:
                break
            fetched += len(rows)
            n = _insert(conn, market, unit, rows)
            inserted += n
            oldest = min(r["ts"] for r in rows)
            page += 1
            if progress_cb and page % 10 == 0:
                progress_cb(fetched, oldest)
            # 종료 조건: 페이지 전체가 이미 DB에 존재(이전 부트스트랩 도달) 또는 cutoff 초과
            if n == 0:
                break
            if oldest <= cutoff:
                break
            to = oldest.replace("T", " ")  # 다음 페이지: 가장 오래된 캔들 이전
            time.sleep(_REQ_SLEEP)
        logger.info("[minute_data] bootstrap %s: %d행 삽입", market, inserted)
        return inserted
    finally:
        conn.close()


def sync(market: str, unit: int = 5) -> int:
    """증분 수집: DB의 MAX(ts) 이후 누락분만 수집. DB 비어있으면 bootstrap."""
    conn = _connect()
    try:
        last = _max_ts(conn, market, unit)
    finally:
        conn.close()
    if last is None:
        return bootstrap(market, unit)

    conn = _connect()
    try:
        inserted = 0
        to: Optional[str] = None
        while True:
            rows = fetch_minutes_upbit(market, unit, to=to)
            if not rows:
                break
            inserted += _insert(conn, market, unit, rows)
            oldest = min(r["ts"] for r in rows)
            if oldest <= last:
                break
            to = oldest.replace("T", " ")
            time.sleep(_REQ_SLEEP)
        logger.info("[minute_data] sync %s: %d행 삽입", market, inserted)
        return inserted
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────
# 조회 / 집계 (1m 단일소스 → 해상도별 파생)
# ──────────────────────────────────────────────────────────────
def _bucket_ts(ts: str, unit: int) -> str:
    """ts(YYYY-MM-DDTHH:MM:SS)를 unit분 버킷 시작 시각으로 내림."""
    # HH:MM 추출 (초는 분봉이라 00)
    date, _, hm = ts.partition("T")
    hh = int(hm[0:2]); mm = int(hm[3:5])
    tot = (hh * 60 + mm) // unit * unit
    return f"{date}T{tot // 60:02d}:{tot % 60:02d}:00"


def aggregate_minute(rows: List[Dict], unit: int) -> List[Dict]:
    """1분봉(또는 더 잘은 단위) 캔들을 unit분 OHLCV로 집계. ts 오름차순 가정."""
    if unit <= 1 or not rows:
        return rows
    out: List[Dict] = []
    cur_key: Optional[str] = None
    o = h = l = c = v = 0.0
    for r in rows:
        key = _bucket_ts(r["ts"], unit)
        if key != cur_key:
            if cur_key is not None:
                out.append({"ts": cur_key, "open": o, "high": h, "low": l, "close": c, "volume": v})
            cur_key = key
            o = r["open"]; h = r["high"]; l = r["low"]; c = r["close"]; v = r["volume"]
        else:
            h = max(h, r["high"]); l = min(l, r["low"]); c = r["close"]; v += r["volume"]
    if cur_key is not None:
        out.append({"ts": cur_key, "open": o, "high": h, "low": l, "close": c, "volume": v})
    return out


def aggregate_daily(rows: List[Dict]) -> List[Dict]:
    """분봉 → 일봉 OHLCV. 반환 키는 일봉 관례(date)."""
    out: List[Dict] = []
    cur_day: Optional[str] = None
    o = h = l = c = v = 0.0
    for r in rows:
        day = r["ts"][:10]
        if day != cur_day:
            if cur_day is not None:
                out.append({"date": cur_day, "open": o, "high": h, "low": l, "close": c, "volume": v})
            cur_day = day
            o = r["open"]; h = r["high"]; l = r["low"]; c = r["close"]; v = r["volume"]
        else:
            h = max(h, r["high"]); l = min(l, r["low"]); c = r["close"]; v += r["volume"]
    if cur_day is not None:
        out.append({"date": cur_day, "open": o, "high": h, "low": l, "close": c, "volume": v})
    return out


def _raw_candles(conn, market: str, unit: int,
                 start: Optional[str], end: Optional[str]) -> List[Dict]:
    q = "SELECT ts, open, high, low, close, volume FROM minute_candles WHERE market=? AND unit=?"
    params: list = [market, unit]
    if start:
        q += " AND ts >= ?"; params.append(start)
    if end:
        q += " AND ts <= ?"; params.append(end)
    q += " ORDER BY ts"
    return [
        {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
        for r in conn.execute(q, params)
    ]


def get_candles(market: str, unit: int = 5,
                start: Optional[str] = None, end: Optional[str] = None,
                derive: bool = True) -> List[Dict]:
    """
    market/unit 분봉 조회. 네이티브 unit 데이터가 없고 derive=True면
    1분봉(단일소스)에서 해당 unit으로 온더플라이 집계해 반환한다.
    더 미세한 네이티브 단위가 있으면 그걸로 집계(예: unit=5 없고 1m 있으면 1m→5m).
    """
    conn = _connect()
    try:
        rows = _raw_candles(conn, market, unit, start, end)
        if rows or not derive or unit <= 1:
            return rows
        # 파생: unit을 나눌 수 있는 가장 큰 네이티브 단위 탐색 (1 우선)
        for base in (1, 2, 3, 5, 10, 15):
            if base >= unit or unit % base != 0:
                continue
            base_rows = _raw_candles(conn, market, base, start, end)
            if base_rows:
                logger.debug("[minute_data] %s %dm 네이티브 부재 → %dm에서 파생", market, unit, base)
                return aggregate_minute(base_rows, unit)
        return rows
    finally:
        conn.close()


def get_daily_candles(market: str, start: Optional[str] = None,
                      end: Optional[str] = None) -> List[Dict]:
    """1분봉(또는 가장 미세한 네이티브 분봉)에서 일봉 OHLCV 파생. 분봉 없으면 []."""
    conn = _connect()
    try:
        for base in (1, 3, 5):
            rows = _raw_candles(conn, market, base, start, end)
            if rows:
                return aggregate_daily(rows)
        return []
    finally:
        conn.close()


def get_stats() -> Dict:
    conn = _connect()
    try:
        stats = {}
        for market, unit, cnt, oldest, newest in conn.execute(
            "SELECT market, unit, COUNT(*), MIN(ts), MAX(ts) "
            "FROM minute_candles GROUP BY market, unit"
        ):
            stats[f"{market}_{unit}m"] = {
                "market": market, "unit": unit,
                "count": cnt, "oldest": oldest, "newest": newest}
        return stats
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="업비트 분봉 데이터 파이프라인")
    ap.add_argument("--bootstrap", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--unit", type=int, default=5)
    args = ap.parse_args()

    if args.bootstrap:
        for m in MARKETS:
            n = bootstrap(m, args.unit,
                          progress_cb=lambda f, o, m=m: print(f"  {m}: {f}개 수집, 최고(最古) {o}"))
            print(f"{m}: {n}행 삽입")
    elif args.sync:
        for m in MARKETS:
            print(f"{m}: {sync(m, args.unit)}행 삽입")
    elif args.stats:
        print(json.dumps(get_stats(), ensure_ascii=False, indent=2))
    else:
        ap.print_help()
