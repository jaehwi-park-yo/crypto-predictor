"""
utils/dataset_export.py — ML 학습용 데이터셋 내보내기
======================================================
수집된 전체 데이터를 ZIP 하나로 묶어 내보낸다. 외부 ML 환경(샌드박스)에
업로드해 박스권 예측 F1 개선 실험에 바로 사용할 수 있는 구성.

ZIP 구성:
  daily_btc.csv            BTC/KRW 일봉 전체 (date,open,high,low,close,volume)
  daily_usdt.csv           USDT/KRW 일봉 전체 (date,open,high,low,close,volume)
  minute_KRW-BTC_5m.csv    BTC 5분봉 (수집된 경우)
  minute_KRW-USDT_5m.csv   USDT 5분봉 (수집된 경우)
  monthly_labels.csv       BTC 지도학습 라벨셋 — 월별 (특징 → 실현 결과)
  monthly_labels_usdt.csv  USDT 지도학습 라벨셋
  meta.json                스키마·행수·생성 정보

monthly_labels.csv 컬럼:
  as_of           예측 기준일 (전월 말일)
  target_month    예측 대상 월
  ref_price       기준가
  daily_sigma     예측에 사용된 일간 σ
  monthly_sigma_pct 월간 σ(%)
  box_u1/l1/u2/l2 예측 박스 (1σ/2σ)
  ret_prev_31d    직전 31일 수익률 (모멘텀 특징)
  realized_days   대상 월 실측 일수
  contain_1s_pct  종가가 1σ 박스 내인 일수 비율 (핵심 라벨)
  contain_2s_pct  종가가 2σ 박스 내인 일수 비율
  breach_up_days / breach_dn_days  2σ 이탈 일수
  max_up_dev_pct / max_dn_dev_pct  최대 이탈폭(%)
  realized_sigma_pct  대상 월 실현 변동성(%)
  label_ok        contain_1s_pct >= 68.3 여부 (이진 분류 라벨, F1 산출 기준)

실행:
  python -m utils.dataset_export                    # data/dataset_export.zip
  python -m utils.dataset_export --out my.zip
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("dataset_export")

ROOT = Path(__file__).parent.parent
DEFAULT_OUT = ROOT / "data" / "dataset_export.zip"


# ──────────────────────────────────────────────────────────────
# 개별 테이블 빌더
# ──────────────────────────────────────────────────────────────
def _csv_bytes(rows: List[Dict], fields: List[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _daily_csv(market: str = "BTC") -> Optional[bytes]:
    if market == "USDT":
        from utils.data_cache import get_usdt_history
        hist = get_usdt_history()
    else:
        from utils.data_cache import get_history
        hist = get_history()
    if not hist:
        return None
    return _csv_bytes(hist, ["date", "open", "high", "low", "close", "volume"])


def _minute_csv(market: str, unit: int = 5) -> Optional[bytes]:
    try:
        from utils import minute_data
        candles = minute_data.get_candles(market, unit=unit)
    except Exception as e:
        logger.warning("[내보내기] 분봉 조회 실패 (%s): %s", market, e)
        return None
    if not candles:
        return None
    fields = list(candles[0].keys())
    return _csv_bytes(candles, fields)


def _month_days(history: List[Dict], ym: str) -> List[Dict]:
    return [c for c in history if c["date"][:7] == ym]


def _realized_sigma_pct(candles: List[Dict]) -> float:
    closes = [float(c["close"]) for c in candles]
    if len(closes) < 5:
        return 0.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) * math.sqrt(len(closes)) * 100


def build_monthly_labels(min_lookback: int = 40) -> List[Dict]:
    """월별 (예측 특징 → 실현 결과) 라벨셋 생성.

    각 월 말일을 as_of로 predict_as_of를 호출해 당시 시점 기준 예측을 재현하고,
    대상 월의 실측 캔들과 비교해 containment 라벨을 계산한다.
    (as_of 이전 데이터만 사용 — 미래 정보 누설 없음)
    """
    from utils.data_cache import get_history
    from services.prediction_service import predict_as_of

    history = get_history()
    if len(history) < min_lookback + 28:
        return []

    # 히스토리에 존재하는 월 목록 (오름차순)
    months = sorted({c["date"][:7] for c in history})
    rows: List[Dict] = []

    for i in range(1, len(months)):
        target_ym = months[i]
        prev_ym = months[i - 1]
        # as_of = 전월 마지막 거래일
        prev_candles = _month_days(history, prev_ym)
        if not prev_candles:
            continue
        as_of = prev_candles[-1]["date"]
        # 전월 말일까지 누적 데이터가 충분한지
        upto = [c for c in history if c["date"] <= as_of]
        if len(upto) < min_lookback:
            continue

        try:
            snap = predict_as_of(history, as_of)
        except Exception:
            continue
        if snap.target_month != target_ym:
            continue

        actual = _month_days(history, target_ym)
        if len(actual) < 5:
            continue  # 실측이 너무 적으면 라벨 불가 (진행 중인 월 등)

        in1 = in2 = up = dn = 0
        max_up = max_dn = 0.0
        for c in actual:
            close = float(c["close"])
            if snap.box_lower_1s <= close <= snap.box_upper_1s:
                in1 += 1
            if snap.box_lower_2s <= close <= snap.box_upper_2s:
                in2 += 1
            if close > snap.box_upper_2s:
                up += 1
                max_up = max(max_up, (close - snap.box_upper_2s) / snap.box_upper_2s * 100)
            elif close < snap.box_lower_2s:
                dn += 1
                max_dn = max(max_dn, (snap.box_lower_2s - close) / snap.box_lower_2s * 100)

        n = len(actual)
        # 모멘텀 특징: as_of 직전 31일 수익률
        tail = upto[-32:]
        ret_31 = (float(tail[-1]["close"]) / float(tail[0]["close"]) - 1) * 100 if len(tail) >= 2 else 0.0
        contain_1s = in1 / n * 100

        rows.append({
            "as_of": as_of,
            "target_month": target_ym,
            "ref_price": round(snap.reference_price),
            "daily_sigma": round(snap.daily_sigma, 6),
            "monthly_sigma_pct": round(snap.monthly_sigma_pct, 3),
            "box_u1": round(snap.box_upper_1s),
            "box_l1": round(snap.box_lower_1s),
            "box_u2": round(snap.box_upper_2s),
            "box_l2": round(snap.box_lower_2s),
            "ret_prev_31d": round(ret_31, 3),
            "realized_days": n,
            "contain_1s_pct": round(contain_1s, 1),
            "contain_2s_pct": round(in2 / n * 100, 1),
            "breach_up_days": up,
            "breach_dn_days": dn,
            "max_up_dev_pct": round(max_up, 2),
            "max_dn_dev_pct": round(max_dn, 2),
            "realized_sigma_pct": round(_realized_sigma_pct(actual), 3),
            "label_ok": int(contain_1s >= 68.3),
        })
    return rows


LABEL_FIELDS = [
    "as_of", "target_month", "ref_price", "daily_sigma", "monthly_sigma_pct",
    "box_u1", "box_l1", "box_u2", "box_l2", "ret_prev_31d", "realized_days",
    "contain_1s_pct", "contain_2s_pct", "breach_up_days", "breach_dn_days",
    "max_up_dev_pct", "max_dn_dev_pct", "realized_sigma_pct", "label_ok",
]


def build_usdt_monthly_labels(min_lookback: int = 40) -> List[Dict]:
    """USDT/KRW 월별 라벨셋 — build_monthly_labels()와 동일 구조."""
    from utils.data_cache import get_usdt_history
    from services.prediction_service import predict_as_of

    history = get_usdt_history()
    if len(history) < min_lookback + 28:
        return []

    months = sorted({c["date"][:7] for c in history})
    rows: List[Dict] = []

    for i in range(1, len(months)):
        target_ym = months[i]
        prev_ym = months[i - 1]
        prev_candles = _month_days(history, prev_ym)
        if not prev_candles:
            continue
        as_of = prev_candles[-1]["date"]
        upto = [c for c in history if c["date"] <= as_of]
        if len(upto) < min_lookback:
            continue

        try:
            snap = predict_as_of(history, as_of)
        except Exception:
            continue
        if snap.target_month != target_ym:
            continue

        actual = _month_days(history, target_ym)
        if len(actual) < 5:
            continue

        in1 = in2 = up = dn = 0
        max_up = max_dn = 0.0
        for c in actual:
            close = float(c["close"])
            if snap.box_lower_1s <= close <= snap.box_upper_1s:
                in1 += 1
            if snap.box_lower_2s <= close <= snap.box_upper_2s:
                in2 += 1
            if close > snap.box_upper_2s:
                up += 1
                max_up = max(max_up, (close - snap.box_upper_2s) / snap.box_upper_2s * 100)
            elif close < snap.box_lower_2s:
                dn += 1
                max_dn = max(max_dn, (snap.box_lower_2s - close) / snap.box_lower_2s * 100)

        n = len(actual)
        tail = upto[-32:]
        ret_31 = (float(tail[-1]["close"]) / float(tail[0]["close"]) - 1) * 100 if len(tail) >= 2 else 0.0
        contain_1s = in1 / n * 100

        rows.append({
            "as_of": as_of,
            "target_month": target_ym,
            "ref_price": round(snap.reference_price, 4),
            "daily_sigma": round(snap.daily_sigma, 6),
            "monthly_sigma_pct": round(snap.monthly_sigma_pct, 3),
            "box_u1": round(snap.box_upper_1s, 4),
            "box_l1": round(snap.box_lower_1s, 4),
            "box_u2": round(snap.box_upper_2s, 4),
            "box_l2": round(snap.box_lower_2s, 4),
            "ret_prev_31d": round(ret_31, 3),
            "realized_days": n,
            "contain_1s_pct": round(contain_1s, 1),
            "contain_2s_pct": round(in2 / n * 100, 1),
            "breach_up_days": up,
            "breach_dn_days": dn,
            "max_up_dev_pct": round(max_up, 2),
            "max_dn_dev_pct": round(max_dn, 2),
            "realized_sigma_pct": round(_realized_sigma_pct(actual), 3),
            "label_ok": int(contain_1s >= 68.3),
        })
    return rows


# ──────────────────────────────────────────────────────────────
# 퍼블릭 API
# ──────────────────────────────────────────────────────────────
def export_dataset(out_path: Path | str = DEFAULT_OUT) -> Dict:
    """전체 데이터셋을 ZIP으로 내보내고 메타 정보를 반환."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    meta: Dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": {},
        "schema": {
            "monthly_labels.csv": "BTC 지도학습 라벨셋 — label_ok=1이면 1σ containment ≥ 68.3%",
            "monthly_labels_usdt.csv": "USDT 지도학습 라벨셋 — 동일 구조",
            "daily_btc.csv": "BTC/KRW 일봉",
            "daily_usdt.csv": "USDT/KRW 일봉",
            "minute_*.csv": "5분봉 (업비트, 수집된 범위)",
        },
        "notes": "as_of 이전 데이터만으로 예측을 재현했으므로 미래 정보 누설 없음. "
                 "진행 중인 월(실측 5일 미만)은 라벨에서 제외됨.",
    }

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        daily_btc = _daily_csv("BTC")
        if daily_btc:
            zf.writestr("daily_btc.csv", daily_btc)
            meta["files"]["daily_btc.csv"] = daily_btc.count(b"\n") - 1

        daily_usdt = _daily_csv("USDT")
        if daily_usdt:
            zf.writestr("daily_usdt.csv", daily_usdt)
            meta["files"]["daily_usdt.csv"] = daily_usdt.count(b"\n") - 1

        for market in ("KRW-BTC", "KRW-USDT"):
            mb = _minute_csv(market)
            if mb:
                name = f"minute_{market}_5m.csv"
                zf.writestr(name, mb)
                meta["files"][name] = mb.count(b"\n") - 1

        labels = build_monthly_labels()
        if labels:
            lb = _csv_bytes(labels, LABEL_FIELDS)
            zf.writestr("monthly_labels.csv", lb)
            meta["files"]["monthly_labels.csv"] = len(labels)

        usdt_labels = build_usdt_monthly_labels()
        if usdt_labels:
            ulb = _csv_bytes(usdt_labels, LABEL_FIELDS)
            zf.writestr("monthly_labels_usdt.csv", ulb)
            meta["files"]["monthly_labels_usdt.csv"] = len(usdt_labels)

        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    meta["zip_path"] = str(out_path)
    meta["zip_bytes"] = out_path.stat().st_size
    logger.info("[내보내기] 완료: %s (%d bytes, files=%s)",
                out_path, meta["zip_bytes"], meta["files"])
    return meta


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="ML 학습용 데이터셋 ZIP 내보내기")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="출력 ZIP 경로")
    args = ap.parse_args()
    m = export_dataset(args.out)
    print(json.dumps(m, ensure_ascii=False, indent=2))
