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
USDT_CACHE_FILE = CACHE_DIR / "usdt_history.json"

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

def validate_against_live(data: List[Dict], live_price: float,
                          max_deviation: float = 0.30) -> bool:
    """캐시 마지막 종가가 실시간 가격과 max_deviation 이상 괴리되면 오염으로 판정."""
    if not data or not live_price or live_price <= 0:
        return True  # 판정 불가 시 통과
    last_close = float(data[-1]["close"])
    dev = abs(last_close - live_price) / live_price
    if dev > max_deviation:
        logger.warning("[캐시] 실시간 가격 검증 실패: 캐시 종가 %s vs 실시간 %s (괴리 %.0f%%)",
                       f"{last_close:,.0f}", f"{live_price:,.0f}", dev * 100)
        return False
    return True


def get_history(start: str = "2020-01-01", force_refresh: bool = False,
                live_price: Optional[float] = None) -> List[Dict]:
    """
    캐시 우선 히스토리 반환. 실패하면 fetch_max_history() 호출 후 저장.
    force_refresh=True면 항상 API에서 새로 받음.
    live_price를 주면 캐시 종가와 30% 이상 괴리 시 강제 재수집 (합성 오염 방지).
    합성 폴백 데이터는 절대 캐시에 저장하지 않는다.
    """
    if not force_refresh:
        cached = load_cache()
        if cached and _is_fresh(cached) and validate_against_live(cached, live_price):
            # filter by start date
            return [d for d in cached if d["date"] >= start]

    from utils.historical_data import fetch_max_history
    logger.info("[캐시] API에서 신규 수집 시작...")
    # 1) 실데이터 소스만 시도 (합성 제외) — 성공 시에만 캐시 저장
    data = fetch_max_history(start=start, use_synthetic_fallback=False)
    if data and len(data) >= 100:
        save_cache(data)
        return data

    # 2) API 전부 실패 → 캐시 반환 (오래되었어도 / live 검증 실패했어도 차선책)
    cached = load_cache()
    if cached:
        if not validate_against_live(cached, live_price):
            logger.warning("[캐시] ⚠️ 캐시가 실시간 가격과 괴리됨 — 합성 오염 가능성. "
                           "네트워크 복구 후 자동 재수집됩니다.")
        else:
            logger.warning("[캐시] API 실패 → 캐시 사용")
        return [d for d in cached if d["date"] >= start]

    # 3) 캐시조차 없음 → 합성 폴백 (캐시 저장 금지 — 오염 방지)
    from utils.historical_data import _synthetic_realistic
    logger.warning("[캐시] ⚠️ 실데이터·캐시 모두 없음 → 합성 폴백 (캐시 저장 안 함)")
    return _synthetic_realistic(start=start)


# ──────────────────────────────────────────────────────────────
# USDT/KRW 캐시 (BTC와 독립적인 파일)
# ──────────────────────────────────────────────────────────────
def _load_usdt_cache() -> Optional[List[Dict]]:
    if not USDT_CACHE_FILE.exists():
        return None
    try:
        with open(USDT_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or len(data) < 30:
            return None
        logger.info("[캐시] USDT 로드 완료: %d일봉 (%s ~ %s)", len(data), data[0]["date"], data[-1]["date"])
        return data
    except Exception as e:
        logger.warning("[캐시] USDT 읽기 실패: %s", e)
        return None


def _save_usdt_cache(data: List[Dict]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(USDT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info("[캐시] USDT 저장 완료: %d일봉 → %s", len(data), USDT_CACHE_FILE)
    except Exception as e:
        logger.warning("[캐시] USDT 저장 실패: %s", e)


def get_usdt_history(start: str = "2020-01-01", force_refresh: bool = False,
                     live_price: Optional[float] = None) -> List[Dict]:
    """USDT/KRW 일봉 캐시 우선 반환 — 구조는 get_history()와 동일."""
    if not force_refresh:
        cached = _load_usdt_cache()
        if cached and _is_fresh(cached) and validate_against_live(cached, live_price):
            return [d for d in cached if d["date"] >= start]

    from utils.historical_data import fetch_usdt_history
    logger.info("[캐시] USDT API에서 신규 수집 시작...")
    data = fetch_usdt_history(start=start)
    if data and len(data) >= 30:
        _save_usdt_cache(data)
        return data

    cached = _load_usdt_cache()
    if cached:
        logger.warning("[캐시] USDT API 실패 → 캐시 사용")
        return [d for d in cached if d["date"] >= start]

    logger.warning("[캐시] USDT 데이터 없음 — 빈 목록 반환")
    return []
