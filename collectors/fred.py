"""FRED 거시 지표 — 키 없이 받을 수 있는 CSV 주소를 쓴다 (브라우저 User-Agent 를 붙이면 오히려 느려져 기본값을 쓴다)."""
import time

import requests


def series(series_id: str, last: int = 600, retries: int = 3):
    """[(날짜 'YYYY-MM-DD', 값)] 오래된 순. 실패하면 None."""
    for attempt in range(retries):
        try:
            r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series_id}, timeout=45)
        except requests.RequestException:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code != 200:
            time.sleep(3 * (attempt + 1))
            continue
        out = []
        for line in r.text.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 2 or parts[1] in ("", "."):
                continue
            try:
                out.append((parts[0], float(parts[1])))
            except ValueError:
                continue
        return out[-last:]
    return None
