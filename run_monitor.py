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

from monitor import BingX, scan_market
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
    if (not prior or
            (signal["peak_ms"] > prior.get("peak_ms", 0) and
             (signal["peak"] >= prior.get("anchor_peak", 0) * 1.15 or
              now_ms - prior.get("started_ms", 0) >= 24 * 3_600_000))):
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
             f"💵 Текущая цена: <b>{price(signal['price'])} USDT</b>"]
    if momentum:
        lines.extend([f"📈 Рост: +{signal['gain15_pct']}% / 15м; +{signal['gain60_pct']}% / 1ч",
                      f"📊 Объём 5м: {signal['volume_ratio']}× обычного",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read public markets, print; do not send or save")
    parser.add_argument("--test-telegram", action="store_true", help="Send one setup message only")
    args = parser.parse_args()
    if args.test_telegram:
        token, chat_id = telegram_credentials()
        number = telegram_send(token, chat_id, "Тест нового сканера BingX: Telegram подключён; "
                               "сделки остаются ручными.")
        print(f"Telegram accepted message_id={number}")
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
    alerts = select_alerts(signals, state, now_ms)
    print(json.dumps({"coverage": coverage, "signals": len(signals),
                      "alerts": [{"symbol": s["symbol"], "stage": s["stage"],
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
            message_id = telegram_send(token, chat_id, message(signal, context))
            mark_sent(signal, state)
            state.setdefault("alert_history", []).append({
                "at_ms": now_ms,
                "side": "long" if signal["stage"].startswith("momentum_") else "short"})
            print(f"Telegram accepted {signal['symbol']} {signal['stage']} message_id={message_id}")
        except Exception as error:
            failures.append(f"{signal['symbol']}: {error}")
    state["alert_history"] = [row for row in state.get("alert_history", [])
                              if 0 <= now_ms - row.get("at_ms", 0) <= 24 * 3_600_000]
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
