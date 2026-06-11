"""σ 배수 스윕 — 월별 walk-forward로 박스 적중률/폭/이탈일 비교 (분석용 임시 스크립트)."""
import json, math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import numpy as np
from utils.statistics import compute_log_returns, ewma_daily_volatility

ROOT = Path(__file__).parent.parent

def load(name):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))

def month_ends(history):
    """각 월 마지막 일봉 인덱스."""
    out = []
    for i in range(len(history) - 1):
        if history[i]["date"][:7] != history[i + 1]["date"][:7]:
            out.append(i)
    return out

def sweep(history, pairs, label):
    closes = [c["close"] for c in history]
    me = month_ends(history)
    print(f"\n=== {label} ({len(me)} month-ends) ===")
    print(f"{'su/sd':>12} {'ok':>6} {'okx':>6} {'width%':>8} {'upX_d':>6} {'dnX_d':>6} {'cont%':>6}")
    for su, sd in pairs:
        ok = okx = total = 0
        widths = []
        up_days = dn_days = inside_days = all_days = 0
        for i in me:
            if i < 120:
                continue
            ref = closes[i]
            rets = compute_log_returns(closes[max(0, i - 89):i + 1])
            dsig = ewma_daily_volatility(rets, span=60)
            if dsig <= 0:
                continue
            sh = dsig * math.sqrt(30)
            u = ref * math.exp(su * sh)
            l = ref * math.exp(-sd * sh)
            # 다음 달 일봉
            nm = []
            j = i + 1
            tgt = history[i + 1]["date"][:7] if i + 1 < len(history) else None
            while j < len(history) and history[j]["date"][:7] == tgt:
                nm.append(history[j]); j += 1
            if len(nm) < 15:
                continue
            total += 1
            widths.append((u - l) / l * 100)
            ins = sum(1 for c in nm if l <= c["close"] <= u)
            upd = sum(1 for c in nm if c["close"] > u)
            dnd = sum(1 for c in nm if c["close"] < l)
            up_days += upd; dn_days += dnd; inside_days += ins; all_days += len(nm)
            if ins == len(nm):
                ok += 1
            if ins / len(nm) >= 0.9:
                okx += 1
        if total:
            print(f"{su:>5.2f}/{sd:<5.2f} {ok:>4}/{total:<3} {okx:>4}/{total:<3} "
                  f"{np.mean(widths):>7.1f} {up_days:>6} {dn_days:>6} "
                  f"{inside_days / all_days * 100:>5.1f}")

btc = load("btc_history.json")
usdt = load("usdt_history.json")

# 1σ 후보 + 2σ 후보
btc_pairs = [(1.1, 1.2), (1.0, 1.1), (0.9, 1.0), (1.0, 1.2),
             (2.2, 2.4), (1.8, 2.0), (1.65, 1.8), (1.5, 1.65), (1.4, 1.5)]
usdt_pairs = [(1.0, 1.1), (0.9, 1.0), (0.8, 0.9), (0.7, 0.8),
              (2.0, 2.2), (1.5, 1.65), (1.3, 1.4), (1.2, 1.3), (1.0, 1.0)]
sweep(btc, btc_pairs, "BTC/KRW")
sweep(usdt, usdt_pairs, "USDT/KRW")
