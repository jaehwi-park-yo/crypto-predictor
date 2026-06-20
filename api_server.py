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
    print("fastapi/uvicorn 미설치. 설치: pip install -r requirements.txt")
    print("(또는 start.bat 실행 시 자동 설치됩니다)")
    sys.exit(1)

from calendar import monthrange
import threading
from utils import minute_data
from utils.dataset_seed import restore_from_seed
from utils.live_data import fetch_current_price, fetch_usdt_price
from utils.data_cache import get_history, get_usdt_history
from services.prediction_service import predict_as_of
from services.prediction_monitor import monitor as monitor_prediction

app = FastAPI(title="BTC Grid Prediction API", version="1.0.0")

# CORS — index.html (file://) + 로컬 개발 서버 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
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
            use_ml_sigma=True,
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
            use_ml_sigma=True,
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
            "rev_u1":      mon_obj.forecast_upper_1s,
            "rev_l1":      mon_obj.forecast_lower_1s,
            "rev_u2":      mon_obj.forecast_upper_2s,
            "rev_l2":      mon_obj.forecast_lower_2s,
            "today":       today.isoformat(),
            # 데드존/그리드락 카운터
            "consec_dn":   mon_obj.consec_breach_lower,
            "consec_up":   mon_obj.consec_breach_upper,
            "reset_rec":   mon_obj.reset_recommended,
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
            "rev_u1": snap.box_upper_1s_asym or snap.box_upper_1s,
            "rev_l1": snap.box_lower_1s_asym or snap.box_lower_1s,
            "rev_u2": snap.box_upper_2s_asym or snap.box_upper_2s,
            "rev_l2": snap.box_lower_2s_asym or snap.box_lower_2s,
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
        # 비대칭 σ 밴드
        "sigma_up":       snap.sigma_up,
        "sigma_dn":       snap.sigma_dn,
        "sigma_up_2":     snap.sigma_up_2,
        "sigma_dn_2":     snap.sigma_dn_2,
        "u1a":            snap.box_upper_1s_asym,
        "l1a":            snap.box_lower_1s_asym,
        "u2a":            snap.box_upper_2s_asym,
        "l2a":            snap.box_lower_2s_asym,
        # 듀얼레이어 자본 배분
        "layer_a":        snap.layer_a_krw,
        "layer_b":        snap.layer_b_krw,
        "layer_c":        snap.layer_c_krw,
        "dca_levels":     snap.dca_levels or [],
        "reset_days":     snap.dead_zone_reset_days,
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


# 최적 간격 추정 결과 월별 캐시 (key: f"{market}_{target_month}_{deployed}_{prev_vol}")
_intervals_cache: Dict[str, dict] = {}


@app.get("/api/intervals")
def get_intervals(
    market: Literal["KRW-BTC", "KRW-USDT"] = Query(default="KRW-BTC"),
    capital: float = Query(default=40_000_000, ge=1_000_000),
    btc_ratio: float = Query(default=0.60, ge=0.0, le=1.0),
    krw_hold: float = Query(default=0.30, ge=0.0, le=0.7),
    months: int = Query(default=3, ge=1, le=12),
    prev_vol: float = Query(default=1e9, ge=0.0),
    target_month: Optional[str] = Query(default=None, description="YYYY-MM (기본: 이번달)"),
    refresh: bool = Query(default=False, description="캐시 무시하고 재계산"),
):
    """1분봉 기반 부스트/일반 모드 최적 매수·매도 간격 추정.

    매월 1회 산출 후 캐시 — target_month가 같으면 재계산하지 않는다(refresh=true 예외).
    box_lower/upper는 prediction_service.predict_as_of로 자동 산출.
    """
    from utils.interval_optimizer import optimize_intervals

    today = date.today()
    if target_month is None:
        target_month = today.strftime("%Y-%m")

    # as_of = 대상 월 전달 말일 (해당 시점까지 데이터로 예측·시뮬)
    try:
        tm = date.fromisoformat(f"{target_month}-01")
    except ValueError:
        raise HTTPException(status_code=400, detail="target_month는 YYYY-MM 형식이어야 합니다")
    as_of = (tm - timedelta(days=1)).isoformat()

    # 투입 자본 = capital × (1-krw_hold) × 자산비중
    deployed = capital * (1 - krw_hold)
    deployed *= (1 - btc_ratio) if market.endswith("USDT") else btc_ratio

    cache_key = f"{market}_{target_month}_{int(deployed)}_{int(prev_vol)}_{months}"
    if not refresh and cache_key in _intervals_cache:
        cached = dict(_intervals_cache[cache_key])
        cached["cached"] = True
        return cached

    try:
        result = optimize_intervals(
            market=market,
            as_of=as_of,
            deployed_krw=deployed,
            lookback_months=months,
            prev_vol_monthly=prev_vol,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"간격 추정 실패: {e}")

    result["target_month"] = target_month
    result["deployed_krw"] = round(deployed)
    result["cached"] = False
    _intervals_cache[cache_key] = dict(result)
    return result


@app.get("/api/ml/status")
def ml_status():
    """ML σ 모델 상태 및 walk-forward OOS 평가 결과 반환."""
    from utils.ml_sigma import _load_model, MODEL_PATH
    model = _load_model()
    if model is None:
        return {"model_exists": False, "message": "모델 미학습 — retrain() 필요"}
    oos = model.get("oos_eval") or {}
    return {
        "model_exists": True,
        "n_train": model.get("n_train"),
        "trained_at": model.get("trained_at"),
        "oos_eval": oos,
        "note": (
            "oos_eval은 expanding-window walk-forward 결과 (honest OOS). "
            "mae_improvement_pct > 0이면 ML이 통계 모델보다 정확."
        ),
    }


@app.post("/api/ml/retrain")
def ml_retrain(skip_oos: bool = False):
    """ML σ 모델 재학습 (OOS 평가 포함). skip_oos=true 시 OOS 평가 생략."""
    import threading
    result: dict = {}

    def _do():
        from utils.ml_sigma import retrain, _model_cache
        import utils.ml_sigma as _ms
        _ms._model_cache = None  # 캐시 초기화
        m = retrain(skip_oos_eval=skip_oos)
        result["ok"] = m is not None
        if m:
            result["n_train"] = m.get("n_train")
            result["oos_eval"] = m.get("oos_eval", {})

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=120)
    return result if result else {"ok": False, "message": "타임아웃 (120초) — 백그라운드 실행 중"}


@app.get("/api/global")
def global_market():
    """글로벌 시장 컨텍스트: 원/달러 환율 · 달러 BTC · 김치프리미엄 (캐시 기반)."""
    from utils.fx_data import get_fx_history, get_btc_usd_history, kimchi_premium_series
    fx = get_fx_history()
    usd = get_btc_usd_history()
    kimp = kimchi_premium_series()
    return {
        "fx_days": len(fx),
        "btc_usd_days": len(usd),
        "fx_latest": fx[-1] if fx else None,
        "btc_usd_latest": {"date": usd[-1]["date"], "close": usd[-1]["close"]} if usd else None,
        "kimp_latest": kimp[-1] if kimp else None,
        "kimp_tail_30d": kimp[-30:] if kimp else [],
    }


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
        from config import MINUTE_COLLECT_UNIT as _unit
    except Exception:
        _unit = 1
    try:
        total = 0
        for market in minute_data.MARKETS:
            _minute_status["market"] = market

            def _progress(fetched, oldest, m=market):
                _minute_status["fetched"] = total + fetched
                _minute_status["oldest"] = oldest
                log.info("[minutes] %s %dm 수집 중: %d개 (최고 %s)", m, _unit, total + fetched, oldest)

            # sync는 자동으로 bootstrap으로 폴백 (bootstrap에만 progress_cb 적용)
            if minute_data.has_data(market, _unit):
                n = minute_data.sync(market, unit=_unit)
            else:
                n = minute_data.bootstrap(market, unit=_unit, progress_cb=_progress)
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


_1m_status: Dict[str, object] = {
    "state": "idle",   # idle | collecting | done | disabled | error
    "market": None, "fetched": 0, "oldest": None, "newest": None, "error": None,
}


def _1m_collector():
    """
    기동 시 백그라운드에서 최근 MINUTE_1M_ROLLING_DAYS일치 1분봉을 수집·유지.
    - 최초: bootstrap(unit=1, max_days=롤링기간)
    - 이후: sync(unit=1) 증분
    - 완료 후: MINUTE_1M_PURGE_OLD=True 면 롤링 윈도우 초과 1m 자동 삭제
    - API 실패(403 등)는 조용히 기록하고 종료 — 5m 기반 폴백 유지
    """
    log = logging.getLogger("api_server")
    try:
        from config import MINUTE_1M_COLLECT, MINUTE_1M_ROLLING_DAYS, MINUTE_1M_PURGE_OLD
    except Exception:
        MINUTE_1M_COLLECT, MINUTE_1M_ROLLING_DAYS, MINUTE_1M_PURGE_OLD = True, 90, True

    if not MINUTE_1M_COLLECT:
        _1m_status["state"] = "disabled"
        log.info("[1m] MINUTE_1M_COLLECT=False — 1분봉 수집 비활성")
        return

    _1m_status["state"] = "collecting"
    log.info("[1m] 1분봉 롤링 수집 시작 (최근 %d일)", MINUTE_1M_ROLLING_DAYS)
    total = 0
    try:
        for market in minute_data.MARKETS:
            _1m_status["market"] = market

            def _prog(fetched, oldest, m=market):
                _1m_status["fetched"] = total + fetched
                _1m_status["oldest"] = oldest
                log.info("[1m] %s 수집 중: %d개 (최고 %s)", m, total + fetched, oldest)

            if minute_data.has_sufficient_history(market, unit=1, min_days=MINUTE_1M_ROLLING_DAYS):
                n = minute_data.sync(market, unit=1)
                log.info("[1m] %s 증분 sync: %d행 추가", market, n)
            else:
                n = minute_data.bootstrap(
                    market, unit=1,
                    max_days=MINUTE_1M_ROLLING_DAYS,
                    progress_cb=_prog,
                )
                log.info("[1m] %s bootstrap 완료: %d행", market, n)
            total += n
            _1m_status["fetched"] = total

            # 롤링 윈도우 초과분 삭제
            if MINUTE_1M_PURGE_OLD:
                from datetime import datetime as _dt, timedelta as _td
                cutoff = (_dt.now() - _td(days=MINUTE_1M_ROLLING_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
                deleted = minute_data.purge_before(market, unit=1, before_ts=cutoff)
                if deleted:
                    log.info("[1m] %s 롤링 정리: %d행 삭제 (<%s)", market, deleted, cutoff[:10])

        stats = minute_data.get_stats()
        s1m = {k: v for k, v in stats.items() if v.get("unit") == 1}
        if s1m:
            _1m_status["oldest"] = min(v["oldest"] for v in s1m.values())
            _1m_status["newest"] = max(v["newest"] for v in s1m.values())
        _1m_status["state"] = "done"
        log.info("[1m] 완료: 신규 %d행 / DB %s",
                 total,
                 " | ".join(f"{k}: {v['count']}행" for k, v in s1m.items()) if s1m else "0행")
    except Exception as e:
        _1m_status["state"] = "error"
        _1m_status["error"] = str(e)
        log.warning("[1m] 수집 실패 (5m 기반 폴백 유지): %s", e)


def _train_ml_sigma():
    """ML σ 보정 모델 학습 — 일봉 캐시가 준비된 후 백그라운드에서 실행."""
    import logging as _lg
    log = _lg.getLogger("api_server")
    try:
        from utils.ml_sigma import retrain, MODEL_PATH
        if MODEL_PATH.exists():
            log.info("[startup] ML σ 모델 존재 — 재학습으로 갱신")
        m = retrain()
        if m:
            log.info("[startup] ML σ 모델 학습 완료 (%d개월)", m["n_train"])
        else:
            log.info("[startup] ML σ 학습 샘플 부족 — 통계 σ만 사용")
    except Exception as e:
        log.warning("[startup] ML σ 학습 실패 (통계 σ만 사용): %s", e)


def _sync_global_then_train():
    """환율·달러 BTC 동기화 후 ML 학습 — 김프/FX 특징이 학습에 포함되도록 순서 보장."""
    import logging as _lg
    log = _lg.getLogger("api_server")
    try:
        from utils.fx_data import sync_all
        stats = sync_all()
        log.info("[startup] 글로벌 데이터 동기화: 환율 %d일, 달러BTC %d일",
                 stats["fx_days"], stats["btc_usd_days"])
    except Exception as e:
        log.warning("[startup] 글로벌 데이터 동기화 실패 (특징 중립값 사용): %s", e)
    _train_ml_sigma()


def _preload_usdt_history():
    """USDT/KRW 일봉 캐시 사전 로드 — 서버 기동 직후 백그라운드에서 실행."""
    import logging as _lg
    try:
        data = get_usdt_history()
        _lg.getLogger("api_server").info("[startup] USDT 일봉 %d건 캐시 준비 완료", len(data))
    except Exception as e:
        _lg.getLogger("api_server").warning("[startup] USDT 일봉 로드 실패: %s", e)


@app.on_event("startup")
def _start_minute_collector():
    log = logging.getLogger("api_server")
    # 시드 복원은 분봉 수집보다 먼저, 동기적으로 실행
    restore_from_seed()

    # 1분봉 데이터가 부족하면 서버 기동 전에 먼저 동기 수집
    try:
        from config import MINUTE_1M_COLLECT, MINUTE_1M_ROLLING_DAYS
    except Exception:
        MINUTE_1M_COLLECT, MINUTE_1M_ROLLING_DAYS = True, 90

    need_bootstrap = MINUTE_1M_COLLECT and any(
        not minute_data.has_sufficient_history(m, 1, MINUTE_1M_ROLLING_DAYS)
        for m in minute_data.MARKETS
    )
    if need_bootstrap:
        log.info("[startup] 1분봉 데이터 부족 — 수집 후 서버를 기동합니다 (최대 10분 소요)")
        _1m_collector()  # 동기 실행: 완료 전까지 서버 기동 대기
        log.info("[startup] 1분봉 수집 완료 — 서버 기동")
    else:
        # 충분한 데이터 있으면 백그라운드 증분 sync만 실행
        threading.Thread(target=_1m_collector, daemon=True, name="1m-collector").start()

    threading.Thread(target=_minute_collector,    daemon=True, name="minute-collector").start()
    threading.Thread(target=_preload_usdt_history, daemon=True, name="usdt-preload").start()
    threading.Thread(target=_sync_global_then_train, daemon=True, name="global-sync-ml-train").start()


@app.get("/api/minutes/status")
def minutes_status():
    return {
        "status_5m": dict(_minute_status),
        "status_1m": dict(_1m_status),
        "stats": minute_data.get_stats(),
    }


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


@app.get("/api/update/check")
def update_check_api(force: bool = Query(False)):
    """GitHub 최신 버전 확인. {current, latest, available}"""
    from utils.update_check import check_update
    try:
        return check_update(force=force)
    except Exception as e:
        return {"current": None, "latest": None, "available": False, "error": str(e)}


@app.post("/api/update/apply")
def update_apply_api():
    """최신 버전 다운로드·덮어쓰기 후 서버 자동 재시작."""
    from utils.update_check import apply_update, restart_server
    try:
        result = apply_update()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"업데이트 적용 실패: {e}")
    restart_server(delay=1.0)
    return {"status": "ok", **result,
            "message": "업데이트 적용 완료 — 서버를 재시작합니다."}


@app.post("/api/shutdown")
def shutdown_server():
    """캐시 플러시 후 서버를 안전하게 종료합니다."""
    import os, signal, threading, subprocess

    def _do_shutdown():
        import time
        time.sleep(0.8)  # 응답이 클라이언트에 도달할 시간 확보
        log.info("[shutdown] 서버 종료 신호 전송")
        # "API Server" 제목의 cmd 창 종료 (start.bat 이 붙인 이름)
        try:
            subprocess.run(
                ["taskkill", "/F", "/FI", "WINDOWTITLE eq API Server*"],
                capture_output=True, check=False
            )
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"status": "ok", "message": "서버를 종료합니다."}


if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
