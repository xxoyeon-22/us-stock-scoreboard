import unittest
from datetime import datetime, timezone

from analysis.scoring import percentile_scores, score_universe
from collectors import news

WEIGHTS = {
    "min_group_size": 3,
    "regime": {"min": 0.8, "max": 1.1, "current": 1.0},
    "components": {
        "fundamental": {"weight": 0.35, "metrics": {"growth": {"higher_better": True}}},
        "valuation": {"weight": 0.20, "metrics": {"pe": {"higher_better": False, "positive_only": True}}},
        "investors": {"weight": 0.20},
    },
}


def stock(sym, sector, growth, pe):
    return {"symbol": sym, "sector": sector, "metrics": {"growth": growth, "pe": pe}}


class PercentileTest(unittest.TestCase):
    def test_order_and_range(self):
        p = percentile_scores({"a": 1, "b": 2, "c": 3})
        self.assertLess(p["a"], p["b"])
        self.assertLess(p["b"], p["c"])
        self.assertTrue(all(0 <= v <= 100 for v in p.values()))

    def test_lower_better_and_missing(self):
        p = percentile_scores({"a": 10, "b": 30, "c": None}, higher_better=False)
        self.assertGreater(p["a"], p["b"])
        self.assertNotIn("c", p)

    def test_negative_pe_is_worst(self):
        p = percentile_scores({"a": -5, "b": 10, "c": 40}, higher_better=False, positive_only=True)
        self.assertEqual(p["a"], 0.0)
        self.assertGreater(p["b"], p["c"])


class ScoreTest(unittest.TestCase):
    def test_ranking(self):
        stocks = [stock("A", "T", 30, 10), stock("B", "T", 10, 20), stock("C", "T", -5, 40)]
        out = score_universe(stocks, WEIGHTS)
        self.assertEqual([s["symbol"] for s in out], ["A", "B", "C"])

    def test_missing_component_is_excluded_not_zero(self):
        stocks = [stock("A", "T", 30, None), stock("B", "T", 10, 20), stock("C", "T", -5, 40)]
        a = next(s for s in score_universe(stocks, WEIGHTS) if s["symbol"] == "A")
        self.assertNotIn("valuation", a["components"])
        # 밸류에이션이 없으면 펀더멘털 점수가 그대로 종합 점수가 된다
        self.assertEqual(a["score"], a["components"]["fundamental"]["score"])

    def test_no_data_gives_none(self):
        stocks = [stock("A", "T", None, None), stock("B", "T", 1, 1), stock("C", "T", 2, 2), stock("D", "T", 3, 3)]
        a = next(s for s in score_universe(stocks, WEIGHTS) if s["symbol"] == "A")
        self.assertIsNone(a["score"])

    def test_regime_coefficient_is_clamped(self):
        w = {**WEIGHTS, "regime": {"min": 0.8, "max": 1.1, "current": 5}}
        stocks = [stock("A", "T", 30, 10), stock("B", "T", 10, 20), stock("C", "T", -5, 40)]
        base = {s["symbol"]: s["raw_score"] for s in score_universe(stocks, WEIGHTS)}
        out = {s["symbol"]: s["score"] for s in score_universe(stocks, w)}
        self.assertAlmostEqual(out["B"], min(100, base["B"] * 1.1), places=1)

    def test_small_sector_falls_back_to_universe(self):
        stocks = [stock("A", "T", 30, 10), stock("B", "T", 10, 20), stock("C", "T", -5, 40), stock("X", "Solo", 50, 5)]
        x = next(s for s in score_universe(stocks, WEIGHTS) if s["symbol"] == "X")
        self.assertGreater(x["score"], 80)


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Nvidia shares jump after earnings &amp; guidance</title><link>https://example.com/a</link>
<description>&lt;p&gt;Chipmaker beats.&lt;/p&gt;</description><pubDate>Mon, 16 Nov 2026 14:00:00 GMT</pubDate></item>
<item><title>No link item</title></item>
</channel></rss>"""


class NewsTest(unittest.TestCase):
    def test_parse_feed(self):
        items = news.parse_feed(RSS, "Test", "test", 280)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Nvidia shares jump after earnings & guidance")
        self.assertEqual(items[0]["summary"], "Chipmaker beats.")
        self.assertTrue(items[0]["published"].startswith("2026-11-16T14:00:00"))

    def test_matcher(self):
        uni = [{"symbol": "NVDA", "name": "Nvidia", "sector": "IT"}, {"symbol": "TGT", "name": "Target Corporation", "sector": "CD"},
               {"symbol": "HD", "name": "Home Depot (The)", "sector": "CD"}]
        m = news.build_matcher(uni, {"NVDA": ["NVIDIA"]})
        self.assertEqual(m("Nvidia and Home Depot rally"), ["HD", "NVDA"])
        self.assertEqual(m("Fed misses inflation Target"), [])
        self.assertEqual(m("Retailer (NYSE: TGT) slides, $HD too"), ["HD", "TGT"])

    def test_merge_dedupes_and_expires(self):
        now = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
        mk = lambda t, src, pub: {"id": t, "title": t, "summary": "", "url": "https://e.com", "source": src,
                                  "source_id": src, "published": pub, "tickers": []}
        out = news.merge_news(
            [mk("x", "A", "2026-11-16T10:00:00+00:00"), mk("old", "A", "2026-11-01T10:00:00+00:00")],
            [mk("x", "B", "2026-11-16T11:00:00+00:00"), mk("y", "B", "2026-11-16T12:00:00+00:00")],
            {"retention_days": 7}, now)
        self.assertEqual([i["id"] for i in out], ["y", "x"])
        self.assertEqual(out[1]["also"], ["B"])


if __name__ == "__main__":
    unittest.main()
