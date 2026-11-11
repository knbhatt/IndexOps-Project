from unittest.mock import patch

from indexops import approval as A
from indexops.tools import remediation as R


def _action(count=414, kind="reindex_affected_tickets"):
    return {"action_id": 1, "incident_id": 7, "action_taken": kind, "status": "pending",
            "params": {"ticket_ids": list(range(count)), "ticket_count": count}}


def _db(action, inc, latest=1, claim=True):
    def fetch_one(sql, params=None):
        if "FROM remediation_actions" in sql:
            return action
        if "FROM incidents" in sql:
            return inc
        if "MAX(run_id)" in sql:
            return {"m": latest}

    def execute(sql, params=None):
        if "RETURNING action_id" in sql:
            return {"action_id": 1} if claim else None
        return None
    return fetch_one, execute


def _inc(passed=True, run_id=1, count=414):
    return {"status": "report_submitted", "validation_passed": passed, "run_id": run_id,
            "report": {"affected_ticket_count": count}}


def _approve(action, inc, latest=1, claim=True, result=None, **kw):
    f, e = _db(action, inc, latest, claim)
    result = result or {"records_fixed": 414, "records_failed": 0}
    with patch.object(A, "fetch_one", side_effect=f), patch.object(A, "execute", side_effect=e) as ex, \
         patch.object(A, "_execute_reindex", return_value=dict(result)), \
         patch.object(A, "_verify_after_fix", return_value={"mismatched_count": 0}):
        return A.approve(1, **kw), ex


def test_validated_scope_consistent_incident_executes_and_stores_proof():
    out, ex = _approve(_action(), _inc())
    assert out["status"] == "completed" and out["post_fix_verification"] == {"mismatched_count": 0}


def test_unvalidated_incident_is_blocked():
    out, ex = _approve(_action(), _inc(passed=None))
    assert out["status"] == "blocked" and "not validated" in out["error"]
    assert not any("'running'" in c[0][0] for c in ex.call_args_list)     # never claimed


def test_failed_validation_is_blocked():
    assert _approve(_action(), _inc(passed=False))[0]["status"] == "blocked"


def test_manual_override_allows_unvalidated():
    out, _ = _approve(_action(), _inc(passed=None), allow_unvalidated=True)
    assert out["status"] == "completed"


def test_stale_incident_is_blocked_even_with_override():
    out, _ = _approve(_action(), _inc(run_id=1), latest=2, allow_unvalidated=True)
    assert out["status"] == "blocked" and "latest run" in out["error"]


def test_scope_mismatch_between_report_and_action_is_blocked():
    out, _ = _approve(_action(count=40), _inc(count=414))
    assert out["status"] == "blocked" and "scope mismatch" in out["error"]


def test_lost_atomic_claim_does_not_execute():
    out, _ = _approve(_action(), _inc(), claim=False)
    assert "already claimed" in out["error"]


def test_partial_failures_are_not_reported_as_completed():
    out, _ = _approve(_action(), _inc(), result={"records_fixed": 410, "records_failed": 4})
    assert out["status"] == "partial"


# ---- remediation tool: scope is computed in code, not taken from the LLM ----
def test_reindex_tool_stages_full_scope_ignoring_agent_sample():
    row = {"action_id": 5, "status": "pending", "created_at": "t"}
    with patch.object(R, "_affected_ids", return_value=list(range(414))), \
         patch.object(R, "execute", return_value=row), patch.object(R, "fetch_one", return_value={"m": 3}):
        out = R.reindex_affected_tickets(reason="drift", ticket_ids=list(range(40)), incident_id=7)
    assert out["params"]["ticket_count"] == 414 and out["params"]["agent_supplied_count"] == 40
    assert out["params"]["staged_for_run_id"] == 3


def test_reindex_tool_with_nothing_to_fix_errors():
    with patch.object(R, "_affected_ids", return_value=[]):
        assert "error" in R.reindex_affected_tickets(reason="x")
