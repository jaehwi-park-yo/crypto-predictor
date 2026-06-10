"""
⑦ 미스터리쇼퍼 에이전트 — 집단지성 사용자 리뷰 시뮬레이션

투입자본 × 목표거래량 × 경험 × 성향 공간을 층화 샘플링해 120+개의
가상 사용자(페르소나)를 생성하고, 각자가 대시보드(index.html)와 동일한
계산 모델을 사용해 "사용 여정"을 수행한 뒤 리뷰를 작성한다.
리뷰를 통계 집계해 공통 지적사항(집단지성)을 개선과제로 도출한다.

특징:
  - 읽기 전용 감사자 — 예측 파이프라인에 영향 없음
  - 결정적 시드 → 매 실행 동일 페르소나 집단 (회귀 테스트 가능)
  - index.html의 JS 계산 로직을 파이썬으로 미러링 (UI가 보여줄 숫자를 재현)

실행:
    python agents/mystery_shopper.py            # 단독 실행
    python agents/mystery_shopper.py --n 200    # 페르소나 수 조정
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ═══════════════════════════════════════════════════════════════════════════
# 대시보드(index.html) 계산 모델 미러 — UI 사용자가 보게 될 숫자를 재현
# ═══════════════════════════════════════════════════════════════════════════

BTC_COEF = {  # 존별 자본당 계수 (index.html updatePnlTable과 동일)
    "inner": {"vol_per_cap": 5.6e8 / 10.08e6, "grid_per_cap": 67.2e4 / 10.08e6},
    "outer": {"vol_per_cap": 1.8e8 / 6.72e6,  "grid_per_cap": 19.6e4 / 6.72e6},
}
BTC_FEE = 0.0008          # 왕복 0.04% × 2
USDT_FEE = 0.0004
USDT_DAILY_RANGE = 8.0    # 원
USDT_EFF = 0.40
USDT_REF = 1401.0
USDT_TDAYS = 30
USDT_BOX = {"inner": 31, "outer": 30}
USDT_ACTIVE = {"inner": 1.0, "outer": 0.27}
REWARD_CAP = 3_000_000
MIN_ORDER_KRW = 5_000     # 빗썸 최소주문금액 (봇당 자본이 이보다 작으면 실주문 불가)

REWARD_TIERS = [  # (직전월 거래량 하한, 리워드율, 라벨)
    (1e11, 0.0002,  "1,000억원 이상"),
    (1e10, 0.00018, "100억원 이상"),
    (1e9,  0.00015, "10억원 이상"),
    (1e8,  0.00008, "1억원 이상"),
    (1e7,  0.00005, "1,000만원 이상"),
    (0,    0.00003, "1,000만원 미만"),
]


def reward_rate(prev_month_vol: float) -> float:
    for threshold, rate, _ in REWARD_TIERS:
        if prev_month_vol >= threshold:
            return rate
    return 0.00003


def calc_usdt_zone(cap: float, buy: float, sell: float, zone: str,
                   rate: float) -> dict:
    """index.html calcUsdt 미러 (사이클 기반 RT 모델)."""
    cycle = buy + sell
    bots = max(1, int(USDT_BOX[zone] // buy))
    cap_per_bot = cap / bots
    rt_day = (USDT_DAILY_RANGE / cycle) * USDT_EFF
    total_rt = rt_day * USDT_TDAYS * bots
    sell_pct = sell / USDT_REF
    profit = total_rt * sell_pct * cap_per_bot
    fee = total_rt * 2 * USDT_FEE * cap_per_bot
    active = USDT_ACTIVE[zone]
    net_grid = (profit - fee) * active
    vol = total_rt * cap_per_bot * 2 * active
    reward = min(vol * rate, REWARD_CAP)
    return {"vol": vol, "net": net_grid + reward, "cap_per_bot": cap_per_bot,
            "bots": bots, "grid_positive": (profit - fee) > 0}


def calc_btc_zone(cap: float, zone: str, rate: float) -> dict:
    c = BTC_COEF[zone]
    vol = cap * c["vol_per_cap"]
    grid = cap * c["grid_per_cap"]
    fee = vol * BTC_FEE
    reward = min(vol * rate, REWARD_CAP)
    bots = 71 if zone == "inner" else 82  # 기본 박스폭 기준
    return {"vol": vol, "net": grid - fee + reward,
            "cap_per_bot": cap / bots, "bots": bots}


def estimate_portfolio(capital: float, btc_ratio: float, inner_ratio: float,
                       krw_hold: float, rate: float) -> dict:
    """index.html estimatePortfolio 미러 — 4전략 합산."""
    investable = capital * (1 - krw_hold)
    btc_cap, usdt_cap = investable * btc_ratio, investable * (1 - btc_ratio)
    zones = [
        calc_btc_zone(btc_cap * inner_ratio, "inner", rate),
        calc_btc_zone(btc_cap * (1 - inner_ratio), "outer", rate),
        calc_usdt_zone(usdt_cap * inner_ratio, 1, 3, "inner", rate),
        calc_usdt_zone(usdt_cap * (1 - inner_ratio), 1, 3, "outer", rate),
    ]
    return {
        "vol": sum(z["vol"] for z in zones),
        "net": sum(z["net"] for z in zones),
        "investable": investable,
        "min_cap_per_bot": min(z["cap_per_bot"] for z in zones),
        "zones": zones,
    }


def recommend_params(capital: float, target_vol: float, rate: float) -> dict:
    """index.html recommendParams 미러 — 슬라이더 범위 전수 탐색."""
    best, fallback = None, None
    for b in range(40, 75, 5):
        for i in range(40, 75, 5):
            for k in range(20, 55, 5):
                r = estimate_portfolio(capital, b / 100, i / 100, k / 100, rate)
                cand = {"b": b, "i": i, "k": k, **r}
                if r["vol"] >= target_vol and (best is None or r["net"] > best["net"]):
                    best = cand
                if (fallback is None or r["vol"] > fallback["vol"]
                        or (r["vol"] == fallback["vol"] and r["net"] > fallback["net"])):
                    fallback = cand
    return {"pick": best or fallback, "reached": best is not None}


# ═══════════════════════════════════════════════════════════════════════════
# 페르소나 집단 생성 (층화 샘플링)
# ═══════════════════════════════════════════════════════════════════════════

CAPITAL_STRATA = [  # (라벨, 최소, 최대) — 로그 스케일 구간
    ("초소액", 1_000_000, 5_000_000),
    ("소액",   5_000_000, 20_000_000),
    ("표준",   20_000_000, 60_000_000),
    ("중액",   60_000_000, 150_000_000),
    ("고액",   150_000_000, 500_000_000),
    ("자산가", 500_000_000, 2_000_000_000),
]
TARGET_STRATA = [  # (라벨, 목표거래량 후보 풀 [억])
    ("목표없음", [0]),
    ("소목표",   [1, 2, 3, 5, 8]),
    ("중목표",   [10, 15, 20, 30, 50]),
    ("대목표",   [70, 100, 150, 200]),
]
EXPERIENCE = ["입문 (그리드 첫 경험)", "초급 (용어만 아는 수준)",
              "중급 (그리드 운용 경험)", "고급 (리워드 구조 숙지)"]
TEMPERAMENT = ["신중형", "균형형", "공격형", "리워드헌터형", "오조작형"]

SURNAMES = "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노"
GIVEN = ["민준", "서연", "지우", "하은", "도윤", "지호", "수아", "예준", "시우",
         "지민", "주원", "하준", "지유", "채원", "준서", "은우", "다은", "건우",
         "유나", "현우", "서아", "태호", "영자", "순자", "철수", "영희", "광수"]


@dataclass
class Persona:
    pid: int
    name: str
    capital: int
    capital_label: str
    target_eok: int           # 목표거래량 (억), 0=자동
    target_label: str
    prev_month_vol: float     # 직전월 거래량 (리워드율 결정)
    experience: str
    temperament: str


@dataclass
class Review:
    persona: Persona
    scores: dict = field(default_factory=dict)   # 5개 항목 1~5
    blockers: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    verdict: str = ""
    journey: list = field(default_factory=list)


def generate_personas(n: int = 120, seed: int = 42) -> list[Persona]:
    """층화 샘플링: 자본 6계층 × 목표 4계층 = 24셀을 균등하게 채움."""
    rng = random.Random(seed)
    personas = []
    cells = [(c, t) for c in CAPITAL_STRATA for t in TARGET_STRATA]
    per_cell = max(1, n // len(cells))
    pid = 0
    for (cap_label, lo, hi), (tgt_label, pool) in cells:
        for _ in range(per_cell):
            pid += 1
            capital = int(rng.uniform(lo, hi) // 100_000 * 100_000)
            target = rng.choice(pool)
            # 직전월 거래량: 60%는 신규(0), 40%는 자본의 1~50배 거래 이력
            prev_vol = 0.0 if rng.random() < 0.6 else capital * rng.uniform(1, 50)
            personas.append(Persona(
                pid=pid,
                name=rng.choice(SURNAMES) + rng.choice(GIVEN),
                capital=capital, capital_label=cap_label,
                target_eok=target, target_label=tgt_label,
                prev_month_vol=prev_vol,
                experience=rng.choice(EXPERIENCE),
                temperament=rng.choice(TEMPERAMENT),
            ))
    # 잔여분: 무작위 셀에서 추가 (모순 페르소나 — 경계 테스터 강제 포함)
    while len(personas) < n:
        pid += 1
        personas.append(Persona(
            pid=pid, name=rng.choice(SURNAMES) + rng.choice(GIVEN),
            capital=1_000_000, capital_label="초소액",
            target_eok=200, target_label="대목표",
            prev_month_vol=0.0,
            experience="입문 (그리드 첫 경험)", temperament="오조작형",
        ))
    return personas


# ═══════════════════════════════════════════════════════════════════════════
# 사용 여정 시뮬레이션 + 규칙 기반 리뷰
# ═══════════════════════════════════════════════════════════════════════════

def simulate_journey(p: Persona) -> Review:
    rv = Review(persona=p)
    rate = reward_rate(p.prev_month_vol)
    scores = {"clarity": 4, "trust": 4, "usability": 4, "guidance": 4, "decision": 4}

    # STEP 1~2: 진입·설정
    rv.journey.append(f"자본 {p.capital:,}원 입력, 직전월 거래량 {p.prev_month_vol:,.0f}원")
    if "입문" in p.experience:
        scores["clarity"] -= 1  # σ·존·간격 용어 진입장벽
        rv.suggestions.append("입문자용 용어 툴팁/온보딩 (1σ·내부존·간격 설명)")

    # STEP 3: 목표 설정 → 자동 추천
    if p.target_eok > 0:
        rec = recommend_params(p.capital, p.target_eok * 1e8, rate)
        pick, reached = rec["pick"], rec["reached"]
        rv.journey.append(
            f"목표 {p.target_eok}억 → 추천 b{pick['b']}/i{pick['i']}/k{pick['k']}"
            f" 예상 {pick['vol']/1e8:.1f}억 ({'달성' if reached else '미달'})")
        if not reached:
            gap = pick["vol"] / (p.target_eok * 1e8)
            if gap < 0.3:
                scores["guidance"] -= 2
                rv.blockers.append(
                    f"목표 {p.target_eok}억 대비 최대 {pick['vol']/1e8:.1f}억 "
                    f"({gap*100:.0f}%) — 필요 자본 역산 안내 없음")
                rv.suggestions.append("목표 미달 시 '필요 자본 약 X원' 역산 표시")
            else:
                scores["guidance"] -= 1
        est = pick
    else:
        est = estimate_portfolio(p.capital, 0.6, 0.6, 0.3, rate)
        rv.journey.append(f"자동 모드 — 기본 배분으로 예상 {est['vol']/1e8:.1f}억")

    # STEP 4: 추정치 신뢰성 검증
    if est["min_cap_per_bot"] < MIN_ORDER_KRW:
        scores["trust"] -= 2
        scores["decision"] -= 2
        rv.blockers.append(
            f"봇당 자본 {est['min_cap_per_bot']:,.0f}원 < 최소주문 {MIN_ORDER_KRW:,}원"
            " — 실제 주문 불가인데 UI는 정상 추정치 표시")
        rv.suggestions.append("봇당 자본이 최소주문금액 미만이면 경고 + 봇 수 자동 축소")
    if est["net"] < 0:
        scores["trust"] -= 1
        rv.blockers.append("월 순익 추정 음수 — 운용 의미 없음 경고 부재")
    monthly_pct = est["net"] / max(est["investable"], 1) * 100
    if monthly_pct > 5:
        scores["trust"] -= 1
        rv.suggestions.append(
            f"월 {monthly_pct:.1f}% 추정 — 과대 추정 우려, 보수/낙관 구간 병기 필요")

    # 리워드 이해도
    if p.prev_month_vol == 0 and p.target_eok >= 10:
        rv.suggestions.append(
            "신규 계정은 첫 달 리워드율 0.003% — 목표 거래량의 리워드 기여가 "
            "차월부터임을 명시 필요")
        scores["clarity"] -= 1

    # STEP 5: 조작성 (성향 반영)
    if p.temperament == "오조작형":
        scores["usability"] -= 1
        rv.suggestions.append("입력 실수 방지: 자본 입력칸 천단위 콤마 표시")
    if p.temperament == "리워드헌터형" and rate < 0.00015:
        rv.suggestions.append("리워드 티어 시뮬레이터: '다음 티어까지 X억 남음' 표시")

    # STEP 6: 최종 의사결정
    if rv.blockers:
        rv.verdict = "이탈" if scores["decision"] <= 2 else "보류"
    elif monthly_pct >= 1.0:
        rv.verdict = "봇 켠다"
    else:
        rv.verdict = "보류"
        rv.suggestions.append("저수익 구간(월 1% 미만)에서 대안 전략 제안 부재")

    rv.scores = {k: max(1, min(5, v)) for k, v in scores.items()}
    return rv


# ═══════════════════════════════════════════════════════════════════════════
# 집단지성 집계 리포트
# ═══════════════════════════════════════════════════════════════════════════

def aggregate(reviews: list[Review]) -> str:
    n = len(reviews)
    lines = []
    w = lines.append
    w("═" * 72)
    w(f"  🕵️ 미스터리쇼퍼 집단지성 리포트 — 페르소나 {n}명")
    w(f"  생성일: {date.today().isoformat()}")
    w("═" * 72)

    # 1. 점수 평균
    w("\n[ 1. 항목별 평균 점수 (5점 만점) ]")
    for key, label in [("clarity", "이해 용이성"), ("trust", "추정치 신뢰성"),
                       ("usability", "조작 편의성"), ("guidance", "추천·경고 유용성"),
                       ("decision", "의사결정 가능성")]:
        avg = sum(r.scores[key] for r in reviews) / n
        bar = "█" * int(avg * 4) + "░" * (20 - int(avg * 4))
        w(f"  {label:<10} {bar} {avg:.2f}")

    # 2. 최종 평결 분포
    w("\n[ 2. 최종 평결 분포 ]")
    verdicts = Counter(r.verdict for r in reviews)
    for v, cnt in verdicts.most_common():
        w(f"  {v:<6} {cnt:>4}명 ({cnt/n*100:.0f}%)")

    # 3. 세그먼트별 평결 (자본 계층 × 평결)
    w("\n[ 3. 자본 계층별 '봇 켠다' 비율 ]")
    for cap_label, *_ in CAPITAL_STRATA:
        seg = [r for r in reviews if r.persona.capital_label == cap_label]
        if not seg:
            continue
        on = sum(1 for r in seg if r.verdict == "봇 켠다")
        w(f"  {cap_label:<6} {on}/{len(seg)}명 ({on/len(seg)*100:.0f}%)")

    # 4. 블로커 빈도 (집단지성 핵심)
    w("\n[ 4. 블로커 빈도 — 사용을 막은 결정적 문제 ]")
    blocker_keys = Counter()
    for r in reviews:
        for b in r.blockers:
            key = b.split(" — ")[-1] if " — " in b else b[:40]
            blocker_keys[key] += 1
    if blocker_keys:
        for key, cnt in blocker_keys.most_common(10):
            w(f"  [{cnt:>3}명] {key}")
    else:
        w("  (없음)")

    # 5. 개선 제안 빈도 → 우선순위
    w("\n[ 5. 개선과제 우선순위 (2명 이상 공통 제안) ]")
    sug = Counter()
    for r in reviews:
        for s in set(r.suggestions):
            sug[s] += 1
    rank = 0
    for s, cnt in sug.most_common():
        if cnt < 2:
            continue
        rank += 1
        w(f"  P{rank}. [{cnt:>3}명] {s}")

    # 6. 대표 페르소나 여정 샘플
    w("\n[ 6. 대표 여정 샘플 (셀별 1명) ]")
    seen = set()
    for r in reviews:
        cell = (r.persona.capital_label, r.persona.target_label)
        if cell in seen:
            continue
        seen.add(cell)
        p = r.persona
        w(f"  · {p.name} ({p.capital_label}/{p.target_label}, {p.temperament})"
          f" → {r.verdict}")
        for j in r.journey[:2]:
            w(f"      {j}")
        if len(seen) >= 8:
            break

    w("\n" + "═" * 72)
    return "\n".join(lines)


def run(n: int = 120, seed: int = 42) -> str:
    personas = generate_personas(n, seed)
    reviews = [simulate_journey(p) for p in personas]
    report = aggregate(reviews)
    out = Path(__file__).resolve().parent.parent / "reports"
    out.mkdir(exist_ok=True)
    path = out / f"shopper_review_{date.today().isoformat()}.txt"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n리포트 저장: {path}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120, help="페르소나 수 (기본 120)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    run(args.n, args.seed)
