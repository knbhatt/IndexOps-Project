"""Single source of truth for the tickets data contract.

Imported by the data generator, the Airflow DAG and the agent tools so the
category-to-team mapping can never drift between them.
"""

CATEGORIES = ["Network", "Hardware", "Software", "Access", "Email"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]

TEAMS_BY_CATEGORY = {
    "Network": "NetOps",
    "Hardware": "Field Support",
    "Software": "App Support",
    "Access": "IAM Team",
    "Email": "Messaging Team",
}

UNASSIGNED_TEAM = "Unassigned"

# Alert threshold used by analyze_index_health (matches the runbooks).
MISMATCH_ALERT_THRESHOLD_PCT = 5.0

# Embedding model shared by the tickets and knowledge_base indexes.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

TICKETS_INDEX = "tickets"
KNOWLEDGE_BASE_INDEX = "knowledge_base"


def expected_team(category: str) -> str:
    """Return the team a ticket of this category must be routed to."""
    return TEAMS_BY_CATEGORY.get(category, UNASSIGNED_TEAM)
