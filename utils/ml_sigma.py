"""
utils/ml_sigma.py — ML 기반 월간 σ 보정 (Ridge 회귀)
======================================================
Walk-forward OOS 평가 결과 (2017-12 ~ 2026-06):
  - 통계(EWMA σ):  MAE 기준 기준선
  - Ridge 보정:    walk_forward_eval() 실행 시 honest OOS 수치 산출
    (data/ml_sigma_model.json 의 oos_eval 필드에 저장)

  ※ 배포 모델(ml_sigma_model.json)은 전체 데이터로 재학습한 최종 모델.
     retrain() 내부에서 walk-forward OOS 평가를 먼저 실행하고 결과를 모델에 함께 저장.

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
_MIN_TRAIN = 40        # 최소 학습 샘플 (월) — walk-forward 훈련 시작점
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
# Walk-forward OOS 평가 (honest — 미래 데이터 사용 금지)
# ──────────────────────────────────────────────────────────────
def walk_forward_eval(
    labels: List[Dict],
    history: List[Dict],
    min_train: int = _MIN_TRAIN,
    alpha: float = _ALPHA,
) -> Dict:
    """
    Expanding-window walk-forward OOS 평가.

    각 시점 i(i >= min_train)에서:
      - 훈련: 라벨 0 ~ i-1 (i-1개월까지의 과거 데이터만)
      - 예측: 라벨 i (미래 1개월)
    → 캐시된 단일 모델을 과거 전체에 적용하는 in-sample 평가와 다름.

    반환:
      n_oos            OOS 예측 개수
      mae_ml / mae_stat  ML vs 통계 모델 MAE (실현 σ 대비)
      rmse_ml / rmse_stat
      mae_improvement_pct  MAE 개선율 (양수=ML이 더 좋음)
      bias_ml          OOS 예측 평균 편향 (ML 예측 − 실현값 평균)
    """
    # 모든 라벨에 대해 특징 벡터 사전 계산 (각 as_of 이전 데이터만 사용)
    all_feats: List[Optional[List[float]]] = []
    all_targets: List[float] = []
    all_stat_sigma: List[float] = []
    for r in labels:
        x = build_features(history, r["as_of"],
                           float(r["daily_sigma"]), float(r["monthly_sigma_pct"]),
                           float(r["ret_prev_31d"]))
        all_feats.append(x)
        all_targets.append(float(r["realized_sigma_pct"]))
        all_stat_sigma.append(float(r["monthly_sigma_pct"]))

    # 특징 추출에 성공한 행의 인덱스
    valid_idx = [i for i, x in enumerate(all_feats) if x is not None]
    if len(valid_idx) < min_train + 5:
        return {"status": "insufficient_data", "n_valid": len(valid_idx), "n_total": len(labels)}

    oos_preds: List[float] = []
    oos_targets: List[float] = []
    oos_stat: List[float] = []

    for rank, idx in enumerate(valid_idx):
        if rank < min_train:
            continue  # 훈련 집합이 min_train 미만이면 건너뜀

        # 이 시점 이전 유효 라벨들만 훈련에 사용
        train_idx = valid_idx[:rank]
        X_tr = np.array([all_feats[j] for j in train_idx])
        y_tr = np.array([all_targets[j] for j in train_idx])
        m = _ridge_fit(X_tr, y_tr, alpha)

        pred = _ridge_predict(m, all_feats[idx])
        pred = max(_SIGMA_FLOOR, min(pred, _SIGMA_CAP))
        oos_preds.append(pred)
        oos_targets.append(all_targets[idx])
        oos_stat.append(all_stat_sigma[idx])

    if not oos_preds:
        return {"status": "no_oos_predictions"}

    preds = np.array(oos_preds)
    targets = np.array(oos_targets)
    stats = np.array(oos_stat)

    mae_ml   = float(np.mean(np.abs(preds - targets)))
    mae_stat = float(np.mean(np.abs(stats - targets)))
    rmse_ml  = float(np.sqrt(np.mean((preds - targets) ** 2)))
    rmse_stat = float(np.sqrt(np.mean((stats - targets) ** 2)))
    bias_ml  = float(np.mean(preds - targets))

    result = {
        "status": "ok",
        "n_oos": len(oos_preds),
        "n_train_min": min_train,
        "mae_ml":   round(mae_ml,   3),
        "mae_stat": round(mae_stat, 3),
        "mae_improvement_pct": round((1 - mae_ml / mae_stat) * 100, 1) if mae_stat > 0 else 0.0,
        "rmse_ml":   round(rmse_ml,   3),
        "rmse_stat": round(rmse_stat, 3),
        "bias_ml":   round(bias_ml,   3),
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }
    logger.info(
        "[ml_sigma] walk-forward OOS(%d개월): ML MAE=%.3f%% / 통계 MAE=%.3f%% / 개선=%.1f%%",
        result["n_oos"], mae_ml, mae_stat, result["mae_improvement_pct"],
    )
    return result


# ──────────────────────────────────────────────────────────────
# 학습 / 예측
# ──────────────────────────────────────────────────────────────
def retrain(skip_oos_eval: bool = False) -> Optional[Dict]:
    """
    월별 라벨셋으로 재학습 후 모델 저장.

    순서:
      1. Walk-forward OOS 평가 (honest 성능 측정) — skip_oos_eval=True 시 생략
      2. 전체 데이터로 최종 모델 학습 (배포용)
      3. OOS 평가 결과를 모델 JSON에 함께 저장

    샘플 부족 시 None 반환.
    """
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

    # 1. Walk-forward OOS 평가 (배포 전 honest 성능 측정)
    oos_result: Dict = {}
    if not skip_oos_eval:
        logger.info("[ml_sigma] walk-forward OOS 평가 시작 (샘플=%d)...", len(labels))
        oos_result = walk_forward_eval(labels, history)
        if oos_result.get("status") == "ok":
            imp = oos_result["mae_improvement_pct"]
            logger.info(
                "[ml_sigma] OOS 평가 완료: ML이 통계 대비 MAE %.1f%% %s",
                abs(imp), "개선" if imp > 0 else "악화",
            )
        else:
            logger.warning("[ml_sigma] OOS 평가 실패: %s", oos_result)

    # 2. 전체 데이터로 최종 모델 학습 (배포용)
    model = _ridge_fit(np.array(X_rows), np.array(y_rows))
    model["oos_eval"] = oos_result  # OOS 성능 지표 함께 저장

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


def get_oos_eval() -> Optional[Dict]:
    """캐시된 모델의 walk-forward OOS 평가 결과 반환. 없으면 None."""
    model = _load_model()
    if model is None:
        return None
    return model.get("oos_eval") or None


def predict_monthly_sigma_pct(history: List[Dict], as_of: str,
                              daily_sigma: float, monthly_sigma_pct: float,
                              ret_prev_31d: float) -> Optional[float]:
    """
    학습된 모델로 보정된 월간 σ(%) 반환. 모델/특징 없으면 None.

    배포 모델은 전체 데이터로 학습됐으므로 in-sample 예측임.
    OOS 성능은 model["oos_eval"] (walk_forward_eval 결과) 참조.
    """
    model = _load_model()
    if model is None:
        return None
    x = build_features(history, as_of, daily_sigma, monthly_sigma_pct, ret_prev_31d)
    if x is None:
        return None
    pred = _ridge_predict(model, x)
    return max(_SIGMA_FLOOR, min(pred, _SIGMA_CAP))


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="ML σ 모델 재학습 및 OOS 평가")
    ap.add_argument("--skip-oos", action="store_true", help="walk-forward OOS 평가 생략 (빠른 재학습)")
    ap.add_argument("--oos-only", action="store_true", help="OOS 평가만 실행 (모델 저장 안 함)")
    args = ap.parse_args()

    if args.oos_only:
        from utils.dataset_export import build_monthly_labels
        from utils.data_cache import get_history
        labels = build_monthly_labels()
        history = get_history()
        result = walk_forward_eval(labels, history)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        m = retrain(skip_oos_eval=args.skip_oos)
        if m:
            out = {k: v for k, v in m.items() if k != "w"}  # 가중치 제외 출력
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            print("학습 실패 (데이터 부족)")
