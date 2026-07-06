# config.py - Global configuration & constants

CAPITAL_KRW = 40_000_000  # Initial capital (variable - reinvested)
FEE_RATE = 0.0004          # Bithumb 0.04%
EXCHANGE = "bithumb"
SYMBOL = "BTC_KRW"

# Reward tiers (previous month volume → reward rate)
REWARD_TIERS = [
    (100_000_000_000, 0.0002),   # 1,000억+ → 0.02%
    (10_000_000_000,  0.00018),  # 100억+  → 0.018%
    (1_000_000_000,   0.00015),  # 10억+   → 0.015%
    (100_000_000,     0.00008),  # 1억+    → 0.008%
]
MAX_REWARD_KRW = 3_000_000      # Max 3M KRW/month

MAX_BOTS = 1000

# Sigma levels for box boundary
SIGMA_LEVELS = [1.0, 2.0]  # 레거시 참고용 (실제 사용은 아래 비대칭 값)

# ──────────────────────────────────────────────────────────
# 비대칭 σ 밴드 (상방/하방 독립 배수)
# ──────────────────────────────────────────────────────────
# BTC: 로그수익률 왜도=-0.64 (하방 fat tail), 초과첨도=15.17
#   walk-forward 백테스트(103개월): σ_up=1.1/σ_dn=1.2 → ok 45→47/63 (+4%)
#   하방이탈 31일 감소 (180→149일), 박스폭 +3.7%p (허용 범위)
SIGMA_UP_BTC = 1.1    # 상방 σ 배수
SIGMA_DN_BTC = 1.2    # 하방 σ 배수 (하방 fat tail 보완)

# USDT: 왜도=+0.896 (상방 fat tail — KRW 절하 방향 쏠림)
#   σ_up=1.0/σ_dn=1.1 → 하방이탈 9일 감소, ok 동일 유지
SIGMA_UP_USDT = 1.0   # 상방 σ 배수 (보수적 — 상단 돌파는 드묾)
SIGMA_DN_USDT = 1.1   # 하방 σ 배수 (KRW 강세 국면 대비)

# 2σ 외부 밴드 배수 — 1σ 배수와 독립 (단순 ×2는 박스가 과도하게 넓어짐)
# walk-forward 스윕 (BTC 100개월 / USDT 19개월, 2026-06):
#   BTC  2.2/2.4 → ok 85/100, 폭 116%  |  1.8/2.0 → ok 77/100, 폭 87%, cont 91.7%
#   USDT 2.0/2.2 → ok 18/19,  폭 17.2% |  1.5/1.65 → ok 17/19, 폭 12.6%, 하방이탈 0일 유지
# → Layer C(역추세) 가동 빈도·자본효율 개선을 위해 축소 채택
SIGMA_UP_2_BTC  = 1.8
SIGMA_DN_2_BTC  = 2.0
SIGMA_UP_2_USDT = 1.5
SIGMA_DN_2_USDT = 1.65

# Risk management
UPSIDE_BREAKOUT_ACTION = "hold"              # Do nothing on upside
DOWNSIDE_BREAKOUT_ACTION = "partial_stop"    # Partial stop on downside
PARTIAL_STOP_RATIO = 0.3                     # 30% position stop on downside

# ──────────────────────────────────────────────────────────
# 듀얼레이어 자본 배분 (Layer A: 1σ 그리드 / Layer B: DCA 예비 / Layer C: 2σ 외부)
# ──────────────────────────────────────────────────────────
# BTC: A=60% 그리드 운용, B=30% 하단 DCA 예비, C=10% 2σ 외부 역추세
# USDT: A=80%, B=20% (변동성 낮아 외부 배치 불필요)
LAYER_A_RATIO_BTC  = 0.60   # 1σ 내부 그리드 운용 자본
LAYER_B_RATIO_BTC  = 0.30   # 하단 이탈 DCA 예비 현금
LAYER_C_RATIO_BTC  = 0.10   # 2σ 외부 역추세 진입 (극단 이탈 시)

LAYER_A_RATIO_USDT = 0.80
LAYER_B_RATIO_USDT = 0.20
LAYER_C_RATIO_USDT = 0.00

# Layer B DCA 트리거: 1σ 하단 대비 낙폭 (%, 3단계)
DCA_TRIGGER_PCT = [-5.0, -10.0, -15.0]   # 하단 -5%, -10%, -15%
DCA_TRIGGER_PCT_USDT = [-1.5, -3.0, -5.0]
DCA_SIZE_EACH_PCT = 0.33   # Layer B의 33%씩 투입 (3회 = 100%)

# 하방 데드존 재설정 트리거 (연속 이탈일 초과 시 레인지 재설정 권고)
DEAD_ZONE_RESET_DAYS = 8   # 8일 연속 하단 이탈 → 재설정 권고

# MTM 서킷브레이커 (2026-07 백테스트: reports/mtm_circuit_backtest.py, 103개월 walk-forward)
# 가상 그리드 미실현 손실(MTM)이 투입자본의 θ를 넘으면 "신규 매수 정지" 권고.
# 보유분 익절 매도는 계속, MTM이 θ×RESUME 이내로 회복되면 재개(히스테리시스).
# 근거: 평균 손익은 θ에 둔감(월 ±2만, 노이즈)하나 꼬리 위험이 극적으로 감소 —
#   최악월 −222만→−102만, 최악 드로다운 −279만→−116만 (θ=2%), 거래량 −24%.
#   과열 사전필터는 데이터 기각(docs/box_model_review_202607.md) → 사후 대응이 정답.
MTM_CIRCUIT_THRESHOLD = 0.02   # 투입자본 대비 2% 미실현 손실 시 발동
MTM_CIRCUIT_RESUME = 0.5       # θ의 50%(=자본 1%) 이내 회복 시 매수 재개

# KRW holding ratio: Layer A 기준으로 계산 (기존 호환)
KRW_HOLD_RATIO = 1.0 - LAYER_A_RATIO_BTC  # = 0.40 (Layer B+C 합산)

# --- 그리드/수수료 파생 상수 (오케스트레이터 판단 반영) ---
ROUND_TRIP_FEE = 2 * FEE_RATE                 # 왕복 수수료 0.08%
FEE_MARGIN_MULT = 3.0                         # 그리드 간격 >= 왕복수수료 × 3
MIN_GRID_INTERVAL_PCT = FEE_MARGIN_MULT * ROUND_TRIP_FEE * 100   # ≈ 0.24%
GRID_INTERVAL_CANDIDATES_PCT = [0.3, 0.5, 1.0]

# 그리드 공격성 다이얼 (거래량 vs 수수료 절충) — 사용자 선택: 균형
GRID_AGGRESSIVENESS = "balanced"        # "conservative" | "balanced" | "aggressive"
AGGRESSIVENESS_INTERVAL_PCT = {
    "conservative": 1.0,                # 넓은 간격 · 수수료 절약 · 안정
    "balanced": 0.5,                    # 매매수익·리워드 균형 (기본)
    "aggressive": 0.3,                  # 좁은 간격 · 거래량/리워드 극대화
}

# 거래량/회전율 추정 가정 (실데이터로 보정 예정)
TRADING_DAYS_PER_MONTH = 30                   # 코인 24/7 → 30일
GRID_FILL_EFFICIENCY = 0.5                    # 진동 중 실제 왕복 체결 비율
DAILY_RANGE_SIGMA_MULT = 1.5                  # 일중 고저 범위 ≈ 1.5σ 근사
COMFORTABLE_DAILY_TURNOVER = 10.0             # 목표구간 자동선정용 일일회전율 상한

# 박스권 매매 지평
HORIZON_DAYS = 30                             # 익월 1개월 예측 지평
PREDICTION_DRIFT = 0.0                        # 박스권 매매 = 평균회귀 가정 → drift 0

# ──────────────────────────────────────────────────────────
# 자동 업데이트 (GitHub 저장소 VERSION 비교 → 원클릭 적용)
# ──────────────────────────────────────────────────────────
GITHUB_REPO = "jaehwi-park-yo/crypto-predictor"
UPDATE_BRANCH = "main"

# API endpoints
BITHUMB_BASE_URL = "https://api.bithumb.com"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
FEAR_GREED_URL = "https://api.alternative.me/fng/"

# ──────────────────────────────────────────────────────────
# 비대칭 그리드 설정 (매수간격 ≠ 매도간격)
# ──────────────────────────────────────────────────────────
ASYMMETRIC_GRID = True           # True = 비대칭 활성화
GRID_BUY_INTERVAL_PCT  = 0.5    # 매수 간격: 체결 빈도 기준
GRID_SELL_INTERVAL_PCT = 1.0    # 매도 간격: 수익 크기 결정 (2× 보수 수익)

# ──────────────────────────────────────────────────────────
# USDT-KRW 전략 설정 (1원 단위 초단기 그리드)
# ──────────────────────────────────────────────────────────
USDT_ENABLED = True
USDT_REFERENCE_PRICE_KRW = 1_400.0   # USDT/KRW 참고가 (설정값, 수집 시 갱신)
USDT_DAILY_RANGE_KRW     = 8.0       # 일중 USDT 변동폭 ±(KRW) 참고치
USDT_BUY_INTERVAL_KRW    = 1.0       # 매수 간격 (1원)
USDT_SELL_INTERVAL_KRW   = 3.0   # 매도 3원: 분석상 수익/리워드 최적 균형 (연 ~43%)

# 리워드 캡 도달 후 봇 중단 기준 (거래량 기준)
# 최상위 티어(0.02%) 기준: 300만원 리워드 = 150억원 거래량
REWARD_CAP_VOLUME_KRW = 15_000_000_000   # 150억: 이 이상이면 추가 리워드 없음
REWARD_CAP_STOP_BOTS  = True             # True = 캡 도달 시 봇 중단 권고

# ──────────────────────────────────────────────────────────
# 복합전략 자본 배분 (BTC conservative + USDT aggressive)
# ──────────────────────────────────────────────────────────
COMPOSITE_BTC_RATIO  = 0.6   # BTC 배분 비중 (60%)
COMPOSITE_USDT_RATIO = 0.4   # USDT 배분 비중 (40%)
COMPOSITE_BTC_AGGRESSIVENESS  = "conservative"
COMPOSITE_USDT_AGGRESSIVENESS = "aggressive"

# ──────────────────────────────────────────────────────────
# 하방 이탈 시 현금 배치 전략
# ──────────────────────────────────────────────────────────
CASH_DEPLOY_ON_BREAKOUT = True      # True = 하방 이탈 시 KRW 예비금 일부 배치
CASH_DEPLOY_RATIO       = 0.5       # KRW 예비금 중 배치 비율 (기본 50%)
CASH_DEPLOY_GRID_MULT   = 2.0       # 회복 그리드 간격 배수 (평소 간격 × 2)
CASH_DEPLOY_SIGMA_LEVEL = 2.0       # 회복 박스 σ 레벨 (넓게)

# ──────────────────────────────────────────────────────────
# 누진 포지션 크기 (Progressive Sizing)
# ──────────────────────────────────────────────────────────
# walk-forward 백테스트(105개월, BTC 일봉) 결과:
#   대칭 progressive (α=0.15): 누적 +2,291만
#   균등 (α=0.0):              누적 +2,771만  (+479만, +21%)
# → BTC는 상방 편중(하반부 체류 44.9%)으로 중심부 체결 빈도가 높음.
#   대칭 progressive는 중심부 자본을 경계부로 빼내 오히려 손해.
#   균등 배분이 최적 — PROGRESSIVE_SIZING=False 로 비활성화.
PROGRESSIVE_SIZING = False
PROGRESSIVE_ALPHA  = 0.0   # 균등 배분 (대칭 progressive 비활성)

# ──────────────────────────────────────────────────────────
# USDT 하방 집중 배분 (Bottom-Heavy Progressive Sizing)
# 봇 인덱스 0(상단) → N-1(하단)으로 선형 증가:
#   weight_i = 1 + USDT_BOTTOM_ALPHA × i / (N-1)
#   → 하단 봇이 상단 봇보다 (1+α)배 더 큰 자본 보유
#   → 하락 시 더 큰 매수/매도 → 거래량·수익 집중
# α=0: 균등 배분 / α=0.5: 하단 1.5× / α=1.0: 하단 2× / α=1.5: 하단 2.5×
# ──────────────────────────────────────────────────────────
USDT_BOTTOM_ALPHA = 0.5   # 기본 중간 집중 (하단 봇 1.5× 자본)

# 비대칭 TP (Asymmetric Take-Profit)
# 매도 목표가 = 매수가 × (1 + ASYM_TP_MULT × 간격) — 홀드 더 길게
ASYM_TP_ENABLED = True
ASYM_TP_MULT    = 2.0   # 기본 간격의 2배 위에서 매도 (수익 확대, 체결 빈도 감소)

# ──────────────────────────────────────────────────────────
# 분봉 데이터 소스 (업비트 공개 API 우선)
# ──────────────────────────────────────────────────────────
UPBIT_BASE_URL  = "https://api.upbit.com"
UPBIT_MARKET_BTC  = "KRW-BTC"
UPBIT_MARKET_USDT = "KRW-USDT"

# ──────────────────────────────────────────────────────────
# 글로벌 시장 데이터 (원/달러 환율 · 달러 BTC · 김치프리미엄)
# ──────────────────────────────────────────────────────────
# USDT/KRW는 환율 앵커 자산 → FX 변동성과 김프를 예측에 반영
FRANKFURTER_BASE_URL = "https://api.frankfurter.app"   # ECB 기준환율 (무료·키 불필요)
ERAPI_FX_URL = "https://open.er-api.com/v6/latest/USD" # 환율 폴백 (당일 시세)
BINANCE_BASE_URL = "https://api.binance.com"           # BTC/USDT 일봉 (무료·키 불필요)

# USDT σ 추정 시 FX 변동성 블렌딩 가중치 (0=미사용)
#   σ_blend = (1-w)·σ_usdt + w·σ_fx — USDT/KRW 분봉/일봉 표본 부족 보완
FX_SIGMA_BLEND_WEIGHT = 0.3

# ──────────────────────────────────────────────────────────
# 5분봉 일중 변동성 활용 (utils.intraday_vol)
# ──────────────────────────────────────────────────────────
# Tier1: 일별 실현변동성(RV)의 EWMA를 기본 일간 σ로 사용 (일봉 close-to-close 대체)
#   백테스트(2017-12~2026-05, 102개월): 월간 실현 σ 예측 MAE 일봉EWMA 대비 −10.6%
USE_INTRADAY_SIGMA = True
INTRADAY_RV_EWMA_SPAN = 20      # 일별 RV 계열 EWMA span
INTRADAY_RV_WINDOW_DAYS = 30    # EWMA에 쓰는 최근 일수
# ML σ 보정이 켜진 경우 ML 예측과 5m RV-EWMA σ의 블렌드 가중 (0=ML단독, 1=5m단독)
#   ML 모델은 일봉 기반 학습이므로, 5m 기반 재학습 전까지 검증된 5m 신호를 블렌드로 반영
INTRADAY_SIGMA_ML_BLEND = 0.5
# Tier2: 상승/하락 실현 반변동성 비율로 비대칭 밴드 동적화 — 검증 결과 효과 없어 비활성.
#   (5m 반변동성 비율 평균 1.01·범위 0.90~1.13으로 방향 신호 미약, containment 82.2%→82.0%)
USE_INTRADAY_ASYMMETRY = False
INTRADAY_ASYM_MAX_TILT = 0.15
# Tier3: 실측 진동으로 그리드 월 거래량 추정 보정 (휴리스틱 과대추정 교정)
USE_INTRADAY_VOLUME_CALIB = True

# ──────────────────────────────────────────────────────────
# 5분봉 거래량 과소측정 보정 계수
# ──────────────────────────────────────────────────────────
# 단일인벤토리 5m 시뮬은 캔들 내부 왕복을 흡수해 거래량을 과소측정.
# 실측(2026-03~06, 90일 1m·5m 비교):
#   로그수익률 기반 그리드 교차 비율 1m/5m = 2.11 (gi=0.2~1.0% 전구간 일치)
#   실현변동성 비율 = 1.03 → 미세구조 노이즈 무시 가능, 1m 신뢰도 확인
#   BTC 일평균 총이동거리: 49.1%/일 (3개월 평균, 4월 43.8%~6월 64.5%)
# BTC_COEF·USDT_EFF 양쪽에 동일 계수 적용. index.html VOL_RES_COEF와 동일값 유지.
VOLUME_RESOLUTION_COEF = 2.1   # 5m 과소측정 보정 (실측 2.11, index.html VOL_RES_COEF와 동일값 유지)
# 기동 시 백그라운드로 수집할 분봉 단위. 5m 유지.
MINUTE_COLLECT_UNIT = 5
DERIVE_DAILY_FROM_MINUTE = False

# ──────────────────────────────────────────────────────────
# 단일인벤토리 시뮬 거래량 보정계수 (interval_optimizer 전용)
# ──────────────────────────────────────────────────────────
# 단일인벤토리 시뮬은 "매수라인 → +m라인 상승 시 매도" 규칙이라 횡보장에서
# 재고가 잠겨 실거래 대비 거래량을 과소측정한다. 실제 빗썸 그리드 봇은 더
# 빈번히 왕복을 완료한다.
# 실측 보정 (2026-05, 사용자 실거래): 매수 2만원(≈0.017%)/매도 0.08%, 자본 ~37M,
#   BTC+USDT 합산 실제 10억 달성. 동일 파라미터 모델 추정 ~6.0억 → 계수 ≈ 1.67.
# → 1.6 적용 (단일 기준점 기반 잠정값). 운영 체결 로그로 정밀 재보정 예정((B) 검증루프).
VOLUME_SIM_CALIB = 1.6

# ──────────────────────────────────────────────────────────
# 실측 pnl율 앵커 (그리드 순익 / 거래대금, 수수료 차감·리워드 별도)
# ──────────────────────────────────────────────────────────
# 2026-06 실거래 3-포인트 캘리브레이션 (동일 전략: BTC 매수 0.2%/매도 0.3%):
#   6/18~24: 92,952원 / 1.0335억 = 0.0899%   (6/24 종일 누락분 병합 정정)
#   6/25~29: 75,678원 / 0.8793억 = 0.0861%
#   6/30   :  7,777원 / 0.0952억 = 0.0817%
#   거래량가중(6/18~30): 176,407원 / 2.0080억 = 0.0879%
# → 모델 순익 추정의 sanity-check 앵커. 리워드는 별도(멤버십 부여비율 추정).
# 주의: 이 값은 0.2%/0.3% 촘촘 전략 기준. 간격을 넓히면(일반모드) 상승.
GRID_NET_RATE_OBSERVED = 0.00088   # ≈0.088% (거래대금 대비 그리드 순익, 리워드 별도)
# 월말 효과: 월 마지막날은 거래량 급감(주간 일평균의 ~55%)·왕복당 마진↑ →
#   pnl율은 주간평균 대비 약 −7%. 월간 거래량 추정 시 말일 디스카운트 반영 권장.
MONTH_END_VOLUME_DISCOUNT = 0.55

# ──────────────────────────────────────────────────────────
# 1분봉 롤링 수집 (거래량 추정·간격 최적화용)
# ──────────────────────────────────────────────────────────
# 기동 시 별도 스레드로 최근 MINUTE_1M_ROLLING_DAYS일치 1m 분봉을 수집.
# 5m 수집과 독립 병렬 실행 — 업비트 API 경합 없음 (같은 엔드포인트, 별도 호출).
# DB 크기 관리: MINUTE_1M_PURGE_OLD=True 면 롤링 윈도우 초과분을 자동 삭제.
#   90일 × 2마켓 ≈ 26만행 ≈ 16MB — 실용적.
# 이 환경(원격)은 업비트 403 차단 → 수집 시도 후 조용히 실패 (5m 폴백 유지).
MINUTE_1M_COLLECT = True           # 1m 수집 on/off
MINUTE_1M_ROLLING_DAYS = 90        # 보유 기간(일). 오래된 것 자동 삭제.
MINUTE_1M_PURGE_OLD = True         # 롤링 윈도우 초과 1m 자동 삭제

# Retry settings
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0

# Logging
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# Report output directory
REPORT_DIR = "reports"
