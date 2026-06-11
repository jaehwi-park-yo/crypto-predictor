"""
utils/fx_data.py — 글로벌 시장 데이터: 원/달러 환율 · 달러 BTC · 김치프리미엄
==============================================================================
USDT/KRW는 원/달러 환율에 앵커링된 자산이므로, 환율 σ를 USDT 박스권
추정에 블렌딩하면 표본 부족(일봉 ~2년)을 보완할 수 있다.
또한 김치프리미엄(KRW BTC vs USD BTC×환율 괴리)은 BTC/USDT 공통의
수급 지표로 ML σ 보정 특징에 추가된다.

데이터 소스 (모두 무료·API 키 불필요):
  1. 원/달러 환율  : Frankfurter (ECB 기준환율, 1999~, 주말 결측 → 전일 채움)
  2. 달러 BTC 일봉 : Binance BTCUSDT klines (1000개/요청 페이지네이션)
                     폴백: CoinGecko market_chart
  3. 김치프리미엄  : KRW-BTC 종가 / (BTCUSDT 종가 × USD/KRW) − 1

캐시:
  data/fx_usdkrw.json      [{"date","close"}, ...]
  data/btc_usd_history.json [{"date","open","high","low","close","volume"}, ...]

모든 함수는 best-effort: 네트워크 실패 시 캐시 반환, 캐시도 없으면 빈 목록.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

from config import FRANKFURTER_BASE_URL, ERAPI_FX_URL, BINANCE_BASE_URL, COINGECKO_BASE_URL

logger = logging.getLogger("fx_data")

DATA_DIR = Path(__file__).parent.parent / "data"
FX_CACHE = DATA_DIR / "fx_usdkrw.json"
BTC_USD_CACHE = DATA_DIR / "btc_usd_history.json"

_TIMEOUT = 12
_SLEEP = 0.25
_FX_START = "2017-09-01"   # BTC/KRW 히스토리 시작과 동일

# 프로세스 내 메모 — ML 재학습이 월별로 반복 호출해도 네트워크/디스크 1회만
_FETCH_FAILED_AT: Dict[str, float] = {}   # 소스별 마지막 실패 시각 (쿨다운 10분)
_FAIL_COOLDOWN_SEC = 600
_memo: Dict[str, List[Dict]] = {}


def _recently_failed(key: str) -> bool:
    return time.time() - _FETCH_FAILED_AT.get(key, 0) < _FAIL_COOLDOWN_SEC


def _mark_failed(key: str) -> None:
    _FETCH_FAILED_AT[key] = time.time()


# ──────────────────────────────────────────────────────────────
# 공통 캐시 헬퍼
# ──────────────────────────────────────────────────────────────
def _load_json(path: Path, min_rows: int = 30) -> Optional[List[Dict]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) >= min_rows:
            return data
    except Exception as e:
        logger.warning("[FX] 캐시 읽기 실패 (%s): %s", path.name, e)
    return None


def _save_json(path: Path, data: List[Dict]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("[FX] 캐시 저장: %s (%d행)", path.name, len(data))
    except Exception as e:
        logger.warning("[FX] 캐시 저장 실패 (%s): %s", path.name, e)


def _is_fresh(data: List[Dict], max_age_days: int = 3) -> bool:
    """주말·ECB 휴장 고려해 며칠 여유를 둔 신선도 판정."""
    if not data:
        return False
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    return data[-1]["date"] >= cutoff


# ──────────────────────────────────────────────────────────────
# 원/달러 환율 (Frankfurter — ECB 기준환율)
# ──────────────────────────────────────────────────────────────
def _fetch_frankfurter(start: str, end: str) -> List[Dict]:
    """기간 조회 — Frankfurter는 한 요청으로 수년치 반환 가능."""
    url = f"{FRANKFURTER_BASE_URL}/{start}..{end}"
    r = requests.get(url, params={"from": "USD", "to": "KRW"},
                     timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    rates = r.json().get("rates", {})
    return sorted(
        ({"date": d, "close": float(v["KRW"])} for d, v in rates.items() if "KRW" in v),
        key=lambda x: x["date"],
    )


def _forward_fill_daily(rows: List[Dict]) -> List[Dict]:
    """주말/휴장 결측일을 직전 영업일 값으로 채워 연속 일봉으로 변환."""
    if not rows:
        return rows
    out: List[Dict] = []
    cur = datetime.strptime(rows[0]["date"], "%Y-%m-%d").date()
    last_date = datetime.strptime(rows[-1]["date"], "%Y-%m-%d").date()
    by_date = {r["date"]: r["close"] for r in rows}
    prev = rows[0]["close"]
    while cur <= last_date:
        ds = cur.isoformat()
        prev = by_date.get(ds, prev)
        out.append({"date": ds, "close": prev})
        cur += timedelta(days=1)
    return out


def get_fx_history(start: str = _FX_START, force_refresh: bool = False) -> List[Dict]:
    """USD/KRW 일별 환율 (주말 전일 채움). 캐시 우선, 증분 갱신."""
    if not force_refresh and "fx" in _memo:
        return [r for r in _memo["fx"] if r["date"] >= start]
    cached = _load_json(FX_CACHE)
    if not force_refresh and cached and (_is_fresh(cached) or _recently_failed("fx")):
        _memo["fx"] = cached
        return [r for r in cached if r["date"] >= start]
    if not force_refresh and _recently_failed("fx"):
        return []

    today = date.today().isoformat()
    fetch_from = start
    if cached:
        # 증분: 마지막 캐시일 이후만 조회 (휴장 보정 위해 7일 겹침)
        last = datetime.strptime(cached[-1]["date"], "%Y-%m-%d").date()
        fetch_from = (last - timedelta(days=7)).isoformat()

    try:
        new_rows = _fetch_frankfurter(fetch_from, today)
    except Exception as e:
        logger.warning("[FX] Frankfurter 실패: %s — 캐시 폴백", e)
        _mark_failed("fx")
        new_rows = []

    if new_rows:
        merged = {r["date"]: r["close"] for r in (cached or [])}
        merged.update({r["date"]: r["close"] for r in new_rows})
        rows = _forward_fill_daily(
            sorted(({"date": d, "close": c} for d, c in merged.items()),
                   key=lambda x: x["date"])
        )
        _save_json(FX_CACHE, rows)
        _memo["fx"] = rows
        return [r for r in rows if r["date"] >= start]

    if cached:
        _memo["fx"] = cached
        return [r for r in cached if r["date"] >= start]
    return []


def latest_fx_rate() -> Optional[float]:
    """가장 최근 USD/KRW 환율. 캐시 → er-api 당일 시세 순으로 시도."""
    hist = get_fx_history()
    if hist and _is_fresh(hist):
        return hist[-1]["close"]
    try:
        r = requests.get(ERAPI_FX_URL, timeout=_TIMEOUT)
        r.raise_for_status()
        v = r.json().get("rates", {}).get("KRW")
        if v:
            return float(v)
    except Exception as e:
        logger.debug("[FX] er-api 폴백 실패: %s", e)
    return hist[-1]["close"] if hist else None


def fx_daily_sigma_asof(as_of: str, lookback: int = 90, span: int = 60) -> Optional[float]:
    """as_of 이전 환율로 EWMA 일간 σ 추정 (USDT σ 블렌딩용, 미래 누설 없음)."""
    hist = get_fx_history()
    closes = [r["close"] for r in hist if r["date"] <= as_of[:10]]
    if len(closes) < 30:
        return None
    from utils.statistics import compute_log_returns, ewma_daily_volatility
    rets = compute_log_returns(closes[-lookback:])
    sig = ewma_daily_volatility(rets, span=span)
    return sig if sig > 0 else None


# ──────────────────────────────────────────────────────────────
# 달러 BTC 일봉 (Binance BTCUSDT → 폴백 CoinGecko)
# ──────────────────────────────────────────────────────────────
def _fetch_binance_btcusdt(start: str) -> List[Dict]:
    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    start_ms = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    out: List[Dict] = []
    while True:
        r = requests.get(url, params={"symbol": "BTCUSDT", "interval": "1d",
                                      "startTime": start_ms, "limit": 1000},
                         timeout=_TIMEOUT)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for k in batch:
            out.append({
                "date": datetime.utcfromtimestamp(k[0] / 1000).strftime("%Y-%m-%d"),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            })
        if len(batch) < 1000:
            break
        start_ms = batch[-1][0] + 86_400_000
        time.sleep(_SLEEP)
    return out


def _fetch_coingecko_btc_usd() -> List[Dict]:
    url = f"{COINGECKO_BASE_URL}/coins/bitcoin/market_chart"
    r = requests.get(url, params={"vs_currency": "usd", "days": "max", "interval": "daily"},
                     timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    prices = r.json().get("prices", [])
    out: List[Dict] = []
    for ts, p in prices:
        d = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        if not out or out[-1]["date"] != d:
            out.append({"date": d, "open": float(p), "high": float(p),
                        "low": float(p), "close": float(p), "volume": 0.0})
    return out


def get_btc_usd_history(start: str = _FX_START, force_refresh: bool = False) -> List[Dict]:
    """BTC/USD 일봉. 캐시 우선, 증분 갱신, Binance → CoinGecko 폴백."""
    if not force_refresh and "btc_usd" in _memo:
        return [r for r in _memo["btc_usd"] if r["date"] >= start]
    cached = _load_json(BTC_USD_CACHE, min_rows=100)
    if not force_refresh and cached and (_is_fresh(cached, max_age_days=2)
                                         or _recently_failed("btc_usd")):
        _memo["btc_usd"] = cached
        return [r for r in cached if r["date"] >= start]
    if not force_refresh and _recently_failed("btc_usd"):
        return []

    fetch_from = start
    if cached:
        fetch_from = cached[-1]["date"]  # 마지막 일자부터 (당일 갱신 포함)

    rows: List[Dict] = []
    try:
        rows = _fetch_binance_btcusdt(fetch_from)
    except Exception as e:
        logger.warning("[FX] Binance 실패: %s", e)
        if not cached:
            try:
                rows = _fetch_coingecko_btc_usd()
                fetch_from = _FX_START
            except Exception as e2:
                logger.warning("[FX] CoinGecko 폴백 실패: %s", e2)
        if not rows:
            _mark_failed("btc_usd")

    if rows:
        merged = {r["date"]: r for r in (cached or [])}
        merged.update({r["date"]: r for r in rows})
        all_rows = sorted(merged.values(), key=lambda x: x["date"])
        _save_json(BTC_USD_CACHE, all_rows)
        _memo["btc_usd"] = all_rows
        return [r for r in all_rows if r["date"] >= start]

    if cached:
        _memo["btc_usd"] = cached
        return [r for r in cached if r["date"] >= start]
    return []


# ──────────────────────────────────────────────────────────────
# 김치프리미엄
# ──────────────────────────────────────────────────────────────
def kimchi_premium_series(start: str = _FX_START) -> List[Dict]:
    """일별 김치프리미엄(%) = KRW BTC / (USD BTC × USD/KRW) − 1.

    주의: 업비트 일봉은 KST, Binance/ECB는 UTC 기준이라 같은 날짜 라벨이
    수 시간 어긋난 구간을 가리킨다. 김프는 일 단위로 완만히 움직이므로
    일봉 근사로 허용 (ML 특징·추세 판단 용도).

    세 데이터의 날짜 교집합만 반환. 어느 하나라도 없으면 빈 목록.
    [{"date", "kimp_pct", "btc_krw", "btc_usd", "usdkrw"}, ...]
    """
    if "kimp" in _memo:
        return [r for r in _memo["kimp"] if r["date"] >= start]
    from utils.data_cache import load_cache
    krw = load_cache() or []
    usd = get_btc_usd_history(start)
    fx = get_fx_history(start)
    if not krw or not usd or not fx:
        return []

    usd_by = {r["date"]: float(r["close"]) for r in usd}
    fx_by = {r["date"]: float(r["close"]) for r in fx}
    out: List[Dict] = []
    for c in krw:
        d = c["date"][:10]
        if d < start or d not in usd_by or d not in fx_by:
            continue
        implied = usd_by[d] * fx_by[d]
        if implied <= 0:
            continue
        out.append({
            "date": d,
            "kimp_pct": round((float(c["close"]) / implied - 1) * 100, 4),
            "btc_krw": float(c["close"]),
            "btc_usd": usd_by[d],
            "usdkrw": fx_by[d],
        })
    _memo["kimp"] = out
    return out


def kimp_features_asof(as_of: str) -> Optional[Dict[str, float]]:
    """ML 특징용: as_of 기준 김프 수준·30일 변화·FX 30일 σ. 데이터 부족 시 None."""
    series = kimchi_premium_series()
    upto = [r for r in series if r["date"] <= as_of[:10]]
    if len(upto) < 35:
        return None
    kimp_now = upto[-1]["kimp_pct"]
    kimp_30d_ago = upto[-31]["kimp_pct"]
    from utils.statistics import compute_log_returns, daily_volatility
    fx_closes = [r["usdkrw"] for r in upto[-91:]]
    fx_sig30 = daily_volatility(compute_log_returns(fx_closes)[-30:])
    return {
        "kimp_pct": kimp_now,
        "kimp_chg_30d": kimp_now - kimp_30d_ago,
        "fx_sigma30_monthly_pct": fx_sig30 * math.sqrt(30) * 100,
    }


def sync_all() -> Dict[str, int]:
    """기동 시 호출용: 환율 + 달러 BTC 동기화 (best-effort)."""
    _memo.clear()
    fx = get_fx_history()
    usd = get_btc_usd_history()
    return {"fx_days": len(fx), "btc_usd_days": len(usd)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    print(json.dumps(sync_all(), ensure_ascii=False))
    kp = kimchi_premium_series()
    if kp:
        print(f"김프 최근: {kp[-1]}")
