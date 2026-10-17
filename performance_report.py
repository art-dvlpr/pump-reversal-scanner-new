"""Render the scanner's research summary from its public state branch."""

from __future__ import annotations

import argparse
import json
from urllib.request import Request, urlopen

from monitor import utc
from performance import render_report, summarize
from state_store import LOCAL_PATH

DEFAULT_REPO = "art-dvlpr/pump-reversal-scanner-new"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if args.local:
        data = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
    else:
        url = (f"https://raw.githubusercontent.com/{args.repo}/"
               "scanner-state/market-state.json")
        with urlopen(Request(url, headers={"User-Agent": "BingX-performance-report/1.0"}),
                     timeout=20) as response:
            data = json.load(response)
    reports = data.get("performance_reports") or []
    if reports:
        print(render_report(reports[-1]))
    else:
        completed = [row for row in data.get("evaluations", [])
                     if row.get("status") == "complete"]
        if not completed:
            print("Завершённых наблюдений пока нет. "
                  f"Открытых: {sum(row.get('status') == 'pending' for row in data.get('evaluations', []))}.")
        else:
            report = {"milestone": data.get("next_report_at", 100),
                      "generated_utc": utc(max(row["completed_at_ms"] for row in completed)),
                      "summary": summarize(completed)}
            print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
