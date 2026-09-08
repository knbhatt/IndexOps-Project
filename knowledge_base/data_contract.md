# Tickets Data Contract

Every ticket must have: ticket_id (unique), description (non-empty text),
category (one of: Network, Hardware, Software, Access, Email),
priority (Low, Medium, High, Critical), assigned_team, status.

Correct category-to-team mapping:
Network -> NetOps
Hardware -> Field Support
Software -> App Support
Access -> IAM Team
Email -> Messaging Team

Any ticket whose assigned_team does not match this mapping for its
category is considered a data quality violation.