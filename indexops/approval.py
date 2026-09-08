"""Human-approval gate (Task 16).

Nothing in IndexOps may modify tickets, OpenSearch, or trigger an Airflow re-run
except through approve(action_id). Remediation tools only write pending rows;
this module is the only place that executes them.
"""
from __future__ import annotations

import time
from typing import Any

import requests
from opensearchpy import helpers
from requests.auth import HTTPBasicAuth

from indexops.config import AIRFLOW_BASE_URL, AIRFLOW_USER, AIRFLOW_PASSWORD, AIRFLOW_DAG_ID
from indexops.contract import TICKETS_INDEX, EMBEDDING_MODEL, EMBEDDING_DIM, expected_team
from indexops.db import fetch_one, execute, fetch_all, os_client, dumps


def approve(action_id: int) -> dict[str, Any]:
    """Execute a pending remediation_actions row and mark it completed or failed."""
    row = fetch_one("SELECT * FROM remediation_actions WHERE action_id = %s", (action_id,))
    if not row:
        return {"error": f"action_id {action_id} not found"}
    if row["status"] != "pending":
        return {"error": f"action_id {action_id} is '{row['status']}', not 'pending'"}

    execute(
        "UPDATE remediation_actions SET status = 'running' WHERE action_id = %s",
        (action_id,),
    )

    action = row["action_taken"]
    params = row["params"] or {}
    if isinstance(params, str):
        import json
        params = json.loads(params)

    try:
        if action == "reindex_affected_tickets":
            result = _execute_reindex(params.get("ticket_ids") or [])
        elif action == "trigger_pipeline_rerun":
            result = _execute_pipeline_rerun(bool(params.get("reset_injection", True)))
        else:
            raise ValueError(f"unknown action_taken '{action}'")

        execute(
            """UPDATE remediation_actions
               SET status = 'completed', records_fixed = %s, records_failed = %s,
                   params = COALESCE(params, '{}'::jsonb) || %s::jsonb
               WHERE action_id = %s""",
            (result.get("records_fixed", 0), result.get("records_failed", 0),
             dumps({"execution_result": result}), action_id),
        )
        return {"action_id": action_id, "status": "completed", "action": action, **result}
    except Exception as e:
        execute(
            """UPDATE remediation_actions
               SET status = 'failed',
                   params = COALESCE(params, '{}'::jsonb) || %s::jsonb
               WHERE action_id = %s""",
            (dumps({"execution_error": str(e)}), action_id),
        )
        return {"action_id": action_id, "status": "failed", "action": action, "error": str(e)}


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
                r = requests.post(
                    f"{AIRFLOW_BASE_URL}/api/v1/variables",
                    json={"key": key, "value": val},
                    auth=_airflow_auth(),
                    timeout=30,
                )
            r.raise_for_status()

    run_id = f"remediation_{int(time.time())}"
    r = requests.post(
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
