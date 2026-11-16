"""SEC 10-K 에서 ‘Item 1. Business’(사업 설명) 본문을 뽑는다. 해자(경쟁 우위) 요약 입력용."""
import html
import re

MAX_CHARS = 40_000
ITEM1 = re.compile(r"item\s*1\s*[\.\:\-–—]?\s*(?:and\s*2\s*[\.\:\-–—]?\s*)?business", re.I)
ITEM1A = re.compile(r"item\s*1a\s*[\.\:\-–—]?\s*risk\s*factors", re.I)
TAGS = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</(p|div|tr|li|h\d|br)\s*>|<br\s*/?>", "\n", raw)
    return SPACE.sub(" ", html.unescape(TAGS.sub(" ", raw)).replace("\xa0", " ")).strip()


def extract_item1(text: str, max_chars: int = MAX_CHARS) -> str:
    """차례(목차)에도 ‘Item 1. Business’ 가 나오므로, 다음 ‘Item 1A’ 까지의 구간이 가장 긴 후보를 본문으로 본다."""
    starts = [m.start() for m in ITEM1.finditer(text)]
    ends = [m.start() for m in ITEM1A.finditer(text)]
    best = ""
    for s in starts:
        e = next((x for x in ends if x > s + 200), None)
        seg = text[s:e] if e else text[s:s + max_chars]
        if len(seg) > len(best):
            best = seg
    return best[:max_chars].strip()


def latest_10k(edgar, cik: int) -> dict | None:
    data = edgar.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
    r = data["filings"]["recent"]
    for form, acc, filed, doc, period in zip(r["form"], r["accessionNumber"], r["filingDate"], r["primaryDocument"], r["reportDate"]):
        if form == "10-K":
            return {"accession": acc, "filed": filed, "doc": doc, "period": period}
    return None


def fetch_item1(edgar, cik: int, k: dict) -> str:
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{k['accession'].replace('-', '')}/{k['doc']}"
    return extract_item1(html_to_text(edgar.get(url).content.decode("utf-8", "ignore")))
