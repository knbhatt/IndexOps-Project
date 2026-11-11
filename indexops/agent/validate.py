"""Reflection / validation pass for incident reports.

Addresses L2 review items: (1) no reflection step, (4) unvalidated structured output.

Layers, in the order the loop applies them:
  1. Schema check (Pydantic): the report has the right fields, types and ranges.
  2. Deterministic evidence check: every cited source_tool was actually called
     successfully this investigation, and affected_ticket_count appears in a tool result.
  3. LLM evaluator: an independent model call audits the draft against the real
     tool observations and either approves it or returns a corrected report.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from indexops import llm_client as llm
from indexops.agent.prompt import ROOT_CAUSE_CATEGORIES


class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    finding: str
    source_tool: str
    detail: Any = None


class IncidentReport(BaseModel):
    model_config = ConfigDict(extra="allow")  # keep optional fields like proposed_remediation_tool
    root_cause_category: str
    root_cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    severity: Literal["low", "medium", "high", "critical"]
    evidence: list[Evidence]
    recommended_action: str
    affected_ticket_count: int = Field(default=0, ge=0)

    @field_validator("root_cause_category")
    @classmethod
    def _known_category(cls, v: str) -> str:
        if v not in ROOT_CAUSE_CATEGORIES:
            raise ValueError(f"must be one of {ROOT_CAUSE_CATEGORIES}")
        return v

    @field_validator("affected_ticket_count", mode="before")
    @classmethod
    def _none_to_zero(cls, v: Any) -> Any:
        return 0 if v is None else v


def _fmt_errors(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    return "schema: " + "; ".join(parts)


def check_report(
    report: Any, called_tools: set[str], observations_text: str, queued_scope: int | None = None
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return (clean_report_or_None, problems). None means the schema itself is invalid.

    queued_scope = ticket_count of the pending reindex action queued for this incident (or None).
    """
    try:
        model = IncidentReport.model_validate(report)
    except ValidationError as e:
        return None, [_fmt_errors(e)]

    problems: list[str] = []
    for ev in model.evidence:
        if ev.source_tool not in called_tools:
            problems.append(
                f"evidence cites tool '{ev.source_tool}' which was never successfully called "
                f"(successfully called: {sorted(called_tools)})"
            )
    n = model.affected_ticket_count
    if n > 0 and str(n) not in observations_text:
        problems.append(f"affected_ticket_count={n} does not appear in any tool result")
    clean = model.model_dump()
    if clean.get("proposed_remediation_tool") == "reindex_affected_tickets":
        if queued_scope is None:
            problems.append("report proposes reindex_affected_tickets but no reindex action is queued; "
                            "call reindex_affected_tickets first")
        elif queued_scope != n:
            problems.append(f"report says affected_ticket_count={n} but the queued reindex action covers "
                            f"{queued_scope} tickets; set affected_ticket_count to the queued scope "
                            f"({queued_scope}) or explain the difference")
    return clean, problems


EVALUATOR_SYSTEM = """\
You are a strict evidence auditor for an incident report written by an AI agent.
You receive the draft report and the real tool observations from the investigation.

Check that every number, id, percentage and claim in the report is supported by the
observations, and that the recommended_action follows from them. Do not nitpick wording.

Call evaluate_report exactly once:
- verdict="approve" if everything is supported.
- verdict="corrected" if something is unsupported or wrong; include corrected_report with the
  SAME fields as the draft, fixing only what the observations do not support.
"""

EVALUATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "evaluate_report",
        "description": "Approve the draft incident report or return a corrected version.",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["approve", "corrected"]},
                "notes": {"type": "string", "description": "Why approved, or what was wrong."},
                "corrected_report": {"type": "object", "description": "Only when verdict is corrected."},
            },
            "required": ["verdict", "notes"],
        },
    },
}


class EvaluatorVerdict(BaseModel):
    """Strict shape of the evaluator's answer. Anything else is treated as invalid."""
    model_config = ConfigDict(extra="ignore")
    verdict: Literal["approve", "corrected"]
    notes: str = ""
    corrected_report: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _corrected_needs_report(self):
        if self.verdict == "corrected" and not self.corrected_report:
            raise ValueError("verdict 'corrected' requires a non-empty corrected_report")
        return self


def _compact(observations: list[dict[str, Any]], per_output: int = 3000, total: int = 40000) -> str:
    items = []
    for o in observations:
        out = json.dumps(o.get("output"), default=str)
        items.append({"tool": o.get("tool"), "input": o.get("input"), "output": out[:per_output]})
    return json.dumps(items, default=str)[:total]


def evaluate_report(
    report: dict[str, Any], observations: list[dict[str, Any]]
) -> tuple[str, str, dict[str, Any] | None]:
    """Independent LLM audit. Returns (verdict, notes, corrected_report_or_None).

    verdict is one of:
      'approve'     - evaluator found every claim supported
      'corrected'   - evaluator returned a complete corrected report (caller must re-validate it)
      'invalid'     - evaluator answered, but not in the required shape (retried once)
      'unavailable' - the evaluator call itself failed (quota/network); report stays unvalidated
    """
    messages = [
        {"role": "system", "content": EVALUATOR_SYSTEM},
        {"role": "user", "content": json.dumps({
            "draft_report": report,
            "observations": json.loads(_compact(observations)) if observations else [],
        }, default=str)},
    ]
    last_problem = "no attempt made"
    for _attempt in range(2):  # one retry for a malformed answer
        try:
            resp = llm.chat(
                messages, tools=[EVALUATOR_TOOL],
                tool_choice={"type": "function", "function": {"name": "evaluate_report"}},
            )
        except Exception as e:  # rate limit, network, etc. -> do not retry (protects free-tier quota)
            return "unavailable", f"evaluator call failed: {type(e).__name__}: {e}", None

        call = next((tc for tc in resp.tool_calls if tc.name == "evaluate_report"), None)
        if call is None:
            last_problem = "evaluator returned no structured verdict"
            continue
        try:
            parsed = EvaluatorVerdict.model_validate(call.arguments)
        except ValidationError as e:
            last_problem = f"evaluator verdict failed validation: {_fmt_errors(e)}"
            continue
        if parsed.verdict == "approve":
            return "approve", parsed.notes, None
        return "corrected", parsed.notes, parsed.corrected_report
    return "invalid", last_problem, None
