"""Forward-only signal outcome accounting, with ambiguous candles kept separate."""

from __future__ import annotations

import unittest

from monitor import Bar, FIVE_MINUTES
from performance import (DIGEST_INTERVAL_MS, advance_evaluations, apply_bar,
                         mark_digest_sent, outcome, prepare_digest,
                         prepare_milestone_report, record_alert,
                         recurring_patterns, render_digest, summarize)


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

    def test_new_short_uses_pump_base_target_and_seventy_two_hours(self):
        signal = {**self.short, "pump_base": 70, "pump_target": 73,
                  "route_pct": 27, "reward_risk": 5.4}
        record = record_alert(signal, {}, self.sent, "new")
        self.assertEqual(record["target_scheme"], "pump_base_90")
        self.assertEqual(record["targets"], [73])
        self.assertEqual(record["end_ms"] - record["first_full_bar_ms"],
                         72 * 3_600_000)
        first = record["first_full_bar_ms"]
        apply_bar(record, Bar(first, 100, 104, 95, 98, 100))
        apply_bar(record, Bar(first + FIVE_MINUTES, 98, 103, 73, 76, 100))
        self.assertEqual(outcome(record), "tp_first")
        old = record_alert(self.short, {}, self.sent, "old")
        for row in (record, old):
            row["status"] = "complete"
        summary = summarize([record, old])
        self.assertEqual(summary["by_scheme"]["pump_base_90"]["tp_first"], 1)
        self.assertEqual(summary["by_scheme"]["risk_multiple"]["n"], 1)

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

    def test_three_day_digest_counts_outcomes_and_waits_for_telegram(self):
        state = {"evaluations": [], "completed_total": 0, "data_gap_total": 0}
        start = 1_800_000_000_000
        self.assertIsNone(prepare_digest(state, start))
        record = record_alert(self.short, {}, start + FIVE_MINUTES, "v1")
        record["status"] = "complete"
        record["completed_at_ms"] = start + 30 * 3_600_000
        record["first_tp_ms"][0] = record["first_full_bar_ms"]
        state["evaluations"] = [record]
        state["completed_total"] = 1
        state["data_gap_total"] = 1
        self.assertIsNone(prepare_digest(state, start + DIGEST_INTERVAL_MS - 1))
        due = start + DIGEST_INTERVAL_MS
        report = prepare_digest(state, due)
        self.assertEqual(report["complete_total"], 1)
        self.assertEqual(report["summary"]["by_side"]["short"]["tp_first"], 1)
        self.assertEqual(report["data_gaps"], 1)
        self.assertIn("TP1→SL: 1", render_digest(report))
        # Preparation alone must not suppress a retry after Telegram failure.
        self.assertEqual(prepare_digest(state, due)["complete_total"], 1)
        mark_digest_sent(state, report, 123)
        self.assertIsNone(prepare_digest(state, due + FIVE_MINUTES))
        self.assertEqual(state["digest_history"][-1]["message_id"], 123)

    def test_no_pattern_claim_on_small_sample(self):
        record = record_alert(self.short, {}, self.sent, "v1")
        record["status"] = "complete"
        record["completed_at_ms"] = record["end_ms"]
        self.assertEqual(recurring_patterns([record] * 79), [])

    def test_pattern_requires_same_direction_in_both_cohorts(self):
        records = []
        for index in range(80):
            row = record_alert(self.short, {}, self.sent + index * FIVE_MINUTES, "v1")
            row["status"] = "complete"
            row["completed_at_ms"] = row["end_ms"]
            high_pump = index % 40 < 20
            row["features"]["pump_pct"] = 100 if high_pump else 20
            if high_pump:
                row["first_tp_ms"][0] = row["first_full_bar_ms"]
            else:
                row["first_sl_ms"] = row["first_full_bar_ms"]
            records.append(row)
        patterns = recurring_patterns(records)
        self.assertTrue(any(item["feature"] == "pump_pct" and
                            item["direction"] == "stronger" for item in patterns))
        for row in records[40:]:
            row["first_sl_ms"], row["first_tp_ms"][0] = (
                row["first_tp_ms"][0], row["first_sl_ms"])
        self.assertEqual(recurring_patterns(records), [])


if __name__ == "__main__":
    unittest.main()
