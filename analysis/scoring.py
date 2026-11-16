"""종합 점수 계산.

- 각 지표는 같은 섹터 내 백분위(0~100)로 정규화한다.
- 구성 요소 점수 = 사용 가능한 지표 백분위의 평균.
- 종목 점수 = 사용 가능한 구성 요소의 가중 평균 (없는 항목은 제외 후 가중치 재분배).
- 최종 점수 = 종목 점수 × 시장 국면 보정 계수.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict


def percentile_scores(values: dict, higher_better: bool = True, positive_only: bool = False) -> dict:
    """{키: 값} → {키: 0~100 백분위}. None 은 결과에서 빠진다.

    positive_only 지표(PER 등)에서 0 이하 값은 순위에서 빼고 0점을 준다.
    """
    valid = {k: v for k, v in values.items() if v is not None and not (positive_only and v <= 0)}
    xs = sorted(valid.values())
    n = len(xs)
    out = {}
    for k, v in valid.items():
        less = bisect_left(xs, v)
        equal = bisect_right(xs, v) - less
        p = (less + 0.5 * equal) / n * 100
        out[k] = p if higher_better else 100 - p
    if positive_only:
        for k, v in values.items():
            if v is not None and v <= 0:
                out[k] = 0.0
    return out


def _metric_percentiles(stocks: list[dict], name: str, spec: dict, min_group: int) -> dict:
    hb = spec.get("higher_better", True)
    po = spec.get("positive_only", False)
    all_values = {s["symbol"]: s["metrics"].get(name) for s in stocks}
    universe_pct = percentile_scores(all_values, hb, po)

    by_sector = defaultdict(dict)
    for s in stocks:
        by_sector[s["sector"]][s["symbol"]] = s["metrics"].get(name)

    out = {}
    for sector, values in by_sector.items():
        n_valid = sum(1 for v in values.values() if v is not None and not (po and v <= 0))
        if n_valid >= min_group:
            out.update(percentile_scores(values, hb, po))
        else:  # 섹터 표본이 너무 작으면 전체 기준
            out.update({k: universe_pct[k] for k in values if k in universe_pct})
    return out


def score_universe(stocks: list[dict], weights: dict) -> list[dict]:
    """stocks: [{symbol, sector, metrics: {지표명: 값|None}}] → 점수가 붙은 목록 (점수 내림차순)."""
    min_group = weights.get("min_group_size", 5)
    regime = weights.get("regime", {})
    coef = max(regime.get("min", 0.8), min(regime.get("max", 1.1), regime.get("current", 1.0)))

    pct: dict[str, dict] = {}
    for comp in weights["components"].values():
        for name, spec in (comp.get("metrics") or {}).items():
            pct[name] = _metric_percentiles(stocks, name, spec, min_group)

    results = []
    for s in stocks:
        components = {}
        weighted, weight_sum = 0.0, 0.0
        for comp_id, comp in weights["components"].items():
            metrics = {}
            for name, spec in (comp.get("metrics") or {}).items():
                value = s["metrics"].get(name)
                p = pct[name].get(s["symbol"])
                if value is not None and p is not None:
                    metrics[name] = {"value": round(value, 3), "pct": round(p, 1)}
            if not metrics:
                continue
            comp_score = sum(m["pct"] for m in metrics.values()) / len(metrics)
            components[comp_id] = {"score": round(comp_score, 1), "metrics": metrics}
            weighted += comp["weight"] * comp_score
            weight_sum += comp["weight"]

        raw = weighted / weight_sum if weight_sum else None
        final = None if raw is None else round(max(0.0, min(100.0, raw * coef)), 1)
        results.append({
            **{k: v for k, v in s.items() if k != "metrics"},
            "score": final,
            "raw_score": None if raw is None else round(raw, 1),
            "components": components,
        })

    results.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["symbol"]))
    return results
