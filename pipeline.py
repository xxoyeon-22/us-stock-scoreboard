"""데이터 파이프라인 진입점.

  python pipeline.py news         뉴스 헤드라인 수집 (10분마다)
  python pipeline.py scores       S&P 500 재무·시세 수집 + 점수 계산 (하루 1회, 약 35분)
  python pipeline.py investors    유명 투자자 13F 포트폴리오 + 관련 뉴스 (하루 1회)
  python pipeline.py analyze      Claude 로 뉴스 쉬운 설명·호재 판단·전망 생성 (새 뉴스가 있는 것만)
  python pipeline.py calendar     실적 발표 일정 + config/events.yaml 의 거시 이벤트
  python pipeline.py rescore      API 호출 없이 저장된 자료로 점수만 다시 계산
  python pipeline.py auto         news → (필요하면 investors, scores) → analyze → rescore
  python pipeline.py sample       네트워크 없이 가짜 데이터로 화면 확인용 파일 생성

결과는 web/data/*.json 에 쓴다 (정적 사이트가 그대로 읽는다).
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests

from analysis.investors import build_signals
from analysis.scoring import score_universe
from collectors import ROOT, USER_AGENT, load_config, load_env
from collectors import news as news_mod
from collectors.finnhub import Finnhub, FinnhubError
from collectors.universe import load_universe

NY = ZoneInfo("America/New_York")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def out_dir(settings: dict | None = None):
    d = ROOT / (settings or load_config("settings"))["output_dir"]
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


def parse_iso(s):
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def digest(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- 점수

def _first(raw: dict, keys) -> float | None:
    for key in keys or []:
        v = num(raw.get(key))
        if v is not None:
            return v
    return None


def extract_metrics(raw_metric: dict, raw_earnings: list, weights: dict, price: float | None = None) -> dict:
    out = {}
    for comp in weights["components"].values():
        for name, spec in (comp.get("metrics") or {}).items():
            value, src = None, spec.get("source")
            if src == "metric":
                value = _first(raw_metric, spec.get("keys"))
            elif src == "diff":            # 첫 값 − 둘째 값 (예: 현재 마진 − 5년 평균)
                a, b = _first(raw_metric, spec.get("keys")), _first(raw_metric, spec.get("keys_b"))
                value = a - b if a is not None and b is not None else None
            elif src == "range":           # 52주 저점~고점 사이에서 현재가의 위치 (0~100)
                lo, hi = _first(raw_metric, spec.get("keys")), _first(raw_metric, spec.get("keys_b"))
                if price and lo is not None and hi is not None and hi > lo:
                    value = max(0.0, min(100.0, (price - lo) / (hi - lo) * 100))
            elif src == "earnings":
                vals = [num(e.get("surprisePercent")) for e in (raw_earnings or [])[:4]]
                vals = [v for v in vals if v is not None]
                value = sum(vals) / len(vals) if vals else None
            out[name] = value
    return out


def extract_info(raw_metric: dict, weights: dict, price: float | None) -> dict:
    """점수 외에 화면에 보여줄 값 + 위험 표시."""
    info = {k: _first(raw_metric, keys) for k, keys in (weights.get("info") or {}).items()}
    risk = weights.get("risk") or {}
    vol = info.get("avg_volume_10d_m")
    info["dollar_volume"] = vol * 1e6 * price if vol is not None and price else None
    info["low_liquidity"] = info["dollar_volume"] is not None and info["dollar_volume"] < risk.get("min_dollar_volume", 0)
    beta = info.get("beta")
    info["risk_level"] = None if beta is None else "high" if beta >= risk.get("beta_high", 1.3) else "low" if beta <= risk.get("beta_low", 0.8) else "mid"
    hi = info.get("high_52w")
    info["from_high_pct"] = (price / hi - 1) * 100 if price and hi else None
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in info.items()}


def public_weights(weights: dict, scored: list[dict]) -> dict:
    """화면에서 지표 이름·가중치를 보여주기 위한 요약. active = 실제로 점수에 들어간 구성 요소."""
    comps = {}
    for cid, c in weights["components"].items():
        comps[cid] = {
            "label": c.get("label", cid),
            "weight": c["weight"],
            "active": any(cid in s["components"] for s in scored),
            "metrics": {
                m: {"label": s.get("label", m), "unit": s.get("unit", ""), "higher_better": s.get("higher_better", True)}
                for m, s in (c.get("metrics") or {}).items()
            },
        }
    return {"components": comps, "regime": weights.get("regime", {}), "risk": weights.get("risk", {})}


def load_extras(d, allow_sample: bool) -> dict:
    """투자자 포지션·뉴스 감성처럼 이미 0~100 으로 계산된 구성 요소를 모은다."""
    extras = {}
    inv = read_json(d / "investors.json") or {}
    if inv.get("signals"):
        extras["investors"] = inv["signals"]
    an = read_json(d / "analysis.json") or {}
    if an.get("stocks") and (allow_sample or not an.get("sample")):
        valid = load_config("settings").get("llm", {}).get("sentiment_valid_days", 30)
        cutoff = now_utc() - timedelta(days=valid)
        news = {}
        for sym, a in an["stocks"].items():
            dt = parse_iso(a.get("generated_at"))
            if dt and dt >= cutoff and a.get("sentiment_score") is not None:
                news[sym] = {"score": a["sentiment_score"], "detail": {"stance": a.get("stance"), "outlook": a.get("outlook")}}
        if news:
            extras["news"] = news
    return extras


def rescore() -> bool:
    """raw_metrics.json + investors.json + analysis.json → scores.json. API 호출 없음."""
    settings, weights = load_config("settings"), load_config("weights")
    d = out_dir(settings)
    raw = read_json(d / "raw_metrics.json")
    if not raw or not raw.get("stocks"):
        print("[rescore] raw_metrics.json 이 없어 건너뜁니다 (scores 를 먼저 실행).")
        return False
    sample = bool(raw.get("sample"))
    scored = score_universe(raw["stocks"], weights, load_extras(d, allow_sample=sample))

    path = d / "scores.json"
    prev = read_json(path) or {}
    today = now_utc().astimezone(NY).date().isoformat()
    prev_map = {}
    if bool(prev.get("sample")) == sample:
        same_day = prev.get("score_date") == today
        for s in prev.get("stocks", []):
            prev_map[s["symbol"]] = s.get("prev_score") if same_day else s.get("score")
    for s in scored:
        s["prev_score"] = prev_map.get(s["symbol"])

    coverage = {}
    for cid, c in weights["components"].items():
        for m in (c.get("metrics") or {}):
            coverage[m] = sum(1 for s in scored if m in s["components"].get(cid, {}).get("metrics", {}))
        if not c.get("metrics"):
            coverage[cid] = sum(1 for s in scored if cid in s["components"])
    write_json(path, {
        "generated_at": iso_now(), "metrics_collected_at": raw.get("collected_at"), "score_date": today,
        "sample": sample, "universe": settings["universe"]["name"], "count": len(scored),
        "coverage": coverage, "config": public_weights(weights, scored), "stocks": scored,
    })
    print(f"[rescore] {len(scored)}개 종목 점수 계산 완료")
    return True


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
        price = quote["c"] if quote else None
        stocks.append({**u, "metrics": extract_metrics(metric, earnings, weights, price),
                       "info": extract_info(metric, weights, price), "quote": quote})
        if i % 25 == 0:
            print(f"[scores] {i}/{len(universe)}")

    if len(stocks) < max(1, len(universe) // 2):
        raise RuntimeError(f"수집 성공 종목이 너무 적습니다 ({len(stocks)}/{len(universe)}) — 기존 점수를 유지합니다.")

    write_json(out_dir(settings) / "raw_metrics.json", {"collected_at": iso_now(), "sample": False, "stocks": stocks})
    run_calendar(client)
    rescore()
    data = read_json(out_dir(settings) / "scores.json")
    print(f"[scores] 완료: {len(stocks)}개, 실패 {len(failed)}개, API 호출 {client.calls}회")
    print("[scores] 지표별 데이터 보유 종목 수 (0 이면 config/weights.yaml 의 keys 를 확인):")
    for m, n in data["coverage"].items():
        print(f"    {m}: {n}/{len(stocks)}")


# ---------------------------------------------------------------- 일정·수수료

def run_calendar(client: Finnhub | None = None) -> None:
    """실적 발표일(Finnhub) + config/events.yaml 의 거시 이벤트 → calendar.json"""
    settings, ev = load_config("settings"), load_config("events")
    d = out_dir(settings)
    prev = read_json(d / "calendar.json") or {}
    earnings = prev.get("earnings", [])
    key = os.environ.get("FINNHUB_API_KEY", "")
    if client or key:
        try:
            client = client or Finnhub(key, settings["finnhub"]["base_url"], 60)
            today = now_utc().astimezone(NY).date()
            data = client.get("/calendar/earnings", **{"from": today.isoformat(),
                              "to": (today + timedelta(days=ev.get("earnings_days_ahead", 30))).isoformat()})
            universe = {s["symbol"] for s in (read_json(d / "raw_metrics.json") or {}).get("stocks", [])}
            earnings = sorted(({"date": e.get("date"), "symbol": e.get("symbol"), "hour": e.get("hour"),
                                "eps_estimate": e.get("epsEstimate")} for e in (data or {}).get("earningsCalendar", [])
                               if e.get("date") and (not universe or e.get("symbol") in universe)), key=lambda e: (e["date"], e["symbol"]))
        except Exception as e:
            print(f"[calendar] 실적 일정 수집 실패 (이전 자료 유지): {type(e).__name__}: {str(e)[:120]}")
    write_json(d / "calendar.json", {"generated_at": iso_now(), "earnings": earnings, "events": ev.get("events") or []})
    print(f"[calendar] 실적 발표 {len(earnings)}건, 거시 이벤트 {len(ev.get('events') or [])}건")


def write_static() -> None:
    """설정 파일 중 화면에서 쓰는 것(증권사 수수료)을 JSON 으로 내보낸다."""
    write_json(out_dir() / "brokers.json", load_config("brokers"))
    ko = read_json(ROOT / "config" / "ko_names.json")       # 기업 한글 이름 (원본은 config, 화면용 사본은 매번 새로 쓴다)
    if ko:
        write_json(out_dir() / "ko_names.json", ko)


# ---------------------------------------------------------------- 뉴스

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
    items = news_mod.merge_news(previous, fresh, media)   # 이전 항목이 먼저라 이미 붙은 한국어 풀이는 유지된다
    write_json(path, {"generated_at": iso_now(), "sample": False, "feeds": status, "count": len(items), "items": items})
    ok = sum(1 for s in status.values() if s["ok"])
    print(f"[news] 피드 {ok}/{len(status)} 성공, 새로 받은 {len(fresh)}건, 저장 {len(items)}건")
    for fid, s in status.items():
        if not s["ok"]:
            print(f"    {fid} 실패: {s['error']}")


# ---------------------------------------------------------------- 투자자

def investor_news(inv: dict, cfg: dict, general: list[dict]) -> list[dict]:
    """투자자 이름으로 검색한 헤드라인 + 일반 뉴스 중 이름이 들어간 것. 제목과 링크만."""
    items = []
    search = cfg.get("news_search") or {}
    kws = inv.get("keywords") or [inv.get("name_en", "")]
    if search.get("enabled") and kws:
        query = quote_plus(" OR ".join(f'"{k}"' for k in kws))
        try:
            r = requests.get(search["url"].format(query=query), headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            for it in news_mod.parse_feed(r.content, "", "search", 200):
                title, _, src = it["title"].rpartition(" - ")   # "제목 - 언론사" 형식
                if title:
                    it["title"], it["source"] = title, src
                else:
                    it["source"] = "뉴스 검색"
                it["summary"] = ""
                items.append(it)
        except Exception as e:
            print(f"[investors] {inv['id']} 뉴스 검색 실패: {type(e).__name__}: {str(e)[:100]}")
    low = [k.lower() for k in kws]
    for it in general:
        text = f'{it["title"]} {it.get("summary", "")}'.lower()
        if any(k in text for k in low):
            items.append({k: it.get(k) for k in ("id", "title", "summary", "url", "source", "published", "ko", "sentiment", "event")})
    seen, out = set(), []
    for it in items:
        key = news_mod._norm_title(it["title"])
        if key in seen or not it.get("published"):
            continue
        seen.add(key)
        it["id"] = it.get("id") or digest(key)
        out.append({k: v for k, v in it.items() if k not in ("tickers", "source_id") and v is not None})
    out.sort(key=lambda x: x["published"], reverse=True)
    return out[: cfg.get("news_per_investor", 12)]


def run_investors() -> None:
    from collectors import sec13f

    cfg = load_config("investors")
    d = out_dir()
    path = d / "investors.json"
    prev_data = read_json(path) or {}
    prev_by_id = {i["id"]: i for i in prev_data.get("investors", [])}
    general = (read_json(d / "news.json") or {}).get("items", [])

    try:
        edgar = sec13f.Edgar()
    except sec13f.EdgarError as e:
        print(f"[investors] {e}")
        edgar = None

    cache = sec13f.load_cusip_cache()
    top, map_top = cfg.get("top_holdings", 30), cfg.get("map_top", 60)
    result = []
    for inv in cfg["investors"]:
        if not inv.get("enabled", True):
            continue
        base = {k: inv.get(k) for k in ("id", "name", "name_en", "fund", "style", "weight")}
        old = prev_by_id.get(inv["id"], {})
        entry = {**old, **base}
        if edgar:
            try:
                filings = edgar.filings_13f(inv["cik"], 2)
                if not filings:
                    raise sec13f.EdgarError("13F 공시가 없습니다")
                cur_f = filings[0]
                if old.get("accession") == cur_f["accession"] and old.get("holdings"):
                    print(f"[investors] {inv['id']}: 변동 없음 ({cur_f['period']})")
                else:
                    cur = edgar.holdings(inv["cik"], cur_f["accession"])
                    prv = edgar.holdings(inv["cik"], filings[1]["accession"]) if len(filings) > 1 else None
                    held, exits = sec13f.compare(cur, prv, cfg.get("change_threshold", 0.05))
                    wanted = [h["cusip"] for h in held[:map_top]] + [h["cusip"] for h in exits[:20]]
                    cache = sec13f.map_cusips(wanted, cache)
                    for h in held + exits:
                        h["ticker"] = cache.get(h["cusip"])
                        h["pct"] = round(h["pct"], 2)
                        if h.get("change_pct") is not None:
                            h["change_pct"] = round(h["change_pct"], 1)
                    entry.update({
                        "accession": cur_f["accession"], "period": cur_f["period"], "filed": cur_f["filed"],
                        "prev_period": filings[1]["period"] if len(filings) > 1 else None,
                        "total_value": sum(h["value"] for h in held), "positions": len(held),
                        "holdings": held[:top],
                        "scored": [{k: h.get(k) for k in ("ticker", "action", "pct", "change_pct", "put_call")}
                                   for h in held[:map_top] if h.get("ticker")],
                        "exits": exits[:15],
                        "counts": {a: sum(1 for h in held if h["action"] == a) for a in ("new", "add", "hold", "trim")} | {"exit": len(exits)},
                    })
                    print(f"[investors] {inv['id']}: {cur_f['period']} 기준 {len(held)}개 보유, 전량 매도 {len(exits)}개")
            except Exception as e:
                print(f"[investors] {inv['id']} 13F 실패 (이전 자료 유지): {type(e).__name__}: {str(e)[:140]}")
        entry["news"] = investor_news(inv, cfg, general)
        # 이전에 붙은 한국어 풀이는 같은 헤드라인이면 이어받는다
        old_news = {n["id"]: n for n in old.get("news", [])}
        for n in entry["news"]:
            for k in ("ko", "sentiment", "event"):
                if k not in n and old_news.get(n["id"], {}).get(k):
                    n[k] = old_news[n["id"]][k]
        result.append(entry)

    signals = build_signals(
        [{**e, "holdings_all": e.get("scored", []), "exits": [x for x in e.get("exits", []) if x.get("ticker")]} for e in result], cfg)
    write_json(path, {"generated_at": iso_now(), "investors": result, "signals": signals})
    print(f"[investors] 완료: {len(result)}명, 신호가 붙은 종목 {len(signals)}개")


# ---------------------------------------------------------------- Claude 분석

def run_analyze() -> None:
    from analysis.llm import Analyst, Ledger, LLMUnavailable

    settings = load_config("settings")
    cfg = settings.get("llm", {})
    d = out_dir(settings)
    try:
        analyst = Analyst(cfg, Ledger(d / "usage.json", cfg.get("budget_usd", 8.0)))
    except LLMUnavailable as e:
        print(f"[analyze] {e} 쉬운 설명·전망 생성을 건너뜁니다.")
        return

    news_path, inv_path, an_path = d / "news.json", d / "investors.json", d / "analysis.json"
    news = read_json(news_path) or {"items": []}
    inv = read_json(inv_path) or {"investors": []}
    an = read_json(an_path) or {}
    if an.get("sample"):
        an = {}
    an.setdefault("stocks", {})
    an.setdefault("investors", {})
    scores = {s["symbol"]: s for s in (read_json(d / "scores.json") or {}).get("stocks", [])}
    labels = {cid: c.get("label", cid) for cid, c in load_config("weights")["components"].items()}
    scores_sample = bool((read_json(d / "scores.json") or {}).get("sample"))   # 샘플 점수는 분석 근거로 쓰지 않는다

    def save():
        an.update({"generated_at": iso_now(), "model": analyst.model, "sample": False})
        write_json(news_path, news)
        write_json(inv_path, inv)
        write_json(an_path, an)

    try:
        # 1) 헤드라인 한 줄 풀이 (일반 뉴스 + 투자자 관련 뉴스)
        pending = [n for n in news.get("items", []) if not n.get("ko")]
        pending += [n for i in inv.get("investors", []) for n in i.get("news", []) if not n.get("ko")]
        pending.sort(key=lambda n: n.get("published") or "", reverse=True)
        pending = pending[: cfg.get("max_headlines_per_run", 160)]
        by_id = {}
        for n in pending:
            by_id.setdefault(n["id"], []).append(n)
        ids = list(by_id)
        size = cfg.get("headline_batch", 40)
        done = 0
        for i in range(0, len(ids), size):
            batch = [{"id": x, "title": by_id[x][0]["title"], "summary": by_id[x][0].get("summary", ""),
                      "source": by_id[x][0].get("source", "")} for x in ids[i:i + size]]
            for nid, res in analyst.headlines(batch).items():
                for n in by_id[nid]:
                    n.update(res)
                done += 1
            save()
        print(f"[analyze] 헤드라인 풀이 {done}/{len(ids)}건")

        # 2) 종목별 쉬운 설명·호재 판단·전망 (뉴스 구성이 바뀐 종목만)
        cutoff = (now_utc() - timedelta(days=cfg.get("stock_news_days", 7))).isoformat()
        per = {}
        for n in news.get("items", []):
            if (n.get("published") or "") >= cutoff:
                for t in n.get("tickers") or []:
                    per.setdefault(t, []).append(n)
        todo = []
        for sym, items in per.items():
            if sym not in scores:
                continue
            items = sorted(items, key=lambda n: n["published"], reverse=True)[: cfg.get("stock_news_max", 12)]
            h = digest(*sorted(n["id"] for n in items))
            prev_a = an["stocks"].get(sym, {})
            last = parse_iso(prev_a.get("generated_at"))
            fresh_enough = last and now_utc() - last < timedelta(hours=cfg.get("min_hours_between", 12))
            if prev_a.get("hash") != h and not fresh_enough:
                todo.append((len(items), sym, items, h))
        todo.sort(key=lambda t: (-t[0], t[1]))
        done = 0
        today = iso_now()[:10]
        daily = an.get("daily") or {}
        used = daily.get("count", 0) if daily.get("date") == today else 0
        budget = max(0, cfg.get("max_stocks_per_day", 20) - used)
        if not budget and todo:
            print(f"[analyze] 오늘 종목 분석 상한({cfg.get('max_stocks_per_day', 20)}개)에 도달해 내일까지 쉽니다.")
        for _, sym, items, h in todo[: min(cfg.get("max_stocks_per_run", 10), budget)]:
            s = scores[sym]
            payload = {
                "symbol": sym, "name": s["name"], "sector": s["sector"], "total_score_0_100": None if scores_sample else s.get("score"),
                "factor_scores_0_100": {} if scores_sample else {labels.get(c, c): v["score"] for c, v in s.get("components", {}).items()},
                "day_change_pct": (s.get("quote") or {}).get("dp"),
                "famous_investor_moves_last_quarter": (inv.get("signals") or {}).get(sym, {}).get("detail", []),
                "headlines": [{"date": n["published"][:10], "source": n.get("source"), "title": n["title"],
                               "summary": n.get("summary", "")} for n in items],
            }
            res = analyst.stock(payload)
            if res:
                an["stocks"][sym] = {**res, "hash": h, "generated_at": iso_now(), "news_count": len(items),
                                     "news_ids": [n["id"] for n in items]}
                an["daily"] = {"date": today, "count": used + done + 1}
                done += 1
                save()
        print(f"[analyze] 종목 분석 {done}건 (대기 {max(0, len(todo) - done)}건)")

        # 3) 투자자별 최근 동향·발언 정리
        done = 0
        for i in inv.get("investors", []):
            heads = i.get("news", [])
            h = digest(i.get("accession"), *sorted(n["id"] for n in heads))
            last_i = parse_iso(an["investors"].get(i["id"], {}).get("generated_at"))
            too_soon = bool(last_i and now_utc() - last_i < timedelta(hours=cfg.get("investor_min_hours", 72)))
            if an["investors"].get(i["id"], {}).get("hash") == h or too_soon or not (heads or i.get("holdings")):
                continue
            row = lambda x: {"name": x.get("issuer"), "ticker": x.get("ticker"), "action": x.get("action"),
                             "portfolio_pct": x.get("pct"), "share_change_pct": x.get("change_pct"), "option": x.get("put_call")}
            payload = {
                "investor": i.get("name_en"), "fund": i.get("fund"), "style": i.get("style"),
                "filing_period_end": i.get("period"), "filed": i.get("filed"),
                "top_holdings": [row(x) for x in (i.get("holdings") or [])[:12]],
                "new_or_added": [row(x) for x in (i.get("holdings") or []) if x.get("action") in ("new", "add")][:10],
                "trimmed": [row(x) for x in (i.get("holdings") or []) if x.get("action") == "trim"][:10],
                "sold_out": [row(x) for x in (i.get("exits") or [])[:8]],
                "headlines": [{"date": (n.get("published") or "")[:10], "source": n.get("source"), "title": n["title"]} for n in heads],
            }
            res = analyst.investor(payload)
            if res:
                an["investors"][i["id"]] = {**res, "hash": h, "generated_at": iso_now()}
                done += 1
                save()
        print(f"[analyze] 투자자 동향 {done}건")
    except LLMUnavailable as e:
        print(f"[analyze] 중단: {e}")
    finally:
        if analyst.calls:
            save()
            if analyst.ledger:
                print(f"[analyze] 누적 비용 ${analyst.ledger.spent:.4f} / 예산 ${analyst.ledger.budget:.2f}")
            print(f"[analyze] Claude 호출 {analyst.calls}회, 입력 {analyst.usage['input']:,} / 출력 {analyst.usage['output']:,} 토큰 ({analyst.model})")


def run_names() -> None:
    """S&P 500 기업의 한글 이름 중 아직 없는 것만 Claude(싼 모델)로 만들어 config/ko_names.json 에 보탠다."""
    from analysis.llm import Analyst, Ledger, LLMUnavailable

    settings = load_config("settings")
    cfg = settings.get("llm", {})
    path = ROOT / "config" / "ko_names.json"
    names = read_json(path) or {}
    todo = [u for u in load_universe(settings) if u["symbol"] not in names]
    if todo:
        try:
            analyst = Analyst(cfg, Ledger(out_dir(settings) / "usage.json", cfg.get("budget_usd", 8.0)))
            for i in range(0, len(todo), 80):
                got = analyst.names([{"symbol": u["symbol"], "name": u["name"]} for u in todo[i:i + 80]])
                names.update(got)
                path.write_text(json.dumps(names, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
            print(f"[names] 한글 이름 {len(names)}개 (새로 만든 {len(todo)}개 중 성공 분 포함), 누적 비용 ${analyst.ledger.spent:.4f}")
        except LLMUnavailable as e:
            print(f"[names] 중단: {e}")
    else:
        print(f"[names] 새로 만들 이름이 없습니다 ({len(names)}개)")
    write_static()


# ---------------------------------------------------------------- 샘플

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
    d = out_dir()

    def rnd(sym, name, lo, hi):
        h = int(hashlib.sha1(f"{sym}:{name}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return lo + (hi - lo) * h

    ranges = {"revenue_growth": (-8, 35), "eps_growth": (-25, 60), "operating_margin": (3, 45), "roe": (4, 60),
              "earnings_surprise": (-6, 12), "pe": (-5, 60), "forward_pe": (8, 45), "ps": (0.8, 22), "ev_ebitda": (6, 40),
              "margin_trend": (-6, 8), "roic": (3, 40), "debt_to_equity": (0.1, 2.5), "interest_coverage": (2, 40),
              "ev_fcf": (10, 70), "current_ratio": (0.7, 3), "return_13w": (-15, 25), "return_26w": (-20, 40),
              "rel_strength": (-25, 35), "range_52w": (5, 98)}
    stocks = []
    for sym, name, sector in SAMPLE:
        metrics = {m: rnd(sym, m, *r) for m, r in ranges.items()}
        if rnd(sym, "missing", 0, 1) < 0.15:
            metrics["forward_pe"] = None
        price = round(rnd(sym, "price", 30, 700), 2)
        dp = round(rnd(sym, "dp", -3, 3), 2)
        pc = round(price / (1 + dp / 100), 2)
        beta = round(rnd(sym, "beta", 0.5, 1.9), 2)
        stocks.append({"symbol": sym, "name": name, "sector": sector, "metrics": metrics,
                       "info": {"beta": beta, "risk_level": "high" if beta >= 1.3 else "low" if beta <= 0.8 else "mid",
                                "from_high_pct": round(-rnd(sym, "fh", 0, 30), 1), "low_liquidity": False,
                                "dollar_volume": round(rnd(sym, "dv", 2e8, 9e9)), "market_cap_m": round(rnd(sym, "mc", 5e4, 3e6))},
                       "quote": {"c": price, "pc": pc, "d": round(price - pc, 2), "dp": dp, "t": int(now_utc().timestamp())}})
    write_json(d / "raw_metrics.json", {"collected_at": iso_now(), "sample": True, "stocks": stocks})

    existing = read_json(d / "analysis.json") or {}
    if not existing or existing.get("sample"):
        def brief(stance, outlook, conf, score, summary, points, reason, risk, terms):
            return {"summary": summary, "points": points, "stance": stance, "outlook": outlook, "confidence": conf,
                    "reason": reason, "risk": risk, "sentiment_score": score, "terms": terms,
                    "hash": "sample", "generated_at": iso_now(), "news_count": 4, "news_ids": []}
        write_json(d / "analysis.json", {"generated_at": iso_now(), "model": "sample", "sample": True, "investors": {}, "stocks": {
            "NVDA": brief("positive", "strong_up", "medium", 78,
                          "[샘플] 최근 분기 실적이 시장 예상보다 좋게 나왔고, 다음 분기 매출 전망도 올렸다는 보도가 이어졌습니다. 데이터센터용 칩 주문이 계속 늘고 있다는 내용이 중심입니다.",
                          ["[샘플] 실적이 예상치를 웃돌았습니다.", "[샘플] 회사가 다음 분기 전망을 올렸습니다.", "[샘플] 대형 고객사의 주문이 늘었다는 보도가 있습니다."],
                          "[샘플] 실적과 전망 상향이라는 뚜렷한 재료가 함께 나왔기 때문입니다.",
                          "[샘플] 이미 주가에 기대가 많이 반영돼 있으면 좋은 소식에도 내릴 수 있습니다.",
                          [{"term": "가이던스", "meaning": "회사가 스스로 밝히는 앞으로의 실적 전망"}]),
            "BA": brief("negative", "down", "low", 34,
                        "[샘플] 생산 차질과 규제 당국의 점검 소식이 이어졌습니다. 납품 일정이 늦어질 수 있다는 우려가 기사에 반복해서 나옵니다.",
                        ["[샘플] 납품 지연 우려가 있습니다.", "[샘플] 규제 당국이 추가 점검에 나섰습니다."],
                        "[샘플] 부정적인 헤드라인이 더 많지만 근거가 되는 기사 수가 적습니다.",
                        "[샘플] 점검이 문제없이 끝나면 분위기가 바뀔 수 있습니다.", []),
            "JPM": brief("neutral", "neutral", "low", 52,
                         "[샘플] 금리 전망을 둘러싼 엇갈린 보도가 섞여 있어 뚜렷한 방향이 보이지 않습니다.",
                         ["[샘플] 금리 관련 전망이 엇갈립니다."], "[샘플] 호재와 악재가 비슷하게 섞여 있습니다.",
                         "[샘플] 금리 발표에 따라 어느 쪽으로든 움직일 수 있습니다.", []),
        }})
    write_static()
    if not (d / "calendar.json").exists():
        run_calendar()
    rescore()
    news = read_json(d / "news.json")
    if not news or news.get("sample"):
        write_json(d / "news.json", {"generated_at": iso_now(), "sample": True, "feeds": {}, "count": 2, "items": [
            {"id": "sample1", "title": "[Sample] Chipmaker shares rise after quarterly results beat estimates", "summary": "",
             "ko": "[샘플] 반도체 회사 실적이 예상보다 좋아 주가가 올랐습니다.", "sentiment": "positive", "event": "실적",
             "url": "https://example.com/", "source": "샘플", "source_id": "sample", "published": iso_now(), "tickers": ["NVDA"]},
            {"id": "sample2", "title": "[Sample] Aircraft maker faces new production review", "summary": "",
             "ko": "[샘플] 항공기 제조사가 생산 과정에 대해 추가 점검을 받게 됐습니다.", "sentiment": "negative", "event": "규제",
             "url": "https://example.com/", "source": "샘플", "source_id": "sample", "published": iso_now(), "tickers": ["BA"]},
        ]})
    print(f"[sample] 샘플 {len(stocks)}개 종목 생성 → {d}")


# ---------------------------------------------------------------- 자동

def run_auto() -> None:
    d = out_dir()
    daily = os.environ.get("RUN_SCORES") == "1"
    run_news()
    if daily or not (read_json(d / "investors.json") or {}).get("investors"):
        try:
            run_investors()
        except Exception as e:
            print(f"[auto] 투자자 수집 실패: {type(e).__name__}: {e}")
    raw = read_json(d / "raw_metrics.json")
    if daily or not raw or raw.get("sample"):
        if os.environ.get("FINNHUB_API_KEY"):
            run_scores()
            run_news()  # 점수 수집이 오래 걸리므로 끝난 뒤 뉴스를 한 번 더 갱신
        else:
            print("[auto] FINNHUB_API_KEY 가 없어 재무·시세 수집을 건너뜁니다.")
    if not (d / "calendar.json").exists():
        run_calendar()
    write_static()
    run_analyze()
    rescore()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["news", "scores", "investors", "analyze", "rescore", "calendar", "names", "auto", "sample"])
    p.add_argument("--limit", type=int, help="scores: 앞에서 N개 종목만 (시험용)")
    args = p.parse_args()
    load_env()
    for k in ("FINNHUB_API_KEY", "ANTHROPIC_API_KEY", "SEC_USER_AGENT"):   # .env 의 빈 값은 없는 것으로 본다
        if os.environ.get(k) == "":
            os.environ.pop(k)
    try:
        if args.command == "scores":
            run_scores(args.limit)
        else:
            {"news": run_news, "calendar": run_calendar, "names": run_names, "investors": run_investors, "analyze": run_analyze, "rescore": rescore,
             "auto": run_auto, "sample": run_sample}[args.command]()
    except (FinnhubError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
