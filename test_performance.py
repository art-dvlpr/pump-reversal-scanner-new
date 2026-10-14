"""Forward-only signal outcome accounting, with ambiguous candles kept separate."""

from __future__ import annotations

import unittest

from monitor import Bar, FIVE_MINUTES
from performance import (advance_evaluations, apply_bar, outcome,
                         prepare_milestone_report, record_alert, summarize)


class FakeBingX:
    def __init__(self, bars):
        self.rows = bars

    def bars(self, symbol, interval, limit):
        assert symbol == "TEST-USDT" and interval == "5m" and limit == 400
        return self.rows


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.sent = 100 * FIVE_MINUTES + 90_000
        self.short = {"stage": "reversal", "symbol": "TEST-USDT", "entry": 100,
                      "stop": 105, "pump_pct": 60, "retrace_pct": 7,
                      "volume_ratio": 2.5, "old_peak_multiple": 10,
                      "spread_pct": 0.1, "risk_pct": 5}

    def test_first_full_candle_and_ordered_targets(self):
        record = record_alert(self.short, {}, self.sent, "v1")
        first = record["first_full_bar_ms"]
        self.assertEqual(first, 101 * FIVE_MINUTES)
        apply_bar(record, Bar(first, 100, 103, 96, 98, 100))
        apply_bar(record, Bar(first + FIVE_MINUTES, 98, 104, 94, 95, 100))
        apply_bar(record, Bar(first + 2 * FIVE_MINUTES, 95, 106, 93, 104, 100))
        self.assertEqual(outcome(record), "tp_first")
        self.assertEqual(outcome(record, 1), "sl_first")
        self.assertEqual(record["max_favorable_pct"], 7)
        self.assertEqual(record["max_adverse_pct"], 6)
        self.assertEqual(record["strategy_version"], "v1")

    def test_same_candle_sl_and_tp_is_not_counted_as_win(self):
        record = record_alert(self.short, {}, self.sent)
        start = record["first_full_bar_ms"]
        record["end_ms"] = start + FIVE_MINUTES
        state = {"evaluations": [record]}
        # A pre-alert candle has large extremes and must be ignored.
        bars = [Bar(start - FIVE_MINUTES, 100, 150, 50, 100, 100),
                Bar(start, 100, 106, 94, 100, 100)]
        result = advance_evaluations(FakeBingX(bars), state, start + FIVE_MINUTES)
        self.assertEqual(result["completed_now"], 1)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(outcome(record), "same_bar")
        self.assertEqual(record["max_favorable_pct"], 6)
        self.assertEqual(record["max_adverse_pct"], 6)
        # Replaying a run must not double-count a completed case.
        advance_evaluations(FakeBingX(bars), state, start + 2 * FIVE_MINUTES)
        self.assertEqual(state["completed_total"], 1)

    def test_long_uses_opposite_price_direction(self):
        signal = {"stage": "momentum_confirmed", "symbol": "TEST-USDT",
                  "entry": 100, "stop": 95, "gain15_pct": 4,
                  "oi_change_pct": 3, "spread_pct": 0.1}
        record = record_alert(signal, {}, self.sent)
        first = record["first_full_bar_ms"]
        apply_bar(record, Bar(first, 100, 106, 98, 104, 100))
        apply_bar(record, Bar(first + FIVE_MINUTES, 104, 105, 94, 96, 100))
        self.assertEqual(outcome(record), "tp_first")
        self.assertEqual(record["max_favorable_pct"], 6)
        self.assertEqual(record["max_adverse_pct"], 6)

    def test_missing_candle_is_a_gap_not_a_qualifying_outcome(self):
        record = record_alert(self.short, {}, self.sent)
        start = record["first_full_bar_ms"]
        state = {"evaluations": [record]}
        bars = [Bar(start + FIVE_MINUTES, 100, 101, 90, 91, 100)]
        result = advance_evaluations(FakeBingX(bars), state, start + 2 * FIVE_MINUTES)
        self.assertEqual(result["data_gaps_now"], 1)
        self.assertEqual(record["status"], "data_gap")
        self.assertEqual(state["completed_total"], 0)
        self.assertEqual(state["data_gap_total"], 1)

    def test_hundred_case_report_keeps_both_sides_separate(self):
        records = []
        for index in range(100):
            row = record_alert(self.short, {}, self.sent + index * FIVE_MINUTES)
            row["status"] = "complete"
            row["completed_at_ms"] = row["end_ms"]
            row["first_tp_ms"][0] = row["first_full_bar_ms"]
            records.append(row)
        state = {"evaluations": records, "completed_total": 100}
        report = prepare_milestone_report(state, self.sent + 25 * 3_600_000)
        self.assertEqual(report["milestone"], 100)
        self.assertEqual(report["summary"]["by_side"]["short"]["tp_first"], 100)
        self.assertEqual(report["summary"]["by_side"]["long"]["n"], 0)
        self.assertEqual(state["next_report_at"], 200)
        self.assertIsNone(prepare_milestone_report(state, self.sent + 25 * 3_600_000))
        self.assertEqual(summarize(records)["overall"]["n"], 100)


if __name__ == "__main__":
    unittest.main()
