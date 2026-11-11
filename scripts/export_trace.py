"""Export one investigation to docs/sample_trace.md (readable) and docs/sample_trace.json (complete).

The JSON file is untruncated and machine-readable: every tool call, observation, reflection step,
the report, the queued/executed remediation actions (with post-fix verification), plus metadata
(git commit, model, run id, timestamps).

Usage: python scripts/export_trace.py                # latest incident with a report
       python scripts/export_trace.py --incident 3
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from indexops.db import fetch_all, fetch_one  # noqa: E402


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _model() -> str:
    try:
        from indexops import llm_client
        d = llm_client.describe()
        return f"{d.get('provider')}/{d.get('model')}"
    except Exception:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--incident", type=int)
    args = ap.parse_args()
    if args.incident:
        inc = fetch_one("SELECT * FROM incidents WHERE incident_id = %s", (args.incident,))
    else:
        inc = fetch_one("SELECT * FROM incidents WHERE report IS NOT NULL ORDER BY incident_id DESC LIMIT 1")
    if not inc:
        sys.exit("No matching incident found. Run an investigation first.")
    iid = inc["incident_id"]
    steps = fetch_all("SELECT * FROM investigation_steps WHERE incident_id = %s ORDER BY step_id", (iid,))
    actions = fetch_all("SELECT * FROM remediation_actions WHERE incident_id = %s ORDER BY action_id", (iid,))

    meta = {"git_commit": _git_commit(), "model": _model(), "incident_id": iid,
            "run_id": inc.get("run_id"), "alert_id": inc.get("alert_id"),
            "exported_at": datetime.now(timezone.utc).isoformat()}
    full = {"metadata": meta, "incident": inc, "steps": steps, "remediation_actions": actions}
    out_dir = ROOT / "docs"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "sample_trace.json").write_text(json.dumps(full, indent=2, default=str), encoding="utf-8")

    def short(x, n=600):
        s = json.dumps(x, default=str)
        return s if len(s) <= n else s[:n] + " ...(truncated here; full data in sample_trace.json)"

    L = [f"# Investigation trace (incident {iid})", "",
         f"- commit: `{meta['git_commit']}`  model: `{meta['model']}`  run_id: {meta['run_id']}  alert_id: {meta['alert_id']}",
         f"- exported: {meta['exported_at']}", f"- status: {inc['status']}",
         f"- validation_passed: {inc.get('validation_passed')}", f"- validation_notes: {inc.get('validation_notes')}", "",
         "## Steps (tool calls, observations, reflection)", ""]
    for i, s in enumerate(steps, 1):
        L += [f"### {i}. `{s['tool_name']}`", f"- input: `{short(s['tool_input'], 300)}`",
              f"- output: `{short(s['tool_output'])}`", ""]
    L += ["## Final report", "", "```json", json.dumps(inc.get("report"), indent=2, default=str), "```", "",
          "## Remediation actions (queued scope, approval, execution, post-fix check)", ""]
    for a in actions:
        p = a.get("params") or {}
        res = p.get("execution_result") or {}
        L += [f"- action {a['action_id']} `{a['action_taken']}` status=**{a['status']}** "
              f"queued_tickets={p.get('ticket_count')} fixed={a.get('records_fixed')} failed={a.get('records_failed')}",
              f"  - post_fix_verification: `{short(res.get('post_fix_verification'), 400)}`"]
    if not actions:
        L.append("- none")
    (out_dir / "sample_trace.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"Wrote {out_dir / 'sample_trace.md'} and sample_trace.json")


if __name__ == "__main__":
    main()
