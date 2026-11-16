"""SEC EDGAR 13F 수집.

13F 의 한계: 분기 종료 후 최대 45일 지연, 매수(롱) 포지션만 공개, 옵션은 별도 행.
SEC 는 연락처가 든 User-Agent 를 요구한다 → 환경변수 SEC_USER_AGENT 로 받는다.
"""
import json
import os
import threading
import time
import xml.etree.ElementTree as ET

import requests

from . import ROOT

CUSIP_CACHE = ROOT / "config" / "cusip_map.json"
FIGI_URL = "https://api.openfigi.com/v3/mapping"


class EdgarError(RuntimeError):
    pass


class Edgar:
    def __init__(self, user_agent: str | None = None):
        ua = user_agent or os.environ.get("SEC_USER_AGENT", "")
        if "@" not in ua:
            raise EdgarError("SEC_USER_AGENT 가 없습니다 (예: 'my-app name@example.com'). SEC 는 연락처 없는 자동 요청을 거부합니다.")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self._last = 0.0
        self._lock = threading.Lock()

    def get(self, url: str) -> requests.Response:
        for attempt in range(4):
            with self._lock:                               # 여러 스레드가 같이 써도 초당 4회 이하 (SEC 한도 10회)
                wait = self._last + 0.25 - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                r = self.session.get(url, timeout=30)
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 503):
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 403:
                raise EdgarError("SEC 가 요청을 거부했습니다 (403). SEC_USER_AGENT 를 확인하세요.")
            r.raise_for_status()
            return r
        raise EdgarError(f"재시도 후에도 실패: {url}")

    def filings_13f(self, cik: str, count: int = 2) -> list[dict]:
        """최근 13F-HR 을 보고 분기 기준 최신순으로. 같은 분기는 가장 늦게 낸 것(정정 포함)을 쓴다."""
        data = self.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json").json()
        r = data["filings"]["recent"]
        by_period: dict[str, dict] = {}
        for form, acc, filed, period in zip(r["form"], r["accessionNumber"], r["filingDate"], r["reportDate"]):
            if form not in ("13F-HR", "13F-HR/A") or not period:
                continue
            cur = by_period.get(period)
            # 정정 공시(/A)는 일부 종목만 담는 경우가 있어 원본을 우선한다
            if cur is None or (cur["form"] != "13F-HR" and form == "13F-HR"):
                by_period[period] = {"form": form, "accession": acc, "filed": filed, "period": period}
        return [by_period[p] for p in sorted(by_period, reverse=True)[:count]]

    def holdings(self, cik: str, accession: str) -> list[dict]:
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
        items = self.get(base + "index.json").json()["directory"]["item"]
        xmls = [i for i in items if i["name"].lower().endswith(".xml") and "primary_doc" not in i["name"].lower()]
        if not xmls:
            raise EdgarError(f"정보 테이블 XML 을 찾지 못했습니다: {accession}")
        xmls.sort(key=lambda i: int(i.get("size") or 0), reverse=True)
        return parse_info_table(self.get(base + xmls[0]["name"]).content)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_info_table(xml: bytes | str) -> list[dict]:
    """정보 테이블 → CUSIP(+옵션 구분)별로 합친 보유 목록."""
    root = ET.fromstring(xml)
    agg: dict[tuple, dict] = {}
    for node in root.iter():
        if _local(node.tag) != "infoTable":
            continue
        f = {}
        for el in node.iter():
            if el.text and el.text.strip():
                f.setdefault(_local(el.tag), el.text.strip())
        cusip = (f.get("cusip") or "").upper()
        if not cusip:
            continue
        put_call = (f.get("putCall") or "").upper() or None
        try:
            value = float(f.get("value") or 0)
            shares = float(f.get("sshPrnamt") or 0)
        except ValueError:
            continue
        key = (cusip, put_call)
        row = agg.setdefault(key, {"cusip": cusip, "issuer": f.get("nameOfIssuer", ""), "cls": f.get("titleOfClass", ""),
                                   "put_call": put_call, "value": 0.0, "shares": 0.0,
                                   "is_shares": (f.get("sshPrnamtType") or "SH").upper() == "SH"})
        row["value"] += value
        row["shares"] += shares
    return sorted(agg.values(), key=lambda r: r["value"], reverse=True)


def compare(current: list[dict], previous: list[dict] | None, threshold: float = 0.05) -> tuple[list[dict], list[dict]]:
    """이번 분기 보유에 action(new/add/hold/trim)을 붙이고, 전량 매도(exit) 목록을 따로 돌려준다."""
    total = sum(r["value"] for r in current) or 1.0
    prev = {(r["cusip"], r["put_call"]): r for r in (previous or [])}
    out = []
    for r in current:
        p = prev.get((r["cusip"], r["put_call"]))
        row = {**r, "pct": r["value"] / total * 100}
        if previous is None:
            row["action"], row["change_pct"] = None, None
        elif p is None or p["shares"] <= 0:
            row["action"], row["change_pct"] = "new", None
        else:
            ch = (r["shares"] - p["shares"]) / p["shares"]
            row["change_pct"] = ch * 100
            row["action"] = "add" if ch > threshold else "trim" if ch < -threshold else "hold"
        out.append(row)
    cur_keys = {(r["cusip"], r["put_call"]) for r in current}
    prev_total = sum(r["value"] for r in (previous or [])) or 1.0
    exits = [{**p, "action": "exit", "change_pct": -100.0, "pct": p["value"] / prev_total * 100}
             for k, p in prev.items() if k not in cur_keys]
    exits.sort(key=lambda r: r["value"], reverse=True)
    return out, exits


# --- CUSIP → 티커 (OpenFIGI, 키 없이 분당 25회·요청당 10건) ---

def load_cusip_cache() -> dict:
    try:
        return json.loads(CUSIP_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def map_cusips(cusips: list[str], cache: dict | None = None, max_requests: int = 80) -> dict:
    """{cusip: ticker|None}. 못 찾은 것도 None 으로 캐시해 다시 묻지 않는다."""
    cache = load_cusip_cache() if cache is None else cache
    todo = [c for c in dict.fromkeys(cusips) if c not in cache]
    sent = 0
    for i in range(0, len(todo), 10):
        if sent >= max_requests:
            break
        batch = todo[i:i + 10]
        body = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]
        try:
            r = requests.post(FIGI_URL, json=body, timeout=30)
            if r.status_code == 429:
                time.sleep(30)
                r = requests.post(FIGI_URL, json=body, timeout=30)
            r.raise_for_status()
            results = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[13f] 티커 매핑 중단: {e}")
            break
        sent += 1
        for cusip, res in zip(batch, results):
            ticker = None
            for d in res.get("data") or []:
                if d.get("ticker") and d.get("marketSector") == "Equity":
                    ticker = d["ticker"].replace("/", ".")
                    break
            cache[cusip] = ticker
        time.sleep(2.6)
    if sent:
        CUSIP_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    return cache
