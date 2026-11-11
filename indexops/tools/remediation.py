"""Remediation tools.

IMPORTANT: these functions never modify tickets, the OpenSearch index or Airflow.
They only queue a *pending* remediation_actions row. The real work happens in
indexops.approval.approve(action_id) after a human approves it.
"""
from indexops.db import execute, dumps, fetch_one


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


def _affected_ids() -> list[int]:
    """Deterministic full repair scope: every ticket that is mismatched OR missing from the index.

    Computed in code (never taken from the LLM), so the queued action always covers the
    whole incident, not a sample the model happened to pick.
    """
    from indexops.tools.investigation import affected_ticket_ids  # local import: avoids a cycle
    return affected_ticket_ids()


def reindex_affected_tickets(reason: str, ticket_ids: list[int] | None = None,
                             incident_id: int | None = None) -> dict:
    """Request re-indexing of ALL currently mismatched or missing tickets (requires approval).

    The repair scope is computed deterministically from live data; `ticket_ids` is optional and
    only recorded for audit (the agent's own estimate), it does not limit the scope.
    """
    ids = _affected_ids()
    if not ids:
        return {"error": "no mismatched or missing tickets found; nothing to reindex"}
    latest = fetch_one("SELECT MAX(run_id) AS m FROM pipeline_runs")
    return _queue(
        incident_id, "reindex_affected_tickets",
        {"ticket_ids": ids, "ticket_count": len(ids), "scope": "all_mismatched_and_missing",
         "agent_supplied_count": len(ticket_ids or []),
         "staged_for_run_id": (latest or {}).get("m")},
        reason,
    )


def trigger_pipeline_rerun(reason: str, reset_injection: bool = True, incident_id: int | None = None) -> dict:
    """Request a fresh run of the Airflow DAG, optionally resetting the failure-injection
    Variables to none/0 first (requires approval)."""
    return _queue(incident_id, "trigger_pipeline_rerun", {"reset_injection": reset_injection}, reason)