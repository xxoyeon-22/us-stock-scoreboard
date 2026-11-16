"""모멘텀 전략 백테스트.

검증하는 것은 종합 점수 전체가 아니라 **가격 모멘텀 단독**이다. 과거 시점의 재무·뉴스·투자자 자료를 무료로 구할 수 없어서
점수 전체를 되돌려 볼 수 없기 때문이다. 그래서 종합 점수의 실제 성과는 ‘전진 검증’(매일 상위 종목을 기록해 두고 이후 수익률 비교)으로 따로 쌓는다.

한계(화면에도 표시): 현재 S&P 500 편입 종목만 쓰므로 중간에 빠진 종목이 없는 ‘생존 편향’이 있어 성과가 좋게 나온다.
"""
from datetime import datetime
from math import sqrt
from statistics import pstdev


def _week_date(key: str) -> datetime:
    return datetime.strptime(f"{key}-4", "%G-W%V-%u")


def _stats(weekly_returns: list[float], curve: list[float]) -> dict:
    n = len(weekly_returns)
    if n < 8:
        return {}
    total = curve[-1] / curve[0] - 1
    years = n / 52
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total_pct": round(total * 100, 1), "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 1),
            "vol_pct": round(pstdev(weekly_returns) * sqrt(52) * 100, 1), "mdd_pct": round(mdd * 100, 1)}


def run_backtest(weekly: dict, bench: list, top_n: int = 50, lookback: int = 52, skip: int = 4, cost: float = 0.002) -> dict | None:
    """weekly: {티커: [(주키, 종가)]}, bench: [(주키, 지수 종가)] (S&P 500). 월말 주에 리밸런싱, 동일 비중."""
    keys = [k for k, _ in bench]
    if len(keys) < lookback + 20:
        return None
    px = {s: dict(v) for s, v in weekly.items() if len(v) >= lookback + 10}
    bpx = dict(bench)
    pos = {k: i for i, k in enumerate(keys)}
    month = [_week_date(k).strftime("%Y-%m") for k in keys]
    reb = [i for i in range(lookback, len(keys) - 1) if month[i] != month[i + 1]]
    if len(reb) < 6:
        return None

    strat, equal, spx = [100.0], [100.0], [100.0]
    s_ret, e_ret, b_ret = [], [], []
    dates = [month[reb[0]]]
    held: set = set()
    beat = months = 0
    for a, b in zip(reb, reb[1:] + [len(keys) - 1]):
        scores = {}
        for s, d in px.items():
            p0, p1, p2 = d.get(keys[a - lookback]), d.get(keys[a - skip]), d.get(keys[a])
            if p0 and p1 and p2:
                scores[s] = p1 / p0 - 1
        if len(scores) < top_n * 2:
            continue
        picks = set(sorted(scores, key=scores.get, reverse=True)[:top_n])
        turnover = 1 - len(picks & held) / top_n if held else 1.0
        held = picks
        pm_s = pm_e = pm_b = 1.0
        for w in range(a + 1, b + 1):
            def avg(group):
                r = [d[keys[w]] / d[keys[w - 1]] - 1 for s in group if (d := px[s]).get(keys[w]) and d.get(keys[w - 1])]
                return sum(r) / len(r) if r else 0.0
            rs, re_ = avg(picks), avg(scores)
            if w == a + 1:
                rs -= cost * turnover
            rb = bpx[keys[w]] / bpx[keys[w - 1]] - 1 if bpx.get(keys[w]) and bpx.get(keys[w - 1]) else 0.0
            s_ret.append(rs); e_ret.append(re_); b_ret.append(rb)
            strat.append(strat[-1] * (1 + rs)); equal.append(equal[-1] * (1 + re_)); spx.append(spx[-1] * (1 + rb))
            pm_s *= 1 + rs; pm_b *= 1 + rb
        months += 1
        beat += pm_s > pm_b
        dates.append(month[b])
    if months < 6:
        return None
    # 곡선은 월말 값만 보내 크기를 줄인다 (주간 곡선에서 리밸런싱 주 인덱스를 따라 뽑음)
    idx, i = [0], 0
    for a, b in zip(reb, reb[1:] + [len(keys) - 1]):
        i += b - a
        idx.append(min(i, len(strat) - 1))
    sample = lambda c: [round(c[j], 1) for j in idx]
    return {"top_n": top_n, "cost_pct_per_turnover": cost * 100, "start": dates[0], "end": dates[-1], "months": months,
            "beat_months_pct": round(beat / months * 100), "dates": dates[:len(idx)],
            "curves": {"momentum": sample(strat), "equal": sample(equal), "sp500": sample(spx)},
            "stats": {"momentum": _stats(s_ret, strat), "equal": _stats(e_ret, equal), "sp500": _stats(b_ret, spx)}}
