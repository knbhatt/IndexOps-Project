from unittest.mock import patch

from indexops.agent import validate as V
from indexops.llm_client import LLMResponse, LLMToolCall


def _report(**over):
    r = {"root_cause_category": "TRANSFORMATION_ERROR", "root_cause": "Mapping drift.", "confidence": 0.9,
         "severity": "high", "recommended_action": "Reindex",
         "evidence": [{"finding": "600 mismatched", "source_tool": "get_run_metrics", "detail": "600"}],
         "affected_ticket_count": 600}
    r.update(over)
    return r


OBS = '[{"tool": "get_run_metrics", "output": {"mismatch_count": 600}}]'


# ---------------- check_report ----------------
def test_valid_report_has_no_problems():
    clean, problems = V.check_report(_report(), {"get_run_metrics"}, OBS)
    assert clean is not None and problems == []


def test_schema_violation_returns_none():
    clean, problems = V.check_report(_report(confidence=5), {"get_run_metrics"}, OBS)
    assert clean is None and "confidence" in problems[0]


def test_unknown_category_rejected():
    clean, _ = V.check_report(_report(root_cause_category="MADE_UP"), {"get_run_metrics"}, OBS)
    assert clean is None


def test_evidence_from_uncalled_tool_is_flagged():
    _, problems = V.check_report(_report(), set(), OBS)
    assert any("never successfully called" in p for p in problems)


def test_unsupported_count_is_flagged():
    _, problems = V.check_report(_report(affected_ticket_count=999), {"get_run_metrics"}, OBS)
    assert any("999" in p for p in problems)


def test_reindex_proposal_without_queued_action_is_flagged():
    rep = _report(proposed_remediation_tool="reindex_affected_tickets")
    _, problems = V.check_report(rep, {"get_run_metrics"}, OBS, queued_scope=None)
    assert any("no reindex action is queued" in p for p in problems)


def test_report_scope_must_equal_queued_scope():
    rep = _report(proposed_remediation_tool="reindex_affected_tickets", affected_ticket_count=600)
    _, problems = V.check_report(rep, {"get_run_metrics"}, OBS, queued_scope=40)
    assert any("queued reindex action covers 40" in p for p in problems)


def test_report_scope_equal_to_queued_scope_passes():
    obs = OBS.replace("600", "40")
    rep = _report(proposed_remediation_tool="reindex_affected_tickets", affected_ticket_count=40)
    _, problems = V.check_report(rep, {"get_run_metrics"}, obs, queued_scope=40)
    assert problems == []


# ---------------- evaluate_report: the REAL validator, only the LLM is mocked ----------------
def _resp(**args):
    return LLMResponse(text=None, tool_calls=[LLMToolCall(id="e", name="evaluate_report", arguments=args)])


def test_evaluator_approve():
    with patch.object(V.llm, "chat", return_value=_resp(verdict="approve", notes="ok")):
        assert V.evaluate_report(_report(), [])[0] == "approve"


def test_evaluator_corrected_returns_report():
    fixed = _report(root_cause="Better.")
    with patch.object(V.llm, "chat", return_value=_resp(verdict="corrected", notes="fix", corrected_report=fixed)):
        verdict, _, rep = V.evaluate_report(_report(), [])
    assert verdict == "corrected" and rep["root_cause"] == "Better."


def test_corrected_without_report_is_invalid_not_silently_approved():
    with patch.object(V.llm, "chat", return_value=_resp(verdict="corrected", notes="fix")) as chat:
        verdict, _, rep = V.evaluate_report(_report(), [])
    assert verdict == "invalid" and rep is None and chat.call_count == 2   # retried once


def test_unexpected_verdict_string_is_invalid():
    with patch.object(V.llm, "chat", return_value=_resp(verdict="rejected", notes="?")):
        assert V.evaluate_report(_report(), [])[0] == "invalid"


def test_missing_tool_call_is_invalid():
    with patch.object(V.llm, "chat", return_value=LLMResponse(text="looks fine")):
        assert V.evaluate_report(_report(), [])[0] == "invalid"


def test_malformed_then_valid_answer_is_accepted_on_retry():
    seq = [_resp(verdict="maybe"), _resp(verdict="approve", notes="ok")]
    with patch.object(V.llm, "chat", side_effect=seq):
        assert V.evaluate_report(_report(), [])[0] == "approve"


def test_evaluator_llm_failure_is_unavailable_and_not_retried():
    with patch.object(V.llm, "chat", side_effect=RuntimeError("429")) as chat:
        assert V.evaluate_report(_report(), [])[0] == "unavailable"
    assert chat.call_count == 1
