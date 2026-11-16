import unittest

from analysis.investors import build_signals
from analysis.scoring import score_universe
from collectors.sec13f import compare, parse_info_table
from pipeline import extract_info, extract_metrics

XML = """<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>600</value>
<shrsOrPrnAmt><sshPrnamt>30</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>400</value>
<shrsOrPrnAmt><sshPrnamt>20</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50</value>
<shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
</informationTable>"""


def h(cusip, shares, value=100.0, put_call=None):
    return {"cusip": cusip, "issuer": cusip, "cls": "COM", "put_call": put_call, "value": value, "shares": shares, "is_shares": True}


class Sec13fTest(unittest.TestCase):
    def test_parse_aggregates_rows_and_separates_options(self):
        rows = parse_info_table(XML)
        self.assertEqual(len(rows), 2)
        stock = next(r for r in rows if r["put_call"] is None)
        self.assertEqual((stock["value"], stock["shares"]), (1000.0, 50.0))
        self.assertEqual(next(r for r in rows if r["put_call"])["put_call"], "PUT")

    def test_compare_actions(self):
        cur = [h("NEW", 10), h("ADD", 120), h("HOLD", 101), h("TRIM", 50)]
        prev = [h("ADD", 100), h("HOLD", 100), h("TRIM", 100), h("GONE", 100)]
        held, exits = compare(cur, prev, 0.05)
        self.assertEqual({r["cusip"]: r["action"] for r in held}, {"NEW": "new", "ADD": "add", "HOLD": "hold", "TRIM": "trim"})
        self.assertEqual([r["cusip"] for r in exits], ["GONE"])
        self.assertAlmostEqual(sum(r["pct"] for r in held), 100.0)

    def test_compare_without_previous_has_no_action(self):
        held, exits = compare([h("A", 1)], None)
        self.assertIsNone(held[0]["action"])
        self.assertEqual(exits, [])


class SignalTest(unittest.TestCase):
    CFG = {"action_scores": {"new": 100, "add": 80, "hold": 60, "trim": 35, "exit": 10}, "consensus_bonus": 5}

    def inv(self, id_, rows, weight=1.0):
        return {"id": id_, "name": id_, "weight": weight, "holdings_all": rows, "exits": []}

    def test_order_and_consensus(self):
        row = lambda t, a: {"ticker": t, "action": a, "pct": 5, "change_pct": None, "put_call": None}
        sig = build_signals([self.inv("a", [row("X", "new"), row("Y", "trim"), row("Z", "add")]),
                             self.inv("b", [row("Z", "add")])], self.CFG)
        self.assertGreater(sig["X"]["score"], sig["Z"]["score"] - 5)
        self.assertGreater(sig["Z"]["score"], sig["Y"]["score"])
        self.assertEqual(sig["Z"]["score"], 85.0)        # 확대 80 + 동시 매수 가산 5
        self.assertEqual(len(sig["Z"]["detail"]), 2)

    def test_options_and_unmapped_are_ignored(self):
        sig = build_signals([self.inv("a", [{"ticker": "X", "action": "new", "pct": 5, "put_call": "PUT"},
                                            {"ticker": None, "action": "new", "pct": 5, "put_call": None}])], self.CFG)
        self.assertEqual(sig, {})


class ExtrasTest(unittest.TestCase):
    W = {"min_group_size": 2, "regime": {"current": 1.0},
         "components": {"fundamental": {"weight": 0.5, "metrics": {"g": {"higher_better": True}}}, "investors": {"weight": 0.5}}}

    def test_precomputed_component_is_blended_only_where_present(self):
        stocks = [{"symbol": s, "sector": "T", "metrics": {"g": g}} for s, g in (("A", 1), ("B", 2), ("C", 3))]
        out = {s["symbol"]: s for s in score_universe(stocks, self.W, {"investors": {"A": {"score": 100, "detail": []}}})}
        self.assertIn("investors", out["A"]["components"])
        self.assertNotIn("investors", out["B"]["components"])
        self.assertAlmostEqual(out["A"]["score"], (out["A"]["components"]["fundamental"]["score"] + 100) / 2, places=1)
        self.assertEqual(out["B"]["score"], out["B"]["components"]["fundamental"]["score"])


class MetricTest(unittest.TestCase):
    W = {"risk": {"beta_high": 1.3, "beta_low": 0.8, "min_dollar_volume": 2e7},
         "info": {"beta": ["beta"], "high_52w": ["52WeekHigh"], "avg_volume_10d_m": ["vol"]},
         "components": {"c": {"weight": 1, "metrics": {
             "trend": {"source": "diff", "keys": ["m"], "keys_b": ["m5"]},
             "pos": {"source": "range", "keys": ["52WeekLow"], "keys_b": ["52WeekHigh"]},
             "plain": {"source": "metric", "keys": ["missing", "x"]}}}}}

    def test_derived_metrics(self):
        raw = {"m": 30.0, "m5": 25.0, "52WeekLow": 50, "52WeekHigh": 150, "x": 7}
        m = extract_metrics(raw, [], self.W, price=125)
        self.assertEqual((m["trend"], m["pos"], m["plain"]), (5.0, 75.0, 7.0))
        self.assertIsNone(extract_metrics(raw, [], self.W, price=None)["pos"])

    def test_risk_flags(self):
        info = extract_info({"beta": 1.5, "52WeekHigh": 200, "vol": 0.1}, self.W, price=100)
        self.assertEqual(info["risk_level"], "high")
        self.assertTrue(info["low_liquidity"])            # 0.1백만 주 × $100 = $1천만 < $2천만
        self.assertEqual(info["from_high_pct"], -50.0)


if __name__ == "__main__":
    unittest.main()
