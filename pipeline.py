"""데이터 파이프라인 진입점.

  python pipeline.py news            뉴스 헤드라인만 갱신 (빠름, 10분마다)
  python pipeline.py scores          S&P 500 재무·시세 수집 + 점수 계산 (느림, 하루 1회)
  python pipeline.py auto            news 를 돌리고, 점수가 없거나 샘플이거나 RUN_SCORES=1 이면 scores 도
  python pipeline.py sample          네트워크 없이 가짜 데이터로 화면 확인용 파일 생성

결과는 web/data/*.json 에 쓴다 (정적 사이트가 그대로 읽는다).
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from analysis.scoring import score_universe
from collectors import ROOT, load_config, load_env
from collectors import news as news_mod
from collectors.finnhub import Finnhub, FinnhubError
from collectors.universe import load_universe

NY = ZoneInfo("America/New_York")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def out_dir(settings: dict):
    d = ROOT / settings["output_dir"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else None


def extract_metrics(raw_metric: dict, raw_earnings: list, weights: dict) -> dict:
    out = {}
    for comp in weights["components"].values():
        for name, spec in (comp.get("metrics") or {}).items():
            value = None
            if spec.get("source") == "metric":
                for key in spec.get("keys", []):
                    value = num(raw_metric.get(key))
                    if value is not None:
                        break
            elif spec.get("source") == "earnings":
                vals = [num(e.get("surprisePercent")) for e in (raw_earnings or [])[:4]]
                vals = [v for v in vals if v is not None]
                value = sum(vals) / len(vals) if vals else None
            out[name] = value
    return out


def public_weights(weights: dict) -> dict:
    """화면에서 지표 이름·가중치를 보여주기 위한 요약."""
    comps = {}
    for cid, c in weights["components"].items():
        comps[cid] = {
            "label": c.get("label", cid),
            "weight": c["weight"],
            "active": bool(c.get("metrics")),
            "metrics": {
                m: {"label": s.get("label", m), "unit": s.get("unit", ""), "higher_better": s.get("higher_better", True)}
                for m, s in (c.get("metrics") or {}).items()
            },
        }
    return {"components": comps, "regime": weights.get("regime", {})}


def finish_scores(scored: list[dict], weights: dict, settings: dict, sample: bool, started: datetime) -> dict:
    path = out_dir(settings) / "scores.json"
    prev = read_json(path) or {}
    today = now_utc().astimezone(NY).date().isoformat()
    prev_map = {}
    if not prev.get("sample") or sample:
        same_day = prev.get("score_date") == today
        for s in prev.get("stocks", []):
            prev_map[s["symbol"]] = s.get("prev_score") if same_day else s.get("score")
    for s in scored:
        s["prev_score"] = prev_map.get(s["symbol"])

    coverage = {}
    for cid, c in weights["components"].items():
        for m in (c.get("metrics") or {}):
            coverage[m] = sum(1 for s in scored if m in s["components"].get(cid, {}).get("metrics", {}))

    data = {
        "generated_at": now_utc().isoformat(timespec="seconds"),
        "collection_started_at": started.isoformat(timespec="seconds"),
        "score_date": today,
        "sample": sample,
        "universe": settings["universe"]["name"],
        "count": len(scored),
        "coverage": coverage,
        "config": public_weights(weights),
        "stocks": scored,
    }
    write_json(path, data)
    return data


def run_scores(limit: int | None = None) -> None:
    settings, weights = load_config("settings"), load_config("weights")
    fh_cfg = settings["finnhub"]
    client = Finnhub(os.environ.get("FINNHUB_API_KEY", ""), fh_cfg["base_url"],
                     int(os.environ.get("FINNHUB_RPM", fh_cfg["requests_per_minute"])))
    universe = load_universe(settings)
    if limit:
        universe = universe[:limit]
    need_earnings = any(s.get("source") == "earnings"
                        for c in weights["components"].values() for s in (c.get("metrics") or {}).values())
    started = now_utc()
    print(f"[scores] {len(universe)}개 종목 수집 시작 (분당 {60 / client.interval:.0f}회)")

    stocks, failed = [], []
    for i, u in enumerate(universe, 1):
        sym = u["symbol"]
        try:
            metric = client.metrics(sym)
            earnings = client.earnings(sym) if need_earnings else []
            q = client.quote(sym)
        except FinnhubError as e:
            if "인증" in str(e) and not stocks:
                raise  # 첫 종목부터 인증 실패면 키 문제 — 바로 중단
            failed.append(sym)
            print(f"[scores] {sym} 실패: {e}")
            continue
        except Exception as e:
            failed.append(sym)
            print(f"[scores] {sym} 실패: {type(e).__name__}: {e}")
            continue
        quote = None
        if num(q.get("c")):
            quote = {"c": q.get("c"), "pc": q.get("pc"), "d": q.get("d"), "dp": q.get("dp"), "t": q.get("t")}
        stocks.append({**u, "metrics": extract_metrics(metric, earnings, weights), "quote": quote})
        if i % 25 == 0:
            print(f"[scores] {i}/{len(universe)}")

    if len(stocks) < max(1, len(universe) // 2):
        raise RuntimeError(f"수집 성공 종목이 너무 적습니다 ({len(stocks)}/{len(universe)}) — 기존 점수를 유지합니다.")

    scored = score_universe(stocks, weights)
    data = finish_scores(scored, weights, settings, sample=False, started=started)
    print(f"[scores] 완료: {len(scored)}개, 실패 {len(failed)}개, API 호출 {client.calls}회")
    print("[scores] 지표별 데이터 보유 종목 수 (0 이면 config/weights.yaml 의 keys 를 확인):")
    for m, n in data["coverage"].items():
        print(f"    {m}: {n}/{len(scored)}")


def run_news() -> None:
    settings, media = load_config("settings"), load_config("media")
    path = out_dir(settings) / "news.json"
    prev = read_json(path) or {}
    limit = media.get("summary_max_chars", 280)

    fresh, status = news_mod.fetch_rss(media)
    key = os.environ.get("FINNHUB_API_KEY", "")
    fh = media.get("finnhub_news") or {}
    if key and fh.get("enabled"):
        try:
            client = Finnhub(key, settings["finnhub"]["base_url"], 60)
            got = news_mod.from_finnhub(client.market_news(fh.get("category", "general")), limit)
            fresh.extend(got)
            status["finnhub"] = {"ok": True, "count": len(got)}
        except Exception as e:
            status["finnhub"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    try:
        match = news_mod.build_matcher(load_universe(settings), media.get("aliases"))
        for item in fresh:
            item["tickers"] = match(f'{item["title"]} {item["summary"]}')
    except Exception as e:
        print(f"[news] 티커 매핑 생략: {e}")

    previous = [] if prev.get("sample") else prev.get("items", [])
    items = news_mod.merge_news(previous, fresh, media)
    write_json(path, {
        "generated_at": now_utc().isoformat(timespec="seconds"),
        "sample": False,
        "feeds": status,
        "count": len(items),
        "items": items,
    })
    ok = sum(1 for s in status.values() if s["ok"])
    print(f"[news] 피드 {ok}/{len(status)} 성공, 새로 받은 {len(fresh)}건, 저장 {len(items)}건")
    for fid, s in status.items():
        if not s["ok"]:
            print(f"    {fid} 실패: {s['error']}")


SAMPLE = [
    ("AAPL", "Apple Inc.", "Information Technology"), ("MSFT", "Microsoft", "Information Technology"),
    ("NVDA", "Nvidia", "Information Technology"), ("AVGO", "Broadcom", "Information Technology"),
    ("ORCL", "Oracle Corporation", "Information Technology"), ("CRM", "Salesforce", "Information Technology"),
    ("AMD", "Advanced Micro Devices", "Information Technology"), ("INTC", "Intel", "Information Technology"),
    ("GOOGL", "Alphabet Inc. (Class A)", "Communication Services"), ("META", "Meta Platforms", "Communication Services"),
    ("NFLX", "Netflix", "Communication Services"), ("DIS", "Walt Disney Company", "Communication Services"),
    ("VZ", "Verizon", "Communication Services"), ("AMZN", "Amazon", "Consumer Discretionary"),
    ("TSLA", "Tesla, Inc.", "Consumer Discretionary"), ("HD", "Home Depot", "Consumer Discretionary"),
    ("MCD", "McDonald's", "Consumer Discretionary"), ("NKE", "Nike, Inc.", "Consumer Discretionary"),
    ("SBUX", "Starbucks", "Consumer Discretionary"), ("JPM", "JPMorgan Chase", "Financials"),
    ("BAC", "Bank of America", "Financials"), ("GS", "Goldman Sachs", "Financials"),
    ("V", "Visa Inc.", "Financials"), ("MA", "Mastercard", "Financials"),
    ("BRK.B", "Berkshire Hathaway", "Financials"), ("UNH", "UnitedHealth Group", "Health Care"),
    ("LLY", "Eli Lilly and Company", "Health Care"), ("JNJ", "Johnson & Johnson", "Health Care"),
    ("PFE", "Pfizer", "Health Care"), ("MRK", "Merck & Co.", "Health Care"),
    ("XOM", "ExxonMobil", "Energy"), ("CVX", "Chevron Corporation", "Energy"),
    ("WMT", "Walmart", "Consumer Staples"), ("KO", "Coca-Cola Company", "Consumer Staples"),
    ("PG", "Procter & Gamble", "Consumer Staples"), ("COST", "Costco", "Consumer Staples"),
    ("BA", "Boeing", "Industrials"), ("CAT", "Caterpillar Inc.", "Industrials"),
    ("GE", "GE Aerospace", "Industrials"), ("UPS", "United Parcel Service", "Industrials"),
]


def run_sample() -> None:
    """화면 확인용 가짜 데이터. 실제 수치가 아니며 파일에 sample=true 로 표시된다."""
    settings, weights = load_config("settings"), load_config("weights")

    def rnd(sym, name, lo, hi):
        h = int(hashlib.sha1(f"{sym}:{name}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return lo + (hi - lo) * h

    ranges = {"revenue_growth": (-8, 35), "eps_growth": (-25, 60), "operating_margin": (3, 45), "roe": (4, 60),
              "earnings_surprise": (-6, 12), "pe": (-5, 60), "forward_pe": (8, 45), "ps": (0.8, 22), "ev_ebitda": (6, 40)}
    stocks = []
    for sym, name, sector in SAMPLE:
        metrics = {m: rnd(sym, m, *r) for m, r in ranges.items()}
        if rnd(sym, "missing", 0, 1) < 0.15:
            metrics["forward_pe"] = None
        price = round(rnd(sym, "price", 30, 700), 2)
        dp = round(rnd(sym, "dp", -3, 3), 2)
        pc = round(price / (1 + dp / 100), 2)
        stocks.append({"symbol": sym, "name": name, "sector": sector, "metrics": metrics,
                       "quote": {"c": price, "pc": pc, "d": round(price - pc, 2), "dp": dp, "t": int(now_utc().timestamp())}})
    started = now_utc()
    scored = score_universe(stocks, weights)
    for s in scored:
        s["prev_score"] = None if s["score"] is None else round(s["score"] + rnd(s["symbol"], "delta", -4, 4), 1)
    d = out_dir(settings)
    path = d / "scores.json"
    if path.exists():
        path.unlink()
    data = finish_scores(scored, weights, settings, sample=True, started=started)
    data["stocks"] = scored  # finish_scores 가 prev_score 를 비우므로 다시 채운다
    for s in scored:
        s["prev_score"] = round(s["score"] + rnd(s["symbol"], "delta", -4, 4), 1)
    write_json(path, data)
    iso = now_utc().isoformat(timespec="seconds")
    write_json(d / "news.json", {
        "generated_at": iso, "sample": True, "feeds": {}, "count": 2,
        "items": [
            {"id": "sample1", "title": "[샘플] 뉴스 수집이 한 번 돌면 실제 헤드라인으로 바뀝니다", "summary": "python pipeline.py news 를 실행하세요.",
             "url": "https://example.com/", "source": "샘플", "source_id": "sample", "published": iso, "tickers": ["AAPL"]},
            {"id": "sample2", "title": "[샘플] 종목이 인식된 헤드라인은 종목 상세 화면에도 표시됩니다", "summary": "",
             "url": "https://example.com/", "source": "샘플", "source_id": "sample", "published": iso, "tickers": ["NVDA", "MSFT"]},
        ],
    })
    print(f"[sample] 샘플 {len(scored)}개 종목 생성 → {d}")


def run_auto() -> None:
    settings = load_config("settings")
    run_news()
    existing = read_json(out_dir(settings) / "scores.json")
    want = os.environ.get("RUN_SCORES") == "1" or not existing or existing.get("sample")
    if not want:
        return
    if not os.environ.get("FINNHUB_API_KEY"):
        print("[auto] FINNHUB_API_KEY 가 없어 점수 계산을 건너뜁니다 (뉴스만 갱신).")
        return
    run_scores()
    run_news()  # 점수 수집이 오래 걸리므로 끝난 뒤 뉴스를 한 번 더 갱신


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["news", "scores", "auto", "sample"])
    p.add_argument("--limit", type=int, help="scores: 앞에서 N개 종목만 (시험용)")
    args = p.parse_args()
    load_env()
    try:
        if args.command == "news":
            run_news()
        elif args.command == "scores":
            run_scores(args.limit)
        elif args.command == "auto":
            run_auto()
        else:
            run_sample()
    except (FinnhubError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
