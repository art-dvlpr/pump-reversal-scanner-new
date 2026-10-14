"""BingX public-data pump/reversal monitor. It never accesses trading accounts."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://open-api.bingx.com"
FIVE_MINUTES = 300_000
DAY = 86_400_000
PINNED = {"VELVET-USDT", "BAT-USDT"}
USER_AGENT = "BingX-public-market-research/2.0"


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def number(value: object, fallback: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else fallback
    except (TypeError, ValueError):
        return fallback


class RateLimiter:
    """Keep public requests below BingX's shared market-IP limit."""

    def __init__(self, requests_per_second: float = 3.0):
        self.interval = 1 / requests_per_second
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            target = max(time.monotonic(), self.next_at)
            self.next_at = target + self.interval
        time.sleep(max(0.0, target - time.monotonic()))


class BingX:
    def __init__(self, request_rate: float = 3.0):
        self.limiter = RateLimiter(request_rate)
        self.oi_limiter = RateLimiter(0.9)
        self.price_precisions: dict[str, int] = {}

    def get(self, path: str, **params: object) -> object:
        url = BASE + path + ("?" + urlencode(params) if params else "")
        for attempt in range(3):
            self.limiter.wait()
            try:
                with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=14) as response:
                    body = json.load(response)
                if not isinstance(body, dict):
                    raise ValueError(f"BingX {path}: invalid JSON")
                if body.get("code") != 0:
                    message = str(body.get("msg", ""))
                    if attempt < 2 and ("请稍后重试" in message or "retry" in message.lower() or
                                        "rate" in message.lower()):
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise ValueError(f"BingX {path}: {message}")
                return body.get("data")
            except HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError(f"BingX {path}: HTTP {error.code}") from None
            except (URLError, TimeoutError) as error:
                if attempt == 2:
                    raise RuntimeError(f"BingX {path}: {type(error).__name__}") from None
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"BingX {path}: retries exhausted")

    def tickers(self) -> list["Ticker"]:
        rows = self.get("/openApi/swap/v2/quote/ticker")
        if not isinstance(rows, list):
            raise ValueError("BingX ticker list unavailable")
        result = []
        for row in rows:
            symbol = str(row.get("symbol", ""))
            if not symbol.endswith("-USDT"):
                continue
            price = number(row.get("lastPrice"))
            high = number(row.get("highPrice"))
            low = number(row.get("lowPrice"))
            bid = number(row.get("bidPrice"))
            ask = number(row.get("askPrice"))
            if price <= 0 or high <= 0 or low <= 0:
                continue
            result.append(Ticker(symbol, price, high, low, number(row.get("quoteVolume")), bid, ask))
        return result

    def contracts(self) -> set[str]:
        rows = self.get("/openApi/swap/v2/quote/contracts")
        if not isinstance(rows, list):
            raise ValueError("BingX contract list unavailable")
        self.price_precisions = {str(row.get("symbol")): int(row["pricePrecision"])
                                 for row in rows if row.get("pricePrecision") is not None}
        return {str(row.get("symbol")) for row in rows
                if row.get("status") == 1 and str(row.get("apiStateOpen", "")).lower() == "true"}

    def bars(self, symbol: str, interval: str, limit: int) -> list["Bar"]:
        rows = self.get("/openApi/swap/v3/quote/klines", symbol=symbol, interval=interval, limit=limit)
        if not isinstance(rows, list):
            raise ValueError(f"BingX {symbol} {interval} candles unavailable")
        bars = [Bar(int(row["time"]), number(row.get("open")), number(row.get("high")),
                    number(row.get("low")), number(row.get("close")), number(row.get("volume")))
                for row in rows]
        return sorted(bars, key=lambda bar: bar.start_ms)

    def context(self, symbol: str) -> dict:
        result: dict = {}
        for label, path in (("funding", "/openApi/swap/v2/quote/premiumIndex"),
                            ("open_interest", "/openApi/swap/v2/quote/openInterest")):
            try:
                result[label] = (self.open_interest(symbol) if label == "open_interest"
                                 else self.get(path, symbol=symbol))
            except Exception as error:
                result[label + "_error"] = str(error)
        try:
            rows = self.get("/openApi/swap/v2/quote/trades", symbol=symbol, limit=1000)
            if isinstance(rows, list):
                window_start = int(time.time() * 1000) - FIVE_MINUTES
                recent = [row for row in rows if int(row.get("time", 0)) >= window_start]
                buy = sum(number(row.get("quoteQty")) for row in recent
                          if row.get("isBuyerMaker") is False)
                sell = sum(number(row.get("quoteQty")) for row in recent
                           if row.get("isBuyerMaker") is True)
                if buy + sell > 0:
                    result["flow"] = {"delta_pct": round(100 * (buy - sell) / (buy + sell), 1),
                                      "sample_trades": len(recent),
                                      "complete_5m": len(rows) < 1000 or
                                      min(int(row.get("time", 0)) for row in rows) <= window_start}
        except Exception as error:
            result["flow_error"] = str(error)
        return result

    def open_interest(self, symbol: str) -> dict:
        self.oi_limiter.wait()
        row = self.get("/openApi/swap/v2/quote/openInterest", symbol=symbol)
        if not isinstance(row, dict) or number(row.get("openInterest")) <= 0:
            raise ValueError(f"BingX {symbol} open interest unavailable")
        return row


@dataclass(frozen=True)
class Ticker:
    symbol: str
    price: float
    high24: float
    low24: float
    turnover_usdt: float
    bid: float
    ask: float

    @property
    def range_pct(self) -> float:
        return 100 * (self.high24 / self.low24 - 1)

    @property
    def spread_pct(self) -> float:
        return 100 * (self.ask / self.bid - 1) if self.bid > 0 and self.ask >= self.bid else math.inf


@dataclass(frozen=True)
class Bar:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def closed_bars(bars: list[Bar], now_ms: int) -> list[Bar]:
    return [bar for bar in bars if bar.start_ms + FIVE_MINUTES <= now_ms and
            min(bar.open, bar.high, bar.low, bar.close) > 0]


def old_peak(monthly: list[Bar], now_ms: int) -> float | None:
    month_start = datetime.fromtimestamp(now_ms / 1000, timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    eligible = [bar.high for bar in monthly if bar.start_ms < month_start and
                bar.start_ms < now_ms - 30 * DAY and bar.high > 0]
    return max(eligible) if eligible else None


def evaluate(ticker: Ticker, bars: list[Bar], now_ms: int,
             historical_high: float | None = None) -> dict | None:
    """Return an observation from data known at now_ms; never inspect future bars."""
    closed = closed_bars(bars, now_ms)
    if len(closed) < 30 or closed[-1].start_ms + FIVE_MINUTES < now_ms - 12 * 60_000:
        return None
    # A 24h absolute high can hide a fresh pump below yesterday's high (ZC case).
    recent = [bar for bar in closed if bar.start_ms >= now_ms - 12 * 3_600_000]
    if len(recent) < 20:
        return None
    peak_index = max(range(len(recent)), key=lambda index: recent[index].high)
    peak = recent[peak_index]
    # Require a trough before the peak; otherwise an old high cannot masquerade as a new pump.
    before = recent[max(0, peak_index - 144):peak_index + 1]
    if peak_index < 3 or len(before) < 4:
        return None
    trough = min(bar.low for bar in before)
    pump_pct = 100 * (peak.high / trough - 1)
    retrace_pct = 100 * (1 - closed[-1].close / peak.high)
    if pump_pct < 20 or peak.start_ms < now_ms - 12 * 3_600_000:
        return None
    last = closed[-1]
    prior = closed[-25:-1]
    median_volume = statistics.median(bar.volume for bar in prior if bar.volume > 0) if any(
        bar.volume > 0 for bar in prior) else 0
    volume_ratio = last.volume / median_volume if median_volume > 0 else 0
    previous_low = min(bar.low for bar in closed[-4:-1])
    breakdown = (last.start_ms > peak.start_ms and last.close < previous_low * 0.997 and
                 volume_ratio >= 1.15)
    prior_high = max(bar.high for bar in closed[-7:-1])
    rejection = (last.start_ms >= peak.start_ms and last.close < last.open * 0.97 and
                 last.high >= prior_high * 0.985 and last.close < last.high * 0.96 and
                 volume_ratio >= 1.3)
    recent_four = closed[-4:]
    rolling_decline = (last.start_ms > peak.start_ms and
                       last.close <= recent_four[0].close * 0.95 and
                       sum(recent_four[i].high < recent_four[i - 1].high
                           for i in range(1, 4)) >= 2 and
                       sum(bar.volume for bar in recent_four) >= 4 * median_volume)
    # Re-evaluate a recent event if a scheduled runner was delayed by several minutes.
    event = last
    if not (breakdown or rejection):
        for candidate_index in range(len(closed) - 2, max(3, len(closed) - 5), -1):
            bar = closed[candidate_index]
            earlier = closed[candidate_index - 3:candidate_index]
            if (bar.start_ms > peak.start_ms and bar.close < min(x.low for x in earlier) * 0.997 and
                    bar.volume >= 1.15 * median_volume):
                breakdown = True
                event = bar
                break
    stage = "watch"
    if 3 <= retrace_pct <= 25 and (breakdown or rejection or rolling_decline):
        stage = "reversal"
    if retrace_pct > 25:
        stage = "late"
    if stage == "watch" and retrace_pct > 16:
        return None
    if ticker.turnover_usdt < 50_000 or ticker.spread_pct > 1.5:
        return None
    actionable = ticker.turnover_usdt >= 300_000 and ticker.spread_pct <= 0.5
    level = previous_low if breakdown else event.low
    entry = level
    stop = max(bar.high for bar in closed[-7:]) * 1.005
    risk_pct = 100 * (stop / entry - 1) if entry > 0 else math.inf
    actionable = (actionable and stage == "reversal" and 0 < risk_pct <= 8 and
                  ticker.price >= entry * 0.98 and ticker.price <= entry * 1.01)
    target1 = entry - 1.5 * (stop - entry) if actionable else None
    if target1 is not None and target1 <= 0:
        actionable = False
        target1 = None
    return {
        "exchange": "BingX", "symbol": ticker.symbol, "stage": stage,
        "observed_utc": utc(now_ms), "last_closed_utc": utc(last.start_ms + FIVE_MINUTES),
        "event_utc": utc(event.start_ms + FIVE_MINUTES), "event_ms": event.start_ms,
        "peak_utc": utc(peak.start_ms), "peak_ms": peak.start_ms,
        "peak": peak.high, "price": ticker.price, "pump_pct": round(pump_pct, 1),
        "retrace_pct": round(retrace_pct, 1), "range24_pct": round(ticker.range_pct, 1),
        "turnover_usdt": round(ticker.turnover_usdt), "spread_pct": round(ticker.spread_pct, 3),
        "volume_ratio": round(volume_ratio, 2), "reversal_type": "пробой опоры" if breakdown else
        "отказ от максимума" if rejection else "серия понижающихся максимумов" if rolling_decline else None,
        "level": level, "entry": entry if actionable else None,
        "stop": stop if actionable else None, "target1": target1,
        "risk_pct": round(risk_pct, 2), "actionable": actionable,
        "historical_high": historical_high,
        "old_peak_multiple": round(historical_high / ticker.price, 1) if historical_high else None,
    }


def evaluate_momentum(ticker: Ticker, bars: list[Bar], now_ms: int) -> dict | None:
    """Flag fresh price/volume expansion; OI confirmation is added from live snapshots."""
    closed = closed_bars(bars, now_ms)
    if len(closed) < 30 or closed[-1].start_ms + FIVE_MINUTES < now_ms - 12 * 60_000:
        return None
    last = closed[-1]
    prior = closed[-28:-4]
    baseline = statistics.median(bar.volume for bar in prior if bar.volume > 0) if any(
        bar.volume > 0 for bar in prior) else 0
    if baseline <= 0 or ticker.turnover_usdt < 300_000 or ticker.spread_pct > 0.7:
        return None
    gain15 = 100 * (last.close / closed[-4].close - 1)
    gain60 = 100 * (last.close / closed[-13].close - 1)
    volume_ratio = sum(bar.volume for bar in closed[-3:]) / (3 * baseline)
    earlier_high = max(bar.high for bar in closed[-28:-3])
    distance_from_high = 100 * (1 - last.close / max(bar.high for bar in closed[-4:]))
    if not (2 <= gain15 <= 18 and 3 <= gain60 <= 25 and volume_ratio >= 2 and
            last.close >= earlier_high * 1.002 and distance_from_high <= 3 and
            ticker.price >= earlier_high and ticker.price >= last.close * 0.985 and
            ticker.price <= last.close * 1.05):
        return None
    # Price strength is an observation. It becomes a conditional long scenario
    # only after a later run confirms growing open interest as well.
    stop = min(bar.low for bar in closed[-4:]) * 0.995
    risk_pct = 100 * (1 - stop / ticker.price) if stop > 0 and ticker.price > 0 else math.inf
    return {
        "exchange": "BingX", "symbol": ticker.symbol, "stage": "momentum_watch",
        "observed_utc": utc(now_ms), "last_closed_utc": utc(last.start_ms + FIVE_MINUTES),
        "event_utc": utc(last.start_ms + FIVE_MINUTES), "event_ms": last.start_ms,
        "peak_ms": last.start_ms, "peak": last.high, "price": ticker.price,
        "gain15_pct": round(gain15, 1), "gain60_pct": round(gain60, 1),
        "volume_ratio": round(volume_ratio, 2), "breakout_level": earlier_high,
        "turnover_usdt": round(ticker.turnover_usdt), "spread_pct": round(ticker.spread_pct, 3),
        "risk_pct": round(risk_pct, 2), "stop_candidate": stop,
        "oi_change_pct": None, "actionable": False, "entry": None,
        "stop": None, "target1": None,
    }


def confirm_momentum(signal: dict, current_oi: float, prior: dict | None,
                     now_ms: int) -> dict:
    signal = dict(signal)
    if prior and current_oi > 0:
        samples = prior.get("samples") or [prior]
        eligible = [row for row in samples if
                    4 * 60_000 <= now_ms - row.get("at_ms", 0) <= 60 * 60_000 and
                    number(row.get("value")) > 0]
        if eligible:
            baseline = min(eligible, key=lambda row: row["at_ms"])
            signal["oi_change_pct"] = round(100 * (current_oi / number(baseline["value"]) - 1), 2)
            signal["oi_window_min"] = round((now_ms - baseline["at_ms"]) / 60_000)
    confirmed = (signal["oi_change_pct"] is not None and signal["oi_change_pct"] >= 2 and
                 signal["turnover_usdt"] >= 1_000_000 and signal["spread_pct"] <= 0.4 and
                 0 < signal["risk_pct"] <= 7)
    if confirmed:
        signal["stage"] = "momentum_confirmed"
        signal["actionable"] = True
        signal["entry"] = signal["price"]
        signal["stop"] = signal["stop_candidate"]
        signal["target1"] = signal["price"] + 1.5 * (signal["price"] - signal["stop"])
    return signal


def candidates(tickers: list[Ticker], tradable: set[str],
               price_snapshots: dict | None = None, now_ms: int | None = None) -> list[Ticker]:
    """No top-N cut; fresh 5m changes supplement the 24h range screen."""
    price_snapshots = price_snapshots or {}
    return [ticker for ticker in tickers if ticker.symbol in tradable and
            (ticker.range_pct >= 8 or ticker.symbol in PINNED or
             (ticker.symbol in price_snapshots and
              (now_ms is None or 0 <= now_ms - price_snapshots[ticker.symbol].get("at_ms", 0) <=
               20 * 60_000) and
              number(price_snapshots[ticker.symbol].get("price")) > 0 and
              abs(ticker.price / number(price_snapshots[ticker.symbol]["price"]) - 1) >= 0.012))]


def scan_market(client: BingX, now_ms: int, history: dict,
                price_snapshots: dict | None = None,
                oi_snapshots: dict | None = None) -> tuple[list[dict], dict]:
    tickers = client.tickers()
    tradable = client.contracts()
    selected = candidates(tickers, tradable, price_snapshots, now_ms)
    results: list[dict] = []
    momentum: list[dict] = []
    errors: list[str] = []
    historical_updates: dict = {}
    oi_updates: dict = {}

    def inspect(ticker: Ticker) -> tuple[dict | None, dict | None, tuple[str, dict] | None]:
        bars = client.bars(ticker.symbol, "5m", 400)
        old = history.get(ticker.symbol)
        update = None
        high = old.get("high") if old else None
        result = evaluate(ticker, bars, now_ms, high)
        if result and (not old or now_ms - int(old.get("checked_ms", 0)) > 7 * DAY):
            high = old_peak(client.bars(ticker.symbol, "1M", 100), now_ms)
            update = (ticker.symbol, {"high": high, "checked_ms": now_ms})
            result = evaluate(ticker, bars, now_ms, high)
        return result, evaluate_momentum(ticker, bars, now_ms), update

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(inspect, ticker): ticker.symbol for ticker in selected}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result, rising, update = future.result()
                if result:
                    results.append(result)
                if rising:
                    momentum.append(rising)
                if update:
                    historical_updates[update[0]] = update[1]
            except Exception as error:
                errors.append(f"{symbol}: {error}")
    # OI is a current snapshot, not a historical series. Compare only values
    # observed by this scanner on separate runs; never infer a delta from price.
    for rising in sorted(momentum, key=lambda item: -item["gain15_pct"])[:25]:
        symbol = rising["symbol"]
        try:
            row = client.open_interest(symbol)
            value = number(row.get("openInterest"))
            previous = (oi_snapshots or {}).get(symbol)
            prior_samples = (previous.get("samples") or [previous]) if previous else []
            samples = [item for item in prior_samples if
                       now_ms - item.get("at_ms", 0) <= 60 * 60_000]
            samples.append({"value": value, "at_ms": now_ms})
            oi_updates[symbol] = {"value": value, "at_ms": now_ms, "samples": samples[-13:]}
            results.append(confirm_momentum(rising, value, previous, now_ms))
        except Exception as error:
            errors.append(f"{symbol} OI: {error}")
            rising["oi_error"] = str(error)
            results.append(rising)
    for signal in results:
        signal["price_precision"] = client.price_precisions.get(signal["symbol"])
    results.sort(key=lambda item: (item["stage"] not in ("reversal", "momentum_confirmed"),
                                   -item.get("pump_pct", item.get("gain15_pct", 0))))
    return results, {"ticker_count": len(tickers), "tradable_count": len(tradable),
                     "deep_count": len(selected), "errors": errors,
                     "history_updates": historical_updates, "oi_updates": oi_updates,
                     "price_updates": {ticker.symbol: {"price": ticker.price, "at_ms": now_ms}
                                       for ticker in tickers if ticker.symbol in tradable}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    now_ms = int(time.time() * 1000)
    signals, coverage = scan_market(BingX(), now_ms, {})
    if args.json:
        print(json.dumps({"signals": signals, "coverage": coverage}, ensure_ascii=False, indent=2))
    else:
        print(f"BingX tickers={coverage['ticker_count']} deep={coverage['deep_count']} "
              f"signals={len(signals)} errors={len(coverage['errors'])}")
        for signal in signals[:30]:
            if signal["stage"].startswith("momentum_"):
                detail = f"gain15={signal['gain15_pct']}% oiChange={signal.get('oi_change_pct')}%"
            else:
                detail = (f"pump={signal['pump_pct']}% retrace={signal['retrace_pct']}% "
                          f"oldPeak={signal['old_peak_multiple']}x")
            print(f"{signal['symbol']} {signal['stage']} {detail} "
                  f"actionable={signal['actionable']}")
        for error in coverage["errors"][:10]:
            print(error, file=sys.stderr)
    return 0 if coverage["ticker_count"] and len(coverage["errors"]) < max(5, coverage["deep_count"] // 5) else 2


if __name__ == "__main__":
    raise SystemExit(main())
