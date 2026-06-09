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
SIGMA_LEVELS = [1.0, 2.0]  # 1σ ~68%, 2σ ~95%

# Risk management
UPSIDE_BREAKOUT_ACTION = "hold"              # Do nothing on upside
DOWNSIDE_BREAKOUT_ACTION = "partial_stop"    # Partial stop on downside
PARTIAL_STOP_RATIO = 0.3                     # 30% position stop on downside

# KRW holding ratio (to be discussed - placeholder)
KRW_HOLD_RATIO = 0.3  # 30% in KRW cash by default

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
USDT_SELL_INTERVAL_KRW   = 2.0   # 매도 2원: 수익/거래량 균형

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
# 분봉 데이터 소스 (업비트 공개 API 우선)
# ──────────────────────────────────────────────────────────
UPBIT_BASE_URL  = "https://api.upbit.com"
UPBIT_MARKET_BTC  = "KRW-BTC"
UPBIT_MARKET_USDT = "KRW-USDT"

# Retry settings
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0

# Logging
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# Report output directory
REPORT_DIR = "reports"
