"""S&P 500 구성 종목 (티커, 회사명, GICS 섹터)."""
import csv
import io

import requests

from . import ROOT, USER_AGENT


def load_universe(settings: dict) -> list[dict]:
    cfg = settings["universe"]
    cache = ROOT / cfg["cache"]
    text = None
    try:
        r = requests.get(cfg["url"], headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        text = r.text
        cache.write_text(text, encoding="utf-8")
    except requests.RequestException as e:
        if not cache.exists():
            raise RuntimeError(f"유니버스 목록을 받지 못했고 캐시도 없습니다: {e}") from e
        print(f"[universe] 내려받기 실패, 캐시 사용: {e}")
        text = cache.read_text(encoding="utf-8")

    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "name": (row.get("Security") or symbol).strip(),
            "sector": (row.get("GICS Sector") or "기타").strip(),
        })
    if len(rows) < 400:
        raise RuntimeError(f"유니버스 종목 수가 비정상입니다: {len(rows)}")
    return rows
