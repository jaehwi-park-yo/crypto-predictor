# 5분봉 활용 강화 — 검증 및 적용 결과

박스권 예측·그리드 추정에 5분봉(일중) 데이터를 최대한 활용하기 위한 작업.
공용 모듈 `utils/intraday_vol.py` (as-of 재현, 분봉 부족 시 일봉 폴백) 신설.

## Tier 1 — 핵심 σ를 5m RV-EWMA로 승격 ✅ 적용
- **변경:** `prediction_service`의 기본 일간 σ를 일봉 close-to-close EWMA →
  **5분봉 일별 실현변동성(RV)의 EWMA**(span 20, 최근 30일)로 교체. 분봉 없으면 일봉 폴백.
- **ML 상호작용:** 라이브(`use_ml_sigma=True`)는 ML이 σ를 완전히 덮어쓰던 것을
  **ML × 5m = 50:50 블렌드**로 변경(`INTRADAY_SIGMA_ML_BLEND`). ML은 일봉 기반 학습이므로
  특징은 일봉 σ로 투입(학습 일관성 유지). ML 5m-기반 재학습은 6월말 데이터로 추후 진행.
- **검증(102개월, 2017-12~2026-05):** 월간 실현 σ 예측 MAE
  - 일봉 EWMA(기존): **5.41**
  - 5m RV-EWMA(신규): **4.84** → **−10.6%** (최근 2024+ 구간은 −15%)

## Tier 2 — 비대칭 밴드 5m 동적화 ❌ 미적용 (검증상 무효)
- 상승/하락 실현 반변동성 비율로 σ_up/σ_dn을 기울이는 안.
- **검증 결과:** 반변동성 비율 평균 1.01(범위 0.90~1.13)로 방향 신호 미약.
  밴드를 기울여도 containment 82.2%→82.0%(미세 악화). → `USE_INTRADAY_ASYMMETRY=False`.
- **부수 관찰:** 고정밴드 상방이탈 365 vs 하방이탈 187 — 상방 이탈이 2배.
  이는 BTC 상승 드리프트(현재 drift=0 가정) 때문이며, 별도 사안(드리프트/정적 비대칭 재보정).

## Tier 3 — 그리드 월 거래량 추정을 5m 실측 진동으로 보정 ✅ 적용
- **변경:** `grid_optimizer.optimize`가 `as_of·market`을 받아
  `oscillation_per_day`(박스 내 일평균 그리드 라인 진동)로 거래량 직접 산출.
  기존 휴리스틱(daily_range×fill_eff)은 미체결·단일인벤토리 제약을 무시해 과대.
- **검증(2026-04 BTC, 0.5%/53봇):** 휴리스틱 **26.6억** → 5m보정 **3.2억** = **8.3배 과대 교정**.
  사용자 실거래 관찰(앱 예측의 ~1/10)과 정합.

## config 플래그
```
USE_INTRADAY_SIGMA        = True   # Tier1
INTRADAY_RV_EWMA_SPAN     = 20
INTRADAY_RV_WINDOW_DAYS   = 30
INTRADAY_SIGMA_ML_BLEND   = 0.5    # ML on일 때 ML:5m 블렌드(0=ML단독,1=5m단독)
USE_INTRADAY_ASYMMETRY    = False  # Tier2 (무효)
USE_INTRADAY_VOLUME_CALIB = True   # Tier3
```

## 남은 사안 (후속)
- **index.html 클라이언트 P&L**: 화면의 거래량·월순익 표는 `BTC_COEF` 기반 독립 계산이라
  여전히 낙관적. 백엔드(`estimated_monthly_volume_krw`)는 Tier3로 현실화됐으나,
  클라이언트 표시값도 동일 보정 계수로 맞추는 작업 필요.
- **ML σ 재학습**: 6월말 데이터 확보 후 5m RV-EWMA를 기본 특징으로 한 재학습 시 블렌드
  비중 재조정 권장.
