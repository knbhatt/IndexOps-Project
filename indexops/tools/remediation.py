"""Remediation tools.

IMPORTANT: these functions never modify tickets, the OpenSearch index or Airflow.
They only queue a *pending* remediation_actions row. The real work happens in
indexops.approval.approve(action_id) after a human approves it.
"""
from indexops.db import execute, dumps


def _queue(incident_id: int | None, action: str, params: dict, reason: str) -> dict:
    row = execute(
        """INSERT INTO remediation_actions (incident_id, action_taken, status, params)
           VALUES (%s, %s, 'pending', %s::jsonb) RETURNING action_id, status, created_at""",
        (incident_id, action, dumps({**params, "reason": reason})),
    )
    return {
        "action_id": row["action_id"],
        "status": row["status"],
        "action": action,
        "params": params,
        "message": "Queued for human approval. Nothing has been executed.",
    }


def reindex_affected_tickets(ticket_ids: list[int], reason: str, incident_id: int | None = None) -> dict:
    """Request re-indexing of specific tickets with the correct team mapping (requires approval)."""
    ids = sorted({int(t) for t in ticket_ids})
    if not ids:
        return {"error": "ticket_ids is empty"}
    return _queue(incident_id, "reindex_affected_tickets",
                  {"ticket_ids": ids, "ticket_count": len(ids)}, reason)


def trigger_pipeline_rerun(reason: str, reset_injection: bool = True, incident_id: int | None = None) -> dict:
    """Request a fresh run of the Airflow DAG, optionally resetting the failure-injection
    Variables to none/0 first (requires approval)."""
    return _queue(incident_id, "trigger_pipeline_rerun", {"reset_injection": reset_injection}, reason)
