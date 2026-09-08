# Runbook: Category / Team Mismatch

Symptom: data_quality_metrics shows mismatch_pct above 5%, while the
Airflow pipeline run itself shows SUCCESS.

Likely cause: the transform_data step's category-to-team mapping logic
was changed, corrupted, or misconfigured, causing tickets to be tagged
with an incorrect assigned_team even though the pipeline completes
without errors.

Recommended steps:
1. Identify affected ticket_ids by comparing source category against
   indexed assigned_team.
2. Verify the mapping logic in the transform step against the current
   data contract.
3. Correct the mapping and re-index only the affected records.
4. Re-run validate_index to confirm mismatch_pct returns to near 0.