"""Automated tests for the agent loop's robustness — covers the 'should fix'
items from the L2 review: unknown tool, malformed JSON, tool failure, LLM
failure, iteration cap, and the new evaluator rejecting unsupported evidence.

Run with: pytest tests/ -v
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from indexops.llm_client import LLMResponse, LLMToolCall


def _mock_response(tool_calls=None, text=None):
    return LLMResponse(
        text=text, tool_calls=tool_calls or [], finish_reason="stop",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        raw_message={"role": "assistant", "content": text},
    )


class FakeMCP:
    tools = [{"name": "get_run_metrics", "description": "x", "input_schema": {"type": "object", "properties": {}}}]

    async def call_tool(self, name, args):
        if name == "fails_tool":
            raise RuntimeError("boom")
        return {"ok": True}


@pytest.mark.asyncio
async def test_unknown_tool_does_not_crash():
    from indexops.agent import loop as agent_loop
    bad_call = LLMToolCall(id="1", name="not_a_real_tool", arguments={})
    submit_call = LLMToolCall(id="2", name="submit_incident_report", arguments={
        "root_cause_category": "NO_ISSUE", "root_cause": "fine", "confidence": 1.0,
        "severity": "low", "evidence": [], "recommended_action": "none",
    })
    with patch("indexops.agent.loop.llm.chat") as mock_chat, \
         patch("indexops.agent.loop._create_incident", return_value=1), \
         patch("indexops.agent.loop._log_step"), \
         patch("indexops.agent.loop._save_report"), \
         patch("indexops.agent.loop.execute"), \
         patch("indexops.agent.loop.validate_report") as mock_val:
        mock_val.return_value = MagicMock(
            passed_deterministic=True, verdict="approve",
            deterministic_notes="ok", evaluator_notes="ok", final_report={},
        )
        mock_chat.side_effect = [
            _mock_response(tool_calls=[bad_call]),
            _mock_response(tool_calls=[submit_call]),
        ]
        result = await agent_loop.run_investigation(FakeMCP(), alert_id=None, run_id=1)
        assert result.status == "report_submitted"


@pytest.mark.asyncio
async def test_tool_failure_does_not_crash():
    from indexops.agent import loop as agent_loop
    fail_call = LLMToolCall(id="1", name="fails_tool", arguments={})
    submit_call = LLMToolCall(id="2", name="submit_incident_report", arguments={
        "root_cause_category": "NO_ISSUE", "root_cause": "fine", "confidence": 1.0,
        "severity": "low", "evidence": [], "recommended_action": "none",
    })
    with patch("indexops.agent.loop.llm.chat") as mock_chat, \
         patch("indexops.agent.loop._create_incident", return_value=1), \
         patch("indexops.agent.loop._log_step"), \
         patch("indexops.agent.loop._save_report"), \
         patch("indexops.agent.loop.execute"), \
         patch("indexops.agent.loop.validate_report") as mock_val:
        mock_val.return_value = MagicMock(
            passed_deterministic=True, verdict="approve",
            deterministic_notes="ok", evaluator_notes="ok", final_report={},
        )
        mock_chat.side_effect = [
            _mock_response(tool_calls=[fail_call]),
            _mock_response(tool_calls=[submit_call]),
        ]
        result = await agent_loop.run_investigation(FakeMCP(), alert_id=None, run_id=1)
        assert result.status == "report_submitted"


@pytest.mark.asyncio
async def test_llm_failure_saves_error_status():
    from indexops.agent import loop as agent_loop
    with patch("indexops.agent.loop.llm.chat", side_effect=RuntimeError("api down")), \
         patch("indexops.agent.loop._create_incident", return_value=1), \
         patch("indexops.agent.loop.execute") as mock_execute:
        result = await agent_loop.run_investigation(FakeMCP(), alert_id=None, run_id=1)
        assert result.status == "error"
        assert mock_execute.called


@pytest.mark.asyncio
async def test_evaluator_rejects_unsupported_evidence_and_loop_continues():
    from indexops.agent import loop as agent_loop
    bad_submit = LLMToolCall(id="1", name="submit_incident_report", arguments={
        "root_cause_category": "TRANSFORMATION_ERROR", "root_cause": "x",
        "confidence": 0.9, "severity": "high",
        "evidence": [{"finding": "made up", "source_tool": "never_called_tool"}],
        "recommended_action": "fix it",
    })
    good_submit = LLMToolCall(id="2", name="submit_incident_report", arguments={
        "root_cause_category": "NO_ISSUE", "root_cause": "fine", "confidence": 1.0,
        "severity": "low", "evidence": [], "recommended_action": "none",
    })
    with patch("indexops.agent.loop.llm.chat") as mock_chat, \
         patch("indexops.agent.loop._create_incident", return_value=1), \
         patch("indexops.agent.loop._log_step"), \
         patch("indexops.agent.loop._save_report"), \
         patch("indexops.agent.loop.execute"), \
         patch("indexops.agent.loop.validate_report") as mock_val:
        mock_val.side_effect = [
            MagicMock(passed_deterministic=False, verdict="rejected",
                      deterministic_notes="cites uncalled tool", evaluator_notes="", final_report={}),
            MagicMock(passed_deterministic=True, verdict="approve",
                      deterministic_notes="ok", evaluator_notes="ok", final_report={}),
        ]
        mock_chat.side_effect = [
            _mock_response(tool_calls=[bad_submit]),
            _mock_response(tool_calls=[good_submit]),
        ]
        result = await agent_loop.run_investigation(FakeMCP(), alert_id=None, run_id=1)
        assert result.status == "report_submitted"
        assert mock_val.call_count == 2


@pytest.mark.asyncio
async def test_iteration_cap_marks_incomplete():
    from indexops.agent import loop as agent_loop
    with patch("indexops.agent.loop.llm.chat") as mock_chat, \
         patch("indexops.agent.loop._create_incident", return_value=1), \
         patch("indexops.agent.loop._log_step"), \
         patch("indexops.agent.loop.execute"):
        mock_chat.return_value = _mock_response(text="I am still thinking", tool_calls=[])
        result = await agent_loop.run_investigation(FakeMCP(), alert_id=None, run_id=1)
        assert result.status == "incomplete"