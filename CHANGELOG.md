# Changelog

## v0.4.0 (2026-06-11)
### 추가
- 글로벌 시장 데이터 통합: 원/달러 환율(Frankfurter), 달러 BTC(Binance), 김치프리미엄
- 데이터셋 내보내기에 fx_usdkrw.csv / daily_btc_usd.csv / kimchi_premium.csv 포함
- /api/global 엔드포인트 (환율·달러BTC·김프 현황 조회)
- USDT σ에 환율 EWMA σ 30% 블렌딩 (표본 부족 보완)
- ML σ 보정 특징 9→12개 (김프 수준/30일 변화·환율 30일 σ)
- 데이터셋 시드를 분봉 포함 완전체(20MB)로 교체 — 최초 기동 10초 만에 자동 복원
- patch.bat / patch.sh — 덮어씌우기 원클릭 패치 스크립트
- make_patch.py — 배포자용 패치 ZIP 생성 스크립트

### 변경
- 2σ 밴드 walk-forward 재보정: BTC 1.8/2.0, USDT 1.5/1.65 (독립 상수, 1σ×2 아님)
- 시드 복원 스킵 조건 강화: 일봉+분봉 모두 존재할 때만 스킵
- 프런트엔드 USDT 2σ 표기 ±4.4% → ≈±3.3% 갱신

## v0.3.0 (2026-06-10)
### 추가
- 비대칭 σ 밴드 (BTC: ↑1.1/↓1.2, USDT: ↑1.0/↓1.1)
- 듀얼레이어 자본 배분 (Layer A 그리드 / Layer B DCA / Layer C 역추세)
- ML Ridge σ 보정 (walk-forward 백테스트: 적중 71%→78%)
- Progressive Sizing / Asymmetric TP (ASYM_TP_MULT=2.0)
- 탭 네비게이션 충돌 수정 (duplicate const badge)

## v0.1-0.2
- 초기 구현: BTC/USDT 박스권 예측, 그리드 최적화, 리워드 계산기
