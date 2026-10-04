"""Task: reflection / validation pass before a report is persisted.

Two checks, run in order:
1. Deterministic: every evidence item's source_tool must have actually been
   called in this investigation (per investigation_steps). Catches citing a
   tool that was never invoked.
2. LLM evaluator: a second, independent model call reviews the draft report
   against the real tool observations and must return APPROVE or a
   CORRECTED report. This is the actual "reflection" step the review asked for.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from indexops import llm_client as llm
from indexops.db import fetch_all

EVALUATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "evaluate_report",
        "description": "Approve the draft report, or return a corrected version if any claim is unsupported by the observations.",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["approve", "corrected"]},
                "notes": {"type": "string", "description": "Why approved, or what was wrong/fixed."},
                "corrected_report": {
                    "type": "object",
                    "description": "Only if verdict is 'corrected': the full corrected report, same shape as the original.",
                },
            },
            "required": ["verdict", "notes"],
        },
    },
}

EVALUATOR_SYSTEM = """\
You are a strict evidence auditor. You will be given a draft incident report
and the actual tool call observations from the investigation that produced it.

Check:
- Every evidence item's source_tool was actually called (listed below).
- Every number/id/percentage in the report matches something actually present
  in the observations, not invented.
- affected_ticket_count and affected_ticket_ids_sample actually match the
  observations, if present.

Call evaluate_report with verdict="approve" if everything is supported.
Call it with verdict="corrected" and a full corrected_report if anything is
unsupported, invented, or inconsistent with the observations.
"""


@dataclass
class ValidationResult:
    passed_deterministic: bool
    deterministic_notes: str
    verdict: str                 # "approve" | "corrected" | "evaluator_error"
    evaluator_notes: str
    final_report: dict[str, Any]


def _deterministic_check(incident_id: int, report: dict[str, Any]) -> tuple[bool, str]:
    rows = fetch_all(
        "SELECT DISTINCT tool_name FROM investigation_steps WHERE incident_id = %s",
        (incident_id,),
    )
    called = {r["tool_name"] for r in rows}
    bad = []
    for item in report.get("evidence", []) or []:
        src = item.get("source_tool")
        if src and src not in called:
            bad.append(src)
    if bad:
        return False, f"Evidence cites tool(s) never actually called: {sorted(set(bad))}. Tools actually called: {sorted(called)}."
    return True, "All cited source_tools were actually called."


def _evaluator_pass(incident_id: int, report: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    rows = fetch_all(
        """SELECT tool_name, tool_input, tool_output FROM investigation_steps
           WHERE incident_id = %s ORDER BY step_id""",
        (incident_id,),
    )
    observations = [
        {"tool": r["tool_name"], "input": r["tool_input"], "output": r["tool_output"]}
        for r in rows
    ]
    messages = [
        {"role": "system", "content": EVALUATOR_SYSTEM},
        {"role": "user", "content": json.dumps({
            "draft_report": report,
            "observations": observations,
        }, default=str)},
    ]
    try:
        response = llm.chat(
            messages, tools=[EVALUATOR_TOOL],
            tool_choice={"type": "function", "function": {"name": "evaluate_report"}},
        )
    except Exception as e:
        return "evaluator_error", f"Evaluator LLM call failed: {e}", report

    if not response.tool_calls:
        return "evaluator_error", "Evaluator did not return a structured verdict.", report

    args = response.tool_calls[0].arguments
    verdict = args.get("verdict", "evaluator_error")
    notes = args.get("notes", "")
    if verdict == "corrected" and args.get("corrected_report"):
        return verdict, notes, args["corrected_report"]
    return verdict, notes, report


def validate_report(incident_id: int, report: dict[str, Any]) -> ValidationResult:
    """Run both checks. Always returns a usable final_report (falls back to the
    original draft if the evaluator itself fails, so the loop never hard-crashes)."""
    passed, det_notes = _deterministic_check(incident_id, report)
    verdict, ev_notes, final_report = _evaluator_pass(incident_id, report)
    return ValidationResult(
        passed_deterministic=passed,
        deterministic_notes=det_notes,
        verdict=verdict,
        evaluator_notes=ev_notes,
        final_report=final_report,
    )