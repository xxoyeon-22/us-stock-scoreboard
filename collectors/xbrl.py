"""SEC XBRL 재무 공시에서 주식 희석·주식 보상(SBC)·자사주 매입을 계산한다 (무료, 연락처 User-Agent 필요).

- 희석: 연간 희석 가중평균 주식 수의 전년 대비 변화 % (늘면 기존 주주 몫이 줄어듦)
- SBC: 주식 보상 비용 ÷ 시가총액 %
- 자사주 매입: 자사주 매입 금액 ÷ 시가총액 %
"""
from datetime import date

import requests

DILUTED = "WeightedAverageNumberOfDilutedSharesOutstanding"
BASIC = "WeightedAverageNumberOfSharesOutstandingBasic"
SBC = "ShareBasedCompensation"
BUYBACK = "PaymentsForRepurchaseOfCommonStock"


def ticker_map(edgar) -> dict:
    data = edgar.get("https://www.sec.gov/files/company_tickers.json").json()
    return {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}


def concept(edgar, cik: int, tag: str):
    try:
        return edgar.get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json").json()
    except requests.HTTPError:
        return None   # 이 회사는 그 항목을 쓰지 않음


def annual_series(c: dict | None, unit: str) -> list[tuple[str, float]]:
    """10-K 의 연간(약 1년 기간) 값만 모아 [(회계연도 종료일, 값)] 오래된 순으로. 같은 종료일은 나중에 공시한 것을 쓴다."""
    by_end: dict[str, dict] = {}
    for f in ((c or {}).get("units") or {}).get(unit, []):
        if f.get("fp") != "FY" or not str(f.get("form", "")).startswith("10-K") or not f.get("end"):
            continue
        if f.get("start"):
            try:
                days = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
            except ValueError:
                continue
            if not 340 <= days <= 380:
                continue
        cur = by_end.get(f["end"])
        if cur is None or f.get("filed", "") > cur.get("filed", ""):
            by_end[f["end"]] = f
    return sorted((e, float(f["val"])) for e, f in by_end.items())


def derive(diluted, basic, sbc, buyback, mcap_usd: float | None, today: date | None = None) -> dict:
    today = today or date.today()
    out: dict = {}
    shares = diluted if len(diluted) >= 2 else basic
    if len(shares) >= 2 and shares[-2][1] > 0 and (today - date.fromisoformat(shares[-1][0])).days < 600:
        out["share_change"] = round((shares[-1][1] / shares[-2][1] - 1) * 100, 2)
    if mcap_usd and mcap_usd > 0:
        if sbc and (today - date.fromisoformat(sbc[-1][0])).days < 600 and sbc[-1][1] >= 0:
            out["sbc_yield"] = round(sbc[-1][1] / mcap_usd * 100, 3)
        if buyback and (today - date.fromisoformat(buyback[-1][0])).days < 600 and buyback[-1][1] >= 0:
            out["buyback_yield"] = round(buyback[-1][1] / mcap_usd * 100, 3)
        elif not buyback:
            out["buyback_yield"] = 0.0           # 매입 항목을 안 쓰는 회사는 매입 없음으로 본다
    return out


def collect(edgar, cik: int, mcap_usd: float | None) -> dict:
    d = annual_series(concept(edgar, cik, DILUTED), "shares")
    b = annual_series(concept(edgar, cik, BASIC), "shares") if len(d) < 2 else []
    s = annual_series(concept(edgar, cik, SBC), "USD")
    r = annual_series(concept(edgar, cik, BUYBACK), "USD")
    return derive(d, b, s, r, mcap_usd)
