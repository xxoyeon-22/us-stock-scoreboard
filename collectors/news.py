"""뉴스 헤드라인 수집 — RSS + Finnhub 시장 뉴스.

헤드라인, 짧은 요약, 원문 링크만 저장한다. 기사 본문은 받지 않는다.
"""
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from . import USER_AGENT

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
NAME_SUFFIX_RE = re.compile(
    r"[,.]?\s+(Inc|Incorporated|Corp|Corporation|Company|Co|plc|PLC|Ltd|Limited|N\.V|S\.A|"
    r"Holdings?|Group|Technologies|Technology|International|Enterprises|Industries|& Co|"
    r"\(The\)|\(Class [A-C]\)|Class [A-C])\.?$"
)
# 회사명에서 뽑았을 때 일반 단어와 겹쳐 오탐이 많은 별칭
ALIAS_STOP = {
    "Target", "Gap", "Ball", "Match", "Dover", "Block", "Gen", "Key", "Best", "Global", "First",
    "American", "National", "General", "United", "Public", "Southern", "Western", "Digital",
    "Progressive", "Travelers", "Principal", "Republic", "Regions", "Discover", "Visa", "Crown",
    "Fox", "News", "Loews", "Ross", "Waters", "Cooper", "Hess", "Linde", "Nordson", "Realty",
    "Mosaic", "Garmin", "Corning", "Prologis", "Paramount", "Carrier", "Dominion", "Entergy",
    "Fortive", "Aptiv", "Viatris", "Assurant", "Invesco", "Insulet", "Incyte", "Equinix",
    "Universal", "Allegion", "Ventas", "Welltower", "Chubb", "Trimble", "Teradyne", "Hologic",
}


def _clean(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    text = WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", html.unescape(text)))).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _make_item(title, url, summary, published, source, source_id, limit) -> dict | None:
    title = _clean(title)
    url = (url or "").strip()
    if not title or not url.startswith(("http://", "https://")):
        return None
    summary = _clean(summary, limit)
    if summary == title:
        summary = ""
    return {
        "id": hashlib.sha1(_norm_title(title).encode()).hexdigest()[:16],
        "title": title,
        "summary": summary,
        "url": url,
        "source": source,
        "source_id": source_id,
        "published": published.isoformat(timespec="seconds") if published else None,
        "tickers": [],
    }


def parse_feed(xml_text: str | bytes, source: str, source_id: str, limit: int) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        f = {"title": None, "link": None, "summary": None, "date": None}
        for child in node:
            name = _local(child.tag)
            if name == "title":
                f["title"] = child.text
            elif name == "link":
                f["link"] = f["link"] or child.get("href") or child.text
            elif name in ("description", "summary") and not f["summary"]:
                f["summary"] = child.text
            elif name in ("pubdate", "published", "updated", "date") and not f["date"]:
                f["date"] = child.text
        item = _make_item(f["title"], f["link"], f["summary"], _parse_date(f["date"]), source, source_id, limit)
        if item:
            items.append(item)
    return items


def fetch_rss(media: dict) -> tuple[list[dict], dict]:
    """모든 활성 피드를 읽는다. (기사 목록, 피드별 상태) 를 돌려준다."""
    limit = media.get("summary_max_chars", 280)
    items, status = [], {}
    for feed in media.get("feeds", []):
        if not feed.get("enabled", True):
            continue
        try:
            r = requests.get(feed["url"], headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            got = parse_feed(r.content, feed["name"], feed["id"], limit)
            items.extend(got)
            status[feed["id"]] = {"ok": True, "count": len(got)}
        except Exception as e:  # 피드 하나가 죽어도 나머지는 계속
            status[feed["id"]] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return items, status


def from_finnhub(raw: list, limit: int) -> list[dict]:
    items = []
    for n in raw or []:
        ts = n.get("datetime")
        published = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        source = n.get("source") or "Finnhub"
        item = _make_item(n.get("headline"), n.get("url"), n.get("summary"), published, source, "finnhub", limit)
        if item:
            items.append(item)
    return items


# --- 티커 매핑 ---

def build_matcher(universe: list[dict], aliases: dict | None):
    """헤드라인에서 종목을 찾는 함수를 만든다. 회사명·별칭(대소문자 구분)과 $TICKER, (TICKER) 표기를 본다."""
    alias_to_symbol: dict[str, str] = {}
    symbols = {u["symbol"] for u in universe}
    for u in universe:
        name = u["name"]
        for _ in range(3):
            name = NAME_SUFFIX_RE.sub("", name).strip(" ,.")
        # 한 단어 이름은 4자 이상, 여러 단어 이름은 8자 이상일 때만 쓴다
        if name not in ALIAS_STOP and len(name) >= (8 if " " in name else 4):
            alias_to_symbol.setdefault(name, u["symbol"])
    for symbol, names in (aliases or {}).items():
        if symbol in symbols:
            for n in names:
                alias_to_symbol[n] = symbol
    if not alias_to_symbol:
        return lambda text: []
    pattern = "|".join(re.escape(a) for a in sorted(alias_to_symbol, key=len, reverse=True))
    alias_re = re.compile(rf"(?<![A-Za-z0-9])({pattern})(?![A-Za-z0-9])")
    ticker_re = re.compile(r"(?:\$([A-Z]{1,5}(?:\.[A-Z])?)\b|\((?:NYSE|NASDAQ|Nasdaq)?:?\s?([A-Z]{1,5}(?:\.[A-Z])?)\))")

    def match(text: str) -> list[str]:
        found = []
        for m in alias_re.finditer(text):
            found.append(alias_to_symbol[m.group(1)])
        for m in ticker_re.finditer(text):
            t = m.group(1) or m.group(2)
            if t in symbols:
                found.append(t)
        return sorted(set(found))

    return match


def merge_news(previous: list[dict], fresh: list[dict], media: dict, now: datetime | None = None) -> list[dict]:
    """이전 목록과 합치고, 같은 제목은 하나로 묶고, 보존 기간이 지난 것은 버린다."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=media.get("retention_days", 7))
    seen: dict[str, dict] = {}
    for item in list(previous or []) + list(fresh or []):
        if not item.get("published"):
            # 날짜가 없는 피드 항목은 처음 본 시각을 쓴다
            item["published"] = item.get("first_seen") or now.isoformat(timespec="seconds")
        item.setdefault("first_seen", now.isoformat(timespec="seconds"))
        old = seen.get(item["id"])
        if old:
            # 같은 사건(제목) — 먼저 본 것을 남기고 출처만 덧붙인다
            others = old.setdefault("also", [])
            if item["source"] != old["source"] and item["source"] not in others:
                others.append(item["source"])
            continue
        seen[item["id"]] = item
    out = []
    for item in seen.values():
        dt = _parse_date(item["published"])
        if dt and dt > now + timedelta(hours=1):
            dt = now  # 미래 시각으로 찍힌 피드 보정
            item["published"] = dt.isoformat(timespec="seconds")
        if dt and dt >= cutoff:
            out.append(item)
    out.sort(key=lambda x: x["published"], reverse=True)
    return out[: media.get("max_items", 400)]
