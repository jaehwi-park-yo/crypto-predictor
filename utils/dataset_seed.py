"""
utils/dataset_seed.py — ZIP 시드 파일에서 로컬 DB/캐시 복원
=============================================================
내보낸 데이터셋 ZIP(dataset_export.py 생성)을 data/dataset_seed.zip 위치에 두면
서버 최초 기동 시 API 대신 이 파일에서 데이터를 복원한다.

복원 순서 (기동 시 _minute_collector 앞에 호출):
  1. data/dataset_seed.zip 존재 여부 확인
  2. minute_KRW-BTC_5m.csv / minute_KRW-USDT_5m.csv → SQLite minute_candles
  3. daily_btc.csv  → data/btc_history.json  (캐시 파일)
  4. daily_usdt.csv → data/usdt_history.json (캐시 파일)
  5. 복원 완료 후 dataset_seed.zip → dataset_seed.zip.imported 로 이름 변경
     (재기동 시 재처리 방지; 다시 필요하면 .imported 제거)

이후 _minute_collector는 DB에 데이터가 있으므로 증분 sync만 실행한다.

사용 방법:
  1. 서버에서 📤 데이터셋 내보내기 버튼으로 ZIP 다운로드
  2. ZIP 파일을 data/dataset_seed.zip 으로 복사
  3. 서버 (재)기동 → 자동 복원 + 증분 sync
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("dataset_seed")

ROOT = Path(__file__).parent.parent
SEED_PATH = ROOT / "data" / "dataset_seed.zip"
SEED_DONE_PATH = ROOT / "data" / "dataset_seed.zip.imported"
DB_PATH = ROOT / "data" / "candles.db"
BTC_CACHE = ROOT / "data" / "btc_history.json"
USDT_CACHE = ROOT / "data" / "usdt_history.json"
FX_CACHE = ROOT / "data" / "fx_usdkrw.json"
BTC_USD_CACHE = ROOT / "data" / "btc_usd_history.json"


def _seed_available() -> Optional[Path]:
    """복원할 시드 파일이 있으면 경로 반환, 없으면 None."""
    if SEED_PATH.exists():
        return SEED_PATH
    return None


def _db_has_data() -> bool:
    """SQLite에 분봉이 1건이라도 있으면 True."""
    if not DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        row = conn.execute("SELECT COUNT(*) FROM minute_candles").fetchone()
        conn.close()
        return row[0] > 0
    except Exception:
        return False


def _cache_has_data() -> bool:
    """BTC 일봉 캐시가 100건 이상이면 True."""
    if not BTC_CACHE.exists():
        return False
    try:
        data = json.loads(BTC_CACHE.read_text(encoding="utf-8"))
        return isinstance(data, list) and len(data) >= 100
    except Exception:
        return False


def _read_csv_from_zip(zf: zipfile.ZipFile, name: str) -> List[Dict]:
    try:
        raw = zf.read(name).decode("utf-8")
    except KeyError:
        return []
    reader = csv.DictReader(io.StringIO(raw))
    return list(reader)


def _restore_minutes(zf: zipfile.ZipFile) -> Dict[str, int]:
    """분봉 CSV → SQLite. 테이블이 없으면 생성."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
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

    inserted: Dict[str, int] = {}
    for market_key, csv_name in [("KRW-BTC", "minute_KRW-BTC_5m.csv"),
                                  ("KRW-USDT", "minute_KRW-USDT_5m.csv")]:
        rows = _read_csv_from_zip(zf, csv_name)
        if not rows:
            logger.warning("[시드] %s 없음 — 건너뜀", csv_name)
            continue

        batch = [
            (market_key, 5, r["ts"],
             float(r["open"]), float(r["high"]), float(r["low"]),
             float(r["close"]), float(r["volume"]))
            for r in rows if r.get("ts")
        ]
        cur = conn.executemany(
            "INSERT OR IGNORE INTO minute_candles"
            "(market, unit, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
            batch,
        )
        conn.commit()
        inserted[market_key] = cur.rowcount
        logger.info("[시드] %s: %d행 삽입 (전체 %d행)", market_key, cur.rowcount, len(batch))

    conn.close()
    return inserted


def _restore_daily(zf: zipfile.ZipFile, csv_name: str,
                   cache_path: Path, min_rows: int = 30) -> int:
    """일봉 CSV → JSON 캐시 파일."""
    rows = _read_csv_from_zip(zf, csv_name)
    if len(rows) < min_rows:
        logger.warning("[시드] %s 없음 또는 너무 적음 (%d행) — 건너뜀", csv_name, len(rows))
        return 0

    # 숫자 변환
    converted = []
    for r in rows:
        try:
            converted.append({
                "date":   r["date"],
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": float(r["volume"]),
            })
        except (KeyError, ValueError):
            continue

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(converted, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("[시드] %s → %s (%d행)", csv_name, cache_path.name, len(converted))
    return len(converted)


def _restore_fx(zf: zipfile.ZipFile) -> int:
    """fx_usdkrw.csv → JSON 캐시 (date/close 2컬럼 — _restore_daily와 스키마 다름)."""
    rows = _read_csv_from_zip(zf, "fx_usdkrw.csv")
    if len(rows) < 30:
        return 0
    converted = []
    for r in rows:
        try:
            converted.append({"date": r["date"], "close": float(r["close"])})
        except (KeyError, ValueError):
            continue
    FX_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FX_CACHE.write_text(json.dumps(converted, ensure_ascii=False), encoding="utf-8")
    logger.info("[시드] fx_usdkrw.csv → %s (%d행)", FX_CACHE.name, len(converted))
    return len(converted)


def restore_from_seed(force: bool = False) -> bool:
    """
    시드 ZIP이 있고 DB/캐시가 비어있으면 복원한다.

    force=True 면 DB/캐시 유무와 관계없이 강제 복원.
    반환값: 복원 수행 여부.
    """
    seed = _seed_available()
    if seed is None:
        return False

    # 일봉 캐시·분봉 DB가 모두 차 있을 때만 건너뜀.
    # 시드가 분봉을 포함하므로, 일봉만 있는 기존 설치자도 분봉을 자동 복원받는다.
    # (복원 후 zip → .imported rename + INSERT OR IGNORE라 중복 위험 없음)
    if not force and _cache_has_data() and _db_has_data():
        logger.info("[시드] 일봉 캐시·분봉 DB 모두 존재 — 시드 복원 건너뜀 (%s)", seed.name)
        return False

    logger.info("[시드] 복원 시작: %s (%.1f MB)", seed, seed.stat().st_size / 1e6)
    try:
        with zipfile.ZipFile(seed, "r") as zf:
            _restore_minutes(zf)
            _restore_daily(zf, "daily_btc.csv", BTC_CACHE, min_rows=100)
            _restore_daily(zf, "daily_usdt.csv", USDT_CACHE, min_rows=30)
            _restore_fx(zf)
            _restore_daily(zf, "daily_btc_usd.csv", BTC_USD_CACHE, min_rows=100)
    except Exception as e:
        logger.error("[시드] 복원 실패: %s", e)
        return False

    # 성공 → .imported 로 rename (재기동 시 재처리 방지)
    try:
        SEED_DONE_PATH.unlink(missing_ok=True)
        seed.rename(SEED_DONE_PATH)
        logger.info("[시드] 복원 완료 → %s 로 이름 변경", SEED_DONE_PATH.name)
    except Exception as e:
        logger.warning("[시드] rename 실패 (무시): %s", e)

    return True
