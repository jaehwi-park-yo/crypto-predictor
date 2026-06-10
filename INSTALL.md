# 📦 BTC Grid Prediction System — 설치 및 실행 안내

> **버전**: Beta v0.3 | **대상 거래소**: 빗썸(Bithumb) | **지원 종목**: BTC/KRW, USDT/KRW

---

## ⚠️ 투자 면책 고지

이 앱의 모든 수치는 과거 데이터 기반 **통계적 추정치**이며 미래 수익을 보장하지 않습니다.
그리드 봇 운용에 따른 손실 책임은 전적으로 사용자 본인에게 있습니다.
**투자 판단에 앞서 반드시 소액 테스트와 충분한 위험 검토를 권장합니다.**

---

## 1. 시스템 요구 사항

| 항목 | 최소 사양 | 권장 |
|---|---|---|
| OS | Windows 10, macOS 12, Ubuntu 20.04 | 최신 버전 |
| Python | 3.10 이상 | 3.11 또는 3.12 |
| RAM | 512 MB | 2 GB 이상 |
| 디스크 | 200 MB | 1 GB 이상 |
| 포트 | 8000 (API), 8501 (Streamlit) | 방화벽 미차단 |
| 인터넷 | 선택 (없어도 캐시로 동작) | 있으면 실시간 가격 수신 |

---

## 2. Python 설치 (없는 경우)

### Windows
1. [https://www.python.org/downloads/](https://www.python.org/downloads/) 접속
2. **Download Python 3.12.x** 클릭
3. 설치 시 **"Add Python to PATH"** 반드시 체크 ✔

### macOS
```bash
# Homebrew가 없는 경우 먼저 설치
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12
```

### Ubuntu / Debian
```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv -y
```

---

## 3. 앱 설치

### 3-1. 압축 파일 해제
`btc-grid-prediction-beta.zip`을 원하는 폴더에 압축 해제하세요.

```
📁 btc-grid-prediction-beta/
├── index.html          ← 대시보드 (브라우저로 열기)
├── start.bat           ← Windows 실행 스크립트
├── start.sh            ← macOS/Linux 실행 스크립트
├── requirements.txt    ← Python 의존성 목록
├── api_server.py       ← 실시간 API 서버
├── main.py             ← CLI 예측 엔진
├── config.py           ← 전략 파라미터 설정
├── data/
│   └── btc_history.json  ← 사전 로드된 BTC 히스토리 캐시
└── ...
```

### 3-2. 최초 실행 (자동 환경 설정)

**Windows** — `start.bat` 파일을 더블클릭

**macOS / Linux** — 터미널에서 실행
```bash
# 압축 해제 폴더로 이동
cd btc-grid-prediction-beta

# 실행 권한 부여 (최초 1회)
chmod +x start.sh

# 실행
./start.sh
```

실행 시 다음이 자동으로 진행됩니다:

```
[1/4] 가상환경 생성 (.venv) ···
[2/4] 의존성 설치 (requests, fastapi, plotly 등) ···
[3/4] API 서버 시작 (http://localhost:8000) ···
[4/4] 대시보드 브라우저 열기 (index.html) ···
```

> 첫 실행 시 의존성 다운로드에 1~2분이 소요될 수 있습니다.

---

## 4. 실행 방법 비교

| 방법 | 특징 | 명령 |
|---|---|---|
| **start.bat / start.sh** | 원클릭, 권장 | 더블클릭 또는 `./start.sh` |
| 수동 API 서버 | 디버깅, 로그 확인 | `python api_server.py` |
| Streamlit 대시보드 | 추가 분석 화면 | `streamlit run gui/app.py` |
| CLI 예측 엔진 | 텍스트 리포트 | `python main.py` |
| 백테스터 | 전략 검증 | `python backtest.py` |

---

## 5. 대시보드 사용법

### 5-1. 탭 구성

| 탭 | 내용 |
|---|---|
| 📊 포트폴리오 | 4전략 자본 배분·월간 P&L 합계 (슬라이더 연동) |
| ₿ BTC | BTC/KRW 예측 차트·박스권·그리드 상세 |
| 💵 USDT | USDT/KRW 차트·간격 시뮬레이터·존별 상세 |
| 📡 모니터 | 실시간 포지션 현황·박스권 이탈 경보 |
| 💬 피드백 | 피드백 작성 |

### 5-2. 파라미터 조정 (왼쪽 사이드바)

| 항목 | 설명 |
|---|---|
| **투입 자본** | 총 운용 자본 (원 단위) |
| **직전월 거래량** | 전월 계정 전체 거래량 입력 → 당월 적용 리워드율 자동 결정 (0.003%~0.02%) |
| **거래량 목표** | 0~200억. 직접입력·±버튼·슬라이더 연동. 목표 입력 시 아래 3개 변수를 자동 추천 |
| **BTC 비중** | 40~70% (나머지는 USDT에 배분) |
| **내부존(1σ) 비율** | 내부존 배분 비율 (외부존은 자동 계산) |
| **KRW 예비금** | 총 자본 중 현금 보유 비율 |

### 5-3. USDT 그리드 간격 시뮬레이터

1. USDT 탭에서 내부존/외부존 매수·매도 간격(1~5원)을 스테퍼로 조정
2. 조정 즉시 **존별 그리드 상세 테이블**이 업데이트됩니다
3. 최적 간격 확인 후 **💾 포트폴리오에 적용** 버튼 클릭
4. 포트폴리오 탭의 4전략 상세·월간 P&L 합계에 반영됩니다

---

## 6. 실시간 가격 수신

API 서버(`start.sh` / `start.bat`)가 실행 중이면 대시보드가 **60초 주기로 자동 갱신**됩니다.

- 🟢 **실시간**: 업비트/빗썸 API에서 현재가 수신 중
- 🟡 **폴백**: 인터넷 미연결 시 사전 캐시 데이터 사용 (기능은 정상)

> 외부 API가 일시 차단되거나 요청 한도를 초과하면 자동으로 캐시로 전환됩니다.

---

## 7. CLI 예측 엔진 사용법

```bash
# 기본 실행 (40M 자본, 익월 예측)
python main.py

# 자본 변경
python main.py --capital 60000000

# 대상 월 지정
python main.py --month 2026-07

# 공격성 조절 (conservative / balanced / aggressive)
python main.py --aggressiveness aggressive
```

리포트는 `reports/YYYY-MM_report.txt`에 저장됩니다.

---

## 8. 자주 묻는 질문 (FAQ)

**Q. 브라우저를 열었는데 차트가 안 보여요**
→ `start.bat` / `start.sh`로 API 서버를 먼저 실행하세요. 또는 파일을 직접 열어도 차트는 표시되며, 실시간 가격만 폴백 데이터로 표시됩니다.

**Q. 포트 8000이 이미 사용 중이라고 나와요**
→ 이미 API 서버가 실행 중입니다. `start.sh`는 자동으로 기존 프로세스를 종료하고 재시작합니다. Windows에서는 기존 "API Server" 창을 닫고 다시 실행하세요.

**Q. 패키지 설치 중 에러가 나요**
→ Python 버전을 확인하세요(`python --version`). 3.10 미만이면 업그레이드가 필요합니다. 회사 PC 등 네트워크가 제한된 경우 IT 담당자에게 pip 프록시 설정을 문의하세요.

**Q. 가격이 계속 폴백으로 나와요**
→ 업비트·빗썸 API가 간헐적으로 차단될 수 있습니다. 일반 가정 인터넷에서는 대부분 실시간 수신됩니다. 회사 인터넷은 차단될 수 있습니다.

**Q. 예측 박스권이 실제와 많이 달라요**
→ 본 예측은 과거 변동성 기반 통계 모델(1σ/2σ)로 익월 박스권을 추정합니다. 급등락·뉴스 이벤트는 반영되지 않으며, 참고 지표로만 활용하시기 바랍니다.

**Q. Streamlit 대시보드를 열었더니 pandas 에러가 나요**
→ 가상환경이 활성화된 상태에서 `pip install pandas`를 실행하거나, `start.sh` / `start.bat`를 통해 환경을 재설치하세요.

---

## 9. 문제 발생 시 로그 확인

```bash
# API 서버 로그 (터미널 출력)
python api_server.py

# 서버 상태 확인
curl http://localhost:8000/api/health
```

---

## 10. 디렉토리 구조 (참고)

```
btc-grid-prediction-beta/
├── index.html              대시보드 (브라우저 단일 파일)
├── start.bat               Windows 실행
├── start.sh                macOS/Linux 실행
├── api_server.py           FastAPI 실시간 API 서버
├── main.py                 오케스트레이터 (CLI)
├── backtest.py             백테스터
├── config.py               전략 파라미터
├── requirements.txt        의존성 목록
├── agents/
│   ├── box_predictor.py    박스권 예측 (1σ/2σ)
│   ├── grid_optimizer.py   그리드 최적화
│   ├── backtester.py       비대칭 백테스터
│   └── usdt_grid.py        USDT 특화 그리드
├── services/
│   ├── prediction_service.py  예측 파이프라인
│   └── prediction_monitor.py  실시간 모니터링
├── utils/
│   ├── live_data.py        실시간 가격 수신
│   ├── historical_data.py  히스토리 수집
│   └── data_cache.py       디스크 캐시
└── data/
    └── btc_history.json    사전 로드 캐시 (오프라인 폴백용)
```

---

*BTC Grid Prediction System Beta v0.3 — 2026.06*
