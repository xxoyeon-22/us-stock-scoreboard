"""Finnhub REST 클라이언트 — 호출 간격 제한과 429 재시도를 포함한다."""
import time

import requests

from . import USER_AGENT


class FinnhubError(RuntimeError):
    pass


class Finnhub:
    def __init__(self, api_key: str, base_url: str, requests_per_minute: int = 45):
        if not api_key:
            raise FinnhubError("FINNHUB_API_KEY 가 없습니다.")
        self.base_url = base_url.rstrip("/")
        self.interval = 60.0 / max(1, requests_per_minute)
        self._last = 0.0
        self.calls = 0
        self.session = requests.Session()
        # 키는 URL 이 아니라 헤더로 보낸다 (로그에 남지 않게).
        self.session.headers.update({"X-Finnhub-Token": api_key, "User-Agent": USER_AGENT})

    def get(self, path: str, **params):
        for attempt in range(5):
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                r = self.session.get(f"{self.base_url}{path}", params=params, timeout=20)
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise FinnhubError(f"{path}: 인증 실패 또는 요금제 밖의 엔드포인트 (HTTP {r.status_code})")
            r.raise_for_status()
            return r.json()
        raise FinnhubError(f"{path}: 재시도 후에도 실패")

    # --- 엔드포인트 ---
    def metrics(self, symbol: str) -> dict:
        return (self.get("/stock/metric", symbol=symbol, metric="all") or {}).get("metric") or {}

    def earnings(self, symbol: str) -> list:
        return self.get("/stock/earnings", symbol=symbol) or []

    def quote(self, symbol: str) -> dict:
        return self.get("/quote", symbol=symbol) or {}

    def insider_sentiment(self, symbol: str, frm: str, to: str) -> dict:
        return self.get("/stock/insider-sentiment", symbol=symbol, **{"from": frm, "to": to}) or {}

    def recommendation(self, symbol: str) -> list:
        return self.get("/stock/recommendation", symbol=symbol) or []

    def market_news(self, category: str = "general") -> list:
        return self.get("/news", category=category) or []
