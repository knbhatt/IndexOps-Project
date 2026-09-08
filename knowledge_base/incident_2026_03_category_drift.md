# Historical Incident: March 2026 Category Mapping Drift

Summary: A code change to the category-to-team mapping dictionary
introduced a typo, causing all "Network" category tickets to be mapped
to "Hardware" team for a period of 6 hours.

Impact: Approximately 18% of tickets during that window were misrouted.
NetOps had zero visibility into real network incidents during this time.

Root cause: TRANSFORMATION_ERROR - a manual edit to the mapping
dictionary was deployed without validation.

Resolution: Reverted the mapping change, re-ran the transform step for
affected tickets, re-indexed 540 corrected records. Added a validation
check to compare mapping keys against the data contract before deploy.

Confidence in this diagnosis: high (root cause confirmed via git blame
on the mapping change).