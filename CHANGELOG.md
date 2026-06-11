# Changelog

## v0.4.3 (2026-06-11)
### 추가
- 대시보드 왼쪽 하단에 **서버 종료 버튼** 추가 (`⏻ 서버 종료`)
  - 클릭 → 확인 다이얼로그 → `/api/shutdown` POST → 0.8초 후 SIGTERM으로 안전 종료
  - 종료 중/완료 피드백 표시 (버튼 비활성화 + 메시지 변경)
- `api_server.py`: `POST /api/shutdown` 엔드포인트 추가

## v0.4.2 (2026-06-11)
### 변경
- Windows 전용으로 정리: start.sh / patch.sh 제거, 설치 문서(INSTALL/README/PDF)에서
  macOS·Linux 안내 삭제 — 배포 대상을 Windows로 한정해 패키지 경량화

## v0.4.1 (2026-06-11)
### 개선
- 모니터/BTC 차트: 수정 2σ 상·하단 라인 추가 + 전체 수정 밴드에 호버 수치 표시
- 수정 예측 vs 원본 비교표 확장: 1σ/2σ 상·하단 + Δ(변화율) 열, 상향 빨강/하향 파랑
- 수정 예측 밴드를 비대칭 σ 기준으로 통일 (기존: 대칭 2σ)
- /api/dashboard 응답에 rev_u1/rev_l1 (수정 1σ 밴드) 추가

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
