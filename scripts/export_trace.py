"""Export one investigation (steps + report + validation) to docs/sample_trace.md.

Usage: python scripts/export_trace.py            # latest submitted incident
       python scripts/export_trace.py --incident 3
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from indexops.db import fetch_all, fetch_one  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--incident", type=int)
    args = ap.parse_args()
    if args.incident:
        inc = fetch_one("SELECT * FROM incidents WHERE incident_id = %s", (args.incident,))
    else:
        inc = fetch_one("SELECT * FROM incidents WHERE status = 'report_submitted' ORDER BY incident_id DESC LIMIT 1")
    if not inc:
        sys.exit("No matching incident found. Run an investigation first.")
    steps = fetch_all("SELECT * FROM investigation_steps WHERE incident_id = %s ORDER BY step_id", (inc["incident_id"],))

    def short(x, n=700):
        s = json.dumps(x, default=str)
        return s if len(s) <= n else s[:n] + " ...(truncated)"

    lines = [f"# Sample investigation trace (incident {inc['incident_id']})", "",
             f"- status: {inc['status']}", f"- alert_id: {inc['alert_id']}  run_id: {inc['run_id']}",
             f"- validation_passed: {inc.get('validation_passed')}",
             f"- validation_notes: {inc.get('validation_notes')}", "", "## Steps (tool calls and reflection)", ""]
    for i, s in enumerate(steps, 1):
        lines += [f"### {i}. `{s['tool_name']}`", f"- input: `{short(s['tool_input'], 300)}`",
                  f"- output: `{short(s['tool_output'])}`", ""]
    lines += ["## Final report", "", "```json", json.dumps(inc["report"], indent=2, default=str), "```", ""]
    out = Path(__file__).resolve().parents[1] / "docs" / "sample_trace.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
