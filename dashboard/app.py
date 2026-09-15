"""Intelligent IndexOps — Streamlit dashboard.

Story flow:
  Ticket Board → Pipeline Runs → Faulty Tickets → Investigate → Steps → Approve

Modes of investigation:
  1. Alert-triggered (from an alerts row)
  2. Free-form question (Interactive Investigation)

Run:  python -m streamlit run dashboard/app.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from indexops.db import fetch_all, fetch_one, execute  # noqa: E402
from indexops.approval import approve  # noqa: E402
from indexops.mcp_client import MCPConnection  # noqa: E402
from indexops.agent.loop import run_investigation  # noqa: E402
from indexops.tools.investigation import compare_source_vs_index, sample_mismatched_tickets  # noqa: E402
from indexops.board import ensure_kanban_statuses  # noqa: E402
from indexops.contract import TEAMS_BY_CATEGORY  # noqa: E402

st.set_page_config(page_title="Intelligent IndexOps", page_icon="🔎", layout="wide")

POLL_SECONDS = 2
BOARD_SAMPLE = 48
STATUS_COLUMNS = [("todo", "TO DO"), ("in_progress", "IN PROGRESS"), ("done", "DONE")]
TEAM_COLUMNS = list(TEAMS_BY_CATEGORY.values())
PRIORITY_COLOR = {
    "Critical": "#b91c1c",
    "High": "#ea580c",
    "Medium": "#ca8a04",
    "Low": "#2563eb",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fmt(ts):
    if ts is None:
        return "—"
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return str(ts)


def _severity_badge(sev: str) -> str:
    colors = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    return f"{colors.get((sev or '').lower(), '⚪')} {(sev or '').upper()}"


def _ticket_card(t: dict, extra: str | None = None):
    pri = t.get("priority") or ""
    color = PRIORITY_COLOR.get(pri, "#64748b")
    with st.container(border=True):
        st.markdown(
            f"<div style='font-size:0.85rem;color:{color};font-weight:600'>{pri}</div>",
            unsafe_allow_html=True,
        )
        desc = (t.get("description") or "")[:90]
        st.markdown(f"**{desc}**" + ("…" if len(t.get("description") or "") > 90 else ""))
        st.caption(f"IDX-{t['ticket_id']} · {t.get('category')} · {t.get('assigned_team')}")
        if extra:
            st.caption(extra)


def _render_incident_result(result) -> None:
    """Human-readable summary after an investigation (not raw JSON)."""
    st.success(
        f"Done — incident #{result.incident_id} · status=`{result.status}` · "
        f"{result.llm_calls} LLM calls · tools={result.tools_used}"
    )
    report = result.report or {}
    if not report:
        st.warning("No structured report was submitted. Open **Incident & Steps** for partial progress.")
        return

    st.subheader("Incident report")
    c1, c2, c3 = st.columns(3)
    c1.metric("Category", report.get("root_cause_category") or "—")
    c2.metric("Severity", (report.get("severity") or "—").upper())
    c3.metric("Confidence", report.get("confidence") if report.get("confidence") is not None else "—")

    st.markdown(f"**Root cause:** {report.get('root_cause') or '—'}")
    st.markdown(f"**Recommended action:** {report.get('recommended_action') or '—'}")
    if report.get("proposed_remediation_tool") and report.get("proposed_remediation_tool") != "none":
        st.info(
            f"Remediation staged: `{report.get('proposed_remediation_tool')}` "
            f"({report.get('affected_ticket_count') or '?'} tickets). "
            "Go to **Approve Remediation** to execute it — nothing is fixed until you approve."
        )

    evidence = report.get("evidence") or []
    if evidence:
        st.markdown("**Evidence**")
        for e in evidence:
            if isinstance(e, dict):
                st.markdown(
                    f"- `{e.get('source_tool', '?')}` — {e.get('finding', '')} "
                    f"{('· ' + str(e.get('detail'))) if e.get('detail') else ''}"
                )
            else:
                st.markdown(f"- {e}")

    sample = report.get("affected_ticket_ids_sample") or []
    if sample:
        st.markdown(
            f"**Sample affected ticket IDs** ({len(sample)} shown"
            + (f", of {report.get('affected_ticket_count')}" if report.get("affected_ticket_count") else "")
            + "): "
            + ", ".join(f"IDX-{i}" for i in sample[:25])
        )

    with st.expander("Raw report JSON (debug)"):
        st.json(report)

    b1, b2 = st.columns(2)
    if b1.button("Open Incident & Steps", key=f"goto_steps_{result.incident_id}"):
        st.session_state["last_incident_id"] = result.incident_id
        st.session_state["nav_to"] = "Incident & Steps"
        st.rerun()
    if b2.button("Go to Approve Remediation", key=f"goto_approve_{result.incident_id}"):
        st.session_state["nav_to"] = "Approve Remediation"
        st.rerun()


@st.cache_resource
def _get_mcp() -> MCPConnection:
    conn = MCPConnection()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(conn.start())
    conn._loop = loop  # type: ignore[attr-defined]
    return conn


def _run_async(coro):
    conn = _get_mcp()
    return conn._loop.run_until_complete(coro)  # type: ignore[attr-defined]


def _run_options() -> dict[str, int]:
    rows = fetch_all(
        """SELECT r.run_id, r.dag_run_id, r.status, r.inject_type, r.inject_pct,
                  m.mismatch_pct, m.duplicate_count, m.total_processed, m.total_indexed
           FROM pipeline_runs r
           LEFT JOIN data_quality_metrics m ON m.run_id = r.run_id
           ORDER BY r.run_id DESC LIMIT 30"""
    )
    opts = {}
    for r in rows:
        label = (
            f"#{r['run_id']} {r.get('dag_run_id')} · {r.get('status')} · "
            f"inject={r.get('inject_type')}@{r.get('inject_pct')} · "
            f"mismatch={r.get('mismatch_pct')}%"
        )
        opts[label] = r["run_id"]
    return opts


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("Intelligent IndexOps")
st.sidebar.caption("Service desk · pipeline · AI investigation")

PAGES = [
    "Ticket Board",
    "Pipeline Runs",
    "Faulty Tickets",
    "Alerts",
    "Investigate",
    "Incident & Steps",
    "Approve Remediation",
    "Health check",
]
if "page" not in st.session_state:
    st.session_state.page = "Ticket Board"
if st.session_state.get("nav_to") in PAGES:
    st.session_state.page = st.session_state.pop("nav_to")

page = st.sidebar.radio(
    "Navigate",
    PAGES,
    index=PAGES.index(st.session_state.page),
    label_visibility="collapsed",
)
st.session_state.page = page

# ---------------------------------------------------------------------------
# TICKET BOARD (Kanban-ish)
# ---------------------------------------------------------------------------
if page == "Ticket Board":
    st.header("Ticket Board")
    st.caption("Jira-style overview of IT service-desk tickets (sample of ~48 for the demo).")

    result = ensure_kanban_statuses()
    if result.get("updated"):
        st.toast(f"Updated {result['updated']} tickets to Kanban statuses (todo / in_progress / done).")

    view = st.radio("Group by", ["Status", "Team"], horizontal=True)
    tickets = fetch_all(
        """SELECT ticket_id, description, category, priority, assigned_team, status, created_at
           FROM tickets ORDER BY ticket_id LIMIT %s""",
        (BOARD_SAMPLE,),
    )
    st.caption(f"Showing {len(tickets)} of {fetch_one('SELECT COUNT(*) AS n FROM tickets')['n']} tickets")

    if view == "Status":
        cols = st.columns(len(STATUS_COLUMNS))
        for col, (key, title) in zip(cols, STATUS_COLUMNS):
            group = [t for t in tickets if (t.get("status") or "").lower() == key]
            with col:
                st.markdown(f"### {title} ({len(group)})")
                for t in group:
                    _ticket_card(t)
    else:
        cols = st.columns(len(TEAM_COLUMNS))
        for col, team in zip(cols, TEAM_COLUMNS):
            group = [t for t in tickets if t.get("assigned_team") == team]
            with col:
                st.markdown(f"### {team} ({len(group)})")
                for t in group:
                    _ticket_card(t, extra=f"status: {t.get('status')}")

# ---------------------------------------------------------------------------
# PIPELINE RUNS
# ---------------------------------------------------------------------------
elif page == "Pipeline Runs":
    st.header("Pipeline Runs")
    st.caption("Every indexing run — including ones that reported SUCCESS with bad data.")

    runs = fetch_all(
        """SELECT r.run_id, r.dag_run_id, r.status, r.inject_type, r.inject_pct,
                  r.started_at, r.ended_at,
                  m.total_processed, m.total_indexed, m.mismatch_count, m.mismatch_pct,
                  m.duplicate_count,
                  (SELECT COUNT(*) FROM alerts a WHERE a.run_id = r.run_id) AS alert_count
           FROM pipeline_runs r
           LEFT JOIN data_quality_metrics m ON m.run_id = r.run_id
           ORDER BY r.run_id DESC LIMIT 20"""
    )
    if not runs:
        st.info("No pipeline runs yet. Use `.\\demo.ps1 -SkipInvestigate` to trigger one.")
    else:
        for r in runs:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
                c1.markdown(f"**Run #{r['run_id']}**")
                c1.caption(r.get("dag_run_id") or "")
                c2.metric("Status", r.get("status") or "—")
                c3.metric("Mismatch %", f"{r.get('mismatch_pct') or 0:.1f}%")
                c4.metric("Alerts", r.get("alert_count") or 0)
                st.caption(
                    f"inject={r.get('inject_type')} @ {r.get('inject_pct')} · "
                    f"processed={r.get('total_processed')} · indexed={r.get('total_indexed')} · "
                    f"dupes={r.get('duplicate_count')} · {_fmt(r.get('started_at'))}"
                )
                b1, b2 = st.columns(2)
                if b1.button("View faulty tickets", key=f"faulty_{r['run_id']}"):
                    st.session_state["selected_run_id"] = r["run_id"]
                    st.session_state["nav_to"] = "Faulty Tickets"
                    st.rerun()
                if b2.button("Investigate this run", key=f"invrun_{r['run_id']}"):
                    st.session_state["pending_run_id"] = r["run_id"]
                    st.session_state["nav_to"] = "Investigate"
                    st.rerun()

# ---------------------------------------------------------------------------
# FAULTY TICKETS
# ---------------------------------------------------------------------------
elif page == "Faulty Tickets":
    st.header("Faulty Tickets")
    st.caption("Data-quality issues discovered for a selected pipeline run — even when Airflow said SUCCESS.")

    opts = _run_options()
    if not opts:
        st.warning("No runs available. Trigger the pipeline first (`demo.ps1`).")
    else:
        labels = list(opts.keys())
        default_idx = 0
        sel = st.session_state.get("selected_run_id")
        if sel is not None:
            for i, rid in enumerate(opts.values()):
                if rid == sel:
                    default_idx = i
                    break
        chosen = st.selectbox("Pipeline run", labels, index=default_idx)
        run_id = opts[chosen]
        st.session_state["selected_run_id"] = run_id

        run = fetch_one("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,))
        metrics = fetch_one(
            "SELECT * FROM data_quality_metrics WHERE run_id = %s ORDER BY metric_id DESC LIMIT 1",
            (run_id,),
        )
        alerts = fetch_all("SELECT * FROM alerts WHERE run_id = %s ORDER BY alert_id", (run_id,))

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Processed", metrics.get("total_processed") if metrics else "—")
        m2.metric("Indexed", metrics.get("total_indexed") if metrics else "—")
        m3.metric("Mismatch %", f"{(metrics or {}).get('mismatch_pct') or 0:.1f}%")
        m4.metric("Duplicates", (metrics or {}).get("duplicate_count") or 0)

        dropped = 0
        if metrics:
            dropped = max(
                0,
                (metrics.get("total_processed") or 0)
                - (metrics.get("duplicate_count") or 0)
                - (metrics.get("total_indexed") or 0),
            )

        st.subheader("Issue cards")
        c1, c2, c3 = st.columns(3)
        with c1:
            with st.container(border=True):
                st.markdown("### Category / team mismatch")
                st.markdown(f"**{(metrics or {}).get('mismatch_count') or 0}** tickets misrouted")
                st.caption(f"inject={run.get('inject_type')} @ {run.get('inject_pct')}")
                if (metrics or {}).get("mismatch_count"):
                    st.error("Pipeline SUCCESS, but routing is wrong vs data contract.")
        with c2:
            with st.container(border=True):
                st.markdown("### Missing / dropped fields")
                st.markdown(f"**{dropped}** tickets dropped in validate_data")
                if dropped:
                    st.warning("Required fields blank → never indexed.")
                else:
                    st.success("No material drop for this run.")
        with c3:
            with st.container(border=True):
                st.markdown("### Duplicate tickets")
                st.markdown(f"**{(metrics or {}).get('duplicate_count') or 0}** duplicate ticket_ids")
                if (metrics or {}).get("duplicate_count"):
                    st.warning("Batch contained duplicate ids (retry without idempotency).")
                else:
                    st.success("No duplicates recorded.")

        if alerts:
            st.subheader("Alerts for this run")
            for a in alerts:
                st.markdown(f"{_severity_badge(a['severity'])} `{a['status']}` — {a['message']}")

        st.subheader("Sample mismatched tickets (live index vs source)")
        st.caption("Live comparison against the current OpenSearch index (reflects latest index state).")
        try:
            samples = sample_mismatched_tickets(8)
            if not samples.get("samples"):
                st.info("No mismatches in the live index right now (already fixed, or this run left no live drift).")
            else:
                for s in samples["samples"]:
                    with st.container(border=True):
                        st.markdown(f"**IDX-{s['ticket_id']}** · {s.get('category')} · {s.get('description', '')[:80]}")
                        st.caption(
                            f"expected `{s.get('expected_team')}` · indexed `{s.get('indexed_assigned_team')}` · "
                            f"source team `{s.get('source_assigned_team')}`"
                        )
        except Exception as e:
            st.error(f"Could not sample mismatches: {e}")

        if st.button("Investigate this run", type="primary"):
            st.session_state["pending_run_id"] = run_id
            st.session_state["nav_to"] = "Investigate"
            st.rerun()

# ---------------------------------------------------------------------------
# ALERTS
# ---------------------------------------------------------------------------
elif page == "Alerts":
    st.header("Alerts")
    st.caption("Data-quality alerts raised by the pipeline even when Airflow reported SUCCESS.")

    status_filter = st.selectbox("Status", ["open", "all", "acknowledged", "resolved"], index=0)
    sql = """
        SELECT a.alert_id, a.run_id, a.severity, a.status, a.message, a.created_at,
               r.dag_run_id, r.inject_type, r.inject_pct,
               (SELECT COUNT(*) FROM incidents i WHERE i.alert_id = a.alert_id) AS incident_count
        FROM alerts a
        LEFT JOIN pipeline_runs r ON r.run_id = a.run_id
    """
    if status_filter != "all":
        rows = fetch_all(sql + " WHERE a.status = %s ORDER BY a.alert_id DESC", (status_filter,))
    else:
        rows = fetch_all(sql + " ORDER BY a.alert_id DESC")

    if not rows:
        st.info("No alerts match this filter.")
    else:
        for a in rows:
            with st.container(border=True):
                c1, c2, c3 = st.columns([1, 4, 2])
                c1.markdown(f"**#{a['alert_id']}**")
                c1.markdown(_severity_badge(a["severity"]))
                c2.markdown(a["message"])
                c2.caption(
                    f"run_id={a['run_id']} · {a.get('dag_run_id')} · "
                    f"inject={a.get('inject_type')}@{a.get('inject_pct')} · {_fmt(a.get('created_at'))}"
                )
                c3.markdown(f"status: `{a['status']}`")
                c3.markdown(f"incidents: {a['incident_count']}")
                if st.button("Investigate this alert", key=f"inv_{a['alert_id']}"):
                    st.session_state["pending_alert_id"] = a["alert_id"]
                    st.session_state["nav_to"] = "Investigate"
                    st.rerun()

# ---------------------------------------------------------------------------
# INVESTIGATE (Mode 1 alert / Mode 2 free-form)
# ---------------------------------------------------------------------------
elif page == "Investigate":
    st.header("Investigate")
    mode = st.radio(
        "Mode",
        ["Mode 1 — Alert-triggered", "Mode 2 — Interactive (free-form question)", "From pipeline run"],
        horizontal=False,
    )

    if mode.startswith("Mode 1"):
        st.caption("Start from a real alert row (Task 17 style).")
        open_alerts = fetch_all(
            """SELECT alert_id, severity, left(message, 80) AS msg, run_id
               FROM alerts WHERE status = 'open' ORDER BY alert_id DESC"""
        )
        alert_options = {
            f"#{a['alert_id']} [{a['severity']}] run {a['run_id']}: {a['msg']}": a["alert_id"]
            for a in open_alerts
        }
        default_idx = 0
        pending = st.session_state.pop("pending_alert_id", None)
        if pending is not None:
            for i, aid in enumerate(alert_options.values()):
                if aid == pending:
                    default_idx = i
                    break
        if not alert_options:
            st.warning("No open alerts. Use Mode 2, or run `demo.ps1` to create one.")
        else:
            label = st.selectbox("Open alert", list(alert_options.keys()), index=default_idx)
            alert_id = alert_options[label]
            if st.button("Start investigation", type="primary", key="start_alert"):
                with st.spinner("Agent investigating via MCP tools (typically 30–90s)…"):
                    try:
                        result = _run_async(run_investigation(_get_mcp(), alert_id=alert_id))
                        st.session_state["last_incident_id"] = result.incident_id
                        _render_incident_result(result)
                    except Exception as e:
                        st.error(f"Investigation failed: {e}")

    elif mode.startswith("Mode 2"):
        st.caption(
            "Ask anything about the current pipeline / index state — no alert required. "
            "Uses the same agent loop, MCP tools, and investigation_steps logging."
        )
        question = st.text_area(
            "Ask the agent a question",
            value=st.session_state.get("pending_question", ""),
            placeholder="e.g. why are so many tickets incorrectly categorized?",
            height=100,
        )
        st.session_state.pop("pending_question", None)
        if st.button("Ask agent", type="primary", key="start_question", disabled=not (question or "").strip()):
            with st.spinner("Agent investigating from your question…"):
                try:
                    result = _run_async(
                        run_investigation(_get_mcp(), question=question.strip())
                    )
                    st.session_state["last_incident_id"] = result.incident_id
                    _render_incident_result(result)
                except Exception as e:
                    st.error(f"Investigation failed: {e}")

    else:
        st.caption("Investigate a specific pipeline run by id.")
        opts = _run_options()
        pending_run = st.session_state.pop("pending_run_id", None)
        if not opts:
            st.warning("No pipeline runs yet.")
        else:
            labels = list(opts.keys())
            idx = 0
            if pending_run is not None:
                for i, rid in enumerate(opts.values()):
                    if rid == pending_run:
                        idx = i
                        break
            label = st.selectbox("Run", labels, index=idx)
            run_id = opts[label]
            if st.button("Start investigation", type="primary", key="start_run"):
                with st.spinner("Agent investigating this run…"):
                    try:
                        result = _run_async(run_investigation(_get_mcp(), run_id=run_id))
                        st.session_state["last_incident_id"] = result.incident_id
                        _render_incident_result(result)
                    except Exception as e:
                        st.error(f"Investigation failed: {e}")

# ---------------------------------------------------------------------------
# INCIDENT & STEPS
# ---------------------------------------------------------------------------
elif page == "Incident & Steps":
    st.header("Incident report & live steps")
    st.caption(f"investigation_steps is polled every {POLL_SECONDS}s — this is the streaming view.")

    incidents = fetch_all(
        """SELECT incident_id, alert_id, run_id, status, confidence,
                  left(COALESCE(root_cause, ''), 60) AS rc, created_at
           FROM incidents ORDER BY incident_id DESC LIMIT 30"""
    )
    if not incidents:
        st.info("No incidents yet. Investigate an alert or ask a free-form question first.")
    else:
        labels = {
            f"#{i['incident_id']} · {i['status']} · alert {i['alert_id']} · run {i['run_id']} · {_fmt(i['created_at'])}":
                i["incident_id"]
            for i in incidents
        }
        default = st.session_state.get("last_incident_id")
        idx = 0
        if default is not None:
            for j, aid in enumerate(labels.values()):
                if aid == default:
                    idx = j
                    break
        chosen = st.selectbox("Incident", list(labels.keys()), index=idx)
        incident_id = labels[chosen]

        inc = fetch_one("SELECT * FROM incidents WHERE incident_id = %s", (incident_id,))
        col_a, col_b = st.columns([1, 1])
        with col_a:
            st.subheader("Report")
            st.markdown(f"**Status:** `{inc['status']}`")
            st.markdown(f"**Confidence:** {inc.get('confidence')}")
            st.markdown(f"**Root cause:** {inc.get('root_cause') or '—'}")
            st.markdown(f"**Recommended action:** {inc.get('recommended_action') or '—'}")
            if inc.get("evidence"):
                st.markdown("**Evidence**")
                st.json(inc["evidence"])
            if inc.get("report"):
                with st.expander("Full report JSON"):
                    st.json(inc["report"])
        with col_b:
            st.subheader("Investigation steps (live)")
            steps = fetch_all(
                """SELECT step_id, tool_name, tool_input, tool_output, created_at
                   FROM investigation_steps WHERE incident_id = %s ORDER BY step_id""",
                (incident_id,),
            )
            if not steps:
                st.caption("No steps yet…")
            for s in steps:
                out = s.get("tool_output")
                err = isinstance(out, dict) and out.get("error")
                icon = "⚠️" if err else ("✅" if s["tool_name"] == "submit_incident_report" else "🔧")
                with st.expander(f"{icon} #{s['step_id']}  {s['tool_name']}  ·  {_fmt(s['created_at'])}", expanded=False):
                    st.markdown("**input**")
                    st.json(s.get("tool_input") or {})
                    st.markdown("**output**")
                    text = json.dumps(out or {}, default=str)
                    if len(text) > 4000:
                        st.code(text[:4000] + "\n…truncated")
                    else:
                        st.json(out or {})

            if inc["status"] == "investigating":
                st.info("Investigation still running — auto-refreshing…")
                import time as _t
                _t.sleep(POLL_SECONDS)
                st.rerun()
            elif st.button("Refresh steps"):
                st.rerun()

# ---------------------------------------------------------------------------
# APPROVE
# ---------------------------------------------------------------------------
elif page == "Approve Remediation":
    st.header("Approve remediation")
    st.caption("Pending actions only become real after you click Approve.")

    pending = fetch_all(
        """SELECT a.action_id, a.incident_id, a.action_taken, a.status, a.params, a.created_at,
                  i.root_cause
           FROM remediation_actions a
           LEFT JOIN incidents i ON i.incident_id = a.incident_id
           WHERE a.status = 'pending'
           ORDER BY a.action_id DESC"""
    )
    if not pending:
        st.success("No pending remediation actions.")
    else:
        for a in pending:
            params = a["params"] or {}
            with st.container(border=True):
                st.markdown(f"**Action #{a['action_id']}** · `{a['action_taken']}` · incident {a['incident_id']}")
                st.caption(_fmt(a.get("created_at")))
                if a.get("root_cause"):
                    st.markdown(f"_Incident:_ {a['root_cause']}")
                if isinstance(params, dict) and params.get("reason"):
                    st.markdown(f"**Reason:** {params['reason']}")
                if isinstance(params, dict) and params.get("ticket_count"):
                    st.markdown(f"**Tickets to fix:** {params['ticket_count']}")
                with st.expander("Params"):
                    st.json(params)
                c1, c2 = st.columns(2)
                if c1.button("Approve & execute", key=f"ok_{a['action_id']}", type="primary"):
                    with st.spinner("Executing remediation…"):
                        result = approve(a["action_id"])
                    if result.get("status") == "completed":
                        st.success(result)
                        if a["action_taken"] == "reindex_affected_tickets":
                            with st.spinner("Re-checking source vs index…"):
                                diff = compare_source_vs_index()
                            st.metric(
                                "Mismatch % after fix",
                                f"{diff['mismatched_pct']}%",
                                delta=f"{diff['mismatched_count']} tickets remaining",
                            )
                    else:
                        st.error(result)
                    st.rerun()
                if c2.button("Reject", key=f"no_{a['action_id']}"):
                    execute(
                        "UPDATE remediation_actions SET status = 'rejected' WHERE action_id = %s",
                        (a["action_id"],),
                    )
                    st.warning(f"Action #{a['action_id']} rejected.")
                    st.rerun()

    st.divider()
    st.subheader("Recent remediation history")
    hist = fetch_all(
        """SELECT action_id, incident_id, action_taken, status, records_fixed, records_failed, created_at
           FROM remediation_actions ORDER BY action_id DESC LIMIT 15"""
    )
    if hist:
        st.dataframe(hist, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# HEALTH
# ---------------------------------------------------------------------------
elif page == "Health check":
    st.header("Health check")
    try:
        tickets = fetch_one("SELECT COUNT(*) AS n FROM tickets")["n"]
        alerts = fetch_one("SELECT COUNT(*) AS n FROM alerts WHERE status='open'")["n"]
        incidents = fetch_one("SELECT COUNT(*) AS n FROM incidents")["n"]
        pending = fetch_one("SELECT COUNT(*) AS n FROM remediation_actions WHERE status='pending'")["n"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tickets (Postgres)", tickets)
        c2.metric("Open alerts", alerts)
        c3.metric("Incidents", incidents)
        c4.metric("Pending remediations", pending)
        diff = compare_source_vs_index()
        st.subheader("Source vs index")
        st.json({k: diff[k] for k in (
            "source_count", "indexed_count", "mismatched_count", "mismatched_pct",
            "missing_from_index_count", "wrong_team_by_category",
        )})
        st.subheader("Status mix (board)")
        mix = fetch_all(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status ORDER BY n DESC"
        )
        st.dataframe(mix, use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(e)
