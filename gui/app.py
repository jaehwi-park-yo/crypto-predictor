"""
Streamlit GUI - 박스권 예측 인터랙티브 대시보드
================================================
실행:
    streamlit run gui/app.py

기능:
  - 좌측 사이드바: 예측 기준 시점(as-of) · 자본 · 현금비중 · σ레벨 · 공격성 조정
  - 메인: 캔들차트 + 1σ/2σ 박스밴드 + 그리드 라인 오버레이
  - 하단: 그리드 설정 · 거래량/리워드 추정 · 복합전략 요약
  - 모든 조정은 즉시 재예측 → 차트/지표 실시간 갱신

"4월까지 데이터로 5월 박스권 예측"을 기본값으로 시작한다.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, date

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import plotly.graph_objects as go

from config import (
    CAPITAL_KRW, KRW_HOLD_RATIO, GRID_AGGRESSIVENESS,
    AGGRESSIVENESS_INTERVAL_PCT, MIN_GRID_INTERVAL_PCT,
    COMPOSITE_BTC_RATIO, COMPOSITE_USDT_RATIO,
    USDT_REFERENCE_PRICE_KRW, USDT_BUY_INTERVAL_KRW, USDT_SELL_INTERVAL_KRW,
)
from utils.historical_data import fetch_max_history
from services.prediction_service import predict_as_of, grid_lines
from agents.composite_strategy import CompositeStrategyAgent
from agents.backtester import BacktestAgent


# ──────────────────────────────────────────────────────────
# 데이터 로딩 (캐시)
# ──────────────────────────────────────────────────────────
@st.cache_data(show_spinner="히스토리 수집 중...")
def load_history():
    return fetch_max_history(start="2020-01-01")


@st.cache_data(show_spinner="BTC 레그 백테스트 중...")
def backtest_btc_monthly(capital: float, aggressiveness: str, sigma, as_of: str):
    """BTC 레그의 검증된 월평균 순수익·리워드를 캐시. (휴리스틱 대신 백테스트 사용)"""
    hist = load_history()
    # as_of 이전 구간만으로 과거 성과를 평가 (미래 정보 누설 방지)
    sliced = [c for c in hist if c["date"][:10] <= as_of]
    summary = BacktestAgent().run(
        history=sliced, capital_krw=capital,
        aggressiveness=aggressiveness, use_sigma=sigma,
    )
    if summary.total_months == 0:
        return 0.0, 0.0
    return summary.total_net_grid / summary.total_months, \
        summary.total_reward / summary.total_months


def _eok(x: float) -> str:
    return f"{x/1e8:,.2f}억"


# ──────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="BTC/KRW 박스권 예측", layout="wide")
    st.title("📊 BTC/KRW 박스권 예측 대시보드")
    st.caption("통계 기반(1σ/2σ) 익월 박스권 예측 · 그리드 매매 파라미터 산출")

    history = load_history()
    dates = [c["date"][:10] for c in history]
    min_date = datetime.strptime(dates[31], "%Y-%m-%d").date()
    max_date = datetime.strptime(dates[-1], "%Y-%m-%d").date()

    # ── 사이드바: 조정 컨트롤 ──────────────────────────────
    st.sidebar.header("⚙️ 예측 파라미터")

    # 기본값: 2026-04-30 (없으면 마지막 가용일)
    default_asof = date(2026, 4, 30)
    if not (min_date <= default_asof <= max_date):
        default_asof = max_date
    as_of = st.sidebar.date_input(
        "예측 기준 시점 (as-of)",
        value=default_asof, min_value=min_date, max_value=max_date,
        help="이 시점까지의 데이터만 사용해 다음 달 박스권을 예측합니다.",
    )
    as_of_str = as_of.strftime("%Y-%m-%d")

    capital = st.sidebar.number_input(
        "투입 자본 (원)", min_value=1_000_000, max_value=10_000_000_000,
        value=int(CAPITAL_KRW), step=1_000_000, format="%d",
    )
    krw_hold = st.sidebar.slider(
        "KRW 현금 비중 (%)", min_value=30, max_value=70,
        value=int(KRW_HOLD_RATIO * 100), step=5,
        help="하방 이탈 시 회복 그리드 배치용 예비금. 기본 30% 이상.",
    ) / 100.0

    sigma_choice = st.sidebar.radio(
        "박스 σ 레벨", ["자동", "1σ (68.3%)", "2σ (95.5%)"], index=1,
        help="1σ=정상 운영범위(그리드 밀집), 2σ=이탈 흡수(넓은 박스)",
    )
    use_sigma = {"자동": None, "1σ (68.3%)": 1.0, "2σ (95.5%)": 2.0}[sigma_choice]

    aggressiveness = st.sidebar.selectbox(
        "그리드 공격성", ["conservative", "balanced", "aggressive"],
        index=["conservative", "balanced", "aggressive"].index(GRID_AGGRESSIVENESS),
        format_func=lambda x: {
            "conservative": "보수 (1.0%)", "balanced": "균형 (0.5%)",
            "aggressive": "공격 (0.3%)",
        }[x],
    )

    show_grid = st.sidebar.checkbox("그리드 라인 표시", value=True)
    show_composite = st.sidebar.checkbox("복합전략(BTC+USDT) 요약", value=True)

    # ── 예측 실행 ──────────────────────────────────────────
    try:
        snap = predict_as_of(
            history, as_of=as_of_str, capital_krw=capital,
            aggressiveness=aggressiveness, krw_hold_ratio=krw_hold,
            use_sigma=use_sigma,
        )
    except ValueError as e:
        st.error(str(e))
        return

    # ── 상단 메트릭 ────────────────────────────────────────
    st.subheader(f"🎯 {snap.target_month} 박스권 예측  (기준: {snap.as_of_date})")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("기준가", f"{snap.reference_price:,.0f}원")
    c2.metric(f"권장 박스 ({snap.sigma_level_used:.0f}σ)",
              f"폭 {snap.box_range_pct:.1f}%",
              help=f"{snap.recommended_lower:,.0f} ~ {snap.recommended_upper:,.0f}")
    c3.metric("월간 변동성(σ)", f"{snap.monthly_sigma_pct:.1f}%")
    c4.metric("예측 신뢰도", f"{snap.confidence_pct:.1f}%")

    # ── 차트 ───────────────────────────────────────────────
    fig = build_chart(snap, show_grid)
    st.plotly_chart(fig, use_container_width=True)

    # ── 박스/그리드 상세 ──────────────────────────────────
    left, right = st.columns(2)
    with left:
        st.markdown("#### 📦 박스권 상세")
        st.table({
            "구분": ["1σ 상단", "1σ 하단", "2σ 상단", "2σ 하단",
                    "권장 상단", "권장 하단"],
            "가격(원)": [
                f"{snap.box_upper_1s:,.0f}", f"{snap.box_lower_1s:,.0f}",
                f"{snap.box_upper_2s:,.0f}", f"{snap.box_lower_2s:,.0f}",
                f"{snap.recommended_upper:,.0f}", f"{snap.recommended_lower:,.0f}",
            ],
        })
    with right:
        st.markdown("#### 🤖 그리드 봇 설정")
        st.table({
            "항목": ["그리드 간격", "봇 수", "그리드 투입", "KRW 예비금",
                    "봇당 자본", "예상 월거래량", "예상 리워드"],
            "값": [
                f"{snap.grid_interval_pct:.2f}% ({snap.grid_interval_krw:,.0f}원)",
                f"{snap.bot_count}개",
                f"{snap.capital_deployed_krw:,.0f}원",
                f"{snap.krw_reserve_krw:,.0f}원",
                f"{snap.capital_per_bot_krw:,.0f}원",
                _eok(snap.estimated_monthly_volume_krw),
                f"{snap.estimated_reward_krw:,.0f}원",
            ],
        })

    # ── 리워드 목표 ────────────────────────────────────────
    st.markdown("#### 🎁 리워드 목표 구간")
    safe, stretch = snap.safe_target, snap.stretch_target
    rc1, rc2 = st.columns(2)
    rc1.info(
        f"**안전 목표**: {_eok(safe['threshold'])} 거래량 → "
        f"리워드 {safe['reward']:,.0f}원\n\n"
        f"필요 일회전율 {safe['req_daily_turnover']:.2f}배"
    )
    rc2.warning(
        f"**스트레치 목표**: {_eok(stretch['threshold'])} 거래량 → "
        f"리워드 {stretch['reward']:,.0f}원\n\n"
        f"필요 일회전율 {stretch['req_daily_turnover']:.2f}배"
    )

    # ── 복합전략 ───────────────────────────────────────────
    if show_composite:
        st.markdown("#### 🧩 복합전략 (BTC conservative + USDT aggressive)")
        composite = CompositeStrategyAgent()
        # BTC 레그 월수익은 검증된 백테스트의 월평균(자본 60% 배분 기준)을 사용
        btc_cap = capital * COMPOSITE_BTC_RATIO
        btc_net_full, btc_reward_full = backtest_btc_monthly(
            btc_cap, "conservative", use_sigma, snap.as_of_date,
        )
        comp = composite.analyze(
            total_capital_krw=capital,
            btc_monthly_net_grid=btc_net_full,
            btc_monthly_reward=btc_reward_full,
        )
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("BTC 레그 월수익", f"{comp.btc_total_krw:,.0f}원",
                   f"{comp.btc_return_pct:+.2f}%")
        cc2.metric("USDT 레그 월수익", f"{comp.usdt_total_krw:,.0f}원",
                   f"{comp.usdt_return_pct:+.2f}%")
        cc3.metric("복합 월수익률", f"{comp.combined_return_pct:+.2f}%",
                   f"연환산 {comp.combined_return_pct*12:.1f}%")
        with st.expander("복합전략 평가 상세"):
            for line in comp.evaluation:
                st.markdown(f"- {line}")

    st.caption(
        "⚠️ 외부 API 차단 환경에서는 합성 데이터로 동작합니다. "
        "거래량/리워드/수익은 명시적 가정 기반 추정치이며 실거래 데이터로 보정이 필요합니다."
    )


def build_chart(snap, show_grid: bool) -> go.Figure:
    """캔들차트 + 박스밴드 + 그리드라인 Plotly Figure."""
    tail = snap.history_tail
    x = [c["date"][:10] for c in tail]

    fig = go.Figure()
    # 캔들스틱
    fig.add_trace(go.Candlestick(
        x=x,
        open=[c["open"] for c in tail],
        high=[c["high"] for c in tail],
        low=[c["low"] for c in tail],
        close=[c["close"] for c in tail],
        name="BTC/KRW",
        increasing_line_color="#d24f45", decreasing_line_color="#1f77b4",
    ))

    # 미래 구간 x축 라벨 (대상 월 표기)
    future_x = [snap.as_of_date, f"{snap.target_month}-15", f"{snap.target_month}-30"]
    last_x = x[-1] if x else snap.as_of_date

    # 박스 밴드 (수평선 + 음영) — 예측 구간으로 연장
    band_x = [last_x] + future_x
    def hline(y, color, name, dash="solid", width=2):
        fig.add_trace(go.Scatter(
            x=band_x, y=[y] * len(band_x), mode="lines",
            line=dict(color=color, width=width, dash=dash),
            name=name,
        ))

    # 2σ 밴드 음영
    fig.add_trace(go.Scatter(
        x=band_x + band_x[::-1],
        y=[snap.box_upper_2s] * len(band_x) + [snap.box_lower_2s] * len(band_x),
        fill="toself", fillcolor="rgba(255,165,0,0.08)",
        line=dict(width=0), name="2σ 영역", hoverinfo="skip",
    ))
    # 1σ 밴드 음영
    fig.add_trace(go.Scatter(
        x=band_x + band_x[::-1],
        y=[snap.box_upper_1s] * len(band_x) + [snap.box_lower_1s] * len(band_x),
        fill="toself", fillcolor="rgba(31,119,180,0.12)",
        line=dict(width=0), name="1σ 영역", hoverinfo="skip",
    ))

    hline(snap.box_upper_2s, "orange", "2σ 상단", "dot", 1)
    hline(snap.box_lower_2s, "orange", "2σ 하단", "dot", 1)
    hline(snap.box_upper_1s, "#1f77b4", "1σ 상단", "dash", 1)
    hline(snap.box_lower_1s, "#1f77b4", "1σ 하단", "dash", 1)
    hline(snap.recommended_upper, "green", "권장 상단", "solid", 2)
    hline(snap.recommended_lower, "green", "권장 하단", "solid", 2)
    hline(snap.reference_price, "gray", "기준가", "solid", 1)

    # 그리드 라인
    if show_grid:
        lines = grid_lines(snap.recommended_lower, snap.recommended_upper,
                           snap.grid_interval_pct)
        for i, gl in enumerate(lines):
            fig.add_trace(go.Scatter(
                x=future_x, y=[gl] * len(future_x), mode="lines",
                line=dict(color="rgba(120,120,120,0.25)", width=0.5),
                showlegend=(i == 0), name="그리드 라인" if i == 0 else None,
                hoverinfo="skip",
            ))

    fig.update_layout(
        height=560, xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        margin=dict(l=40, r=20, t=40, b=40),
        title=f"{snap.target_month} 박스권 예측 (기준 {snap.as_of_date})",
    )
    fig.update_yaxes(title="가격 (KRW)", tickformat=",.0f")
    return fig


if __name__ == "__main__":
    main()
