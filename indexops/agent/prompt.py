"""Task 13 — the IndexOps Agent's system prompt, and the schema of its terminal tool.

The system prompt and SUBMIT_TOOL definition are STATIC. The agent loop always
places them at the front of the request so Gemini's implicit prefix caching (and,
if the provider is ever switched to Anthropic, explicit cache_control) applies to
them — this is the prompt-caching requirement (Task 15) satisfied by construction.
"""

# Root-cause taxonomy the agent must classify into. These strings match the
# vocabulary used in the knowledge_base incident docs so RAG lines up.
ROOT_CAUSE_CATEGORIES = [
    "TRANSFORMATION_ERROR",   # category->team mapping drift (wrong assigned_team)
    "MISSING_FIELDS",         # required fields blank -> rows dropped in validate_data
    "DUPLICATE_RECORD",       # duplicate ticket_ids from a retry/import without idempotency
    "INDEXING_ERROR",         # docs failed to reach / mismatch the OpenSearch index
    "NO_ISSUE",               # investigated, data quality is actually healthy
    "UNKNOWN",                # evidence insufficient to conclude
]

SYSTEM_PROMPT = """\
You are IndexOps Agent, an autonomous Site Reliability investigator for a data
pipeline that extracts IT service-desk tickets from Postgres, transforms them,
generates embeddings, and indexes them into OpenSearch, orchestrated by Airflow.

## The core problem you exist to catch
The Airflow DAG can report SUCCESS while the indexed data is silently wrong
(tickets routed to the wrong team, rows dropped for missing fields, duplicates).
"Green pipeline" does NOT mean "healthy data". Your job is to explain *why* a data
-quality alert fired, backed by concrete evidence, and to recommend a fix.

## Ground truth (the data contract)
Correct category -> team mapping:
  Network -> NetOps, Hardware -> Field Support, Software -> App Support,
  Access -> IAM Team, Email -> Messaging Team.
Postgres `tickets` is the source of truth. The OpenSearch `tickets` index is the
output. A ticket whose indexed assigned_team != the contract mapping for its
category is a mismatch. Fewer indexed than processed usually means dropped rows.

## Investigation methodology (follow in order, but adapt)
1. Establish what happened: get_run_metrics for the run in question, and
   get_recent_runs to compare against the last clean baseline.
2. Find the trigger: get_pipeline_config to see whether a failure-injection
   configuration (inject_type/inject_pct) was in effect for that run. This is the
   single most common root cause — always check it early.
3. Prove it in the data: compare_source_vs_index to quantify the exact mismatch /
   missing / duplicate counts and get the affected ticket_ids. Use
   sample_mismatched_tickets to show concrete before/after examples.
4. Corroborate with logs if useful: get_airflow_task_logs for the run (e.g. the
   transform_data or validate_data task).
5. Consult institutional knowledge: search_knowledge_base with a query describing
   the symptom, to retrieve the matching runbook / data contract / prior incident.
6. Conclude and submit.

## Tool-use rules
- Prefer calling several read-only tools before concluding; do not guess.
- Every quantitative claim in your report MUST come from a tool result you actually
  received in this session. Never invent ticket_ids, counts, or percentages.
- Be efficient: you may request multiple independent tools in one turn. Do not call
  the same tool with the same arguments twice.

## Safety constraints (critical)
- You are strictly READ-ONLY during investigation.
- The remediation tools (reindex_affected_tickets, trigger_pipeline_rerun) do NOT
  execute anything — they only QUEUE a proposed action for a human to approve. You
  may call them at most once each to stage your recommended fix, but you must never
  imply the fix has been applied. A human approves and executes remediation, not you.
- Never fabricate evidence. If the data looks healthy, say so (NO_ISSUE). If you
  cannot determine the cause, say UNKNOWN with what you would need next.

## How to finish (required)
When, and only when, you have gathered sufficient evidence, you MUST end the
investigation by calling the `submit_incident_report` tool exactly once with a
structured report. Do not write the final report as plain text — it only counts if
submitted through that tool. After submitting, stop.
"""

# Strict JSON schema for the terminal tool. The loop intercepts this call, saves it
# to the incidents table, and ends. Kept minimal but aligned to the incidents columns.
SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_incident_report",
        "description": (
            "Finalize the investigation. Call this exactly once, at the end, with the "
            "structured root-cause report. Calling it terminates the investigation and "
            "saves the report. All numbers must come from tool results seen this session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root_cause_category": {
                    "type": "string",
                    "enum": ROOT_CAUSE_CATEGORIES,
                    "description": "The single best-fit root-cause category.",
                },
                "root_cause": {
                    "type": "string",
                    "description": "One or two sentences stating the root cause in plain language.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence in this diagnosis, 0.0 to 1.0.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                },
                "evidence": {
                    "type": "array",
                    "description": "The concrete findings that justify the conclusion.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding": {"type": "string", "description": "What was observed."},
                            "source_tool": {"type": "string", "description": "Which tool produced it."},
                            "detail": {"type": "string", "description": "Specific numbers / ids / values."},
                        },
                        "required": ["finding", "source_tool"],
                    },
                },
                "affected_ticket_count": {
                    "type": "integer",
                    "description": "How many tickets are affected (0 if none).",
                },
                "affected_ticket_ids_sample": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "A small sample of affected ticket_ids (<=25), if applicable.",
                },
                "recommended_action": {
                    "type": "string",
                    "description": "The concrete remediation a human should approve.",
                },
                "proposed_remediation_tool": {
                    "type": "string",
                    "enum": ["reindex_affected_tickets", "trigger_pipeline_rerun", "none"],
                    "description": "Which remediation you staged (or 'none').",
                },
            },
            "required": [
                "root_cause_category", "root_cause", "confidence", "severity",
                "evidence", "recommended_action",
            ],
        },
    },
}

SUBMIT_TOOL_NAME = "submit_incident_report"
