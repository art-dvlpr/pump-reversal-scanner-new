"""Quiet, forward-only evaluation of sent alerts using later closed BingX candles."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from monitor import FIVE_MINUTES, Bar, closed_bars, number, utc

HORIZON_MS = 24 * 3_600_000
SHORT_HORIZON_MS = 72 * 3_600_000
REPORT_EVERY = 100
MAX_RECORDS = 300
DIGEST_INTERVAL_MS = 3 * 24 * 3_600_000
FACTOR_EDGES = {
    "short": {"old_peak_multiple": (3, 10), "pump_pct": (40, 80),
              "retrace_pct": (8, 15), "spread_pct": (0.1, 0.3)},
    "long": {"oi_change_pct": (5, 10), "gain15_pct": (5, 10),
             "volume_ratio": (3, 5), "spread_pct": (0.1, 0.3)},
}
FEATURE_LABELS = {"old_peak_multiple": "старый пик / цена",
                  "pump_pct": "размер пампа", "retrace_pct": "откат от пика",
                  "spread_pct": "спред", "oi_change_pct": "рост OI",
                  "gain15_pct": "рост за 15 минут", "volume_ratio": "объём / обычный"}


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
    price_drop_20 = side == "short" and signal.get("main_target") is not None
    structural = side == "short" and signal.get("pump_target") is not None
    target = number(signal.get("main_target") if price_drop_20 else
                    signal.get("pump_target")) if structural else None
    if structural and not 0 < target < entry:
        raise ValueError("Invalid short target for outcome tracking")
    if price_drop_20 and 100 * (1 - target / entry) < 20 - 1e-9:
        raise ValueError("Short target must require at least 20% price decline")
    targets = [target] if structural else [entry + direction * risk * multiple
                                           for multiple in (1, 1.5, 2)]
    funding = context.get("funding") or {}
    flow = context.get("flow") or {}
    return {
        "id": f"{side}:{signal['symbol']}:{sent_at_ms}",
        "symbol": signal["symbol"], "side": side,
        "strategy_version": strategy_version,
        "sent_at_ms": sent_at_ms, "first_full_bar_ms": start_ms,
        "next_bar_ms": start_ms, "end_ms": start_ms + (
            SHORT_HORIZON_MS if structural else HORIZON_MS),
        "entry": entry, "stop": stop,
        "targets": targets,
        "target_scheme": "price_drop_20" if price_drop_20 else
        "pump_base_90" if structural else "risk_multiple",
        "features": {
            "pump_pct": signal.get("pump_pct"),
            "pump_base": signal.get("pump_base"),
            "pump_target": signal.get("pump_target"),
            "main_target": signal.get("main_target"),
            "base_route_pct": signal.get("base_route_pct"),
            "route_pct": signal.get("route_pct"),
            "reward_risk": signal.get("reward_risk"),
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
        "first_tp_ms": [None] * len(targets),
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
                    record["finalized_at_ms"] = now_ms
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
                    record["finalized_at_ms"] = now_ms
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
                record["finalized_at_ms"] = now_ms
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
    by_scheme = {scheme: _counts([row for row in completed if
                                  row.get("target_scheme", "risk_multiple") == scheme])
                 for scheme in sorted({row.get("target_scheme", "risk_multiple")
                                       for row in completed})}
    by_side_scheme = {
        f"{side}:{scheme}": _counts([row for row in completed if row["side"] == side and
                                      row.get("target_scheme", "risk_multiple") == scheme])
        for scheme in by_scheme for side in ("short", "long")}
    factors = {}
    for scheme in by_scheme:
        for side, fields in FACTOR_EDGES.items():
            for field, edges in fields.items():
                grouped: dict[str, list[dict]] = defaultdict(list)
                for row in completed:
                    if (row["side"] != side or
                            row.get("target_scheme", "risk_multiple") != scheme):
                        continue
                    label = _bucket(row.get("features", {}).get(field), edges)
                    if label is not None:
                        grouped[label].append(row)
                factors[f"{scheme}:{side}:{field}"] = {label: _counts(group)
                    for label, group in grouped.items() if len(group) >= 10}
    return {"sample_count": len(completed), "overall": _counts(completed),
            "by_side": by_side, "by_version": by_version,
            "by_scheme": by_scheme, "by_side_scheme": by_side_scheme, "factors": factors}


def recurring_patterns(records: list[dict]) -> list[dict]:
    """Flag stable associations in two disjoint forward cohorts, never tune on them."""
    suggestions = []
    for side, fields in FACTOR_EDGES.items():
        side_rows = sorted((row for row in records if row.get("status") == "complete" and
                            row.get("side") == side), key=lambda row: row["completed_at_ms"])
        if not side_rows:
            continue
        version = side_rows[-1].get("strategy_version", "unknown")
        scheme = side_rows[-1].get("target_scheme", "risk_multiple")
        side_rows = [row for row in side_rows if row.get("strategy_version", "unknown") == version
                     and row.get("target_scheme", "risk_multiple") == scheme]
        if len(side_rows) < 80:
            continue
        midpoint = len(side_rows) // 2
        cohorts = (side_rows[:midpoint], side_rows[midpoint:])
        for field, edges in fields.items():
            for bucket in (f"<{edges[0]}", f"{edges[0]}–{edges[1]}", f"≥{edges[1]}"):
                deltas = []
                sample_sizes = []
                for cohort in cohorts:
                    group = [row for row in cohort if
                             _bucket(row.get("features", {}).get(field), edges) == bucket]
                    others = [row for row in cohort if
                              _bucket(row.get("features", {}).get(field), edges) not in (bucket, None)]
                    if len(group) < 12 or len(others) < 12:
                        break
                    deltas.append((_counts(group)["tp_first"] / len(group) -
                                   _counts(others)["tp_first"] / len(others)))
                    sample_sizes.append(len(group))
                if len(deltas) != 2 or not (all(delta >= 0.25 for delta in deltas) or
                                            all(delta <= -0.25 for delta in deltas)):
                    continue
                suggestions.append({"side": side, "feature": field, "bucket": bucket,
                                    "direction": "stronger" if deltas[0] > 0 else "weaker",
                                    "group_n": sum(sample_sizes),
                                    "min_tp_first_gap_pp": round(100 * min(map(abs, deltas)), 1),
                                    "strategy_version": version})
    return sorted(suggestions, key=lambda row: -row["min_tp_first_gap_pp"])


def prepare_digest(state: dict, now_ms: int) -> dict | None:
    """Prepare one rolling three-day Telegram digest; sending is handled separately."""
    since_ms = state.get("digest_since_ms")
    if not isinstance(since_ms, int) or since_ms <= 0:
        state["digest_since_ms"] = now_ms
        state["digest_completed_baseline"] = state.get("completed_total", 0)
        state["digest_gap_baseline"] = state.get("data_gap_total", 0)
        return None
    if now_ms - since_ms < DIGEST_INTERVAL_MS:
        return None
    rows = state.get("evaluations", [])
    complete = [row for row in rows if row.get("status") == "complete" and
                since_ms <= row.get("completed_at_ms", 0) <= now_ms]
    total_complete = max(0, state.get("completed_total", 0) -
                         state.get("digest_completed_baseline", 0))
    return {"since_ms": since_ms, "until_ms": now_ms,
            "summary": summarize(complete),
            "complete_total": total_complete,
            "detail_complete": len(complete) == total_complete,
            "data_gaps": max(0, state.get("data_gap_total", 0) -
                             state.get("digest_gap_baseline", 0)),
            "pending": sum(row.get("status") == "pending" for row in rows),
            "patterns": recurring_patterns(rows)[:2]}


def mark_digest_sent(state: dict, report: dict, message_id: int) -> None:
    """Commit the new interval only after Telegram confirms acceptance."""
    if report["since_ms"] != state.get("digest_since_ms"):
        raise ValueError("Digest interval changed before send confirmation")
    state["digest_since_ms"] = report["until_ms"]
    state["digest_completed_baseline"] = state.get("completed_total", 0)
    state["digest_gap_baseline"] = state.get("data_gap_total", 0)
    state.setdefault("digest_history", []).append({
        "sent_at_ms": report["until_ms"], "message_id": message_id,
        "completed": report["complete_total"], "data_gaps": report["data_gaps"]})
    state["digest_history"] = state["digest_history"][-10:]


def render_digest(report: dict) -> str:
    """Compact HTML for the same Telegram bot used by entry alerts."""
    def msk(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(
            timezone(timedelta(hours=3))).strftime("%d.%m %H:%M")

    summary = report["summary"]
    lines = ["📊 <b>СВОДКА СИГНАЛОВ · BingX</b>",
             f"🕒 {msk(report['since_ms'])} — {msk(report['until_ms'])} МСК",
             "━━━━━━━━━━━━━━━━━━",
             f"✅ Завершено за период: <b>{report['complete_total']}</b>"]
    for scheme, label in (("price_drop_20", "🔴 Новые SHORT · снижение цены ≥20%, 72ч"),
                          ("pump_base_90", "🔴 Прежние SHORT · цель у основания, 72ч")):
        structural = summary.get("by_scheme", {}).get(scheme)
        if structural:
            lines.append(f"{label}: {structural['n']} · "
                         f"цель раньше отмены: {structural['tp_first']} · "
                         f"отмена раньше цели: {structural['sl_first']} · "
                         f"в одной свече: {structural['same_bar']} · "
                         f"без касания: {structural['neither']}")
    for side, label in (("short", "🔴"), ("long", "🟢")):
        legacy = summary.get("by_side_scheme", {}).get(f"{side}:risk_multiple")
        if legacy and legacy["n"]:
            lines.append(f"{label} Ранее отправленные {side.upper()} · прежняя цель 1R, 24ч: "
                         f"{legacy['n']} · TP1→SL: {legacy['tp_first']} · "
                         f"SL→TP1: {legacy['sl_first']} · в одной свече: "
                         f"{legacy['same_bar']} · без касания: {legacy['neither']}")
    lines.extend([f"⏳ Ещё проверяются: {report['pending']} · "
                  f"⚠️ Пробелы данных: {report['data_gaps']}",
                  "━━━━━━━━━━━━━━━━━━"])
    if not report["detail_complete"]:
        lines.append("⚠️ Часть старых деталей вышла из хранилища; разбивка неполная.")
    if report["patterns"]:
        for pattern in report["patterns"]:
            label = "лучше" if pattern["direction"] == "stronger" else "хуже"
            lines.append(f"🔎 Гипотеза по последней версии: {pattern['side'].upper()} · "
                         f"{FEATURE_LABELS[pattern['feature']]} {pattern['bucket']} — "
                         f"цель раньше отмены {label} в двух выборках "
                         f"(n={pattern['group_n']}).")
        lines.append("Правила входа по этой сводке автоматически не менялись: "
                     "нужна проверка издержек и новых данных.")
    else:
        lines.append("🔎 Устойчивых повторяющихся признаков для пересмотра правил пока нет.")
    lines.append("Исходы расчётных уровней по свечам, не твои сделки и не PnL; "
                 "без комиссий, funding и проскальзывания.")
    return "\n".join(lines)


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
             f"Создано: {report['generated_utc']} · новые SHORT ≥20%: 72ч; старые цели 1R: 24ч.",
             "Это исходы сигналов по свечам, не фактические сделки пользователя. "
             "Учтена цена последней сделки, без комиссий, funding и проскальзывания.",
             f"Исключено из статистики из-за пробелов данных: {report.get('data_gap_total', 0)}.", ""]
    groups = (("short:price_drop_20", "🔴 Новые SHORT · снижение цены ≥20%"),
              ("short:pump_base_90", "🔴 Прежние SHORT · цель у основания"),
              ("short:risk_multiple", "🔴 Ранее отправленные SHORT · старая цель 1R"),
              ("long:risk_multiple", "🟢 Ранее отправленные LONG · старая цель 1R"))
    for group, label in groups:
        row = summary.get("by_side_scheme", {}).get(group)
        if not row:
            continue
        lines.append(f"{label}: {row['n']} · цель раньше SL: {row['tp_first']} · "
                     f"SL раньше цели: {row['sl_first']} · в одной свече: {row['same_bar']} · "
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
