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

# Retry settings
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0

# Logging
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# Report output directory
REPORT_DIR = "reports"
