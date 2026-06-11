"""
utils/ml_sigma.py — ML 기반 월간 σ 보정 (Ridge 회귀)
======================================================
103개월 walk-forward 백테스트 결과 (2017-12 ~ 2026-06):
  - 통계(EWMA σ):  박스 적중 45/63 (71%), 평균 containment 78.8%
  - Ridge 보정:    박스 적중 49/63 (78%), 평균 containment 83.1%
  - 평균 박스폭 ×1.15이지만 상수 확대와 달리 17/63개월은 오히려 좁힘
    (필요할 때만 넓히는 조건부 보정 — 같은 적중률을 k=1.3 상수로 얻으면 폭 ×1.30)

구조:
  - 순수 파이썬 closed-form Ridge (sklearn 불필요)
  - 학습 데이터: dataset_export.build_monthly_labels() (ML 미적용 통계 라벨)
  - 특징 12개: 일간σ, 월간σ, 31일 수익률, |수익률|, σ30/σ90 비율,
               90/180일 σ 투영, 5분봉 일중 실현변동성(RV30), RV/σ 괴리,
               김치프리미엄(수준·30일 변화), 원/달러 환율 30일 σ (v2)
  - 모델 파라미터는 data/ml_sigma_model.json 에 캐시 (재학습: retrain())

사용:
  predict_monthly_sigma_pct(history, as_of) -> Optional[float]
    학습된 모델이 있으면 보정 σ(%) 반환, 없거나 특징 부족 시 None
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger("ml_sigma")

ROOT = Path(__file__).parent.parent
MODEL_PATH = ROOT / "data" / "ml_sigma_model.json"

_ALPHA = 1.0           # Ridge 정규화 강도
_MIN_TRAIN = 40        # 최소 학습 샘플 (월)
_SIGMA_FLOOR = 2.0     # 예측 σ 하한 (%)
_SIGMA_CAP = 60.0      # 예측 σ 상한 (%)

# 메모리 캐시 (프로세스당 1회 로드)
_model_cache: Optional[Dict] = None


# ──────────────────────────────────────────────────────────────
# 특징 추출
# ──────────────────────────────────────────────────────────────
def _log_returns(closes: List[float]) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    arr = arr[arr > 0]
    if arr.size < 2:
        return np.array([])
    return np.diff(np.log(arr))


def _minute_rv30(as_of: str) -> Optional[float]:
    """as_of 직전 30일의 5분봉 일중 실현변동성 평균. 분봉 없으면 None."""
    try:
        from utils import minute_data
        # as_of 35일 전부터 조회 (여유분)
        start_dt = datetime.strptime(as_of[:10], "%Y-%m-%d")
        from datetime import timedelta
        start = (start_dt - timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%S")
        end = as_of[:10] + "T23:59:59"
        candles = minute_data.get_candles("KRW-BTC", unit=5, start=start, end=end)
        if len(candles) < 1000:
            return None
        closes = np.array([c["close"] for c in candles], dtype=float)
        rets = np.diff(np.log(closes[closes > 0]))
        days = [c["ts"][:10] for c in candles][1:]
        # 일별 sqrt(sum r^2)
        rv_by_day: Dict[str, float] = {}
        for d, r in zip(days, rets):
            rv_by_day[d] = rv_by_day.get(d, 0.0) + r * r
        rvs = [math.sqrt(v) for _, v in sorted(rv_by_day.items())[-30:]]
        return float(np.mean(rvs)) if rvs else None
    except Exception as e:
        logger.debug("[ml_sigma] 분봉 RV 계산 실패: %s", e)
        return None


def build_features(history: List[Dict], as_of: str,
                   daily_sigma: float, monthly_sigma_pct: float,
                   ret_prev_31d: float) -> Optional[List[float]]:
    """예측 시점 특징 벡터. 데이터 부족 시 None."""
    upto = [c for c in history if c["date"][:10] <= as_of]
    closes = [float(c["close"]) for c in upto]
    if len(closes) < 91:
        return None
    lr = _log_returns(closes)
    s30 = float(np.std(lr[-30:], ddof=1))
    s90 = float(np.std(lr[-90:], ddof=1))
    s180 = float(np.std(lr[-180:], ddof=1)) if lr.size >= 180 else s90

    rv30 = _minute_rv30(as_of)
    if rv30 is None:
        rv30 = daily_sigma  # 분봉 없으면 일봉 σ로 대체 (rv_ratio=1)

    feats = [
        daily_sigma,
        monthly_sigma_pct,
        ret_prev_31d,
        abs(ret_prev_31d),
        s30 / s90 if s90 > 0 else 1.0,
        s90 * math.sqrt(30) * 100,
        s180 * math.sqrt(30) * 100,
        rv30 * math.sqrt(30) * 100,
        rv30 / daily_sigma if daily_sigma > 0 else 1.0,
    ]

    # 글로벌 시장 특징 (v2): 김치프리미엄 수준·30일 변화, 환율 30일 σ
    # 데이터 없으면 중립값 (김프 0, FX σ는 BTC 월σ의 1/10 근사)
    kimp_pct, kimp_chg, fx_ms = 0.0, 0.0, monthly_sigma_pct * 0.1
    try:
        from utils.fx_data import kimp_features_asof
        kf = kimp_features_asof(as_of)
        if kf:
            kimp_pct = kf["kimp_pct"]
            kimp_chg = kf["kimp_chg_30d"]
            fx_ms = kf["fx_sigma30_monthly_pct"]
    except Exception as e:
        logger.debug("[ml_sigma] 김프/FX 특징 생략: %s", e)
    feats += [kimp_pct, kimp_chg, fx_ms]
    return feats


# ──────────────────────────────────────────────────────────────
# Ridge (closed form)
# ──────────────────────────────────────────────────────────────
def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = _ALPHA) -> Dict:
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Xs = (X - mu) / sd
    d = Xs.shape[1]
    A = Xs.T @ Xs + alpha * np.eye(d)
    w = np.linalg.solve(A, Xs.T @ (y - y.mean()))
    return {
        "mu": mu.tolist(), "sd": sd.tolist(),
        "w": w.tolist(), "b": float(y.mean()),
        "n_train": int(len(y)),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
    }


def _ridge_predict(model: Dict, x: List[float]) -> float:
    mu = np.asarray(model["mu"]); sd = np.asarray(model["sd"])
    w = np.asarray(model["w"])
    # 구버전 모델(9특징) 호환: 모델 차원에 맞게 특징 절단
    xv = np.asarray(x, dtype=float)[: mu.size]
    xs = (xv - mu) / sd
    return float(xs @ w + model["b"])


# ──────────────────────────────────────────────────────────────
# 학습 / 예측
# ──────────────────────────────────────────────────────────────
def retrain() -> Optional[Dict]:
    """월별 라벨셋으로 재학습 후 모델 저장. 샘플 부족 시 None."""
    from utils.dataset_export import build_monthly_labels
    from utils.data_cache import get_history

    labels = build_monthly_labels()
    if len(labels) < _MIN_TRAIN:
        logger.warning("[ml_sigma] 학습 샘플 부족 (%d < %d) — 모델 미생성", len(labels), _MIN_TRAIN)
        return None

    history = get_history()
    X_rows, y_rows = [], []
    for r in labels:
        x = build_features(history, r["as_of"],
                           float(r["daily_sigma"]), float(r["monthly_sigma_pct"]),
                           float(r["ret_prev_31d"]))
        if x is None:
            continue
        X_rows.append(x)
        y_rows.append(float(r["realized_sigma_pct"]))

    if len(X_rows) < _MIN_TRAIN:
        logger.warning("[ml_sigma] 특징 생성 후 샘플 부족 (%d) — 모델 미생성", len(X_rows))
        return None

    model = _ridge_fit(np.array(X_rows), np.array(y_rows))
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(json.dumps(model), encoding="utf-8")
    global _model_cache
    _model_cache = model
    logger.info("[ml_sigma] 재학습 완료: %d개월 학습 → %s", model["n_train"], MODEL_PATH.name)
    return model


def _load_model() -> Optional[Dict]:
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    if not MODEL_PATH.exists():
        return None
    try:
        _model_cache = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
        return _model_cache
    except Exception as e:
        logger.warning("[ml_sigma] 모델 로드 실패: %s", e)
        return None


def predict_monthly_sigma_pct(history: List[Dict], as_of: str,
                              daily_sigma: float, monthly_sigma_pct: float,
                              ret_prev_31d: float) -> Optional[float]:
    """학습된 모델로 보정된 월간 σ(%) 반환. 모델/특징 없으면 None."""
    model = _load_model()
    if model is None:
        return None
    x = build_features(history, as_of, daily_sigma, monthly_sigma_pct, ret_prev_31d)
    if x is None:
        return None
    pred = _ridge_predict(model, x)
    return max(_SIGMA_FLOOR, min(pred, _SIGMA_CAP))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    m = retrain()
    print(json.dumps(m, indent=2) if m else "학습 실패 (데이터 부족)")
