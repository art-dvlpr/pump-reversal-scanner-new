"""Signal timing and coverage checks for the BingX five-minute scanner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import run_monitor
from monitor import (Bar, Ticker, candidates, confirm_momentum, evaluate,
                     evaluate_momentum, old_peak, prior_daily_peak,
                     scan_market, updated_price_snapshots)
from run_monitor import (book_slippage_pct, mark_sent, message, refresh_entry,
                         select_alerts, short_target)
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
        self.assertTrue(result["actionable"])
        self.assertAlmostEqual(result["pump_base"], 0.1)
        self.assertLess(result["pump_base"], result["pump_target"])
        self.assertLess(result["pump_target"], result["entry"])
        self.assertGreaterEqual(result["base_route_pct"], 20)
        self.assertGreaterEqual(result["route_pct"], 20)
        self.assertAlmostEqual(result["main_target"], result["entry"] * 0.8)
        self.assertIn("≥20%", message(result))

    def test_forming_candle_cannot_create_signal(self):
        bars = sample_bars(self.now)
        last = bars[-1]
        bars[-1] = Bar(last.start_ms + 2 * FIVE, 0.145, 0.147, 0.1, 0.101, 10000)
        result = evaluate(self.ticker, bars, self.now, 2.16)
        self.assertEqual(result["stage"], "watch")

    def test_single_low_wick_does_not_move_pump_base(self):
        bars = sample_bars(self.now)
        old = bars[7]
        bars[7] = Bar(old.start_ms, old.open, old.high, 0.001, old.close, old.volume)
        last = bars[-1]
        bars[-1] = Bar(last.start_ms, 0.145, 0.146, 0.138, 0.139, 180)
        ticker = Ticker("VELVET-USDT", 0.139, 0.152, 0.001,
                        1_000_000, 0.1389, 0.1391)
        result = evaluate(ticker, bars, self.now, 2.16)
        self.assertAlmostEqual(result["pump_base"], 0.1)

    def test_late_retrace_is_not_an_entry(self):
        bars = sample_bars(self.now)
        last = bars[-1]
        bars[-1] = Bar(last.start_ms, 0.145, 0.146, 0.125, 0.126, 180)
        ticker = Ticker("VELVET-USDT", 0.126, 0.152, 0.1,
                        1_000_000, 0.1259, 0.1261)
        result = evaluate(ticker, bars, self.now, 2.16)
        self.assertFalse(result["actionable"])
        self.assertEqual(select_alerts([result], {"episodes": {}}, self.now), [])

    def test_old_monthly_peak_excludes_current_month(self):
        month_start = 1_798_761_600_000
        history = [Bar(month_start - 40 * 86_400_000, 1, 2.16, 0.08, 0.1, 1),
                   Bar(month_start, 0.1, 9, 0.1, 0.2, 1)]
        self.assertEqual(old_peak(history, month_start + 3 * 86_400_000), 2.16)

    def test_young_listing_prior_peak_excludes_current_pump(self):
        day = 86_400_000
        history = [Bar(self.now - 10 * day, 1, 8, 0.1, 0.2, 1),
                   Bar(self.now - day, 0.2, 12, 0.1, 0.2, 1)]
        self.assertEqual(prior_daily_peak(history, self.now), 8)

    def test_live_scan_uses_daily_peak_for_young_degraded_coin(self):
        bars = sample_bars(self.now)
        last = bars[-1]
        bars[-1] = Bar(last.start_ms, 0.145, 0.146, 0.138, 0.139, 180)
        ticker = Ticker("VELVET-USDT", 0.139, 0.152, 0.1,
                        1_000_000, 0.1389, 0.1391)
        client = Mock()
        client.tickers.return_value = [ticker]
        client.contracts.return_value = {ticker.symbol}
        client.price_precisions = {ticker.symbol: 6}
        client.bars.side_effect = lambda _, interval, __: (
            bars if interval == "5m" else
            [Bar(self.now - 10 * 86_400_000, 1, 2.16, 0.1, 0.2, 1)]
            if interval == "1d" else [])
        signals, _ = scan_market(client, self.now, {}, include_momentum=False)
        self.assertEqual(len(signals), 1)
        self.assertTrue(signals[0]["actionable"])
        self.assertGreater(signals[0]["old_peak_multiple"], 3)
        client.open_interest.assert_not_called()

    def test_alert_state_deduplicates_same_episode(self):
        signal = {"symbol": "VELVET-USDT", "stage": "reversal", "actionable": True,
                  "peak_ms": self.now - FIVE,
                  "peak": 0.152, "pump_pct": 50, "retrace_pct": 3,
                  "old_peak_multiple": 15, "event_ms": self.now - FIVE,
                  "rebound3h_pct": 50, "route_pct": 20,
                  "base_route_pct": 25, "reward_risk": 3,
                  "pump_target": 0.105, "main_target": 0.1176}
        state = {"episodes": {}}
        alerts = select_alerts([signal], state, self.now)
        self.assertEqual(len(alerts), 1)
        mark_sent(signal, state)
        self.assertEqual(select_alerts([signal], state, self.now), [])
        newer = {**signal, "peak_ms": self.now + FIVE, "peak": 0.18}
        self.assertEqual(select_alerts([newer], state, self.now + FIVE), [])
        next_cycle = {**signal, "peak_ms": self.now + 3 * 3_600_000,
                      "peak": 0.13, "event_ms": self.now + 3 * 3_600_000 + FIVE,
                      "rebound3h_pct": 25}
        self.assertEqual(len(select_alerts([next_cycle], state,
                                           self.now + 3 * 3_600_000 + 2 * FIVE)), 1)

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
        self.assertEqual(alerts, [])
        self.assertEqual(state["episodes"], {})
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
        seeded = confirm_momentum({**signal, "price_trigger": False}, 110,
                                  {"value": 100, "at_ms": self.now - FIVE}, self.now)
        self.assertEqual(seeded["stage"], "momentum_watch")
        self.assertTrue(confirmed["target1"] > confirmed["entry"] > confirmed["stop"])
        with self.assertRaisesRegex(ValueError, "Long alerts are disabled"):
            message(confirmed)

    def test_early_growth_seeds_oi_without_sending_entry(self):
        bars = [Bar(self.now - (40 - index) * FIVE, 1, 1.001, 0.999, 1, 100)
                for index in range(40)]
        for offset, close in ((-3, 1.003), (-2, 1.009), (-1, 1.016)):
            old = bars[offset]
            bars[offset] = Bar(old.start_ms, close * 0.998, close * 1.001,
                               close * 0.997, close, 200)
        ticker = Ticker("EARLY-USDT", 1.016, 1.03, 0.99,
                        1_500_000, 1.0155, 1.0165)
        signal = evaluate_momentum(ticker, bars, self.now)
        self.assertIsNotNone(signal)
        self.assertFalse(signal["price_trigger"])
        confirmed = confirm_momentum(signal, 110,
                                     {"value": 100, "at_ms": self.now - FIVE}, self.now)
        self.assertEqual(select_alerts([confirmed], {"episodes": {}}, self.now), [])

    def test_watch_cannot_be_formatted_for_telegram(self):
        signal = evaluate(self.ticker, sample_bars(self.now), self.now, 2.16)
        with self.assertRaisesRegex(ValueError, "Only confirmed entry"):
            message(signal)

    def test_entry_refresh_rejects_price_drift_and_thin_book(self):
        signal = {"symbol": "VELVET-USDT", "stage": "reversal", "actionable": True,
                  "event_ms": self.now - FIVE, "entry": 0.147, "stop": 0.1550001,
                  "historical_high": 2.16, "price": 0.147,
                  "pump_target": 0.105, "price_precision": 3}
        book = {"T": self.now, "bids": [["0.1469", "10000"]],
                "asks": [["0.1471", "10000"]]}
        ticker = Ticker("VELVET-USDT", 0.147, 0.16, 0.1,
                        1_000_000, 0.1469, 0.1471, self.now)
        refreshed = refresh_entry(signal, ticker, book, self.now)
        self.assertEqual(refreshed["price"], 0.147)
        self.assertLess(refreshed["book_slippage_pct"], 0.5)
        self.assertEqual(refreshed["stop"], 0.156)
        self.assertGreaterEqual(100 * (1 - refreshed["main_target"] / refreshed["entry"]), 20)
        self.assertEqual(refreshed["main_target"], short_target(0.147, 3))
        drifted = Ticker("VELVET-USDT", 0.13, 0.16, 0.1,
                         1_000_000, 0.1469, 0.1471, self.now)
        with self.assertRaisesRegex(ValueError, "left short entry zone"):
            refresh_entry(signal, drifted, book, self.now)
        thin = {"T": self.now, "bids": [["0.1469", "1"]],
                "asks": [["0.1471", "1"]]}
        with self.assertRaisesRegex(ValueError, "too thin"):
            refresh_entry(signal, ticker, thin, self.now)

    def test_stale_book_and_old_oi_window_fail_closed(self):
        signal = {"symbol": "VELVET-USDT", "stage": "reversal", "actionable": True,
                  "event_ms": self.now - FIVE, "entry": 0.147, "stop": 0.155,
                  "price": 0.147}
        book = {"T": self.now - 40_000, "bids": [["0.1469", "10000"]],
                "asks": [["0.1471", "10000"]]}
        ticker = Ticker("VELVET-USDT", 0.147, 0.16, 0.1,
                        1_000_000, 0.1469, 0.1471, self.now)
        with self.assertRaisesRegex(ValueError, "order book stale"):
            refresh_entry(signal, ticker, book, self.now)
        long_signal = {"stage": "momentum_watch", "oi_change_pct": None,
                       "turnover_usdt": 2_000_000, "spread_pct": 0.1,
                       "risk_pct": 3, "price": 1, "stop_candidate": 0.97}
        confirmed = confirm_momentum(long_signal, 110,
                                     {"value": 100, "at_ms": self.now - 50 * 60_000}, self.now)
        self.assertFalse(confirmed.get("actionable", False))
        self.assertIsNone(confirmed["oi_change_pct"])

    def test_main_keeps_long_disabled(self):
        now_ms = int(run_monitor.time.time() * 1000)
        signal = {"symbol": "TEST-USDT", "stage": "momentum_confirmed",
                  "actionable": True, "entry": 100, "stop": 95,
                  "oi_baseline": 100}
        store = Mock()
        store.load.return_value = {"history": {}, "episodes": {},
                                   "price_snapshots": {}, "oi_snapshots": {}}
        client = Mock()
        client.context.return_value = {"open_interest": {
            "time": now_ms, "openInterest": 110}}
        coverage = {"history_updates": {}, "price_updates": {}, "oi_updates": {},
                    "deep_count": 1, "errors": [], "momentum_count": 1,
                    "oi_failures": 0}
        with (patch.object(run_monitor, "configured_store", return_value=store),
              patch.object(run_monitor, "BingX", return_value=client),
              patch.object(run_monitor, "scan_market", return_value=([signal], coverage)),
              patch.object(run_monitor, "select_alerts", return_value=[]),
              patch.object(run_monitor, "refresh_entry", return_value=signal),
              patch.object(run_monitor, "message", return_value="entry"),
              patch.object(run_monitor, "mark_sent"),
              patch.object(run_monitor, "advance_evaluations", return_value={"pending": 1}),
              patch.object(run_monitor, "telegram_credentials", return_value=("token", "chat")),
              patch.object(run_monitor, "telegram_send", return_value=42) as send,
              patch.object(run_monitor.sys, "argv", ["run_monitor.py"])):
            self.assertEqual(run_monitor.main(), 0)
        send.assert_not_called()
        store.save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
