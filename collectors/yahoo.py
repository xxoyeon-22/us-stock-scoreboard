"""Yahoo Finance 차트 (키 없음, 비공식). 일별 가격 이력용 — 막히거나 실패하면 None 을 돌려주고 호출한 쪽이 이전 값을 유지한다."""
import time
from urllib.parse import quote

import requests

UA = "Mozilla/5.0"


def chart(symbol: str, range_: str = "5y", interval: str = "1d", retries: int = 3):
    """[(unix_ts, 수정종가)] 오래된 순. 종목이 없으면 [], 실패(차단·오류)면 None."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol.replace('.', '-'), safe='^=')}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params={"range": range_, "interval": interval}, headers={"User-Agent": UA}, timeout=25)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 404:
            return []
        if r.status_code == 429:
            time.sleep(6 * (attempt + 1))
            continue
        if r.status_code != 200:
            time.sleep(2 * (attempt + 1))
            continue
        try:
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                return []
            d = res[0]
            ind = d.get("indicators") or {}
            close = ((ind.get("adjclose") or [{}])[0].get("adjclose")) or ind["quote"][0]["close"]
            return [(t, c) for t, c in zip(d.get("timestamp") or [], close) if c is not None]
        except (ValueError, KeyError, IndexError, TypeError):
            return None
    return None
