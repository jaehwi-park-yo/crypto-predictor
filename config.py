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
# 박스 중심에서 멀어질수록 주문 크기 × (1 + α×레벨)
# ──────────────────────────────────────────────────────────
PROGRESSIVE_SIZING = True
PROGRESSIVE_ALPHA  = 0.15  # 레벨당 15% 가중 (중심+3레벨 = 1.45×기본)

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

# Retry settings
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0

# Logging
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# Report output directory
REPORT_DIR = "reports"
