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

from utils.live_data import fetch_current_price, fetch_usdt_price
from utils.data_cache import get_history
from services.prediction_service import predict_as_of

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


@app.get("/api/health")
def health():
    return {"status": "ok", "date": date.today().isoformat()}


if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
