"""Signal timing and coverage checks for the BingX five-minute scanner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monitor import (Bar, Ticker, candidates, confirm_momentum, evaluate,
                     evaluate_momentum, old_peak, updated_price_snapshots)
from run_monitor import mark_sent, message, select_alerts
from state_store import LocalStateStore

FIVE = 300_000


def sample_bars(now_ms: int) -> list[Bar]:
    bars = []
    for index in range(44):
        base = 0.1 if index < 30 else 0.1 + min(index - 29, 8) * 0.006
        bars.append(Bar(now_ms - (45 - index) * FIVE, base, base * 1.005,
                        base * 0.995, base, 100))
    return bars


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000_000
        self.ticker = Ticker("VELVET-USDT", 0.147, 0.152, 0.1,
                             1_000_000, 0.1469, 0.1471)

    def test_no_top_twenty_cut_and_pinned_quiet_symbol(self):
        tickers = [Ticker(f"X{i}-USDT", 1, 1.2, 1, 200_000, 0.999, 1.001)
                   for i in range(40)]
        tickers.append(Ticker("VELVET-USDT", 1, 1.01, 1, 200_000, 0.999, 1.001))
        chosen = candidates(tickers, {ticker.symbol for ticker in tickers})
        self.assertEqual(len(chosen), 41)

    def test_gradual_hourly_move_below_eight_percent_is_deep_scanned(self):
        ticker = Ticker("SLOW-USDT", 1.035, 1.04, 1, 800_000, 1.034, 1.036)
        previous = {}
        for minute, price in ((-60, 1), (-45, 1.008), (-30, 1.016),
                              (-15, 1.025), (-5, 1.032)):
            previous = updated_price_snapshots(
                [Ticker(ticker.symbol, price, 1.04, 1, 800_000, 1.034, 1.036)],
                {ticker.symbol}, previous, self.now + minute * 60_000)
        self.assertIn(ticker, candidates([ticker], {ticker.symbol}, previous, self.now))

    def test_watch_then_closed_five_minute_breakdown(self):
        bars = sample_bars(self.now)
        self.assertEqual(evaluate(self.ticker, bars, self.now, 2.16)["stage"], "watch")
        last = bars[-1]
        bars[-1] = Bar(last.start_ms, 0.145, 0.146, 0.138, 0.139, 180)
        ticker = Ticker("VELVET-USDT", 0.139, 0.152, 0.1,
                        1_000_000, 0.1389, 0.1391)
        result = evaluate(ticker, bars, self.now, 2.16)
        self.assertEqual(result["stage"], "reversal")
        self.assertEqual(result["reversal_type"], "пробой опоры")
        self.assertLess(result["retrace_pct"], 25)

    def test_forming_candle_cannot_create_signal(self):
        bars = sample_bars(self.now)
        last = bars[-1]
        bars[-1] = Bar(last.start_ms + 2 * FIVE, 0.145, 0.147, 0.1, 0.101, 10000)
        result = evaluate(self.ticker, bars, self.now, 2.16)
        self.assertEqual(result["stage"], "watch")

    def test_old_monthly_peak_excludes_current_month(self):
        month_start = 1_798_761_600_000
        history = [Bar(month_start - 40 * 86_400_000, 1, 2.16, 0.08, 0.1, 1),
                   Bar(month_start, 0.1, 9, 0.1, 0.2, 1)]
        self.assertEqual(old_peak(history, month_start + 3 * 86_400_000), 2.16)

    def test_alert_state_deduplicates_same_episode(self):
        signal = {"symbol": "VELVET-USDT", "stage": "reversal", "actionable": True,
                  "peak_ms": self.now - FIVE,
                  "peak": 0.152, "pump_pct": 50, "retrace_pct": 3,
                  "old_peak_multiple": 15, "event_ms": self.now - FIVE}
        state = {"episodes": {}}
        alerts = select_alerts([signal], state, self.now)
        self.assertEqual(len(alerts), 1)
        mark_sent(signal, state)
        self.assertEqual(select_alerts([signal], state, self.now), [])
        newer = {**signal, "peak_ms": self.now + FIVE, "peak": 0.18}
        self.assertEqual(len(select_alerts([newer], state, self.now + FIVE)), 1)

    def test_only_confirmed_entry_is_selected(self):
        short = {"symbol": "VELVET-USDT", "stage": "watch", "peak_ms": self.now - FIVE,
                 "peak": 0.15, "pump_pct": 30, "retrace_pct": 2,
                 "old_peak_multiple": 10, "event_ms": self.now - FIVE,
                 "actionable": False}
        long_watch = {"symbol": "VELVET-USDT", "stage": "momentum_watch",
                      "gain15_pct": 4, "oi_change_pct": None,
                      "event_ms": self.now - FIVE, "actionable": False}
        long_entry = {**long_watch, "stage": "momentum_confirmed", "actionable": True}
        state = {"episodes": {}}
        alerts = select_alerts([short, long_watch, long_entry], state, self.now)
        self.assertEqual(alerts, [long_entry])
        self.assertEqual(list(state["episodes"]), ["VELVET-USDT#momentum"])
        mark_sent(long_entry, state)
        self.assertEqual(select_alerts([short, long_watch, long_entry], state, self.now), [])

    def test_local_state_survives_new_run(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LocalStateStore(Path(folder) / "state.json")
            state = store.load()
            state["history"]["VELVET-USDT"] = {"high": 2.16, "checked_ms": self.now}
            store.save(state)
            self.assertEqual(store.load()["history"]["VELVET-USDT"]["high"], 2.16)

    def test_local_pump_below_yesterdays_high_is_seen(self):
        bars = [Bar(self.now - (300 - index) * FIVE, 0.1, 0.101, 0.099, 0.1, 100)
                for index in range(300)]
        bars[1] = Bar(bars[1].start_ms, 0.1, 0.22, 0.099, 0.1, 100)
        bars[-6] = Bar(bars[-6].start_ms, 0.1, 0.16, 0.099, 0.155, 1000)
        result = evaluate(self.ticker, bars, self.now, 2.16)
        self.assertIsNotNone(result)
        self.assertEqual(result["peak"], 0.16)

    def test_momentum_needs_separate_oi_snapshot_for_confirmation(self):
        bars = [Bar(self.now - (40 - index) * FIVE, 0.1, 0.101, 0.099, 0.1, 100)
                for index in range(40)]
        old = bars[-4]
        bars[-4] = Bar(old.start_ms, 0.102, 0.1035, 0.102, 0.103, 200)
        for offset, close in ((-3, 0.102), (-2, 0.105), (-1, 0.108)):
            old = bars[offset]
            low = max(close * 0.99, 0.102)
            bars[offset] = Bar(old.start_ms, low, close * 1.002,
                               low, close, 500)
        ticker = Ticker("TEST-USDT", 0.108, 0.11, 0.09, 2_000_000, 0.1079, 0.1081)
        signal = evaluate_momentum(ticker, bars, self.now)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["stage"], "momentum_watch")
        self.assertEqual(confirm_momentum(signal, 110, None, self.now)["stage"],
                         "momentum_watch")
        confirmed = confirm_momentum(signal, 110,
                                     {"value": 100, "at_ms": self.now - FIVE}, self.now)
        self.assertEqual(confirmed["stage"], "momentum_confirmed")
        self.assertTrue(confirmed["target1"] > confirmed["entry"] > confirmed["stop"])
        rendered = message(confirmed)
        self.assertIn("🟢 #LONG", rendered)
        self.assertIn("TP1", rendered)
        self.assertIn("TP2", rendered)
        self.assertIn("TP3", rendered)
        self.assertIn("SL", rendered)
        self.assertIn("━━━━━━━━", rendered)

    def test_watch_cannot_be_formatted_for_telegram(self):
        signal = evaluate(self.ticker, sample_bars(self.now), self.now, 2.16)
        with self.assertRaisesRegex(ValueError, "Only confirmed entry"):
            message(signal)


if __name__ == "__main__":
    unittest.main()
