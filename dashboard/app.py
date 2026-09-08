"""Intelligent IndexOps — Day 3 Streamlit dashboard.

Views:
  - Alerts table (the "notifications panel")
  - Incident report
  - Live investigation_steps log (polls every ~2s)
  - Approve button wired to indexops.approval.approve()

Run:  streamlit run dashboard/app.py
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
from indexops.tools.investigation import compare_source_vs_index  # noqa: E402

st.set_page_config(page_title="Intelligent IndexOps", page_icon="🔎", layout="wide")

POLL_SECONDS = 2


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


@st.cache_resource
def _get_mcp() -> MCPConnection:
    """One MCP subprocess for the Streamlit process lifetime."""
    conn = MCPConnection()
    # Streamlit is sync; spin up the async MCP connection once.
    loop = asyncio.new_event_loop()
    loop.run_until_complete(conn.start())
    conn._loop = loop  # type: ignore[attr-defined]
    return conn


def _run_async(coro):
    conn = _get_mcp()
    return conn._loop.run_until_complete(coro)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------
st.sidebar.title("Intelligent IndexOps")
st.sidebar.caption("Airflow → OpenSearch · AI investigation")

PAGES = ["Alerts", "Investigate", "Incident & Steps", "Approve Remediation", "Health check"]
if "page" not in st.session_state:
    st.session_state.page = "Alerts"
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
# ALERTS
# ---------------------------------------------------------------------------
if page == "Alerts":
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
        sql += " WHERE a.status = %s"
        rows = fetch_all(sql + " ORDER BY a.alert_id DESC", (status_filter,))
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
# INVESTIGATE
# ---------------------------------------------------------------------------
elif page == "Investigate":
    st.header("Investigate")
    st.caption("Kick off the Agent from a real alert. Steps stream into the Incident & Steps view.")

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
        for i, (label, aid) in enumerate(alert_options.items()):
            if aid == pending:
                default_idx = i
                break

    if not alert_options:
        st.warning("No open alerts. Run the pipeline with a failure injected first (or use demo.ps1).")
    else:
        label = st.selectbox("Open alert", list(alert_options.keys()), index=default_idx)
        alert_id = alert_options[label]

        if st.button("Start investigation", type="primary"):
            with st.spinner("Agent investigating via MCP tools (typically 30–90s)…"):
                try:
                    result = _run_async(run_investigation(_get_mcp(), alert_id=alert_id))
                    st.session_state["last_incident_id"] = result.incident_id
                    st.success(
                        f"Done — incident #{result.incident_id} · status={result.status} · "
                        f"{result.llm_calls} LLM calls · tools={result.tools_used}"
                    )
                    if result.report:
                        st.json(result.report)
                except Exception as e:
                    st.error(f"Investigation failed: {e}")

# ---------------------------------------------------------------------------
# INCIDENT & STEPS (live poll)
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
        st.info("No incidents yet. Investigate an alert first.")
    else:
        labels = {
            f"#{i['incident_id']} · {i['status']} · alert {i['alert_id']} · {_fmt(i['created_at'])}": i["incident_id"]
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
                err = None
                out = s.get("tool_output")
                if isinstance(out, dict) and out.get("error"):
                    err = out["error"]
                icon = "⚠️" if err else ("✅" if s["tool_name"] == "submit_incident_report" else "🔧")
                with st.expander(f"{icon} #{s['step_id']}  {s['tool_name']}  ·  {_fmt(s['created_at'])}", expanded=False):
                    st.markdown("**input**")
                    st.json(s.get("tool_input") or {})
                    st.markdown("**output**")
                    out = s.get("tool_output") or {}
                    # Truncate huge outputs for the UI
                    text = json.dumps(out, default=str)
                    if len(text) > 4000:
                        st.code(text[:4000] + "\n…truncated")
                    else:
                        st.json(out)

            if inc["status"] in ("investigating",):
                st.info("Investigation still running — auto-refreshing…")
                import time as _t
                _t.sleep(POLL_SECONDS)
                st.rerun()
            else:
                if st.button("Refresh steps"):
                    st.rerun()

# ---------------------------------------------------------------------------
# APPROVE REMEDIATION
# ---------------------------------------------------------------------------
elif page == "Approve Remediation":
    st.header("Approve remediation")
    st.caption("Pending actions only become real after you click Approve. Nothing else can mutate the index or re-run the DAG.")

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
                reason = params.get("reason") if isinstance(params, dict) else None
                if reason:
                    st.markdown(f"**Reason:** {reason}")
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
                        # Show post-fix mismatch if this was a reindex
                        if a["action_taken"] == "reindex_affected_tickets":
                            with st.spinner("Re-checking source vs index…"):
                                diff = compare_source_vs_index()
                            st.metric("Mismatch % after fix", f"{diff['mismatched_pct']}%",
                                      delta=f"{diff['mismatched_count']} tickets remaining")
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
    except Exception as e:
        st.error(e)
