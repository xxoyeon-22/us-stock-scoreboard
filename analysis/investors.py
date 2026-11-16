"""투자자 포지션 → 종목별 신호 점수 (0~100).

신규 편입 > 비중 확대 > 유지 > 축소 > 전량 매도. 포트폴리오 비중이 클수록, 투자자 신뢰도(weight)가
높을수록 크게 반영하고, 같은 분기에 여러 투자자가 함께 매수하면 가산점을 준다.
13F 는 최대 45일 지연된 롱 포지션만 보여주므로 옵션 행은 점수에서 뺀다.
"""
from collections import defaultdict


def build_signals(investors: list[dict], cfg: dict) -> dict:
    """{티커: {"score": float, "detail": [{id, name, action, pct, change_pct}]}}"""
    action_scores = cfg.get("action_scores", {"new": 100, "add": 80, "hold": 60, "trim": 35, "exit": 10})
    bonus = cfg.get("consensus_bonus", 5)
    rows = defaultdict(list)
    for inv in investors:
        w = float(inv.get("weight", 1.0))
        for h in list(inv.get("holdings_all") or inv.get("holdings") or []) + list(inv.get("exits") or []):
            if not h.get("ticker") or h.get("put_call") or not h.get("action"):
                continue
            size = 0.5 + 0.5 * min(h.get("pct") or 0, 10) / 10      # 비중 10% 이상이면 최대
            rows[h["ticker"]].append({
                "id": inv["id"], "name": inv["name"], "action": h["action"], "pct": round(h.get("pct") or 0, 2),
                "change_pct": None if h.get("change_pct") is None else round(h["change_pct"], 1),
                "_w": w * size, "_s": action_scores.get(h["action"], 60),
            })
    out = {}
    for ticker, items in rows.items():
        wsum = sum(i["_w"] for i in items)
        if wsum <= 0:
            continue
        base = sum(i["_w"] * i["_s"] for i in items) / wsum
        buyers = sum(1 for i in items if i["action"] in ("new", "add"))
        score = max(0.0, min(100.0, base + bonus * max(0, buyers - 1)))
        detail = sorted(({k: v for k, v in i.items() if not k.startswith("_")} for i in items), key=lambda d: -d["pct"])
        out[ticker] = {"score": round(score, 1), "detail": detail}
    return out
