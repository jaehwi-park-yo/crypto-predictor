"""
Streamlit GUI - 박스권 예측 인터랙티브 대시보드
================================================
실행: streamlit run gui/app.py

탭 구성:
  📈 예측 워크벤치  — as-of 시점 기준 익월 박스권 예측 (파라미터 조정)
  📡 실시간 모니터  — 실제 데이터가 쌓이면서 예측→측정 전환 추적

예측→측정 전환 메커니즘:
  월 경과에 따라 실제 캔들이 측정 구간을 채워가고,
  남은 기간은 갱신된 σ로 수정 예측을 제공한다.
  σ가 변하면 수정 박스(주황 실선)가 원본 박스(녹색 점선)와 달라진다.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import plotly.graph_objects as go

from config import (
    CAPITAL_KRW, KRW_HOLD_RATIO, GRID_AGGRESSIVENESS,
    AGGRESSIVENESS_INTERVAL_PCT, MIN_GRID_INTERVAL_PCT,
    COMPOSITE_BTC_RATIO,
)
from utils.historical_data import fetch_max_history
from utils.live_data import fetch_current_price, fetch_month_candles
from services.prediction_service import predict_as_of, grid_lines
from services.prediction_monitor import monitor, get_month_actuals
from agents.composite_strategy import CompositeStrategyAgent
from agents.backtester import BacktestAgent


# ──────────────────────────────────────────────────────────
# 데이터 캐시
# ──────────────────────────────────────────────────────────
@st.cache_data(show_spinner="히스토리 수집 중 (업비트 API → 폴백)...")
def load_history():
    return fetch_max_history(start="2020-01-01")


@st.cache_data(ttl=60, show_spinner="실시간 가격 조회 중...")
def load_live_price(fallback: float):
    return fetch_current_price(fallback_price=fallback)


@st.cache_data(ttl=300, show_spinner="당월 실제 데이터 조회 중 (업비트)...")
def load_month_actuals(year: int, month: int):
    """업비트 API로 당월 실제 일봉 수집. 실패 시 빈 리스트."""
    return fetch_month_candles(year, month)


@st.cache_data(show_spinner="BTC 레그 백테스트 중...")
def backtest_btc_monthly(capital: float, aggressiveness: str, sigma, as_of: str):
    hist = load_history()
    sliced = [c for c in hist if c["date"][:10] <= as_of]
    s = BacktestAgent().run(
        history=sliced, capital_krw=capital,
        aggressiveness=aggressiveness, use_sigma=sigma,
    )
    if s.total_months == 0:
        return 0.0, 0.0
    return s.total_net_grid / s.total_months, s.total_reward / s.total_months


# ──────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────
def _eok(x):  return f"{x/1e8:,.2f}억"
def _won(x):  return f"{x:,.0f}원"
def _pct(x):  return f"{x:+.2f}%"

STATUS_COLOR = {
    "PREDICTED": "🔵", "ON_TRACK": "🟢",
    "WARNING": "🟡",   "BREACH": "🔴",
}


# ──────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="BTC/KRW 박스권 예측", layout="wide",
                       page_icon="📊")
    st.title("📊 BTC/KRW 박스권 예측 & 측정 대시보드")
    st.caption("업비트 공개 API 기반 실시간 연동 · 통계(1σ/2σ) 익월 예측 · 예측→측정 전환 추적")

    history = load_history()
    dates   = [c["date"][:10] for c in history]
    min_date = datetime.strptime(dates[31], "%Y-%m-%d").date()
    max_date = datetime.strptime(dates[-1], "%Y-%m-%d").date()

    # ── 공통 사이드바 ──────────────────────────────────────
    st.sidebar.header("⚙️ 공통 파라미터")
    capital = st.sidebar.number_input(
        "투입 자본 (원)", min_value=1_000_000, max_value=10_000_000_000,
        value=int(CAPITAL_KRW), step=1_000_000, format="%d",
    )
    krw_hold = st.sidebar.slider(
        "KRW 현금 비중 (%)", min_value=30, max_value=70,
        value=int(KRW_HOLD_RATIO * 100), step=5,
        help="하방 이탈 시 회복 그리드 배치용 예비금. 30% 이상 권장.",
    ) / 100.0
    sigma_choice = st.sidebar.radio(
        "박스 σ 레벨", ["자동", "1σ (68.3%)", "2σ (95.5%)"],
        index=1,
    )
    use_sigma = {"자동": None, "1σ (68.3%)": 1.0, "2σ (95.5%)": 2.0}[sigma_choice]
    aggressiveness = st.sidebar.selectbox(
        "그리드 공격성",
        ["conservative", "balanced", "aggressive"],
        index=["conservative", "balanced", "aggressive"].index(GRID_AGGRESSIVENESS),
        format_func=lambda x: {
            "conservative": "보수 (1.0%)",
            "balanced": "균형 (0.5%)",
            "aggressive": "공격 (0.3%)",
        }[x],
    )

    # ── 탭 ────────────────────────────────────────────────
    tab_predict, tab_monitor = st.tabs(
        ["📈 예측 워크벤치", "📡 실시간 모니터 (예측→측정 전환)"]
    )

    # ══════════════════════════════════════════════════════
    # 탭 1: 예측 워크벤치
    # ══════════════════════════════════════════════════════
    with tab_predict:
        st.subheader("📈 익월 박스권 예측")

        default_asof = date(2026, 4, 30)
        if not (min_date <= default_asof <= max_date):
            default_asof = max_date
        as_of = st.date_input(
            "예측 기준 시점 (as-of)",
            value=default_asof, min_value=min_date, max_value=max_date,
            help="이 날짜까지의 데이터만 사용해 다음 달을 예측합니다.",
            key="wb_asof",
        )
        as_of_str = as_of.strftime("%Y-%m-%d")

        show_grid_wb = st.checkbox("그리드 라인 표시", value=True, key="wb_grid")
        show_composite = st.checkbox("복합전략(BTC+USDT) 요약", value=False, key="wb_comp")

        try:
            snap = predict_as_of(
                history, as_of=as_of_str, capital_krw=capital,
                aggressiveness=aggressiveness, krw_hold_ratio=krw_hold,
                use_sigma=use_sigma,
            )
        except ValueError as e:
            st.error(str(e)); return

        # 메트릭
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("기준가", _won(snap.reference_price))
        c2.metric(f"권장 박스 ({snap.sigma_level_used:.0f}σ)",
                  f"폭 {snap.box_range_pct:.1f}%",
                  help=f"{_won(snap.recommended_lower)} ~ {_won(snap.recommended_upper)}")
        c3.metric("월간 변동성(σ)", f"{snap.monthly_sigma_pct:.1f}%")
        c4.metric("일간 변동성(σ)", f"{snap.daily_sigma*100:.2f}%")

        st.plotly_chart(build_prediction_chart(snap, show_grid_wb),
                        use_container_width=True)

        # 박스·그리드 테이블
        left, right = st.columns(2)
        with left:
            st.markdown("**📦 박스권 상세**")
            st.table({
                "구분": ["1σ 상단","1σ 하단","2σ 상단","2σ 하단","권장 상단","권장 하단"],
                "가격(원)": [
                    _won(snap.box_upper_1s), _won(snap.box_lower_1s),
                    _won(snap.box_upper_2s), _won(snap.box_lower_2s),
                    _won(snap.recommended_upper), _won(snap.recommended_lower),
                ],
            })
        with right:
            st.markdown("**🤖 그리드 봇 설정**")
            st.table({
                "항목": ["그리드 간격","봇 수","그리드 투입","KRW 예비금","봇당 자본","예상 월거래량","예상 리워드"],
                "값": [
                    f"{snap.grid_interval_pct:.2f}% ({_won(snap.grid_interval_krw)})",
                    f"{snap.bot_count}개",
                    _won(snap.capital_deployed_krw),
                    _won(snap.krw_reserve_krw),
                    _won(snap.capital_per_bot_krw),
                    _eok(snap.estimated_monthly_volume_krw),
                    _won(snap.estimated_reward_krw),
                ],
            })

        # 리워드
        st.markdown("**🎁 리워드 목표**")
        rc1, rc2 = st.columns(2)
        rc1.info(
            f"**안전** {_eok(snap.safe_target['threshold'])} → "
            f"리워드 {_won(snap.safe_target['reward'])}\n\n"
            f"일회전율 {snap.safe_target['req_daily_turnover']:.2f}배"
        )
        rc2.warning(
            f"**스트레치** {_eok(snap.stretch_target['threshold'])} → "
            f"리워드 {_won(snap.stretch_target['reward'])}\n\n"
            f"일회전율 {snap.stretch_target['req_daily_turnover']:.2f}배"
        )

        # 복합전략
        if show_composite:
            st.markdown("**🧩 복합전략 (BTC conservative + USDT aggressive)**")
            btc_cap = capital * COMPOSITE_BTC_RATIO
            btc_net, btc_rew = backtest_btc_monthly(
                btc_cap, "conservative", use_sigma, as_of_str,
            )
            comp = CompositeStrategyAgent().analyze(
                total_capital_krw=capital,
                btc_monthly_net_grid=btc_net,
                btc_monthly_reward=btc_rew,
            )
            cc1, cc2, cc3 = st.columns(3)
            cc1.metric("BTC 레그", _won(comp.btc_total_krw), _pct(comp.btc_return_pct))
            cc2.metric("USDT 레그", _won(comp.usdt_total_krw), _pct(comp.usdt_return_pct))
            cc3.metric("복합 월수익률", _pct(comp.combined_return_pct),
                       f"연 {comp.combined_return_pct*12:.1f}%")
            with st.expander("복합전략 평가 상세"):
                for ln in comp.evaluation:
                    st.markdown(f"- {ln}")

    # ══════════════════════════════════════════════════════
    # 탭 2: 실시간 모니터
    # ══════════════════════════════════════════════════════
    with tab_monitor:
        st.subheader("📡 실시간 예측→측정 전환 모니터")

        # 모니터 설정
        mc1, mc2 = st.columns([2, 1])
        with mc1:
            today_val = st.date_input(
                "기준일 (오늘)",
                value=min(date.today(), max_date),
                min_value=min_date, max_value=max_date,
                key="mon_today",
                help="실운영 시 자동으로 오늘 날짜. 테스트 시 날짜를 변경해 월중 시뮬레이션 가능.",
            )
        with mc2:
            live_mode = st.toggle(
                "실시간 가격 조회", value=True,
                help="업비트 API로 현재가를 조회합니다. 오프라인 환경에서는 히스토리 마지막 종가를 사용합니다.",
            )

        today_str = today_val.strftime("%Y-%m-%d")

        # 대상 월 = today 다음 달 (월초라면 이번 달도 선택 가능)
        pred_year  = today_val.year
        pred_month = today_val.month
        # 예측 기준: 대상 월 전날 (이전 월 말일)
        first_of_target = date(pred_year, pred_month, 1)
        pred_asof = (first_of_target - timedelta(days=1)).strftime("%Y-%m-%d")

        # 원본 예측 (대상 월 시작 전 기준)
        try:
            base_snap = predict_as_of(
                history, as_of=pred_asof, capital_krw=capital,
                aggressiveness=aggressiveness, krw_hold_ratio=krw_hold,
                use_sigma=use_sigma,
            )
        except ValueError:
            st.warning("대상 월 이전 데이터가 부족합니다. 예측 워크벤치 탭에서 as-of를 조정해 주세요.")
            return

        # 업비트 실제 데이터 병합: API 성공 시 우선, 실패 시 히스토리로 폴백
        live_actuals = load_month_actuals(pred_year, pred_month)
        if live_actuals:
            # 업비트에서 받아온 당월 실제 데이터를 히스토리에 병합
            existing_dates = {c["date"] for c in history}
            extra = [c for c in live_actuals if c["date"] not in existing_dates]
            merged_history = history + extra
            data_source_label = f"업비트 API ({len(live_actuals)}일)"
        else:
            merged_history = history
            data_source_label = "히스토리 (합성/캐시)"

        # 실시간 현재가
        last_hist_price = merged_history[-1]["close"] if merged_history else 0
        if live_mode:
            quote = load_live_price(fallback=last_hist_price)
            cur_price = quote.price
            price_src  = quote.source
        else:
            cur_price = last_hist_price
            price_src  = "cache"

        # 모니터 실행
        mon = monitor(
            snapshot=base_snap,
            full_history=merged_history,
            today_str=today_str,
            live_price=cur_price,
            live_source=price_src,
        )

        # ── 상태 헤더 ──────────────────────────────────────
        badge = STATUS_COLOR.get(mon.overall_status, "⚪")
        st.markdown(
            f"### {badge} {mon.target_month}  |  {mon.overall_status}  "
            f"|  경과 {mon.elapsed_days}/{mon.total_days}일  "
            f"|  데이터: {data_source_label}"
        )
        st.progress(int(mon.progress_pct),
                    text=f"예측→측정 전환 {mon.progress_pct:.0f}% 완료")

        # ── 실시간 현재가 배너 ─────────────────────────────
        if price_src not in ("fallback", "cache"):
            price_label = f"💱 현재가 (업비트): **{_won(cur_price)}**"
        else:
            price_label = f"💱 현재가 (히스토리 폴백): **{_won(cur_price)}**"
        st.info(price_label + f"  |  원본 기준가: {_won(base_snap.reference_price)}")

        # ── 요약 메트릭 ────────────────────────────────────
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("측정 Containment", f"{mon.measured_containment_pct:.0f}%",
                  help="경과 일 중 권장 박스 내 종가 비율")
        m2.metric("경고 구간", f"{mon.days_warning}일",
                  help="1σ~2σ 사이 (박스 경계 접근)")
        m3.metric("이탈 (상방)", f"{mon.days_breach_upper}일")
        m4.metric("이탈 (하방)", f"{mon.days_breach_lower}일")
        m5.metric("σ 변화 (수정예측)", _pct(mon.sigma_change_pct),
                  help="원본 σ 대비 현재까지 데이터로 추정한 σ 변화율")

        # ── 권고 ──────────────────────────────────────────
        if mon.overall_status == "BREACH":
            st.error(f"🚨 {mon.status_detail}\n\n💡 {mon.recommendation}")
        elif mon.overall_status == "WARNING":
            st.warning(f"⚠️ {mon.status_detail}\n\n💡 {mon.recommendation}")
        elif mon.overall_status == "ON_TRACK":
            st.success(f"✅ {mon.status_detail}\n\n💡 {mon.recommendation}")
        else:
            st.info(f"🔵 {mon.status_detail}")

        # ── 모니터 차트 ────────────────────────────────────
        show_grid_mon = st.checkbox("그리드 라인 표시", value=True, key="mon_grid")
        fig = build_monitor_chart(mon, merged_history, show_grid_mon)
        st.plotly_chart(fig, use_container_width=True)

        # ── 수정 예측 vs 원본 비교 ─────────────────────────
        if mon.revised and mon.elapsed_days >= 3:
            st.markdown("**📐 수정 예측 vs 원본 비교**")
            diff_left, diff_right = st.columns(2)
            with diff_left:
                st.table({
                    "항목": ["일간 σ","권장 상단","권장 하단","박스 중심 이동"],
                    "원본": [
                        f"{base_snap.daily_sigma*100:.2f}%",
                        _won(base_snap.recommended_upper),
                        _won(base_snap.recommended_lower),
                        "기준",
                    ],
                    "수정": [
                        f"{mon.revised_sigma*100:.2f}%",
                        _won(mon.forecast_revised_upper),
                        _won(mon.forecast_revised_lower),
                        _pct(mon.box_center_shift_pct),
                    ],
                })
            with diff_right:
                st.markdown(
                    f"**남은 {mon.remaining_days}일 수정 예측 근거:**\n\n"
                    f"- 원본: 예측 기준일 전 31일 로그수익률\n"
                    f"- 수정: 원본 + 실제 경과 {mon.elapsed_days}일 포함\n"
                    f"- 더 긴 데이터 → σ 추정 정밀도 향상\n"
                    f"- σ 변화: {_pct(mon.sigma_change_pct)}"
                )

        # ── 일별 측정 테이블 ──────────────────────────────
        if mon.measured_candles:
            with st.expander(f"일별 측정 상세 ({mon.elapsed_days}일)"):
                zone_icon = {
                    "inner": "🟢", "warning": "🟡",
                    "breach_upper": "🔴↑", "breach_lower": "🔴↓",
                }
                rows = []
                for d in mon.measured_candles:
                    rows.append({
                        "날짜": d.date,
                        "종가": _won(d.close),
                        "상태": zone_icon.get(d.zone, "?"),
                        "이탈폭": f"{d.deviation_pct:+.2f}%" if d.deviation_pct else "-",
                    })
                import pandas as pd
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.caption(
            "💡 실운영 시: 업비트 API로 당일 종가 자동 수집 → 측정 구간 갱신 → 남은 기간 수정예측."
            " 현재는 환경 제약으로 합성 히스토리를 사용합니다."
        )


# ══════════════════════════════════════════════════════════
# 차트 빌더 (탭 1: 예측 워크벤치)
# ══════════════════════════════════════════════════════════
def build_prediction_chart(snap, show_grid: bool) -> go.Figure:
    tail = snap.history_tail
    x    = [c["date"][:10] for c in tail]
    last_x = x[-1] if x else snap.as_of_date
    future_x = [snap.as_of_date, f"{snap.target_month}-15",
                 f"{snap.target_month}-30"]
    band_x = [last_x] + future_x

    fig = go.Figure()

    # 캔들
    fig.add_trace(go.Candlestick(
        x=x,
        open=[c["open"] for c in tail], high=[c["high"] for c in tail],
        low=[c["low"] for c in tail],   close=[c["close"] for c in tail],
        name="BTC/KRW",
        increasing_line_color="#d24f45", decreasing_line_color="#4169e1",
    ))

    # 밴드
    _add_band(fig, band_x, snap.box_upper_2s, snap.box_lower_2s,
              "rgba(255,165,0,0.08)", "orange", "2σ 영역", 1)
    _add_band(fig, band_x, snap.box_upper_1s, snap.box_lower_1s,
              "rgba(65,105,225,0.12)", "#4169e1", "1σ 영역", 1)
    _hline(fig, band_x, snap.recommended_upper, "green", "권장 상단", 2)
    _hline(fig, band_x, snap.recommended_lower, "green", "권장 하단", 2)
    _hline(fig, band_x, snap.reference_price,   "#888",  "기준가", 1)

    # 그리드
    if show_grid:
        for i, gl in enumerate(grid_lines(snap.recommended_lower,
                                          snap.recommended_upper,
                                          snap.grid_interval_pct)):
            fig.add_trace(go.Scatter(
                x=future_x, y=[gl]*len(future_x), mode="lines",
                line=dict(color="rgba(100,100,100,0.2)", width=0.5),
                showlegend=(i == 0),
                name="그리드 라인" if i == 0 else None,
                hoverinfo="skip",
            ))

    fig.update_layout(
        height=520, xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        margin=dict(l=40, r=20, t=40, b=40),
        title=f"{snap.target_month} 박스권 예측 (기준 {snap.as_of_date})",
    )
    fig.update_yaxes(title="가격 (KRW)", tickformat=",.0f")
    return fig


# ══════════════════════════════════════════════════════════
# 차트 빌더 (탭 2: 모니터 — 측정 + 수정예측)
# ══════════════════════════════════════════════════════════
def build_monitor_chart(mon, full_history, show_grid: bool) -> go.Figure:
    snap = mon.original
    year  = int(snap.target_month[:4])
    month = int(snap.target_month[5:7])

    # 사전 히스토리 (대상 월 전 90일)
    month_start = f"{year:04d}-{month:02d}-01"
    pre_hist = [c for c in full_history
                if c["date"] < month_start][-90:]

    fig = go.Figure()

    # ── 사전 히스토리 캔들 (흐림) ─────────────────────────
    if pre_hist:
        fig.add_trace(go.Candlestick(
            x=[c["date"] for c in pre_hist],
            open=[c["open"] for c in pre_hist],
            high=[c["high"] for c in pre_hist],
            low=[c["low"] for c in pre_hist],
            close=[c["close"] for c in pre_hist],
            name="사전 히스토리",
            opacity=0.35,
            increasing_line_color="#d24f45",
            decreasing_line_color="#4169e1",
        ))

    # ── 측정 구간 캔들 (컬러 코딩) ───────────────────────
    zone_up   = [d for d in mon.measured_candles if d.zone == "breach_upper"]
    zone_dn   = [d for d in mon.measured_candles if d.zone == "breach_lower"]
    zone_warn = [d for d in mon.measured_candles if d.zone == "warning"]
    zone_in   = [d for d in mon.measured_candles if d.zone == "inner"]

    def _add_measured_candles(days, color, name):
        if not days:
            return
        raw = {c["date"]: c for c in full_history}
        xs, opens, highs, lows, closes = [], [], [], [], []
        for d in days:
            c = raw.get(d.date, {})
            if c:
                xs.append(d.date); opens.append(c["open"])
                highs.append(c["high"]); lows.append(c["low"])
                closes.append(c["close"])
        if xs:
            fig.add_trace(go.Candlestick(
                x=xs, open=opens, high=highs, low=lows, close=closes,
                name=name,
                increasing_line_color=color,
                decreasing_line_color=color,
            ))

    _add_measured_candles(zone_in,   "#2ca02c", "🟢 박스 내 (측정)")
    _add_measured_candles(zone_warn, "#ff7f0e", "🟡 경고 구간 (측정)")
    _add_measured_candles(zone_up,   "#d62728", "🔴 상방 이탈 (측정)")
    _add_measured_candles(zone_dn,   "#8b0000", "🔴 하방 이탈 (측정)")

    # ── 오늘 이후: 수정 예측 밴드 ─────────────────────────
    days_left = mon.remaining_days
    if days_left > 0:
        today_d = datetime.strptime(mon.today, "%Y-%m-%d").date()
        fut_dates = [
            (today_d + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days_left + 1)
        ]

        # 원본 박스 (점선, 기준)
        _hline(fig, fut_dates, snap.recommended_upper,
               "#2ca02c", "원본 상단", 1, "dot")
        _hline(fig, fut_dates, snap.recommended_lower,
               "#2ca02c", "원본 하단", 1, "dot")

        # 수정 예측 밴드 (실선)
        _add_band(fig, fut_dates,
                  mon.forecast_upper_2s, mon.forecast_lower_2s,
                  "rgba(255,165,0,0.08)", "orange", "수정 2σ", 1)
        _add_band(fig, fut_dates,
                  mon.forecast_upper_1s, mon.forecast_lower_1s,
                  "rgba(65,105,225,0.10)", "#4169e1", "수정 1σ", 1)
        _hline(fig, fut_dates, mon.forecast_revised_upper,
               "#ff7f0e", "수정 권장 상단", 2)
        _hline(fig, fut_dates, mon.forecast_revised_lower,
               "#ff7f0e", "수정 권장 하단", 2)

        # 그리드 라인
        if show_grid:
            for i, gl in enumerate(grid_lines(
                mon.forecast_revised_lower, mon.forecast_revised_upper,
                snap.grid_interval_pct,
            )):
                fig.add_trace(go.Scatter(
                    x=fut_dates, y=[gl]*len(fut_dates), mode="lines",
                    line=dict(color="rgba(100,100,100,0.18)", width=0.5),
                    showlegend=(i == 0),
                    name="그리드 라인" if i == 0 else None,
                    hoverinfo="skip",
                ))

    # ── 현재가 마커 ──────────────────────────────────────
    if mon.current_price > 0 and mon.today:
        fig.add_trace(go.Scatter(
            x=[mon.today], y=[mon.current_price],
            mode="markers+text",
            marker=dict(size=10, color="gold", symbol="diamond"),
            text=[f"  현재가\n  {_won(mon.current_price)}"],
            textposition="middle right",
            name=f"현재가 ({mon.current_source})",
        ))

    # ── 전환 구분선 (오늘) ────────────────────────────────
    fig.add_vline(
        x=mon.today, line=dict(color="rgba(150,150,150,0.5)", width=1, dash="dash"),
    )

    fig.update_layout(
        height=580, xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, font=dict(size=11)),
        margin=dict(l=40, r=20, t=50, b=40),
        title=(
            f"{snap.target_month} 예측→측정 전환  |  "
            f"측정 {mon.elapsed_days}일 완료 / 수정예측 {mon.remaining_days}일  |  "
            f"Containment {mon.measured_containment_pct:.0f}%"
        ),
    )
    fig.update_yaxes(title="가격 (KRW)", tickformat=",.0f")
    return fig


# ──────────────────────────────────────────────────────────
# 차트 헬퍼
# ──────────────────────────────────────────────────────────
def _add_band(fig, xs, upper, lower, fill_color, line_color, name, width=1):
    fig.add_trace(go.Scatter(
        x=xs + xs[::-1],
        y=[upper]*len(xs) + [lower]*len(xs),
        fill="toself", fillcolor=fill_color,
        line=dict(width=0), name=name, hoverinfo="skip",
    ))
    _hline(fig, xs, upper, line_color, f"{name} 상단", width, "dot")
    _hline(fig, xs, lower, line_color, f"{name} 하단", width, "dot")


def _hline(fig, xs, y, color, name, width=1, dash="solid"):
    fig.add_trace(go.Scatter(
        x=xs, y=[y]*len(xs), mode="lines",
        line=dict(color=color, width=width, dash=dash),
        name=name,
    ))


if __name__ == "__main__":
    main()
