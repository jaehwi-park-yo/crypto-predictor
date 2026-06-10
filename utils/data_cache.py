"""
utils/data_cache.py — 히스토리컬 데이터 디스크 캐시
- data/btc_history.json 에 저장
- 파일이 있고 오늘 날짜 데이터를 포함하면 캐시 사용
- 없거나 outdated면 historical_data.fetch_max_history() 호출 후 저장
"""
import json, os, logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger("data_cache")
CACHE_DIR  = Path(__file__).parent.parent / "data"
CACHE_FILE = CACHE_DIR / "btc_history.json"

def _is_fresh(data: List[Dict]) -> bool:
    """마지막 캔들이 오늘 혹은 어제이면 신선(신선 기준: 영업일 기반)."""
    if not data:
        return False
    last_date = data[-1]["date"]
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    # simple: if last date >= yesterday
    return last_date >= yesterday

def load_cache() -> Optional[List[Dict]]:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or len(data) < 100:
            return None
        logger.info("[캐시] 로드 완료: %d일봉 (%s ~ %s)", len(data), data[0]["date"], data[-1]["date"])
        return data
    except Exception as e:
        logger.warning("[캐시] 읽기 실패: %s", e)
        return None

def save_cache(data: List[Dict]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info("[캐시] 저장 완료: %d일봉 → %s", len(data), CACHE_FILE)
    except Exception as e:
        logger.warning("[캐시] 저장 실패: %s", e)

def get_history(start: str = "2020-01-01", force_refresh: bool = False) -> List[Dict]:
    """
    캐시 우선 히스토리 반환. 실패하면 fetch_max_history() 호출 후 저장.
    force_refresh=True면 항상 API에서 새로 받음.
    """
    if not force_refresh:
        cached = load_cache()
        if cached and _is_fresh(cached):
            # filter by start date
            return [d for d in cached if d["date"] >= start]

    from utils.historical_data import fetch_max_history
    logger.info("[캐시] API에서 신규 수집 시작...")
    data = fetch_max_history(start=start)
    if data and len(data) >= 100:
        save_cache(data)
    elif not data:
        # API 실패 → 캐시 반환 (오래돼도)
        cached = load_cache()
        if cached:
            logger.warning("[캐시] API 실패 → 오래된 캐시 사용")
            return [d for d in cached if d["date"] >= start]
    return data
