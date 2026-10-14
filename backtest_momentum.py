"""Exploratory replay of price/volume momentum observations, without historical OI."""

from __future__ import annotations

import argparse
import json
import random
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

from monitor import BingX, Ticker, evaluate_momentum


def replay(client: BingX, symbol: str) -> dict:
    bars = client.bars(symbol, "5m", 400)
    if len(bars) < 100:
        return {"symbol": symbol, "error": "insufficient five-minute history"}
    events = []
    last_alert_ms = 0
    for index in range(30, len(bars) - 37):
        now_ms = bars[index].start_ms + 300_000
        if now_ms - last_alert_ms < 4 * 3_600_000:
            continue
        past = bars[:index + 1]
        last_day = past[-288:]
        current = past[-1]
        turnover = sum(bar.volume * bar.close for bar in last_day)
        ticker = Ticker(symbol, current.close, max(bar.high for bar in last_day),
                        min(bar.low for bar in last_day), turnover,
                        current.close / 1.001, current.close * 1.001)
        signal = evaluate_momentum(ticker, past, now_ms)
        if not signal or not signal["price_trigger"] or signal["gain15_pct"] < 3:
            continue
        last_alert_ms = now_ms
        entry = bars[index + 1].open
        stop = signal["stop_candidate"]
        if entry <= 0 or stop >= entry:
            continue
        tp = entry + 1.5 * (entry - stop)
        future = bars[index + 1:index + 37]
        outcome = "none"
        for bar in future:
            if bar.low <= stop:
                outcome = "stop"
                break
            if bar.high >= tp:
                outcome = "tp"
                break
        events.append({"at": signal["observed_utc"], "entry_next_open": entry,
                       "gain15_pct": signal["gain15_pct"],
                       "volume_ratio": signal["volume_ratio"],
                       "max_favorable_3h_pct": round(100 *
                           (max(bar.high for bar in future) / entry - 1), 1),
                       "max_adverse_3h_pct": round(100 *
                           (1 - min(bar.low for bar in future) / entry), 1),
                       "outcome_1_5R": outcome})
    return {"symbol": symbol, "events": events}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=60)
    parser.add_argument("--symbols", nargs="*")
    args = parser.parse_args()
    client = BingX()
    tickers = client.tickers()
    tradable = client.contracts()
    if args.symbols:
        symbols = [symbol for symbol in args.symbols if symbol in tradable]
    else:
        ranked = [ticker.symbol for ticker in sorted(tickers, key=lambda t: -t.range_pct)
                  if ticker.symbol in tradable]
        movers = ranked[:args.sample // 2]
        others = [symbol for symbol in ranked if symbol not in movers]
        random.Random(20261014).shuffle(others)
        symbols = movers + others[:args.sample - len(movers)]
    outputs = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(replay, client, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                outputs.append(future.result())
            except Exception as error:
                outputs.append({"symbol": symbol, "error": str(error)})
    events = [event | {"symbol": row["symbol"]} for row in outputs
              for event in row.get("events", [])]
    result = {"sample_symbols": len(symbols), "symbols_with_data": sum(
                  "error" not in row for row in outputs),
              "observations": len(events),
              "tp_before_stop": sum(e["outcome_1_5R"] == "tp" for e in events),
              "stop_before_tp": sum(e["outcome_1_5R"] == "stop" for e in events),
              "median_max_favorable_3h_pct": statistics.median(
                  [e["max_favorable_3h_pct"] for e in events]) if events else None,
              "errors": [row for row in outputs if "error" in row],
              "events": sorted(events, key=lambda item: item["at"]),
              "limits": ["OI history is unavailable; this tests price/volume WATCH only, not confirmed long signals.",
                         "Surviving contracts only; current top movers are oversampled.",
                         "Historical turnover is estimated, spread is assumed 0.2%; fees, funding, slippage and scheduling delay excluded.",
                         "Both TP and stop in one candle count as stop first."]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
