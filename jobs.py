"""느린 수집·계산 작업 모음: 가격 이력, 시장 국면, 재무 희석(XBRL), 수급(내부자·애널리스트), 해자(10-K), 백테스트.

pipeline.py 의 명령(prices, market, xbrl, flow, moat, backtest)과 auto 가 이 모듈을 부른다.
각 작업은 실패하면 이전 결과를 그대로 두고 로그만 남긴다 (점수 계산이 멈추지 않게).
"""
import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

from analysis import technicals as tech
from analysis.backtest import run_backtest
from analysis.regime import regime
from collectors import ROOT, load_config, replace_retry

INFO_KEYS = {"ma50", "ma200", "mdd_1y", "vol_1y"}       # 점수에는 안 넣고 화면에 보여 주기만 하는 값


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso() -> str:
    return now().isoformat(timespec="seconds")


def outdir():
    d = ROOT / load_config("settings")["output_dir"]
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
    replace_retry(tmp, path)


def age_hours(path) -> float | None:
    """파일 안 fetched_at 기준 경과 시간. 파일이 없거나 값이 없으면 None."""
    ts = (read_json(path) or {}).get("fetched_at")
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return (now() - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))).total_seconds() / 3600


def stale(path, hours: float) -> bool:
    a = age_hours(path)
    return a is None or a > hours


def _stocks(d, limit=None) -> list[dict]:
    raw = read_json(d / "raw_metrics.json") or {}
    stocks = [] if raw.get("sample") else raw.get("stocks", [])
    return stocks[:limit] if limit else stocks


# ------------------------------------------------------------------ 가격 이력 → 이동평균·최대 낙폭
def run_prices(limit=None) -> None:
    from collectors import yahoo

    d = outdir()
    stocks = _stocks(d, limit)
    if not stocks:
        print("[prices] 실제 종목 목록(raw_metrics.json)이 없어 건너뜁니다.")
        return
    result = dict((read_json(d / "x_prices.json") or {}).get("stocks", {}))
    weekly = dict((read_json(d / "prices_weekly.json") or {}).get("weekly", {}))
    spx = yahoo.chart("^GSPC", "5y")
    if spx:
        weekly["^GSPC"] = tech.weekly(spx)
    ok = fails = 0
    for i, s in enumerate(stocks, 1):
        sym = s["symbol"]
        pts = yahoo.chart(sym)
        if pts is None:
            fails += 1
            if ok == 0 and fails >= 5:
                print("[prices] 가격 이력을 계속 받지 못해 중단합니다 (출처가 막혔을 수 있음). 이전 값을 유지합니다.")
                break
            continue
        if pts:
            result[sym] = tech.technicals([c for _, c in pts])
            weekly[sym] = tech.weekly(pts)
            (d / "prices").mkdir(exist_ok=True)
            write_json(d / "prices" / f"{sym}.json", {"d": tech.daily_dated(pts), "w": tech.weekly_dated(pts)})   # 종목 상세 차트용
            ok += 1
        time.sleep(0.25)
        if i % 100 == 0:
            print(f"[prices] {i}/{len(stocks)}")
    if ok:
        write_json(d / "x_prices.json", {"fetched_at": iso(), "stocks": result})
        write_json(d / "prices_weekly.json", {"fetched_at": iso(), "weekly": weekly})
    print(f"[prices] 성공 {ok}개, 실패 {fails}개")


# ------------------------------------------------------------------ 시장 국면
def _spark(series, n=60):
    return [round(v, 2) for _, v in series[-n:]]


def run_market() -> None:
    from collectors import fred, yahoo

    d = outdir()
    fr = {sid: fred.series(sid) for sid in ("DGS10", "T10Y2Y", "FEDFUNDS", "CPIAUCSL", "UNRATE", "VIXCLS")}

    def last(sid):
        s = fr.get(sid)
        return s[-1][1] if s else None

    def back(sid, n):
        s = fr.get(sid)
        return s[-1 - n][1] if s and len(s) > n else None

    cpi = fr.get("CPIAUCSL") or []
    yoy = lambda i: (cpi[-1 - i][1] / cpi[-13 - i][1] - 1) * 100 if len(cpi) > 13 + i else None

    yh = {sym: yahoo.chart(sym, "2y") for sym in ("^GSPC", "KRW=X", "DX-Y.NYB", "CL=F")}
    spx_closes = [c for _, c in (yh["^GSPC"] or [])]
    gap = tech.technicals(spx_closes).get("ma200_gap") if spx_closes else None

    reg = regime({"vix": last("VIXCLS"), "curve_10y2y": last("T10Y2Y"), "cpi_yoy": yoy(0), "cpi_yoy_3m_ago": yoy(3),
                  "y10": last("DGS10"), "y10_3m_ago": back("DGS10", 63), "spx_gap200": gap,
                  "unemp": last("UNRATE"), "unemp_6m_ago": back("UNRATE", 6)})

    def card(id_, label, unit, series, month_back, as_pct=False, decimals=2):
        if not series:
            return None
        v = series[-1][1]
        ref = series[-1 - month_back][1] if len(series) > month_back else None
        chg = None if ref in (None, 0) else ((v / ref - 1) * 100 if as_pct else v - ref)
        return {"id": id_, "label": label, "unit": unit, "value": round(v, decimals), "as_of": str(series[-1][0])[:10],
                "change": None if chg is None else round(chg, 2), "change_unit": "%" if as_pct else unit, "spark": _spark(series)}

    ts_dates = lambda pts: [(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), c) for t, c in pts]
    cards = [c for c in (
        card("y10", "미 국채 10년 금리", "%", fr.get("DGS10"), 21),
        card("curve", "장단기 금리차 (10년−2년)", "%p", fr.get("T10Y2Y"), 21),
        card("fed", "연준 기준금리", "%", fr.get("FEDFUNDS"), 1),
        card("cpi", "소비자물가 상승률 (전년 대비)", "%", [(cpi[i][0], (cpi[i][1] / cpi[i - 12][1] - 1) * 100) for i in range(12, len(cpi))] if len(cpi) > 12 else None, 3),
        card("unemp", "실업률", "%", fr.get("UNRATE"), 3),
        card("vix", "VIX (공포지수)", "", fr.get("VIXCLS"), 21),
        card("spx", "S&P 500", "", ts_dates(yh["^GSPC"] or []), 21, as_pct=True),
        card("krw", "원/달러 환율", "원", ts_dates(yh["KRW=X"] or []), 21, as_pct=True, decimals=1),
        card("dxy", "달러 인덱스", "", ts_dates(yh["DX-Y.NYB"] or []), 21, as_pct=True),
        card("oil", "WTI 유가", "달러", ts_dates(yh["CL=F"] or []), 21, as_pct=True),
    ) if c]
    if not cards and not reg["factors"]:
        print("[market] 거시 자료를 하나도 받지 못해 이전 값을 유지합니다.")
        return
    write_json(d / "market.json", {"fetched_at": iso(), "generated_at": iso(), "regime": reg, "indicators": cards,
                                   "spx": spx_closes[-1] if spx_closes else None})
    print(f"[market] 국면 계수 {reg['coef']} ({reg['label']}, {reg['points']:+d}점), 지표 {len(cards)}개")


# ------------------------------------------------------------------ 희석·자사주 (SEC XBRL)
def run_xbrl(limit=None) -> None:
    from collectors import xbrl
    from collectors.sec13f import Edgar, EdgarError

    try:
        edgar = Edgar()
    except EdgarError as e:
        print(f"[xbrl] {e}")
        return
    d = outdir()
    stocks = _stocks(d, limit)
    if not stocks:
        print("[xbrl] 실제 종목 목록이 없어 건너뜁니다.")
        return
    ciks = xbrl.ticker_map(edgar)
    out = dict((read_json(d / "x_xbrl.json") or {}).get("stocks", {}))
    ok = 0
    for i, s in enumerate(stocks, 1):
        sym = s["symbol"]
        cik = ciks.get(sym.upper()) or ciks.get(sym.upper().replace(".", "-"))
        if not cik:
            continue
        mcap = (s.get("info") or {}).get("market_cap_m")
        try:
            got = xbrl.collect(edgar, cik, mcap * 1e6 if mcap else None)
        except Exception as e:
            print(f"[xbrl] {sym} 실패: {type(e).__name__}: {str(e)[:80]}")
            continue
        if got:
            out[sym] = got
            ok += 1
        if i % 100 == 0:
            print(f"[xbrl] {i}/{len(stocks)}")
    write_json(d / "x_xbrl.json", {"fetched_at": iso(), "stocks": out})
    print(f"[xbrl] {ok}개 종목")


# ------------------------------------------------------------------ 수급: 내부자 거래 + 애널리스트 의견 변화 (Finnhub)
def insider_mspr(data, months=6):
    """Finnhub 내부자 심리(MSPR, −100~+100: 내부자가 순매수면 +). 최근 months 개월 평균."""
    rows = sorted(((r["year"], r["month"], r["mspr"]) for r in (data or []) if r.get("mspr") is not None))[-months:]
    return round(statistics.mean(m for _, _, m in rows), 2) if rows else None


def rec_scores(recs):
    """애널리스트 추천 의견: (수준, 3개월 변화). 강력매수·매수는 +, 매도·강력매도는 −, 범위 −100~+100."""
    def level(r):
        tot = sum(r.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        return (2 * r.get("strongBuy", 0) + r.get("buy", 0) - r.get("sell", 0) - 2 * r.get("strongSell", 0)) / tot * 50 if tot else None
    rs = sorted(recs or [], key=lambda r: r.get("period", ""), reverse=True)
    if not rs or level(rs[0]) is None:
        return None, None
    now_l = level(rs[0])
    past = level(rs[3]) if len(rs) > 3 else (level(rs[-1]) if len(rs) > 1 else None)
    return round(now_l, 1), (None if past is None else round(now_l - past, 1))


def run_flow(limit=None) -> None:
    from collectors.finnhub import Finnhub, FinnhubError

    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        print("[flow] FINNHUB_API_KEY 가 없어 건너뜁니다.")
        return
    s_cfg = load_config("settings")["finnhub"]
    client = Finnhub(key, s_cfg["base_url"], int(os.environ.get("FINNHUB_RPM", s_cfg["requests_per_minute"])))
    d = outdir()
    stocks = _stocks(d, limit)
    out = dict((read_json(d / "x_flow.json") or {}).get("stocks", {}))
    frm = (now() - timedelta(days=200)).strftime("%Y-%m-%d")
    to = now().strftime("%Y-%m-%d")
    ok = 0
    for i, s in enumerate(stocks, 1):
        sym = s["symbol"]
        try:
            ins = client.insider_sentiment(sym, frm, to)
            rec = client.recommendation(sym)
        except FinnhubError as e:
            if "인증" in str(e) and not out:
                print(f"[flow] {e}")
                return
            continue
        except Exception as e:
            print(f"[flow] {sym} 실패: {type(e).__name__}")
            continue
        row = {}
        m = insider_mspr(ins.get("data") if isinstance(ins, dict) else ins)
        if m is not None:
            row["insider_mspr"] = m
        lvl, chg = rec_scores(rec)
        if lvl is not None:
            row["rec_level"] = lvl
        if chg is not None:
            row["rec_change"] = chg
        if row:
            out[sym] = row
            ok += 1
        if i % 50 == 0:
            print(f"[flow] {i}/{len(stocks)}")
    write_json(d / "x_flow.json", {"fetched_at": iso(), "stocks": out})
    print(f"[flow] {ok}개 종목 (API 호출 {client.calls}회)")


# ------------------------------------------------------------------ 해자: 10-K 사업 설명을 Claude(싼 모델)로 요약
def run_moat(limit=None) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from analysis.llm import Analyst, Ledger, LLMUnavailable
    from collectors import tenk, xbrl
    from collectors.sec13f import Edgar, EdgarError

    settings = load_config("settings")
    cfg = settings.get("llm", {})
    d = outdir()
    path = ROOT / "config" / "moat.json"
    moat = read_json(path) or {}
    stocks = _stocks(d)
    if not stocks:
        print("[moat] 실제 종목 목록이 없어 건너뜁니다.")
        return
    cutoff = (now() - timedelta(days=400)).isoformat()
    retry_cutoff = (now() - timedelta(days=30)).isoformat()
    # 시가총액 큰 순서로: 없는 것 → 1년 넘은 것 / 실패 후 30일 지난 것
    order = sorted(stocks, key=lambda s: -((s.get("info") or {}).get("market_cap_m") or 0))
    todo = [s for s in order if (m := moat.get(s["symbol"])) is None
            or (m.get("error") and m.get("generated_at", "") < retry_cutoff)
            or (not m.get("error") and m.get("generated_at", "") < cutoff)]
    cap = limit or cfg.get("moat_per_run", 15)
    if not todo:
        print(f"[moat] 새로 만들 항목이 없습니다 ({len(moat)}개)")
        return
    try:
        edgar = Edgar()
        analyst = Analyst(cfg, Ledger(d / "usage.json", cfg.get("budget_usd", 8.0), ROOT / "config" / "usage_local.json"))
    except (EdgarError, LLMUnavailable) as e:
        print(f"[moat] {e}")
        return
    ciks = xbrl.ticker_map(edgar)
    lock, stop = threading.Lock(), threading.Event()
    state = {"done": 0, "n": 0}

    def work(s):
        if stop.is_set():
            return
        sym = s["symbol"]
        cik = ciks.get(sym.upper()) or ciks.get(sym.upper().replace(".", "-"))
        rec = {"generated_at": iso()}
        try:
            k = tenk.latest_10k(edgar, cik) if cik else None
            text = tenk.fetch_item1(edgar, cik, k) if k else ""
        except Exception as e:
            k, text = None, ""
            rec["error"] = f"10-K 를 읽지 못함 ({type(e).__name__})"
        try:
            if k and len(text) >= 2000:
                res = analyst.moat({"symbol": sym, "name": s["name"], "sector": s["sector"], "filing": {"form": "10-K", "filed": k["filed"], "period_end": k["period"]}, "business_description": text})
                if res:
                    rec.update(res)
                    rec.update({"filed": k["filed"], "accession": k["accession"]})
                    state["done"] += 1
                else:
                    rec["error"] = "분석 결과 없음"
            elif "error" not in rec:
                rec["error"] = "사업 설명 본문을 찾지 못함"
        except LLMUnavailable as e:
            print(f"[moat] 중단: {e}", flush=True)
            stop.set()
            return
        with lock:
            moat[sym] = rec
            write_json(path, moat)
            state["n"] += 1
            if state["n"] % 25 == 0:
                print(f"[moat] {state['n']}개 처리 (누적 AI 비용 ${analyst.ledger.spent:.4f})", flush=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, todo[:cap]))
    write_json(path, moat)
    print(f"[moat] 이번에 {state['done']}개 분석, 전체 {sum(1 for m in moat.values() if not m.get('error'))}개 (누적 AI 비용 ${analyst.ledger.spent:.4f})", flush=True)


def moat_extras() -> dict:
    out = {}
    for sym, m in (read_json(ROOT / "config" / "moat.json") or {}).items():
        if m.get("error") or m.get("moat_score") is None:
            continue
        out[sym] = {"score": max(0, min(100, int(m["moat_score"]))),
                    "detail": {k: m.get(k) for k in ("summary", "sources", "threats", "confidence", "filed")}}
    return out


# ------------------------------------------------------------------ 백테스트 (모멘텀 단독)
def run_backtest_job() -> None:
    d = outdir()
    weekly = (read_json(d / "prices_weekly.json") or {}).get("weekly", {})
    bench = weekly.get("^GSPC")
    stocks = {s: v for s, v in weekly.items() if s != "^GSPC"}
    if not bench or len(stocks) < 100:
        print("[backtest] 가격 이력이 부족해 건너뜁니다 (prices 를 먼저 실행).")
        return
    res = run_backtest(stocks, bench)
    if not res:
        print("[backtest] 기간이 짧아 계산하지 못했습니다.")
        return
    write_json(d / "backtest.json", {"fetched_at": iso(), "generated_at": iso(), "universe": len(stocks), "result": res,
                                     "caveats": ["가격 모멘텀(12개월 수익률, 최근 1개월 제외) 상위 50종목을 월말에 동일 비중으로 담는 전략만 검증합니다. 종합 점수 전체가 아닙니다.",
                                                 "현재 S&P 500 편입 종목만 쓰므로 중간에 빠진 종목이 없는 ‘생존 편향’이 있어 실제보다 좋게 나옵니다.",
                                                 "거래 비용은 리밸런싱 때 바뀐 비중에 0.2%만 반영했고 세금·환전 비용은 없습니다.",
                                                 "과거 성과가 앞으로를 보장하지 않습니다."]})
    s = res["stats"]
    print(f"[backtest] {res['start']}~{res['end']} 모멘텀 연 {s['momentum'].get('cagr_pct')}% vs 동일비중 {s['equal'].get('cagr_pct')}% vs S&P {s['sp500'].get('cagr_pct')}%")


# ------------------------------------------------------------------ 점수 계산에 합치기
def merge_extras(stocks: list[dict], d, weights: dict) -> None:
    """수집한 추가 지표를 종목별 metrics / info 에 넣는다 (저장은 안 함). 위험 등급은 최대 낙폭까지 보고 다시 정한다."""
    sources = [(read_json(d / f"x_{n}.json") or {}).get("stocks", {}) for n in ("prices", "xbrl", "flow")]
    risk = weights.get("risk") or {}
    for s in stocks:
        info = s.setdefault("info", {})
        for src in sources:
            for k, v in (src.get(s["symbol"]) or {}).items():
                (info if k in INFO_KEYS else s["metrics"])[k] = v
        beta, mdd = info.get("beta"), info.get("mdd_1y")
        if beta is not None or mdd is not None:
            high = (beta is not None and beta >= risk.get("beta_high", 1.3)) or (mdd is not None and mdd <= risk.get("mdd_high", -40))
            low = (beta is None or beta <= risk.get("beta_low", 0.8)) and (mdd is None or mdd >= risk.get("mdd_low", -20))
            info["risk_level"] = "high" if high else "low" if low else "mid"


def snapshot(scored: list[dict], d, market: dict | None, ny_date: str) -> None:
    """전진 검증: 하루에 한 번 종합 점수 상위 20개와 그날 가격을 기록해 둔다. 이후 수익률은 화면에서 S&P 500 과 비교한다."""
    path = d / "snapshots.json"
    data = read_json(path) or {"snapshots": []}
    snaps = data["snapshots"]
    if snaps and snaps[-1]["date"] == ny_date:
        return
    picks = [[s["symbol"], (s.get("quote") or {}).get("c")] for s in scored if s.get("score") is not None][:20]
    picks = [p for p in picks if p[1]]
    if len(picks) < 10:
        return
    snaps.append({"date": ny_date, "spx": (market or {}).get("spx"), "picks": picks})
    write_json(path, {"snapshots": snaps[-400:]})
