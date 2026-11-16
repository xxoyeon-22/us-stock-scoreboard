import json
import tempfile
import unittest
from pathlib import Path

import jobs


class FlowTest(unittest.TestCase):
    def test_insider_mspr_averages_recent_months(self):
        data = [{"year": 2026, "month": m, "mspr": v} for m, v in ((1, 100.0), (2, -20.0), (3, 20.0), (4, 40.0), (5, 0.0), (6, 10.0), (7, 30.0))]
        self.assertEqual(jobs.insider_mspr(data, months=6), round((-20 + 20 + 40 + 0 + 10 + 30) / 6, 2))
        self.assertIsNone(jobs.insider_mspr([]))
        self.assertIsNone(jobs.insider_mspr([{"year": 2026, "month": 1, "mspr": None}]))

    def test_rec_scores_level_and_change(self):
        rec = lambda period, sb, b, h, s, ss: {"period": period, "strongBuy": sb, "buy": b, "hold": h, "sell": s, "strongSell": ss}
        recs = [rec("2026-11-01", 10, 10, 5, 0, 0), rec("2026-10-01", 8, 10, 7, 0, 0), rec("2026-09-01", 6, 10, 9, 0, 0), rec("2026-08-01", 2, 8, 10, 3, 2)]
        level, change = jobs.rec_scores(recs)
        self.assertGreater(level, 40)                  # 매수 우세
        self.assertGreater(change, 0)                  # 3개월 전보다 좋아짐
        self.assertEqual(jobs.rec_scores([]), (None, None))
        self.assertEqual(jobs.rec_scores([rec("2026-11-01", 0, 0, 0, 0, 0)]), (None, None))


class MergeTest(unittest.TestCase):
    def test_merge_puts_scored_values_in_metrics_and_display_values_in_info(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "x_prices.json").write_text(json.dumps({"stocks": {"AAA": {"ma200_gap": 12.0, "ma_cross": 3.0, "mdd_1y": -45.0, "vol_1y": 30.0}}}))
            (d / "x_flow.json").write_text(json.dumps({"stocks": {"AAA": {"insider_mspr": 5.0}}}))
            stocks = [{"symbol": "AAA", "metrics": {}, "info": {"beta": 1.0}}, {"symbol": "BBB", "metrics": {}, "info": {"beta": 0.7}}]
            jobs.merge_extras(stocks, d, {"risk": {"beta_high": 1.3, "beta_low": 0.8, "mdd_high": -40, "mdd_low": -20}})
            a, b = stocks
            self.assertEqual(a["metrics"], {"ma200_gap": 12.0, "ma_cross": 3.0, "insider_mspr": 5.0})
            self.assertEqual(a["info"]["mdd_1y"], -45.0)
            self.assertEqual(a["info"]["risk_level"], "high")      # 베타는 보통이지만 최대 낙폭이 깊음
            self.assertEqual(b["info"]["risk_level"], "low")


class SnapshotTest(unittest.TestCase):
    def test_one_snapshot_per_day(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            scored = [{"symbol": f"S{i}", "score": 90 - i, "quote": {"c": 10.0 + i}} for i in range(25)]
            jobs.snapshot(scored, d, {"spx": 7000.0}, "2026-11-16")
            jobs.snapshot(scored, d, {"spx": 7100.0}, "2026-11-16")        # 같은 날 두 번째는 무시
            jobs.snapshot(scored, d, {"spx": 7200.0}, "2026-11-17")
            snaps = json.loads((d / "snapshots.json").read_text())["snapshots"]
            self.assertEqual([s["date"] for s in snaps], ["2026-11-16", "2026-11-17"])
            self.assertEqual(len(snaps[0]["picks"]), 20)
            self.assertEqual(snaps[0]["spx"], 7000.0)

    def test_too_few_priced_picks_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            jobs.snapshot([{"symbol": "A", "score": 50, "quote": {"c": 1.0}}], Path(t), {}, "2026-11-16")
            self.assertFalse((Path(t) / "snapshots.json").exists())


if __name__ == "__main__":
    unittest.main()
