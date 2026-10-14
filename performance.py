"""Quiet, forward-only evaluation of sent alerts using later closed BingX candles."""

from __future__ import annotations

import statistics
from collections import defaultdict

from monitor import FIVE_MINUTES, Bar, closed_bars, number, utc

HORIZON_MS = 24 * 3_600_000
REPORT_EVERY = 100
MAX_RECORDS = 300


def record_alert(signal: dict, context: dict, sent_at_ms: int,
                 strategy_version: str = "unknown") -> dict:
    """Capture exactly what was known when Telegram accepted an entry alert."""
    if signal.get("stage") not in ("reversal", "momentum_confirmed") or not signal.get("actionable", True):
        raise ValueError("Only entry alerts can be tracked")
    side = "long" if signal["stage"] == "momentum_confirmed" else "short"
    entry, stop = number(signal["entry"]), number(signal["stop"])
    risk = stop - entry if side == "short" else entry - stop
    if entry <= 0 or risk <= 0:
        raise ValueError("Invalid entry or stop for outcome tracking")
    direction = -1 if side == "short" else 1
    start_ms = ((sent_at_ms + FIVE_MINUTES - 1) // FIVE_MINUTES) * FIVE_MINUTES
    funding = context.get("funding") or {}
    flow = context.get("flow") or {}
    return {
        "id": f"{side}:{signal['symbol']}:{sent_at_ms}",
        "symbol": signal["symbol"], "side": side,
        "strategy_version": strategy_version,
        "sent_at_ms": sent_at_ms, "first_full_bar_ms": start_ms,
        "next_bar_ms": start_ms, "end_ms": start_ms + HORIZON_MS,
        "entry": entry, "stop": stop,
        "targets": [entry + direction * risk * multiple for multiple in (1, 1.5, 2)],
        "features": {
            "pump_pct": signal.get("pump_pct"),
            "retrace_pct": signal.get("retrace_pct"),
            "rebound3h_pct": signal.get("rebound3h_pct"),
            "old_peak_multiple": signal.get("old_peak_multiple"),
            "reversal_type": signal.get("reversal_type"),
            "gain15_pct": signal.get("gain15_pct"),
            "gain60_pct": signal.get("gain60_pct"),
            "oi_change_pct": signal.get("oi_change_pct"),
            "oi_window_min": signal.get("oi_window_min"),
            "volume_ratio": signal.get("volume_ratio"),
            "turnover_usdt": signal.get("turnover_usdt"),
            "spread_pct": signal.get("spread_pct"),
            "book_slippage_pct": signal.get("book_slippage_pct"),
            "risk_pct": signal.get("risk_pct"),
            "funding_pct": 100 * number(funding.get("lastFundingRate")) if
                           funding.get("lastFundingRate") is not None else None,
            "trade_delta_pct": flow.get("delta_pct") if flow.get("complete_5m") else None,
        },
        "status": "pending", "first_sl_ms": None,
        "first_tp_ms": [None, None, None],
        "max_favorable_pct": 0.0, "max_adverse_pct": 0.0,
    }


def apply_bar(record: dict, bar: Bar) -> None:
    """Record threshold touches; same-candle order stays explicitly ambiguous."""
    entry = record["entry"]
    if record["side"] == "short":
        favorable = max(0.0, 100 * (entry - bar.low) / entry)
        adverse = max(0.0, 100 * (bar.high - entry) / entry)
        stop_hit = bar.high >= record["stop"]
        tp_hit = [bar.low <= target for target in record["targets"]]
    else:
        favorable = max(0.0, 100 * (bar.high - entry) / entry)
        adverse = max(0.0, 100 * (entry - bar.low) / entry)
        stop_hit = bar.low <= record["stop"]
        tp_hit = [bar.high >= target for target in record["targets"]]
    record["max_favorable_pct"] = round(max(record["max_favorable_pct"], favorable), 3)
    record["max_adverse_pct"] = round(max(record["max_adverse_pct"], adverse), 3)
    if stop_hit and record["first_sl_ms"] is None:
        record["first_sl_ms"] = bar.start_ms
    for index, hit in enumerate(tp_hit):
        if hit and record["first_tp_ms"][index] is None:
            record["first_tp_ms"][index] = bar.start_ms
    record["next_bar_ms"] = bar.start_ms + FIVE_MINUTES


def outcome(record: dict, target_index: int = 0) -> str:
    tp, sl = record["first_tp_ms"][target_index], record["first_sl_ms"]
    if tp is not None and sl is not None:
        return "same_bar" if tp == sl else "tp_first" if tp < sl else "sl_first"
    if tp is not None:
        return "tp_first"
    if sl is not None:
        return "sl_first"
    return "neither"


def advance_evaluations(client, state: dict, now_ms: int) -> dict:
    """Advance all pending records; never use an unfinished or pre-alert candle."""
    records = state.setdefault("evaluations", [])
    pending_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record.get("status") == "pending":
            pending_by_symbol[record["symbol"]].append(record)
    errors = []
    completed = 0
    gaps = 0
    for symbol, symbol_records in pending_by_symbol.items():
        try:
            bars = closed_bars(client.bars(symbol, "5m", 400), now_ms)
        except Exception as error:
            errors.append(f"{symbol}: {error}")
            for record in symbol_records:
                if now_ms >= record["end_ms"] + 12 * 3_600_000:
                    record["status"] = "data_gap"
                    record["gap_at_ms"] = record["next_bar_ms"]
                    gaps += 1
            continue
        for record in symbol_records:
            expected = record["next_bar_ms"]
            for bar in bars:
                if bar.start_ms < expected:
                    continue
                if bar.start_ms >= record["end_ms"]:
                    break
                if bar.start_ms != expected:
                    record["status"] = "data_gap"
                    record["gap_at_ms"] = expected
                    gaps += 1
                    break
                apply_bar(record, bar)
                expected = record["next_bar_ms"]
            if record["status"] != "pending":
                continue
            if expected >= record["end_ms"]:
                record["status"] = "complete"
                record["completed_at_ms"] = now_ms
                completed += 1
            elif now_ms >= expected + 2 * FIVE_MINUTES:
                record["status"] = "data_gap"
                record["gap_at_ms"] = expected
                gaps += 1
    state["completed_total"] = state.get("completed_total", 0) + completed
    state["data_gap_total"] = state.get("data_gap_total", 0) + gaps
    # Keep all live evaluations and the latest detailed results; older milestone
    # summaries remain in the state so the GitHub JSON file does not grow forever.
    finished = sorted((row for row in records if row["status"] != "pending"),
                      key=lambda row: row["sent_at_ms"])
    live = sorted((row for row in records if row["status"] == "pending"),
                  key=lambda row: row["sent_at_ms"])
    state["evaluations"] = live + finished[-MAX_RECORDS:]
    return {"pending": len(live), "completed_now": completed,
            "data_gaps_now": gaps, "errors": errors}


def _counts(records: list[dict]) -> dict:
    categories = {name: 0 for name in ("tp_first", "sl_first", "same_bar", "neither")}
    for record in records:
        categories[outcome(record)] += 1
    return {"n": len(records), **categories,
            "median_favorable_pct": round(statistics.median(
                row["max_favorable_pct"] for row in records), 2) if records else None,
            "median_adverse_pct": round(statistics.median(
                row["max_adverse_pct"] for row in records), 2) if records else None}


def _bucket(value: object, edges: tuple[float, float]) -> str | None:
    if value is None:
        return None
    value = number(value, float("nan"))
    if value != value:
        return None
    if value < edges[0]:
        return f"<{edges[0]}"
    if value < edges[1]:
        return f"{edges[0]}–{edges[1]}"
    return f"≥{edges[1]}"


def summarize(records: list[dict]) -> dict:
    """Predeclared factor bins avoid searching arbitrary profitable thresholds."""
    completed = [row for row in records if row.get("status") == "complete"]
    by_side = {side: _counts([row for row in completed if row["side"] == side])
               for side in ("short", "long")}
    by_version = {version: _counts([row for row in completed if row.get("strategy_version") == version])
                  for version in sorted({row.get("strategy_version", "unknown") for row in completed})}
    definitions = {
        "short": {"old_peak_multiple": (3, 10), "pump_pct": (40, 80),
                  "retrace_pct": (8, 15), "spread_pct": (0.1, 0.3)},
        "long": {"oi_change_pct": (5, 10), "gain15_pct": (5, 10),
                 "volume_ratio": (3, 5), "spread_pct": (0.1, 0.3)},
    }
    factors = {}
    for side, fields in definitions.items():
        for field, edges in fields.items():
            grouped: dict[str, list[dict]] = defaultdict(list)
            for row in completed:
                if row["side"] != side:
                    continue
                label = _bucket(row.get("features", {}).get(field), edges)
                if label is not None:
                    grouped[label].append(row)
            factors[f"{side}:{field}"] = {label: _counts(group)
                                          for label, group in grouped.items() if len(group) >= 10}
    return {"sample_count": len(completed), "overall": _counts(completed),
            "by_side": by_side, "by_version": by_version, "factors": factors}


def prepare_milestone_report(state: dict, now_ms: int) -> dict | None:
    total = state.get("completed_total", 0)
    next_at = state.get("next_report_at", REPORT_EVERY)
    if total < next_at:
        return None
    completed = sorted((row for row in state.get("evaluations", []) if row["status"] == "complete"),
                       key=lambda row: row["sent_at_ms"])
    report = {"milestone": next_at, "generated_utc": utc(now_ms),
              "data_gap_total": state.get("data_gap_total", 0),
              "summary": summarize(completed[-REPORT_EVERY:])}
    state.setdefault("performance_reports", []).append(report)
    state["performance_reports"] = state["performance_reports"][-5:]
    state["next_report_at"] = next_at + REPORT_EVERY
    return report


def render_report(report: dict) -> str:
    summary = report["summary"]
    lines = [f"# Проверка {summary['sample_count']} сигналов · рубеж {report['milestone']}",
             f"Создано: {report['generated_utc']} · горизонт: 24 часа после первого полного 5м бара.",
             "Это исходы сигналов по свечам, не фактические сделки пользователя. "
             "Учтена цена последней сделки, без комиссий, funding и проскальзывания.",
             f"Исключено из статистики из-за пробелов данных: {report.get('data_gap_total', 0)}.", ""]
    for side, label in (("short", "🔴 SHORT"), ("long", "🟢 LONG")):
        row = summary["by_side"][side]
        lines.append(f"{label}: {row['n']} · TP1 раньше SL: {row['tp_first']} · "
                     f"SL раньше TP1: {row['sl_first']} · в одной свече: {row['same_bar']} · "
                     f"ни один уровень: {row['neither']}.")
        if row["n"]:
            lines.append(f"Медиана MFE/MAE: {row['median_favorable_pct']}% / "
                         f"{row['median_adverse_pct']}%.")
    lines.append("Версии стратегии: " + ", ".join(
        f"{version[:7]} — {counts['n']}" for version, counts in summary["by_version"].items()))
    lines.append("\nГруппы признаков (только группы от 10 случаев):")
    for key, groups in summary["factors"].items():
        for bucket, counts in groups.items():
            lines.append(f"- {key} {bucket}: {counts['n']} случаев, TP1 раньше SL "
                         f"{counts['tp_first']}, SL раньше TP1 {counts['sl_first']}.")
    lines.append("\nГруппы служат для поиска гипотез. Порог нельзя менять только по этой выборке: "
                 "нужна проверка на следующих, не использованных для подбора сигналах.")
    return "\n".join(lines)
