"""Task 14 — the IndexOps agent tool-calling loop.

Flow:
  user prompt (from an alert) -> LLM picks tool(s) over MCP -> tool results fed back
  -> LLM iterates -> LLM calls submit_incident_report -> loop saves report and ends.

An incident row is created at the START (status 'investigating') so that every
tool call can be logged to investigation_steps (FK -> incidents). Streamlit polls
that table for the live "streaming" view; no SSE needed.

submit_incident_report is a control-flow tool handled here (not on the MCP server):
it must terminate the loop AND write to the incident row created with this
investigation's context (alert_id/run_id), which the stdio MCP subprocess doesn't
hold. Its schema is still presented to the model exactly like a normal tool.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from indexops import llm_client as llm
from indexops.agent.validate import validate_report
from indexops.db import execute, fetch_one, dumps
from indexops.agent.prompt import SYSTEM_PROMPT, SUBMIT_TOOL, SUBMIT_TOOL_NAME

MAX_ITERATIONS = 15
TOOL_RESULT_CHAR_CAP = 8000

# Tools that accept an incident_id. The LLM must never supply it (it tends to guess
# the alert_id); the loop injects the real incident_id it created for this run.
REMEDIATION_TOOLS = {"reindex_affected_tickets", "trigger_pipeline_rerun"}


@dataclass
class InvestigationResult:
    incident_id: int
    status: str                       # 'report_submitted' | 'incomplete'
    report: dict[str, Any] | None
    run_id: int | None
    alert_id: int | None
    llm_calls: int
    tools_used: list[str] = field(default_factory=list)
    usage_totals: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def _load_alert_context(alert_id: int) -> dict | None:
    return fetch_one(
        """SELECT a.alert_id, a.run_id, a.severity, a.message, a.status,
                  r.dag_run_id, r.inject_type, r.inject_pct
           FROM alerts a LEFT JOIN pipeline_runs r ON r.run_id = a.run_id
           WHERE a.alert_id = %s""",
        (alert_id,),
    )


def _create_incident(alert_id: int | None, run_id: int | None) -> int:
    row = execute(
        """INSERT INTO incidents (alert_id, run_id, status)
           VALUES (%s, %s, 'investigating') RETURNING incident_id""",
        (alert_id, run_id),
    )
    return row["incident_id"]


def _log_step(incident_id: int, tool_name: str, tool_input: Any, tool_output: Any) -> None:
    execute(
        """INSERT INTO investigation_steps (incident_id, tool_name, tool_input, tool_output)
           VALUES (%s, %s, %s::jsonb, %s::jsonb)""",
        (incident_id, tool_name, dumps(tool_input), dumps(tool_output)),
    )


def _save_report(incident_id: int, report: dict[str, Any]) -> None:
    evidence = {
        "evidence": report.get("evidence", []),
        "affected_ticket_count": report.get("affected_ticket_count"),
        "affected_ticket_ids_sample": report.get("affected_ticket_ids_sample", []),
        "root_cause_category": report.get("root_cause_category"),
        "proposed_remediation_tool": report.get("proposed_remediation_tool", "none"),
    }
    execute(
        """UPDATE incidents
           SET status = 'report_submitted', root_cause = %s, confidence = %s,
               recommended_action = %s, evidence = %s::jsonb, report = %s::jsonb
           WHERE incident_id = %s""",
        (report.get("root_cause"), report.get("confidence"), report.get("recommended_action"),
         dumps(evidence), dumps(report), incident_id),
    )


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
def _build_user_message(alert_ctx: dict | None, run_id: int | None, question: str | None) -> str:
    if alert_ctx:
        return (
            f"A data-quality alert has fired and needs investigation.\n"
            f"- alert_id: {alert_ctx['alert_id']}\n"
            f"- severity: {alert_ctx['severity']}\n"
            f"- message: {alert_ctx['message']}\n"
            f"- affected run_id: {alert_ctx['run_id']} (Airflow dag_run_id: {alert_ctx.get('dag_run_id')})\n\n"
            f"Investigate this alert end-to-end, determine the root cause with evidence, and submit an incident report."
        )
    if question:
        target = f" Focus on run_id {run_id}." if run_id else ""
        return (
            "Interactive investigation (no alert pre-selected). The operator asked:\n"
            f'"{question}"\n\n'
            "Use your tools to inspect the current pipeline state, metrics, index health, "
            "and knowledge base. Determine the root cause with evidence and submit an "
            "incident report."
            + target
        )
    if run_id is not None:
        return (f"Investigate the data quality of pipeline run_id {run_id}. Determine whether the indexed data is "
                f"correct, find the root cause of any issue with evidence, and submit an incident report.")
    return ("Investigate the most recent pipeline run for data-quality problems, find the root cause with evidence, "
            "and submit an incident report.")


def _accumulate_usage(totals: dict[str, int], usage: dict[str, int]) -> None:
    for k, v in usage.items():
        totals[k] = totals.get(k, 0) + (v or 0)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
async def run_investigation(
    mcp,
    alert_id: int | None = None,
    run_id: int | None = None,
    question: str | None = None,
    on_step: Callable[[str, dict], None] | None = None,
) -> InvestigationResult:
    """Run one full investigation. `mcp` is a started MCPConnection.

    Provide alert_id (preferred, Task 17), or run_id, or a free-form question.
    """
    alert_ctx = _load_alert_context(alert_id) if alert_id is not None else None
    if alert_ctx and run_id is None:
        run_id = alert_ctx["run_id"]

    incident_id = _create_incident(alert_id, run_id)

    # Static prefix first (system prompt + tool defs) -> cacheable. Dynamic content last.
    tools = llm.mcp_tools_to_llm_tools(mcp.tools) + [SUBMIT_TOOL]
    tool_names = {t["function"]["name"] for t in tools}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(alert_ctx, run_id, question)},
    ]

    llm_calls = 0
    tools_used: list[str] = []
    usage_totals: dict[str, int] = {}

    for iteration in range(MAX_ITERATIONS):
        last_turn = iteration == MAX_ITERATIONS - 1
        # On the final allowed turn, force the model to finalize.
        tool_choice = (
            {"type": "function", "function": {"name": SUBMIT_TOOL_NAME}} if last_turn else "auto"
        )
        try:
            response = llm.chat(messages, tools=tools, tool_choice=tool_choice)
        except Exception as e:
            # e.g. free-tier quota (429) exhausted after retries, or a network error.
            # Persist what we have rather than crashing so the UI still shows partial progress.
            execute(
                "UPDATE incidents SET status = 'error', root_cause = %s WHERE incident_id = %s",
                (f"Investigation aborted: LLM call failed ({type(e).__name__}: {e})", incident_id),
            )
            return InvestigationResult(
                incident_id=incident_id, status="error", report=None,
                run_id=run_id, alert_id=alert_id, llm_calls=llm_calls,
                tools_used=tools_used, usage_totals=usage_totals,
            )
        llm_calls += 1
        _accumulate_usage(usage_totals, response.usage)
        messages.append(response.raw_message)

        if not response.tool_calls:
            # Model replied with prose instead of finalizing: nudge it once and continue.
            messages.append({
                "role": "user",
                "content": ("Do not answer in prose. If you have enough evidence, call "
                            "submit_incident_report now. Otherwise call more investigation tools."),
            })
            continue

        submitted: dict[str, Any] | None = None
        for tc in response.tool_calls:
            if tc.name == SUBMIT_TOOL_NAME:
                validation = validate_report(incident_id, tc.arguments)
                _log_step(incident_id, "evaluate_report", tc.arguments, {
                    "passed_deterministic": validation.passed_deterministic,
                    "deterministic_notes": validation.deterministic_notes,
                    "verdict": validation.verdict,
                    "evaluator_notes": validation.evaluator_notes,
                })
                if not validation.passed_deterministic or validation.verdict == "evaluator_error":
                    # Give the model a chance to fix it within its remaining iterations.
                    messages.append(llm.tool_result_message(tc, {
                        "status": "rejected",
                        "reason": validation.deterministic_notes if not validation.passed_deterministic else validation.evaluator_notes,
                        "instruction": "Correct the report (use only tools you actually called) and call submit_incident_report again.",
                    }))
                    if on_step:
                        on_step("evaluate_report", {"rejected": True, "notes": validation.deterministic_notes})
                    continue
                final_report = validation.final_report
                execute(
                    "UPDATE incidents SET validation_passed = %s, validation_notes = %s WHERE incident_id = %s",
                    (True, validation.evaluator_notes, incident_id),
                )
                _save_report(incident_id, final_report)
                if on_step:
                    on_step(tc.name, final_report)
                messages.append(llm.tool_result_message(
                    tc, {"status": "saved", "incident_id": incident_id}))
                submitted = final_report
                continue

            if tc.name in REMEDIATION_TOOLS:
                # Override any LLM-supplied incident_id with the real one for this investigation.
                tc.arguments["incident_id"] = incident_id
            if tc.name not in tool_names:
                result: Any = {"error": f"unknown tool '{tc.name}'"}
            else:
                try:
                    result = await mcp.call_tool(tc.name, tc.arguments)
                except Exception as e:  # a tool raising must not crash the whole investigation
                    result = {"error": f"tool '{tc.name}' failed: {e}"}
            tools_used.append(tc.name)
            _log_step(incident_id, tc.name, tc.arguments, result)
            if on_step:
                on_step(tc.name, {"input": tc.arguments, "output": result})
            payload = dumps(result)
            if len(payload) > TOOL_RESULT_CHAR_CAP:
                payload = payload[:TOOL_RESULT_CHAR_CAP] + "...<truncated>"
            messages.append(llm.tool_result_message(tc, payload))

        if submitted is not None:
            return InvestigationResult(
                incident_id=incident_id, status="report_submitted", report=submitted,
                run_id=run_id, alert_id=alert_id, llm_calls=llm_calls,
                tools_used=tools_used, usage_totals=usage_totals,
            )

    # Fell through without a submitted report (shouldn't happen due to forced last turn).
    execute("UPDATE incidents SET status = 'incomplete' WHERE incident_id = %s", (incident_id,))
    return InvestigationResult(
        incident_id=incident_id, status="incomplete", report=None,
        run_id=run_id, alert_id=alert_id, llm_calls=llm_calls,
        tools_used=tools_used, usage_totals=usage_totals,
    )
