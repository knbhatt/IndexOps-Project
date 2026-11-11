"""Offline tests for the agent loop (no Docker, no API key, no network)."""
import asyncio
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from indexops.agent import loop as L
from indexops.llm_client import LLMResponse, LLMToolCall

TOOL_DEF = {"type": "function", "function": {
    "name": "get_run_metrics", "description": "metrics",
    "parameters": {"type": "object", "properties": {"run_id": {"type": "integer"}}, "required": ["run_id"]}}}
FAIL_DEF = {"type": "function", "function": {
    "name": "fails_tool", "description": "always raises", "parameters": {"type": "object", "properties": {}}}}


class FakeMCP:
    tools = []

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    async def call_tool(self, name, args):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError("boom")
        return {"mismatch_count": 600, "queued": 40}


def reply(*calls, text=None):
    return LLMResponse(text=text, tool_calls=list(calls), finish_reason="stop", usage={},
                       raw_message={"role": "assistant", "content": text or ""})


def call(name, **args):
    return LLMToolCall(id=f"id-{name}", name=name, arguments=args)


def report(source="get_run_metrics", count=600, **over):
    r = {"root_cause_category": "TRANSFORMATION_ERROR", "root_cause": "Mapping drift.", "confidence": 0.9,
         "severity": "high", "recommended_action": "Reindex",
         "evidence": [{"finding": "600 mismatched", "source_tool": source, "detail": "600"}] if source else [],
         "affected_ticket_count": count}
    r.update(over)
    return r


def submit(**kw):
    return reply(call("submit_incident_report", **report(**kw)))


class Env:
    pass


@contextmanager
def _env(real_evaluator=False, latest_run=1, queued=None):
    e = Env()
    with patch.object(L.llm, "chat") as e.chat, \
         patch.object(L.llm, "mcp_tools_to_llm_tools", return_value=[TOOL_DEF, FAIL_DEF]), \
         patch.object(L, "_create_incident", return_value=1), \
         patch.object(L, "_latest_run_id", return_value=latest_run), \
         patch.object(L, "_queued_reindex_scope", return_value=queued), \
         patch.object(L, "_cancel_pending") as e.cancel, \
         patch.object(L, "_log_step"), \
         patch.object(L, "_save_report") as e.save, \
         patch.object(L, "execute") as e.execute:
        if real_evaluator:
            yield e
        else:
            with patch.object(L, "evaluate_report", return_value=("approve", "ok", None)) as e.evaluate:
                yield e


@pytest.fixture
def env():
    with _env() as e:
        yield e


def run(mcp, **kw):
    kw.setdefault("run_id", 1)
    return asyncio.run(L.run_investigation(mcp, **kw))


def test_happy_path_saves_grounded_report(env):
    env.chat.side_effect = [reply(call("get_run_metrics", run_id=1)), submit()]
    assert run(FakeMCP()).status == "report_submitted"
    env.save.assert_called_once()


def test_unknown_tool_is_not_called(env):
    env.chat.side_effect = [reply(call("not_a_tool")), submit(source=None, count=0)]
    mcp = FakeMCP()
    assert run(mcp).status == "report_submitted" and mcp.calls == []


def test_tool_exception_branch_is_really_exercised(env):
    env.chat.side_effect = [reply(call("fails_tool")), submit(source=None, count=0)]
    mcp = FakeMCP(fail={"fails_tool"})
    res = run(mcp)
    assert mcp.calls == ["fails_tool"]          # the failing tool WAS invoked and raised
    assert res.status == "report_submitted"


def test_invalid_tool_arguments_never_reach_the_tool(env):
    env.chat.side_effect = [reply(call("get_run_metrics", run_id="abc")), submit(source=None, count=0)]
    mcp = FakeMCP()
    run(mcp)
    assert mcp.calls == []


def test_malformed_json_arguments_never_reach_the_tool(env):
    bad = LLMToolCall(id="x", name="get_run_metrics", arguments={"_raw": "{bad"})
    env.chat.side_effect = [reply(bad), submit(source=None, count=0)]
    mcp = FakeMCP()
    run(mcp)
    assert mcp.calls == []


def test_report_citing_uncalled_tool_is_rejected_then_repaired(env):
    env.chat.side_effect = [submit(source="never_called", count=0), submit(source=None, count=0)]
    assert run(FakeMCP()).status == "report_submitted"
    assert env.chat.call_count == 2
    assert env.save.call_args[0][1]["evidence"] == []


def test_unsupported_count_is_rejected_then_repaired(env):
    env.chat.side_effect = [reply(call("get_run_metrics", run_id=1)), submit(count=999), submit(count=600)]
    assert run(FakeMCP()).status == "report_submitted"
    assert env.save.call_args[0][1]["affected_ticket_count"] == 600


def test_invalid_report_after_retries_is_not_saved_and_cancels_actions(env):
    env.chat.side_effect = [submit(confidence=5)] * 3
    assert run(FakeMCP()).status == "incomplete"
    env.save.assert_not_called()
    env.cancel.assert_called()


def test_evaluator_correction_is_used_when_valid(env):
    env.chat.side_effect = [reply(call("get_run_metrics", run_id=1)), submit()]
    fixed = report(root_cause="Corrected root cause.")
    with patch.object(L, "evaluate_report", return_value=("corrected", "fixed", fixed)):
        assert run(FakeMCP()).status == "report_submitted"
    assert env.save.call_args[0][1]["root_cause"] == "Corrected root cause."


def test_invalid_evaluator_correction_marks_incomplete(env):
    env.chat.side_effect = [reply(call("get_run_metrics", run_id=1)), submit()]
    bad_fix = report(source="never_called")
    with patch.object(L, "evaluate_report", return_value=("corrected", "fixed", bad_fix)):
        res = run(FakeMCP())
    assert res.status == "incomplete"
    env.cancel.assert_called()


def test_invalid_evaluator_verdict_marks_incomplete(env):
    env.chat.side_effect = [reply(call("get_run_metrics", run_id=1)), submit()]
    with patch.object(L, "evaluate_report", return_value=("invalid", "bad verdict", None)):
        assert run(FakeMCP()).status == "incomplete"


def test_evaluator_unavailable_saves_report_as_unvalidated(env):
    env.chat.side_effect = [reply(call("get_run_metrics", run_id=1)), submit()]
    with patch.object(L, "evaluate_report", return_value=("unavailable", "quota", None)):
        res = run(FakeMCP())
    assert res.status == "report_submitted"
    updates = [c for c in env.execute.call_args_list if "validation_passed" in c[0][0]]
    assert updates and updates[-1][0][1][0] is None          # validation_passed stays NULL


def test_report_scope_must_match_queued_action_then_repairs():
    with _env(queued=40) as env:
        env.chat.side_effect = [
            reply(call("get_run_metrics", run_id=1)),
            submit(count=600, proposed_remediation_tool="reindex_affected_tickets"),   # disagrees with queued 40
            submit(count=40, proposed_remediation_tool="reindex_affected_tickets"),
        ]
        assert run(FakeMCP()).status == "report_submitted"
        assert env.save.call_args[0][1]["affected_ticket_count"] == 40


def test_real_evaluator_runs_through_the_loop():
    with _env(real_evaluator=True) as env:
        verdict = LLMResponse(text=None, tool_calls=[
            LLMToolCall(id="e", name="evaluate_report", arguments={"verdict": "approve", "notes": "ok"})])
        env.chat.side_effect = [reply(call("get_run_metrics", run_id=1)), submit(), verdict]
        res = run(FakeMCP())
    assert res.status == "report_submitted" and env.chat.call_count == 3


def test_stale_run_is_refused_without_calling_the_llm():
    with _env(latest_run=2) as env:
        res = run(FakeMCP(), run_id=1)
    assert res.status == "stale"
    env.chat.assert_not_called()


def test_llm_failure_returns_error_and_cancels_actions(env):
    env.chat.side_effect = RuntimeError("api down")
    assert run(FakeMCP()).status == "error"
    env.cancel.assert_called()


def test_iteration_cap_without_submit_is_incomplete(env):
    env.chat.return_value = reply(text="still thinking")
    assert run(FakeMCP()).status == "incomplete"
    assert env.chat.call_count == L.MAX_ITERATIONS
