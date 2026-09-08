# Historical Incident: May 2026 Duplicate Ticket Spike

Summary: A retry mechanism in the ticket intake system did not
properly check for existing ticket_ids before resubmitting, causing
roughly 400 duplicate ticket entries over 2 days.

Impact: Inflated ticket counts in dashboards, some tickets double
worked by different agents.

Root cause: DUPLICATE_RECORD - missing idempotency check in the intake
retry logic.

Resolution: Added a deduplication step in validate_data that checks
ticket_id uniqueness before indexing. Removed duplicate records from
the index.

Confidence in this diagnosis: high.