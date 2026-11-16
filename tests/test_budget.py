import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from analysis import llm


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "usage.json"

    def tearDown(self):
        self.tmp.cleanup()

    def analyst(self, budget, spent=0.0):
        ledger = llm.Ledger(self.path, budget)
        if spent:
            ledger.add(spent)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            return llm.Analyst({"model": "claude-sonnet-5-5", "fallbacks": False}, ledger)

    def test_ledger_persists(self):
        a = self.analyst(8.0, spent=1.25)
        self.assertAlmostEqual(llm.Ledger(self.path, 8.0).spent, 1.25)
        self.assertEqual(a.ledger.data["calls"], 1)

    def test_call_blocked_before_any_network_when_budget_cannot_cover_worst_case(self):
        a = self.analyst(budget=0.05, spent=0.049)
        with mock.patch.object(a, "_create") as create:
            with self.assertRaises(llm.LLMUnavailable):
                a._json("sys", {"x": 1}, llm.STOCK_SCHEMA, "stock")
            create.assert_not_called()

    def test_cost_is_added_after_a_call(self):
        a = self.analyst(8.0)
        resp = mock.Mock(stop_reason="end_turn", content=[mock.Mock(type="text", text='{"summary":"s"}')],
                         usage=mock.Mock(input_tokens=1000, output_tokens=2000))
        with mock.patch.object(a, "_create", return_value=resp):
            a._json("sys", {"x": 1}, llm.STOCK_SCHEMA, "stock")
        expected = (1000 * 2.0 + 2000 * 10.0) / 1e6      # claude-sonnet-5-5: $2 / $10 per MTok
        self.assertAlmostEqual(a.ledger.spent, expected, places=6)

    def test_unknown_model_is_priced_conservatively(self):
        self.assertGreater(llm.UNKNOWN_PRICE[1], max(p[1] for p in llm.PRICES.values()))


if __name__ == "__main__":
    unittest.main()
