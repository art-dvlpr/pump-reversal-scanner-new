"""Five-minute BingX monitoring and Telegram alerts; never places trades."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from monitor import BingX, FIVE_MINUTES, number, scan_market, utc
from performance import (advance_evaluations, mark_digest_sent, prepare_digest,
                         prepare_milestone_report, record_alert, render_digest)
from state_store import configured_store


def telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    try:
        from telegram_config import load_config
    except ImportError:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID") from None
    return load_config()


def telegram_send(token: str, chat_id: str, message: str) -> int:
    request = Request("https://api.telegram.org/bot" + token + "/sendMessage",
                      data=urlencode({"chat_id": chat_id, "text": message,
                                      "parse_mode": "HTML",
                                      "disable_web_page_preview": "true"}).encode("utf-8"),
                      headers={"User-Agent": "BingX-public-market-monitor/2.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                data = json.load(response)
            if not data.get("ok"):
                raise RuntimeError("Telegram rejected the message")
            return int(data["result"]["message_id"])
        except HTTPError as error:
            if error.code != 429 or attempt == 2:
                raise RuntimeError(f"Telegram sendMessage HTTP {error.code}") from None
            time.sleep(2 * (attempt + 1))
        except URLError as error:
            if attempt == 2:
                raise RuntimeError("Telegram network unavailable") from error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("Telegram retries exhausted")


def episode_for(signal: dict, episodes: dict, now_ms: int) -> dict:
    momentum = signal["stage"].startswith("momentum_")
    symbol = signal["symbol"] + "#momentum" if momentum else signal["symbol"]
    prior = episodes.get(symbol)
    if momentum:
        if not prior or now_ms - prior.get("started_ms", 0) >= 4 * 3_600_000:
            prior = {"started_ms": now_ms, "confirmed_sent": False}
            episodes[symbol] = prior
        return prior
    new_cycle = (prior and prior.get("reversal_sent") and
                 signal["peak_ms"] > prior.get("sent_event_ms", prior.get("peak_ms", 0)) and
                 now_ms - prior.get("sent_at_ms", prior.get("started_ms", 0)) >= 2 * 3_600_000 and
                 signal.get("rebound3h_pct", 0) >= 20)
    if not prior or new_cycle:
        prior = {"started_ms": now_ms, "peak_ms": signal["peak_ms"],
                 "anchor_peak": signal["peak"], "reversal_sent": False}
        episodes[symbol] = prior
    return prior


def should_send(signal: dict, episode: dict, now_ms: int) -> bool:
    if not signal.get("actionable"):
        return False
    if signal["stage"] == "momentum_confirmed":
        return (not episode["confirmed_sent"] and
                0 <= now_ms - signal["event_ms"] <= 20 * 60_000)
    if signal["stage"] != "reversal":
        return False
    profile = (signal.get("old_peak_multiple") is not None and
               signal["old_peak_multiple"] >= 2)
    return (not episode["reversal_sent"] and
            (profile or signal["pump_pct"] >= 40) and
            0 <= now_ms - signal["event_ms"] <= 25 * 60_000)


def format_price(value: float, precision: int | None = None) -> str:
    digits = max(0, min(12, precision if precision is not None else 8))
    rendered = f"{value:.{digits}f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def local_time(utc_text: str) -> str:
    value = datetime.fromisoformat(utc_text)
    return value.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m %H:%M МСК")


def message(signal: dict, context: dict | None = None) -> str:
    if not signal.get("actionable") or signal["stage"] not in ("reversal", "momentum_confirmed"):
        raise ValueError("Only confirmed entry conditions can be sent to Telegram")
    stage = signal["stage"]
    momentum = stage.startswith("momentum_")
    side = "🟢 #LONG" if momentum else "🔴 #SHORT"
    status = "УСЛОВИЯ ВХОДА — проверь цену"
    symbol = html.escape(signal["symbol"].replace("-", "/"))
    price = lambda value: format_price(value, signal.get("price_precision"))
    lines = [f"<b>{side} — {symbol}</b> · BingX Futures",
             f"🕒 {local_time(signal['last_closed_utc'])} · закрытая 5м свеча",
             f"📍 <b>{status}</b>",
             "━━━━━━━━━━━━━━━━━━",
             f"💵 Цена: <b>{price(signal['price'])} USDT</b> · {local_time(signal['observed_utc'])}"]
    if momentum:
        lines.extend([f"📈 Рост: +{signal['gain15_pct']}% / 15м; +{signal['gain60_pct']}% / 1ч",
                      f"📊 Объём 15м: {signal['volume_ratio']}× обычного",
                      f"🧲 OI: {signal['oi_change_pct']:+.2f}% за {signal['oi_window_min']}м" if
                      signal.get("oi_change_pct") is not None else
                      "🧲 OI: ждём второго замера"])
    else:
        lines.extend([f"🚀 Памп: +{signal['pump_pct']}% от локального минимума",
                      f"↘️ Откат: {signal['retrace_pct']}% от пика {price(signal['peak'])}"])
        if signal.get("old_peak_multiple"):
            lines.append(f"🕰 Старый пик: {signal['old_peak_multiple']}× текущей цены")
        if signal.get("reversal_type"):
            lines.append(f"📉 Признак: {html.escape(signal['reversal_type'])}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    entry, stop = signal["entry"], signal["stop"]
    unit = abs(entry - stop)
    direction = 1 if momentum else -1
    targets = [entry + direction * unit * multiple for multiple in (1, 1.5, 2)]
    lines.extend([f"🔹 <b>Точка входа:</b> около {price(entry)} USDT",
                  "🎯 <b>Расчётные цели (Take-Profit):</b>",
                  f"   TP1 · {price(targets[0])} USDT (1R)",
                  f"   TP2 · {price(targets[1])} USDT (1,5R)",
                  f"   TP3 · {price(targets[2])} USDT (2R)",
                  f"🔴 <b>Стоп-лосс (SL) / отмена:</b> {price(stop)} USDT",
                  f"⚠️ Риск до стопа: {signal['risk_pct']}% цены. "
                  "Размер позиции рассчитай вручную; ориентир — риск до 2% капитала."])
    lines.extend(["━━━━━━━━━━━━━━━━━━",
                  f"💧 Оборот 24ч: {signal['turnover_usdt']:,} USDT · спред: {signal['spread_pct']}%"])
    if signal.get("book_slippage_pct") is not None:
        lines.append(f"📚 Стакан: оценка исполнения 1 000 USDT — {signal['book_slippage_pct']:.3f}%")
    if context:
        oi = context.get("open_interest") or {}
        if not momentum and oi.get("openInterest"):
            lines.append(f"🧲 OI сейчас: {float(oi['openInterest']):,.0f} монет (без истории)")
        funding = context.get("funding") or {}
        rate = funding.get("lastFundingRate")
        hours = funding.get("fundingIntervalHours")
        if rate is not None:
            lines.append(f"💸 Funding: {100 * float(rate):+.4f}% / {hours}ч")
        flow = context.get("flow") or {}
        if flow.get("sample_trades"):
            qualifier = "5м" if flow.get("complete_5m") else "частичная выборка"
            lines.append(f"⚖️ Дельта сделок ({qualifier}): {flow['delta_pct']:+.1f}%")
    lines.append("⚠️ Сценарий для ручной проверки, исход не гарантирован.")
    return "\n".join(lines)


def select_alerts(signals: list[dict], state: dict, now_ms: int) -> list[dict]:
    episodes = state.setdefault("episodes", {})
    ready: list[dict] = []
    eligible = [item for item in signals if item.get("actionable") and
                item["stage"] in ("reversal", "momentum_confirmed")]
    confirmed_reversals = {item["symbol"] for item in eligible if item["stage"] == "reversal"}
    for signal in sorted(eligible, key=lambda item: -item.get("pump_pct", item.get("gain15_pct", 0))):
        if signal["stage"].startswith("momentum_") and signal["symbol"] in confirmed_reversals:
            continue
        episode = episode_for(signal, episodes, now_ms)
        if should_send(signal, episode, now_ms):
            ready.append(signal)
    return ready


def mark_sent(signal: dict, state: dict) -> None:
    momentum = signal["stage"].startswith("momentum_")
    key = signal["symbol"] + "#momentum" if momentum else signal["symbol"]
    episode = state["episodes"][key]
    field = "confirmed_sent" if momentum else "reversal_sent"
    episode[field] = True
    if not momentum:
        episode["sent_event_ms"] = signal["event_ms"]
        episode["sent_at_ms"] = signal["event_ms"] + FIVE_MINUTES


def book_slippage_pct(book: dict, side: str, reference_price: float,
                      notional_usdt: float = 1_000) -> float:
    """Estimate immediate $1,000 execution cost from visible book levels."""
    levels = book.get("asks" if side == "long" else "bids") or []
    remaining = notional_usdt / reference_price
    filled_quote = 0.0
    for raw_price, raw_qty in levels:
        price, qty = float(raw_price), float(raw_qty)
        if price <= 0 or qty <= 0:
            continue
        taken = min(remaining, qty)
        filled_quote += taken * price
        remaining -= taken
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        raise ValueError("Order book too thin for $1,000 reference order")
    average = filled_quote / (notional_usdt / reference_price)
    return 100 * (average / reference_price - 1) if side == "long" else \
        100 * (1 - average / reference_price)


def refresh_entry(signal: dict, ticker, book: dict, now_ms: int) -> dict:
    """Fail closed if a closed-candle setup is no longer executable."""
    if not signal.get("actionable"):
        raise ValueError("entry conditions absent")
    age_limit = 20 * 60_000 if signal["stage"] == "momentum_confirmed" else 25 * 60_000
    if not 0 <= now_ms - signal["event_ms"] <= age_limit:
        raise ValueError("entry event expired")
    if not ticker.at_ms or abs(now_ms - ticker.at_ms) > 120_000:
        raise ValueError("ticker stale")
    book_at = int(book.get("T") or 0)
    if not book_at or abs(now_ms - book_at) > 30_000:
        raise ValueError("order book stale")
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        raise ValueError("order book empty")
    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    if best_bid <= 0 or best_ask <= best_bid:
        raise ValueError("order book crossed or invalid")
    if abs(best_bid / ticker.bid - 1) > 0.01 or abs(best_ask / ticker.ask - 1) > 0.01:
        raise ValueError("ticker and order book disagree")
    spread = 100 * (best_ask / best_bid - 1)
    side = "long" if signal["stage"] == "momentum_confirmed" else "short"
    limit = 0.4 if side == "long" else 0.5
    if ticker.turnover_usdt < (1_000_000 if side == "long" else 300_000):
        raise ValueError("24h turnover below entry threshold")
    if spread > limit:
        raise ValueError("spread above entry threshold")
    slippage = book_slippage_pct(book, side, (best_bid + best_ask) / 2)
    if slippage > 0.5:
        raise ValueError("visible $1,000 book slippage above 0.5%")
    latest = dict(signal)
    latest["price"] = ticker.price
    latest["spread_pct"] = round(spread, 3)
    latest["book_slippage_pct"] = round(slippage, 3)
    latest["observed_utc"] = utc(now_ms)
    if side == "long":
        if not (signal["price"] * 0.99 <= ticker.price <= signal["price"] * 1.01 and
                ticker.price >= signal["breakout_level"]):
            raise ValueError("price left long entry zone")
        risk = 100 * (1 - signal["stop"] / ticker.price)
        if not 0 < risk <= 7:
            raise ValueError("long stop distance no longer valid")
        latest["entry"] = ticker.price
        latest["risk_pct"] = round(risk, 2)
        latest["target1"] = ticker.price + 1.5 * (ticker.price - signal["stop"])
    else:
        if not signal["entry"] * 0.98 <= ticker.price <= signal["entry"] * 1.01:
            raise ValueError("price left short entry zone")
        risk = 100 * (signal["stop"] / ticker.price - 1)
        if not 0 < risk <= 8:
            raise ValueError("short stop distance no longer valid")
        latest["entry"] = ticker.price
        latest["risk_pct"] = round(risk, 2)
        latest["target1"] = ticker.price - 1.5 * (signal["stop"] - ticker.price)
        if latest.get("historical_high"):
            latest["old_peak_multiple"] = round(latest["historical_high"] / ticker.price, 1)
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read public markets, print; do not send or save")
    parser.add_argument("--test-telegram", action="store_true", help="Send one setup message only")
    args = parser.parse_args()
    if args.test_telegram:
        token, chat_id = telegram_credentials()
        test_message_id = telegram_send(token, chat_id, "Тест нового сканера BingX: Telegram подключён; "
                                        "сделки остаются ручными.")
        print(f"Telegram accepted message_id={test_message_id}")
        return 0
    store = configured_store()
    state = store.load()
    before = json.dumps(state, sort_keys=True)
    now_ms = int(time.time() * 1000)
    client = BingX()
    signals, coverage = scan_market(client, now_ms, state.get("history", {}),
                                    state.get("price_snapshots", {}),
                                    state.get("oi_snapshots", {}))
    state.setdefault("history", {}).update(coverage.pop("history_updates"))
    state["price_snapshots"] = coverage.pop("price_updates")
    oi_updates = coverage.pop("oi_updates")
    state["oi_snapshots"] = {symbol: row for symbol, row in
                             (state.get("oi_snapshots", {}) | oi_updates).items()
                             if now_ms - row.get("at_ms", 0) <= 60 * 60_000}
    if coverage["deep_count"] == 0 or len(coverage["errors"]) >= max(5, coverage["deep_count"] // 5):
        raise RuntimeError("BingX data coverage failed; no alerts sent: " +
                           "; ".join(coverage["errors"][:5]))
    if coverage["momentum_count"] and coverage["oi_failures"] == coverage["momentum_count"]:
        raise RuntimeError("BingX open interest unavailable for all momentum candidates")
    alerts = select_alerts(signals, state, int(time.time() * 1000))
    if args.dry_run:
        checked = []
        for signal in alerts:
            try:
                ticker = client.ticker(signal["symbol"])
                book = client.depth(signal["symbol"])
                checked.append(refresh_entry(signal, ticker, book, int(time.time() * 1000)))
            except (ValueError, RuntimeError) as error:
                print(f"Skipped stale/illiquid {signal['symbol']}: {error}")
        alerts = checked
    print(json.dumps({"coverage": coverage, "signals": len(signals),
                      "entry_candidates": [{"symbol": s["symbol"], "stage": s["stage"],
                                  "actionable": s["actionable"],
                                  "pump_pct": s.get("pump_pct"),
                                  "retrace_pct": s.get("retrace_pct"),
                                  "gain15_pct": s.get("gain15_pct")}
                                 for s in alerts]}, ensure_ascii=False))
    if args.dry_run:
        for signal in alerts:
            print(message(signal))
        return 0
    token, chat_id = telegram_credentials()
    failures = []
    for signal in alerts:
        try:
            context = client.context(signal["symbol"])
            if signal["stage"] == "momentum_confirmed":
                oi = context.get("open_interest") or {}
                observed = int(number(oi.get("time")))
                value = number(oi.get("openInterest"))
                if not observed or abs(int(time.time() * 1000) - observed) > 120_000:
                    raise ValueError("latest open interest stale")
                if not signal.get("oi_baseline") or value < signal["oi_baseline"] * 1.02:
                    raise ValueError("open interest confirmation expired")
            ticker = client.ticker(signal["symbol"])
            book = client.depth(signal["symbol"])
            signal = refresh_entry(signal, ticker, book, int(time.time() * 1000))
        except (ValueError, RuntimeError) as error:
            print(f"Skipped stale/illiquid {signal['symbol']}: {error}")
            continue
        try:
            strategy_version = os.environ.get("GITHUB_SHA", "local")
            record_alert(signal, context, int(time.time() * 1000), strategy_version)
            message_id = telegram_send(token, chat_id, message(signal, context))
            sent_at_ms = int(time.time() * 1000)
            candidate_record = record_alert(signal, context, sent_at_ms, strategy_version)
            mark_sent(signal, state)
            state.setdefault("evaluations", []).append(candidate_record)
            state.setdefault("alert_history", []).append({
                "at_ms": sent_at_ms,
                "side": "long" if signal["stage"].startswith("momentum_") else "short"})
            print(f"Telegram accepted {signal['symbol']} {signal['stage']} message_id={message_id}")
        except Exception as error:
            failures.append(f"{signal['symbol']}: {error}")
    pruning_ms = int(time.time() * 1000)
    state["alert_history"] = [row for row in state.get("alert_history", [])
                              if 0 <= pruning_ms - row.get("at_ms", 0) <= 24 * 3_600_000]
    evaluation = advance_evaluations(client, state, int(time.time() * 1000))
    report = prepare_milestone_report(state, int(time.time() * 1000))
    digest = prepare_digest(state, int(time.time() * 1000))
    if digest:
        try:
            digest_id = telegram_send(token, chat_id, render_digest(digest))
            mark_digest_sent(state, digest, digest_id)
            print(f"Telegram accepted three-day digest message_id={digest_id}")
        except Exception as error:
            failures.append(f"Three-day digest: {error}")
    print(json.dumps({"evaluation": evaluation,
                      "report_ready": report["milestone"] if report else None,
                      "digest_due": digest is not None},
                     ensure_ascii=False))
    # The latest ticker/OI snapshots make five-minute changes measurable.
    if json.dumps(state, sort_keys=True) != before:
        store.save(state)
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Scanner failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
