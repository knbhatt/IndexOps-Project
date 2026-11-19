"""Human-approval gate (Task 16).

Nothing in IndexOps may modify tickets, OpenSearch, or trigger an Airflow re-run
except through approve(action_id). Remediation tools only write pending rows;
this module is the only place that executes them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import time
from typing import Any

import requests
from indexops.http_util import http_post
from opensearchpy import helpers
from requests.auth import HTTPBasicAuth

from indexops.config import AIRFLOW_BASE_URL, AIRFLOW_USER, AIRFLOW_PASSWORD, AIRFLOW_DAG_ID
from indexops.contract import TICKETS_INDEX, EMBEDDING_MODEL, EMBEDDING_DIM, expected_team
from indexops.db import fetch_one, execute, fetch_all, os_client, dumps


def _gate(row: dict[str, Any], allow_unvalidated: bool) -> tuple[str | None, dict[str, Any] | None]:
    """Return (reason_to_block_or_None, incident_row).

    Order matters: the stale-run and report/action scope checks ALWAYS apply. The manual override
    can waive only the "incident is validated" requirement, never these two.
    """
    inc = None
    if row.get("incident_id") is not None:
        inc = fetch_one(
            "SELECT status, validation_passed, run_id, report FROM incidents WHERE incident_id = %s",
            (row["incident_id"],),
        )
    if inc is None:
        return "action has no incident; refusing to run an unreviewed action", None

    # Stale incidents are never executable: the index is rebuilt every run, so an older
    # incident's evidence no longer describes the live state. Re-investigate instead.
    latest = (fetch_one("SELECT MAX(run_id) AS m FROM pipeline_runs") or {}).get("m")
    if inc.get("run_id") is not None and latest is not None and inc["run_id"] != latest:
        return (f"incident is for run {inc['run_id']} but the latest run is {latest}; "
                "re-investigate the latest run before approving"), inc

    params = row.get("params") or {}
    if isinstance(params, str):
        params = json.loads(params)
    report = inc.get("report") or {}
    if isinstance(report, str):
        report = json.loads(report)
    if row.get("action_taken") == "reindex_affected_tickets":
        if not report:
            return "incident has no saved report to compare the queued action against", inc
        if report.get("affected_ticket_count") != params.get("ticket_count"):
            return (f"scope mismatch: report says {report.get('affected_ticket_count')} tickets but the "
                    f"queued action covers {params.get('ticket_count')}"), inc

    if not allow_unvalidated and inc.get("validation_passed") is not True:
        return ("incident report is not validated (validation_passed is not true); "
                "fix the report or use the explicit unvalidated override"), inc
    return None, inc


def approve(action_id: int, allow_unvalidated: bool = False) -> dict[str, Any]:
    """Execute a pending remediation_actions row and mark it completed, partial or failed.

    Refuses when: the action is not pending; the incident is stale (not the latest run); the
    incident report is not validated or its scope disagrees with the queued action (unless the
    caller explicitly passes allow_unvalidated=True, a manual override).
    """
    row = fetch_one("SELECT * FROM remediation_actions WHERE action_id = %s", (action_id,))
    if not row:
        return {"error": f"action_id {action_id} not found"}
    if row["status"] != "pending":
        return {"error": f"action_id {action_id} is '{row['status']}', not 'pending'"}

    blocked, inc = _gate(row, allow_unvalidated)
    if blocked:
        return {"action_id": action_id, "status": "blocked", "error": blocked}

    # Durable audit trail: record when the validation requirement was manually waived.
    override_used = bool(allow_unvalidated and not (inc and inc.get("validation_passed") is True))
    audit = ({"approval_override": {
        "used": True, "incident_validation_passed": (inc or {}).get("validation_passed"),
        "approved_at": datetime.now(timezone.utc).isoformat()}} if override_used else {})

    # Atomic claim: only one concurrent approver can move pending -> running.
    claimed = execute(
        "UPDATE remediation_actions SET status = 'running' WHERE action_id = %s AND status = 'pending' "
        "RETURNING action_id",
        (action_id,),
    )
    if not claimed:
        return {"error": f"action_id {action_id} was already claimed by another approval"}

    action = row["action_taken"]
    params = row["params"] or {}
    if isinstance(params, str):
        params = json.loads(params)

    try:
        if action == "reindex_affected_tickets":
            result = _execute_reindex(params.get("ticket_ids") or [])
            result["post_fix_verification"] = _verify_after_fix()
        elif action == "trigger_pipeline_rerun":
            result = _execute_pipeline_rerun(bool(params.get("reset_injection", True)))
        else:
            raise ValueError(f"unknown action_taken '{action}'")

        fixed, failed = result.get("records_fixed", 0), result.get("records_failed", 0)
        status = "partial" if failed else "completed"
        execute(
            """UPDATE remediation_actions
               SET status = %s, records_fixed = %s, records_failed = %s,
                   params = COALESCE(params, '{}'::jsonb) || %s::jsonb
               WHERE action_id = %s""",
            (status, fixed, failed, dumps({"execution_result": result, **audit}), action_id),
        )
        return {"action_id": action_id, "status": status, "action": action,
                "override_used": override_used, **result}
    except Exception as e:
        execute(
            """UPDATE remediation_actions
               SET status = 'failed',
                   params = COALESCE(params, '{}'::jsonb) || %s::jsonb
               WHERE action_id = %s""",
            (dumps({"execution_error": str(e), **audit}), action_id),
        )
        return {"action_id": action_id, "status": "failed", "action": action, "error": str(e)}


def _verify_after_fix() -> dict[str, Any]:
    """Fresh source-vs-index comparison, stored as durable evidence of what the fix achieved."""
    from indexops.tools.investigation import compare_source_vs_index
    d = compare_source_vs_index()
    return {k: d[k] for k in ("source_count", "indexed_count", "mismatched_count",
                              "mismatched_pct", "missing_from_index_count", "orphan_in_index_count")}


def _execute_reindex(ticket_ids: list[int]) -> dict[str, Any]:
    """Re-index the given tickets with the correct category→team mapping into OpenSearch.

    Source of truth is Postgres. We rewrite assigned_team from the data contract and
    regenerate embeddings so the index is fully consistent with a clean pipeline run.
    """
    ids = sorted({int(t) for t in ticket_ids})
    if not ids:
        return {"records_fixed": 0, "records_failed": 0, "message": "no ticket_ids"}

    tickets = fetch_all(
        """SELECT ticket_id, description, category, priority, assigned_team
           FROM tickets WHERE ticket_id = ANY(%s)""",
        (ids,),
    )
    found = {t["ticket_id"] for t in tickets}
    missing = [i for i in ids if i not in found]

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(EMBEDDING_MODEL)
    texts = [t["description"] or "" for t in tickets]
    vectors = model.encode(texts, batch_size=64).tolist() if texts else []

    client = os_client()
    if not client.indices.exists(index=TICKETS_INDEX):
        client.indices.create(index=TICKETS_INDEX, body={
            "mappings": {"properties": {
                "ticket_id": {"type": "integer"},
                "description": {"type": "text"},
                "category": {"type": "keyword"},
                "priority": {"type": "keyword"},
                "assigned_team": {"type": "keyword"},
                "embedding": {"type": "knn_vector", "dimension": EMBEDDING_DIM},
            }},
            "settings": {"index.knn": True},
        })

    actions = []
    for t, v in zip(tickets, vectors):
        correct_team = expected_team(t["category"])
        actions.append({
            "_op_type": "index",
            "_index": TICKETS_INDEX,
            "_id": t["ticket_id"],
            "_source": {
                "ticket_id": t["ticket_id"],
                "description": t["description"],
                "category": t["category"],
                "priority": t["priority"],
                "assigned_team": correct_team,
                "embedding": v,
            },
        })

    success, errors = helpers.bulk(client, actions, chunk_size=200, raise_on_error=False, request_timeout=120)
    client.indices.refresh(index=TICKETS_INDEX)
    failed = len(errors) if isinstance(errors, list) else 0
    return {
        "records_fixed": success,
        "records_failed": failed + len(missing),
        "missing_from_source": missing[:50],
        "message": f"Re-indexed {success} tickets with correct team mapping.",
    }


def _airflow_auth():
    return HTTPBasicAuth(AIRFLOW_USER, AIRFLOW_PASSWORD)


def _execute_pipeline_rerun(reset_injection: bool) -> dict[str, Any]:
    """Reset injection Variables (optional) and trigger a fresh Airflow DAG run."""
    if reset_injection:
        for key, val in (("inject_failure_type", "none"), ("inject_failure_pct", "0")):
            r = requests.patch(
                f"{AIRFLOW_BASE_URL}/api/v1/variables/{key}",
                json={"key": key, "value": val},
                auth=_airflow_auth(),
                timeout=30,
            )
            if r.status_code == 404:
                r = http_post(
                    f"{AIRFLOW_BASE_URL}/api/v1/variables",
                    json={"key": key, "value": val},
                    auth=_airflow_auth(),
                    timeout=30,
                )
            r.raise_for_status()

    run_id = f"remediation_{int(time.time())}"
    r = http_post(
        f"{AIRFLOW_BASE_URL}/api/v1/dags/{AIRFLOW_DAG_ID}/dagRuns",
        json={"dag_run_id": run_id, "conf": {}},
        auth=_airflow_auth(),
        timeout=30,
    )
    r.raise_for_status()
    return {
        "records_fixed": 0,
        "records_failed": 0,
        "dag_run_id": run_id,
        "reset_injection": reset_injection,
        "message": f"Triggered DAG run {run_id}" + (" with injection cleared" if reset_injection else ""),
    }
