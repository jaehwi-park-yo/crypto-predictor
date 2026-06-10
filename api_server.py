"""
api_server.py — 실시간 가격 API 서버 (index.html 연동용)

실행:
    pip install fastapi uvicorn
    python api_server.py          # http://localhost:8000

엔드포인트:
    GET /api/price        → BTC + USDT 현재가
    GET /api/history      → 최근 90일 BTC 일봉 (캐시 우선)
    GET /api/predict      → as-of 예측 (as_of, capital 파라미터)
"""
from __future__ import annotations
import logging, sys
from datetime import date, timedelta
from typing import Literal, Optional

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
    format="%(asctime)s [%(name)s] %(message)s")

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
except ImportError:
    print("fastapi/uvicorn 미설치. 설치: pip install fastapi uvicorn")
    sys.exit(1)

from calendar import monthrange
import threading
from utils import minute_data
from utils.live_data import fetch_current_price, fetch_usdt_price
from utils.data_cache import get_history
from services.prediction_service import predict_as_of
from services.prediction_monitor import monitor as monitor_prediction

app = FastAPI(title="BTC Grid Prediction API", version="1.0.0")

# CORS — index.html (file://) + 로컬 개발 서버 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/price")
def get_price():
    """BTC/KRW + USDT/KRW 현재가."""
    history = get_history()
    fallback_btc = float(history[-1]["close"]) if history else None
    btc  = fetch_current_price(fallback_price=fallback_btc)
    usdt = fetch_usdt_price()
    return {
        "btc": {
            "price":      btc.price,
            "prev_close": btc.prev_close,
            "change_pct": round(btc.change_pct, 4),
            "source":     btc.source,
            "is_live":    btc.is_live,
            "timestamp":  btc.timestamp,
        },
        "usdt": {
            "price":      usdt.price,
            "prev_close": usdt.prev_close,
            "change_pct": round(usdt.change_pct, 4),
            "source":     usdt.source,
            "is_live":    usdt.is_live,
            "timestamp":  usdt.timestamp,
        },
    }


@app.get("/api/history")
def get_history_api(days: int = Query(90, ge=10, le=1000)):
    """최근 N일 BTC/KRW 일봉 (캐시 우선)."""
    data = get_history()
    return {"candles": data[-days:], "count": min(days, len(data))}


@app.get("/api/predict")
def get_predict(
    as_of:     str   = Query(default=None, description="YYYY-MM-DD (기본: 어제)"),
    capital:   float = Query(default=40_000_000, ge=1_000_000),
    krw_hold:  float = Query(default=0.30, ge=0.0, le=0.7),
    aggressiveness: Literal["conservative", "balanced", "aggressive"] = Query(default="balanced"),
    target_month: Optional[str] = Query(default=None, description="YYYY-MM 형식 대상 월 (현재달 수정예측용)"),
):
    """as-of 시점 기준 익월 박스권 예측.

    target_month를 지정하면 as_of를 해당 달의 전달 말일로 조정하여
    현재 달 예측을 가져올 수 있습니다.
    """
    if as_of is None:
        as_of = (date.today() - timedelta(days=1)).isoformat()
    else:
        try:
            date.fromisoformat(as_of)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of는 YYYY-MM-DD 형식이어야 합니다")

    # target_month가 지정된 경우, predict_as_of가 해당 월을 예측하도록
    # as_of를 대상 달의 전달 말일로 조정
    if target_month is not None:
        try:
            from datetime import datetime as _dt
            tm = _dt.strptime(target_month, "%Y-%m")
            # 전달 말일 = 대상 월 1일에서 하루 빼기
            prev_month_last = (tm - timedelta(days=1)).date()
            as_of = prev_month_last.isoformat()
        except ValueError:
            raise HTTPException(status_code=400, detail="target_month는 YYYY-MM 형식이어야 합니다")

    history = get_history()
    try:
        snap = predict_as_of(
            history, as_of,
            capital_krw=capital,
            krw_hold_ratio=krw_hold,
            aggressiveness=aggressiveness,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "as_of":         snap.as_of_date,
        "target":        snap.target_month,
        "ref":           snap.reference_price,
        "u1": snap.box_upper_1s, "l1": snap.box_lower_1s,
        "u2": snap.box_upper_2s, "l2": snap.box_lower_2s,
        "rec_upper":     snap.recommended_upper,
        "rec_lower":     snap.recommended_lower,
        "monthly_sigma": round(snap.monthly_sigma_pct, 2),
        "daily_sigma":   round(snap.daily_sigma * 100, 2),
        "bots":          snap.bot_count,
        "grid_buy_pct":  snap.buy_interval_pct,
        "grid_sell_pct": snap.sell_interval_pct,
        "volume_est":    round(snap.estimated_monthly_volume_krw),
        "reward_est":    round(snap.estimated_reward_krw),
    }


@app.get("/api/dashboard")
def get_dashboard(
    capital: float = Query(default=40_000_000, ge=1_000_000),
    krw_hold: float = Query(default=0.30, ge=0.0, le=0.7),
    aggressiveness: Literal["conservative", "balanced", "aggressive"] = Query(default="balanced"),
    history_days: int = Query(default=120, ge=30, le=365),
):
    """대시보드 전체 데이터: 예측 스냅, 모니터링, 차트용 캔들, 그리드 라인."""
    # 실시간 가격을 먼저 확보 → 캐시가 실가격과 30% 이상 괴리되면 강제 재수집
    # (합성 데이터로 오염된 캐시가 영구 사용되는 것을 방지)
    live_check = fetch_current_price(fallback_price=None)
    live_px = live_check.price if (live_check.is_live and live_check.price > 0) else None
    history = get_history(live_price=live_px)
    today = date.today()

    # 예측 시점 결정
    last_day_of_month = monthrange(today.year, today.month)[1]
    days_left = last_day_of_month - today.day

    if days_left >= 8:
        # 이번달 예측: as_of = 전달 말일
        first_of_this_month = date(today.year, today.month, 1)
        as_of = (first_of_this_month - timedelta(days=1)).isoformat()
    else:
        # 다음달 예측: as_of = 어제
        as_of = (today - timedelta(days=1)).isoformat()

    try:
        snap = predict_as_of(
            history, as_of,
            capital_krw=capital,
            krw_hold_ratio=krw_hold,
            aggressiveness=aggressiveness,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # monitor() 호출
    mon_obj = None  # 아래 meas_* 루프에서 참조하므로 None으로 초기화
    try:
        history_fallback = get_history()
        fallback_btc = float(history_fallback[-1]["close"]) if history_fallback else None
        price_obj = fetch_current_price(fallback_price=fallback_btc)
        # 실시간 가격이 없으면 폴백 가격도 전달 (monitor가 latest close 대신 사용)
        live_price = price_obj.price if price_obj.price and price_obj.price > 0 else None

        mon_obj = monitor_prediction(snap, history, today.isoformat(), live_price=live_price)
        mon_data = {
            "elapsed":     mon_obj.elapsed_days,
            "total":       mon_obj.total_days,
            "progress":    round(mon_obj.progress_pct, 1),
            "containment": round(mon_obj.measured_containment_pct, 1),
            "status":      mon_obj.overall_status,
            "detail":      mon_obj.status_detail,
            "sigma_change": round(mon_obj.sigma_change_pct, 2),
            "box_shift":   round(mon_obj.box_center_shift_pct, 2),
            "cur_price":   mon_obj.current_price,
            "rev_ru":      mon_obj.forecast_revised_upper,
            "rev_rl":      mon_obj.forecast_revised_lower,
            "rev_u2":      mon_obj.forecast_upper_2s,
            "rev_l2":      mon_obj.forecast_lower_2s,
            "today":       today.isoformat(),
        }
    except Exception as e:
        import logging as _log
        _log.getLogger("api_server").warning("[dashboard] monitor() 실패: %s", e)
        history_fallback2 = get_history()
        fallback_price = float(history_fallback2[-1]["close"]) if history_fallback2 else snap.reference_price
        mon_data = {
            "elapsed": 0, "total": 31, "progress": 0, "containment": 100,
            "status": "PREDICTED", "detail": "예측 단계",
            "sigma_change": 0, "box_shift": 0, "cur_price": fallback_price,
            "rev_ru": snap.recommended_upper, "rev_rl": snap.recommended_lower,
            "rev_u2": snap.box_upper_2s, "rev_l2": snap.box_lower_2s,
            "today": today.isoformat(),
        }

    # snap 직렬화
    snap_data = {
        "as_of":          snap.as_of_date,
        "target":         snap.target_month,
        "ref":            snap.reference_price,
        "u1":             snap.box_upper_1s,
        "l1":             snap.box_lower_1s,
        "u2":             snap.box_upper_2s,
        "l2":             snap.box_lower_2s,
        "ru":             snap.recommended_upper,
        "rl":             snap.recommended_lower,
        "sigma":          snap.sigma_level_used,
        "range_pct":      round(snap.box_range_pct, 2),
        "monthly_sigma":  round(snap.monthly_sigma_pct, 2),
        "daily_sigma":    round(snap.daily_sigma * 100, 2),
        "confidence":     68.3,
        "gi_pct":         snap.buy_interval_pct,
        "gi_krw":         round(snap.buy_interval_pct / 100 * snap.reference_price) if snap.buy_interval_pct else 0,
        "bots":           snap.bot_count,
        "deployed":       snap.capital_deployed_krw,
        "reserve":        snap.krw_reserve_krw,
        "per_bot":        snap.capital_per_bot_krw,
        "volume":         snap.estimated_monthly_volume_krw,
        "reward":         snap.estimated_reward_krw,
        "capital":        snap.capital_krw,
    }

    # 대상 월 파싱
    ty, tm = map(int, snap.target_month.split("-"))
    total_target_days = monthrange(ty, tm)[1]

    # pre_* 배열: history_days일치 캔들 중 대상 월 시작 전 데이터
    target_month_start = f"{ty:04d}-{tm:02d}-01"
    pre_candles = [c for c in history if c["date"] < target_month_start]
    pre_candles = pre_candles[-history_days:]

    pre_x = [c["date"] for c in pre_candles]
    pre_o = [c["open"]  for c in pre_candles]
    pre_h = [c["high"]  for c in pre_candles]
    pre_l = [c["low"]   for c in pre_candles]
    pre_c = [c["close"] for c in pre_candles]

    # fut: 대상 월 모든 날짜
    pad2 = lambda n: str(n).zfill(2)
    fut = [f"{ty:04d}-{pad2(tm)}-{pad2(d)}" for d in range(1, total_target_days + 1)]

    # fut_rem: 오늘 이후 대상 월 날짜
    today_iso = today.isoformat()
    fut_rem = [d for d in fut if d >= today_iso]

    # glines: buy_interval_pct 기준 그리드 라인
    glines = []
    if snap.buy_interval_pct and snap.box_lower_1s > 0:
        price = snap.box_lower_1s
        step = snap.buy_interval_pct / 100
        while price <= snap.box_upper_1s * 1.01 and len(glines) < 80:
            glines.append(round(price))
            price *= (1 + step)

    # meas_*: measured_candles 분류
    meas_in  = {"x": [], "o": [], "h": [], "l": [], "c": []}
    meas_w   = {"x": [], "o": [], "h": [], "l": [], "c": []}
    meas_bu  = {"x": [], "o": [], "h": [], "l": [], "c": []}
    meas_bl  = {"x": [], "o": [], "h": [], "l": [], "c": []}

    # Build lookup dict for history by date
    hist_by_date = {c["date"]: c for c in history}

    zone_map = {"inner": meas_in, "warning": meas_w, "breach_upper": meas_bu, "breach_lower": meas_bl}
    if mon_obj is not None:
        for ds in mon_obj.measured_candles:
            bucket = zone_map.get(ds.zone)
            if bucket is None:
                continue
            candle = hist_by_date.get(ds.date)
            if candle is None:
                continue
            bucket["x"].append(ds.date)
            bucket["o"].append(candle["open"])
            bucket["h"].append(candle["high"])
            bucket["l"].append(candle["low"])
            bucket["c"].append(candle["close"])

    return {
        "snap":    snap_data,
        "mon":     mon_data,
        "pre_x":   pre_x,
        "pre_o":   pre_o,
        "pre_h":   pre_h,
        "pre_l":   pre_l,
        "pre_c":   pre_c,
        "fut":     fut,
        "fut_rem": fut_rem,
        "glines":  glines,
        "meas_in": meas_in,
        "meas_w":  meas_w,
        "meas_bu": meas_bu,
        "meas_bl": meas_bl,
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "date": date.today().isoformat()}


# ─── 분봉 데이터 파이프라인 (백그라운드 수집) ─────────────────────────────────
_minute_status = {
    "state": "idle",   # idle | collecting | done | error
    "market": None,
    "fetched": 0,
    "oldest": None,
    "newest": None,
    "error": None,
}


def _minute_collector():
    """서버 시작 시 백그라운드 스레드에서 분봉 증분 수집 (DB 비면 부트스트랩)."""
    log = logging.getLogger("api_server")
    _minute_status["state"] = "collecting"
    try:
        total = 0
        for market in minute_data.MARKETS:
            _minute_status["market"] = market

            def _progress(fetched, oldest, m=market):
                _minute_status["fetched"] = total + fetched
                _minute_status["oldest"] = oldest
                log.info("[minutes] %s 수집 중: %d개 (최고 %s)", m, total + fetched, oldest)

            # sync는 자동으로 bootstrap으로 폴백 (bootstrap에만 progress_cb 적용)
            conn_stats = minute_data.get_stats()
            if market in conn_stats:
                n = minute_data.sync(market)
            else:
                n = minute_data.bootstrap(market, progress_cb=_progress)
            total += n
            _minute_status["fetched"] = total
        stats = minute_data.get_stats()
        if stats:
            _minute_status["oldest"] = min(s["oldest"] for s in stats.values())
            _minute_status["newest"] = max(s["newest"] for s in stats.values())
        _minute_status["state"] = "done"
        log.info("[minutes] 수집 완료: %d행 신규", total)
    except Exception as e:
        _minute_status["state"] = "error"
        _minute_status["error"] = str(e)
        log.warning("[minutes] 수집 실패: %s", e)


@app.on_event("startup")
def _start_minute_collector():
    threading.Thread(target=_minute_collector, daemon=True, name="minute-collector").start()


@app.get("/api/minutes/status")
def minutes_status():
    return {"status": dict(_minute_status), "stats": minute_data.get_stats()}


@app.get("/api/minutes")
def minutes_api(
    market: str = Query("KRW-BTC"),
    unit: int = Query(5, ge=1, le=240),
    days: int = Query(7, ge=1, le=30),
):
    from datetime import datetime as _dt
    start = (_dt.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    candles = minute_data.get_candles(market, unit, start=start)
    return {"market": market, "unit": unit, "count": len(candles), "candles": candles}


@app.get("/api/export/dataset")
def export_dataset_api():
    """ML 학습용 데이터셋 ZIP 생성 후 다운로드.

    구성: daily_btc.csv + minute_*.csv(수집분) + monthly_labels.csv(지도학습 라벨) + meta.json
    """
    from fastapi.responses import FileResponse
    from utils.dataset_export import export_dataset

    try:
        meta = export_dataset()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터셋 생성 실패: {e}")
    fname = f"btc-grid-dataset_{date.today().isoformat()}.zip"
    return FileResponse(meta["zip_path"], media_type="application/zip", filename=fname)


if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
