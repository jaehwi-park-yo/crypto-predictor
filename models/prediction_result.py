"""
prediction_result.py - Data models for prediction and grid configuration outputs.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional


@dataclass
class BoxPrediction:
    """Box range prediction result from BoxPredictorAgent."""
    symbol: str
    reference_price: float
    box_upper_1sigma: float
    box_lower_1sigma: float
    box_upper_2sigma: float
    box_lower_2sigma: float
    recommended_upper: float
    recommended_lower: float
    sigma_level_used: float
    box_range_pct: float          # (upper - lower) / lower * 100
    confidence_pct: float         # 0 ~ 100
    scenario: str
    scenario_probabilities: Dict[str, float]
    predicted_for_month: str      # "YYYY-MM"
    created_at: datetime

    def range_krw(self) -> float:
        """Absolute KRW range of the recommended box."""
        return self.recommended_upper - self.recommended_lower

    def mid_price(self) -> float:
        """Midpoint of the recommended box."""
        return (self.recommended_upper + self.recommended_lower) / 2


@dataclass
class GridConfig:
    """Optimized grid bot configuration."""
    symbol: str
    box_upper: float
    box_lower: float
    grid_interval_pct: float              # e.g., 0.5 means 0.5%
    grid_interval_krw: float              # actual KRW amount per grid step
    bot_count: int
    capital_total_krw: float
    capital_deployed_krw: float
    krw_reserve_krw: float
    capital_per_bot_krw: float
    estimated_monthly_volume_krw: float
    estimated_reward_tier_threshold: float
    estimated_reward_rate: float
    estimated_reward_krw: float
    fee_rate: float
    created_at: datetime

    # Extra derived fields
    net_reward_after_fee_krw: float = 0.0
    grid_count: int = 0                   # number of grid levels
    round_trips_per_day: float = 2.0      # estimated round trips per bot per day
    aggressiveness: str = "balanced"      # conservative / balanced / aggressive
    buy_interval_pct: Optional[float] = None   # 비대칭 그리드: 매수 간격
    sell_interval_pct: Optional[float] = None  # 비대칭 그리드: 매도 간격


@dataclass
class BreakoutStatus:
    """Risk monitor breakout status."""
    symbol: str
    current_price: float
    box_upper: float
    box_lower: float
    is_within_box: bool
    breakout_direction: Optional[str]   # "UPSIDE" / "DOWNSIDE" / None
    severity: str                        # "NORMAL" / "WARNING" / "CRITICAL"
    action_required: str                 # "HOLD" / "PARTIAL_STOP" / "MONITOR"
    deviation_pct: float                 # how far outside box as %
    suggested_new_upper: Optional[float] = None
    suggested_new_lower: Optional[float] = None
    cash_deploy_plan: Optional[dict] = None   # 하방 이탈 시 현금 배치 전략
    checked_at: datetime = field(default_factory=datetime.now)
