# Known limitations (accepted for the single-operator MVP)

## 1. Run consistency is checked at points in time, not locked
The Airflow DAG rebuilds the shared OpenSearch index on every run, and the live tools read the current
state. What the code does to limit this:
- `run_investigation` refuses an alert whose run is not the latest run (status `stale`).
- Just before a report is saved, the latest run is checked again; if a newer run started during the
  investigation the incident is marked `incomplete` and its pending actions are cancelled.
- `approve()` checks the latest run again before claiming an action and refuses stale incidents.

Residual risk: a DAG run that starts between the approval check and the end of the repair. This is
accepted because the demo has one operator, runs are only started by that operator (`demo.ps1` / manual
trigger), and the steps are sequential. Fully closing it needs a stable index generation (build a new
index, then switch an alias) or a lock shared by the DAG and the repair; both are out of scope for the MVP.

## 2. No user authentication on approval
The dashboard and FastAPI `/approve` route are unauthenticated. Streamlit is bound to `127.0.0.1`
(`.streamlit/config.toml` and the launch commands) and the infrastructure ports are loopback-only.
Add authentication and authorization before any shared deployment.

## 3. Evidence grounding limits
`affected_ticket_count` must equal a typed count field returned by a tool (for example `mismatched_count`
or the queued `ticket_count`). Free-text claims are checked by the evaluator model, which is a semantic
check and not a proof.

## 4. Free-tier model latency and quota
Investigations take roughly 1-2 minutes and share a daily request quota. If the evaluator cannot run, the
report is saved as unvalidated and normal approval stays blocked (manual override only).
