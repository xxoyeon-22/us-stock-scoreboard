import unittest
from datetime import date

from analysis import regime, technicals
from analysis.backtest import run_backtest
from collectors import tenk, xbrl


class TechnicalsTest(unittest.TestCase):
    def test_moving_average_gap_and_cross(self):
        up = [100 + i for i in range(260)]
        t = technicals.technicals(up)
        self.assertGreater(t["ma200_gap"], 0)
        self.assertGreater(t["ma_cross"], 0)          # 상승 추세: 50일선이 200일선 위
        down = list(reversed(up))
        self.assertLess(technicals.technicals(down)["ma_cross"], 0)

    def test_max_drawdown(self):
        series = [100] * 100 + [50] + [60] * 160
        self.assertAlmostEqual(technicals.technicals(series)["mdd_1y"], -50.0, places=1)

    def test_too_short_history_gives_nothing(self):
        self.assertEqual(technicals.technicals([1.0] * 10), {})

    def test_weekly_keeps_last_close_per_iso_week(self):
        # 2026-11-09(월)~13(금) 은 같은 ISO 주
        base = 1_794_000_000
        pts = [(base + i * 86400, float(i)) for i in range(10)]
        w = technicals.weekly(pts)
        self.assertEqual(len(w), len({k for k, _ in w}))
        self.assertTrue(all(k.count("-W") == 1 for k, _ in w))


class RegimeTest(unittest.TestCase):
    def test_friendly_and_risky_are_clamped(self):
        good = regime.regime({"vix": 12, "curve_10y2y": 1.0, "cpi_yoy": 2.0, "cpi_yoy_3m_ago": 2.6, "y10": 3.5, "y10_3m_ago": 4.2, "spx_gap200": 8, "unemp": 4.0, "unemp_6m_ago": 4.0})
        bad = regime.regime({"vix": 35, "curve_10y2y": -0.5, "cpi_yoy": 5.0, "cpi_yoy_3m_ago": 4.0, "y10": 5.0, "y10_3m_ago": 4.2, "spx_gap200": -9, "unemp": 5.0, "unemp_6m_ago": 4.2})
        self.assertEqual(good["coef"], 1.1)
        self.assertEqual(bad["coef"], 0.8)
        self.assertGreater(good["coef"], bad["coef"])

    def test_missing_inputs_are_skipped_not_zeroed(self):
        r = regime.regime({"vix": 12})
        self.assertEqual([f["key"] for f in r["factors"]], ["vix"])
        self.assertEqual(regime.regime({})["coef"], 1.0)


class XbrlTest(unittest.TestCase):
    def fact(self, start, end, val, filed, fp="FY", form="10-K"):
        return {"start": start, "end": end, "val": val, "filed": filed, "fp": fp, "form": form}

    def test_annual_series_ignores_quarters_and_keeps_latest_filing(self):
        c = {"units": {"shares": [
            self.fact("2023-10-01", "2024-09-28", 100, "2024-11-01"),
            self.fact("2023-10-01", "2024-09-28", 101, "2024-12-01"),        # 정정: 나중 공시
            self.fact("2024-06-30", "2024-09-28", 25, "2024-11-01"),         # 분기 → 제외
            self.fact("2024-10-01", "2025-09-27", 98, "2025-11-01"),
            self.fact("2025-01-01", "2025-03-29", 24, "2025-05-01", fp="Q2", form="10-Q"),
        ]}}
        self.assertEqual(xbrl.annual_series(c, "shares"), [("2024-09-28", 101.0), ("2025-09-27", 98.0)])

    def test_derive(self):
        d = xbrl.derive([("2024-09-28", 100.0), ("2025-09-27", 103.0)], [], [("2025-09-27", 5e9)], [("2025-09-27", 2e10)], 1e12, today=date(2026, 1, 1))
        self.assertEqual(d["share_change"], 3.0)
        self.assertEqual(d["sbc_yield"], 0.5)
        self.assertEqual(d["buyback_yield"], 2.0)

    def test_stale_data_is_dropped(self):
        d = xbrl.derive([("2020-09-28", 100.0), ("2021-09-27", 90.0)], [], [], [], 1e12, today=date(2026, 1, 1))
        self.assertNotIn("share_change", d)


class TenKTest(unittest.TestCase):
    def test_extract_picks_body_not_table_of_contents(self):
        toc = "Table of Contents Item 1. Business 3 Item 1A. Risk Factors 9 Item 2. Properties 20 "
        body = "Item 1. Business " + "We make widgets and have strong brands. " * 80 + " Item 1A. Risk Factors We face competition."
        out = tenk.extract_item1(tenk.html_to_text(f"<html><body><p>{toc}</p><div>{body}</div></body></html>"))
        self.assertIn("strong brands", out)
        self.assertNotIn("We face competition", out)

    def test_html_to_text_strips_tags_and_entities(self):
        self.assertEqual(tenk.html_to_text("<p>A&amp;B&nbsp;<b>C</b></p><script>x()</script>"), "A&B C")


class BacktestTest(unittest.TestCase):
    def make(self, n_weeks=130, n_stocks=120):
        keys = []
        from datetime import date as d, timedelta
        start = d(2023, 1, 5)
        for i in range(n_weeks):
            y, w, _ = (start + timedelta(weeks=i)).isocalendar()
            keys.append(f"{y}-W{w:02d}")
        weekly = {}
        for s in range(n_stocks):
            drift = (s - n_stocks / 2) / n_stocks * 0.01       # 번호가 클수록 꾸준히 오르는 종목
            p, series = 100.0, []
            for k in keys:
                p *= 1 + drift
                series.append((k, round(p, 2)))
            weekly[f"S{s:03d}"] = series
        bench = [(k, 100 * (1.002 ** i)) for i, k in enumerate(keys)]
        return weekly, bench

    def test_momentum_beats_equal_weight_when_trends_persist(self):
        weekly, bench = self.make()
        r = run_backtest(weekly, bench, top_n=20)
        self.assertIsNotNone(r)
        self.assertGreater(r["stats"]["momentum"]["total_pct"], r["stats"]["equal"]["total_pct"])
        self.assertEqual(len(r["curves"]["momentum"]), len(r["dates"]))

    def test_too_little_history_returns_none(self):
        weekly, bench = self.make(n_weeks=40)
        self.assertIsNone(run_backtest(weekly, bench, top_n=20))


if __name__ == "__main__":
    unittest.main()
