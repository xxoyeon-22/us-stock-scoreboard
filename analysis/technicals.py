"""가격 이력으로 계산하는 지표: 이동평균, 최대 낙폭(MDD), 변동성. 입력은 오래된 순 종가 리스트."""
from math import sqrt
from statistics import pstdev


def technicals(closes: list[float]) -> dict:
    out: dict = {}
    n = len(closes)
    if n < 50:
        return out
    ma50 = sum(closes[-50:]) / 50
    out["ma50"] = ma50
    if n >= 200:
        ma200 = sum(closes[-200:]) / 200
        out["ma200"] = ma200
        out["ma200_gap"] = (closes[-1] / ma200 - 1) * 100          # 현재가가 200일선보다 몇 % 위(+)/아래(−)
        out["ma_cross"] = (ma50 / ma200 - 1) * 100                  # 50일선이 200일선보다 위면 골든크로스 상태(+)
    win = closes[-252:]
    peak, mdd = win[0], 0.0
    for c in win:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)
    out["mdd_1y"] = mdd * 100                                      # 최근 1년 최대 낙폭 (음수)
    rets = [win[i] / win[i - 1] - 1 for i in range(1, len(win)) if win[i - 1]]
    if len(rets) >= 30:
        out["vol_1y"] = pstdev(rets) * sqrt(252) * 100              # 연환산 변동성 %
    return {k: round(v, 2) for k, v in out.items()}


def daily_dated(points: list[tuple[int, float]], n: int = 460) -> list[tuple[str, float]]:
    """차트용 최근 n 거래일 [(날짜, 종가)] — 1년 차트에 200일선을 끝까지 그리려면 252+199 개 이상이 필요하다."""
    from datetime import datetime, timezone

    return [(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), round(c, 2)) for t, c in points][-n:]


def weekly_dated(points: list[tuple[int, float]]) -> list[tuple[str, float]]:
    """차트용 주간 [(그 주 마지막 거래일, 종가)] (최대 5년)"""
    from datetime import datetime, timezone

    by_week = {}
    for ts, c in points:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        y, w, _ = dt.isocalendar()
        by_week[(y, w)] = (dt.strftime("%Y-%m-%d"), round(c, 2))
    return [by_week[k] for k in sorted(by_week)]


def weekly(points: list[tuple[int, float]]) -> list[tuple[str, float]]:
    """[(unix_ts, 종가)] → [(ISO 주 'YYYY-Www', 그 주 마지막 종가)]"""
    from datetime import datetime, timezone

    by_week: dict[str, float] = {}
    for ts, c in points:
        y, w, _ = datetime.fromtimestamp(ts, tz=timezone.utc).isocalendar()
        by_week[f"{y}-W{w:02d}"] = round(c, 2)
    return sorted(by_week.items())
