# Runbook: Missing Required Fields

Symptom: validate_data drops a higher-than-usual number of records, or
data_quality_metrics shows a spike in incomplete tickets.

Likely cause: tickets submitted without a category or description,
often from a frontend form validation gap or bulk-import script.

Recommended steps:
1. Check failed_records / validation logs for which fields are missing.
2. Trace back to the source of those records (manual entry vs bulk
   import) to identify the root cause.
3. Backfill missing fields where possible, or flag records for manual
   review.