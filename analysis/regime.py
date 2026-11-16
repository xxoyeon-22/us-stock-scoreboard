"""시장 국면 보정 계수 (0.8 ~ 1.1).

점수는 단순하고 설명 가능하게 둔다: 금리·물가·변동성·고용·지수 추세를 각각 −2~+1 점으로 매겨 더하고,
합계 1점당 계수를 0.03 씩 움직인다. 우호적이면 최대 1.1, 위험하면 최소 0.8 이다.
이 규칙은 출발점으로 잡은 임의값이며, 백테스트로 검증된 것이 아니다.
"""

PER_POINT = 0.03


def regime(x: dict, lo: float = 0.8, hi: float = 1.1) -> dict:
    """x: vix, curve_10y2y, cpi_yoy, cpi_yoy_3m_ago, y10, y10_3m_ago, spx_gap200, unemp, unemp_6m_ago (없으면 건너뜀)"""
    factors = []

    def add(key, label, value, pts, note):
        factors.append({"key": key, "label": label, "value": None if value is None else round(value, 2), "points": pts, "note": note})

    v = x.get("vix")
    if v is not None:
        pts, note = (1, "시장이 차분함") if v < 15 else (0, "보통 수준") if v < 20 else (-1, "불안이 커지는 구간") if v < 28 else (-2, "공포 구간")
        add("vix", "VIX (공포지수)", v, pts, note)
    c = x.get("curve_10y2y")
    if c is not None:
        pts, note = (1, "장단기 금리차가 충분히 양수") if c > 0.5 else (0, "금리차가 거의 없음") if c >= 0 else (-1, "장단기 금리 역전 (경기 둔화 신호로 읽힘)")
        add("curve", "장단기 금리차 (10년−2년, %p)", c, pts, note)
    if x.get("cpi_yoy") is not None:
        now, prev = x["cpi_yoy"], x.get("cpi_yoy_3m_ago")
        pts = 0
        notes = []
        if prev is not None:
            d = now - prev
            if d <= -0.3:
                pts += 1; notes.append("물가 상승률이 내려가는 중")
            elif d >= 0.3:
                pts -= 1; notes.append("물가 상승률이 오르는 중")
        if now > 4:
            pts -= 1; notes.append("물가 상승률이 4% 위")
        add("cpi", "소비자물가 상승률 (전년 대비 %)", now, pts, " · ".join(notes) or "큰 변화 없음")
    if x.get("y10") is not None and x.get("y10_3m_ago") is not None:
        d = x["y10"] - x["y10_3m_ago"]
        pts, note = (-1, "3개월 새 금리가 크게 오름 (성장주에 부담)") if d >= 0.5 else (1, "3개월 새 금리가 크게 내림") if d <= -0.5 else (0, "금리 변화가 크지 않음")
        add("y10", "미 국채 10년 금리 (%)", x["y10"], pts, note)
    g = x.get("spx_gap200")
    if g is not None:
        pts, note = (1, "S&P 500 이 200일선보다 3% 넘게 위") if g > 3 else (-1, "S&P 500 이 200일선보다 3% 넘게 아래") if g < -3 else (0, "200일선 근처")
        add("trend", "S&P 500 의 200일선 대비 (%)", g, pts, note)
    if x.get("unemp") is not None and x.get("unemp_6m_ago") is not None:
        d = x["unemp"] - x["unemp_6m_ago"]
        pts, note = (-1, "6개월 새 실업률이 0.5%p 이상 오름") if d >= 0.5 else (0, "실업률 큰 변화 없음")
        add("unemp", "실업률 (%)", x["unemp"], pts, note)
    total = sum(f["points"] for f in factors)
    coef = round(max(lo, min(hi, 1.0 + PER_POINT * total)), 3) if factors else 1.0
    label = "우호적" if coef >= 1.05 else "경계" if coef <= 0.92 else "중립"
    return {"coef": coef, "points": total, "label": label, "factors": factors, "rule": f"합계 1점당 계수 {PER_POINT:+.2f}, 범위 {lo}~{hi}"}
