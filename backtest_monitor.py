"""Exploratory no-lookahead replay of BingX five-minute candles; no orders or alerts."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from monitor import BingX, FIVE_MINUTES, PINNED, Ticker, evaluate, old_peak
from run_monitor import mark_sent, select_alerts


def replay_symbol(symbol: str, client: BingX, spread_assumption_pct: float = 0.2) -> dict:
    bars = client.bars(symbol, "5m", 1000)
    months = client.bars(symbol, "1M", 100)
    bars = sorted(bars, key=lambda bar: bar.start_ms)
    if len(bars) < 550:
        return {"symbol": symbol, "error": "less than 550 five-minute bars"}
    state = {"episodes": {}}
    observations = 0
    reversal_events: list[dict] = []
    late_events = 0
    last_peak = 0
    for index in range(400, len(bars)):
        current = bars[index]
        now_ms = current.start_ms + FIVE_MINUTES
        past = bars[max(0, index - 399):index + 1]
        last_day = bars[max(0, index - 287):index + 1]
        high24 = max(bar.high for bar in last_day)
        low24 = min(bar.low for bar in last_day)
        quote_turnover = sum(bar.volume * bar.close for bar in last_day)
        ticker = Ticker(symbol, current.close, high24, low24, quote_turnover,
                        current.close / (1 + spread_assumption_pct / 200),
                        current.close * (1 + spread_assumption_pct / 200))
        signal = evaluate(ticker, past, now_ms, old_peak(months, now_ms))
        if not signal:
            continue
        if signal["stage"] == "late" and signal["peak_ms"] != last_peak:
            late_events += 1
            last_peak = signal["peak_ms"]
        ready = select_alerts([signal], state, now_ms)
        if not ready:
            continue
        mark_sent(signal, state)
        if signal["stage"] == "watch":
            observations += 1
            continue
        if signal["event_ms"] != current.start_ms:
            continue
        if index + 1 >= len(bars):
            continue
        entry = bars[index + 1].open
        future = bars[index + 1:index + 145]
        if entry <= 0:
            continue
        if len(future) < 144:
            reversal_events.append({"utc": signal["observed_utc"],
                                    "entry_next_open": entry, "pump_pct": signal["pump_pct"],
                                    "retrace_pct": signal["retrace_pct"],
                                    "actionable": signal["actionable"],
                                    "outcome_complete": False})
            continue
        min_low = min(bar.low for bar in future)
        max_high = max(bar.high for bar in future)
        stop = signal["stop"] if signal["actionable"] else entry * 1.08
        stopped_first = False
        hit20 = False
        hit30 = False
        for bar in future:
            if bar.high >= stop:
                stopped_first = True
                break
            if bar.low <= entry * 0.7:
                hit30 = True
                hit20 = True
                break
            if bar.low <= entry * 0.8:
                hit20 = True
                break
        reversal_events.append({
            "utc": signal["observed_utc"], "entry_next_open": entry,
            "pump_pct": signal["pump_pct"], "retrace_pct": signal["retrace_pct"],
            "actionable": signal["actionable"],
            "max_favorable_12h_pct": round(100 * (1 - min_low / entry), 1),
            "max_adverse_12h_pct": round(100 * (max_high / entry - 1), 1),
            "hit20_before_stop": hit20, "hit30_before_stop": hit30,
            "stop_first": stopped_first,
            "outcome_complete": True,
        })
    return {"symbol": symbol, "observations": observations,
            "late_events": late_events, "reversals": reversal_events}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=80)
    parser.add_argument("--symbols", nargs="*")
    args = parser.parse_args()
    client = BingX()
    tickers = client.tickers()
    tradable = client.contracts()
    if args.symbols:
        symbols = [symbol for symbol in args.symbols if symbol in tradable]
    else:
        moving = [ticker.symbol for ticker in sorted(tickers, key=lambda ticker: -ticker.range_pct)
                  if ticker.symbol in tradable][:args.sample // 2]
        others = [ticker.symbol for ticker in tickers if ticker.symbol in tradable and
                  ticker.symbol not in moving and ticker.symbol not in PINNED]
        random.Random(20261014).shuffle(others)
        symbols = list(dict.fromkeys(sorted(PINNED & tradable) + moving +
                                     others[:max(0, args.sample - len(moving) - len(PINNED))]))
    outputs: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(replay_symbol, symbol, client): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                outputs.append(future.result())
            except Exception as error:
                outputs.append({"symbol": symbol, "error": str(error)})
    events = [event | {"symbol": output["symbol"]} for output in outputs
              for event in output.get("reversals", [])]
    completed = [event for event in events if event["outcome_complete"]]
    total = len(completed)
    result = {
        "sample_symbols": len(symbols), "symbols_with_data": sum("error" not in row for row in outputs),
        "errors": [row for row in outputs if "error" in row],
        "watch_observations": sum(row.get("observations", 0) for row in outputs),
        "late_events": sum(row.get("late_events", 0) for row in outputs),
        "reversal_events_with_complete_12h_outcome": total,
        "reversal_events_recent_outcome_pending": len(events) - total,
        "actionable_events": sum(event["actionable"] for event in completed),
        "hit20_before_stop": sum(event["hit20_before_stop"] for event in completed),
        "hit30_before_stop": sum(event["hit30_before_stop"] for event in completed),
        "stop_first": sum(event["stop_first"] for event in completed),
        "median_max_favorable_12h_pct": statistics.median(
            [event["max_favorable_12h_pct"] for event in completed]) if completed else None,
        "median_max_adverse_12h_pct": statistics.median(
            [event["max_adverse_12h_pct"] for event in completed]) if completed else None,
        "events": sorted(events, key=lambda row: row["utc"]),
        "limits": ["Current surviving contracts only; delisted symbols are absent.",
                   "Five-minute history covers roughly three days; completed 12h outcomes only.",
                   "Historical spread is assumed 0.2%; fees, funding, slippage and alert delay excluded.",
                   "Current top movers are deliberately oversampled; not a market-wide success rate."],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
